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
    try:
        row = conn.execute(
            "SELECT username, password_hash, role FROM users WHERE username = ?",
            (body.username.strip(),),
        ).fetchone()
        if row is None or not verify_password(body.password, row["password_hash"]):
            audit(conn, body.username.strip() or "anonymous", "login_failed", "", "用户名或密码错误", ip)
            raise HTTPException(status_code=401, detail="用户名或密码错误")
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
