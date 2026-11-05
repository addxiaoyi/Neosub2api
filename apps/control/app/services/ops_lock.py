"""备份/恢复与在线更新共用的目录互斥锁。

原实现私放在 routers/backup.py 里；在线更新必须和备份/恢复抢同一把锁，
否则「更新换目录」和「恢复写库」会同时进行。锁文件落在数据目录下的备份目录，
语义沿用旧实现：O_CREAT|O_EXCL 抢锁，超时视为持有进程已崩溃。
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

LOCK_FILE = "backup.lock"
LOCK_STALE_SEC = 600


class OpsBusy(RuntimeError):
    """已有任务持锁；路由层据此返回 409，消息里带占用者与开始时间。"""


def lock_meta(lock: Path) -> dict[str, Any]:
    try:
        meta = json.loads(lock.read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def lock_age_sec(lock: Path) -> float:
    started = lock_meta(lock).get("started_at")
    if isinstance(started, (int, float)):
        return time.time() - started
    try:
        return time.time() - lock.stat().st_mtime
    except OSError:
        return 0.0


def busy_detail(lock: Path, op: str) -> str:
    meta = lock_meta(lock)
    running = str(meta.get("op") or "备份")
    started = meta.get("started_at")
    when = (
        time.strftime("%H:%M:%S", time.localtime(started))
        if isinstance(started, (int, float))
        else "刚才"
    )
    return f"已有 {running} 在进行中（{when} 开始），同一时间只能有一个任务，稍后再试（本次请求：{op}）"


def acquire(directory: Path, op: str) -> Path:
    """抢锁并返回锁文件路径；抢不到抛 OpsBusy（不含 HTTP 语义）。"""
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / LOCK_FILE
    payload = json.dumps(
        {"op": op, "pid": os.getpid(), "started_at": int(time.time())}, ensure_ascii=False
    )
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if lock_age_sec(lock) > LOCK_STALE_SEC:
                lock.unlink(missing_ok=True)
                continue
            raise OpsBusy(busy_detail(lock, op)) from None
        break
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    return lock


def release(lock: Path) -> None:
    lock.unlink(missing_ok=True)


@contextmanager
def busy_guard(directory: Path, op: str) -> Iterator[None]:
    lock = acquire(directory, op)
    try:
        yield
    finally:
        release(lock)


def release_stale(directory: Path, prefix: str) -> bool:
    """启动时清理上一次进程留下的锁（更新进程会被重启策略杀掉，锁必然残留）。

    只清 op 前缀匹配的锁：备份/恢复的锁可能是另一个进程正在跑，仍走 10 分钟超时。
    不能靠 pid 判活——容器里新进程往往还是 PID 1，与残留锁里的 pid 相同。
    """
    lock = directory / LOCK_FILE
    op = str(lock_meta(lock).get("op") or "")
    if not op.startswith(prefix):
        return False
    lock.unlink(missing_ok=True)
    return True
