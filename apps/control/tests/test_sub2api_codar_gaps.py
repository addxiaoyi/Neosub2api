"""Sub2API 建号链路 + Codar JWT 三态补齐（2026-11-06）。

Sub2API 只验证「缺配置时是否给出界面可执行的下一步」与兼容面错误映射；
Codar 只验证本地 JWT 判定、缺 token 话术、直连流式与校验接口权限。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.adapters import codar, sub2api
from app.adapters.base import ProviderError
from app.config import Settings

pytestmark = pytest.mark.providers

MESSAGES = [{"role": "user", "content": "hi"}]


def _settings(tmp: Path, *, allow_private: bool = False) -> Settings:
    return Settings(
        data_dir=tmp,
        db_path=tmp / "unified.db",
        web_dir=tmp / "web",
        admin_username="admin",
        admin_password="test",
        session_ttl_sec=3600,
        cookie_secure=False,
        codebuddy_base="http://127.0.0.1:7899",
        codebuddy_api_key="",
        opencode_upstream_base="http://127.0.0.1:7899",
        grok_register_executor_url="",
        grok_register_max_concurrency=1,
        grok_register_max_workers=2,
        allow_anonymous_gateway=False,
        cors_origins=(),
        allow_private_upstreams=allow_private,
    )


def _chat(model: str, stream: bool = False) -> dict[str, Any]:
    return {"model": model, "messages": MESSAGES, "stream": stream}


def _sub2api(fake_base: str, tmp: Path, *, group: str = "g1", seed: bool = True) -> sub2api.Sub2ApiAdapter:
    adapter = sub2api.build(_settings(tmp, allow_private=True), fake_base)
    adapter.configure(options={"group": group, "timeout": 30.0})
    if seed:
        adapter.store.upsert_group(group, name=group)
        adapter.store.add_account(group, name="a1", upstream_base=fake_base, api_key="sub-key")
    return adapter


def _codar(base: str, tmp: Path, token: str = "") -> codar.CodarAdapter:
    adapter = codar.build(_settings(tmp), base_url=base)
    adapter.configure(api_key=token, options={})
    return adapter


async def _collect(reply) -> bytes:
    chunks = []
    async for chunk in reply.iter_bytes():
        chunks.append(chunk)
    return b"".join(chunks)


def _jwt(payload: dict[str, Any]) -> str:
    def seg(data: dict[str, Any]) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(payload)}.sig"


# ---------------------------------------------------------------- Sub2API


async def test_sub2api_missing_group_points_to_quota_ui(fake_base: str, tmp_path: Path) -> None:
    adapter = _sub2api(fake_base, tmp_path, seed=False)
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion(_chat("fake-chat-1"), stream=False)
    assert caught.value.status_code == 503
    main, _, tech = str(caught.value).partition("技术细节")
    assert "配额" in main and "添加账号" in main
    assert "/api/sub2api/groups" not in main
    assert "/api/sub2api/groups" in tech


async def test_sub2api_empty_pool_points_to_accounts_tab(fake_base: str, tmp_path: Path) -> None:
    adapter = _sub2api(fake_base, tmp_path, seed=False)
    adapter.store.upsert_group("g1", name="g1")
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion(_chat("fake-chat-1"), stream=False)
    assert caught.value.status_code == 429
    main, _, tech = str(caught.value).partition("技术细节")
    assert "配额" in main and "账号 tab" in main
    assert "/api/sub2api/accounts" not in main
    assert "/api/sub2api/accounts" in tech


async def test_sub2api_models_without_group_503(fake_base: str, tmp_path: Path) -> None:
    adapter = _sub2api(fake_base, tmp_path, seed=False)
    with pytest.raises(ProviderError) as caught:
        await adapter.fetch_models()
    assert caught.value.status_code == 503
    assert "配额" in str(caught.value)


async def test_sub2api_models_through_account(fake_base: str, tmp_path: Path) -> None:
    ids = {item["id"] for item in await _sub2api(fake_base, tmp_path).fetch_models()}
    assert ids == {"fake-chat-1", "fake-chat-2"}


async def test_sub2api_upstream_429_cools_then_pool_empty(fake_base: str, tmp_path: Path) -> None:
    adapter = _sub2api(fake_base, tmp_path)
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion(_chat("fake-chat-1-err-429"), stream=False)
    assert caught.value.status_code == 429
    assert "账号 tab" in str(caught.value)
    assert adapter.store.list_groups()[0]["active_accounts"] == 0


async def test_sub2api_upstream_5xx_maps_to_empty_pool(fake_base: str, tmp_path: Path) -> None:
    adapter = _sub2api(fake_base, tmp_path)
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion(_chat("fake-chat-1-err-503"), stream=False)
    assert caught.value.status_code == 429
    assert adapter.store.list_groups()[0]["active_accounts"] == 0


async def test_sub2api_stream_break_does_not_hang(fake_base: str, tmp_path: Path) -> None:
    adapter = _sub2api(fake_base, tmp_path)
    reply = await adapter.chat_completion(_chat("fake-chat-1-stream-break", True), stream=True)
    with pytest.raises(Exception) as caught:
        await asyncio.wait_for(_collect(reply), timeout=10)
    assert not isinstance(caught.value, asyncio.TimeoutError)


async def test_sub2api_messages_sends_anthropic_headers(fake_base: str, tmp_path: Path) -> None:
    adapter = _sub2api(fake_base, tmp_path)
    payload = {"model": "fake-chat-1", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
    reply = await adapter.messages(payload, stream=False)
    assert reply.status_code == 200
    text = (reply.body or b"").decode()
    assert "anthropic-version=2023-06-01" in text
    assert "x-api-key=sub-key" in text
    assert "user-agent=sub2api/1.0" in text


# ---------------------------------------------------------------- Codar JWT


def test_inspect_token_states() -> None:
    now = int(time.time())
    assert codar.inspect_token("")["state"] == "missing"
    assert codar.inspect_token("not-a-jwt")["state"] == "invalid"
    assert codar.inspect_token("a.b")["state"] == "invalid"
    assert codar.inspect_token(_jwt({"exp": now - 1}))["state"] == "expired"
    valid = codar.inspect_token(_jwt({"exp": now + 600}))
    assert valid["state"] == "valid" and valid["expires_in"] > 0
    no_exp = codar.inspect_token(_jwt({"sub": "codar"}))
    assert no_exp["state"] == "valid" and no_exp["expires_at"] is None


async def test_codar_missing_token_says_sidecar(fake_base: str, tmp_path: Path) -> None:
    with pytest.raises(ProviderError) as caught:
        await _codar(fake_base, tmp_path).chat_completion(_chat("fake-chat-1"), stream=False)
    assert caught.value.status_code == 401
    assert "JWT" in str(caught.value) and "sidecar" in str(caught.value)


async def test_codar_models_chat_stream(fake_base: str, tmp_path: Path) -> None:
    adapter = _codar(fake_base, tmp_path, token="codar-token-1")
    assert {item["id"] for item in await adapter.list_models()} == {"fake-chat-1", "fake-chat-2"}
    plain = await adapter.chat_completion(_chat("fake-chat-1"), stream=False)
    assert plain.status_code == 200 and b"model=fake-chat-1" in (plain.body or b"")
    streamed = await adapter.chat_completion(_chat("fake-chat-2", True), stream=True)
    assert b"data: [DONE]" in await asyncio.wait_for(_collect(streamed), timeout=10)


async def test_codar_stream_break_does_not_hang(fake_base: str, tmp_path: Path) -> None:
    adapter = _codar(fake_base, tmp_path, token="codar-token-1")
    reply = await adapter.chat_completion(_chat("fake-chat-1-stream-break", True), stream=True)
    with pytest.raises(Exception) as caught:
        await asyncio.wait_for(_collect(reply), timeout=10)
    assert not isinstance(caught.value, asyncio.TimeoutError)


async def test_codar_messages_sends_anthropic_headers(fake_base: str, tmp_path: Path) -> None:
    adapter = _codar(fake_base, tmp_path, token="codar-token-1")
    payload = {"model": "fake-chat-1", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
    reply = await adapter.messages(payload, stream=False)
    assert reply.status_code == 200, reply.body
    text = (reply.body or b"").decode()
    assert "anthropic-version=2023-06-01" in text
    assert "x-api-key=codar-token-1" in text
    assert "user-agent=codar-desktop/0.3.3" in text


async def test_codar_external_refresh_is_opt_in(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """401 续期是设计行为，但默认不得静默外呼 api.kuaipaper.com。"""
    adapter = _codar("http://127.0.0.1:9", tmp_path, token="tok-1")
    await adapter._rotation()  # 同步 token 列表，_refresh 才有可续期的索引
    with caplog.at_level(logging.WARNING, logger="unified.codar"):
        fresh = await adapter._refresh(0, "tok-1")
    assert fresh == ""
    assert "已跳过" in caplog.text
    assert "api.kuaipaper.com" in caplog.text, "服务端日志要能查到被拦下的外呼目标"


# ---------------------------------------------------------------- 校验接口


@pytest.fixture(scope="module")
def viewer(base_url: str, admin: httpx.Client):
    name = f"jwt-view-{uuid.uuid4().hex[:6]}"
    created = admin.post("/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"})
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=base_url, trust_env=False, timeout=20.0)
    assert client.post("/api/login", json={"username": name, "password": "viewer-pass"}).status_code == 200
    yield client
    client.close()


def test_validate_endpoint_states(admin: httpx.Client) -> None:
    now = int(time.time())
    cases = [
        (_jwt({"exp": now + 600}), "valid"),
        (_jwt({"exp": now - 10}), "expired"),
        ("not-a-jwt", "invalid"),
        ("", "missing"),
    ]
    for token, state in cases:
        r = admin.post("/api/providers/codar/token/validate", json={"token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == state
        if token:
            assert token not in r.text
        if state != "valid":
            assert body["next_step"]


def test_validate_endpoint_reads_credential(admin: httpx.Client, db_path: str) -> None:
    token = _jwt({"exp": int(time.time()) + 900})
    created = admin.post(
        "/api/provider-credentials",
        json={"provider": "codar", "label": "jwt-e2e", "secret": token, "enabled": True},
    )
    assert created.status_code == 200, created.text
    cid = created.json()["id"]
    r = admin.post("/api/providers/codar/token/validate", json={"credential_id": cid})
    assert r.status_code == 200 and r.json()["state"] == "valid"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT action, detail FROM audit_logs WHERE action='providers.token.validate' ORDER BY id DESC"
        ).fetchall()
    assert rows and rows[0][0] == "providers.token.validate"
    assert "state=valid" in rows[0][1]
    assert token not in rows[0][1]


def test_validate_endpoint_unknown_provider(admin: httpx.Client) -> None:
    r = admin.post("/api/providers/nope/token/validate", json={"token": "x"})
    assert r.status_code == 404


def test_validate_endpoint_viewer_forbidden(viewer: httpx.Client) -> None:
    r = viewer.post("/api/providers/codar/token/validate", json={"token": "x"})
    assert r.status_code == 403
