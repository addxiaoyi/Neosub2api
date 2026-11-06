import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..api_utils import now
from ..config import Settings
from ..security import (
    SESSION_COOKIE,
    admin_user,
    create_session,
    current_user,
    hash_password,
    revoke_user_sessions,
    verify_password,
)
from ..totp import (
    RECOVERY_CODE_COUNT,
    STEP_SEC,
    counter_at,
    hash_recovery_code,
    new_recovery_codes,
    new_secret,
    normalize_recovery_code,
    otpauth_uri,
    verify as verify_totp,
)

router = APIRouter(prefix="/api", tags=["auth"])

# 登录失败节流：按 (来源 IP, 用户名) 做滑动窗口计数，超限后指数退避冷却（S-12）。
# 只按用户名计数会让攻击者用一个 IP 的坏请求锁死任意账号（正确的口令也进不来），
# 所以维度必须带上来源 IP；窗口/冷却阈值由 settings 提供（见 LoginGuard）。
# 库内 failed_login_count 的封顶值，只作留痕，不参与冷却判定
LOGIN_MAX_FAILURES = 5
# 退避步数上限：2^10 已远超默认冷却上限，钳住只是防止极端配置下 2**n 溢出
COOLDOWN_BACKOFF_MAX_STEPS = 10
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()

# 2FA 挑战有效期与尝试上限：5 次错误即作废，避免 6 位码被在线暴力枚举
CHALLENGE_TTL_SEC = 300
CHALLENGE_MAX_ATTEMPTS = 5
# 管理员豁免冷却，避免运维把自己锁在门外（需求明确要求 addxiaoyi 不被锁死）
ADMIN_ROLE = "admin"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _login_key(ip: str, username: str) -> str:
    return f"{ip}|{username.strip().lower()}"


@dataclass(frozen=True)
class LoginGuard:
    """登录节流阈值的不可变快照；单测可直接构造，不必起完整 app。"""

    max_failures: int
    window_sec: int
    cooldown_sec: int
    cooldown_max_sec: int


def _login_guard(settings: Settings) -> LoginGuard:
    return LoginGuard(
        max_failures=max(1, int(settings.login_max_failures)),
        window_sec=max(1, int(settings.login_window_sec)),
        cooldown_sec=max(1, int(settings.login_cooldown_sec)),
        cooldown_max_sec=max(1, int(settings.login_cooldown_max_sec)),
    )


def _prune_failures(key: str, now_ts: float, window_sec: int) -> list[float]:
    stamps = [ts for ts in _login_failures.get(key, ()) if now_ts - ts < window_sec]
    if stamps:
        _login_failures[key] = stamps
    else:
        _login_failures.pop(key, None)
    return stamps


def _login_wait(key: str, guard: LoginGuard, at: float | None = None) -> int:
    """窗口内失败达上限后返回剩余冷却秒数，否则 0。

    冷却从「窗口内最后一次失败」起算，并按超出上限的失败次数翻倍退避。
    429 拒绝不写失败记录，所以客户端在冷却期反复重试不会把冷却一直往后推。
    """
    now_ts = time.time() if at is None else float(at)
    with _login_lock:
        stamps = _prune_failures(key, now_ts, guard.window_sec)
        if len(stamps) < guard.max_failures:
            return 0
        steps = min(len(stamps) - guard.max_failures, COOLDOWN_BACKOFF_MAX_STEPS)
        span = min(guard.cooldown_sec * (2 ** steps), guard.cooldown_max_sec)
        left = span - (now_ts - stamps[-1])
    return max(1, int(left)) if left > 0 else 0


def _login_failed(key: str, at: float | None = None) -> None:
    with _login_lock:
        _login_failures.setdefault(key, []).append(time.time() if at is None else float(at))


def _login_ok(key: str) -> None:
    with _login_lock:
        _login_failures.pop(key, None)


def _login_throttled(seconds: int) -> JSONResponse:
    """冷却响应：文案单独成句，剩余秒数走结构化字段。

    前端据此本地倒计时；把秒数拼进中文句子会让界面无法逐秒刷新，
    也会被读成「请稍后再试，还有 58 秒」这种双逗号。
    """
    return JSONResponse(
        status_code=429,
        content={"detail": "登录失败次数过多，请稍后再试。", "retry_after": seconds},
    )


_USER_COLUMNS = (
    "username, password_hash, role, email, totp_enabled, totp_secret, "
    "totp_pending_secret, totp_last_counter, failed_login_count, last_failed_at"
)


def _user_row(conn, username: str):
    return conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE username = ?", (username,)).fetchone()


def _record_failure(conn, username: str) -> None:
    """库里的计数只作失败留痕，不参与冷却判定（冷却维度是 IP+用户名，见 _login_wait）。"""
    conn.execute(
        "UPDATE users SET failed_login_count = MIN(failed_login_count + 1, ?), last_failed_at = ? "
        "WHERE username = ?",
        (LOGIN_MAX_FAILURES, now(), username),
    )
    conn.commit()


def _clear_failures(conn, username: str) -> None:
    conn.execute(
        "UPDATE users SET failed_login_count = 0, last_failed_at = 0 WHERE username = ?",
        (username,),
    )


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    ttl = request.app.state.settings.session_ttl_sec
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=request.app.state.settings.cookie_secure,
        max_age=ttl,
        path="/",
    )


def _issue_challenge(conn, username: str) -> str:
    token = secrets.token_urlsafe(32)
    ts = now()
    conn.execute("DELETE FROM login_challenges WHERE expires_at < ?", (ts,))
    conn.execute(
        "INSERT INTO login_challenges(token, username, created_at, expires_at) VALUES(?,?,?,?)",
        (token, username, ts, ts + CHALLENGE_TTL_SEC),
    )
    conn.commit()
    return token


class LoginBody(BaseModel):
    username: str
    password: str


class RegisterBody(BaseModel):
    username: str
    password: str
    email: str | None = None


class StartLoginBody(BaseModel):
    realm: str | None = None


class TotpCodeBody(BaseModel):
    code: str


class TotpDisableBody(BaseModel):
    password: str
    code: str


class TotpVerifyBody(BaseModel):
    challenge: str
    code: str | None = None
    recovery_code: str | None = None


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
    username = body.username.strip()
    key = _login_key(ip, username)
    guard = _login_guard(request.app.state.settings)
    try:
        row = _user_row(conn, username)
        # 冷却只按 (来源 IP, 用户名) 计数：同名账号在别的 IP 上被撞库，
        # 不能把这里拿正确口令的用户锁死。未知与已知账号走同一条判定，
        # 因此 retry_after 不会成为区分账号是否存在的信号。管理员豁免。
        is_admin = row is not None and row["role"] == ADMIN_ROLE
        if not is_admin:
            wait = _login_wait(key, guard)
            if wait:
                return _login_throttled(wait)
        if row is None or not verify_password(body.password, row["password_hash"]):
            # 429 拒绝在冷却判定处就返回了，不会走到这里；失败窗口只累计真实的口令错误。
            # 管理员豁免冷却，连失败计数也不落，免得安全页把运维账号显示成「已锁」。
            if not is_admin:
                _login_failed(key)
            if row is not None:
                _record_failure(conn, row["username"])
            audit(conn, username or "anonymous", "login_failed", "", "用户名或密码错误", ip)
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        _login_ok(key)
        _clear_failures(conn, row["username"])
        if int(row["totp_enabled"] or 0):
            # 密码正确也不发 token：只给一次性 challenge，验证码换会话
            challenge = _issue_challenge(conn, row["username"])
            audit(conn, row["username"], "login_totp_required", "", "", ip)
            return {
                "ok": False,
                "need_totp": True,
                "challenge": challenge,
                "expires_in": CHALLENGE_TTL_SEC,
            }
        token = create_session(conn, row["username"], request.app.state.settings.session_ttl_sec)
        audit(conn, row["username"], "login", "", "", ip)
    finally:
        conn.close()

    _set_session_cookie(request, response, token)
    return {"ok": True, "username": row["username"], "role": row["role"]}


@router.post("/auth/register")
def register(body: RegisterBody, request: Request):
    """自助注册：默认关闭，开启后落 role=user 的普通账号。"""
    settings = request.app.state.settings
    if not settings.allow_registration:
        raise HTTPException(status_code=403, detail="注册未开启，请联系管理员")
    username = body.username.strip()
    if not (3 <= len(username) <= 32) or not _USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="用户名需为 3-32 位字母、数字、点、下划线或连字符")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="密码至少 8 个字符")
    email = (body.email or "").strip() or None
    if email is not None and not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")

    conn = request.app.state.db.connect()
    ip = request.client.host if request.client else ""
    try:
        # 唯一性交给数据库约束：并发注册时不会出现「先查后插」的竞态
        conn.execute(
            "INSERT INTO users(username, password_hash, role, email, created_at) VALUES(?,?,?,?,?)",
            (username, hash_password(body.password), "viewer", email, now()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        duplicate_email = (
            email is not None
            and conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() is not None
        )
        raise HTTPException(
            status_code=409,
            detail="邮箱已存在" if duplicate_email else "用户名已存在",
        )
    finally:
        conn.close()
    conn = request.app.state.db.connect()
    try:
        audit(conn, username, "register", "", "", ip)
    finally:
        conn.close()
    return {"ok": True, "username": username, "role": "viewer"}


@router.post("/auth/refresh")
def refresh(request: Request, response: Response, user: dict = Depends(current_user)):
    """续期当前会话：签发新 token 并作废旧 token，仍是同一张 sessions 表，不引入第二套体系。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    conn = request.app.state.db.connect()
    ip = request.client.host if request.client else ""
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        new_token = create_session(conn, user["username"], request.app.state.settings.session_ttl_sec)
        audit(conn, user["username"], "session_refresh", "", "", ip)
    finally:
        conn.close()
    _set_session_cookie(request, response, new_token)
    return {
        "ok": True,
        "username": user["username"],
        "role": user["role"],
        "expires_in": request.app.state.settings.session_ttl_sec,
    }


@router.get("/auth/totp/status")
def totp_status(request: Request, user: dict = Depends(current_user)):
    conn = request.app.state.db.connect()
    try:
        row = conn.execute(
            "SELECT totp_enabled FROM users WHERE username = ?", (user["username"],)
        ).fetchone()
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM totp_recovery_codes WHERE username = ? AND used_at IS NULL",
            (user["username"],),
        ).fetchone()["n"]
    finally:
        conn.close()
    return {
        "enabled": bool(row and row["totp_enabled"]),
        "recovery_codes_left": int(remaining),
        "period": STEP_SEC,
    }


@router.post("/auth/totp/setup")
def totp_setup(request: Request, user: dict = Depends(current_user)):
    conn = request.app.state.db.connect()
    try:
        row = _user_row(conn, user["username"])
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if int(row["totp_enabled"] or 0):
            raise HTTPException(status_code=400, detail="两步验证已开启，如需更换密钥请先关闭")
        secret = new_secret()
        conn.execute(
            "UPDATE users SET totp_pending_secret = ? WHERE username = ?",
            (secret, user["username"]),
        )
        audit(conn, user["username"], "totp_setup", "", "", request.client.host if request.client else "")
    finally:
        conn.close()
    return {
        "secret": secret,
        "otpauth_uri": otpauth_uri(user["username"], secret),
        "algorithm": "SHA1",
        "digits": 6,
        "period": STEP_SEC,
    }


@router.post("/auth/totp/enable")
def totp_enable(body: TotpCodeBody, request: Request, user: dict = Depends(current_user)):
    conn = request.app.state.db.connect()
    ip = request.client.host if request.client else ""
    try:
        row = _user_row(conn, user["username"])
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if int(row["totp_enabled"] or 0):
            raise HTTPException(status_code=400, detail="两步验证已开启")
        pending = (row["totp_pending_secret"] or "").strip()
        if not pending:
            raise HTTPException(status_code=400, detail="请先调用 setup 获取密钥")
        counter, reason = verify_totp(pending, body.code, 0)
        if counter is None:
            raise HTTPException(
                status_code=400,
                detail="验证码已使用，请等待下一个时间窗" if reason == "replay" else "验证码不正确",
            )
        codes = new_recovery_codes()
        conn.execute("DELETE FROM totp_recovery_codes WHERE username = ?", (user["username"],))
        conn.executemany(
            "INSERT INTO totp_recovery_codes(username, code_hash, used_at) VALUES(?,?,NULL)",
            [(user["username"], hash_recovery_code(code)) for code in codes],
        )
        conn.execute(
            "UPDATE users SET totp_secret = ?, totp_enabled = 1, totp_pending_secret = '', "
            "totp_last_counter = ? WHERE username = ?",
            (pending, counter, user["username"]),
        )
        conn.commit()
        audit(conn, user["username"], "totp_enabled", "", "", ip)
    finally:
        conn.close()
    return {
        "ok": True,
        "enabled": True,
        "recovery_codes": codes,
        "recovery_codes_count": RECOVERY_CODE_COUNT,
    }


@router.post("/auth/totp/disable")
def totp_disable(body: TotpDisableBody, request: Request, user: dict = Depends(current_user)):
    conn = request.app.state.db.connect()
    ip = request.client.host if request.client else ""
    try:
        row = _user_row(conn, user["username"])
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if not int(row["totp_enabled"] or 0):
            raise HTTPException(status_code=400, detail="两步验证未开启")
        # 关闭必须同时给密码和验证码：只拿到一次验证码不应能拆掉第二因子
        if not verify_password(body.password, row["password_hash"]):
            # 用 400 而不是 401：前端拦截器会因 401 强制跳登录页，
            # 关 2FA 输错密码不该表现为「被登出」
            raise HTTPException(status_code=400, detail="密码不正确")
        counter, reason = verify_totp(
            row["totp_secret"], body.code, int(row["totp_last_counter"] or 0)
        )
        if counter is None:
            raise HTTPException(
                status_code=400,
                detail="验证码已使用，请等待下一个时间窗" if reason == "replay" else "验证码不正确",
            )
        conn.execute(
            "UPDATE users SET totp_enabled = 0, totp_secret = '', totp_pending_secret = '', "
            "totp_last_counter = 0 WHERE username = ?",
            (user["username"],),
        )
        conn.execute("DELETE FROM totp_recovery_codes WHERE username = ?", (user["username"],))
        conn.commit()
        audit(conn, user["username"], "totp_disabled", "", "", ip)
    finally:
        conn.close()
    return {"ok": True, "enabled": False}


def _consume_recovery_code(conn, username: str, raw_code: str) -> bool:
    rows = conn.execute(
        "SELECT id, code_hash FROM totp_recovery_codes WHERE username = ? AND used_at IS NULL",
        (username,),
    ).fetchall()
    wanted = normalize_recovery_code(raw_code)
    for row in rows:
        if verify_password(wanted, row["code_hash"]):
            updated = conn.execute(
                "UPDATE totp_recovery_codes SET used_at = ? WHERE id = ? AND used_at IS NULL",
                (now(), row["id"]),
            ).rowcount
            conn.commit()
            return updated == 1
    return False


@router.post("/auth/totp/verify")
def totp_verify(body: TotpVerifyBody, request: Request, response: Response):
    conn = request.app.state.db.connect()
    ip = request.client.host if request.client else ""
    try:
        challenge = conn.execute(
            "SELECT username, expires_at, attempts FROM login_challenges WHERE token = ?",
            (body.challenge.strip(),),
        ).fetchone()
        if challenge is None:
            raise HTTPException(status_code=401, detail="验证已过期，请重新登录")
        if int(challenge["expires_at"]) < now():
            conn.execute("DELETE FROM login_challenges WHERE token = ?", (body.challenge.strip(),))
            conn.commit()
            raise HTTPException(status_code=401, detail="验证已过期，请重新登录")
        username = challenge["username"]
        row = _user_row(conn, username)
        if row is None or not int(row["totp_enabled"] or 0):
            raise HTTPException(status_code=400, detail="该账号未开启两步验证")

        def _fail(detail: str, status_code: int = 401) -> None:
            attempts = int(challenge["attempts"]) + 1
            if attempts >= CHALLENGE_MAX_ATTEMPTS:
                conn.execute("DELETE FROM login_challenges WHERE token = ?", (body.challenge.strip(),))
            else:
                conn.execute(
                    "UPDATE login_challenges SET attempts = ? WHERE token = ?",
                    (attempts, body.challenge.strip()),
                )
            conn.commit()
            raise HTTPException(status_code=status_code, detail=detail)

        if body.recovery_code:
            if not _consume_recovery_code(conn, username, body.recovery_code):
                _fail("恢复码无效或已使用")
            # 恢复码登录也推进 last_counter，避免同一窗口的 TOTP 码还能再用；
            # 用 MAX 保持单调，别把之前已用过的更晚时间窗又放回可用状态
            conn.execute(
                "UPDATE users SET totp_last_counter = MAX(totp_last_counter, ?) WHERE username = ?",
                (counter_at(), username),
            )
        else:
            counter, reason = verify_totp(
                row["totp_secret"],
                body.code or "",
                int(row["totp_last_counter"] or 0),
            )
            if counter is None:
                _fail(
                    "验证码已使用，请等待下一个时间窗" if reason == "replay" else "验证码不正确"
                )
            conn.execute(
                "UPDATE users SET totp_last_counter = ? WHERE username = ?",
                (counter, username),
            )
        conn.execute("DELETE FROM login_challenges WHERE token = ?", (body.challenge.strip(),))
        token = create_session(conn, username, request.app.state.settings.session_ttl_sec)
        audit(conn, username, "login_totp", "", "", ip)
    finally:
        conn.close()
    _set_session_cookie(request, response, token)
    return {"ok": True, "username": username, "role": row["role"]}


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
