"""请求日志：只读查询 /api/logs + 管理员清空 /api/logs/clear。

request_logs 表（见 db.py）只有 ts / provider / model / status / tokens / latency /
stream / error / key_id / key_name / key_prefix，没有 IP、UA、首字延迟、缓存三段这些列。
响应仍按 apps/web/lib/types.ts 的 RequestLog 字段名输出，缺的列一律 null
（该类型里 null 的语义就是"未采集"），不编造数值。

逐条扣费：credit 来自 billing_ledger.log_id == request_logs.id 的精确关联
（settle 写流水时带上，同事务落库），绝不用 key+model+时间窗弱关联（会串账）。
credit 为 micro-USD 整数；credit_status 说明口径：
billed=金额已定格（含 0 元）、unpriced=未配价、usage_missing=上游无 usage、
not_billed=失败/断流（本就不写流水）、unlinked=成功但无关联流水（存量行或写流水异常）。

归属：api_keys.owner_user_id 存用户名。admin 读全量；非 admin 只读自己名下 key
的日志，显式传他人的 key_id 返回结构化 403，不返回空集合假装成功。NULL 归属的
老 key 和 key_id 为空的匿名日志对非 admin 一律不可见（无法归属）。
出参不含完整密钥，key_prefix 只回显掩码：写入时固化进 request_logs，老行
（列为 NULL）回退 join api_keys，两边都拿不到给 null 而不是空串。
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..api_utils import log_failure_sql, log_success_sql, now
from ..key_prefix import mask_prefix
from ..security import PermissionDenied, admin_user, audit_event, current_user

router = APIRouter(prefix="/api/logs", tags=["logs"])

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 20
OWN_LOGS_DENIED = "无权查看他人密钥的请求日志"


def _owner(user: dict) -> str | None:
    """admin 返回 None（不收窄）；其他角色返回自己的用户名。"""
    if user.get("role") == "admin":
        return None
    return user.get("username") or ""


def _assert_key_owned(conn, key_id: int, user: dict) -> None:
    """显式查他人 key 一律 403：含不存在、NULL 归属，绝不退化成空集。"""
    owner = _owner(user)
    if owner is None:
        return
    row = conn.execute(
        "SELECT owner_user_id FROM api_keys WHERE id = ?", (key_id,)
    ).fetchone()
    if row is None or (row["owner_user_id"] or "") != owner:
        raise PermissionDenied(OWN_LOGS_DENIED, role=user.get("role"))


def _mask_prefix(prefix: str | None) -> str | None:
    """api_keys.prefix 只回显前缀后 4 位 + 掩码，任何场景都不输出完整密钥。"""
    if not prefix:
        return None
    return mask_prefix(prefix)


def _prefixes(conn, rows: list[Any]) -> dict[int, str | None]:
    """一页一次查询取回掩码前缀，避免逐行 N+1。"""
    ids = {row["key_id"] for row in rows if row["key_id"] is not None}
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    found = conn.execute(
        f"SELECT id, prefix FROM api_keys WHERE id IN ({marks})", list(ids)
    ).fetchall()
    return {int(row["id"]): _mask_prefix(row["prefix"]) for row in found}


def _key_prefix(row: Any, joined: dict[int, str | None]) -> str | None:
    """写时固化的前缀优先（删 key 后仍可溯源）；老行 NULL 回退 join，都拿不到给 null。"""
    stored = row["key_prefix"]
    return _mask_prefix(stored) if stored else joined.get(row["key_id"])


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
    owner: str | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if owner is not None:
        # key_id 为空的匿名日志无法归属，IN 子查询天然排除
        clauses.append("key_id IN (SELECT id FROM api_keys WHERE owner_user_id = ?)")
        params.append(owner)
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


def _classify_credit(billed_micro: int, note: str) -> tuple[int | None, str]:
    """流水 note → 输出口径：金额确定才给数，其余给可区分的状态。"""
    if note == "usage_missing":
        return None, "usage_missing"
    if note.startswith("unpriced:"):
        # 部分类别缺价时流水金额只是估值（estimated=1），不冒充确定金额
        return None, "unpriced"
    return int(billed_micro), "billed"


def _ledger_credits(conn, rows: list[Any]) -> dict[int, tuple[int | None, str]]:
    """一页一次按 log_id 取回关联流水，避免逐行 N+1。"""
    ids = [int(row["id"]) for row in rows]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    found = conn.execute(
        f"SELECT log_id, billed_micro, note FROM billing_ledger WHERE log_id IN ({marks})",
        ids,
    ).fetchall()
    return {int(row["log_id"]): _classify_credit(row["billed_micro"], row["note"]) for row in found}


def _credit_of(row: Any, ledger: dict[int, tuple[int | None, str]]) -> tuple[int | None, str]:
    hit = ledger.get(int(row["id"]))
    if hit is not None:
        return hit
    # 无关联流水：失败/断流本来就不写流水；成功却查不到是存量未关联或写流水异常
    if row["status"] >= 400 or (row["error"] or "") != "":
        return None, "not_billed"
    return None, "unlinked"


def _row_to_public(
    row: Any, key_prefix: str | None = None, credit: tuple[int | None, str] = (None, "unlinked")
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "key_id": row["key_id"],
        "key_name": row["key_name"],
        # 掩码前缀：不含完整密钥，仅用于界面上辨认属于哪把 key
        "key_prefix": key_prefix,
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
        # micro-USD 整数；关联不到流水时为 null，配合 credit_status 区分口径
        "credit": credit[0],
        "credit_status": credit[1],
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
    owner = _owner(user)
    conn = request.app.state.db.connect()
    try:
        if key_id is not None:
            _assert_key_owned(conn, key_id, user)
        where, params = _where(
            key_id=key_id, provider=provider, model=model, status=status,
            days=days, start=start, end=end, owner=owner,
        )
        total = int(conn.execute(f"SELECT COUNT(*) AS n FROM request_logs{where}", params).fetchone()["n"])
        rows = conn.execute(
            f"SELECT * FROM request_logs{where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            [*params, take, skip],
        ).fetchall()
        prefixes = _prefixes(conn, rows)
        credits = _ledger_credits(conn, rows)
    finally:
        conn.close()
    return {
        "total": total,
        "items": [
            _row_to_public(row, _key_prefix(row, prefixes), _credit_of(row, credits))
            for row in rows
        ],
    }


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
