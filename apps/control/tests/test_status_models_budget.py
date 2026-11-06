"""P1 回归：/api/status 与 /api/models 必须在上游卡死时按预算返回部分结果。"""

from __future__ import annotations

import asyncio
import time
import types

from app.model_catalog import SOURCE_DECLARED_FALLBACK, collect_models
from app.routers.models import STATUS_BUDGET_SEC, upstream_status


class FakeSpec:
    def __init__(self, pid: str, name: str, capabilities: dict | None = None):
        self.id = pid
        self.name = name
        self.readiness = "ready"
        self.requires: list[str] = []
        self.limitations: dict = {}
        self.capabilities = capabilities or {}


class FakeAdapter:
    def __init__(self, pid: str, name: str, *, delay: float = 0, total: int = 0):
        self.spec = FakeSpec(pid, name, {"models": True})
        self.delay = delay
        self.total = total
        self.api_key = ""
        self.api_keys: list[str] = []
        self.options: dict = {}
        self.base_url = "http://127.0.0.1:1"

    async def health(self) -> dict:
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"connected": True, "total": self.total, "healthy": self.total}

    async def fetch_models(self) -> list[dict]:
        await asyncio.sleep(30)
        return [{"id": "late-model"}]

    def declared_models(self) -> list[dict]:
        return [{"id": "muse-spark"}]


class FakeRegistry:
    def __init__(self, adapters: list[FakeAdapter]):
        self._adapters = adapters

    def enabled(self) -> list[FakeAdapter]:
        return list(self._adapters)


def fake_request(adapters: list[FakeAdapter]):
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(registry=FakeRegistry(adapters)))
    )


async def test_status_returns_partial_within_budget():
    # 一个上游永久卡住、一个正常：整体必须在预算内返回，卡住的那家标成不可用
    hung = FakeAdapter("muse", "Muse2API", delay=30)
    fast = FakeAdapter("codebuddy", "CodeBuddy", total=7)
    started = time.monotonic()
    out = await upstream_status(fake_request([hung, fast]), user={"sub": "admin"})
    elapsed = time.monotonic() - started

    assert elapsed < STATUS_BUDGET_SEC + 1.0
    assert out["connected"] is True and out["total"] == 7
    assert out["partial"] is True
    assert [item["id"] for item in out["unavailable"]] == ["muse"]
    assert "Muse2API" in out["error"]
    assert [item["id"] for item in out["items"]] == ["muse", "codebuddy"]


async def test_models_falls_back_when_provider_hangs():
    adapter = FakeAdapter("muse", "Muse2API", delay=30)
    started = time.monotonic()
    rows = await collect_models([(adapter, True)], ttl_sec=0, timeout_sec=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert rows[0]["id"] == "muse-spark"
    assert rows[0]["provider_states"]["muse"]["source"] == SOURCE_DECLARED_FALLBACK
