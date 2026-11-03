"""安全面只读端点。

net_guard.py 实际只做一件事：校验上游地址（拒绝环回 / 私网 / 链路本地 / 云元数据），
没有 IP 规则表，也没有"放行/拦截访问日志"表。所以这里只暴露三块**真实存在**的数据：

- /api/security/guard          地址守卫的封禁清单与当前开关
- /api/security/login-failures 登录失败节流状态 + audit_logs 里的失败统计
- /api/security/audit          审计事件（同时挂 /api/audit-logs 供安全页调用）

不提供 /api/security/rules 与 /api/security/logs：库里没有对应存储，也不存在执行逻辑。
"""

import datetime as dt
import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..net_guard import BLOCKED_ADDRESSES, BLOCKED_HOSTNAMES
from ..security import current_user
from .auth import LOGIN_MAX_FAILURES, LOGIN_WINDOW_SEC, _login_failures, _login_lock

router = APIRouter(prefix="/api", tags=["security"])

MAX_AUDIT_LIMIT = 500


def _day_start() -> int:
    return int(dt.datetime.combine(dt.date.today(), dt.time.min).timestamp())


def _audit_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "actor": row["actor"],
        "action": row["action"],
        "target": row["target"],
        "detail": row["detail"],
        "ip": row["ip"],
    }


@router.get("/security/guard")
def guard(request: Request, user: dict = Depends(current_user)):
    settings = request.app.state.settings
    return {
        "allow_private_upstreams": settings.allow_private_upstreams,
        "blocked_hostnames": sorted(BLOCKED_HOSTNAMES),
        "blocked_addresses": [str(addr) for addr in BLOCKED_ADDRESSES],
        "blocked_categories": [
            "loopback", "private", "link_local", "multicast", "reserved", "unspecified",
        ],
        "allowed_schemes": ["http", "https"],
    }


@router.get("/security/login-failures")
def login_failures(request: Request, limit: int = 50, user: dict = Depends(current_user)):
    """当前节流状态（进程内存）+ 历史失败次数（audit_logs）。

    内存里只有"窗口内还在计数"的 IP+用户名组合；窗口一过就清掉，
    所以这里也按 LOGIN_WINDOW_SEC 过滤一遍，避免展示已经失效的记录。
    """
    limit = max(1, min(limit, 200))
    now_ts = time.time()
    with _login_lock:
        snapshot = {key: list(stamps) for key, stamps in _login_failures.items()}

    active: list[dict[str, Any]] = []
    for key, stamps in snapshot.items():
        fresh = [ts for ts in stamps if now_ts - ts < LOGIN_WINDOW_SEC]
        if not fresh:
            continue
        ip, _, username = key.partition("|")
        locked = len(fresh) >= LOGIN_MAX_FAILURES
        active.append({
            "ip": ip,
            "username": username,
            "failures": len(fresh),
            "first_ts": int(fresh[0]),
            "last_ts": int(fresh[-1]),
            "locked": locked,
            "retry_after": max(1, int(LOGIN_WINDOW_SEC - (now_ts - fresh[0]))) if locked else 0,
        })
    active.sort(key=lambda item: item["last_ts"], reverse=True)

    conn = request.app.state.db.connect()
    try:
        counts = conn.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END), 0) AS today "
            "FROM audit_logs WHERE action = 'login_failed'",
            (_day_start(),),
        ).fetchone()
        recent = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'login_failed' ORDER BY ts DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "window_sec": LOGIN_WINDOW_SEC,
        "max_failures": LOGIN_MAX_FAILURES,
        "active": active,
        "stats": {"total": int(counts["total"] or 0), "today": int(counts["today"] or 0)},
        "recent": [_audit_row(row) for row in recent],
    }


@router.get("/audit-logs")
@router.get("/security/audit")
def audit_logs(
    request: Request,
    limit: int = 200,
    offset: int = 0,
    action: str | None = None,
    actor: str | None = None,
    user: dict = Depends(current_user),
):
    limit = max(1, min(limit, MAX_AUDIT_LIMIT))
    offset = max(0, offset)
    clauses: list[str] = []
    params: list[Any] = []
    if action:
        clauses.append("action = ?")
        params.append(action.strip())
    if actor:
        clauses.append("actor = ?")
        params.append(actor.strip())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = request.app.state.db.connect()
    try:
        total = int(conn.execute(f"SELECT COUNT(*) AS n FROM audit_logs{where}", params).fetchone()["n"])
        rows = conn.execute(
            f"SELECT * FROM audit_logs{where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    finally:
        conn.close()
    return {"items": [_audit_row(row) for row in rows], "total": total}
