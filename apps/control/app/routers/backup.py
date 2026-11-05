"""备份与恢复端点：把 scripts/backup_core.py 的 CLI 能力开给管理界面。

语义与 scripts/restore.sh 及 CI 的备份恢复演练保持一致：不做 schema 迁移。
流程 = 校验归档（zip + manifest + 成员 sha256 + PRAGMA integrity_check）→
在静止的归档库上跑严格自检 → 给当前库出一份一致性回滚快照 → 用 SQLite
在线备份 API 把归档页写进活库（src=归档, dst=活库）→ 结构自检，不过就从
一致性快照回滚。不换文件，因此不要求 Windows 上没有任何文件句柄。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..api_utils import db_of, mask_text
from ..security import admin_user, audit_event
from .settings import _apply_stored_runtime

logger = logging.getLogger("unified.backup")

router = APIRouter(prefix="/api/system", tags=["system"])

BACKUP_KEEP = 14  # 与 scripts/backup.sh 的默认 --keep 一致
LOCK_FILE = "backup.lock"
LOCK_STALE_SEC = 600  # 备份恢复都是秒级，10 分钟没释放视为进程已崩溃
CORE_PATH = Path(__file__).resolve().parents[4] / "scripts" / "backup_core.py"

_core_module: Any = None


def core_module() -> Any:
    """按需加载仓库内的备份核心；镜像里没带 scripts/ 时给出明确 503。"""
    global _core_module  # noqa: PLW0603
    if _core_module is not None:
        return _core_module
    if not CORE_PATH.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"备份组件缺失：找不到 {CORE_PATH.name}，当前部署不支持备份与恢复",
        )
    spec = importlib.util.spec_from_file_location("unified_backup_core", CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    _core_module = module
    return _core_module


def _backup_dir(settings: Any) -> Path:
    # 备份与库放在一起：默认 data/unified.db → data/backups，与 backup.sh 一致
    return Path(settings.db_path).parent / "backups"


@contextmanager
def _busy_guard(directory: Path, op: str) -> Iterator[None]:
    """跨进程互斥：同一时间只允许一个备份/恢复，抢不到锁直接 409。"""
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / LOCK_FILE
    payload = json.dumps(
        {"op": op, "pid": os.getpid(), "started_at": int(time.time())}, ensure_ascii=False
    )
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_age_sec(lock) > LOCK_STALE_SEC:
                lock.unlink(missing_ok=True)
                continue
            raise HTTPException(status_code=409, detail=_busy_detail(lock, op)) from None
        break
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _lock_meta(lock: Path) -> dict[str, Any]:
    try:
        meta = json.loads(lock.read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _lock_age_sec(lock: Path) -> float:
    meta = _lock_meta(lock)
    started = meta.get("started_at")
    if isinstance(started, (int, float)):
        return time.time() - started
    try:
        return time.time() - lock.stat().st_mtime
    except OSError:
        return 0.0


def _busy_detail(lock: Path, op: str) -> str:
    meta = _lock_meta(lock)
    running = str(meta.get("op") or "备份")
    started = meta.get("started_at")
    when = (
        time.strftime("%H:%M:%S", time.localtime(started))
        if isinstance(started, (int, float))
        else "刚才"
    )
    return f"已有 {running} 在进行中（{when} 开始），同一时间只能有一个任务，稍后再试（本次请求：{op}）"


def _audit(request: Request, username: str, action: str, target: str, detail: str) -> None:
    conn = db_of(request)
    try:
        audit_event(
            conn,
            username,
            action,
            target,
            detail=mask_text(detail),
            ip=request.client.host if request.client else "",
        )
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"操作已执行，但审计日志写入失败：{exc}") from exc
    finally:
        conn.close()


def _make_archive(core: Any, settings: Any, directory: Path) -> Path:
    # 重定向 stdout：backup_core 的 CLI 会打印进度，这里只要结构化返回值
    with contextlib.redirect_stdout(io.StringIO()):
        return core.make_archive(
            db_source=Path(settings.db_path),
            out_dir=directory,
            keep=BACKUP_KEEP,
            include_demo=False,
            method="",
            source_label=str(settings.db_path),
            root=core.repo_root(),
            already_snapshot=False,
        )


def _digest_of(archive: Path) -> str:
    sidecar = archive.with_name(archive.name + ".sha256")
    try:
        return sidecar.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        return ""


@router.get("/backups")
def list_backups(request: Request, user=Depends(admin_user)):
    core = core_module()
    directory = _backup_dir(request.app.state.settings)
    if not directory.is_dir():
        return {"backups": [], "count": 0, "dir": mask_text(str(directory))}
    items: list[dict[str, Any]] = []
    try:
        for entry in directory.iterdir():
            if not entry.is_file() or not core.ARCHIVE_RE.match(entry.name):
                continue
            stat = entry.stat()
            items.append(
                {
                    "name": entry.name,
                    "size_bytes": stat.st_size,
                    "created_at": int(stat.st_mtime),
                    "path": mask_text(str(entry)),
                    "has_checksum": entry.with_name(entry.name + ".sha256").is_file(),
                }
            )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取备份目录失败：{exc}") from exc
    items.sort(key=lambda item: item["name"], reverse=True)
    return {"backups": items, "count": len(items), "dir": mask_text(str(directory))}


@router.post("/backup")
def create_backup(request: Request, user=Depends(admin_user)):
    settings = request.app.state.settings
    core = core_module()
    directory = _backup_dir(settings)
    started = time.perf_counter()
    with _busy_guard(directory, "backup"):
        try:
            archive = _make_archive(core, settings, directory)
            size = archive.stat().st_size
        except (core.BackupError, sqlite3.Error, OSError) as exc:
            raise HTTPException(status_code=500, detail=f"备份失败：{exc}") from exc
        duration_ms = int((time.perf_counter() - started) * 1000)
        _audit(
            request,
            user["username"],
            "backup.create",
            archive.name,
            f"size={size} duration={duration_ms}ms",
        )
    return {
        "ok": True,
        "name": archive.name,
        "size_bytes": size,
        "duration_ms": duration_ms,
        "path": mask_text(str(archive)),
        "sha256": _digest_of(archive),
        "created_at": int(archive.stat().st_mtime),
    }


class RestoreIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    confirm: bool = False


@router.post("/restore")
def restore_backup(request: Request, payload: RestoreIn, user=Depends(admin_user)):
    settings = request.app.state.settings
    core = core_module()
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="恢复会覆盖当前数据库，必须显式确认（confirm=true）")
    if not core.ARCHIVE_RE.match(payload.name):
        raise HTTPException(status_code=400, detail="备份文件名不合法")
    directory = _backup_dir(settings)
    archive = directory / payload.name
    if not archive.is_file():
        raise HTTPException(status_code=400, detail=f"备份不存在：{payload.name}")

    started = time.perf_counter()
    with _busy_guard(directory, f"restore:{payload.name}"):
        try:
            db_file, manifest, staging = core.load_archive(archive)
        except (
            core.BackupError,
            sqlite3.Error,
            OSError,
            ValueError,
            KeyError,
            zipfile.BadZipFile,
            json.JSONDecodeError,
        ) as exc:
            raise HTTPException(
                status_code=400, detail=f"备份不可用，已中止恢复：{exc}"
            ) from exc

        original = Path(settings.db_path)
        rollback_dir = Path(tempfile.mkdtemp(prefix="u2a-rollback-"))
        try:
            _verify_archive(core, db_file, manifest)
            pre_snapshot = _restore_pre_snapshot(core, settings, directory, user["username"])
            rollback_file = _snapshot_live(core, original, rollback_dir)
            _apply_archive(core, db_file, original, rollback_file, manifest)
            _reapply_runtime(request)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(rollback_dir, ignore_errors=True)

        duration_ms = int((time.perf_counter() - started) * 1000)
        _audit(
            request,
            user["username"],
            "backup.restore",
            payload.name,
            f"snapshot={pre_snapshot.name} duration={duration_ms}ms",
        )

    return {
        "ok": True,
        "restored": payload.name,
        "path": mask_text(str(original)),
        "pre_restore_backup": pre_snapshot.name,
        "pre_restore_path": mask_text(str(pre_snapshot)),
        "duration_ms": duration_ms,
        "selfcheck_ok": True,
        "rows_after": manifest.get("tables", {}),
    }


def _reapply_runtime(request: Request) -> None:
    """恢复出的库带的是另一套运行参数，同步回内存，免得非得重启才生效。"""
    try:
        _apply_stored_runtime(request.app)
    except Exception:  # noqa: BLE001 - 恢复已成功，配置回填失败只告警，不把成功说成失败
        logger.warning("恢复后重新应用运行参数失败，内存里仍是恢复前的值", exc_info=True)


def _restore_pre_snapshot(core: Any, settings: Any, directory: Path, username: str) -> Path:
    """恢复前先给当前库出一份完整备份，失败就中止（当前库一个字节都没动）。"""
    try:
        return _make_archive(core, settings, directory)
    except (core.BackupError, sqlite3.Error, OSError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"无法为当前库生成恢复前快照，已中止恢复（当前库未被修改）：{exc}",
        ) from exc


RESTORE_TIMEOUT_SEC = 30  # 在线备份每一步等锁的上限；超时即中止并回滚
RESTORE_PAGES_PER_STEP = 100
# 正常业务只追加的表：允许并发写把行数写多，但不允许比归档少（防半截恢复）
APPEND_ONLY_TABLES = ("request_logs", "usage_records", "billing_ledger", "audit_logs")


def _verify_archive(core: Any, db_file: Path, manifest: dict) -> None:
    """应用前的严格自检：在静止的归档库上比对行数，并发写无法干扰。"""
    try:
        verdict = core.selfcheck(db_file, manifest)
    except (core.BackupError, sqlite3.Error, OSError) as exc:
        raise HTTPException(status_code=400, detail=f"备份不可用，已中止恢复：{exc}") from exc
    if not verdict["ok"]:
        raise HTTPException(
            status_code=500,
            detail=f"归档自检未通过，已中止恢复（当前库未被修改）：{_failed(verdict)}",
        )


def _snapshot_live(core: Any, original: Path, target_dir: Path) -> Path:
    """回滚材料：走 SQLite 在线快照，不裸拷别人正在写的 -wal。"""
    target = target_dir / original.name
    try:
        core.snapshot(original, target)
    except (core.BackupError, sqlite3.Error, OSError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"无法为当前库生成一致性回滚快照，已中止恢复（当前库未被修改）：{exc}",
        ) from exc
    return target


def _backup_into(source: Path, target: Path) -> None:
    """SQLite 在线备份：src -> dst，dst 全程是合法库，由 sqlite 自己的锁保护。

    不换文件，所以不依赖 Windows 上「目标文件没有其他句柄」——这正是 os.replace
    在服务运行中必然失败的原因。
    """
    deadline = time.monotonic() + RESTORE_TIMEOUT_SEC

    def _no_stall(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise sqlite3.OperationalError(
                f"在线备份超过 {RESTORE_TIMEOUT_SEC}s：目标库持续被占用"
            )

    src = sqlite3.connect(str(source), timeout=RESTORE_TIMEOUT_SEC)
    dst = sqlite3.connect(str(target), timeout=RESTORE_TIMEOUT_SEC)
    try:
        # Connection.backup 的语义是「自己当源，参数当目标」，方向不能反过来
        src.backup(dst, pages=RESTORE_PAGES_PER_STEP, progress=_no_stall, sleep=0.005)
    finally:
        src.close()
        dst.close()


def _ensure_wal(original: Path) -> None:
    """把活库钉回 WAL：归档若是 delete 模式，不能让运行期跟着降级。"""
    last: str | None = None
    for _ in range(6):
        conn = sqlite3.connect(str(original), timeout=RESTORE_TIMEOUT_SEC)
        try:
            last = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        except sqlite3.OperationalError:  # 有写事务在跑，稍等再试
            last = None
        finally:
            conn.close()
        if last == "wal":
            return
        time.sleep(0.05)
    raise sqlite3.OperationalError(f"恢复后无法把 journal_mode 设回 WAL（实际 {last}）")


def _apply_archive(core: Any, db_file: Path, original: Path, rollback_file: Path, manifest: dict) -> None:
    try:
        _backup_into(db_file, original)
        _ensure_wal(original)
    except (sqlite3.Error, OSError) as exc:
        _rollback_db(original, rollback_file)
        raise HTTPException(
            status_code=500, detail=f"在线恢复失败，已回滚到恢复前的库：{exc}"
        ) from exc

    result = _structure_check(core, original, manifest)
    if not result["ok"]:
        _rollback_db(original, rollback_file)
        raise HTTPException(
            status_code=500, detail=f"恢复后自检未通过，已回滚到恢复前的库：{_failed(result)}"
        )


def _rollback_db(original: Path, rollback_file: Path) -> None:
    try:
        _backup_into(rollback_file, original)
        _ensure_wal(original)
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(
            status_code=500, detail=f"回滚失败，当前库可能处于不一致状态：{exc}"
        ) from exc


def _structure_check(core: Any, original: Path, manifest: dict) -> dict:
    """应用后的自检：只查正常并发写改变不了的性质（结构 + 完整性 + 表集合）。

    行数只做「只增不减」的下限校验：恢复瞬间起并发写会继续往下写，严格相等
    必然误判；但比归档少说明恢复不完整，必须回滚。
    """
    conn = sqlite3.connect(str(original), timeout=RESTORE_TIMEOUT_SEC)
    try:
        conn.execute("SELECT 1").fetchone()
        counts = core.table_counts(conn)
        expected = manifest.get("tables") or {}
        missing = sorted(name for name in core.REQUIRED_TABLES if name not in counts)
        absent = sorted(name for name in expected if name not in counts)
        extra = sorted(name for name in counts if name not in expected)
        underflow = {
            name: {"manifest": int(expected[name]), "actual": counts[name]}
            for name in APPEND_ONLY_TABLES
            if name in expected and name in counts and counts[name] < int(expected[name])
        }
        admins = (
            int(conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0])
            if "users" in counts
            else 0
        )
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        check_demo = bool((manifest.get("demo") or {}).get("excluded"))
        residue = core.demo_residue(conn) if check_demo else {}
    finally:
        conn.close()

    checks = {
        "select_1": True,
        "required_tables": {"ok": not missing, "missing": missing},
        "table_count": {
            "ok": not absent and not extra,
            "manifest": len(expected),
            "actual": len(counts),
            "missing": absent,
            "extra": extra,
        },
        "row_floor": {"ok": not underflow, "underflow": underflow},
        "admin_account": {"ok": admins >= 1, "count": admins},
        "demo_free": {"ok": not residue, "residue": residue},
        "integrity_check": integrity,
        "foreign_key_check": {"ok": not fk_rows, "violations": len(fk_rows)},
    }
    ok = (
        checks["select_1"]
        and checks["required_tables"]["ok"]
        and checks["table_count"]["ok"]
        and checks["row_floor"]["ok"]
        and checks["admin_account"]["ok"]
        and checks["demo_free"]["ok"]
        and checks["integrity_check"] == "ok"
        and checks["foreign_key_check"]["ok"]
    )
    return {"ok": ok, "counts": counts, "checks": checks}


def _failed(result: dict) -> str:
    failed: list[str] = []
    for name, value in (result.get("checks") or {}).items():
        if isinstance(value, dict):
            if not value.get("ok", True):
                failed.append(name)
        elif isinstance(value, str) and value != "ok":
            failed.append(f"{name}={value}")
    return ", ".join(failed) or "unknown"
