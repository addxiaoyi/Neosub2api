"""错误契约全量一致性：状态码 × 端点族 × 错误壳。

端点族与约定外壳（见 docs/error-contract.md、docs/API-REFERENCE.md）：
- /v1/chat/completions、/v1/responses → OpenAI：{"error": {...}, "detail": ...}
- /v1/messages → Anthropic：{"type": "error", "error": {...}}，没有顶层 detail
- /api/* → FastAPI：{"detail": ...}；403 额外带结构化 error；配额/限流 detail 是对象
- 探针 → 自有 health JSON，不走错误壳

每个断言固定三件事：外层形状、必填字段类型、敏感信息不泄漏。
"""

from __future__ import annotations

import sqlite3
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.gateway

V1_CASES = [
    ("/v1/chat/completions", "openai"),
    ("/v1/responses", "openai"),
    ("/v1/messages", "anthropic"),
]
V1_IDS = [path for path, _ in V1_CASES]

# 错误体不能出现：上游地址、本机地址、密钥片段、库路径、内部堆栈
LEAK_NEEDLES = (
    "http://",
    "https://",
    "127.0.0.1",
    "u2a_",
    "sk-",
    "Traceback (most recent call last)",
    "sqlite3.",
    ".db",
)


def _payload(path: str, model: str) -> dict:
    if path.endswith("/responses"):
        return {"model": model, "input": "hi"}
    if path.endswith("/messages"):
        return {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
    return {"model": model, "messages": [{"role": "user", "content": "hi"}]}


def _post(
    client: httpx.Client,
    path: str,
    *,
    model: str = "fake-chat-1",
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> httpx.Response:
    h = dict(headers or {})
    if raw is not None:
        h.setdefault("content-type", "application/json")
        return client.post(path, headers=h, content=raw)
    return client.post(path, headers=h, json=_payload(path, model))


def _bearer(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


def _assert_no_leak(resp: httpx.Response) -> None:
    text = resp.text
    for needle in LEAK_NEEDLES:
        assert needle not in text, f"错误体泄露内部信息「{needle}」：{text[:400]}"


def _assert_openai_shell(
    resp: httpx.Response,
    status: int,
    *,
    code: str | None = None,
    type_: str | None = None,
    detail_present: bool = True,
) -> dict:
    assert resp.status_code == status, f"期望 {status}，实际 {resp.status_code}：{resp.text[:400]}"
    body = resp.json()
    error = body.get("error")
    assert isinstance(error, dict), f"OpenAI 壳缺少 error 对象：{body}"
    for field in ("message", "type", "code"):
        assert isinstance(error.get(field), str) and error[field], f"error.{field} 缺失或类型不对：{error}"
    assert "param" in error, f"error.param 必须存在（可为 null）：{error}"
    if detail_present:
        assert "detail" in body, f"OpenAI 壳必须保留兼容字段 detail：{body}"
    assert "type" not in body, "OpenAI 壳不应出现顶层 type=error"
    if code is not None:
        assert error["code"] == code, f"error.code 期望 {code}，实际 {error['code']}"
    if type_ is not None:
        assert error["type"] == type_, f"error.type 期望 {type_}，实际 {error['type']}"
    _assert_no_leak(resp)
    return body


def _assert_anthropic_shell(
    resp: httpx.Response,
    status: int,
    *,
    inner_type: str | None = None,
) -> dict:
    assert resp.status_code == status, f"期望 {status}，实际 {resp.status_code}：{resp.text[:400]}"
    body = resp.json()
    assert set(body) == {"type", "error"}, f"Anthropic 壳只认 type + error：{body}"
    assert body["type"] == "error", f"顶层 type 必须是 error：{body}"
    error = body["error"]
    assert isinstance(error, dict), f"error 必须是对象：{body}"
    assert isinstance(error.get("type"), str) and error["type"], f"error.type 缺失：{error}"
    assert isinstance(error.get("message"), str) and error["message"], f"error.message 缺失：{error}"
    assert "detail" not in body, "Anthropic 壳不应带 OpenAI 的 detail"
    if inner_type is not None:
        assert error["type"] == inner_type, f"error.type 期望 {inner_type}，实际 {error['type']}"
    _assert_no_leak(resp)
    return body


def _assert_detail_shell(resp: httpx.Response, status: int, *, structured_403: bool = False) -> dict:
    assert resp.status_code == status, f"期望 {status}，实际 {resp.status_code}：{resp.text[:400]}"
    body = resp.json()
    assert "detail" in body, f"/api/* 必须使用 detail 壳：{body}"
    if not structured_403:
        assert "error" not in body, f"/api/* 普通错误不应出现 error 对象：{body}"
    _assert_no_leak(resp)
    return body


def _new_key(admin: httpx.Client, name: str) -> dict:
    r = admin.post("/api/keys", json={"name": name})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    return {"id": int(body["id"]), "key": str(body["key"])}


@pytest.fixture(scope="module")
def contract_key(admin: httpx.Client) -> dict:
    return _new_key(admin, f"error-contract-{uuid.uuid4().hex[:8]}")


@pytest.fixture(scope="module")
def viewer(admin: httpx.Client) -> httpx.Client:
    name = f"contract-viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post("/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"})
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return client


def _providers(admin: httpx.Client) -> dict[str, dict]:
    return {item["id"]: item for item in admin.get("/api/providers").json()}


@pytest.fixture
def ambiguous_models(admin: httpx.Client):
    """把 opencode 临时指到假上游并启用：fake-chat-1 同时命中两家，触发 400。"""
    before = _providers(admin)["opencode"]
    added_key = not before.get("has_api_key")
    patched = admin.patch(
        "/api/providers/opencode", json={"enabled": True, "api_key": "fake-key"}
    )
    assert patched.status_code == 200, patched.text
    try:
        yield
    finally:
        restore: dict[str, object] = {"enabled": bool(before.get("enabled"))}
        restore["api_key"] = "" if added_key else "***"
        r = admin.patch("/api/providers/opencode", json=restore)
        assert r.status_code == 200, f"{r.status_code} {r.text}"


@pytest.fixture
def codar_on_fake(admin: httpx.Client, fake_base: str):
    """responses 只有 codar/qoder 声明能力位；临时把 codar 指到假上游验证 5xx/503。"""
    before = _providers(admin)["codar"]
    patched = admin.patch(
        "/api/providers/codar",
        json={"enabled": True, "base_url": fake_base, "api_key": "fake-key"},
    )
    assert patched.status_code == 200, patched.text
    try:
        yield
    finally:
        restore: dict[str, object] = {"enabled": bool(before.get("enabled"))}
        restore["base_url"] = before["base_url"] if before.get("base_url_source") == "db" else ""
        if not before.get("has_api_key"):
            restore["api_key"] = ""
        r = admin.patch("/api/providers/codar", json=restore)
        assert r.status_code == 200, f"{r.status_code} {r.text}"


@pytest.fixture
def providers_off(admin: httpx.Client):
    items = admin.get("/api/providers").json()
    enabled = [item["id"] for item in items if item["enabled"]]
    for pid in enabled:
        r = admin.patch(f"/api/providers/{pid}", json={"enabled": False})
        assert r.status_code == 200, f"{pid}: {r.status_code} {r.text}"
    try:
        yield
    finally:
        for pid in enabled:
            r = admin.patch(f"/api/providers/{pid}", json={"enabled": True})
            assert r.status_code == 200, f"{pid} 恢复失败：{r.status_code} {r.text}"


@pytest.fixture
def trae_sidecar_on(admin: httpx.Client):
    """trae_sidecar 默认停用；能力门槛用例要先启用它，才能验证「声明不支持」而不是「已停用」。"""
    before = _providers(admin)["trae_sidecar"]
    patched = admin.patch("/api/providers/trae_sidecar", json={"enabled": True})
    assert patched.status_code == 200, patched.text
    try:
        yield
    finally:
        r = admin.patch("/api/providers/trae_sidecar", json={"enabled": bool(before.get("enabled"))})
        assert r.status_code == 200, f"{r.status_code} {r.text}"


@pytest.fixture
def trae_sidecar_off(admin: httpx.Client):
    """确保 trae_sidecar 处于停用状态：显式指定停用 provider 必须被拒。"""
    before = _providers(admin)["trae_sidecar"]
    if before.get("enabled"):
        r = admin.patch("/api/providers/trae_sidecar", json={"enabled": False})
        assert r.status_code == 200, r.text
    try:
        yield
    finally:
        if before.get("enabled"):
            r = admin.patch("/api/providers/trae_sidecar", json={"enabled": True})
            assert r.status_code == 200, f"{r.status_code} {r.text}"


# ---------------- 401 ----------------


@pytest.mark.parametrize("path,shell", V1_CASES, ids=V1_IDS)
def test_v1_missing_key_401(anon: httpx.Client, path: str, shell: str) -> None:
    r = _post(anon, path, model="m")
    if shell == "anthropic":
        body = _assert_anthropic_shell(r, 401, inner_type="authentication_error")
        assert "x-api-key" in body["error"]["message"]
    else:
        body = _assert_openai_shell(r, 401, code="invalid_api_key", type_="authentication_error")
        assert "x-api-key" in body["error"]["message"]
    assert r.headers.get("retry-after") is None


@pytest.mark.parametrize("path,shell", V1_CASES, ids=V1_IDS)
def test_v1_invalid_key_401(anon: httpx.Client, path: str, shell: str) -> None:
    bogus = "u2a_contract_bogus_key_value"
    r = _post(anon, path, headers=_bearer(bogus), model="m")
    if shell == "anthropic":
        _assert_anthropic_shell(r, 401, inner_type="authentication_error")
    else:
        _assert_openai_shell(r, 401, code="invalid_api_key", type_="authentication_error")
    assert bogus not in r.text, "错误体不能回显请求里的密钥"
    assert r.headers.get("retry-after") is None


@pytest.mark.parametrize("path,shell", V1_CASES, ids=V1_IDS)
def test_v1_conflicting_auth_headers_401(anon: httpx.Client, path: str, shell: str) -> None:
    left, right = "u2a_contract_left_value", "u2a_contract_right_value"
    r = _post(anon, path, headers={"authorization": f"Bearer {left}", "x-api-key": right}, model="m")
    if shell == "anthropic":
        body = _assert_anthropic_shell(r, 401, inner_type="authentication_error")
        assert "冲突" in body["error"]["message"]
    else:
        body = _assert_openai_shell(r, 401, code="conflicting_auth_headers", type_="authentication_error")
        assert "冲突" in body["error"]["message"]
    assert left not in r.text and right not in r.text
    assert r.headers.get("retry-after") is None


def test_api_401_uses_detail_shell(base_url: str) -> None:
    with httpx.Client(base_url=base_url, trust_env=False, timeout=10.0) as fresh:
        r = fresh.get("/api/settings/upstream")
        body = _assert_detail_shell(r, 401)
        assert isinstance(body["detail"], str) and body["detail"]
        assert r.headers.get("retry-after") is None


# ---------------- 403 ----------------


def test_api_403_has_structured_permission_denied(viewer: httpx.Client) -> None:
    r = viewer.get("/api/settings/upstream")
    body = _assert_detail_shell(r, 403, structured_403=True)
    assert body["detail"] == "需要管理员权限"
    error = body["error"]
    assert error["type"] == "permission_denied"
    assert error["code"] == "insufficient_role"
    assert error["required_role"] == "admin"
    assert error["role"] == "viewer"
    assert isinstance(error["message"], str) and error["message"]
    assert r.headers.get("retry-after") is None


# ---------------- 404 ----------------


@pytest.mark.parametrize("path,shell", V1_CASES, ids=V1_IDS)
def test_v1_404_unknown_model(anon: httpx.Client, contract_key: dict, path: str, shell: str) -> None:
    model = f"no-such-model-{uuid.uuid4().hex[:8]}"
    r = _post(anon, path, headers=_bearer(contract_key["key"]), model=model)
    if shell == "anthropic":
        body = _assert_anthropic_shell(r, 404, inner_type="not_found_error")
        assert body["error"]["code"] == "model_not_found"
    else:
        body = _assert_openai_shell(r, 404, code="model_not_found", type_="model_not_found")
        assert body["detail"]["model"] == model
        assert body["detail"]["candidates"] == []
    assert r.headers.get("retry-after") is None


@pytest.mark.parametrize(
    "path,shell",
    [
        ("/v1/no-such-endpoint", "openai"),
        ("/v1/responses/no-such-endpoint", "openai"),
        ("/v1/messages/no-such-endpoint", "anthropic"),
    ],
    ids=["chat-family", "responses", "messages"],
)
def test_v1_unknown_path_follows_protocol_shell(anon: httpx.Client, path: str, shell: str) -> None:
    """契约：/v1/* 的未匹配路由按所属协议出壳，不能落到 FastAPI 默认 {detail}。"""
    r = anon.post(path, json={"model": "m"})
    if shell == "anthropic":
        body = _assert_anthropic_shell(r, 404, inner_type="not_found_error")
        assert body["error"]["code"] == "route_not_found"
    else:
        body = _assert_openai_shell(r, 404, code="route_not_found", type_="invalid_request_error")
        assert body["detail"] == body["error"]["message"]
    assert r.headers.get("retry-after") is None


@pytest.mark.parametrize("path,shell", V1_CASES, ids=V1_IDS)
def test_v1_method_not_allowed_follows_protocol_shell(
    anon: httpx.Client, path: str, shell: str
) -> None:
    """契约：/v1/* 的 405 同样按协议出壳，并保留 Allow 头。"""
    r = anon.get(path)
    if shell == "anthropic":
        body = _assert_anthropic_shell(r, 405, inner_type="invalid_request_error")
        assert body["error"]["code"] == "method_not_allowed"
    else:
        body = _assert_openai_shell(r, 405, code="method_not_allowed", type_="invalid_request_error")
        assert body["detail"] == body["error"]["message"]
    assert r.headers.get("allow") == "POST", "405 必须告诉调用方允许的方法"
    assert r.headers.get("retry-after") is None


@pytest.mark.parametrize(
    "path",
    ["/api/tasks", "/api/tasks/42/cancel", "/api/no-such-endpoint"],
    ids=["removed-tasks", "removed-tasks-action", "unknown-api-path"],
)
def test_api_404_unknown_and_removed_paths(anon: httpx.Client, path: str) -> None:
    r = anon.get(path)
    body = _assert_detail_shell(r, 404)
    assert isinstance(body["detail"], str) and body["detail"]
    assert r.headers.get("retry-after") is None


def test_api_404_unknown_provider(admin: httpx.Client) -> None:
    r = admin.get("/api/models", params={"provider": f"nope-{uuid.uuid4().hex[:6]}"})
    body = _assert_detail_shell(r, 404)
    assert isinstance(body["detail"], str) and body["detail"]


# ---------------- 400 ----------------


def test_api_400_parameter_validation_detail_shell(admin: httpx.Client) -> None:
    r = admin.get("/api/logs", params={"status": "bogus"})
    body = _assert_detail_shell(r, 400)
    assert "status" in body["detail"]
    assert r.headers.get("retry-after") is None


@pytest.mark.parametrize("path,shell", V1_CASES, ids=V1_IDS)
def test_v1_400_missing_model(
    anon: httpx.Client, contract_key: dict, path: str, shell: str
) -> None:
    r = anon.post(path, headers=_bearer(contract_key["key"]), json={"messages": []})
    if shell == "anthropic":
        body = _assert_anthropic_shell(r, 400, inner_type="invalid_request_error")
        assert "model" in body["error"]["message"]
    else:
        body = _assert_openai_shell(r, 400, code="missing_model", type_="invalid_request_error")
        assert "model" in body["error"]["message"]
    assert r.headers.get("retry-after") is None


@pytest.mark.parametrize("path,shell", V1_CASES, ids=V1_IDS)
def test_v1_400_invalid_json(
    anon: httpx.Client, contract_key: dict, path: str, shell: str
) -> None:
    r = _post(anon, path, headers=_bearer(contract_key["key"]), raw=b'{"model": ')
    if shell == "anthropic":
        _assert_anthropic_shell(r, 400, inner_type="invalid_request_error")
    else:
        _assert_openai_shell(r, 400, code="invalid_json", type_="invalid_request_error")
    assert r.headers.get("retry-after") is None


def test_v1_chat_400_ambiguous_model(
    anon: httpx.Client, contract_key: dict, ambiguous_models: None
) -> None:
    """responses 没有候选能力位，先撞 501 门槛，歧义只在 chat/messages 上可触发。"""
    r = _post(anon, "/v1/chat/completions", headers=_bearer(contract_key["key"]), model="fake-chat-1")
    body = _assert_openai_shell(r, 400, code="ambiguous_model", type_="ambiguous_model")
    candidates = body["detail"]["candidates"]
    assert {item["provider"] for item in candidates} >= {"codebuddy", "opencode"}
    assert all(isinstance(item["readiness"], str) for item in candidates)
    assert r.headers.get("retry-after") is None


def test_v1_messages_400_ambiguous_model(
    anon: httpx.Client, contract_key: dict, ambiguous_models: None
) -> None:
    r = _post(anon, "/v1/messages", headers=_bearer(contract_key["key"]), model="fake-chat-1")
    body = _assert_anthropic_shell(r, 400, inner_type="invalid_request_error")
    assert body["error"]["code"] == "ambiguous_model"
    assert {item["provider"] for item in body["error"]["candidates"]} >= {"codebuddy", "opencode"}


# ---------------- 上游 403 原样透传 ----------------


@pytest.mark.parametrize("path,shell", V1_CASES[:1] + V1_CASES[2:], ids=["chat", "messages"])
def test_v1_upstream_403_passes_through_as_403(
    anon: httpx.Client, contract_key: dict, path: str, shell: str
) -> None:
    r = _post(
        anon, path,
        headers={**_bearer(contract_key["key"]), "x-provider": "codebuddy"},
        model="err-403",
    )
    if shell == "anthropic":
        body = _assert_anthropic_shell(r, 403, inner_type="permission_error")
        error = body["error"]
    else:
        body = _assert_openai_shell(r, 403, code="upstream_forbidden", type_="permission_error")
        error = body["error"]
    assert error["upstream_status"] == 403, "上游 403 原样透传，原始状态码要保留"
    assert error["upstream_code"] == "403"
    assert r.headers.get("retry-after") is None


def test_v1_error_body_redacts_internal_endpoint_but_log_keeps_it(
    admin: httpx.Client, anon: httpx.Client, contract_key: dict
) -> None:
    """上游原文里的回环地址/端口只允许留在服务端日志，错误体必须脱敏。"""
    model = "err-url"
    r = _post(
        anon, "/v1/chat/completions",
        headers={**_bearer(contract_key["key"]), "x-provider": "codebuddy"},
        model=model,
    )
    assert r.status_code == 403, f"{r.status_code} {r.text}"
    for needle in ("127.0.0.1", "localhost", "http://", "8505", "端口 8505"):
        assert needle not in r.text, f"错误体泄露内部端点「{needle}」：{r.text[:400]}"
    assert "[内部地址已隐藏]" in r.text
    rows = admin.get("/api/logs", params={"model": model, "size": 50}).json()["items"]
    row = next(item for item in rows if item["model"] == model)
    assert row["status"] == r.status_code, "日志状态必须与响应状态一致"
    assert "127.0.0.1:8505" in (row.get("error") or ""), "原始端点必须能在服务端日志里查到"


# ---------------- 429 ----------------


@pytest.mark.parametrize("path,shell", V1_CASES[:1] + V1_CASES[2:], ids=["chat", "messages"])
def test_v1_429_upstream_rate_limit(
    anon: httpx.Client, contract_key: dict, path: str, shell: str
) -> None:
    r = _post(
        anon, path,
        headers={**_bearer(contract_key["key"]), "x-provider": "codebuddy"},
        model="err-429",
    )
    if shell == "anthropic":
        body = _assert_anthropic_shell(r, 429, inner_type="rate_limit_error")
    else:
        body = _assert_openai_shell(r, 429, code="upstream_rate_limited", type_="rate_limit_error")
    assert body["error"]["upstream_status"] == 429
    assert r.headers.get("retry-after") == "60", "429 必须带 Retry-After"


def test_v1_responses_429_upstream_rate_limit(
    anon: httpx.Client, contract_key: dict, codar_on_fake: None
) -> None:
    r = _post(
        anon, "/v1/responses",
        headers={**_bearer(contract_key["key"]), "x-provider": "codar"},
        model="err-429",
    )
    body = _assert_openai_shell(r, 429, code="upstream_rate_limited", type_="rate_limit_error")
    assert body["error"]["upstream_status"] == 429
    assert r.headers.get("retry-after") == "60"


def test_v1_429_quota_uses_structured_detail(anon: httpx.Client, admin: httpx.Client) -> None:
    key = _new_key(admin, f"quota-contract-{uuid.uuid4().hex[:8]}")
    policy = admin.post(
        "/api/quota/policies",
        json={"key_id": key["id"], "window": "day", "limit_requests": 1},
    )
    assert policy.status_code == 200, f"{policy.status_code} {policy.text}"
    policy_id = policy.json()["id"]

    try:
        allowed = _post(anon, "/v1/chat/completions", headers=_bearer(key["key"]), model="fake-chat-1")
        assert allowed.status_code == 200, f"{allowed.status_code} {allowed.text}"

        rejected = _post(anon, "/v1/chat/completions", headers=_bearer(key["key"]), model="fake-chat-1")
        body = _assert_openai_shell(rejected, 429, code="quota_exceeded", type_="insufficient_quota")
        detail = body["detail"]
        assert isinstance(detail, dict), f"配额 429 的 detail 必须是结构化对象：{detail}"
        for field, kind in (
            ("type", str), ("layer", str), ("window", str), ("metric", str),
            ("used", int), ("limit", int), ("unit", str),
            ("reset_at", int), ("reset_after", int),
        ):
            assert isinstance(detail.get(field), kind), f"detail.{field} 类型不对：{detail}"
        assert detail["type"] == "quota_exceeded"
        assert detail["metric"] == "requests"
        assert (detail["used"], detail["limit"]) == (1, 1)
        retry_after = rejected.headers.get("retry-after")
        assert retry_after is not None and abs(int(retry_after) - detail["reset_after"]) <= 1

        # Anthropic 外壳按协议只保留 type + message：结构化 detail 不可见，属壳的固有差异
        anthropic = _post(anon, "/v1/messages", headers=_bearer(key["key"]), model="fake-chat-1")
        body = _assert_anthropic_shell(anthropic, 429, inner_type="rate_limit_error")
        assert "已用" in body["error"]["message"]
        anthropic_retry = anthropic.headers.get("retry-after")
        assert anthropic_retry is not None and abs(int(anthropic_retry) - detail["reset_after"]) <= 1
    finally:
        # 管理员策略列表会被后续安全测试做精确断言，必须收走本次临时策略
        admin.delete(f"/api/quota/policies/{policy_id}")


def test_v1_429_failure_rate_is_structured(anon: httpx.Client, admin: httpx.Client) -> None:
    key = _new_key(admin, f"failure-contract-{uuid.uuid4().hex[:8]}")
    headers = {**_bearer(key["key"]), "x-provider": "codebuddy"}

    for _ in range(20):
        r = _post(anon, "/v1/chat/completions", headers=headers, model="err-400")
        assert r.status_code == 400, f"前 20 次应是上游 400：{r.status_code} {r.text}"

    limited = _post(anon, "/v1/chat/completions", headers=headers, model="fake-chat-1")
    body = _assert_openai_shell(limited, 429, code="failure_rate_limited", type_="rate_limit_error")
    detail = body["detail"]
    assert isinstance(detail, dict), f"失败限流的 detail 必须是结构化对象：{detail}"
    assert detail["type"] == "failure_rate_limited"
    assert detail["layer"] == "key"
    assert detail["metric"] == "failures"
    assert detail["window"] == "sliding"
    assert detail["unit"] == "requests"
    assert (detail["used"], detail["limit"]) == (20, 20)
    assert isinstance(detail["reset_after"], int) and detail["reset_after"] >= 1
    assert limited.headers.get("retry-after") == str(detail["reset_after"])


def test_api_429_login_throttle_uses_detail_shell(base_url: str) -> None:
    username = f"no-user-{uuid.uuid4().hex[:8]}"
    with httpx.Client(base_url=base_url, trust_env=False, timeout=10.0) as fresh:
        for _ in range(5):
            r = fresh.post("/api/login", json={"username": username, "password": "wrong"})
            assert r.status_code == 401, f"{r.status_code} {r.text}"
            _assert_detail_shell(r, 401)
        r = fresh.post("/api/login", json={"username": username, "password": "wrong"})
        body = _assert_detail_shell(r, 429)
        assert isinstance(body["detail"], str) and "登录失败次数过多" in body["detail"]
        assert r.headers.get("retry-after") is None, "/api/* 的登录节流不带 Retry-After"


# ---------------- 501 / 502 / 503 ----------------


def test_v1_responses_501_is_openai_shell(
    anon: httpx.Client, contract_key: dict, trae_sidecar_on: None
) -> None:
    # trae_sidecar 声明 responses=False；启用后能力门槛先于上游调用触发 501
    r = _post(
        anon, "/v1/responses",
        headers=_bearer(contract_key["key"]),
        model="trae_sidecar/fake-chat-1",
    )
    body = _assert_openai_shell(r, 501, code="capability_unsupported", type_="invalid_request_error")
    assert body["error"]["provider"] == "trae_sidecar"
    assert r.headers.get("retry-after") is None


def test_v1_messages_501_is_anthropic_shell(
    anon: httpx.Client, contract_key: dict, trae_sidecar_on: None
) -> None:
    # trae_sidecar 声明 messages=False；启用后能力门槛先于上游调用触发 501
    r = _post(
        anon, "/v1/messages",
        headers=_bearer(contract_key["key"]),
        model="trae_sidecar/fake-chat-1",
    )
    body = _assert_anthropic_shell(r, 501, inner_type="invalid_request_error")
    assert body["error"]["provider"] == "trae_sidecar"
    assert r.headers.get("retry-after") is None


@pytest.mark.parametrize("path,shell", V1_CASES[:1] + V1_CASES[2:], ids=["chat", "messages"])
def test_v1_explicit_disabled_provider_is_rejected(
    anon: httpx.Client, contract_key: dict, trae_sidecar_off: None, path: str, shell: str
) -> None:
    """契约：显式 x-provider 指定已停用 provider 时返回 503 provider_disabled，不得静默放行。"""
    r = _post(
        anon, path,
        headers={**_bearer(contract_key["key"]), "x-provider": "trae_sidecar"},
        model="fake-chat-1",
    )
    if shell == "anthropic":
        body = _assert_anthropic_shell(r, 503, inner_type="api_error")
        assert body["type"] == "error"
        assert body["error"]["type"] == "api_error"
    else:
        body = _assert_openai_shell(r, 503, code="provider_disabled", type_="api_error")
        assert body["detail"]["provider"] == "trae_sidecar"
    assert body["error"]["code"] == "provider_disabled"
    assert "已停用" in body["error"]["message"]
    assert r.headers.get("retry-after") is None


def test_v1_prefixed_disabled_provider_is_rejected(
    anon: httpx.Client, contract_key: dict, trae_sidecar_off: None
) -> None:
    """契约：provider/model 前缀同样不能绕过停用状态。"""
    r = _post(
        anon, "/v1/chat/completions",
        headers=_bearer(contract_key["key"]),
        model="trae_sidecar/fake-chat-1",
    )
    body = _assert_openai_shell(r, 503, code="provider_disabled", type_="api_error")
    assert body["detail"]["provider"] == "trae_sidecar"
    assert "已停用" in body["error"]["message"]


def test_v1_messages_prefixed_disabled_provider_is_rejected(
    anon: httpx.Client, contract_key: dict, trae_sidecar_off: None
) -> None:
    """契约：messages 侧的 provider/model 前缀同样不能绕过停用状态。"""
    r = _post(
        anon, "/v1/messages",
        headers=_bearer(contract_key["key"]),
        model="trae_sidecar/fake-chat-1",
    )
    body = _assert_anthropic_shell(r, 503, inner_type="api_error")
    assert body["type"] == "error"
    assert body["error"]["type"] == "api_error"
    assert body["error"]["code"] == "provider_disabled"
    assert body["error"]["provider"] == "trae_sidecar"
    assert "已停用" in body["error"]["message"]
    assert r.headers.get("retry-after") is None


def test_v1_messages_unknown_provider_prefix_is_model_not_found(
    anon: httpx.Client, contract_key: dict
) -> None:
    """契约：前缀指向不存在的 provider 时按模型维度 404，不能当成停用或 500。"""
    model = f"no-such-provider-{uuid.uuid4().hex[:8]}/fake-chat-1"
    r = _post(anon, "/v1/messages", headers=_bearer(contract_key["key"]), model=model)
    body = _assert_anthropic_shell(r, 404, inner_type="not_found_error")
    assert body["error"]["code"] == "model_not_found"
    assert body["error"]["type"] == "not_found_error"
    assert r.headers.get("retry-after") is None

    chat = _post(anon, "/v1/chat/completions", headers=_bearer(contract_key["key"]), model=model)
    openai = _assert_openai_shell(chat, 404, code="model_not_found", type_="model_not_found")
    assert openai["detail"]["model"] == model
    assert openai["detail"]["candidates"] == []


@pytest.mark.parametrize("path,shell", V1_CASES[:1] + V1_CASES[2:], ids=["chat", "messages"])
def test_v1_502_upstream_5xx(
    anon: httpx.Client, contract_key: dict, path: str, shell: str
) -> None:
    r = _post(
        anon, path,
        headers={**_bearer(contract_key["key"]), "x-provider": "codebuddy"},
        model="err-500",
    )
    if shell == "anthropic":
        body = _assert_anthropic_shell(r, 502, inner_type="api_error")
    else:
        body = _assert_openai_shell(r, 502, code="upstream_unavailable", type_="api_error")
    assert body["error"]["upstream_status"] == 500
    assert body["error"]["upstream_code"] == "500"
    assert r.headers.get("retry-after") is None


def test_v1_responses_502_upstream_5xx(
    anon: httpx.Client, contract_key: dict, codar_on_fake: None
) -> None:
    r = _post(
        anon, "/v1/responses",
        headers={**_bearer(contract_key["key"]), "x-provider": "codar"},
        model="err-500",
    )
    body = _assert_openai_shell(r, 502, code="upstream_unavailable", type_="api_error")
    assert body["error"]["provider"] == "codar"
    assert body["error"]["upstream_status"] == 500


@pytest.mark.parametrize("path,shell", V1_CASES[:1] + V1_CASES[2:], ids=["chat", "messages"])
def test_v1_503_upstream_unavailable(
    anon: httpx.Client, contract_key: dict, path: str, shell: str
) -> None:
    r = _post(
        anon, path,
        headers={**_bearer(contract_key["key"]), "x-provider": "codebuddy"},
        model="err-503",
    )
    if shell == "anthropic":
        body = _assert_anthropic_shell(r, 503, inner_type="overloaded_error")
    else:
        body = _assert_openai_shell(r, 503, code="provider_unavailable", type_="api_error")
    assert body["error"]["upstream_status"] == 503
    assert r.headers.get("retry-after") is None


def test_v1_responses_503_upstream_unavailable(
    anon: httpx.Client, contract_key: dict, codar_on_fake: None
) -> None:
    r = _post(
        anon, "/v1/responses",
        headers={**_bearer(contract_key["key"]), "x-provider": "codar"},
        model="err-503",
    )
    body = _assert_openai_shell(r, 503, code="provider_unavailable", type_="api_error")
    assert body["error"]["provider"] == "codar"
    assert body["error"]["upstream_status"] == 503


def test_v1_no_enabled_provider_reports_model_not_found(
    anon: httpx.Client, contract_key: dict, providers_off: None
) -> None:
    """契约：没有已启用的 chat provider 时按模型维度 404，网关本身仍认为是可用状态。

    503 留给上游/上游侧不可用（provider_unavailable）；这里请求的模型确实没有可服务的
    候选，语义是 model_not_found，且 detail 里要给出候选为空的证据。
    """
    r = _post(
        anon, "/v1/chat/completions",
        headers=_bearer(contract_key["key"]),
        model="fake-chat-1",
    )
    body = _assert_openai_shell(r, 404, code="model_not_found", type_="model_not_found")
    assert body["detail"]["model"] == "fake-chat-1"
    assert body["detail"]["candidates"] == [], "没有已启用 provider 时不应有候选"

    anthropic = _post(
        anon, "/v1/messages",
        headers=_bearer(contract_key["key"]),
        model="fake-chat-1",
    )
    shell = _assert_anthropic_shell(anthropic, 404, inner_type="not_found_error")
    assert shell["error"]["code"] == "model_not_found"


# ---------------- 探针 503 ----------------


def test_api_ready_probe_503_shape(anon: httpx.Client, providers_off: None) -> None:
    r = anon.get("/api/health/ready")
    assert r.status_code == 503, f"没有可服务 provider 时 readiness 必须 503：{r.status_code} {r.text}"
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "error"
    assert "providers" in body["unhealthy"]
    providers = body["checks"]["providers"]
    assert providers["available"] == 0
    assert providers["ok"] is False
    assert isinstance(providers["items"], list)
    assert isinstance(body["request_id"], str)
    _assert_no_leak(r)
    assert r.headers.get("retry-after") is None


def test_api_auth_start_503_without_oauth_provider(
    admin: httpx.Client, providers_off: None
) -> None:
    r = admin.post("/api/auth/start", json={"realm": "cn"})
    body = _assert_detail_shell(r, 503)
    assert isinstance(body["detail"], str) and "扫码登录不可用" in body["detail"]
    assert r.headers.get("retry-after") is None


# ---------------- 流式中途断流：统计/日志口径一致 ----------------


def test_stream_break_is_not_counted_as_success(
    anon: httpx.Client, admin: httpx.Client, contract_key: dict
) -> None:
    before = admin.get("/api/stats/summary").json()
    r = anon.post(
        "/v1/chat/completions",
        headers={**_bearer(contract_key["key"]), "x-provider": "codebuddy"},
        json={"model": "stream-break", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, f"已出帧后断流 HTTP 层只能是 200：{r.status_code} {r.text}"
    text = r.text
    assert "partial" in text, "断流前的分片要照常转发"
    assert '"error"' in text and "upstream_stream_broken" in text
    assert "[DONE]" in text, "OpenAI 流式错误事件后仍要收尾"

    time.sleep(0.3)  # 旁路日志异步落库
    after = admin.get("/api/stats/summary").json()
    assert after["today_requests"] == before["today_requests"], "断流不能计入成功请求数"
    assert after["failures"]["today_5xx"] == before["failures"]["today_5xx"] + 1

    params = {"key_id": contract_key["id"], "model": "stream-break"}
    ok_rows = admin.get("/api/logs", params={**params, "status": "ok"}).json()
    err_rows = admin.get("/api/logs", params={**params, "status": "error"}).json()
    assert ok_rows["total"] == 0, "/api/logs 的成功口径要和 stats 一致（带 error 的 2xx 不算成功）"
    assert err_rows["total"] >= 1, "断流日志要落在 error 过滤里"
    assert all(row["error"] for row in err_rows["items"] if row["model"] == "stream-break")


def test_stream_break_writes_no_ledger_and_marks_error(
    anon: httpx.Client, contract_key: dict, db_path: str
) -> None:
    """断流直接断言（不走 /api/stats 间接证据）：request_logs 带 error，且不产生计费流水。"""
    tag = uuid.uuid4().hex[:8]
    model = f"stream-break-{tag}"
    r = anon.post(
        "/v1/chat/completions",
        headers={**_bearer(contract_key["key"]), "x-provider": "codebuddy"},
        json={"model": model, "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, f"已出帧后断流 HTTP 层只能是 200：{r.status_code} {r.text}"
    assert "partial" in r.text, "断流前的分片要照常转发"
    assert "upstream_stream_broken" in r.text

    time.sleep(1.0)  # 落库走 shield 后的线程，留足写盘时间再断言「没写」

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        logs = conn.execute(
            "SELECT status, error FROM request_logs WHERE key_id = ? AND model = ?",
            (contract_key["id"], model),
        ).fetchall()
        ledger = conn.execute(
            "SELECT status, billed_micro, note FROM billing_ledger WHERE key_id = ? AND model = ?",
            (contract_key["id"], model),
        ).fetchall()
    finally:
        conn.close()

    assert logs, "断流必须在 request_logs 留痕"
    for row in logs:
        assert row["status"] == 200, "出帧后断流 HTTP 状态仍是 200，成败只能看 error 列"
        assert (row["error"] or "").strip(), "request_logs.error 必须非空，否则会被算成成功"
    assert not ledger, f"断流不能写 billing_ledger：{[dict(row) for row in ledger]}"
