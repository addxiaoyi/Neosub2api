"""/v1 统一错误契约 + 权限失败可读原因 + sub2api 只读越权回归。

假上游约定：model 里带 err-<code> 返回该状态码；model 带 stream-break 时在第一帧后断流。
"""

from __future__ import annotations

import uuid

import httpx
import pytest

pytestmark = pytest.mark.gateway


@pytest.fixture(scope="module")
def bearer_key(admin: httpx.Client) -> str:
    r = admin.post("/api/keys", json={"name": f"gateway-errors-{uuid.uuid4().hex[:6]}"})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()["key"]


@pytest.fixture(scope="module")
def viewer(admin: httpx.Client) -> httpx.Client:
    name = f"viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post("/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"})
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return client


def _headers(key: str, provider: str = "codebuddy") -> dict[str, str]:
    return {"authorization": f"Bearer {key}", "x-provider": provider}


def _chat(anon: httpx.Client, key: str, model: str, *, stream: bool = False) -> httpx.Response:
    return anon.post(
        "/v1/chat/completions",
        headers=_headers(key),
        json={"model": model, "stream": stream, "messages": [{"role": "user", "content": "hi"}]},
    )


def _messages(anon: httpx.Client, key: str, model: str, *, provider: str = "codebuddy") -> httpx.Response:
    return anon.post(
        "/v1/messages",
        headers=_headers(key, provider),
        json={
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )


@pytest.fixture(scope="module")
def zcode_on_fake(admin: httpx.Client, fake_base: str):
    """临时启用 zcode 并指到假上游（停用 provider 已不允许调用），结束后原样还原。"""
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "zcode")
    patched = admin.patch(
        "/api/providers/zcode",
        json={"enabled": True, "base_url": fake_base, "api_key": "zcode-fake-key"},
    )
    assert patched.status_code == 200, patched.text
    try:
        yield
    finally:
        restore_key = "***" if before["has_api_key"] else ""
        r = admin.patch(
            "/api/providers/zcode",
            json={
                "enabled": bool(before["enabled"]),
                "base_url": before["base_url"],
                "api_key": restore_key,
            },
        )
        assert r.status_code == 200, f"{r.status_code} {r.text}"


# ---------------- 统一错误形状 ----------------


def test_upstream_401_is_openai_shaped(anon: httpx.Client, bearer_key: str) -> None:
    r = _chat(anon, bearer_key, "err-401")
    assert r.status_code == 401, f"{r.status_code} {r.text}"
    body = r.json()
    error = body["error"]
    assert error["type"] == "authentication_error"
    assert error["code"] == "upstream_auth_failed"
    assert error["provider"] == "codebuddy"
    assert error["upstream_status"] == 401
    assert error["upstream_code"] == "401"
    assert "fake vendor error 401" in error["upstream_message"]
    assert body["detail"], "旧的 detail 字段要保留，前端还在读它"


def test_upstream_403_passes_through_as_403(anon: httpx.Client, bearer_key: str) -> None:
    r = _chat(anon, bearer_key, "err-403")
    assert r.status_code == 403, f"{r.status_code} {r.text}"
    error = r.json()["error"]
    assert error["type"] == "permission_error"
    assert error["code"] == "upstream_forbidden"
    assert error["upstream_status"] == 403, "上游 403 不再被压成 401，原始状态码要能区分"
    assert error["upstream_code"] == "403"


def test_upstream_429_keeps_status_and_retry_after(anon: httpx.Client, bearer_key: str) -> None:
    r = _chat(anon, bearer_key, "err-429")
    assert r.status_code == 429, f"{r.status_code} {r.text}"
    assert r.headers.get("retry-after") == "60", "429 必须给 Retry-After"
    error = r.json()["error"]
    assert error["type"] == "rate_limit_error"
    assert error["code"] == "upstream_rate_limited"
    assert error["upstream_status"] == 429


def test_upstream_500_merges_into_502(anon: httpx.Client, bearer_key: str) -> None:
    r = _chat(anon, bearer_key, "err-500")
    assert r.status_code == 502, f"{r.status_code} {r.text}"
    error = r.json()["error"]
    assert error["type"] == "api_error"
    assert error["code"] == "upstream_unavailable"
    assert error["upstream_status"] == 500
    assert error["upstream_code"] == "500"


def test_streamed_upstream_error_is_json_not_sse(anon: httpx.Client, bearer_key: str) -> None:
    r = _chat(anon, bearer_key, "err-429", stream=True)
    assert r.status_code == 429, f"{r.status_code} {r.text}"
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["error"]["type"] == "rate_limit_error"


def test_chat_error_has_no_anthropic_wrapper(anon: httpx.Client, bearer_key: str) -> None:
    body = _chat(anon, bearer_key, "err-401").json()
    assert "type" not in body, "OpenAI 协议不应出现顶层 type=error"
    assert body["error"]["type"] == "authentication_error"
    assert body["detail"]


def test_messages_401_is_anthropic_shaped(
    anon: httpx.Client, bearer_key: str, zcode_on_fake: None
) -> None:
    r = _messages(anon, bearer_key, "err-401", provider="zcode")
    assert r.status_code == 401, f"{r.status_code} {r.text}"
    body = r.json()
    assert set(body) == {"type", "error"}, "Anthropic 协议只认顶层 type + error"
    error = body["error"]
    assert error["type"] == "authentication_error"
    assert error["upstream_status"] == 401
    assert error["upstream_code"] == "401"
    assert error["provider"] == "zcode"
    assert "fake vendor error 401" in error["upstream_message"]


def test_messages_429_is_anthropic_shaped(
    anon: httpx.Client, bearer_key: str, zcode_on_fake: None
) -> None:
    r = _messages(anon, bearer_key, "err-429", provider="zcode")
    assert r.status_code == 429, f"{r.status_code} {r.text}"
    assert r.headers.get("retry-after") == "60"
    body = r.json()
    assert body["type"] == "error"
    error = body["error"]
    assert error["type"] == "rate_limit_error"
    assert error["upstream_status"] == 429
    assert error["upstream_code"] == "429"


def test_stream_midfailure_emits_error_event(anon: httpx.Client, bearer_key: str) -> None:
    with anon.stream(
        "POST",
        "/v1/chat/completions",
        headers=_headers(bearer_key),
        json={"model": "stream-break", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as resp:
        assert resp.status_code == 200, f"{resp.status_code} {resp.read()}"
        text = "".join(resp.iter_text())
    assert "partial" in text, "断流前的分片要照常转发"
    assert '"error"' in text and "upstream_stream_broken" in text, f"断流要下发错误事件：{text[:400]}"
    assert "[DONE]" in text, "错误事件后仍要按 OpenAI 约定收尾"


def test_stream_break_is_not_counted_as_success(admin: httpx.Client, bearer_key: str) -> None:
    """已出帧后断开 HTTP 层仍是 200，但统计要按 error 列算失败。"""
    before = admin.get("/api/stats/summary").json()
    r = _chat(admin, bearer_key, "stream-break", stream=True)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    after = admin.get("/api/stats/summary").json()

    assert after["today_requests"] == before["today_requests"], "断流不能计入成功请求数"
    assert after["failures"]["today_5xx"] == before["failures"]["today_5xx"] + 1
    ok_rows = admin.get("/api/logs", params={"status": "ok", "model": "stream-break"}).json()
    err_rows = admin.get("/api/logs", params={"status": "error", "model": "stream-break"}).json()
    assert ok_rows["total"] == 0, "/api/logs 的成功口径要和 stats 一致"
    assert err_rows["total"] >= 1, "断流日志要落在 error 过滤里"


def test_missing_gateway_key_is_openai_shaped(anon: httpx.Client) -> None:
    r = anon.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert r.status_code == 401
    error = r.json()["error"]
    assert error["type"] == "authentication_error"
    assert error["code"] == "invalid_api_key"


# ---------------- 权限失败的可读原因 ----------------


def test_admin_endpoint_403_has_structured_reason(viewer: httpx.Client) -> None:
    r = viewer.get("/api/settings/upstream")
    assert r.status_code == 403, f"{r.status_code} {r.text}"
    body = r.json()
    assert body["detail"] == "需要管理员权限"
    error = body["error"]
    assert error["type"] == "permission_denied"
    assert error["code"] == "insufficient_role"
    assert error["required_role"] == "admin"
    assert error["role"] == "viewer"


def test_quota_reader_scopes_viewer_to_own_keys(viewer: httpx.Client) -> None:
    """非 admin 不再一律 403：无过滤时只返回自有 key 的数据（此处 viewer 无 key，为空）。"""
    r = viewer.get("/api/quota/policies")
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    assert r.json() == []


# ---------------- sub2api 只读越权 ----------------


def test_sub2api_reads_require_admin(viewer: httpx.Client) -> None:
    """viewer 曾能读到全局分组/账号池/流水；收紧后必须在任何查库前 403。"""
    cases = [
        ("/api/sub2api/groups", None),
        ("/api/sub2api/accounts", {"group_id": "no-such-group"}),
        ("/api/sub2api/ledger", {"group_id": "no-such-group"}),
    ]
    for path, params in cases:
        r = viewer.get(path, params=params)
        assert r.status_code == 403, f"{path} 应该 403，实际 {r.status_code} {r.text}"
        body = r.json()
        assert body["detail"] == "无权查看配额数据", path
        assert body["error"]["type"] == "permission_denied", path
        assert body["error"]["required_role"] == "admin", path


def test_sub2api_reads_work_for_admin(admin: httpx.Client) -> None:
    assert admin.get("/api/sub2api/groups").status_code == 200
    assert admin.get("/api/sub2api/accounts").status_code == 200
    assert admin.get("/api/sub2api/ledger").status_code == 200
