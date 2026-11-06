"""视频任务本地台账（2-D7）。

上游只给一个 task_id，重试、进程重启、计费结算都需要本地那份稳定记录：
本地自增 id 才是主键，状态机 pending → running → succeeded/failed 只能向前走。
计费钩子（金额与价格快照）挂在同一条记录上，避免任务完成后无处补账。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

STATUSES = ("pending", "running", "succeeded", "failed")
TERMINAL_STATUSES = ("succeeded", "failed")
# 上游各家状态词不统一：统一收敛到四档，未知词按 pending（宁可停在未结束，不可猜成成功）
STATUS_MAP = {
    "queued": "pending",
    "pending": "pending",
    "submitted": "pending",
    "created": "pending",
    "in_progress": "running",
    "processing": "running",
    "running": "running",
    "completed": "succeeded",
    "succeeded": "succeeded",
    "success": "succeeded",
    "failed": "failed",
    "error": "failed",
    "cancelled": "failed",
    "canceled": "failed",
}
_FIELDS = (
    "id",
    "provider",
    "upstream_task_id",
    "model",
    "key_id",
    "key_name",
    "request_id",
    "status",
    "seconds",
    "billed_micro",
    "snapshot_id",
    "error",
    "created_at",
    "updated_at",
)


class VideoTaskError(ValueError):
    """台账操作不合法（状态回退、未知任务、非法字段）。"""


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def parse_snapshot(body: bytes) -> dict[str, Any]:
    """从上游响应体取出任务 id / 状态 / 时长；解析失败只回空 task_id，不抛给请求路径。"""
    try:
        raw = json.loads(body or b"{}")
    except (TypeError, ValueError):
        return {"task_id": "", "status": "pending", "seconds": 0}
    if not isinstance(raw, dict):
        return {"task_id": "", "status": "pending", "seconds": 0}
    task_id = str(raw.get("id") or raw.get("task_id") or "").strip()
    status = STATUS_MAP.get(str(raw.get("status") or "").strip().lower(), "pending")
    seconds = raw.get("seconds", raw.get("duration"))
    try:
        seconds = max(int(seconds), 0) if seconds is not None else 0
    except (TypeError, ValueError):
        seconds = 0
    return {"task_id": task_id, "status": status, "seconds": seconds}


def ledger_view(task: dict[str, Any]) -> dict[str, Any]:
    """本地台账的对外形态：source 明说「这不是上游状态」，调用方不用猜。"""
    return {
        "id": task.get("upstream_task_id") or "",
        "object": "video.task",
        "status": task.get("status") or "pending",
        "model": task.get("model") or "",
        "provider": task.get("provider") or "",
        "seconds": int(task.get("seconds") or 0),
        "billed_micro": int(task.get("billed_micro") or 0),
        "source": "local_ledger",
        "created_at": int(task.get("created_at") or 0),
        "updated_at": int(task.get("updated_at") or 0),
    }


def get(conn: sqlite3.Connection, *, provider: str, upstream_task_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {', '.join(_FIELDS)} FROM video_tasks WHERE provider = ? AND upstream_task_id = ?",
        (provider, upstream_task_id),
    ).fetchone()
    return _row(row)


def register(
    conn: sqlite3.Connection,
    *,
    provider: str,
    upstream_task_id: str,
    model: str = "",
    key_id: int | None = None,
    key_name: str = "",
    request_id: str = "",
    ts: int,
) -> dict[str, Any]:
    """登记刚创建的上游任务；同一 (provider, task_id) 重复登记返回原记录，不重置状态。"""
    if not provider or not upstream_task_id:
        raise VideoTaskError("provider 与 upstream_task_id 必须非空")
    existing = get(conn, provider=provider, upstream_task_id=upstream_task_id)
    if existing is not None:
        return existing
    cur = conn.execute(
        "INSERT INTO video_tasks(provider, upstream_task_id, model, key_id, key_name, "
        "request_id, status, created_at, updated_at) VALUES(?,?,?,?,?,?, 'pending', ?, ?)",
        (provider, upstream_task_id, model, key_id, key_name, request_id, ts, ts),
    )
    return get(conn, provider=provider, upstream_task_id=upstream_task_id) or {
        "id": cur.lastrowid
    }


def mark(
    conn: sqlite3.Connection,
    *,
    provider: str,
    upstream_task_id: str,
    status: str,
    ts: int,
    seconds: int | None = None,
    billed_micro: int | None = None,
    snapshot_id: int | None = None,
    error: str = "",
) -> dict[str, Any]:
    """推进状态；终态不可再改，重复写同一终态视为幂等。"""
    if status not in STATUSES:
        raise VideoTaskError(f"status 必须是 {'/'.join(STATUSES)}")
    task = get(conn, provider=provider, upstream_task_id=upstream_task_id)
    if task is None:
        raise VideoTaskError(f"未登记的视频任务：{provider}/{upstream_task_id}")
    if task["status"] in TERMINAL_STATUSES:
        if task["status"] == status:
            return task
        raise VideoTaskError(f"任务已结束（{task['status']}），不能再改成 {status}")
    if seconds is not None and seconds < 0:
        raise VideoTaskError("seconds 不能为负")
    if billed_micro is not None and billed_micro < 0:
        raise VideoTaskError("billed_micro 不能为负")

    assignments = ["status = ?", "updated_at = ?", "error = ?"]
    params: list[Any] = [status, ts, error]
    if seconds is not None:
        assignments.append("seconds = ?")
        params.append(seconds)
    if billed_micro is not None:
        assignments.append("billed_micro = ?")
        params.append(billed_micro)
    if snapshot_id is not None:
        assignments.append("snapshot_id = ?")
        params.append(snapshot_id)
    conn.execute(
        f"UPDATE video_tasks SET {', '.join(assignments)} WHERE id = ?",
        [*params, task["id"]],
    )
    return get(conn, provider=provider, upstream_task_id=upstream_task_id) or {}


def book(
    conn: sqlite3.Connection,
    *,
    provider: str,
    upstream_task_id: str,
    billed_micro: int,
    ts: int,
    snapshot_id: int | None = None,
) -> dict[str, Any]:
    """终态行回填金额；只动金额字段，不改状态与错误，重复调用幂等。"""
    if billed_micro < 0:
        raise VideoTaskError("billed_micro 不能为负")
    task = get(conn, provider=provider, upstream_task_id=upstream_task_id)
    if task is None:
        raise VideoTaskError(f"未登记的视频任务：{provider}/{upstream_task_id}")
    if task["status"] not in TERMINAL_STATUSES:
        raise VideoTaskError(f"只有终态任务可以入账，当前 {task['status']}")
    conn.execute(
        "UPDATE video_tasks SET billed_micro = ?, snapshot_id = COALESCE(?, snapshot_id), "
        "updated_at = ? WHERE id = ?",
        (int(billed_micro), snapshot_id, ts, task["id"]),
    )
    return get(conn, provider=provider, upstream_task_id=upstream_task_id) or {}


def list_tasks(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if status is not None and status not in STATUSES:
        raise VideoTaskError(f"status 必须是 {'/'.join(STATUSES)}")
    limit = max(1, min(int(limit), 1000))
    if status is None:
        rows = conn.execute(
            f"SELECT {', '.join(_FIELDS)} FROM video_tasks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {', '.join(_FIELDS)} FROM video_tasks WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [dict(row) for row in rows]
