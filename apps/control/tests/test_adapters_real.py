"""适配器真实转发回归：逐 provider 验证声明为真的能力位确实打到上游。

覆盖 codebuddy / opencode / sub2api / 四家 sidecar（codar / trae / deepseek / qoder）。
假上游由 scripts/fake_upstream.py 提供：model 带 `err-<code>` 注入状态码，
`stream-break` 注入流中断。muse / gptgrok / zcode / codar 已有各自的专项测试文件。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from app.adapters import BUILDERS, codebuddy, codar_sidecar, deepseek_sidecar, opencode
from app.adapters import qoder_sidecar, sub2api, trae_sidecar
from app.adapters.base import ProviderAdapter, ProviderError

pytestmark = pytest.mark.providers

USERS = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
MESSAGES = [{"role": "user", "content": "hi"}]


def _settings(tmp: Path, *, allow_private: bool = False) -> Any:
    from app.config import Settings

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


async def _collect(reply) -> bytes:
    out = b""
    async for chunk in reply.iter_bytes():
        out += chunk
    return out


async def _collect_with_timeout(reply, timeout: float = 10.0) -> bytes:
    return await asyncio.wait_for(_collect(reply), timeout=timeout)


def _frames(raw: bytes) -> list[dict]:
    frames: list[dict] = []
    for block in raw.decode("utf-8", "ignore").split("\n\n"):
        block = block.strip()
        if not block.startswith("data:"):
            continue
        payload = block[5:].strip()
        if payload == "[DONE]" or not payload:
            continue
        frames.append(json.loads(payload))
    return frames


def _text(raw: bytes) -> str:
    return "".join(
        frame["choices"][0]["delta"].get("content") or "" for frame in _frames(raw)
    )


def _chat_payload(model: str, *, stream: bool) -> dict[str, Any]:
    return {"model": model, "messages": USERS, "stream": stream}


# ---------------------------------------------------------------- codebuddy


def _codebuddy(fake_base: str, tmp: Path) -> codebuddy.CodeBuddyAdapter:
    adapter = codebuddy.build(_settings(tmp), fake_base)
    adapter.configure(api_key="wbt-fake")
    return adapter


async def test_codebuddy_chat_success(fake_base: str, tmp_path: Path) -> None:
    adapter = _codebuddy(fake_base, tmp_path)
    plain = await adapter.chat_completion(_chat_payload("fake-chat-1", stream=False), stream=False)
    assert plain.status_code == 200
    assert "model=fake-chat-1" in json.loads(plain.body)["choices"][0]["message"]["content"]

    streamed = await adapter.chat_completion(_chat_payload("fake-chat-1", stream=True), stream=True)
    raw = await _collect_with_timeout(streamed)
    assert "model=fake-chat-1" in _text(raw)
    assert raw.endswith(b"data: [DONE]\n\n")


async def test_codebuddy_timeout_respects_options(fake_base: str, tmp_path: Path) -> None:
    """options.timeout 必须生效：假上游拖 1.5s，0.3s 超时要快速失败而不是硬等 120s。"""
    adapter = _codebuddy(fake_base, tmp_path)
    adapter.configure(api_key="wbt-fake", options={"timeout": "0.3"})
    started = time.monotonic()
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion(_chat_payload("fake-chat-1-slow", stream=False), stream=False)
    elapsed = time.monotonic() - started
    assert caught.value.status_code == 504, caught.value.message
    assert elapsed < 1.4, f"超时未生效，实际耗时 {elapsed:.2f}s"
    assert "127.0.0.1" not in caught.value.message and "7899" not in caught.value.message


@pytest.mark.parametrize("code", [401, 500])
async def test_codebuddy_chat_upstream_error_passthrough(fake_base: str, tmp_path: Path, code: int) -> None:
    adapter = _codebuddy(fake_base, tmp_path)
    reply = await adapter.chat_completion(_chat_payload(f"fake-chat-1-err-{code}", stream=False), stream=False)
    assert reply.status_code == code
    assert f"fake vendor error {code}" in (reply.body or b"").decode()


async def test_codebuddy_stream_interrupt_does_not_hang(fake_base: str, tmp_path: Path) -> None:
    adapter = _codebuddy(fake_base, tmp_path)
    reply = await adapter.chat_completion(_chat_payload("fake-chat-1-stream-break", stream=True), stream=True)
    with pytest.raises(Exception) as caught:
        await _collect_with_timeout(reply)
    assert not isinstance(caught.value, asyncio.TimeoutError)


async def test_codebuddy_messages_both_modes(fake_base: str, tmp_path: Path) -> None:
    adapter = _codebuddy(fake_base, tmp_path)
    plain = await adapter.messages({"model": "fake-chat-1", "messages": MESSAGES}, stream=False)
    assert plain.status_code == 200 and b'"type":"message"' in (plain.body or b"")
    streamed = await adapter.messages({"model": "fake-chat-1", "messages": MESSAGES, "stream": True}, stream=True)
    assert b"event: message_stop" in await _collect_with_timeout(streamed)


# ---------------------------------------------------------------- opencode


def _opencode(fake_base: str, tmp: Path, **options: Any) -> opencode.OpenCodeAdapter:
    adapter = opencode.build(_settings(tmp), fake_base)
    adapter.configure(api_key="zen-fake", options=options)
    return adapter


async def test_opencode_chat_success(fake_base: str, tmp_path: Path) -> None:
    adapter = _opencode(fake_base, tmp_path, max_attempts=1)
    plain = await adapter.chat_completion(_chat_payload("fake-chat-1", stream=False), stream=False)
    assert plain.status_code == 200
    assert "model=fake-chat-1" in json.loads(plain.body)["choices"][0]["message"]["content"]

    streamed = await adapter.chat_completion(_chat_payload("fake-chat-1", stream=True), stream=True)
    raw = await _collect_with_timeout(streamed)
    assert "model=fake-chat-1" in _text(raw)
    assert raw.endswith(b"data: [DONE]\n\n")


async def test_opencode_without_key_is_explicit_503(fake_base: str, tmp_path: Path) -> None:
    adapter = opencode.build(_settings(tmp_path), fake_base)
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion(_chat_payload("fake-chat-1", stream=False), stream=False)
    assert caught.value.status_code == 503


async def test_opencode_rotates_key_on_401(fake_base: str, tmp_path: Path) -> None:
    adapter = _opencode(fake_base, tmp_path, extra_keys=["zen-good"], max_attempts=2)
    adapter.api_key = "bad-key"
    reply = await adapter.chat_completion(_chat_payload("fake-chat-1", stream=False), stream=False)
    # 第一把 key 被假上游按 bad-key 规则 401，轮换到第二把后必须拿到 200
    assert reply.status_code == 200
    assert b"model=fake-chat-1" in (reply.body or b"")


@pytest.mark.parametrize("code", [400, 401, 500])
async def test_opencode_error_surface(fake_base: str, tmp_path: Path, code: int) -> None:
    adapter = _opencode(fake_base, tmp_path, max_attempts=1)
    reply = await adapter.chat_completion(_chat_payload(f"fake-chat-1-err-{code}", stream=False), stream=False)
    assert reply.status_code == code


async def test_opencode_stream_interrupt_does_not_hang(fake_base: str, tmp_path: Path) -> None:
    adapter = _opencode(fake_base, tmp_path, max_attempts=1)
    reply = await adapter.chat_completion(_chat_payload("fake-chat-1-stream-break", stream=True), stream=True)
    with pytest.raises(Exception) as caught:
        await _collect_with_timeout(reply)
    assert not isinstance(caught.value, asyncio.TimeoutError)


async def test_opencode_messages_stream(fake_base: str, tmp_path: Path) -> None:
    adapter = _opencode(fake_base, tmp_path, max_attempts=1)
    reply = await adapter.messages({"model": "fake-chat-1", "messages": MESSAGES, "stream": True}, stream=True)
    assert b"event: message_stop" in await _collect_with_timeout(reply)


# ---------------------------------------------------------------- sub2api


def _sub2api(fake_base: str, tmp: Path, group: str = "g1") -> sub2api.Sub2ApiAdapter:
    # 账号 upstream_base 走地址守卫，测试必须显式放开私网上游
    adapter = sub2api.build(_settings(tmp, allow_private=True), fake_base)
    adapter.configure(options={"group": group, "timeout": 30.0})
    adapter.store.upsert_group(group, name=group)
    adapter.store.add_account(group, name="a1", upstream_base=fake_base, api_key="sub-key")
    return adapter


async def test_sub2api_chat_through_account_pool(fake_base: str, tmp_path: Path) -> None:
    adapter = _sub2api(fake_base, tmp_path)
    plain = await adapter.chat_completion(_chat_payload("fake-chat-1", stream=False), stream=False)
    assert plain.status_code == 200
    assert b"model=fake-chat-1" in (plain.body or b"")
    ledger = adapter.store.ledger("g1", limit=1)
    assert ledger and ledger[0]["status"] == 200

    streamed = await adapter.chat_completion(_chat_payload("fake-chat-1", stream=True), stream=True)
    raw = await _collect_with_timeout(streamed)
    assert raw.endswith(b"data: [DONE]\n\n")


async def test_sub2api_messages_forwarded(fake_base: str, tmp_path: Path) -> None:
    adapter = _sub2api(fake_base, tmp_path)
    reply = await adapter.messages({"model": "fake-chat-1", "messages": MESSAGES}, stream=False)
    assert reply.status_code == 200 and b'"type":"message"' in (reply.body or b"")


async def test_sub2api_401_recorded(fake_base: str, tmp_path: Path) -> None:
    adapter = _sub2api(fake_base, tmp_path)
    reply = await adapter.chat_completion(_chat_payload("fake-chat-1-err-401", stream=False), stream=False)
    assert reply.status_code == 401
    assert adapter.store.ledger("g1", limit=1)[0]["status"] == 401


async def test_sub2api_500_cools_account_then_429(fake_base: str, tmp_path: Path) -> None:
    """上游 500 属于可换号错误：唯一账号被冷却后池空，按配额语义明确 429 而不是假装 200。"""
    adapter = _sub2api(fake_base, tmp_path)
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion(_chat_payload("fake-chat-1-err-500", stream=False), stream=False)
    assert caught.value.status_code == 429
    assert adapter.store.list_groups()[0]["active_accounts"] == 0


async def test_sub2api_stream_interrupt_does_not_hang(fake_base: str, tmp_path: Path) -> None:
    adapter = _sub2api(fake_base, tmp_path)
    reply = await adapter.chat_completion(_chat_payload("fake-chat-1-stream-break", stream=True), stream=True)
    with pytest.raises(Exception) as caught:
        await _collect_with_timeout(reply)
    assert not isinstance(caught.value, asyncio.TimeoutError)


async def test_sub2api_empty_pool_is_429(fake_base: str, tmp_path: Path) -> None:
    adapter = sub2api.build(_settings(tmp_path, allow_private=True), fake_base)
    adapter.configure(options={"group": "empty"})
    adapter.store.upsert_group("empty", name="empty")
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion(_chat_payload("fake-chat-1", stream=False), stream=False)
    assert caught.value.status_code == 429


# ---------------------------------------------------------------- sidecars


SIDECARS: list[tuple[str, Callable[..., ProviderAdapter], str]] = [
    # messages_mode：none = 上游无该端点；nonstream = 上游仅非流式；stream = 流式+非流式
    ("trae_sidecar", trae_sidecar.build, "none"),
    ("deepseek_sidecar", deepseek_sidecar.build, "nonstream"),
    ("qoder_sidecar", qoder_sidecar.build, "stream"),
    ("codar_sidecar", codar_sidecar.build, "stream"),
]


@pytest.mark.parametrize("pid,build,messages_mode", SIDECARS, ids=[item[0] for item in SIDECARS])
async def test_sidecar_chat_forwarding(
    fake_base: str, tmp_path: Path, pid: str, build: Callable[..., ProviderAdapter], messages_mode: str
) -> None:
    adapter = build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="sidecar-key")
    plain = await adapter.chat_completion(_chat_payload("fake-chat-1", stream=False), stream=False)
    assert plain.status_code == 200, pid
    assert b"model=fake-chat-1" in (plain.body or b""), pid

    streamed = await adapter.chat_completion(_chat_payload("fake-chat-1", stream=True), stream=True)
    raw = await _collect_with_timeout(streamed)
    assert raw.endswith(b"data: [DONE]\n\n"), pid

    body = {"model": "fake-chat-1", "messages": MESSAGES}
    if messages_mode == "stream":
        streamed_msg = await adapter.messages({**body, "stream": True}, stream=True)
        assert b"event: message_stop" in await _collect_with_timeout(streamed_msg)
        return
    if messages_mode == "nonstream":
        # Deepseek2Go 上游 main.go 的 Anthropic 流式恒为 501：非流式真实转发，流式显式拒绝
        assert (await adapter.messages(body, stream=False)).status_code == 200
        with pytest.raises(ProviderError) as caught:
            await adapter.messages(body, stream=True)
        assert caught.value.status_code == 501
        return
    # trae2api 上游没有 /v1/messages，能力位为 False，基类必须显式 501
    assert adapter.spec.capabilities["messages"] is False
    with pytest.raises(ProviderError) as caught:
        await adapter.messages(body, stream=False)
    assert caught.value.status_code == 501


@pytest.mark.parametrize("pid,build,_", SIDECARS, ids=[item[0] for item in SIDECARS])
@pytest.mark.parametrize("code", [401, 500])
async def test_sidecar_upstream_errors_pass_through(
    fake_base: str, tmp_path: Path, pid: str, build: Callable[..., ProviderAdapter], _: str, code: int
) -> None:
    adapter = build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="sidecar-key")
    reply = await adapter.chat_completion(_chat_payload(f"fake-chat-1-err-{code}", stream=False), stream=False)
    assert reply.status_code == code, pid


@pytest.mark.parametrize("pid,build,_", SIDECARS, ids=[item[0] for item in SIDECARS])
async def test_sidecar_stream_interrupt_does_not_hang(
    fake_base: str, tmp_path: Path, pid: str, build: Callable[..., ProviderAdapter], _: str
) -> None:
    adapter = build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="sidecar-key")
    reply = await adapter.chat_completion(_chat_payload("fake-chat-1-stream-break", stream=True), stream=True)
    with pytest.raises(Exception) as caught:
        await _collect_with_timeout(reply)
    assert not isinstance(caught.value, asyncio.TimeoutError), pid


# ---------------------------------------------------------------- 能力位与实现的一致性


def test_declared_capability_is_not_silently_stubbed(tmp_path: Path) -> None:
    """声明为 True 的 chat/messages/responses 必须由子类覆盖，不能落到基类 501。"""
    missing: list[str] = []
    for build in BUILDERS:
        adapter = build(_settings(tmp_path))
        for cap, attr in (
            ("chat", "chat_completion"),
            ("messages", "messages"),
            ("responses", "responses"),
        ):
            if not adapter.spec.capabilities.get(cap):
                continue
            if getattr(type(adapter), attr) is getattr(ProviderAdapter, attr):
                missing.append(f"{adapter.spec.id}.{cap}")
    assert not missing, f"声明了但仍是基类桩：{missing}"
