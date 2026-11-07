"""Qoder 多账号与多身份：选号、身份切换、坏号分档冷却与恢复。"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app.adapters import qoder
from app.adapters.base import ProviderError
from app.adapters.client import UnifiedApiClient
from app.config import load_settings
from app.services.rotation import RotationStore, fingerprint

PAYLOAD = {"model": "cn:qwen3", "messages": [{"role": "user", "content": "hi"}]}


def _uid(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _envelope(inner: dict) -> bytes:
    body = json.dumps({"body": json.dumps(inner, ensure_ascii=False), "statusCodeValue": 200}, ensure_ascii=False)
    return f"data: {body}\n\n".encode()


def _text_sse() -> bytes:
    return (
        _envelope({"id": "c1", "choices": [{"index": 0, "delta": {"content": "ok"}}]})
        + _envelope({
            "id": "c1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        })
        + b'data: {"body":"[DONE]"}\n\n'
    )


def _adapter(credentials: list[str]) -> qoder.QoderAdapter:
    adapter = qoder.build(load_settings())
    adapter.configure(api_key=credentials[0], api_keys=credentials)
    # 单测不落库：轮转组退化为进程内选号，避免动到真实 db
    adapter._store = RotationStore(None)
    return adapter


def _record(monkeypatch, handler) -> list[httpx.Request]:
    recorded: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return handler(request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(wrapped), base_url="http://mock.local", trust_env=False
    )
    monkeypatch.setattr(UnifiedApiClient, "_client", lambda self, *args, **kwargs: client)
    return recorded


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=_text_sse(), headers={"content-type": "text/event-stream"})


def test_parse_account_accepts_token_and_identity_json():
    assert qoder.parse_account("  token-a ").token == "token-a"
    assert qoder.parse_account("Bearer token-b").token == "token-b"

    cred = json.dumps({
        "access_token": "token-c",
        "uid": "user-c",
        "user_type": "enterprise",
        "organization_name": "ACME",
    })
    account = qoder.parse_account(cred)
    assert account is not None
    assert account.token == "token-c"
    assert account.uid == "user-c"
    assert account.identity["user_type"] == "enterprise"

    assert qoder.parse_account("") is None
    assert qoder.parse_account('{"uid":"no-token"}') is None


async def test_multi_account_round_robin_switches_identity(monkeypatch):
    adapter = _adapter(["token-a", "token-b"])
    recorded = _record(monkeypatch, _ok)

    await adapter.chat_completion(dict(PAYLOAD), stream=False)
    await adapter.chat_completion(dict(PAYLOAD), stream=False)

    assert [req.headers["cosy-user"] for req in recorded] == [_uid("token-a"), _uid("token-b")]


async def test_json_identity_overrides_provider_defaults(monkeypatch):
    cred_a = json.dumps({"access_token": "token-a", "uid": "uid-a", "user_type": "personal"})
    cred_b = json.dumps({
        "access_token": "token-b",
        "uid": "uid-b",
        "user_type": "enterprise",
        "organization_name": "ACME",
    })
    adapter = _adapter([cred_a, cred_b])
    adapter.configure(options={"organization_name": "默认组织", "nickname": "默认昵称"})

    accounts = adapter._accounts()
    assert adapter._identity(accounts[0]) == {
        "uid": "uid-a",
        "nickname": "默认昵称",
        "user_type": "personal",
        "organization_id": "",
        "organization_name": "默认组织",
        "refresh_token": "",
    }
    assert adapter._identity(accounts[1])["organization_name"] == "ACME"
    assert adapter._session(accounts[1])["uid"] == "uid-b"

    recorded = _record(monkeypatch, _ok)
    await adapter.chat_completion(dict(PAYLOAD), stream=False)
    assert recorded[0].headers["cosy-user"] == "uid-a"


async def test_bad_account_cools_then_recovers(monkeypatch):
    adapter = _adapter(["token-a", "token-b"])
    uid_a, uid_b = _uid("token-a"), _uid("token-b")
    recorded = _record(
        monkeypatch,
        lambda req: (
            httpx.Response(401, content=b'{"code":12153}')
            if req.headers.get("cosy-user") == uid_a
            else _ok(req)
        ),
    )

    reply = await adapter.chat_completion(dict(PAYLOAD), stream=False)
    assert reply.status_code == 200
    assert recorded[0].headers["cosy-user"] == uid_a
    assert recorded[1].headers["cosy-user"] == uid_b

    snap = await adapter._pool.snapshot(adapter._keys())
    bad = next(item for item in snap["credentials"] if item["id"] == fingerprint("token-a"))
    assert bad["state"] == "cooling"
    assert 590 <= bad["cooldown_remaining"] <= 600
    assert adapter._session_key(adapter._accounts()[0]) not in adapter._sessions

    recorded.clear()
    await adapter.chat_completion(dict(PAYLOAD), stream=False)
    assert [req.headers["cosy-user"] for req in recorded] == [uid_b]

    await adapter._pool.report_success(adapter._keys()[0])
    snap = await adapter._pool.snapshot(adapter._keys())
    assert {item["state"] for item in snap["credentials"]} == {"ready"}


async def test_rate_limit_cools_only_the_model_not_the_account(monkeypatch):
    adapter = _adapter(["token-a", "token-b"])
    uid_a = _uid("token-a")
    _record(
        monkeypatch,
        lambda req: (
            httpx.Response(429, content=b'{"retry_after":12}')
            if req.headers.get("cosy-user") == uid_a
            else _ok(req)
        ),
    )

    await adapter.chat_completion(dict(PAYLOAD), stream=False)
    snap = await adapter._pool.snapshot(adapter._keys())
    bad = next(item for item in snap["credentials"] if item["id"] == fingerprint("token-a"))
    # 429 只冷 (账号, 模型) 60s，账号本身仍 ready；Retry-After 不能把 60s 缩短
    assert bad["cooldown_remaining"] == 0
    assert bad["state"] == "ready"
    assert snap["cooling"] == 0
    assert snap["model_cooling"] == 1
    assert 55 <= snap["model_cooldowns"][fingerprint("token-a")]["cn:qwen3"] <= 60


async def test_single_account_auth_failure_not_masked(monkeypatch):
    adapter = _adapter(["token-a"])
    _record(monkeypatch, lambda req: httpx.Response(401, content=b'{"code":12153}'))

    with pytest.raises(ProviderError) as excinfo:
        await adapter.chat_completion(dict(PAYLOAD), stream=False)
    assert excinfo.value.status_code == 401

    snap = await adapter._pool.snapshot(adapter._keys())
    assert snap["cooling"] == 0
    assert snap["credentials"][0]["state"] == "ready"


async def test_sticky_session_keeps_same_account(monkeypatch):
    adapter = _adapter(["token-a", "token-b"])
    recorded = _record(monkeypatch, _ok)
    payload = {**PAYLOAD, "session_id": "sess-1"}

    await adapter.chat_completion(dict(payload), stream=False)
    await adapter.chat_completion(dict(payload), stream=False)

    assert [req.headers["cosy-user"] for req in recorded] == [_uid("token-a"), _uid("token-a")]
