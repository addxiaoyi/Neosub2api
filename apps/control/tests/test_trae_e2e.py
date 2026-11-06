"""Trae（Solo / Work）端到端：真实 HTTP 打 scripts/fake_upstream.py，不依赖真凭据。

覆盖：Solo 正常回包（流式/非流式）、请求头契约、429 换号、402 与 5xx 分档、
Work 流式与有状态桥、Work 积分受限降级 Solo、对外错误体脱敏。
假上游的注入约定见 scripts/fake_upstream.py 的 Trae 段。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.adapters import trae
from app.adapters.base import ProviderError
from app.gateway_errors import from_provider_error

pytestmark = pytest.mark.providers

USERS = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
OK_JWT = "trae-jwt-ok"


def _settings(tmp: Path) -> Any:
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
        allow_private_upstreams=True,
    )


def _adapter(
    fake_base: str,
    tmp: Path,
    keys: tuple[str, ...] = (OK_JWT,),
    *,
    channel: str = "solo",
    **options: Any,
) -> trae.TraeAdapter:
    adapter = trae.build(_settings(tmp), fake_base)
    adapter.configure(
        api_key=keys[0],
        api_keys=list(keys),
        options={
            "channel": channel,
            "work_host": fake_base,
            "max_attempts": max(1, len(keys)),
            **options,
        },
    )
    return adapter


def _chat(model: str = "glm-5.2", *, stream: bool = False) -> dict[str, Any]:
    return {"model": model, "messages": USERS, "stream": stream}


async def _drain(reply) -> bytes:
    return b"".join([chunk async for chunk in reply.iter_bytes()])


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
    return "".join(frame["choices"][0]["delta"].get("content") or "" for frame in _frames(raw))


def _recent(fake_base: str, path: str) -> list[dict]:
    with httpx.Client(base_url=fake_base, trust_env=False, timeout=5.0) as client:
        return client.get("/__fake/recent", params={"path": path}).json()


def _clear(fake_base: str) -> None:
    with httpx.Client(base_url=fake_base, trust_env=False, timeout=5.0) as client:
        client.delete("/__fake/last")


# ---------------------------------------------------------------- Solo 正常链路


async def test_solo_chat_stream_and_plain(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path)
    plain = await adapter.chat_completion(_chat(stream=False), stream=False)
    data = json.loads(plain.body)
    message = data["choices"][0]["message"]
    assert "model=glm-5.2" in message["content"]
    assert "authorization=Cloud-IDE-JWT trae-jwt-ok" in message["content"]
    assert message["reasoning_content"] == "think"
    assert data["usage"]["total_tokens"] == 18

    streamed = await adapter.chat_completion(_chat(stream=True), stream=True)
    raw = await _drain(streamed)
    assert "model=glm-5.2" in _text(raw)
    assert '"reasoning_content": "think"' in raw.decode()
    assert '"usage"' in raw.decode()
    assert raw.endswith(b"data: [DONE]\n\n")

    models = await adapter.fetch_models()
    assert [item["id"] for item in models] == ["glm-5.2", "glm-5.1", "DeepSeek-V4-Flash-Official"]


async def test_solo_request_contract(fake_base: str, tmp_path: Path) -> None:
    """按真实契约核对路径/头/body；Trae 不走 Anthropic 头，必须确认没被误加。"""
    adapter = _adapter(fake_base, tmp_path)
    _clear(fake_base)
    await adapter.chat_completion(_chat(stream=False), stream=False)

    calls = _recent(fake_base, trae.EP_CHAT)
    assert len(calls) == 1
    headers = calls[0]["headers"]
    assert headers["authorization"] == "Cloud-IDE-JWT trae-jwt-ok"
    assert headers["x-cloudide-token"] == "trae-jwt-ok"
    assert headers["x-ide-token"] == "trae-jwt-ok"
    assert headers["x-app-id"] == trae.CHAT_APP_ID
    assert headers["user-agent"] == "Trae/0.1.52"
    assert headers["x-ide-version"] == trae.SOLO_VERSION
    assert headers["x-uid"]
    assert len(headers["x-machine-id"]) == 32
    assert len(headers["x-device-id"]) == 32
    # Trae 契约只认 Cloud-IDE-JWT，不能把 Anthropic 的头夹带进来
    assert "x-api-key" not in headers
    assert "anthropic-version" not in headers

    body = json.loads(calls[0]["body"])
    assert body["model"] == "glm-5.2"
    assert body["config_name"] == "glm-5.2"
    assert body["function"] == "solo_work_lite"
    assert body["stream"] is True


# ---------------------------------------------------------------- 换号与分档


async def test_solo_429_rotates_to_next_account(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path, ("trae-jwt-429", OK_JWT))
    _clear(fake_base)
    result = await adapter.chat_completion(_chat(stream=False), stream=False)
    content = json.loads(result.body)["choices"][0]["message"]["content"]
    assert "trae-jwt-ok" in content and "trae-jwt-429" not in content

    assert await adapter._pool.available(["trae-jwt-429", OK_JWT]) == [OK_JWT]
    snapshot = await adapter._pool.snapshot(["trae-jwt-429", OK_JWT])
    assert snapshot["cooling"] == 1

    calls = _recent(fake_base, trae.EP_CHAT)
    assert [call["headers"]["x-cloudide-token"] for call in calls] == ["trae-jwt-429", OK_JWT]


async def test_402_plan_limit_is_single_key_error_and_two_key_failover(fake_base: str, tmp_path: Path) -> None:
    single = _adapter(fake_base, tmp_path, ("trae-jwt-plan",))
    with pytest.raises(ProviderError) as caught:
        await single.chat_completion(_chat(stream=False), stream=False)
    assert caught.value.status_code == 402
    assert getattr(caught.value, "plan_limit", False) is True
    assert "积分" in caught.value.message or "plan" in caught.value.message

    adapter = _adapter(fake_base, tmp_path, ("trae-jwt-plan", OK_JWT))
    result = await adapter.chat_completion(_chat(stream=False), stream=False)
    assert "trae-jwt-ok" in json.loads(result.body)["choices"][0]["message"]["content"]
    snapshot = await adapter._pool.snapshot(["trae-jwt-plan", OK_JWT])
    cooled = [item for item in snapshot["credentials"] if item["state"] == "cooling"]
    assert len(cooled) == 1
    # 402 档：900s 起步，不能被按普通 30s 冷却
    assert 850 <= cooled[0]["cooldown_remaining"] <= 910


@pytest.mark.parametrize(
    "bad_key,model",
    [("trae-jwt-500", "glm-5.2"), ("trae-jwt-503", "glm-5.2")],
    ids=["upstream-500", "upstream-503"],
)
async def test_5xx_maps_to_502_and_uses_short_tier(
    fake_base: str, tmp_path: Path, bad_key: str, model: str
) -> None:
    single = _adapter(fake_base, tmp_path, (bad_key,))
    with pytest.raises(ProviderError) as caught:
        await single.chat_completion(_chat(model, stream=False), stream=False)
    assert caught.value.status_code == 502, "上游 5xx 必须映射成网关 502"

    adapter = _adapter(fake_base, tmp_path, (bad_key, OK_JWT))
    result = await adapter.chat_completion(_chat(model, stream=False), stream=False)
    assert result.status_code == 200
    snapshot = await adapter._pool.snapshot([bad_key, OK_JWT])
    cooled = [item for item in snapshot["credentials"] if item["state"] == "cooling"]
    assert len(cooled) == 1
    # 5xx 档：60s，明显短于 402 的 900s
    assert 40 <= cooled[0]["cooldown_remaining"] <= 70


async def test_sse_error_event_is_classified(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path)
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion(_chat("glm-5.2-trae-sse-plan", stream=False), stream=False)
    assert caught.value.status_code == 402
    assert getattr(caught.value, "plan_limit", False) is True


async def test_stream_error_event_cools_account(fake_base: str, tmp_path: Path) -> None:
    """流里下发 error 也要走失败分档，不能因为已经返回流对象就提前记成功。"""
    keys = ("trae-jwt-stream", OK_JWT)
    adapter = _adapter(fake_base, tmp_path, keys)
    reply = await adapter.chat_completion(_chat("glm-5.2-trae-sse-plan", stream=True), stream=True)
    with pytest.raises(ProviderError) as caught:
        await _drain(reply)
    assert caught.value.status_code == 402
    assert getattr(caught.value, "plan_limit", False) is True

    # 头号账号该被 402 档冷却，后续请求才会换到另一把号
    assert await adapter._pool.available(list(keys)) == [OK_JWT]
    snapshot = await adapter._pool.snapshot(list(keys))
    cooled = [item for item in snapshot["credentials"] if item["state"] == "cooling"]
    assert len(cooled) == 1
    assert 850 <= cooled[0]["cooldown_remaining"] <= 910


async def test_stream_success_waits_until_stream_ends(fake_base: str, tmp_path: Path) -> None:
    """正常读完才记成功；中途被取消不能清掉失败记录。"""
    adapter = _adapter(fake_base, tmp_path)
    key = OK_JWT

    async def seed_failure() -> None:
        await adapter._pool.report_failure(key, 429)
        await adapter._pool.cool(key, 0)  # 只清冷却，保留连续失败计数用于观测

    await seed_failure()
    reply = await adapter.chat_completion(_chat(stream=True), stream=True)
    assert reply._stream is not None
    await reply._stream.__anext__()
    await reply._stream.aclose()  # 客户端中途断开

    snapshot = await adapter._pool.snapshot([key])
    assert snapshot["credentials"][0]["consecutive_failures"] == 1
    assert snapshot["credentials"][0]["state"] == "degraded"

    # 对照：完整消费后流才真正成功，失败计数被清掉
    await seed_failure()
    full = await adapter.chat_completion(_chat(stream=True), stream=True)
    raw = await _drain(full)
    assert raw.endswith(b"data: [DONE]\n\n")
    snapshot = await adapter._pool.snapshot([key])
    assert snapshot["credentials"][0]["consecutive_failures"] == 0
    assert snapshot["credentials"][0]["state"] == "ready"


# ---------------------------------------------------------------- Work 桥


async def test_work_native_stream_and_plain(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path, channel="work")
    model = trae.DEFAULT_WORK_MODEL
    plain = await adapter.chat_completion(_chat(model, stream=False), stream=False)
    content = json.loads(plain.body)["choices"][0]["message"]["content"]
    assert "[fake] trae work" in content
    assert "authorization=Cloud-IDE-JWT trae-jwt-ok" in content
    assert json.loads(plain.body)["usage"]["total_tokens"] == 13

    streamed = await adapter.chat_completion(_chat(model, stream=True), stream=True)
    raw = await _drain(streamed)
    assert "[fake] trae work" in _text(raw)
    assert raw.endswith(b"data: [DONE]\n\n")

    calls = _recent(fake_base, trae.EP_CREATE_TASK)
    headers = calls[-1]["headers"]
    assert "text/event-stream" in headers["accept"]
    assert headers["x-app-id"] == trae.CHAT_APP_ID
    assert headers["authorization"] == "Cloud-IDE-JWT trae-jwt-ok"


async def test_work_credit_limit_falls_back_to_solo(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path, channel="work")
    _clear(fake_base)
    result = await adapter.chat_completion(_chat("work-plan", stream=False), stream=False)
    content = json.loads(result.body)["choices"][0]["message"]["content"]
    # 首帧 code=1005 后必须落到 Solo 端点，而不是把错误抛给调用方
    assert "[fake] trae model=work-plan" in content
    assert "[fake] trae work" not in content
    paths = {item["path"] for item in _recent(fake_base, "")}
    assert trae.EP_CREATE_TASK in paths
    assert trae.EP_CHAT in paths


async def test_work_stateful_bridge(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path, channel="work", work_mode="stateful")
    _clear(fake_base)
    plain = await adapter.chat_completion(_chat(trae.DEFAULT_WORK_MODEL, stream=False), stream=False)
    data = json.loads(plain.body)
    assert data["choices"][0]["message"]["content"] == "[fake] trae work stateful"
    assert data["usage"]["total_tokens"] == 13

    paths = {item["path"] for item in _recent(fake_base, "")}
    assert trae.EP_CREATE_TASK in paths
    assert trae.EP_WORKFLOW_START in paths
    assert trae.EP_QUERY_HISTORY in paths


# ---------------------------------------------------------------- 对外错误脱敏


async def test_error_payload_hides_internal_endpoint(fake_base: str, tmp_path: Path) -> None:
    adapter = _adapter(fake_base, tmp_path)
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion(_chat("glm-5.2-trae-err-url", stream=False), stream=False)
    # 先确认假上游确实把端点写进了错误体，否则下面的脱敏断言没有意义
    assert "127.0.0.1" in caught.value.message

    gateway = from_provider_error(caught.value, "trae")
    dumped = json.dumps(gateway.openai_payload(), ensure_ascii=False)
    assert "127.0.0.1" not in dumped
    port = fake_base.rsplit(":", 1)[-1]
    assert f":{port}" not in dumped
    assert "内部地址已隐藏" in dumped
