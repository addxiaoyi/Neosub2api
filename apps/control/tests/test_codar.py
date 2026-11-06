"""codar 仓内实现：假上游覆盖流式/非流式、错误码、模型列表、多 token 与 401 续期。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


async def test_health(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path, api_key="codar-token-1")
    health = await adapter.health()
    assert health["connected"] is True
    assert health["models"] == 2

    missing = await _adapter(fake_base, tmp_path).health()
    assert missing["connected"] is False
    assert "未配置" in missing["error"]
