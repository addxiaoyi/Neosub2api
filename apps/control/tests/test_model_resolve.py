"""model 不带 provider 前缀时的解析：唯一命中、歧义、缺失、needs_config 提示、前缀优先级、三协议一致。"""

from __future__ import annotations

import logging
import os
import subprocess
import time

import httpx
import pytest

from app.adapters.base import ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from app.db import Database
from app.model_resolve import (
    MODE_CATALOG,
    MODE_FALLBACK,
    MODEL_NOT_FOUND,
    ModelResolutionError,
    resolve_model,
)
from tests.conftest import CONTROL, FAKE_PORT, PY, _port_free, _wait_up

pytestmark = pytest.mark.gateway

CATALOG_MODEL = "fake-chat-1"
FALLBACK_PORT = int(os.getenv("UNIFIED_TEST_FALLBACK_PORT", "8062"))


@pytest.fixture(scope="module")
def gateway_key(admin: httpx.Client) -> str:
    r = admin.post("/api/keys", json={"name": "model-resolve-suite"})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()["key"]


def _post(anon: httpx.Client, path: str, token: str, model: str, **extra: object) -> httpx.Response:
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}
    return anon.post(path, headers={"authorization": f"Bearer {token}"}, json=body)


def _enabled_map(admin: httpx.Client) -> dict[str, bool]:
    return {item["id"]: bool(item["enabled"]) for item in admin.get("/api/providers").json()}


def _toggle(admin: httpx.Client, pid: str, enabled: bool) -> None:
    r = admin.patch(f"/api/providers/{pid}", json={"enabled": enabled})
    assert r.status_code == 200, f"{pid} enabled={enabled}: {r.status_code} {r.text}"


def _assert_no_internal_leak(text: str) -> None:
    for needle in ("127.0.0.1", "http://", "https://", "u2a_", "sk-"):
        assert needle not in text, f"错误体泄露了内部信息：{needle} / {text}"


def test_unique_match_routes_and_lands_in_request_log(
    admin: httpx.Client, anon: httpx.Client, gateway_key: str
) -> None:
    r = _post(anon, "/v1/chat/completions", gateway_key, CATALOG_MODEL)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    content = r.json()["choices"][0]["message"]["content"]
    assert CATALOG_MODEL in content, f"上游应收到原模型名：{content}"

    time.sleep(0.2)  # 旁路日志在控制面进程里异步落库
    rows = admin.get("/api/logs", params={"model": CATALOG_MODEL}).json()
    assert rows["total"] >= 1
    assert any(row["provider"] == "codebuddy" and row["model"] == CATALOG_MODEL for row in rows["items"])


def test_ambiguous_model_returns_candidates(
    admin: httpx.Client, anon: httpx.Client, gateway_key: str
) -> None:
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "opencode")
    added_key = not before.get("has_api_key")
    try:
        # opencode 走 env 的假上游地址；给它一把临时 key，目录里同样出现 fake-chat-1
        admin.patch("/api/providers/opencode", json={"enabled": True, "api_key": "fake-key"})
        r = _post(anon, "/v1/chat/completions", gateway_key, CATALOG_MODEL)
        assert r.status_code == 400, f"{r.status_code} {r.text}"
        error = r.json()["error"]
        assert error["type"] == "ambiguous_model"
        assert error["code"] == "ambiguous_model"
        candidates = r.json()["detail"]["candidates"]
        names = {item["provider"] for item in candidates}
        assert {"codebuddy", "opencode"} <= names
        assert all(isinstance(item.get("readiness"), str) for item in candidates)
        _assert_no_internal_leak(r.text)

        anthropic = _post(anon, "/v1/messages", gateway_key, CATALOG_MODEL)
        assert anthropic.status_code == 400, f"{anthropic.status_code} {anthropic.text}"
        assert anthropic.json()["type"] == "error"
        inner = anthropic.json()["error"]
        assert inner["type"] == "invalid_request_error"
        assert inner["code"] == "ambiguous_model"
        assert {"codebuddy", "opencode"} <= {item["provider"] for item in inner["candidates"]}
        _assert_no_internal_leak(anthropic.text)
    finally:
        restore = {"enabled": bool(before.get("enabled"))}
        if added_key:
            restore["api_key"] = ""
        r = admin.patch("/api/providers/opencode", json=restore)
        assert r.status_code == 200, f"{r.status_code} {r.text}"


def test_unknown_model_returns_structured_404(anon: httpx.Client, gateway_key: str) -> None:
    r = _post(anon, "/v1/chat/completions", gateway_key, "definitely-not-a-model")
    assert r.status_code == 404, f"{r.status_code} {r.text}"
    body = r.json()
    assert body["error"]["type"] == "model_not_found"
    assert body["error"]["code"] == "model_not_found"
    assert body["detail"]["model"] == "definitely-not-a-model"
    assert body["detail"]["candidates"] == []
    _assert_no_internal_leak(r.text)


def test_needs_config_model_reports_exists_but_unavailable(
    admin: httpx.Client, anon: httpx.Client, gateway_key: str
) -> None:
    before = _enabled_map(admin)
    try:
        _toggle(admin, "zcode", False)  # GLM-5.3 只存在于 zcode 的静态声明
        r = _post(anon, "/v1/chat/completions", gateway_key, "GLM-5.3")
        assert r.status_code == 404, f"{r.status_code} {r.text}"
        body = r.json()
        assert body["error"]["type"] == "model_unavailable"
        assert body["error"]["code"] == "model_unavailable"
        zcode = next(item for item in body["detail"]["candidates"] if item["provider"] == "zcode")
        assert zcode["readiness"] == "needs_config"
        assert zcode["enabled"] is False
        assert "未启用" in zcode["reason"]
        assert any("official_system" in need for need in zcode["requires"]), "原因要能指向具体缺口"
        assert "已存在但当前不可用" in body["error"]["message"]
        _assert_no_internal_leak(r.text)
    finally:
        _toggle(admin, "zcode", before.get("zcode", False))


def test_prefixed_model_keeps_working_and_wins_over_catalog(
    anon: httpx.Client, gateway_key: str
) -> None:
    # GLM-5.3 一家都没有（zcode 未启用）；显式前缀必须绕过自动解析
    r = _post(anon, "/v1/chat/completions", gateway_key, "codebuddy/GLM-5.3")
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    assert "GLM-5.3" in r.json()["choices"][0]["message"]["content"]

    # 前缀在目录里也不存在的模型名，旧写法同样要能透传
    r = _post(anon, "/v1/chat/completions", gateway_key, "codebuddy/unknown-model-xyz")
    assert r.status_code == 200, f"{r.status_code} {r.text}"


def test_three_protocols_resolve_the_same_provider(anon: httpx.Client, gateway_key: str) -> None:
    chat = _post(anon, "/v1/chat/completions", gateway_key, CATALOG_MODEL)
    assert chat.status_code == 200, f"{chat.status_code} {chat.text}"

    messages = _post(anon, "/v1/messages", gateway_key, CATALOG_MODEL)
    assert messages.status_code == 200, f"{messages.status_code} {messages.text}"

    # codebuddy 没声明 responses：解析照样命中，能力位在统一的 501 门槛上拦下
    responses = _post(anon, "/v1/responses", gateway_key, CATALOG_MODEL)
    assert responses.status_code == 501, f"{responses.status_code} {responses.text}"
    assert responses.json()["error"]["provider"] == "codebuddy"
    assert responses.json()["error"]["code"] == "capability_unsupported"


def test_404_shell_follows_each_protocol(anon: httpx.Client, gateway_key: str) -> None:
    for path in ("/v1/chat/completions", "/v1/responses"):
        r = _post(anon, path, gateway_key, "no-such-model")
        assert r.status_code == 404, f"{path}: {r.status_code} {r.text}"
        assert r.json()["error"]["type"] == "model_not_found", path
        assert r.json()["detail"]["model"] == "no-such-model", path

    r = _post(anon, "/v1/messages", gateway_key, "no-such-model")
    assert r.status_code == 404, f"{r.status_code} {r.text}"
    body = r.json()
    assert set(body) == {"type", "error"}
    assert body["type"] == "error"
    assert body["error"]["type"] == "not_found_error"
    assert body["error"]["code"] == "model_not_found"


# --- 兜底开关：解析层单测（开关状态由 env 决定，集成实例无法运行时切换，故在此直连 resolve_model） ---


class _StubAdapter(ProviderAdapter):
    def __init__(
        self,
        pid: str,
        *,
        chat: bool = True,
        models: bool = True,
        declared: tuple[str, ...] = (),
    ) -> None:
        self.spec = ProviderSpec(
            id=pid,
            name=pid,
            vendor="test",
            license="MIT",
            runtime="in-process",
            status="stub",
            description="",
            default_base_url="http://127.0.0.1:1",
            capabilities=stub_capabilities(chat=chat, models=models),
        )
        super().__init__()
        self._declared = declared

    def declared_models(self) -> list[dict[str, object]]:
        return [{"id": model} for model in self._declared]


class _StubRegistry:
    def __init__(self, db: Database, adapters: list[ProviderAdapter]) -> None:
        self.db = db
        self._adapters = list(adapters)
        self._by_id = {adapter.spec.id: adapter for adapter in adapters}

    def enabled(self) -> list[ProviderAdapter]:
        return list(self._adapters)

    def get(self, pid: str) -> ProviderAdapter:
        adapter = self._by_id.get(pid)
        if adapter is None:
            raise ProviderError(404, f"未知的 provider：{pid}")
        return adapter

    def list_public(self) -> list[dict[str, object]]:
        return []


@pytest.fixture
def stub_db(tmp_path) -> Database:
    return Database(tmp_path / "stub.db")


async def test_fallback_off_unknown_model_is_404(stub_db: Database) -> None:
    registry = _StubRegistry(stub_db, [_StubAdapter("alpha")])
    with pytest.raises(ModelResolutionError) as caught:
        await resolve_model(registry, "mystery-model")
    assert caught.value.status_code == 404
    assert caught.value.error["code"] == MODEL_NOT_FOUND


async def test_fallback_on_routes_first_capable_provider(stub_db: Database) -> None:
    registry = _StubRegistry(stub_db, [_StubAdapter("alpha"), _StubAdapter("beta", chat=False)])
    adapter, effective, mode = await resolve_model(registry, "mystery-model", fallback_first=True)
    assert mode == MODE_FALLBACK
    assert adapter.spec.id == "alpha"
    assert effective == "mystery-model"


async def test_fallback_on_keeps_ambiguous_400(stub_db: Database) -> None:
    registry = _StubRegistry(
        stub_db,
        [
            _StubAdapter("alpha", declared=("shared-model",)),
            _StubAdapter("beta", declared=("shared-model",)),
        ],
    )
    with pytest.raises(ModelResolutionError) as caught:
        await resolve_model(registry, "shared-model", fallback_first=True)
    assert caught.value.status_code == 400
    assert caught.value.error["code"] == "ambiguous_model"


async def test_fallback_log_carries_request_id_and_candidates(stub_db: Database, caplog) -> None:
    registry = _StubRegistry(stub_db, [_StubAdapter("alpha"), _StubAdapter("beta")])
    with caplog.at_level(logging.INFO, logger="unified.model_resolve"):
        adapter, _, mode = await resolve_model(
            registry, "mystery-model", fallback_first=True, request_id="req-123"
        )
    assert mode == MODE_FALLBACK
    assert adapter.spec.id == "alpha"
    line = next(r.getMessage() for r in caplog.records if "unified.model_fallback" in r.getMessage())
    assert "request_id=req-123" in line
    assert "model=mystery-model" in line
    assert "provider=alpha" in line
    assert "candidates=2" in line


async def test_fallback_without_capable_provider_keeps_404(stub_db: Database) -> None:
    registry = _StubRegistry(stub_db, [_StubAdapter("alpha", chat=False)])
    with pytest.raises(ModelResolutionError) as caught:
        await resolve_model(registry, "mystery-model", fallback_first=True)
    assert caught.value.status_code == 404
    assert caught.value.error["code"] == MODEL_NOT_FOUND


async def test_catalog_entry_without_capability_still_hits_501_gate(stub_db: Database) -> None:
    # 目录里确实有这个名字，但没有启用 provider 支持 chat → 交给 v1 的 501 能力位门槛
    registry = _StubRegistry(stub_db, [_StubAdapter("alpha", chat=False, declared=("declared-model",))])
    adapter, effective, mode = await resolve_model(
        registry, "declared-model", capability="chat", fallback_first=True
    )
    assert mode == MODE_CATALOG
    assert effective == "declared-model"
    assert adapter.spec.capabilities["chat"] is False


# --- 兜底开关：独立控制面实例端到端验证（开关只能通过 env 在启动时生效） ---


@pytest.fixture(scope="module")
def fallback_gateway(_bring_up_services, tmp_path_factory):
    if not _port_free(FALLBACK_PORT):
        pytest.skip(f"端口 {FALLBACK_PORT} 已被占用")
    data_dir = tmp_path_factory.mktemp("fallback-data")
    env = os.environ.copy()
    env.update(
        {
            "UNIFIED_DATA_DIR": str(data_dir),
            "UNIFIED_DB_PATH": str(data_dir / "unified.db"),
            "UNIFIED_ADMIN_USER": "admin",
            "UNIFIED_ADMIN_PASSWORD": "test",
            "CODEBUDDY_BASE": f"http://127.0.0.1:{FAKE_PORT}",
            "UNIFIED_WEB_DIR": str(data_dir / "no-web"),
            "UNIFIED_ALLOW_ANONYMOUS_GATEWAY": "false",
            "UNIFIED_ALLOW_PRIVATE_UPSTREAMS": "1",
            "GROK_REGISTER_EXECUTOR_URL": "",
            "UNIFIED_MODEL_FALLBACK_FIRST": "1",
        }
    )
    proc = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(FALLBACK_PORT), "--log-level", "warning"],
        cwd=str(CONTROL),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_up(f"http://127.0.0.1:{FALLBACK_PORT}/healthz"):
            pytest.skip("兜底实例启动失败")
        yield f"http://127.0.0.1:{FALLBACK_PORT}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            proc.wait(timeout=2)


def test_fallback_on_runs_unknown_model_end_to_end(fallback_gateway: str) -> None:
    with httpx.Client(base_url=fallback_gateway, trust_env=False, timeout=10.0) as client:
        login = client.post("/api/login", json={"username": "admin", "password": "test"})
        assert login.status_code == 200, f"{login.status_code} {login.text}"
        key = client.post("/api/keys", json={"name": "fallback-suite"}).json()["key"]

        model = "never-in-any-catalog"
        r = _post(client, "/v1/chat/completions", key, model)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        assert model in r.json()["choices"][0]["message"]["content"], "兜底后应把原模型名透传给上游"

        time.sleep(0.2)  # 旁路日志异步落库
        rows = client.get("/api/logs", params={"model": model}).json()
        assert rows["total"] >= 1
        # 日志写真实承接的 provider，不写「兜底」这种虚拟值
        assert any(row["provider"] == "codebuddy" and row["model"] == model for row in rows["items"])
