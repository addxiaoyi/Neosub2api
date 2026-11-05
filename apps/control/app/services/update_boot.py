"""两阶段启动看门狗。

换目录前写 data/updates/pending.json（boot_attempted=false）；新进程启动第一件事把它
置 true。新版本健康通过 → 删除 pending；若新进程起不来，下一次启动看到 true 就自动回滚
目录再退出重启 —— 这样「新版本根本起不来」也不会卡在半坏状态。

这套机制只在进程被外部（restart 策略 / 服务管理器）拉起时生效：换完目录后执行器主动退出，
真正的健康确认由新进程自己做。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..config import REPO_ROOT
from . import ops_lock
from . import update_executor as ex

CONFIRM_TIMEOUT_SEC = ex.HEALTH_TIMEOUT_SEC
CONFIRM_INTERVAL_SEC = 1.0


def work_dir(settings: Any) -> Path:
    return Path(settings.data_dir) / "updates"


def reconcile(
    settings: Any,
    *,
    repo_root: Path = REPO_ROOT,
    exit_fn: Callable[[Any], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """在 create_app 最开头调用；返回 {"pending": bool, "rolled_back": bool}。"""
    work = work_dir(settings)
    work.mkdir(parents=True, exist_ok=True)
    pending_file = work / "pending.json"
    job_file = work / "job.json"
    # 上一轮更新进程已被重启策略杀掉，锁必然残留；不清掉会挡住第一次备份
    ops_lock.release_stale(Path(settings.db_path).parent / "backups", "update:")
    pending = ex.read_json(pending_file)
    if not pending:
        job = ex.read_json(job_file)
        if job.get("state") == "running":
            job.update(
                state="failed",
                finished_at=int(time.time()),
                error="更新进程在重启前退出，任务未完成；若目录已交换请检查当前版本",
            )
            ex.write_json(job_file, job)
        return {"pending": False, "rolled_back": False}

    job = ex.read_json(job_file)
    if not pending.get("boot_attempted"):
        pending["boot_attempted"] = True
        ex.write_json(pending_file, pending)
        return {"pending": True, "rolled_back": False}

    moves = pending.get("moves") or []
    if not moves:
        # 手动回滚后再次启动失败：没有可还原的历史目录，绝不重启，避免无限循环
        pending_file.unlink(missing_ok=True)
        job.update(
            state="failed",
            finished_at=int(time.time()),
            error="回滚后启动仍未成功，且没有可还原的旧目录，需要人工介入",
        )
        ex.write_json(job_file, job)
        return {"pending": False, "rolled_back": False}

    try:
        ex.rollback_moves(moves, sleep=sleep)
        ex.clear_pycache(Path(repo_root) / ex.APP_REL)
        job.update(
            state="failed",
            step="rollback",
            progress=ex.STEP_PROGRESS["rollback"],
            finished_at=int(time.time()),
            error="新版本启动失败，已自动回滚到旧版本并重新启动",
        )
        ex.write_json(job_file, job)
        (work / "last_swap.json").unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - 回滚失败必须留痕，并保留 pending 让下次启动重试
        job.update(
            state="failed",
            step="rollback",
            progress=ex.STEP_PROGRESS["rollback"],
            finished_at=int(time.time()),
            error=f"自动回滚失败：{exc}；已保留 pending.json，下次启动会重试",
        )
        ex.write_json(job_file, job)
        return {"pending": False, "rolled_back": False}
    pending_file.unlink(missing_ok=True)
    (exit_fn or ex.restart_for)(settings)
    return {"pending": False, "rolled_back": True}


def arm_confirm(settings: Any, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """新进程在后台自证健康：能访问自己的 /healthz 就删除 pending 并收尾任务。"""
    work = work_dir(settings)
    pending_file = work / "pending.json"
    if not pending_file.is_file():
        return
    pending = ex.read_json(pending_file)
    if not pending.get("boot_attempted"):
        return
    threading.Thread(
        target=_confirm,
        args=(settings, pending, sleep),
        name="unified-update-confirm",
        daemon=True,
    ).start()


def _confirm(settings: Any, pending: dict[str, Any], sleep: Callable[[float], None]) -> None:
    deadline = time.monotonic() + CONFIRM_TIMEOUT_SEC
    port = int(getattr(settings, "port", 8000))
    while time.monotonic() < deadline:
        if ex.health_ok(port):
            break
        sleep(CONFIRM_INTERVAL_SEC)
    else:
        # 起得来但健康接口一直不通：保留 pending，下一次重启由看门狗回滚
        return
    job_file = work_dir(settings) / "job.json"
    job = ex.read_json(job_file)
    job.update(
        state="done",
        step="rollback" if str(pending.get("reason") or "") == "rollback" else "health",
        progress=100,
        finished_at=int(time.time()),
        error="",
    )
    ex.write_json(job_file, job)
    (work_dir(settings) / "pending.json").unlink(missing_ok=True)
