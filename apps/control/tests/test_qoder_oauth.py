"""Qoder OAuth 设备流协议层：PKCE、授权 URL、轮询状态机、刷新与到期。

不连真上游：HTTP 由假 client 分派，只验证状态机与凭据形状；
出站守卫单独用真实 guard 校验（不解析外网域名，只打回环地址）。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest

from app.adapters import qoder
from app.adapters.base import ProviderError
from app.adapters.client import UpstreamReply
from app.config import Settings


def _settings(tmp_path: Path, allow_private: bool = True) -> Settings:
    return Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "unified.db",
        web_dir=tmp_path,
        admin_username="admin",
        admin_password="test",
        session_ttl_sec=3600,
        cookie_secure=False,
        codebuddy_base="http://127.0.0.1:7864",
        codebuddy_api_key="",
        opencode_upstream_base="http://127.0.0.1:1",
        grok_register_executor_url="",
        grok_register_max_concurrency=1,
        grok_register_max_workers=1,
        allow_anonymous_gateway=False,
        cors_origins=(),
        allow_private_upstreams=allow_private,
    )


def _adapter(tmp_path: Path, allow_private: bool = True, **options) -> qoder.QoderAdapter:
    adapter = qoder.QoderAdapter(_settings(tmp_path, allow_private))
    adapter.configure(options=options)
    return adapter


class _FakeUpstream:
    """按路径分派的假上游；记录请求路径，用来校验 PKCE 参数与刷新路由。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.poll_replies: list[UpstreamReply] = []
        self.post_replies: list[object] = []
        self.userinfo = UpstreamReply(
            200, {}, body=json.dumps({"uid": "u1", "nickname": "小明"}).encode()
        )

    async def forward(self, path, payload=None, *, stream=False, method="POST", raw=None, accept=None):
        self.calls.append((method, path))
        if path.startswith(qoder.EP_DEVICE_TOKEN_POLL):
            reply = self.poll_replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        if path == qoder.EP_USERINFO:
            return self.userinfo
        raise AssertionError(f"未预期的请求 {method} {path}")

    async def post_json(self, path, payload=None):
        self.calls.append(("POST", path, payload))
        reply = self.post_replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _wire(monkeypatch, adapter: qoder.QoderAdapter, fake: _FakeUpstream) -> _FakeUpstream:
    monkeypatch.setattr(adapter, "_oauth_client", lambda region, token="": fake)
    return fake


def _poll_url(fake: _FakeUpstream) -> str:
    for method, path in fake.calls:
        if method == "GET" and path.startswith(qoder.EP_DEVICE_TOKEN_POLL):
            return path
    raise AssertionError("没有发出轮询请求")


def _machine_id(url: str) -> str:
    return dict(parse_qsl(urlparse(url).query))["machine_id"]


# ---------- 纯协议：PKCE / nonce / 授权 URL ----------


def test_pkce_pair_shape_and_challenge_is_s256_of_verifier() -> None:
    verifier, challenge = qoder.pkce_pair()
    assert len(verifier) == 64
    assert set(verifier) <= set(qoder.VERIFIER_ALPHABET)
    assert "=" not in challenge
    expected = qoder.base64.urlsafe_b64encode(
        qoder.hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    assert challenge == expected
    assert qoder.pkce_pair()[0] != verifier


def test_nonce_shape_differs_by_region() -> None:
    cn_nonce = qoder.nonce_for("cn")
    global_nonce = qoder.nonce_for("global")
    assert len(cn_nonce) == 36 and "-" in cn_nonce
    assert len(global_nonce) == 32 and "-" not in global_nonce


def test_authorize_url_differs_by_region_only_by_redirect_uri() -> None:
    cn = qoder.build_authorize_url("cn", "machine-1", "nonce-1", "challenge-1")
    world = qoder.build_authorize_url("global", "machine-2", "nonce-2", "challenge-2")
    assert cn.startswith("https://qoder.com.cn/device/selectAccounts?")
    assert "challenge_method=S256" in cn
    assert "redirect_uri=qoder-work-cn://" in cn
    assert qoder.REGIONS["cn"]["client_id"] in cn
    assert world.startswith("https://qoder.com/device/selectAccounts?")
    assert "redirect_uri" not in world
    assert qoder.REGIONS["global"]["client_id"] in world


def test_token_kind_and_refresh_plan() -> None:
    assert qoder.token_kind("drt-abc") == "device_refresh"
    assert qoder.token_kind("jrt-abc") == "job_refresh"
    assert qoder.token_kind("dt-abc") == "device_access"
    assert qoder.token_kind("pt-abc") == "personal"
    assert qoder.token_kind("") == ""
    assert qoder.token_kind("drt- abc") == ""
    assert qoder.token_kind("Bearer dt-abc") == ""
    assert qoder.refresh_plan({"refresh_token": "drt-1"}) == "device"
    assert qoder.refresh_plan({"refresh_token": "jrt-1"}) == "job"
    assert qoder.refresh_plan({"personal_token": "pt-1"}) == "pat-exchange"
    assert qoder.refresh_plan({}) == ""


def test_token_expiry_reads_expires_in_as_milliseconds_and_falls_back() -> None:
    now = 1_700_000_000.0
    assert qoder.token_expiry({"expires_in": 3_600_000}, 0, now=now) == int(now) + 3600
    assert qoder.token_expiry({"expires_at": "2030-01-01T00:00:00Z"}, 0, now=now) == int(
        datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()
    )
    fallback = int(now) + qoder.FALLBACK_EXPIRY_DEVICE_SECS
    assert qoder.token_expiry({}, qoder.FALLBACK_EXPIRY_DEVICE_SECS, now=now) == fallback


def test_credential_state_windows() -> None:
    now = time.time()
    assert qoder.credential_state({}) == "unknown"
    assert qoder.credential_state({"expires_at": int((now - 1) * 1000)}, now=now) == "expired"
    assert qoder.credential_state({"expires_at": int((now + 3600) * 1000)}, now=now) == "expiring"
    assert qoder.credential_state({"expires_at": int((now + 30 * 86400) * 1000)}, now=now) == "fresh"


# ---------- 出站守卫与设备身份 ----------


def test_oauth_base_is_guarded(tmp_path: Path) -> None:
    guarded = _adapter(tmp_path, allow_private=False, oauth_openapi_base="http://127.0.0.1:9")
    with pytest.raises(ProviderError) as caught:
        guarded._oauth_base("cn")
    assert caught.value.status_code == 400
    assert "守卫" in caught.value.message

    opened = _adapter(tmp_path, allow_private=True, oauth_openapi_base="http://127.0.0.1:9/")
    assert opened._oauth_base("cn") == "http://127.0.0.1:9"

    with pytest.raises(ProviderError) as unknown:
        _adapter(tmp_path)._oauth_base("mars")
    assert unknown.value.status_code == 400


def test_machine_id_is_stable_and_persisted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(qoder.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(qoder, "_appdata_dir", lambda: tmp_path / "appdata")
    adapter = _adapter(tmp_path, oauth_openapi_base="http://127.0.0.1:9")
    first = adapter.login_start("cn")
    second = adapter.login_start("cn")
    machine = _machine_id(first["verificationUri"])
    assert machine == _machine_id(second["verificationUri"])
    assert (tmp_path / "qoder" / "oauth_machine_id.cn.txt").read_text(encoding="utf-8").strip() == machine
    assert first["expiresIn"] == qoder.OAUTH_TIMEOUT_SECONDS
    assert first["loginId"] != second["loginId"]


def test_login_cancel_removes_session(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    login = adapter.login_start("cn")
    assert adapter.login_cancel(login["loginId"]) is True
    assert adapter.login_cancel(login["loginId"]) is False


# ---------- 轮询状态机 ----------


async def test_poll_success_produces_credential_compatible_with_pasted_token(
    tmp_path: Path, monkeypatch
) -> None:
    adapter = _adapter(tmp_path)
    fake = _wire(monkeypatch, adapter, _FakeUpstream())
    login = adapter.login_start("cn")
    session = adapter._oauth_logins[login["loginId"]]
    fake.poll_replies.append(
        UpstreamReply(
            200,
            {},
            body=json.dumps(
                {"token": "dt-live", "refresh_token": "drt-live", "expires_in": 2_592_000_000}
            ).encode(),
        )
    )

    state = await adapter.login_poll("cn", login["loginId"])
    assert state["done"] is True and state["error"] == ""
    result = state["result"]
    assert result["access_token"] == "dt-live"
    assert result["refresh_token"] == "drt-live"
    assert result["uid"] == "u1"
    assert result["nickname"] == "小明"
    assert result["domain"] == "qoder.com.cn"
    assert result["expires_at"] > int(time.time() * 1000)
    assert qoder.credential_state(result) == "fresh"

    # PKCE 参数必须来自会话本身，且不把 verifier 漏进结果
    url = _poll_url(fake)
    assert f"verifier={session.verifier}" in url
    assert "challenge_method=S256" in url
    assert session.verifier not in json.dumps(result)

    # 与「粘贴 token」同一形状：既有 parse_account 直接认
    raw = json.dumps(result)
    account = qoder.parse_account(raw)
    assert account is not None
    assert account.key == raw
    assert account.token == "dt-live"
    assert account.uid == "u1"

    # 幂等：已完成的会话重复轮询不再打上游
    calls = len(fake.calls)
    again = await adapter.login_poll("cn", login["loginId"])
    assert again["result"] == result
    assert len(fake.calls) == calls


async def test_poll_pending_while_user_has_not_finished(tmp_path: Path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    fake = _wire(monkeypatch, adapter, _FakeUpstream())
    login = adapter.login_start("cn")

    fake.poll_replies.append(UpstreamReply(202, {}, body=b""))
    assert await adapter.login_poll("cn", login["loginId"]) == {"done": False}

    fake.poll_replies.append(UpstreamReply(200, {}, body=b"{}"))
    assert await adapter.login_poll("cn", login["loginId"]) == {"done": False}

    fake.poll_replies.append(UpstreamReply(404, {}, body=b"not ready"))
    assert await adapter.login_poll("cn", login["loginId"]) == {"done": False}

    # 网络抖动同样不算结束
    fake.poll_replies.append(ProviderError(502, "上游不可达"))
    assert await adapter.login_poll("cn", login["loginId"]) == {"done": False}


async def test_poll_timeout(tmp_path: Path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    fake = _wire(monkeypatch, adapter, _FakeUpstream())
    login = adapter.login_start("cn")
    adapter._oauth_logins[login["loginId"]].expires_at = time.time() - 1

    state = await adapter.login_poll("cn", login["loginId"])
    assert state == {"done": True, "result": None, "error": "登录超时"}
    assert fake.calls == []


async def test_poll_token_endpoint_error_stops_session(tmp_path: Path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    fake = _wire(monkeypatch, adapter, _FakeUpstream())
    login = adapter.login_start("cn")
    fake.poll_replies.append(UpstreamReply(500, {}, body=b'{"message":"boom"}'))

    state = await adapter.login_poll("cn", login["loginId"])
    assert state["done"] is True
    assert state["result"] is None
    assert "轮询失败: HTTP 500" in state["error"]
    assert "boom" in state["error"]

    # 已结束的会话不再重试
    again = await adapter.login_poll("cn", login["loginId"])
    assert again["error"] == state["error"]


async def test_poll_pkce_verifier_rejected(tmp_path: Path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    fake = _wire(monkeypatch, adapter, _FakeUpstream())
    login = adapter.login_start("cn")
    fake.poll_replies.append(
        UpstreamReply(400, {}, body=b'{"code":"invalid_grant","message":"code_verifier mismatch"}')
    )

    state = await adapter.login_poll("cn", login["loginId"])
    assert state["done"] is True
    assert "轮询失败: HTTP 400" in state["error"]
    assert "code_verifier mismatch" in state["error"]


async def test_poll_rejects_unknown_or_cross_region_session(tmp_path: Path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    _wire(monkeypatch, adapter, _FakeUpstream())
    login = adapter.login_start("cn")

    assert (await adapter.login_poll("cn", "qoder_missing"))["error"] == "登录请求不存在或已取消"
    assert (await adapter.login_poll("global", login["loginId"]))["error"] == "登录请求与目标版本不匹配"


async def test_poll_userinfo_failure_stops_session(tmp_path: Path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    fake = _wire(monkeypatch, adapter, _FakeUpstream())
    login = adapter.login_start("cn")
    fake.poll_replies.append(UpstreamReply(200, {}, body=b'{"token":"dt-live"}'))
    fake.userinfo = UpstreamReply(403, {}, body=b"no profile")

    state = await adapter.login_poll("cn", login["loginId"])
    assert state["done"] is True
    assert "拉取账号身份失败: HTTP 403" in state["error"]


# ---------- 刷新 ----------


async def test_refresh_device_credential(tmp_path: Path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    fake = _wire(monkeypatch, adapter, _FakeUpstream())
    fake.post_replies.append(
        {"token": "dt-new", "refresh_token": "drt-new", "expires_in": 3_600_000}
    )

    result = await adapter.refresh_credential(
        {"access_token": "dt-old", "refresh_token": "drt-old", "expires_at": 1}
    )
    method, path, payload = fake.calls[-1]
    assert path == qoder.EP_DEVICE_TOKEN_REFRESH
    assert payload == {"refresh_token": "drt-old"}
    assert result["access_token"] == "dt-new"
    assert result["refresh_token"] == "drt-new"
    assert result["expires_at"] > int(time.time() * 1000)
    assert qoder.credential_state(result) == "expiring"


async def test_refresh_job_credential_keeps_old_refresh_token_when_absent(
    tmp_path: Path, monkeypatch
) -> None:
    adapter = _adapter(tmp_path)
    fake = _wire(monkeypatch, adapter, _FakeUpstream())
    fake.post_replies.append({"token": "jt-new"})

    result = await adapter.refresh_credential({"refresh_token": "jrt-old"})
    assert fake.calls[-1][1] == qoder.EP_JOB_TOKEN_REFRESH
    assert result["access_token"] == "jt-new"
    assert result["refresh_token"] == "jrt-old"
    assert result["expires_at"] > int(time.time() * 1000)


async def test_refresh_pat_exchanges_personal_token(tmp_path: Path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    fake = _wire(monkeypatch, adapter, _FakeUpstream())
    fake.post_replies.append({"token": "jt-from-pat"})

    result = await adapter.refresh_credential({"personal_token": "pt-abc"})
    method, path, payload = fake.calls[-1]
    assert path == qoder.EP_JOB_TOKEN_EXCHANGE
    assert payload == {"personal_token": "pt-abc"}
    assert result["access_token"] == "jt-from-pat"


async def test_refresh_requires_plan(tmp_path: Path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    fake = _wire(monkeypatch, adapter, _FakeUpstream())
    with pytest.raises(ProviderError) as caught:
        await adapter.refresh_credential({"access_token": "dt-old"})
    assert caught.value.status_code == 400
    assert fake.calls == []


async def test_refresh_rejects_empty_token(tmp_path: Path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    fake = _wire(monkeypatch, adapter, _FakeUpstream())
    fake.post_replies.append({"refresh_token": "drt-new"})
    with pytest.raises(ProviderError) as caught:
        await adapter.refresh_credential({"refresh_token": "drt-old"})
    assert caught.value.status_code == 502
    assert "拒绝回写" in caught.value.message


async def test_refresh_upstream_error_is_fail_loud(tmp_path: Path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    fake = _wire(monkeypatch, adapter, _FakeUpstream())
    fake.post_replies.append(ProviderError(401, "上游 /api/v1/deviceToken/refresh 返回 401"))
    with pytest.raises(ProviderError) as caught:
        await adapter.refresh_credential({"refresh_token": "drt-old"})
    assert caught.value.status_code == 401
    assert "刷新令牌失败" in caught.value.message
