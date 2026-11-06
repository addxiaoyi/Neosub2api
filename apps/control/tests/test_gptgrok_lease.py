"""G1 租约（Reserve / Release / Feedback）与 gptgrok 适配器接线。

KeyPool 侧覆盖：并发不撞号、归还后可复用、过期租约 id 不误释放、
Feedback 冷却、TTL 回收、池满等待与超时。
适配器侧覆盖：非流式与流式（含提前关闭）都必须归还租约，并发请求不得共用同一把 key。
"""

from __future__ import annotations

import asyncio

import pytest

from app.adapters import gptgrok
from app.adapters.base import ProviderError
from app.adapters.key_pool import KeyLease, KeyPool
from tests.test_vendor_adapters import USERS, _settings as adapter_settings


# ----------------------------- KeyPool -----------------------------


async def test_reserve_hands_out_distinct_keys():
    pool = KeyPool()
    keys = ["k0", "k1", "k2"]
    leases = await asyncio.gather(*[pool.reserve(keys, timeout=0.05) for _ in range(3)])
    assert all(lease is not None for lease in leases), leases
    assert {lease.key for lease in leases} == set(keys), leases
    assert len({lease.id for lease in leases}) == 3, leases
    # 池子占满，后来者等不到归还
    assert await pool.reserve(keys, timeout=0.05) is None


async def test_release_returns_key_to_pool():
    pool = KeyPool()
    lease = await pool.reserve(["k0"], timeout=0.05)
    assert lease is not None
    assert await pool.available(["k0"]) == []
    await pool.release(lease)
    assert await pool.available(["k0"]) == ["k0"]
    again = await pool.reserve(["k0"], timeout=0.05)
    assert again is not None and again.key == "k0"


async def test_stale_lease_id_does_not_free_occupant():
    pool = KeyPool()
    lease = await pool.reserve(["k0"], timeout=0.05)
    assert lease is not None
    await pool.release(KeyLease(lease.key, lease.id + 999))
    assert await pool.available(["k0"]) == [], "别人那份租约不能被旧 id 清掉"
    await pool.release(lease)
    assert await pool.available(["k0"]) == ["k0"]


async def test_feedback_cools_and_counts():
    now = [100.0]
    pool = KeyPool(clock=lambda: now[0])
    lease = await pool.reserve(["k0", "k1"], timeout=0.05)
    assert lease is not None
    await pool.feedback(lease, ok=False, cooldown_seconds=0.5)
    assert await pool.failure_count(lease.key) == 1
    assert await pool.available(["k0", "k1"]) == [k for k in ["k0", "k1"] if k != lease.key]
    assert await pool.uncooled(["k0", "k1"]) == [k for k in ["k0", "k1"] if k != lease.key]
    now[0] += 0.6
    assert await pool.uncooled(["k0", "k1"]) == ["k0", "k1"]
    assert sorted(await pool.available(["k0", "k1"])) == ["k0", "k1"]
    again = await pool.reserve([lease.key], timeout=0.05)
    assert again is not None
    await pool.feedback(again, ok=False, cooldown_seconds=0.5)
    assert await pool.failure_count(lease.key) == 2, "连续失败要累计"
    now[0] += 0.6
    third = await pool.reserve([lease.key], timeout=0.05)
    assert third is not None
    await pool.feedback(third, ok=True)
    assert await pool.failure_count(lease.key) == 0, "成功一次清零"


async def test_lease_ttl_recycles_leaked_lease():
    now = [10.0]
    pool = KeyPool(clock=lambda: now[0], lease_ttl=5.0)
    lease = await pool.reserve(["k0"], timeout=0.05)
    assert lease is not None
    now[0] += 4.0
    assert await pool.available(["k0"]) == [], "未到期不能被其它请求抢走"
    now[0] += 2.0
    assert await pool.available(["k0"]) == ["k0"]
    assert await pool.reserve(["k0"], timeout=0.05) is not None


async def test_reserve_waits_until_release():
    pool = KeyPool()
    held = await pool.reserve(["k0"], timeout=0.05)
    assert held is not None
    waiter = asyncio.create_task(pool.reserve(["k0"], timeout=1.0))
    await asyncio.sleep(0.01)
    assert not waiter.done(), "池满时应等待而不是立即失败"
    await pool.release(held)
    lease = await asyncio.wait_for(waiter, 1.0)
    assert lease is not None and lease.key == "k0"


async def test_reserve_timeout_when_pool_exhausted():
    pool = KeyPool()
    held = await pool.reserve(["k0"], timeout=0.05)
    assert held is not None
    lease = await pool.reserve(["k0"], timeout=0.05)
    assert lease is None, "超时后必须返回 None，让调用方明确失败"


# --------------------------- gptgrok 适配器 ---------------------------


def _build(tmp_path, base_url):
    adapter = gptgrok.build(adapter_settings(tmp_path), base_url)
    adapter.configure(
        api_key="tok-a",
        options={"extra_keys": ["tok-b"], "max_attempts": 1, "key_cooldown": 600},
    )
    return adapter


async def test_non_stream_request_releases_lease(tmp_path, fake_base):
    adapter = _build(tmp_path, fake_base)
    reply = await adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=False)
    assert reply.status_code == 200, reply.body
    assert "authorization=Bearer tok-" in reply.body.decode("utf-8", "ignore")
    assert adapter._keys_pool._leases == {}


async def test_stream_request_releases_lease(tmp_path, fake_base):
    adapter = _build(tmp_path, fake_base)
    reply = await adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=True)
    chunks = [chunk async for chunk in reply.iter_bytes()]
    assert chunks and chunks[-1] == b"data: [DONE]\n\n", chunks[-1:]
    assert adapter._keys_pool._leases == {}
    assert adapter._keys_pool._cooling == {}


async def test_stream_early_close_releases_lease(tmp_path, fake_base):
    adapter = _build(tmp_path, fake_base)
    reply = await adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=True)
    consumer = reply.iter_bytes()
    assert await consumer.__anext__(), "首个 role chunk 必须能取到"
    await reply.aclose()
    assert adapter._keys_pool._leases == {}, "客户端提前断开也要归还租约"


async def test_concurrent_requests_take_different_keys(tmp_path, monkeypatch):
    """两个并发在途请求不得共用同一把 key：上游收到两个不同的 Bearer。"""
    adapter = _build(tmp_path, "http://127.0.0.1:1")
    seen: list[str] = []
    both_started = asyncio.Event()

    class _StubReply:
        status_code = 200
        body = b"{}"
        response = None

        async def iter_bytes(self):
            yield b'data: {"message": {"author": {"role": "assistant"}, '
            yield b'"content": {"parts": ["ok"]}, "status": "finished_successfully"}}\n\n'
            yield b"data: [DONE]\n\n"

    class _StubClient:
        def __init__(self, base, timeout=None, proxy=None, extra_headers=None):
            self._auth = (extra_headers or {}).get("authorization", "")

        async def forward(self, path, body, stream=True):
            seen.append(self._auth)
            if len(seen) >= 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 5.0)
            return _StubReply()

    monkeypatch.setattr(gptgrok, "UnifiedApiClient", _StubClient)

    replies = await asyncio.wait_for(
        asyncio.gather(
            *[
                adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=False)
                for _ in range(2)
            ]
        ),
        timeout=10,
    )
    assert all(reply.status_code == 200 for reply in replies)
    assert set(seen) == {"Bearer tok-a", "Bearer tok-b"}, seen
    assert adapter._keys_pool._leases == {}


async def test_not_retryable_error_releases_lease(tmp_path, fake_base):
    adapter = _build(tmp_path, fake_base)
    with pytest.raises(ProviderError) as failed:
        await adapter.chat_completion(
            {"model": "gpt-5-err-400", "messages": USERS}, stream=False
        )
    assert failed.value.status_code == 400
    assert adapter._keys_pool._leases == {}, "失败退出也必须归还租约"
    assert adapter._keys_pool._cooling == {}, "400 是请求问题，不该冷却凭证"
