"""安全面只读端点。

net_guard.py 实际只做一件事：校验上游地址（拒绝环回 / 私网 / 链路本地 / 云元数据），
没有 IP 规则表，也没有"放行/拦截访问日志"表。所以这里只暴露三块**真实存在**的数据：

- /api/security/guard          地址守卫的封禁清单与当前开关
- /api/security/login-failures 登录失败节流状态 + audit_logs 里的失败统计
                              （admin 看全站；其他人只看自己账号的聚合，不含 IP / 他人用户名）
- /api/security/audit          审计事件（同时挂 /api/audit-logs 供安全页调用）

不提供 /api/security/rules 与 /api/security/logs：库里没有对应存储，也不存在执行逻辑。
"""

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..billing import period_bounds
from ..net_guard import BLOCKED_ADDRESSES, BLOCKED_HOSTNAMES
from ..security import PermissionDenied, admin_user, audit_event, current_user
from ..services import secret_store
from .auth import LoginGuard, _login_failures, _login_guard, _login_lock, _login_wait

router = APIRouter(prefix="/api", tags=["security"])


@router.get("/security/secret-at-rest")
def secret_at_rest(request: Request, user: dict = Depends(admin_user)):
    """凭据落库加密现状：只给计数与开关，不出现任何明文/密文片段。"""
    return secret_store.status(request.app.state.db)


MAX_AUDIT_LIMIT = 500
AUDIT_ACTOR_DENIED = "无权查看他人审计日志"

# 登录失败视图的作用域：all = 全站（仅 admin），self = 当前账号的聚合数字
SCOPE_ALL = "all"
SCOPE_SELF = "self"


def _day_start() -> int:
    """「今天」按 UTC+8 的日界，与配额周期同一口径，不随宿主机时区漂移。"""
    return period_bounds("day", int(time.time()))[0]


def _active_throttle(now_ts: float, guard: LoginGuard) -> list[dict[str, Any]]:
    """窗口内仍在计数的 IP+用户名组合；只给 admin 用，含 IP 与用户名。"""
    with _login_lock:
        snapshot = {key: list(stamps) for key, stamps in _login_failures.items()}

    active: list[dict[str, Any]] = []
    for key, stamps in snapshot.items():
        fresh = [ts for ts in stamps if now_ts - ts < guard.window_sec]
        if not fresh:
            continue
        paused = _login_wait(key, guard, at=now_ts)
        ip, _, username = key.partition("|")
        active.append({
            "ip": ip,
            "username": username,
            "failures": len(fresh),
            "first_ts": int(fresh[0]),
            "last_ts": int(fresh[-1]),
            "locked": paused > 0,
            "retry_after": paused,
        })
    active.sort(key=lambda item: item["last_ts"], reverse=True)
    return active


def _own_window_failures(owner: str, now_ts: float, guard: LoginGuard) -> int:
    """把节流桶按用户名归并成单个数字，避免把撞库 IP 暴露给普通用户。

    节流键里的用户名是 lower() 过的（见 auth._login_key），这里按同样规则比较。
    """
    if not owner:
        return 0
    target = owner.strip().lower()
    total = 0
    with _login_lock:
        for key, stamps in _login_failures.items():
            _, _, username = key.partition("|")
            if username != target:
                continue
            total += len([ts for ts in stamps if now_ts - ts < guard.window_sec])
    return total


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
    """登录失败视图：admin 看全站，其他人只看自己账号的聚合。

    内存里只有"窗口内还在计数"的 IP+用户名组合；窗口一过就清掉，
    所以这里也按配置的窗口长度过滤一遍，避免展示已经失效的记录。
    """
    limit = max(1, min(limit, 200))
    now_ts = time.time()
    guard = _login_guard(request.app.state.settings)
    is_admin = user.get("role") == "admin"
    owner = (user.get("username") or "").strip()

    conn = request.app.state.db.connect()
    try:
        if is_admin:
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
            return {
                "scope": SCOPE_ALL,
                "window_sec": guard.window_sec,
                "max_failures": guard.max_failures,
                "active": _active_throttle(now_ts, guard),
                "stats": {"total": int(counts["total"] or 0), "today": int(counts["today"] or 0)},
                "recent": [_audit_row(row) for row in recent],
            }

        # 非 admin：audit_logs.actor 记的是「被尝试的用户名」（见 auth.login 写入点），
        # 所以按 actor=自己 过滤得到的是「针对我这个账号」的记录，而不是「我做过什么」。
        # 只回聚合数字：不给行的 ip、不给别人的用户名，也不做撞库推测。
        if owner:
            counts = conn.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END), 0) AS today, "
                "COALESCE(MAX(ts), 0) AS last_ts "
                "FROM audit_logs WHERE action = 'login_failed' AND actor = ?",
                (_day_start(), owner),
            ).fetchone()
            total = int(counts["total"] or 0)
            today = int(counts["today"] or 0)
            last_ts = int(counts["last_ts"] or 0)
        else:
            total = today = last_ts = 0

        return {
            "scope": SCOPE_SELF,
            "window_sec": guard.window_sec,
            "max_failures": guard.max_failures,
            "active": [],
            "stats": {"total": total, "today": today},
            "recent": [],
            # 只对自己的账号有意义、且不含任何第三方标识的两个数
            "window_failures": _own_window_failures(owner, now_ts, guard),
            "last_failed_ts": last_ts,
        }
    finally:
        conn.close()


class LoginFailureClear(BaseModel):
    ip: str = ""
    username: str = ""


@router.post("/security/login-failures/clear")
def clear_login_failures(
    body: LoginFailureClear, request: Request, user: dict = Depends(admin_user)
):
    """按 IP / 用户名定向清除登录失败计数与冷却。

    只清「计数与冷却」，不删 audit_logs 里的 login_failed 证据；节流阈值与窗口不变，
    清完之后新的失败仍按原规则继续累计。
    """
    ip = body.ip.strip()
    username = body.username.strip()
    if not ip and not username:
        raise HTTPException(status_code=400, detail="需至少指定 ip 或 username 之一")

    throttle_keys = 0
    target_user = username.lower()
    with _login_lock:
        for key in list(_login_failures):
            key_ip, _, key_user = key.partition("|")
            if ip and key_ip != ip:
                continue
            if target_user and key_user != target_user:
                continue
            _login_failures.pop(key, None)
            throttle_keys += 1

    users_cleared: list[str] = []
    conn = request.app.state.db.connect()
    try:
        if username:
            rows = conn.execute(
                "SELECT username FROM users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE users SET failed_login_count = 0, last_failed_at = 0 WHERE username = ?",
                    (row["username"],),
                )
                users_cleared.append(row["username"])
            conn.commit()
        audit_event(
            conn,
            user["username"],
            "security.login_failures_cleared",
            username or ip,
            f"ip={ip or '-'} username={username or '-'} "
            f"throttle_keys={throttle_keys} users={len(users_cleared)}",
            request.client.host if request.client else "",
        )
    finally:
        conn.close()
    return {
        "ok": True,
        "cleared": {"throttle_keys": throttle_keys, "users": len(users_cleared)},
        "ip": ip,
        "username": username,
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
    # audit_logs.actor 存的是执行者的用户名（见 security.audit_event 的写入点），
    # 非 admin 只能看自己那部分；admin 不过滤，可按 actor 查任意人。
    is_admin = user.get("role") == "admin"
    own_actor = user.get("username") or ""
    asked_actor = actor.strip() if actor else ""
    if not is_admin:
        if asked_actor and asked_actor != own_actor:
            raise PermissionDenied(AUDIT_ACTOR_DENIED, role=user.get("role"))
        asked_actor = own_actor
    clauses: list[str] = []
    params: list[Any] = []
    if action:
        clauses.append("action = ?")
        params.append(action.strip())
    if asked_actor:
        clauses.append("actor = ?")
        params.append(asked_actor)
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
