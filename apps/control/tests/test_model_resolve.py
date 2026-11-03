"""model 不带 provider 前缀时的解析：唯一命中、歧义、缺失、needs_config 提示、前缀优先级、三协议一致。"""

from __future__ import annotations

import time

import httpx
import pytest

pytestmark = pytest.mark.gateway

CATALOG_MODEL = "fake-chat-1"


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
