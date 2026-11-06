"""opencode / muse 接账号池回归：坏 key 换号、全冷却、流式中断登记、健康度字段。

接的是 services/rotation.py 的轮转池（CredentialPool / pick_in_group），与 gptgrok 的
租约模型（key_pool.py 的 reserve/release/feedback）无关。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.adapters import muse, opencode
from app.adapters.base import ProviderError
from app.services.rotation import fingerprint

USERS = [{"role": "user", "content": "hi"}]
MUSE_PATHS = {"chat_path": "/muse/v1/chat/completions", "health_path": "/muse/v1/models"}


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
        allow_private_upstreams=False,
    )


def _opencode(fake_base: str, tmp: Path, api_key: str, extra: list[str] | None = None, **options: Any):
    adapter = opencode.build(_settings(tmp), fake_base)
    adapter.configure(api_key=api_key, options={"extra_keys": extra or [], **options})
    return adapter


def _muse(fake_base: str, tmp: Path, api_key: str, extra: list[str] | None = None, **options: Any):
    adapter = muse.build(_settings(tmp), fake_base)
    adapter.configure(api_key=api_key, options={**MUSE_PATHS, "extra_keys": extra or [], **options})
    return adapter


async def _collect(reply) -> bytes:
    out = b""
    async for chunk in reply.iter_bytes():
        out += chunk
    return out


def _entry(rotation: dict[str, Any], key: str) -> dict[str, Any]:
    return next(item for item in rotation["credentials"] if item["id"] == fingerprint(key))


# ---------------------------------------------------------------- opencode


async def test_opencode_bad_key_rotates_and_cools(fake_base: str, tmp_path: Path) -> None:
    adapter = _opencode(fake_base, tmp_path, "codar-expired", ["zen-good"], max_attempts=2)
    reply = await adapter.chat_completion({"model": "fake-chat-1", "messages": USERS}, stream=False)
    assert reply.status_code == 200, "第一把 key 401 后必须换到第二把拿到 200"

    rotation = await adapter._pool.snapshot(adapter._keys())
    bad = _entry(rotation, "codar-expired")
    assert bad["state"] == "cooling"
    assert bad["cooldown_remaining"] >= 590, "401 按 600s 档冷却"
    assert _entry(rotation, "zen-good")["state"] == "ready"


@pytest.mark.parametrize("code,span", [(402, 900.0), (403, 600.0), (408, 60.0), (429, 120.0), (500, 60.0), (503, 60.0)])
async def test_opencode_status_tiers_cool_first_key(
    fake_base: str, tmp_path: Path, code: int, span: float
) -> None:
    adapter = _opencode(fake_base, tmp_path, "zen-first", ["zen-second"], max_attempts=2)
    reply = await adapter.chat_completion(
        {"model": f"fake-chat-1-err-{code}", "messages": USERS}, stream=False
    )
    # 第二次尝试换了第二把 key，但同 model 的厂商错误照样命中，最终把该状态码如实透出
    assert reply.status_code == code

    rotation = await adapter._pool.snapshot(adapter._keys())
    first = _entry(rotation, "zen-first")
    assert first["state"] == "cooling"
    assert span - 10 <= first["cooldown_remaining"] <= span, f"{code} 应落 {span:.0f}s 档"


async def test_opencode_all_cooled_is_429(fake_base: str, tmp_path: Path) -> None:
    adapter = _opencode(fake_base, tmp_path, "zen-a", ["zen-b"], max_attempts=2)
    for key in adapter._keys():
        await adapter._pool.report_failure(key, 401)
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "fake-chat-1", "messages": USERS}, stream=False)
    assert caught.value.status_code == 429
    assert "冷却" in caught.value.message


async def test_opencode_stream_break_registers_failure(fake_base: str, tmp_path: Path) -> None:
    adapter = _opencode(fake_base, tmp_path, "zen-fake", max_attempts=1)
    calls: list[tuple[str, str]] = []
    original_success, original_net = adapter._pool.report_success, adapter._pool.report_network_error

    async def spy_success(key: str) -> None:
        calls.append(("success", key))
        await original_success(key)

    async def spy_net(key: str) -> None:
        calls.append(("network", key))
        await original_net(key)

    adapter._pool.report_success = spy_success  # type: ignore[method-assign]
    adapter._pool.report_network_error = spy_net  # type: ignore[method-assign]

    reply = await adapter.chat_completion(
        {"model": "fake-chat-1-stream-break", "messages": USERS, "stream": True, "session_id": "s-break"},
        stream=True,
    )
    with pytest.raises(Exception):
        await _collect(reply)
    assert ("network", "zen-fake") in calls, "流式中断必须登记为出口失败"
    assert all(kind != "success" for kind, _ in calls), "拿到响应头不等于流成功，不得提前记健康"


async def test_opencode_stream_success_writes_health_and_sticky(fake_base: str, tmp_path: Path) -> None:
    adapter = _opencode(fake_base, tmp_path, "zen-fake")
    reply = await adapter.chat_completion(
        {"model": "fake-chat-1", "messages": USERS, "stream": True, "session_id": "s-ok"}, stream=True
    )
    raw = await _collect(reply)
    assert raw.endswith(b"data: [DONE]\n\n")

    assert await adapter._pool.remaining("zen-fake") == 0
    pick = await adapter._pool.acquire(adapter._keys(), sticky_key="s-ok")
    assert pick is not None and pick.rule == "sticky", "流式读完要回写粘性"

    health = await adapter.health()
    assert health["connected"] is True
    assert health["cooling"] == 0
    assert health["rotation"]["available"] == 1
    fields = health["rotation"]["credentials"][0]
    assert {"id", "state", "cooldown_remaining", "consecutive_failures"} <= set(fields)
    assert "zen-fake" not in json.dumps(health), "健康度快照只能出现凭据指纹"


# ---------------------------------------------------------------- muse


async def test_muse_bad_key_rotates_and_cools(fake_base: str, tmp_path: Path) -> None:
    adapter = _muse(fake_base, tmp_path, "bad-key", ["tok-good"], max_attempts=2)
    reply = await adapter.chat_completion({"model": "muse-spark", "messages": USERS}, stream=False)
    assert reply.status_code == 200
    assert "authorization=Bearer tok-good" in json.loads(reply.body)["choices"][0]["message"]["content"]

    rotation = await adapter._pool.snapshot(adapter._keys())
    bad = _entry(rotation, "bad-key")
    assert bad["state"] == "cooling" and bad["cooldown_remaining"] >= 590
    assert _entry(rotation, "tok-good")["state"] == "ready"


@pytest.mark.parametrize("code,span", [(402, 900.0), (408, 60.0), (500, 60.0), (503, 60.0)])
async def test_muse_status_tiers_cool_first_key(fake_base: str, tmp_path: Path, code: int, span: float) -> None:
    adapter = _muse(fake_base, tmp_path, "tok-one", ["tok-two"], max_attempts=2)
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": f"muse-spark-err-{code}", "messages": USERS}, stream=False)
    assert caught.value.status_code == code

    rotation = await adapter._pool.snapshot(adapter._keys())
    first = _entry(rotation, "tok-one")
    assert first["state"] == "cooling"
    assert span - 10 <= first["cooldown_remaining"] <= span, f"{code} 应落 {span:.0f}s 档"


async def test_muse_all_cooled_is_429(fake_base: str, tmp_path: Path) -> None:
    adapter = _muse(fake_base, tmp_path, "tok-a", ["tok-b"], max_attempts=2)
    for key in adapter._keys():
        await adapter._pool.report_failure(key, 401)
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "muse-spark", "messages": USERS}, stream=False)
    assert caught.value.status_code == 429
    assert "冷却" in caught.value.message


async def test_muse_stream_break_registers_failure(fake_base: str, tmp_path: Path) -> None:
    adapter = _muse(fake_base, tmp_path, "tok-m", max_attempts=1)
    calls: list[tuple[str, str]] = []
    original_success, original_net = adapter._pool.report_success, adapter._pool.report_network_error

    async def spy_success(key: str) -> None:
        calls.append(("success", key))
        await original_success(key)

    async def spy_net(key: str) -> None:
        calls.append(("network", key))
        await original_net(key)

    adapter._pool.report_success = spy_success  # type: ignore[method-assign]
    adapter._pool.report_network_error = spy_net  # type: ignore[method-assign]

    reply = await adapter.chat_completion(
        {"model": "muse-spark-stream-break", "messages": USERS, "stream": True}, stream=True
    )
    with pytest.raises(Exception):
        await _collect(reply)
    assert ("network", "tok-m") in calls, "流式中断必须登记为出口失败"
    assert all(kind != "success" for kind, _ in calls), "拿到响应头不等于流成功，不得提前记健康"


async def test_muse_health_reports_rotation_fields(fake_base: str, tmp_path: Path) -> None:
    adapter = _muse(fake_base, tmp_path, "tok-one", ["tok-two"], max_attempts=2)
    with pytest.raises(ProviderError):
        await adapter.chat_completion({"model": "muse-spark-err-402", "messages": USERS}, stream=False)

    health = await adapter.health()
    assert health["connected"] is True
    assert health["cooling"] == 1
    assert health["rotation"]["available"] == 1
    assert _entry(health["rotation"], "tok-one")["state"] == "cooling"
    assert "tok-one" not in json.dumps(health), "健康度快照只能出现凭据指纹"
