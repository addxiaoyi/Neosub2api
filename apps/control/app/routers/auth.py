import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ..api_utils import now
from ..security import (
    SESSION_COOKIE,
    admin_user,
    create_session,
    current_user,
    revoke_user_sessions,
    verify_password,
)

router = APIRouter(prefix="/api", tags=["auth"])

# 登录失败节流：同一 IP + 用户名窗口内失败过多就短暂拒绝（S-12）
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SEC = 900
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def _login_key(ip: str, username: str) -> str:
    return f"{ip}|{username.strip().lower()}"


def _login_wait(key: str) -> int:
    now_ts = time.time()
    with _login_lock:
        stamps = [ts for ts in _login_failures.get(key, ()) if now_ts - ts < LOGIN_WINDOW_SEC]
        if stamps:
            _login_failures[key] = stamps
        else:
            _login_failures.pop(key, None)
        if len(stamps) < LOGIN_MAX_FAILURES:
            return 0
        return max(1, int(LOGIN_WINDOW_SEC - (now_ts - stamps[0])))


def _login_failed(key: str) -> None:
    with _login_lock:
        _login_failures.setdefault(key, []).append(time.time())


def _login_ok(key: str) -> None:
    with _login_lock:
        _login_failures.pop(key, None)


class LoginBody(BaseModel):
    username: str
    password: str


class StartLoginBody(BaseModel):
    realm: str | None = None


def _realm(raw: str | None) -> str:
    return "global" if str(raw or "").strip().lower() == "global" else "cn"


def _login_adapter(request: Request):
    """扫码登录统一走适配器；路由只做参数与错误翻译，不直接拼上游 HTTP。"""
    for adapter in request.app.state.registry.enabled():
        if adapter.spec.capabilities.get("oauth"):
            return adapter
    raise HTTPException(
        status_code=503,
        detail="扫码登录不可用：没有已启用的扫码服务商，请先在「服务商」页启用 CodeBuddy 并配置上游地址",
    )


def audit(conn, actor: str, action: str, target: str, detail: str, ip: str) -> None:
    conn.execute(
        "INSERT INTO audit_logs(ts, actor, action, target, detail, ip) VALUES(?,?,?,?,?,?)",
        (now(), actor, action, target, detail, ip),
    )
    conn.commit()


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    conn = request.app.state.db.connect()
    ip = request.client.host if request.client else ""
    key = _login_key(ip, body.username)
    wait = _login_wait(key)
    if wait:
        conn.close()
        raise HTTPException(status_code=429, detail=f"登录失败次数过多，请 {wait} 秒后再试")
    try:
        row = conn.execute(
            "SELECT username, password_hash, role FROM users WHERE username = ?",
            (body.username.strip(),),
        ).fetchone()
        if row is None or not verify_password(body.password, row["password_hash"]):
            _login_failed(key)
            audit(conn, body.username.strip() or "anonymous", "login_failed", "", "用户名或密码错误", ip)
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        _login_ok(key)
        token = create_session(conn, row["username"], request.app.state.settings.session_ttl_sec)
        audit(conn, row["username"], "login", "", "", ip)
    finally:
        conn.close()

    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=request.app.state.settings.cookie_secure,
        max_age=request.app.state.settings.session_ttl_sec,
        path="/",
    )
    return {"ok": True, "username": row["username"], "role": row["role"]}


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        conn = request.app.state.db.connect()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(current_user)):
    return user


@router.post("/auth/start")
async def auth_start(
    request: Request,
    body: StartLoginBody | None = None,
    user: dict = Depends(admin_user),
):
    """发起扫码登录（CodeBuddy 管理端契约：body/query 都可传 realm）。"""
    adapter = _login_adapter(request)
    return await adapter.start_login(_realm(body.realm if body else None))


@router.get("/auth/poll")
async def auth_poll(
    request: Request,
    state: str = "",
    realm: str | None = None,
    region: str | None = None,
    user: dict = Depends(admin_user),
):
    """轮询扫码结果。state 为空直接回 invalid，与上游行为一致。"""
    if not state:
        return {"status": "invalid"}
    adapter = _login_adapter(request)
    return await adapter.poll_login(state, _realm(realm) if realm else None, region or None)


@router.post("/sessions/revoke")
def revoke_sessions(request: Request, user: dict = Depends(current_user)):
    conn = request.app.state.db.connect()
    try:
        revoke_user_sessions(conn, user["username"])
    finally:
        conn.close()
    return {"ok": True, "relogin_required": True}
