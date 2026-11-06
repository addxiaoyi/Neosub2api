"""G3 请求级限流的单元用例：用最小 FastAPI 应用挂中间件，不启动真实控制面。

覆盖：默认全关零行为变化、密钥 RPM 429（两种协议壳 + Retry-After）、
密钥互不连坐、窗口到期恢复、全局并发 503（含流式占用）、探活/管理面不受影响。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from app.middleware.rate_limit import (
    RATE_WINDOW_SEC,
    RateLimitConfig,
    RateLimitMiddleware,
    load_rate_limit_config,
)

pytestmark = pytest.mark.gateway

KEY_A = {"Authorization": "Bearer u2a_key_alpha"}
KEY_B = {"x-api-key": "u2a_key_beta"}


def _app(config: RateLimitConfig) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, config=config)

    @app.get("/v1/models")
    async def models():
        return {"ok": True}

    @app.post("/v1/messages")
    async def messages():
        return {"ok": True}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/health")
    async def api_health():
        return {"ok": True}

    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_unconfigured_keeps_behaviour_unchanged(monkeypatch):
    monkeypatch.delenv("UNIFIED_RATE_LIMIT_RPM", raising=False)
    monkeypatch.delenv("UNIFIED_RATE_LIMIT_MAX_CONCURRENCY", raising=False)

    cfg = load_rate_limit_config()
    assert cfg.rpm == 0
    assert cfg.max_concurrency == 0

    async with _client(_app(cfg)) as c:
        for _ in range(20):
            assert (await c.get("/v1/models", headers=KEY_A)).status_code == 200
        # 未配置时连 429 都不会出现，探活照旧
        assert (await c.get("/healthz")).status_code == 200


def test_invalid_env_falls_back_to_off(monkeypatch):
    monkeypatch.setenv("UNIFIED_RATE_LIMIT_RPM", "not-a-number")
    monkeypatch.setenv("UNIFIED_RATE_LIMIT_MAX_CONCURRENCY", "-5")

    cfg = load_rate_limit_config()
    assert cfg.rpm == 0, "脏值不能把控制面变成半开状态"
    assert cfg.max_concurrency == 0


async def test_key_rpm_limit_uses_openai_shell():
    async with _client(_app(RateLimitConfig(rpm=2))) as c:
        assert (await c.get("/v1/models", headers=KEY_A)).status_code == 200
        assert (await c.get("/v1/models", headers=KEY_A)).status_code == 200

        denied = await c.get("/v1/models", headers=KEY_A)
        assert denied.status_code == 429
        assert denied.headers["Retry-After"].isdigit()
        retry = int(denied.headers["Retry-After"])
        assert 1 <= retry <= RATE_WINDOW_SEC

        body = denied.json()
        assert body["error"]["type"] == "rate_limit_error"
        assert body["error"]["code"] == "key_rate_limited"
        assert body["detail"]["limit"] == 2
        assert body["detail"]["reset_after"] == retry
        # 错误体不能带出凭据与内部地址
        assert "u2a_key_alpha" not in denied.text
        assert "127.0.0.1" not in denied.text


async def test_key_rpm_limit_uses_anthropic_shell():
    async with _client(_app(RateLimitConfig(rpm=1))) as c:
        assert (await c.post("/v1/messages", headers=KEY_B, json={})).status_code == 200

        denied = await c.post("/v1/messages", headers=KEY_B, json={})
        assert denied.status_code == 429
        assert denied.headers["Retry-After"].isdigit()

        body = denied.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "rate_limit_error"
        assert body["error"]["code"] == "key_rate_limited"
        assert "detail" not in body, "Anthropic 壳只给顶层 type + error"
        assert "u2a_key_beta" not in denied.text


async def test_keys_have_separate_rpm_buckets():
    async with _client(_app(RateLimitConfig(rpm=1))) as c:
        assert (await c.get("/v1/models", headers=KEY_A)).status_code == 200
        assert (await c.get("/v1/models", headers=KEY_A)).status_code == 429
        assert (await c.get("/v1/models", headers=KEY_B)).status_code == 200, "另一把 key 不能被连坐"


async def test_window_expiry_allows_new_requests():
    async with _client(_app(RateLimitConfig(rpm=1, window_sec=1))) as c:
        assert (await c.get("/v1/models", headers=KEY_A)).status_code == 200
        assert (await c.get("/v1/models", headers=KEY_A)).status_code == 429

        await asyncio.sleep(1.05)
        assert (await c.get("/v1/models", headers=KEY_A)).status_code == 200, "过窗必须重新放行"


async def test_probe_and_management_paths_are_not_limited():
    async with _client(_app(RateLimitConfig(rpm=1, max_concurrency=1))) as c:
        for _ in range(5):
            assert (await c.get("/healthz", headers=KEY_A)).status_code == 200
            assert (await c.get("/api/health", headers=KEY_A)).status_code == 200

        # 探活没有消耗密钥额度：第一次 /v1 仍放行，第二次才被拦
        assert (await c.get("/v1/models", headers=KEY_A)).status_code == 200
        assert (await c.get("/v1/models", headers=KEY_A)).status_code == 429


async def test_global_concurrency_rejects_with_503_and_releases():
    started = asyncio.Event()
    release = asyncio.Event()

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, config=RateLimitConfig(max_concurrency=1))

    @app.get("/v1/slow")
    async def slow():
        started.set()
        await release.wait()
        return {"ok": True}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    async with _client(app) as c:
        first = asyncio.create_task(c.get("/v1/slow"))
        await asyncio.wait_for(started.wait(), 2)

        overloaded = await c.get("/v1/slow")
        assert overloaded.status_code == 503
        body = overloaded.json()
        assert body["error"]["type"] == "server_error"
        assert body["error"]["code"] == "gateway_overloaded"
        assert "Retry-After" not in overloaded.headers, "并发拒绝不是按时间重试"
        # 并发打满时探活仍要通
        assert (await c.get("/healthz")).status_code == 200

        release.set()
        assert (await first).status_code == 200
        # 名额随请求结束归还
        assert (await c.get("/v1/slow")).status_code == 200


async def test_streaming_holds_concurrency_until_body_finishes():
    started = asyncio.Event()
    release = asyncio.Event()

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, config=RateLimitConfig(max_concurrency=1))

    @app.get("/v1/stream")
    async def stream():
        async def chunks():
            yield b"first"
            started.set()
            await release.wait()
            yield b"second"

        return StreamingResponse(chunks(), media_type="text/plain")

    async with _client(app) as c:
        first = asyncio.create_task(c.get("/v1/stream"))
        await asyncio.wait_for(started.wait(), 2)

        second = await c.get("/v1/stream")
        assert second.status_code == 503, "流还没吐完就要继续占住并发名额"

        release.set()
        first_resp = await first
        assert first_resp.status_code == 200
        assert first_resp.text == "firstsecond"
