"""codar 仓内实现：假上游覆盖流式/非流式、错误码、模型列表、多 token 与 401 续期。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.adapters import codar
from app.adapters.base import ProviderError
from app.config import Settings

pytestmark = pytest.mark.providers


def _settings(tmp: Path) -> Settings:
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
    )


def _adapter(
    base: str,
    tmp: Path,
    *,
    api_key: str = "",
    options: dict[str, Any] | None = None,
) -> codar.CodarAdapter:
    adapter = codar.build(_settings(tmp), base_url=base)
    adapter.configure(api_key=api_key, options=options or {})
    return adapter


async def _collect(reply) -> str:
    chunks = []
    async for chunk in reply.iter_bytes():
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


async def test_models_reads_private_catalog(fake_base: str, tmp_path: Path) -> None:
    models = await _adapter(fake_base, tmp_path, api_key="codar-token-1").list_models()
    assert {item["id"] for item in models} == {"fake-chat-1", "fake-chat-2"}
    assert all(item["object"] == "model" for item in models)


async def test_models_without_token_returns_empty(fake_base: str, tmp_path: Path) -> None:
    assert await _adapter(fake_base, tmp_path).list_models() == []


async def test_chat_non_stream(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path, api_key="codar-token-1")
    reply = await adapter.chat_completion(
        {"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )
    assert reply.status_code == 200
    assert b"model=fake-chat-1" in (reply.body or b"")


async def test_chat_stream(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path, api_key="codar-token-1")
    reply = await adapter.chat_completion(
        {"model": "fake-chat-2", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        stream=True,
    )
    assert reply.status_code == 200
    text = await _collect(reply)
    assert "data: [DONE]" in text
    assert "model=fake-chat-2" in text


async def test_messages_non_stream_and_stream(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path, api_key="codar-token-1")
    plain = await adapter.messages(
        {"model": "fake-chat-1", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )
    assert plain.status_code == 200
    assert b'"type":"message"' in (plain.body or b"")

    streamed = await adapter.messages(
        {"model": "fake-chat-1", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        stream=True,
    )
    text = await _collect(streamed)
    assert "message_stop" in text


async def test_responses_non_stream_and_stream(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path, api_key="codar-token-1")
    plain = await adapter.responses({"model": "fake-chat-1", "input": "hi"}, stream=False)
    assert plain.status_code == 200
    assert b"responses model=fake-chat-1" in (plain.body or b"")

    streamed = await adapter.responses(
        {"model": "fake-chat-1", "input": "hi", "stream": True}, stream=True
    )
    text = await _collect(streamed)
    assert "response.completed" in text


async def test_upstream_error_passthrough(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path, api_key="codar-forbidden")
    reply = await adapter.chat_completion(
        {"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )
    assert reply.status_code == 403
    assert b"account_restricted" in (reply.body or b"")


async def test_no_token_raises_401(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path)
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "fake-chat-1"}, stream=False)
    assert caught.value.status_code == 401
    assert "JWT" in caught.value.message and "sidecar" in caught.value.message


async def test_multi_token_rotation(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(
        fake_base, tmp_path, options={"tokens": ["codar-expired", "codar-token-1"]}
    )
    reply = await adapter.chat_completion(
        {"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )
    assert reply.status_code == 200


async def test_401_refresh(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(
        fake_base,
        tmp_path,
        options={"tokens": ["codar-expired"], "pay_base": fake_base},
    )
    reply = await adapter.chat_completion(
        {"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )
    assert reply.status_code == 200
    # 续期后的新 token 固定为 codar-fresh-1，旧 token 已被替换
    assert await adapter._current_token(0) == "codar-fresh-1"


async def test_401_without_refresh_fails(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(
        fake_base,
        tmp_path,
        options={"tokens": ["codar-expired"], "refresh": False},
    )
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "fake-chat-1"}, stream=False)
    assert caught.value.status_code == 401


async def test_upstream_402_408_5xx_register_pool_failure(fake_base: str, tmp_path: Path) -> None:
    for code in (402, 408, 500):
        adapter = _adapter(fake_base, tmp_path, options={"tokens": ["codar-token-1", "codar-token-2"]})
        with patch.object(adapter._scheduler, "note_failure", AsyncMock()) as note:
            reply = await adapter.chat_completion(
                {"model": f"fake-chat-1-err-{code}", "messages": [{"role": "user", "content": "hi"}]},
                stream=False,
            )
        assert reply.status_code == code
        assert note.await_count >= 1
        assert note.await_args_list[0].args == ("codar-token-1", code)


async def test_health(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path, api_key="codar-token-1")
    health = await adapter.health()
    assert health["connected"] is True
    assert health["models"] == 2

    missing = await _adapter(fake_base, tmp_path).health()
    assert missing["connected"] is False
    assert "未配置" in missing["error"]


# ---------- 403 账号受限：请求黑名单 ----------


def test_request_fingerprint_ignores_key_order_but_not_path() -> None:
    """指纹只认「路径 + 键有序的 JSON」：换顺序同一条，换路径就是另一条。"""
    path = "/v1/chat/completions"
    assert codar.request_fingerprint(path, {"model": "m", "messages": []}) == codar.request_fingerprint(
        path, {"messages": [], "model": "m"}
    )
    assert codar.request_fingerprint(path, {"model": "m"}) != codar.request_fingerprint(
        "/v1/messages", {"model": "m"}
    )
    assert codar.request_fingerprint(path, {}) == codar.request_fingerprint(path, None)


def test_restricted_signal_reads_same_field_paths_as_upstream() -> None:
    """上游 restriction() 看顶层 / detail / error / response.error 的 code 与 type。"""
    assert codar._restricted_signal(403, b'{"detail":{"type":"account_restricted"}}', {}, None) == "account_restricted"
    assert codar._restricted_signal(
        403, b'{"response":{"error":{"code":"account_restricted"}}}', {}, None
    ) == "account_restricted"
    assert codar._restricted_signal(403, b'{"code":"account_restricted"}', {}, None) == "account_restricted"
    # 成功响应里出现同名字段不该短路
    assert codar._restricted_signal(200, b'{"code":"account_restricted"}', {}, None) == ""
    assert codar._restricted_signal(403, b'{"code":403}', {}, None) == ""
    assert codar._restricted_signal(403, None, {}, None) == ""


async def test_restricted_403_keeps_passthrough_when_switch_off(fake_base: str, tmp_path: Path) -> None:
    """默认开关关闭：原样透传、不登记黑名单（空表即 no-op）。"""
    adapter = _adapter(fake_base, tmp_path, api_key="codar-forbidden")
    reply = await adapter.chat_completion(
        {"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]}, stream=False
    )
    assert reply.status_code == 403
    assert adapter.request_blocks()["count"] == 0


async def test_restricted_403_blocks_same_request_when_enabled(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(
        fake_base, tmp_path, api_key="codar-forbidden", options={"restricted_request_block": True}
    )
    body = {"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]}
    reply = await adapter.chat_completion(body, stream=False)
    assert reply.status_code == 403

    snapshot = adapter.request_blocks()
    assert snapshot["count"] == 1
    row = snapshot["items"][0]
    assert row["path"] == "/v1/chat/completions"
    assert row["model"] == "fake-chat-1"
    assert row["reason"] == "account_restricted"
    # 指纹本身就是黑名单 key：同一请求再发 = 直接 403，不再打上游
    assert snapshot["items"][0]["fingerprint"] == codar.request_fingerprint("/v1/chat/completions", body)

    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion(body, stream=False)
    assert caught.value.status_code == 403
    assert "黑名单" in caught.value.message
    assert adapter.request_blocks()["count"] == 1

    # 换了请求体就是另一条请求，不连带拦
    other = await adapter.chat_completion(
        {"model": "fake-chat-1", "messages": [{"role": "user", "content": "other"}]}, stream=False
    )
    assert other.status_code == 403
    assert adapter.request_blocks()["count"] == 2


async def test_clear_request_blocks(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(
        fake_base, tmp_path, api_key="codar-forbidden", options={"restricted_request_block": True}
    )
    await adapter.chat_completion(
        {"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]}, stream=False
    )
    assert adapter.clear_request_blocks() == 1
    assert adapter.request_blocks()["count"] == 0
