"""模型目录聚合：多 provider 并集、来源标注、needs_config 可见性与 TTL 缓存。"""

from __future__ import annotations

import httpx
import pytest

from app.adapters.base import ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from app.model_catalog import (
    SOURCE_DECLARED,
    SOURCE_DECLARED_FALLBACK,
    SOURCE_UPSTREAM,
    ModelCatalogCache,
    collect_models,
)


def _spec(
    pid: str,
    *,
    readiness: str = "ready",
    requires: tuple[str, ...] = (),
    base_url: str = "https://example.test",
) -> ProviderSpec:
    return ProviderSpec(
        id=pid,
        name=pid,
        vendor="test",
        license="MIT",
        runtime="test",
        status="implemented",
        description="test",
        default_base_url=base_url,
        capabilities=stub_capabilities(chat=True, models=True),
        readiness=readiness,
        requires=requires,
    )


class _DeclaredOnly(ProviderAdapter):
    def __init__(self, pid: str, models: list[dict], **spec_kw: object) -> None:
        self.spec = _spec(pid, **spec_kw)
        super().__init__()
        self.models = models

    def declared_models(self) -> list[dict]:
        return list(self.models)


class _UpstreamOnly(ProviderAdapter):
    def __init__(self, pid: str, models: list[dict], base_url: str = "https://example.test") -> None:
        self.spec = _spec(pid, base_url=base_url)
        super().__init__(base_url)
        self.models = models
        self.calls = 0

    async def fetch_models(self) -> list[dict]:
        self.calls += 1
        return list(self.models)


class _Fallback(_UpstreamOnly):
    def __init__(self, pid: str, declared: list[dict], error: str) -> None:
        super().__init__(pid, [])
        self.declared = declared
        self.error = error

    def declared_models(self) -> list[dict]:
        return list(self.declared)

    async def fetch_models(self) -> list[dict]:
        self.calls += 1
        raise ProviderError(503, self.error)


async def test_same_id_merges_across_providers() -> None:
    upstream = _UpstreamOnly("up-a", [{"id": "shared"}, {"id": "only-a"}])
    declared = _DeclaredOnly("decl-b", [{"id": "shared"}])

    models = await collect_models(
        [(upstream, True), (declared, True)], ttl_sec=0, cache=ModelCatalogCache()
    )

    shared = next(item for item in models if item["id"] == "shared")
    assert shared["providers"] == ["up-a", "decl-b"]
    assert shared["provider"] == "up-a"
    assert shared["source"] == SOURCE_UPSTREAM
    assert shared["provider_states"]["decl-b"]["source"] == SOURCE_DECLARED
    assert len(models) == 2


async def test_needs_config_entry_stays_visible_with_reason() -> None:
    declared = _DeclaredOnly(
        "needy", [{"id": "m1"}], readiness="needs_config", requires=("api_key",)
    )

    models = await collect_models([(declared, True)], ttl_sec=0, cache=ModelCatalogCache())

    assert models[0]["readiness"] == "needs_config"
    assert models[0]["available"] is False
    assert "api_key" in (models[0]["unavailable_reason"] or "")


async def test_disabled_provider_is_marked_unavailable() -> None:
    upstream = _UpstreamOnly("off", [{"id": "m1"}])

    models = await collect_models([(upstream, False)], ttl_sec=0, cache=ModelCatalogCache())

    assert models[0]["enabled"] is False
    assert models[0]["available"] is False
    assert models[0]["unavailable_reason"] == "provider 未启用"


async def test_declared_fallback_marks_source_and_redacts_reason() -> None:
    fallback = _Fallback(
        "flaky",
        [{"id": "fallback-model"}],
        "upstream https://user:pass@example.test 拒绝 sk-live-secret",
    )
    fallback.api_key = "sk-live-secret"

    models = await collect_models([(fallback, True)], ttl_sec=0, cache=ModelCatalogCache())

    assert models[0]["source"] == SOURCE_DECLARED_FALLBACK
    reason = models[0]["fallback_reason"] or ""
    assert reason
    assert "user:pass" not in reason
    assert "sk-live-secret" not in reason


async def test_cache_hit_skips_upstream_call() -> None:
    cache = ModelCatalogCache()
    upstream = _UpstreamOnly("cached", [{"id": "m1"}])

    first = await collect_models([(upstream, True)], ttl_sec=60, cache=cache)
    second = await collect_models([(upstream, True)], ttl_sec=60, cache=cache)

    assert upstream.calls == 1
    assert [item["id"] for item in first] == [item["id"] for item in second] == ["m1"]


async def test_cache_expires_and_credential_change_invalidates() -> None:
    cache = ModelCatalogCache()
    upstream = _UpstreamOnly("rotating", [{"id": "m1"}])

    await collect_models([(upstream, True)], ttl_sec=60, cache=cache)
    assert upstream.calls == 1

    upstream.api_key = "key-one"
    await collect_models([(upstream, True)], ttl_sec=60, cache=cache)
    upstream.api_key = "key-two"
    await collect_models([(upstream, True)], ttl_sec=60, cache=cache)
    assert upstream.calls == 3

    await collect_models([(upstream, True)], ttl_sec=0, cache=cache)
    assert upstream.calls == 4


async def test_base_url_change_invalidates_cache() -> None:
    cache = ModelCatalogCache()
    upstream = _UpstreamOnly("moving", [{"id": "m1"}], base_url="https://a.test")

    await collect_models([(upstream, True)], ttl_sec=60, cache=cache)
    upstream.base_url = "https://b.test"
    await collect_models([(upstream, True)], ttl_sec=60, cache=cache)

    assert upstream.calls == 2


def _enabled_map(admin: httpx.Client) -> dict[str, bool]:
    return {item["id"]: bool(item["enabled"]) for item in admin.get("/api/providers").json()}


def _toggle(admin: httpx.Client, pid: str, enabled: bool) -> None:
    response = admin.patch(f"/api/providers/{pid}", json={"enabled": enabled})
    assert response.status_code == 200, f"{response.status_code} {response.text}"


def test_api_models_keeps_backward_compatible_shape(admin: httpx.Client) -> None:
    body = admin.get("/api/models").json()

    assert {"models", "source", "count"} <= set(body)
    assert body["source"] in {"dynamic", "unknown"}
    assert body["count"] == len(body["models"])
    ids = [item["id"] for item in body["models"]]
    assert len(ids) == len(set(ids))
    for item in body["models"]:
        assert item["provider"] in item["providers"]
        assert item["source"] in {SOURCE_DECLARED, SOURCE_UPSTREAM, SOURCE_DECLARED_FALLBACK}


def test_api_models_provider_filter(admin: httpx.Client) -> None:
    before = _enabled_map(admin)
    try:
        _toggle(admin, "muse", True)
        body = admin.get("/api/models", params={"provider": "muse"}).json()

        assert body["models"], "muse 有静态声明，过滤后不应为空"
        assert all(item["provider"] == "muse" for item in body["models"])
        assert all("muse" in item["providers"] for item in body["models"])

        missing = admin.get("/api/models", params={"provider": "nope"})
        assert missing.status_code == 404
    finally:
        _toggle(admin, "muse", before.get("muse", False))


def test_api_models_needs_config_not_hidden(admin: httpx.Client) -> None:
    before = _enabled_map(admin)
    try:
        _toggle(admin, "zcode", True)
        body = admin.get("/api/models").json()

        zcode_entries = [item for item in body["models"] if "zcode" in item["providers"]]
        assert zcode_entries, "needs_config 的 provider 模型也应返回"
        for item in zcode_entries:
            assert item["readiness"] == "needs_config"
            assert item["available"] is False
            assert item["unavailable_reason"]
            assert item["source"] == SOURCE_DECLARED
    finally:
        _toggle(admin, "zcode", before.get("zcode", False))


def test_api_models_declared_fallback(admin: httpx.Client) -> None:
    before = _enabled_map(admin)
    try:
        _toggle(admin, "muse", True)
        body = admin.get("/api/models", params={"provider": "muse"}).json()

        assert body["models"]
        for item in body["models"]:
            assert item["source"] == SOURCE_DECLARED_FALLBACK
            assert item["fallback_reason"], "回落声明必须带失败原因"
    finally:
        _toggle(admin, "muse", before.get("muse", False))


def test_api_models_upstream_source(admin: httpx.Client) -> None:
    before = _enabled_map(admin)
    try:
        if not before.get("codebuddy"):
            _toggle(admin, "codebuddy", True)
        body = admin.get("/api/models", params={"provider": "codebuddy"}).json()

        assert body["models"], "假上游应返回模型"
        assert all(item["source"] == SOURCE_UPSTREAM for item in body["models"])
    finally:
        if not before.get("codebuddy"):
            _toggle(admin, "codebuddy", False)
