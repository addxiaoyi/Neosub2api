"""进程内更新执行器：下载 → 验签（由 feed 完成）→ 逐文件校验 → stage → 停服 → 换目录 → 启动 → 健康/回滚。

为什么必须换部署形态：容器里代码在镜像层，光改文件不重建也能跑，但一重建就回退。
所以 compose 把代码目录绑挂进来，换完后进程主动退出，由 restart: unless-stopped 用新代码拉起。
裸机则由服务管理器重启。两条路都不具备时 updater 直接 can_apply=false，不做半套更新。
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from ..api_utils import mask_text, safe_url
from ..config import REPO_ROOT
from . import ops_lock
from . import update_manifest as mf
from .update_feed import FeedError, assert_transport, detect_container

KEEP_OLD_DIRS = 2
HEALTH_TIMEOUT_SEC = 60.0
HEALTH_INTERVAL_SEC = 1.0
RENAME_ATTEMPTS = 3
RENAME_DELAY_SEC = 2.0
LOG_TAIL_MAX = 200
LOG_LINE_MAX = 500
DOWNLOAD_CHUNK = 256 * 1024

APP_REL = Path("apps") / "control" / "app"
WEB_REL = Path("apps") / "web" / "out"
SCRIPT_REL = Path("scripts") / "backup_core.py"
CHANGELOG_REL = Path("CHANGELOG.md")

STEP_PROGRESS = {
    "download": 10,
    "verify": 25,
    "stage": 45,
    "stop": 55,
    "swap": 65,
    "start": 80,
    "health": 95,
    "rollback": 70,
}


def empty_job() -> dict[str, Any]:
    return {
        "state": "idle",
        "step": "",
        "progress": 0,
        "started_at": 0,
        "finished_at": 0,
        "error": "",
        "log_tail": [],
    }


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写：先写 .tmp 再 replace，断电/重启时不会读到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def move_path(src: Path, dst: Path, *, sleep: Callable[[float], None] = time.sleep,
              attempts: int = RENAME_ATTEMPTS, delay: float = RENAME_DELAY_SEC) -> None:
    """同卷 rename；跨卷（容器里 /data 与 /repo 常是不同的卷）回落到拷贝。

    Windows 上杀毒/索引会短暂持有句柄，rename 抛 PermissionError：按需求重试 3 次、
    每次间隔 2s，仍失败才向上抛。
    """
    for attempt in range(1, attempts + 1):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            sleep(delay)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                if src.is_dir():
                    shutil.copytree(src, dst)
                    shutil.rmtree(src)
                else:
                    shutil.copy2(src, dst)
                    src.unlink()
                return
            raise


def rollback_moves(moves: list[dict[str, Any]], *, sleep: Callable[[float], None] = time.sleep) -> None:
    """按逆序把每个目标还原；rename/overlay/file 三种换法各有各的还原方式。"""
    for move in reversed(moves):
        target = Path(move["target"])
        old = Path(move["old"]) if move.get("old") else None
        mode = move.get("mode")
        if mode == "rename":
            if old is None or not old.exists():
                continue
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            move_path(old, target, sleep=sleep)
        elif mode == "overlay":
            if old is None or not old.exists():
                continue
            _clear_dir(target)
            shutil.copytree(old, target, dirs_exist_ok=True)
        elif mode == "file":
            if old is None or not old.exists():
                continue
            shutil.copy2(old, target)


def _clear_dir(path: Path) -> None:
    if not path.is_dir():
        return
    for entry in path.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def prune_olds(parent: Path, prefix: str, keep: int = KEEP_OLD_DIRS) -> None:
    """保留最近 keep 个 <prefix><时间戳>，其余删掉，免得每升级一次就多占一份代码。"""
    try:
        entries = sorted(
            (p for p in parent.iterdir() if p.name.startswith(prefix)),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError:
        return
    for stale in entries[keep:]:
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
        else:
            stale.unlink(missing_ok=True)


def clear_pycache(root: Path) -> None:
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def health_ok(port: int, *, timeout: float = 2.0) -> bool:
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= int(response.getcode() or 0) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def restart_service(name: str) -> None:
    """裸机重启：Windows 用 sc（stop 后延迟 start，本进程会被 stop 杀掉）；Linux 用 systemctl restart。"""
    service = (name or "").strip()
    if not service:
        raise FeedError("未配置 UNIFIED_SERVICE_NAME，无法重启服务")
    if sys.platform == "win32":
        subprocess.run(["sc", "stop", service], check=False, capture_output=True, text=True)
        subprocess.Popen(
            ["cmd", "/c", "timeout", "3", ">", "nul", "&", "sc", "start", service],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            close_fds=True,
        )
        return
    subprocess.run(["systemctl", "restart", service], check=False, capture_output=True, text=True)


def restart_container_process() -> None:
    """让进程优雅退出，交给 restart: unless-stopped 用新代码拉起。"""
    signal.raise_signal(signal.SIGTERM)
    time.sleep(1.0)
    os._exit(0)


def restart_for(settings: Any) -> None:
    if detect_container():
        restart_container_process()
        return
    restart_service(getattr(settings, "service_name", ""))


class UpdateExecutor:
    """单例；job 状态落盘到 data/updates/job.json，重启后还能看到上一轮结果。"""

    def __init__(
        self,
        settings: Any,
        *,
        repo_root: Path = REPO_ROOT,
        restart: Callable[[Any], None] | None = None,
        health: Callable[[int], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        health_timeout: float = HEALTH_TIMEOUT_SEC,
        download: Callable[[str, Path, int], None] | None = None,
    ):
        self.settings = settings
        self.root = Path(repo_root)
        self.work = Path(settings.data_dir) / "updates"
        self.job_file = self.work / "job.json"
        self.pending_file = self.work / "pending.json"
        self.last_swap_file = self.work / "last_swap.json"
        self.sleep = sleep
        self.health_timeout = health_timeout
        self._restart = restart or restart_for
        self._health = health or health_ok
        self._download = download or self._http_download
        self._lock = threading.Lock()
        self._job = empty_job()
        saved = read_json(self.job_file)
        if saved:
            self._job.update({key: saved[key] for key in self._job if key in saved})

    # ---------- 对外 ----------

    def status(self) -> dict[str, Any]:
        with self._lock:
            job = dict(self._job)
        job["log_tail"] = job["log_tail"][-LOG_TAIL_MAX:]
        return job

    def apply(self, manifest: dict[str, Any], manifest_url: str) -> dict[str, Any]:
        """启动后台任务；抢锁在请求线程里做，抢不到立刻 409，不用等线程。"""
        if self.status()["state"] == "running":
            raise ops_lock.OpsBusy("已有在线更新任务在进行中，等它结束再试")
        lock = ops_lock.acquire(self.backup_dir, f"update:{manifest['version']}")
        started = int(time.time())
        with self._lock:
            self._job = empty_job()
            self._job.update(state="running", step="download", progress=0, started_at=started)
        self._persist()
        thread = threading.Thread(
            target=self._run_locked,
            args=(manifest, manifest_url, lock),
            name="unified-update",
            daemon=True,
        )
        thread.start()
        return self.status()

    def rollback(self) -> dict[str, Any]:
        """手动回滚到最近一次换目录前的版本。"""
        if self.status()["state"] == "running":
            raise ops_lock.OpsBusy("更新任务正在运行，不能同时回滚")
        last = read_json(self.last_swap_file)
        moves = last.get("moves") or []
        if not moves:
            raise mf.UpdateError("没有可回滚的历史版本（还没有通过在线更新换过目录）")
        lock = ops_lock.acquire(self.backup_dir, "update:rollback")
        self._mark("rollback")
        try:
            rollback_moves(moves, sleep=self.sleep)
            clear_pycache(self.root / APP_REL)
            self.last_swap_file.unlink(missing_ok=True)
            # 回滚后如果旧版本起不来，没有可再还原的目录，所以 pending 只作「已启动确认」用
            self._arm_pending(str(last.get("version") or ""), reason="rollback")
            self._restart(self.settings)
        except Exception as exc:  # noqa: BLE001 - 回滚失败也要留下可读状态
            self._fail(f"回滚失败：{exc}")
            ops_lock.release(lock)
            raise
        self._finish_after_health(lock, rolled_back=True)
        return self.status()

    # ---------- 内部 ----------

    @property
    def backup_dir(self) -> Path:
        return Path(self.settings.db_path).parent / "backups"

    def _run_locked(self, manifest: dict[str, Any], manifest_url: str, lock: Path) -> None:
        version = str(manifest["version"])
        archive = self.work / version / "release.zip"
        stage = self.work / version / "stage"
        try:
            self._mark("download")
            self._log(f"开始更新到 {version}，发布源 {safe_url(manifest_url)}")
            self._download(mf.archive_url(manifest_url), archive, int(self.settings.update_max_bytes))
            self._log(f"下载完成：{archive.stat().st_size} 字节")

            self._mark("verify")
            mf.verify_archive(archive, manifest, int(self.settings.update_max_bytes))
            self._log("签名与逐文件 sha256 校验通过")

            self._mark("stage")
            mf.extract_archive(archive, manifest, stage)
            self._log(f"已解到临时目录 {stage}")

            self._mark("stop")
            stamp = self._stamp()
            old_app = (self.root / APP_REL).with_name(f"app.old-{stamp}")
            self._arm_pending(version, rollback_dir=str(old_app))
            self._swap(stage, stamp, version)
            # 交换完成才知道每个目标的旧路径，补进 pending 供看门狗整组回滚。
            # boot_attempted 必须保持 false：由新进程自己置 true，否则它一启动就会
            # 把自己当成「上一轮已尝试过但没起来」而立刻回滚。
            self._arm_pending(version, rollback_dir=str(old_app), moves=self._swap_moves())
            clear_pycache(self.root / APP_REL)
            self._log("目录已交换，旧版本保留为 *.old-<ts>")

            self._mark("start")
            self._restart(self.settings)
            self._finish_after_health(lock, rolled_back=False)
        except Exception as exc:  # noqa: BLE001 - 任何一步失败都要落成可读状态
            self._log(f"失败：{exc}")
            # 只要有换过目录（含换到一半），last_swap 里就有已完成的步骤，逐条还原
            moves = read_json(self.last_swap_file).get("moves") or []
            if moves:
                try:
                    self._mark("rollback")
                    rollback_moves(moves, sleep=self.sleep)
                    clear_pycache(self.root / APP_REL)
                    self.pending_file.unlink(missing_ok=True)
                    self._log("已回滚到交换前的目录")
                except Exception as rollback_exc:  # noqa: BLE001
                    self._log(f"回滚也失败了：{rollback_exc}")
            self._fail(f"更新失败：{exc}")
            ops_lock.release(lock)
        finally:
            self._persist()

    def _finish_after_health(self, lock: Path, *, rolled_back: bool) -> None:
        self._mark("health")
        deadline = time.monotonic() + self.health_timeout
        while time.monotonic() < deadline:
            if self._health(int(self.settings.port)):
                self.pending_file.unlink(missing_ok=True)
                with self._lock:
                    self._job.update(
                        state="done",
                        step="rollback" if rolled_back else "health",
                        progress=100,
                        finished_at=int(time.time()),
                        error="",
                    )
                ops_lock.release(lock)
                return
            self.sleep(HEALTH_INTERVAL_SEC)
        self._log(f"健康检查 {self.health_timeout:.0f}s 未通过")
        moves = read_json(self.last_swap_file).get("moves") or []
        if moves and not rolled_back:
            self._mark("rollback")
            rollback_moves(moves, sleep=self.sleep)
            clear_pycache(self.root / APP_REL)
            self.pending_file.unlink(missing_ok=True)
            self.last_swap_file.unlink(missing_ok=True)
            self._restart(self.settings)
            self._fail("新版本健康检查未通过，已回滚并重新启动旧版本")
        else:
            self._fail("健康检查未通过")
        ops_lock.release(lock)

    def _swap_moves(self) -> list[dict[str, Any]]:
        return read_json(self.last_swap_file).get("moves") or []

    def _swap(self, stage: Path, stamp: str, version: str) -> list[dict[str, Any]]:
        """按目录/文件性质选换法：挂载点上的目录只能原地覆盖，文件不能 rename 只能写内容。

        每一步在动手前先记进 last_swap.json，换到一半出错时也能逐条还原。
        """
        moves: list[dict[str, Any]] = []

        def record(move: dict[str, Any]) -> None:
            moves.append(move)
            write_json(self.last_swap_file, {"version": version, "moves": moves})

        staged_app = stage / APP_REL
        if staged_app.is_dir():
            target = self.root / APP_REL
            old = target.with_name(f"app.old-{stamp}")
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
            record({"mode": "rename", "target": str(target), "old": str(old)})
            move_path(target, old, sleep=self.sleep)
            move_path(staged_app, target, sleep=self.sleep)
            prune_olds(target.parent, "app.old-")

        staged_web = stage / WEB_REL
        if staged_web.is_dir():
            target = self.root / WEB_REL
            backup = self.work / f"backup-{stamp}" / "web-out"
            if target.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(target, backup, dirs_exist_ok=True)
            record({"mode": "overlay", "target": str(target), "old": str(backup)})
            # 先清空再铺新文件：overlay 不了旧版本残留的 chunk，否则前端会混用两版静态资源
            _clear_dir(target)
            target.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staged_web, target, dirs_exist_ok=True)
            prune_olds(self.work, "backup-")
            self._log("nginx 挂载的 apps/web/out 已原地覆盖（挂载点不能 rename）")

        staged_script = stage / SCRIPT_REL
        if staged_script.is_file():
            target = self.root / SCRIPT_REL
            target.parent.mkdir(parents=True, exist_ok=True)
            old = target.with_name(f"backup_core.py.old-{stamp}")
            record({"mode": "rename", "target": str(target), "old": str(old)})
            if target.exists():
                move_path(target, old, sleep=self.sleep)
            move_path(staged_script, target, sleep=self.sleep)
            prune_olds(target.parent, "backup_core.py.old-")

        staged_changelog = stage / CHANGELOG_REL
        if staged_changelog.is_file():
            target = self.root / CHANGELOG_REL
            old = target.with_name(f"CHANGELOG.md.old-{stamp}")
            if target.exists():
                # 单文件绑挂点不能 rename，旧版本留副本、新内容原地写入
                shutil.copy2(target, old)
            record({"mode": "file", "target": str(target), "old": str(old)})
            target.write_bytes(staged_changelog.read_bytes())
            prune_olds(target.parent, "CHANGELOG.md.old-")
        return moves

    def _arm_pending(
        self,
        target_version: str,
        *,
        rollback_dir: str = "",
        moves: list[dict[str, Any]] | None = None,
        boot_attempted: bool = False,
        reason: str = "",
    ) -> None:
        """换目录前写：新进程看到它就知道「上一轮没走完」，起不来还能自动回滚。"""
        write_json(
            self.pending_file,
            {
                "from_dir": str(self.root / APP_REL),
                "rollback_dir": rollback_dir,
                "target_version": target_version,
                "boot_attempted": boot_attempted,
                "created_at": int(time.time()),
                "reason": reason,
                "moves": moves or [],
            },
        )

    def _mark(self, step: str, *, state: str = "running") -> None:
        with self._lock:
            self._job.update(state=state, step=step, progress=STEP_PROGRESS.get(step, 0))
        self._persist()
        self._log(f"步骤：{step}")

    def _fail(self, message: str) -> None:
        with self._lock:
            self._job.update(
                state="failed",
                finished_at=int(time.time()),
                error=mask_text(str(message))[:LOG_LINE_MAX],
            )
        self._log(mask_text(str(message)))

    def _log(self, line: str) -> None:
        text = mask_text(str(line))[:LOG_LINE_MAX]
        with self._lock:
            self._job["log_tail"] = (self._job["log_tail"] + [text])[-LOG_TAIL_MAX:]
        self._persist()

    def _persist(self) -> None:
        try:
            write_json(self.job_file, self.status())
        except OSError:
            # 状态落盘失败不该把已经开始的更新打断；接口仍能返回内存里的 job
            pass

    def _stamp(self) -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S")

    def _http_download(self, url: str, dest: Path, limit: int) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with httpx.Client(
                timeout=httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0),
                follow_redirects=True,
                trust_env=False,
            ) as client:
                with client.stream("GET", url) as response:
                    # 重定向后也要确认没被降级成 http
                    assert_transport(str(response.url))
                    if response.status_code != 200:
                        raise FeedError(f"下载发布包失败：HTTP {response.status_code}")
                    written = 0
                    with dest.open("wb") as fh:
                        for chunk in response.iter_bytes(DOWNLOAD_CHUNK):
                            written += len(chunk)
                            if written > limit:
                                raise FeedError(f"发布包超过 {limit} 字节上限")
                            fh.write(chunk)
        except httpx.HTTPError as exc:
            dest.unlink(missing_ok=True)
            raise FeedError(f"下载发布包失败：{type(exc).__name__}") from exc
