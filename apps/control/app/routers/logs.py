"""请求日志：只读查询 /api/logs + 管理员清空 /api/logs/clear。

request_logs 表（见 db.py）只有 ts / provider / model / status / tokens / latency /
stream / error / key_id / key_name，没有 IP、UA、首字延迟、缓存三段、扣费这些列。
响应仍按 apps/web/lib/types.ts 的 RequestLog 字段名输出，缺的列一律 null
（该类型里 null 的语义就是"未采集"），不编造数值。
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..api_utils import log_failure_sql, log_success_sql, now
from ..security import admin_user, audit_event, current_user

router = APIRouter(prefix="/api/logs", tags=["logs"])

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 20


def _status_clause(raw: str | None) -> tuple[str, list[Any]] | None:
    """status 同时接受 ok / error（前端下拉框的值）和具体状态码。

    ok/error 的口径与 stats 一致：2xx 但带 error（流式中途断流）算失败。
    """
    if raw is None:
        return None
    text = raw.strip().lower()
    if not text or text == "all":
        return None
    if text == "ok":
        return f"({log_success_sql()})", []
    if text == "error":
        return f"({log_failure_sql()})", []
    try:
        code = int(text)
    except ValueError:
        raise HTTPException(status_code=400, detail="status 需为 ok / error 或数字状态码") from None
    return "status = ?", [code]


def _where(
    *,
    key_id: int | None,
    provider: str | None,
    model: str | None,
    status: str | None,
    days: int | None,
    start: int | None,
    end: int | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if key_id is not None:
        clauses.append("key_id = ?")
        params.append(key_id)
    if provider:
        clauses.append("provider = ?")
        params.append(provider.strip())
    if model:
        clauses.append("model LIKE ?")
        params.append(f"%{model.strip()}%")
    status_filter = _status_clause(status)
    if status_filter:
        clause, extra = status_filter
        clauses.append(clause)
        params.extend(extra)
    if days is not None:
        clauses.append("ts >= ?")
        params.append(now() - max(1, min(days, 365)) * 86400)
    if start is not None:
        clauses.append("ts >= ?")
        params.append(start)
    if end is not None:
        clauses.append("ts <= ?")
        params.append(end)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _window(page: int | None, size: int | None, limit: int | None, offset: int) -> tuple[int, int]:
    """limit/offset 优先；前端分页传的是 page/size，两种写法都认。"""
    if limit is not None:
        take = max(1, min(limit, MAX_PAGE_SIZE))
        skip = max(0, offset)
        return take, skip
    take = max(1, min(size or DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE))
    skip = max(0, ((page or 1) - 1) * take)
    return take, skip


def _row_to_public(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "key_id": row["key_id"],
        "key_name": row["key_name"],
        "provider": row["provider"],
        # 表里没有这几列：保持类型里的键，值给 null / 空串表示未采集
        "ip": "",
        "model": row["model"],
        "mapped_model": None,
        "status": row["status"],
        "prompt_tokens": int(row["prompt_tokens"] or 0),
        "completion_tokens": int(row["completion_tokens"] or 0),
        "latency_ms": int(row["latency_ms"] or 0),
        "first_token_ms": None,
        "cache_hit_tokens": None,
        "cache_miss_tokens": None,
        "cache_write_tokens": None,
        "ua": None,
        "error": row["error"],
        "stream": bool(row["stream"]),
        "credit": None,
        "account": None,
    }


@router.get("")
def list_logs(
    request: Request,
    page: int | None = None,
    size: int | None = None,
    limit: int | None = None,
    offset: int = 0,
    key_id: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    status: str | None = None,
    days: int | None = None,
    start: int | None = None,
    end: int | None = None,
    user: dict = Depends(current_user),
):
    take, skip = _window(page, size, limit, offset)
    where, params = _where(
        key_id=key_id, provider=provider, model=model, status=status,
        days=days, start=start, end=end,
    )
    conn = request.app.state.db.connect()
    try:
        total = int(conn.execute(f"SELECT COUNT(*) AS n FROM request_logs{where}", params).fetchone()["n"])
        rows = conn.execute(
            f"SELECT * FROM request_logs{where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            [*params, take, skip],
        ).fetchall()
    finally:
        conn.close()
    return {"total": total, "items": [_row_to_public(row) for row in rows]}


@router.post("/clear")
def clear_logs(request: Request, user: dict = Depends(admin_user)):
    conn = request.app.state.db.connect()
    try:
        cur = conn.execute("DELETE FROM request_logs")
        conn.commit()
        removed = cur.rowcount
        audit_event(
            conn,
            user["username"],
            "logs.clear",
            "request_logs",
            f"deleted={removed}",
            request.client.host if request.client else "",
        )
    finally:
        conn.close()
    return {"ok": True, "deleted": removed}
