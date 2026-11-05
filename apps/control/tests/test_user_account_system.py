"""用户账号体系回归：注册开关、登录冷却、会话续期、TOTP 2FA 与恢复码。

TOTP 校验不 mock：测试进程按 RFC 6238 自己算码，服务端用真实实现校验；
时间窗只靠「当前 / 前 / 后一格」的容错覆盖，不修改服务端时钟。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
import struct
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

CONTROL = Path(__file__).resolve().parents[1]
if str(CONTROL) not in sys.path:
    sys.path.insert(0, str(CONTROL))

from app.db import Database  # noqa: E402
from app.security import hash_password  # noqa: E402

COOKIE = "u2a_session"


def _client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)


def _code(secret: str, offset: int = 0, ts: float | None = None) -> str:
    counter = int((time.time() if ts is None else ts) // 30) + offset
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    pos = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[pos:pos + 4])[0] & 0x7FFFFFFF
    return str(value % 1_000_000).zfill(6)


def _new_user(admin: httpx.Client, password: str = "user-pass-1234") -> str:
    name = f"acct-{uuid.uuid4().hex[:10]}"
    created = admin.post("/api/users", json={"username": name, "password": password, "role": "viewer"})
    assert created.status_code == 200, created.text
    return name


@pytest.fixture(scope="module")
def registration_on(admin: httpx.Client):
    toggled = admin.post("/api/settings/runtime-config", json={"allow_registration": True})
    assert toggled.status_code == 200, toggled.text
    yield True
    admin.post("/api/settings/runtime-config", json={"allow_registration": False})


def test_register_disabled_returns_403(admin: httpx.Client) -> None:
    admin.post("/api/settings/runtime-config", json={"allow_registration": False})
    name = f"reg-{uuid.uuid4().hex[:10]}"
    with _client(str(admin.base_url)) as fresh:
        resp = fresh.post("/api/auth/register", json={"username": name, "password": "pw-12345678"})
    assert resp.status_code == 403, resp.text
    assert "注册未开启" in resp.json()["detail"]


def test_register_enabled_creates_user_and_conflicts(admin: httpx.Client, registration_on) -> None:
    name = f"reg-{uuid.uuid4().hex[:8]}"
    email = f"{name}@example.com"
    body = {"username": name, "password": "pw-12345678", "email": email}
    with _client(str(admin.base_url)) as fresh:
        ok = fresh.post("/api/auth/register", json=body)
        assert ok.status_code == 200, ok.text
        assert ok.json()["role"] == "viewer"
        listed = admin.get("/api/users").json()
        assert "user" not in {row["role"] for row in listed}

        dup_name = fresh.post(
            "/api/auth/register",
            json={"username": name, "password": "pw-12345678", "email": f"other-{name}@example.com"},
        )
        assert dup_name.status_code == 409, dup_name.text
        assert "用户名已存在" in dup_name.json()["detail"]

        dup_email = fresh.post(
            "/api/auth/register",
            json={"username": f"{name}-2", "password": "pw-12345678", "email": email},
        )
        assert dup_email.status_code == 409, dup_email.text
        assert "邮箱已存在" in dup_email.json()["detail"]


def test_login_cooldown_and_admin_exemption(admin: httpx.Client, base_url: str) -> None:
    name = _new_user(admin)
    with _client(base_url) as fresh:
        for _ in range(5):
            wrong = fresh.post("/api/login", json={"username": name, "password": "wrong-pass"})
            assert wrong.status_code == 401, wrong.text
        locked = fresh.post("/api/login", json={"username": name, "password": "wrong-pass"})
        assert locked.status_code == 429, locked.text
        body = locked.json()
        assert "稍后再试" in body["detail"] and "秒" not in body["detail"]
        # 剩余时间必须是结构化字段，前端才能本地倒计时；不能只藏在中文句子里
        first_left = body.get("retry_after")
        assert isinstance(first_left, int) and 0 < first_left <= 60, body

        time.sleep(1.2)
        # 冷却期内正确密码同样被拒：说明拦截发生在校验之前
        still = fresh.post("/api/login", json={"username": name, "password": "user-pass-1234"})
        assert still.status_code == 429, still.text
        second_left = still.json().get("retry_after")
        assert isinstance(second_left, int), still.text
        assert second_left < first_left, f"retry_after 应随时钟递减：{first_left} -> {second_left}"

    # 管理员豁免冷却，连续输错不会把运维锁在门外
    with _client(base_url) as fresh:
        for _ in range(6):
            wrong = fresh.post("/api/login", json={"username": "admin", "password": "wrong-pass"})
            assert wrong.status_code == 401, wrong.text
        ok = fresh.post("/api/login", json={"username": "admin", "password": "test"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["role"] == "admin"


def test_session_refresh_rotates_token(admin: httpx.Client, base_url: str) -> None:
    name = _new_user(admin)
    with _client(base_url) as fresh:
        ok = fresh.post("/api/login", json={"username": name, "password": "user-pass-1234"})
        assert ok.status_code == 200, ok.text
        old = fresh.cookies.get(COOKIE)
        assert old

        rotated = fresh.post("/api/auth/refresh")
        assert rotated.status_code == 200, rotated.text
        new = fresh.cookies.get(COOKIE)
        assert new and new != old
        assert fresh.get("/api/me").status_code == 200

    with httpx.Client(
        base_url=base_url, trust_env=False, timeout=10.0, cookies={COOKIE: old}
    ) as stale:
        assert stale.get("/api/me").status_code == 401


def _setup_and_enable(client: httpx.Client) -> tuple[str, list[str], str]:
    setup = client.post("/api/auth/totp/setup")
    assert setup.status_code == 200, setup.text
    payload = setup.json()
    secret = payload["secret"]
    assert payload["otpauth_uri"].startswith("otpauth://totp/") and f"secret={secret}" in payload["otpauth_uri"]

    enable_code = _code(secret, 0)
    enabled = client.post("/api/auth/totp/enable", json={"code": enable_code})
    assert enabled.status_code == 200, enabled.text
    codes = enabled.json()["recovery_codes"]
    assert len(codes) == 8
    # 一并交出实际提交过的码：重放用例必须复用它，不能按新时钟再生成一次
    return secret, codes, enable_code


def test_totp_login_requires_code_and_replay_rejected(admin: httpx.Client, base_url: str) -> None:
    name = _new_user(admin)
    with _client(base_url) as owner:
        assert owner.post("/api/login", json={"username": name, "password": "user-pass-1234"}).status_code == 200
        secret, _, enable_code = _setup_and_enable(owner)

    # 重放用例必须复用**同一串**码。若每次按当前时钟重新生成，跨过 30 秒时间窗后
    # 第二次拿到的是新窗口的码，服务端按新码接受（200），会把「测试自己换了码」
    # 误报成「服务端放行重放」——全量跑时正是这种假红。
    next_code = _code(secret, 1)

    with _client(base_url) as fresh:
        first = fresh.post("/api/login", json={"username": name, "password": "user-pass-1234"})
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["need_totp"] is True
        assert fresh.cookies.get(COOKIE) is None, "缺 2FA 时不能签发会话 cookie"
        challenge = body["challenge"]

        # 开启时用掉的时间窗不能再用：同窗重放被拒
        replay = fresh.post("/api/auth/totp/verify", json={"challenge": challenge, "code": enable_code})
        assert replay.status_code == 401, replay.text
        assert "已使用" in replay.json()["detail"]

        ok = fresh.post("/api/auth/totp/verify", json={"challenge": challenge, "code": next_code})
        assert ok.status_code == 200, ok.text
        assert fresh.cookies.get(COOKIE)
        assert fresh.get("/api/me").json()["username"] == name

        again = fresh.post("/api/login", json={"username": name, "password": "user-pass-1234"}).json()
        reused = fresh.post(
            "/api/auth/totp/verify", json={"challenge": again["challenge"], "code": next_code}
        )
        assert reused.status_code == 401 and "已使用" in reused.json()["detail"]


def test_totp_recovery_code_single_use(admin: httpx.Client, base_url: str) -> None:
    name = _new_user(admin)
    with _client(base_url) as owner:
        assert owner.post("/api/login", json={"username": name, "password": "user-pass-1234"}).status_code == 200
        _, codes, _ = _setup_and_enable(owner)

    with _client(base_url) as fresh:
        challenge = fresh.post(
            "/api/login", json={"username": name, "password": "user-pass-1234"}
        ).json()["challenge"]
        first = fresh.post(
            "/api/auth/totp/verify",
            json={"challenge": challenge, "recovery_code": codes[0]},
        )
        assert first.status_code == 200, first.text
        assert fresh.cookies.get(COOKIE)

        again = fresh.post("/api/login", json={"username": name, "password": "user-pass-1234"}).json()
        second = fresh.post(
            "/api/auth/totp/verify",
            json={"challenge": again["challenge"], "recovery_code": codes[0]},
        )
        assert second.status_code == 401, second.text
        assert "恢复码" in second.json()["detail"]


def test_totp_disable_requires_password_and_code(admin: httpx.Client, base_url: str) -> None:
    name = _new_user(admin)
    with _client(base_url) as owner:
        assert owner.post("/api/login", json={"username": name, "password": "user-pass-1234"}).status_code == 200
        secret, _, _ = _setup_and_enable(owner)

        wrong_pw = owner.post(
            "/api/auth/totp/disable",
            json={"password": "wrong-pass", "code": _code(secret, 1)},
        )
        assert wrong_pw.status_code == 400 and "密码" in wrong_pw.json()["detail"]

        bad_code = owner.post(
            "/api/auth/totp/disable",
            json={"password": "user-pass-1234", "code": str((int(_code(secret, 1)) + 1) % 1_000_000).zfill(6)},
        )
        assert bad_code.status_code == 400, bad_code.text

        off = owner.post(
            "/api/auth/totp/disable",
            json={"password": "user-pass-1234", "code": _code(secret, 1)},
        )
        assert off.status_code == 200, off.text
        assert owner.get("/api/auth/totp/status").json()["enabled"] is False

    with _client(base_url) as fresh:
        plain = fresh.post("/api/login", json={"username": name, "password": "user-pass-1234"})
        assert plain.status_code == 200 and "need_totp" not in plain.json()


def test_legacy_admin_addxiaoyi_login_still_works(db_path: str, base_url: str) -> None:
    """回归：老库里已有的管理员行（无 2FA 字段）登录仍然成功。

    测试库没有线上那个 addxiaoyi 行，这里按老 schema 的写法直接插一条同构记录，
    验证新增列默认值不会影响既有账号。
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM users WHERE username = ?", ("addxiaoyi",))
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            ("addxiaoyi", hash_password("Lsc123456"), "admin", int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()

    with _client(base_url) as fresh:
        resp = fresh.post("/api/login", json={"username": "addxiaoyi", "password": "Lsc123456"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] == "admin"


def test_legacy_role_user_migrated_to_viewer(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    Database(path)
    conn = sqlite3.connect(path)
    try:
        now = int(time.time())
        for username, role in (("legacy", "user"), ("root", "admin")):
            conn.execute(
                "INSERT INTO users(username, password_hash, role, email, created_at) VALUES(?,?,?,?,?)",
                (username, hash_password("pw-12345678"), role, f"{username}@example.com", now),
            )
        conn.commit()
    finally:
        conn.close()

    Database(path)
    Database(path)  # 再跑一次确认幂等

    conn = sqlite3.connect(path)
    try:
        roles = dict(conn.execute("SELECT username, role FROM users").fetchall())
    finally:
        conn.close()
    assert roles["legacy"] == "viewer"
    assert roles["root"] == "admin"
