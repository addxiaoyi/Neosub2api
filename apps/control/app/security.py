import base64
import hashlib
import hmac
import secrets
import time
from typing import Any

from fastapi import Depends, HTTPException, Request

PBKDF2_ROUNDS = 200_000
SESSION_COOKIE = "u2a_session"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_b64, digest_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def audit_event(conn, actor: str, action: str, target: str, detail: str = "", ip: str = "") -> None:
    """写审计日志；detail 里不要放密钥明文。"""
    conn.execute(
        "INSERT INTO audit_logs(ts, actor, action, target, detail, ip) VALUES(?,?,?,?,?,?)",
        (int(time.time()), actor, action, target, detail, ip),
    )
    conn.commit()


def create_session(conn, username: str, ttl: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    # 顺手清过期会话，避免 sessions 表只增不减（S-13）
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute(
        "INSERT INTO sessions(token, username, created_at, expires_at) VALUES(?, ?, ?, ?)",
        (token, username, now, now + ttl),
    )
    conn.commit()
    return token


def revoke_user_sessions(conn, username: str) -> None:
    conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
    conn.commit()


def session_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    conn = request.app.state.db.connect()
    try:
        row = conn.execute(
            "SELECT s.username, s.expires_at, u.role FROM sessions s "
            "JOIN users u ON u.username = s.username WHERE s.token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < int(time.time()):
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return None
        return {"username": row["username"], "role": row["role"]}
    finally:
        conn.close()


def current_user(request: Request) -> dict[str, Any]:
    user = session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def admin_user(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
