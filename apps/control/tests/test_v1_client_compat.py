"""sub2api 专用客户端兼容面回归：Claude Code / Antigravity / Sora。

上游证据全部来自源码副本 sub2api-codex-gnet-sub2api 下
backend\\internal\\server\\routes\\gateway.go：
Claude Code :158-163、Antigravity :166 与 :169-181、Sora :198-219。
桩上游只校验「命中路径 / 方法 / 透传头 / 状态码 / content-type」，
错误文案不参与断言。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import httpx
import pytest

pytestmark = pytest.mark.gateway

# 上游收到的每一次请求：路径（含查询串）、方法、关键头、原始请求体
STUB_HITS: list[dict[str, Any]] = []


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args: Any) -> None:
        pass

    def _record(self, method: str) -> bytes:
        raw = self.rfile.read(int(self.headers.get("content-length") or 0)) if method != "GET" else b""
        STUB_HITS.append(
            {
                "method": method,
                "path": self.path,
                "body": raw.decode("utf-8", "ignore"),
                "authorization": self.headers.get("authorization", ""),
                "anthropic_version": self.headers.get("anthropic-version", ""),
                "if_none_match": self.headers.get("if-none-match", ""),
            }
        )
        return raw

    def _json(self, status: int, payload: dict[str, Any], extra: dict[str, str] | None = None) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def _binary(self, content_type: str, raw: bytes) -> None:
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        for index in range(2):
            self.wfile.write(f'data: {{"index":{index}}}\n\n'.encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.1)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._record("GET")
        path = self.path.split("?")[0]
        if path == "/api/claude_cli/bootstrap":
            self._json(200, {"client_data": {"region": "stub"}, "additional_model_options": []},
                       {"cache-control": "private, max-age=3600"})
        elif path == "/api/claude_code/organizations/metrics_enabled":
            self._json(200, {"metrics_logging_enabled": False})
        elif path == "/api/claude_code/settings":
            etag = '"stub-etag"'
            if self.headers.get("if-none-match") == etag:
                self.send_response(304)
                self.send_header("etag", etag)
                self.send_header("vary", "If-None-Match")
                self.end_headers()
                return
            self.send_response(204)
            self.send_header("etag", etag)
            self.send_header("vary", "If-None-Match")
            self.end_headers()
        elif path == "/api/claude_code/policy_limits":
            self._json(200, {"restrictions": {}})
        elif path == "/api/claude_code/user_settings":
            self._json(404, {"error": {"type": "not_found_error", "message": "stub has no settings"}})
        elif path == "/antigravity/models":
            self._json(200, {"object": "list", "data": [{"id": "antigravity-fake", "type": "model"}]})
        elif path == "/antigravity/v1beta/models":
            self._json(200, {"models": [{"name": "models/antigravity-fake"}]})
        elif path == "/antigravity/v1beta/models/antigravity-fake":
            self._json(200, {"name": "models/antigravity-fake"})
        elif path == "/sora/v1/models":
            self._json(200, {"object": "list", "data": [{"id": "sora-fake"}]})
        elif path == "/antigravity/v1/usage":
            self._json(200, {"mode": "unrestricted", "total": {"requests": 3}})
        elif path == "/sora/media/image/cat.png":
            self._binary("image/png", b"\x89PNG-stub-bytes")
        elif path == "/sora/media-signed/image/cat.png":
            self._binary("image/png", b"\x89PNG-stub-bytes")
        elif path == "/sora/media-signed/video/v.mp4":
            if "redirect=1" in self.path:
                self.send_response(302)
                self.send_header("location", "http://stub.local/actual.mp4")
                self.send_header("content-length", "0")
                self.end_headers()
                return
            self._binary("video/mp4", b"MP4-stub-bytes")
        else:
            self._json(404, {"error": {"message": f"stub 未实现 {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        raw = self._record("POST")
        path = self.path.split("?")[0]
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}
        if path == "/antigravity/v1/messages":
            if payload.get("stream"):
                self._sse()
            else:
                self._json(200, {"id": "msg-1", "type": "message", "content": [{"type": "text", "text": "ok"}]})
        elif path == "/antigravity/v1/messages/count_tokens":
            self._json(200, {"input_tokens": 42})
        elif path == "/antigravity/v1beta/models/antigravity-fake:generateContent":
            self._json(200, {"modelVersion": "antigravity-fake", "candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
        elif path == "/sora/v1/chat/completions":
            if not payload.get("model"):
                self._json(400, {"error": {"type": "invalid_request_error", "message": "model is required"}})
            elif payload.get("stream"):
                self._sse()
            else:
                self._json(200, {"id": "chat-1", "object": "chat.completion"})
        else:
            self._json(404, {"error": {"message": f"stub 未实现 {self.path}"}})

    def do_PUT(self) -> None:  # noqa: N802
        self._record("PUT")
        if self.path.split("?")[0] == "/api/claude_code/user_settings":
            self._json(200, {"checksum": "stub-checksum", "lastModified": "2026-01-01T00:00:00Z"})
        else:
            self._json(404, {"error": {"message": f"stub 未实现 {self.path}"}})


@pytest.fixture(scope="module")
def stub_upstream() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def bearer_key(admin: httpx.Client) -> str:
    created = admin.post("/api/keys", json={"name": "client-compat"})
    assert created.status_code == 200, created.text
    return str(created.json()["key"])


@pytest.fixture
def sub2api_on_stub(admin: httpx.Client, stub_upstream: str) -> Iterator[None]:
    gid = f"g-compat-{uuid.uuid4().hex[:6]}"
    group = admin.post("/api/sub2api/groups", json={"id": gid, "name": "client-compat"})
    assert group.status_code == 200, group.text
    account = admin.post(
        "/api/sub2api/accounts",
        json={"group_id": gid, "name": "a-compat", "upstream_base": stub_upstream, "api_key": "stub-key"},
    )
    assert account.status_code == 200, account.text
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "sub2api")
    patched = admin.patch("/api/providers/sub2api", json={"enabled": True, "options": {"group": gid}})
    assert patched.status_code == 200, patched.text
    try:
        yield
    finally:
        restored = admin.patch(
            "/api/providers/sub2api",
            json={
                "enabled": bool(before["enabled"]),
                "options": {"group": str((before.get("options") or {}).get("group", "default"))},
            },
        )
        assert restored.status_code == 200, restored.text
        listed = admin.get("/api/sub2api/accounts", params={"group_id": gid})
        if listed.status_code == 200:
            for row in listed.json():
                admin.delete(f"/api/sub2api/accounts/{row.get('id')}")
        admin.delete(f"/api/sub2api/groups/{gid}")


def _auth(key: str, provider: str | None = None) -> dict[str, str]:
    headers = {"authorization": f"Bearer {key}"}
    if provider:
        headers["x-provider"] = provider
    return headers


def _last_hit(path_prefix: str) -> dict[str, Any]:
    for hit in reversed(STUB_HITS):
        if hit["path"].split("?")[0] == path_prefix:
            return hit
    raise AssertionError(f"桩上游没收到 {path_prefix}：{[h['path'] for h in STUB_HITS]}")


# ---------- 鉴权沿用 /v1 同一套 key，缺 key 一律 401 ----------


def test_client_compat_endpoints_enforce_auth(anon: httpx.Client) -> None:
    for path in (
        "/api/claude_cli/bootstrap",
        "/api/claude_code/organizations/metrics_enabled",
        "/api/claude_code/settings",
        "/api/claude_code/policy_limits",
        "/api/claude_code/user_settings",
        "/antigravity/models",
        "/antigravity/v1beta/models",
        "/antigravity/v1beta/models/antigravity-fake",
        "/antigravity/v1/usage",
        "/sora/v1/models",
        "/sora/media/image/cat.png",
    ):
        assert anon.get(path).status_code == 401, path
    for path in (
        "/antigravity/v1/messages",
        "/antigravity/v1/messages/count_tokens",
        "/antigravity/v1beta/models/antigravity-fake:generateContent",
        "/sora/v1/chat/completions",
    ):
        assert anon.post(path, json={"model": "fake"}).status_code == 401, path
    assert anon.put("/api/claude_code/user_settings", json={"entries": {}}).status_code == 401


# ---------- provider 不支持 → 501（不是 500，也不是空响应） ----------


def test_provider_without_compat_surface_answers_501(
    anon: httpx.Client, bearer_key: str, sub2api_on_stub: None
) -> None:
    for method, path in (("get", "/api/claude_cli/bootstrap"), ("get", "/sora/media/image/cat.png")):
        response = getattr(anon, method)(path, headers=_auth(bearer_key, "codebuddy"))
        assert response.status_code == 501, response.text
        error = response.json()["error"]
        assert error["code"] == "capability_unsupported"
        assert error["provider"] == "codebuddy"

    posted = anon.post(
        "/sora/v1/chat/completions",
        headers=_auth(bearer_key, "codebuddy"),
        json={"model": "fake", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert posted.status_code == 501, posted.text
    assert posted.json()["error"]["code"] == "capability_unsupported"


# ---------- Claude Code：6 个端点逐个真转发 ----------


def test_claude_code_endpoints_forward_on_sub2api(
    anon: httpx.Client, bearer_key: str, sub2api_on_stub: None
) -> None:
    STUB_HITS.clear()
    bootstrap = anon.get("/api/claude_cli/bootstrap", headers=_auth(bearer_key))
    assert bootstrap.status_code == 200, bootstrap.text
    assert bootstrap.json()["client_data"]["region"] == "stub"
    assert bootstrap.headers["cache-control"] == "private, max-age=3600"
    hit = _last_hit("/api/claude_cli/bootstrap")
    assert hit["method"] == "GET"
    # 转发用的必须是账号池里的 key，而不是调用方的 key
    assert hit["authorization"] == "Bearer stub-key"

    metrics = anon.get("/api/claude_code/organizations/metrics_enabled", headers=_auth(bearer_key))
    assert metrics.status_code == 200, metrics.text
    assert metrics.json() == {"metrics_logging_enabled": False}
    assert _last_hit("/api/claude_code/organizations/metrics_enabled")["method"] == "GET"

    limits = anon.get("/api/claude_code/policy_limits", headers=_auth(bearer_key))
    assert limits.status_code == 200, limits.text
    assert limits.json() == {"restrictions": {}}

    settings = anon.get("/api/claude_code/settings", headers=_auth(bearer_key))
    assert settings.status_code == 204, settings.text
    assert settings.headers["etag"] == '"stub-etag"'
    cached = anon.get(
        "/api/claude_code/settings",
        headers={**_auth(bearer_key), "if-none-match": '"stub-etag"'},
    )
    assert cached.status_code == 304, cached.text
    assert cached.headers["etag"] == '"stub-etag"'
    assert _last_hit("/api/claude_code/settings")["if_none_match"] == '"stub-etag"'

    missing = anon.get("/api/claude_code/user_settings", headers=_auth(bearer_key))
    assert missing.status_code == 404, missing.text
    assert missing.headers["content-type"].startswith("application/json")

    entries = {"theme": "dark", "model": "fake"}
    saved = anon.put(
        "/api/claude_code/user_settings", headers=_auth(bearer_key), json={"entries": entries}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["checksum"] == "stub-checksum"
    hit = _last_hit("/api/claude_code/user_settings")
    assert hit["method"] == "PUT"
    assert json.loads(hit["body"])["entries"] == entries


# ---------- Antigravity：模型 / messages / count_tokens / usage ----------


def test_antigravity_endpoints_forward_and_stream(
    anon: httpx.Client, bearer_key: str, sub2api_on_stub: None
) -> None:
    STUB_HITS.clear()
    models = anon.get("/antigravity/models", headers=_auth(bearer_key))
    assert models.status_code == 200, models.text
    assert models.json()["data"][0]["id"] == "antigravity-fake"

    count = anon.post(
        "/antigravity/v1/messages/count_tokens",
        headers={**_auth(bearer_key), "anthropic-version": "2023-06-01"},
        json={"model": "antigravity-fake", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert count.status_code == 200, count.text
    assert count.json() == {"input_tokens": 42}
    hit = _last_hit("/antigravity/v1/messages/count_tokens")
    assert hit["method"] == "POST"
    assert hit["anthropic_version"] == "2023-06-01"

    usage = anon.get(
        "/antigravity/v1/usage?start_date=2026-01-01&end_date=2026-01-31",
        headers=_auth(bearer_key),
    )
    assert usage.status_code == 200, usage.text
    assert usage.json()["mode"] == "unrestricted"
    assert _last_hit("/antigravity/v1/usage")["path"].endswith(
        "?start_date=2026-01-01&end_date=2026-01-31"
    )

    plain_payload = {"model": "antigravity-fake", "messages": [{"role": "user", "content": "hi"}]}
    plain = anon.post(
        "/antigravity/v1/messages",
        headers=_auth(bearer_key),
        json=plain_payload,
    )
    assert plain.status_code == 200, plain.text
    assert plain.json()["content"][0]["text"] == "ok"
    # 请求体必须原样转发：网关不做字段补全，也不改写 model
    assert json.loads(_last_hit("/antigravity/v1/messages")["body"]) == plain_payload

    with anon.stream(
        "POST",
        "/antigravity/v1/messages",
        headers=_auth(bearer_key),
        json={"model": "antigravity-fake", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as streamed:
        assert streamed.status_code == 200, streamed.text
        assert streamed.headers["content-type"].startswith("text/event-stream")
        frames = [line for line in streamed.iter_lines() if line.startswith("data:")]
    assert len(frames) == 2, frames
    assert json.loads(frames[0].removeprefix("data: "))["index"] == 0
    assert json.loads(frames[1].removeprefix("data: "))["index"] == 1
    assert json.loads(_last_hit("/antigravity/v1/messages")["body"])["stream"] is True


def test_antigravity_v1beta_and_sora_models_forward(
    anon: httpx.Client, bearer_key: str, sub2api_on_stub: None
) -> None:
    STUB_HITS.clear()
    listing = anon.get("/antigravity/v1beta/models", headers=_auth(bearer_key))
    assert listing.status_code == 200, listing.text
    assert listing.json()["models"][0]["name"] == "models/antigravity-fake"
    assert _last_hit("/antigravity/v1beta/models")["method"] == "GET"

    one = anon.get("/antigravity/v1beta/models/antigravity-fake", headers=_auth(bearer_key))
    assert one.status_code == 200, one.text
    assert one.json()["name"] == "models/antigravity-fake"

    action_payload = {"contents": [{"parts": [{"text": "hi"}]}]}
    action = anon.post(
        "/antigravity/v1beta/models/antigravity-fake:generateContent",
        headers=_auth(bearer_key),
        json=action_payload,
    )
    assert action.status_code == 200, action.text
    assert action.json()["modelVersion"] == "antigravity-fake"
    hit = _last_hit("/antigravity/v1beta/models/antigravity-fake:generateContent")
    assert hit["method"] == "POST"
    assert json.loads(hit["body"]) == action_payload

    sora = anon.get("/sora/v1/models", headers=_auth(bearer_key))
    assert sora.status_code == 200, sora.text
    assert sora.json()["data"][0]["id"] == "sora-fake"
    assert _last_hit("/sora/v1/models")["method"] == "GET"


# ---------- Sora：chat completions / media / media-signed ----------


def test_sora_chat_streams_and_upstream_4xx_passes_through(
    anon: httpx.Client, bearer_key: str, sub2api_on_stub: None
) -> None:
    STUB_HITS.clear()
    with anon.stream(
        "POST",
        "/sora/v1/chat/completions",
        headers=_auth(bearer_key),
        json={"model": "sora-fake", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as streamed:
        assert streamed.status_code == 200, streamed.text
        assert streamed.headers["content-type"].startswith("text/event-stream")
        frames = [line for line in streamed.iter_lines() if line.startswith("data:")]
    assert len(frames) == 2, frames

    rejected = anon.post(
        "/sora/v1/chat/completions",
        headers=_auth(bearer_key),
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.headers["content-type"].startswith("application/json")
    assert rejected.json()["error"]["type"] == "invalid_request_error"


def test_sora_media_and_signed_media_pass_through(
    anon: httpx.Client, bearer_key: str, sub2api_on_stub: None
) -> None:
    STUB_HITS.clear()
    media = anon.get("/sora/media/image/cat.png?token=abc", headers=_auth(bearer_key))
    assert media.status_code == 200, media.text
    assert media.headers["content-type"] == "image/png"
    assert media.content == b"\x89PNG-stub-bytes"
    assert _last_hit("/sora/media/image/cat.png")["path"] == "/sora/media/image/cat.png?token=abc"

    # 签名媒体按上游语义免 key：不发送调用方 key，网关也不向下游塞账号凭据
    redirected = anon.get("/sora/media-signed/video/v.mp4?expires=1&sig=s&redirect=1")
    assert redirected.status_code == 302, redirected.text
    assert redirected.headers["location"] == "http://stub.local/actual.mp4"
    assert _last_hit("/sora/media-signed/video/v.mp4")["authorization"] == ""

    signed = anon.get("/sora/media-signed/video/v.mp4?expires=1&sig=s")
    assert signed.status_code == 200, signed.text
    assert signed.headers["content-type"] == "video/mp4"
    assert signed.content == b"MP4-stub-bytes"


def test_signed_media_allows_anonymous_get(
    anon: httpx.Client, sub2api_on_stub: None, admin: httpx.Client
) -> None:
    # 上游 gateway.go:219 不挂 apiKeyAuth，签名（expires+sig）即鉴权：不带 key 也应可用
    STUB_HITS.clear()
    ok = anon.get("/sora/media-signed/image/cat.png?expires=1&sig=s")
    assert ok.status_code == 200, ok.text
    assert ok.headers["content-type"] == "image/png"
    assert _last_hit("/sora/media-signed/image/cat.png")["authorization"] == ""

    redirected = anon.get("/sora/media-signed/video/v.mp4?expires=1&sig=s&redirect=1")
    assert redirected.status_code == 302, redirected.text
    assert redirected.headers["location"] == "http://stub.local/actual.mp4"

    # 匿名访问在 request_logs 里标成 anonymous，不冒充任何账号
    rows = admin.get("/api/logs", params={"provider": "sub2api", "size": 25}).json()["items"]
    assert any(str(row["key_name"]) == "anonymous" for row in rows), rows


def test_other_paths_still_require_key(anon: httpx.Client, sub2api_on_stub: None) -> None:
    assert anon.get("/sora/media/image/cat.png").status_code == 401
    assert anon.get("/sora/v1/models").status_code == 401
    assert anon.get("/api/claude_cli/bootstrap").status_code == 401
    # 匿名放行只限 GET，其它方法不给
    assert anon.post("/sora/media-signed/image/cat.png?expires=1&sig=s").status_code == 405


def test_signed_media_rejects_out_of_scope_paths(
    anon: httpx.Client, sub2api_on_stub: None
) -> None:
    for bad in (
        "/sora/media-signed/other/file.bin",
        "/sora/media-signed/%2e%2e/v1/models",
        "/sora/media-signed/image/%2e%2e/video/x",
    ):
        response = anon.get(bad)
        assert 400 <= response.status_code < 500, (bad, response.status_code)


def test_client_compat_lands_request_logs(
    admin: httpx.Client, anon: httpx.Client, bearer_key: str, sub2api_on_stub: None
) -> None:
    # 兼容面同样走 /v1 的记账旁路：请求结束后 request_logs 里能看到 provider 与状态码
    anon.get("/api/claude_cli/bootstrap", headers=_auth(bearer_key))
    rows = admin.get("/api/logs", params={"provider": "sub2api", "size": 25}).json()["items"]
    assert any(str(row["status"]) == "200" for row in rows), rows
