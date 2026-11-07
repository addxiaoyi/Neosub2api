"""muse / gptgrok / zcode 三家仓内实现的集成测试。

假上游（scripts/fake_upstream.py）会回显关键鉴权头，并用 model 里的 err-<code> 注入错误。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

CONTROL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROL))

from app.adapters import gptgrok, muse, zcode  # noqa: E402
from app.adapters.base import ProviderError  # noqa: E402
from app.adapters.key_pool import KeyPool  # noqa: E402
from app.config import Settings  # noqa: E402


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "unified.db",
        web_dir=tmp_path / "web",
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


async def _collect(reply) -> bytes:
    out = b""
    async for chunk in reply.iter_bytes():
        out += chunk
    return out


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


def _content(frames: list[dict]) -> str:
    return "".join(frame["choices"][0]["delta"].get("content") or "" for frame in frames)


USERS = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]


# ----------------------------- zcode -----------------------------


async def test_zcode_headers_are_client_shaped(tmp_path):
    adapter = zcode.build(_settings(tmp_path), "https://zcode.z.ai")
    adapter.configure(api_key="tok-z", options={"uid": "u-1", "extra_headers": {"x-extra": "1"}})
    headers = adapter._headers("tok-z", stream=True)
    assert headers["authorization"] == "Bearer tok-z"
    assert headers["x-api-key"] == "tok-z"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["anthropic-beta"] == "mid-conversation-system-2026-04-07"
    assert headers["x-device-mid"] == adapter._device_id("u-1")
    assert headers["x-extra"] == "1"  # options.extra_headers 覆盖


async def test_zcode_stream_translates_anthropic_events(tmp_path, fake_base):
    adapter = zcode.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-z", options={"system_prompt_override": "OVER"})
    reply = await adapter.chat_completion({"model": "GLM-5.3", "messages": USERS}, stream=True)
    raw = await _collect(reply)
    frames = _frames(raw)
    assert len(frames) >= 3, "流式必须逐帧下发，不能拼成整体"
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
    text = _content(frames)
    assert "system='OVER'" in text
    assert "authorization=Bearer tok-z" in text
    assert "x-api-key=tok-z" in text
    assert "anthropic-beta=mid-conversation-system-2026-04-07" in text
    assert raw.endswith(b"data: [DONE]\n\n")


async def test_zcode_nonstream_maps_to_openai(tmp_path, fake_base):
    adapter = zcode.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-z", options={"system_prompt_override": "OVER"})
    reply = await adapter.chat_completion({"model": "GLM-5.3", "messages": USERS}, stream=False)
    data = json.loads(reply.body)
    choice = data["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert "system='OVER'" in choice["message"]["content"]
    assert data["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


async def test_zcode_messages_passthrough(tmp_path, fake_base):
    adapter = zcode.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-z")
    body = {"model": "GLM-5.3", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}
    stream_reply = await adapter.messages(body, stream=True)
    raw = await _collect(stream_reply)
    assert b"event: content_block_delta" in raw
    assert b"event: message_stop" in raw

    plain = await adapter.messages(body, stream=False)
    data = json.loads(plain.body)
    assert data["type"] == "message"
    assert data["content"][0]["text"].startswith("[fake] zcode")


async def test_zcode_error_codes_and_key_rotation(tmp_path, fake_base):
    adapter = zcode.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-z", options={"max_attempts": 1})
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "risk-3012", "messages": USERS}, stream=False)
    assert caught.value.status_code == 403 and "3012" in caught.value.message

    adapter.configure(api_key="tok-z", options={"max_attempts": 1})
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "err-429", "messages": USERS}, stream=False)
    assert caught.value.status_code == 429

    rotating = zcode.build(_settings(tmp_path), fake_base)
    rotating.configure(api_key="bad-key", options={"extra_keys": ["tok-good"], "max_attempts": 2})
    reply = await rotating.chat_completion({"model": "GLM-5.3", "messages": USERS}, stream=False)
    assert "authorization=Bearer tok-good" in reply.body.decode()


async def test_zcode_3012_names_missing_official_system(tmp_path, fake_base, caplog):
    """缺 official_system 时 3012 必须自曝缺项，并留下可检索日志。"""
    caplog.set_level("WARNING")
    adapter = zcode.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-z", options={"max_attempts": 1})
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "risk-3012", "messages": USERS}, stream=False)
    err = caught.value
    assert err.status_code == 403
    assert "official_system" in err.message and "3012" in err.message
    assert "未配置" in err.message
    assert err.upstream_code == "3012"
    assert any("official_system" in record.message for record in caplog.records)


async def test_zcode_3012_with_official_system_reports_mismatch(tmp_path, fake_base):
    """已导入仍回 3012：说明原文不匹配，而不是重复说「缺」。"""
    adapter = zcode.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-z", options={
        "max_attempts": 1, "official_system": "OFFICIAL-SYSTEM-TEXT",
    })
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "risk-3012", "messages": USERS}, stream=False)
    assert caught.value.status_code == 403
    assert "已配置" in caught.value.message and "3012" in caught.value.message


async def test_zcode_models_and_health(tmp_path, fake_base):
    adapter = zcode.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-z", options={"models": ["GLM-5.3-Air"]})
    models = [item["id"] for item in await adapter.list_models()]
    assert models == ["GLM-5.3-Flash", "GLM-5.3", "GLM-5.3-Air"]

    health = await adapter.health()
    assert health["connected"] is True
    assert health["balance"]["balance"] == 123.4

    bare = zcode.build(_settings(tmp_path), fake_base)
    assert (await bare.health())["connected"] is False


# ----------------------------- 多密钥轮换（zcode / gptgrok） -----------------------------


async def test_zcode_rotates_across_requests(tmp_path, fake_base):
    """游标跨请求推进：第二把 key 不再永远排不上号。"""
    adapter = zcode.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-a", options={"extra_keys": ["tok-b"], "max_attempts": 1})

    first = await adapter.chat_completion({"model": "GLM-5.3", "messages": USERS}, stream=False)
    second = await adapter.chat_completion({"model": "GLM-5.3", "messages": USERS}, stream=False)
    assert "x-api-key=tok-a" in first.body.decode()
    assert "x-api-key=tok-b" in second.body.decode()


async def test_zcode_cools_failed_key_and_skips_it(tmp_path, fake_base):
    adapter = zcode.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="bad-key", options={
        "extra_keys": ["tok-good"], "max_attempts": 2, "key_cooldown": 600,
    })

    first = await adapter.chat_completion({"model": "GLM-5.3", "messages": USERS}, stream=False)
    assert "x-api-key=tok-good" in first.body.decode()
    # 失败的那把进入冷却，后续请求不会再撞到它
    assert await adapter._keys_pool.available(adapter._keys()) == ["tok-good"]

    second = await adapter.chat_completion({"model": "GLM-5.3", "messages": USERS}, stream=False)
    assert "x-api-key=tok-good" in second.body.decode()


async def test_zcode_all_keys_failed_reports_provider(tmp_path, fake_base):
    adapter = zcode.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="bad-key", options={"max_attempts": 2, "key_cooldown": 600})
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "GLM-5.3", "messages": USERS}, stream=False)
    assert caught.value.status_code == 401
    assert "ZCode" in caught.value.message
    assert "全部不可用" in caught.value.message


async def test_zcode_5xx_cools_key(tmp_path, fake_base):
    adapter = zcode.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-a", options={"extra_keys": ["tok-b"], "max_attempts": 2})

    with pytest.raises(ProviderError):
        await adapter.chat_completion({"model": "fake-chat-1-err-500", "messages": USERS}, stream=False)
    assert await adapter._keys_pool.available(adapter._keys()) == ["tok-b"]


async def test_gptgrok_rotates_across_requests(tmp_path, fake_base):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-a", options={"extra_keys": ["tok-b"], "max_attempts": 1})

    first = await adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=False)
    second = await adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=False)
    assert "authorization=Bearer tok-a" in first.body.decode()
    assert "authorization=Bearer tok-b" in second.body.decode()


async def test_gptgrok_cools_failed_key_and_skips_it(tmp_path, fake_base):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="bad-key", options={
        "extra_keys": ["tok-good"], "max_attempts": 2, "key_cooldown": 600,
    })

    first = await adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=False)
    assert "authorization=Bearer tok-good" in first.body.decode()
    assert await adapter._keys_pool.available(adapter._keys()) == ["tok-good"]

    second = await adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=False)
    assert "authorization=Bearer tok-good" in second.body.decode()


async def test_gptgrok_all_keys_failed_reports_provider(tmp_path, fake_base):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="bad-key", options={"max_attempts": 2, "key_cooldown": 600})
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=False)
    assert caught.value.status_code == 401
    assert "GPT/Grok" in caught.value.message
    assert "全部不可用" in caught.value.message


async def test_rotation_state_is_per_instance(tmp_path, fake_base):
    """两个实例各持游标：A 用完第一把，不应影响 B 的起点。"""
    left = zcode.build(_settings(tmp_path), fake_base)
    right = zcode.build(_settings(tmp_path), fake_base)
    for adapter in (left, right):
        adapter.configure(api_key="tok-a", options={"extra_keys": ["tok-b"], "max_attempts": 1})

    await left.chat_completion({"model": "GLM-5.3", "messages": USERS}, stream=False)
    fresh_right = await right.chat_completion({"model": "GLM-5.3", "messages": USERS}, stream=False)
    assert "x-api-key=tok-a" in fresh_right.body.decode()


async def test_key_pool_cooldown_expires():
    # 注入时钟：到期与否按推进的时间点判定，不再靠真实 sleep 卡 10ms 余量
    now = [1000.0]
    pool = KeyPool(clock=lambda: now[0])
    await pool.cool("k1", 0.05)
    assert await pool.available(["k1", "k2"]) == ["k2"]
    now[0] += 0.06
    assert await pool.available(["k1", "k2"]) == ["k1", "k2"]


# ----------------------------- gptgrok -----------------------------


async def test_gptgrok_chatgpt_stream_and_fingerprint(tmp_path, fake_base):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g", options={"fingerprint": True})
    reply = await adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=True)
    raw = await _collect(reply)
    frames = _frames(raw)
    assert len(frames) >= 3
    text = _content(frames)
    assert "authorization=Bearer tok-g" in text
    matched = re.search(r"oai-device-id=([0-9a-f-]{36})", text)
    assert matched, text
    assert raw.endswith(b"data: [DONE]\n\n")


async def test_gptgrok_chatgpt_nonstream(tmp_path, fake_base):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g")
    reply = await adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=False)
    data = json.loads(reply.body)
    message = data["choices"][0]["message"]
    assert data["choices"][0]["finish_reason"] == "stop"
    assert "authorization=Bearer tok-g" in message["content"]
    assert "system" not in message


async def test_gptgrok_grok_channel_reasoning(tmp_path, fake_base):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g")
    reply = await adapter.chat_completion({"model": "grok-4.20-fast", "messages": USERS}, stream=True)
    frames = _frames(await _collect(reply))
    reasoning = "".join(
        frame["choices"][0]["delta"].get("reasoning_content") or "" for frame in frames
    )
    assert reasoning == "[think]"
    assert "cookie=sso=tok-g; sso-rw=tok-g" in _content(frames)

    plain = json.loads((await adapter.chat_completion(
        {"model": "grok-4.20-fast", "messages": USERS}, stream=False)).body)
    assert plain["choices"][0]["message"]["reasoning_content"] == "[think]"


async def test_gptgrok_error_passthrough_and_models(tmp_path, fake_base):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g", options={"max_attempts": 1})
    for code in (400, 429, 500):
        with pytest.raises(ProviderError) as caught:
            await adapter.chat_completion({"model": f"gpt-5-err-{code}", "messages": USERS}, stream=False)
        assert caught.value.status_code == code

    ids = [item["id"] for item in await adapter.list_models()]
    assert "gpt-5" in ids and "grok-4.20-fast" in ids

    grok_only = gptgrok.build(_settings(tmp_path), fake_base)
    grok_only.configure(api_key="tok-g", options={"channel": "grok"})
    assert all(item["id"].startswith("grok") for item in await grok_only.list_models())


async def test_gptgrok_403_names_missing_sentinel(tmp_path, fake_base, caplog):
    """缺 sentinel_token 的 403 必须点名缺项，并留下可检索日志。"""
    caplog.set_level("WARNING")
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g", options={"max_attempts": 1})
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "gpt-5-err-403", "messages": USERS}, stream=False)
    err = caught.value
    assert err.status_code == 403
    assert "sentinel_token" in err.message and "403" in err.message
    assert "未配置" in err.message
    assert err.upstream_code == "403"
    assert any("sentinel_token" in record.message for record in caplog.records)


async def test_gptgrok_403_with_sentinel_configured_distinguishes_expiry(tmp_path, fake_base):
    """已导入仍 403：提示可能过期/会话不匹配，同时确认 token 真的进了请求头。"""
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g", options={
        "max_attempts": 1, "sentinel_token": "old-sentinel", "fingerprint": True,
    })
    headers = adapter._chatgpt_headers("tok-g", stream=True)
    assert headers["openai-sentinel-chat-requirements-token"] == "old-sentinel"

    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "gpt-5-err-403", "messages": USERS}, stream=False)
    assert caught.value.status_code == 403
    assert "已配置" in caught.value.message and "过期" in caught.value.message


async def test_gptgrok_health(tmp_path, fake_base):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g")
    assert (await adapter.health())["connected"] is True

    grok_side = gptgrok.build(_settings(tmp_path), fake_base)
    grok_side.configure(api_key="tok-g", options={"health_channel": "grok"})
    assert (await grok_side.health())["connected"] is True

    denied = gptgrok.build(_settings(tmp_path), fake_base)
    denied.configure(api_key="bad-key", options={"max_attempts": 1})
    assert (await denied.health())["connected"] is False


# ----------------------------- muse -----------------------------


MUSE_PATHS = {"chat_path": "/muse/v1/chat/completions", "health_path": "/muse/v1/models"}


async def test_muse_stream_passthrough(tmp_path, fake_base):
    adapter = muse.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-m", options={**MUSE_PATHS, "cookie": "hatch_sess=fake"})
    reply = await adapter.chat_completion({"model": "muse-spark", "messages": USERS}, stream=True)
    raw = await _collect(reply)
    frames = _frames(raw)
    assert len(frames) == 4, "OpenAI 帧必须逐 chunk 直通"
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"
    text = _content(frames)
    assert "authorization=Bearer tok-m" in text
    assert "cookie=hatch_sess=fake" in text
    assert raw.endswith(b"data: [DONE]\n\n")


async def test_muse_nonstream_and_prompt_fallback(tmp_path, fake_base):
    adapter = muse.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-m", options=MUSE_PATHS)
    reply = await adapter.chat_completion({"model": "muse-spark", "prompt": "hello"}, stream=False)
    data = json.loads(reply.body)
    assert data["choices"][0]["message"]["content"].startswith("[fake] muse")
    assert data["usage"]["total_tokens"] == 18

    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "muse-spark"}, stream=False)
    assert caught.value.status_code == 400


async def test_muse_error_and_key_rotation(tmp_path, fake_base):
    adapter = muse.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-m", options={**MUSE_PATHS, "max_attempts": 1})
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "err-429", "messages": USERS}, stream=False)
    assert caught.value.status_code == 429

    rotating = muse.build(_settings(tmp_path), fake_base)
    rotating.configure(api_key="bad-key", options={
        **MUSE_PATHS, "extra_keys": ["tok-good"], "max_attempts": 2,
    })
    reply = await rotating.chat_completion({"model": "muse-spark", "messages": USERS}, stream=False)
    assert "authorization=Bearer tok-good" in reply.body.decode()


async def test_muse_models_and_health(tmp_path, fake_base):
    adapter = muse.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-m", options={**MUSE_PATHS, "models": ["muse-code"]})
    ids = [item["id"] for item in await adapter.list_models()]
    assert ids == ["muse-spark", "muse-image", "muse-video", "muse-code"]

    health = await adapter.health()
    assert health["connected"] is True and health["models"] == 3

    denied = muse.build(_settings(tmp_path), fake_base)
    denied.configure(api_key="bad-key", options={**MUSE_PATHS, "max_attempts": 1})
    assert (await denied.health())["connected"] is False
