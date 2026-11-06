"""新增协议端点回归：能力位真值 + 网关转发 + 账务落点。

覆盖 /v1/embeddings、/v1/images/*、/v1/videos、/v1/media、/v1/messages/count_tokens、
/v1/rerank、/v1/usage；rerank/videos/usage 依据上游源码副本 sub2api-codex-gnet-sub2api 下
backend\\internal\\server\\routes\\gateway.go:80/108/72 一手核实（POST /v1/videos 注册于 :107-109，
以 h.SoraClient != nil 为条件，非 feature flag）。
muse 无 Anthropic /v1/messages（docs/TROUBLESHOOTING.md:101）、codar 直连无 count_tokens，保持 501/未声明。
"""

from __future__ import annotations

import json
import tempfile
import threading
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from app.adapters import BUILDERS
from app.adapters.base import ProviderAdapter

pytestmark = pytest.mark.gateway

# ---------- 进程内构造全量适配器：能力位 ↔ 方法真值 ----------


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
        allow_private_upstreams=True,
    )


ADAPTERS = {
    adapter.spec.id: adapter
    for adapter in (
        build(_settings(Path(tempfile.mkdtemp(prefix="ext-endpoints-")))) for build in BUILDERS
    )
}

# 能力位 → 必须由子类真实覆写的方法；反向也成立：实现了就该声明
EXTENSION_CAPS: dict[str, tuple[str, ...]] = {
    "embeddings": ("embeddings",),
    "images": ("image_generation", "image_edit"),
    "rerank": ("rerank",),
    "videos": ("video_create",),
    "media": ("media",),
    "usage": ("usage",),
    "count_tokens": ("count_tokens",),
}
# 配套方法：实现即对应能力必须声明，但不强制声明者实现
# （sub2api 的 /v1/videos 与 images/edits 只有创建没有查询，读取端点回 501）
OPTIONAL_METHODS: dict[str, tuple[str, ...]] = {"images": ("image_task",), "videos": ("video_task",)}


def _real_method(adapter: Any, name: str) -> bool:
    impl = getattr(type(adapter), name, None)
    base = getattr(ProviderAdapter, name, None)
    return callable(impl) and impl is not base


def test_extension_capability_truth_in_both_directions() -> None:
    problems: list[str] = []
    for pid, adapter in sorted(ADAPTERS.items()):
        caps = adapter.spec.capabilities
        for cap, required in EXTENSION_CAPS.items():
            missing = [name for name in required if not _real_method(adapter, name)]
            if caps.get(cap) is True and missing:
                problems.append(f"{pid}: 声明 {cap}=True 却没有真实实现 {missing}")
            if caps.get(cap) is not True and not missing:
                problems.append(f"{pid}: 已实现 {list(required)} 却没声明 {cap}")
        for cap, optional in OPTIONAL_METHODS.items():
            built = [name for name in optional if _real_method(adapter, name)]
            if built and caps.get(cap) is not True:
                problems.append(f"{pid}: 实现 {built} 但 {cap} 未声明")
    assert not problems, problems


def test_declared_extension_caps_match_local_evidence() -> None:
    """声明必须等于本地证据：sub2api 六项（gateway.go 源码核实），直连 codar 补 count_tokens。"""
    declared = {
        pid: sorted(cap for cap in EXTENSION_CAPS if adapter.spec.capabilities.get(cap) is True)
        for pid, adapter in sorted(ADAPTERS.items())
        if any(adapter.spec.capabilities.get(cap) is True for cap in EXTENSION_CAPS)
    }
    assert declared == {
        "codebuddy": ["count_tokens"],
        "codar": ["count_tokens"],
        "codar_sidecar": ["count_tokens"],
        "muse": ["images", "media", "videos"],
        "sub2api": ["count_tokens", "embeddings", "images", "rerank", "usage", "videos"],
    }


def test_usage_for_ledger_never_lands_zero_usage_as_billed() -> None:
    from app.routers.v1 import _usage_for_ledger

    zero = {"prompt_tokens": 0, "completion_tokens": 0}
    assert _usage_for_ledger("embeddings", zero) is None
    assert _usage_for_ledger("count_tokens", None) is None
    # rerank 响应常见 usage:{total_tokens:N}，totals 口径仍是全零 → usage_missing
    assert _usage_for_ledger("rerank", {"total_tokens": 9}) is None
    # 有真实 token 才保留（embeddings 落 unpriced 而不是 usage_missing）
    real = {"prompt_tokens": 7, "completion_tokens": 0}
    assert _usage_for_ledger("embeddings", real) == real
    # 既有 chat/responses/messages 口径不动：全零 usage 仍走 usage_zero 原逻辑
    assert _usage_for_ledger("chat", zero) == zero


# ---------- 独立桩上游：只为本批新端点回标准体，并记录收到的请求 ----------

STUB_HITS: list[dict[str, str]] = []


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        STUB_HITS.append(
            {"method": "POST", "path": self.path, "body": raw.decode("utf-8", "ignore")}
        )
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        if self.path == "/v1/embeddings":
            self._json(200, {
                "object": "list",
                "model": body.get("model", ""),
                "data": [{"object": "embedding", "index": 0, "embedding": [0.5, 0.25]}],
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            })
        elif self.path == "/v1/images/generations":
            self._json(200, {"created": 1, "data": [{"url": "http://stub.local/gen.png"}]})
        elif self.path == "/v1/images/edits":
            self._json(200, {"created": 1, "data": [{"b64_json": "AA=="}]})
        elif self.path == "/v1/videos":
            self._json(200, {"id": "vt-9", "object": "video.task", "status": "queued"})
        elif self.path == "/v1/messages/count_tokens":
            self._json(200, {"input_tokens": 42})
        elif self.path == "/v1/rerank":
            self._json(200, {
                "model": body.get("model", ""),
                "results": [{"index": 0, "relevance_score": 0.92}],
                "usage": {"total_tokens": 8},
            })
        else:
            self._json(404, {"error": {"message": f"stub 未实现 {self.path}"}})

    def do_GET(self) -> None:
        STUB_HITS.append({"method": "GET", "path": self.path, "body": ""})
        if self.path == "/v1/videos/vt-9":
            self._json(200, {"id": "vt-9", "status": "succeeded", "url": "http://stub.local/v.mp4"})
        elif self.path == "/v1/images/tasks/it-9":
            self._json(200, {"id": "it-9", "status": "succeeded", "data": [{"url": "http://stub.local/gen.png"}]})
        elif self.path == "/v1/media/pic.png":
            raw = b"\x89PNG-stub-bytes"
            self.send_response(200)
            self.send_header("content-type", "image/png")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path.startswith("/v1/usage"):
            # 只回聚合数字：上游真实响应也无单次请求 usage
            self._json(200, {
                "mode": "unrestricted",
                "today": {"requests": 3, "input_tokens": 120, "output_tokens": 30,
                          "cache_read_tokens": 0, "cache_write_tokens": 0,
                          "total_tokens": 150, "total_cost_usd": 0.0021},
                "total": {"requests": 40, "input_tokens": 1500, "output_tokens": 400,
                          "cache_read_tokens": 0, "cache_write_tokens": 0,
                          "total_tokens": 1900, "total_cost_usd": 0.021},
            })
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
    created = admin.post("/api/keys", json={"name": "extension-endpoints"})
    assert created.status_code == 200, created.text
    return str(created.json()["key"])


@pytest.fixture
def sub2api_on_stub(admin: httpx.Client, stub_upstream: str) -> Iterator[None]:
    gid = f"g-ext-{uuid.uuid4().hex[:6]}"
    group = admin.post("/api/sub2api/groups", json={"id": gid, "name": "extension-endpoints"})
    assert group.status_code == 200, group.text
    account = admin.post(
        "/api/sub2api/accounts",
        json={
            "group_id": gid,
            "name": "a-ext",
            "upstream_base": stub_upstream,
            "api_key": "stub-key",
        },
    )
    assert account.status_code == 200, account.text
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "sub2api")
    patched = admin.patch(
        "/api/providers/sub2api", json={"enabled": True, "options": {"group": gid}}
    )
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


@pytest.fixture
def patch_provider_to_stub(admin: httpx.Client, stub_upstream: str):
    @contextmanager
    def _patch(provider: str) -> Iterator[None]:
        before = next(p for p in admin.get("/api/providers").json() if p["id"] == provider)
        patched = admin.patch(
            f"/api/providers/{provider}",
            json={"enabled": True, "base_url": stub_upstream, "api_key": "stub-key"},
        )
        assert patched.status_code == 200, patched.text
        try:
            yield
        finally:
            restored = admin.patch(
                f"/api/providers/{provider}",
                json={
                    "enabled": bool(before["enabled"]),
                    "base_url": before["base_url"],
                    "api_key": "***" if before["has_api_key"] else "",
                },
            )
            assert restored.status_code == 200, restored.text

    return _patch


@pytest.fixture
def muse_on_stub(patch_provider_to_stub) -> Iterator[None]:
    with patch_provider_to_stub("muse"):
        yield


@pytest.fixture
def codar_sidecar_on_stub(patch_provider_to_stub) -> Iterator[None]:
    with patch_provider_to_stub("codar_sidecar"):
        yield


def _auth(key: str, provider: str | None = None) -> dict[str, str]:
    headers = {"authorization": f"Bearer {key}"}
    if provider:
        headers["x-provider"] = provider
    return headers


def _credit_status(admin: httpx.Client, provider: str, model: str) -> str:
    items = admin.get("/api/logs", params={"provider": provider, "size": 25}).json()["items"]
    for row in items:
        if row["model"] == model:
            return str(row["credit_status"])
    raise AssertionError(f"request_logs 里没有 {provider}/{model} 的记录")


# ---------- 路由存在性 / 鉴权复用：新端点全部走 /v1 同一套 key 鉴权 ----------


def test_new_endpoints_enforce_auth(anon: httpx.Client) -> None:
    for path in (
        "/v1/embeddings",
        "/v1/images/generations",
        "/v1/images/edits",
        "/v1/videos",
        "/v1/messages/count_tokens",
        "/v1/rerank",
    ):
        r = anon.post(path, json={"model": "fake-chat-1", "input": "hi"})
        assert r.status_code == 401, (path, r.status_code, r.text)
    for path in ("/v1/images/tasks/it-9", "/v1/videos/vt-9", "/v1/media/pic.png", "/v1/usage"):
        r = anon.get(path)
        assert r.status_code == 401, (path, r.status_code, r.text)


def test_extension_capability_gate_rejects_without_capability(anon: httpx.Client, bearer_key: str) -> None:
    # codebuddy 有 count_tokens，但没有 embeddings/videos/rerank/usage：这些仍必须明确 501
    embeddings = anon.post(
        "/v1/embeddings", headers=_auth(bearer_key, "codebuddy"), json={"model": "fake-chat-1", "input": "hi"}
    )
    assert embeddings.status_code == 501, embeddings.text
    error = embeddings.json()["error"]
    assert error["code"] == "capability_unsupported"
    assert error["provider"] == "codebuddy"

    video = anon.get("/v1/videos/vt-9", headers=_auth(bearer_key, "codebuddy"))
    assert video.status_code == 501, video.text
    assert video.json()["error"]["code"] == "capability_unsupported"

    rerank = anon.post(
        "/v1/rerank",
        headers=_auth(bearer_key, "codebuddy"),
        json={"model": "fake-chat-1", "query": "hi", "documents": ["a"]},
    )
    assert rerank.status_code == 501, rerank.text
    assert rerank.json()["error"]["code"] == "capability_unsupported"
    assert rerank.json()["error"]["provider"] == "codebuddy"

    usage = anon.get("/v1/usage", headers=_auth(bearer_key, "codebuddy"))
    assert usage.status_code == 501, usage.text
    assert usage.json()["error"]["code"] == "capability_unsupported"


# ---------- sub2api：转发实现 ----------


def test_embeddings_forwards_and_lands_unpriced(
    anon: httpx.Client, admin: httpx.Client, bearer_key: str, sub2api_on_stub: None
) -> None:
    STUB_HITS.clear()
    r = anon.post(
        "/v1/embeddings",
        headers=_auth(bearer_key, "sub2api"),
        json={"model": "fake-chat-1", "input": "hello", "stream": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["usage"]["total_tokens"] == 7
    hit = STUB_HITS[-1]
    assert hit["method"] == "POST" and hit["path"] == "/v1/embeddings"
    assert '"stream"' not in hit["body"], "非流式端点不得把客户端 stream 透传给上游"
    # token 有真实用量但模型无价行：unpriced，不是 0 元 billed
    assert _credit_status(admin, "sub2api", "fake-chat-1") == "unpriced"


def test_count_tokens_forwards_on_sub2api_and_sidecar(
    anon: httpx.Client, admin: httpx.Client, bearer_key: str, sub2api_on_stub: None
) -> None:
    payload = {"model": "fake-chat-1", "messages": [{"role": "user", "content": "hello"}]}
    STUB_HITS.clear()
    r = anon.post(
        "/v1/messages/count_tokens", headers=_auth(bearer_key, "sub2api"), json=payload
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"input_tokens": 42}
    assert STUB_HITS[-1]["path"] == "/v1/messages/count_tokens"
    # 计数端点只返回 input_tokens，没有 usage：usage_missing，不按 0 计费
    assert _credit_status(admin, "sub2api", "fake-chat-1") == "usage_missing"


def test_count_tokens_forwards_on_codar_sidecar(
    anon: httpx.Client, bearer_key: str, codar_sidecar_on_stub: None
) -> None:
    STUB_HITS.clear()
    r = anon.post(
        "/v1/messages/count_tokens",
        headers=_auth(bearer_key, "codar_sidecar"),
        json={"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"input_tokens": 42}
    assert STUB_HITS[-1]["path"] == "/v1/messages/count_tokens"


def test_images_generations_and_edits_forward(
    anon: httpx.Client, admin: httpx.Client, bearer_key: str, sub2api_on_stub: None
) -> None:
    STUB_HITS.clear()
    gen = anon.post(
        "/v1/images/generations",
        headers=_auth(bearer_key, "sub2api"),
        json={"model": "fake-chat-1", "prompt": "a cat"},
    )
    assert gen.status_code == 200, gen.text
    assert gen.json()["data"][0]["url"] == "http://stub.local/gen.png"
    assert STUB_HITS[-1]["path"] == "/v1/images/generations"
    # 图片响应没有 usage：usage_missing 而不是 billed(0)
    assert _credit_status(admin, "sub2api", "fake-chat-1") == "usage_missing"

    edit = anon.post(
        "/v1/images/edits",
        headers=_auth(bearer_key, "sub2api"),
        json={"model": "fake-chat-1", "prompt": "make it blue", "image": "aW1n"},
    )
    assert edit.status_code == 200, edit.text
    assert edit.json()["data"][0]["b64_json"] == "AA=="
    assert STUB_HITS[-1]["path"] == "/v1/images/edits"

    # multipart 变体明确 415，不静默丢字段
    multipart = anon.post(
        "/v1/images/edits",
        headers={
            **_auth(bearer_key, "sub2api"),
            "content-type": "multipart/form-data; boundary=stub",
        },
        content=b"--stub\r\ncontent-disposition: form-data; name=\"image\"\r\n\r\nxx\r\n--stub--\r\n",
    )
    assert multipart.status_code == 415, multipart.text
    assert multipart.json()["error"]["code"] == "unsupported_media_type"


def test_rerank_forwards_and_lands_usage_missing(
    anon: httpx.Client, admin: httpx.Client, bearer_key: str, sub2api_on_stub: None
) -> None:
    STUB_HITS.clear()
    r = anon.post(
        "/v1/rerank",
        headers=_auth(bearer_key, "sub2api"),
        json={"model": "fake-chat-1", "query": "best model", "documents": ["a", "b"], "stream": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["relevance_score"] == 0.92
    hit = STUB_HITS[-1]
    assert hit["path"] == "/v1/rerank"
    assert '"stream"' not in hit["body"], "非流式端点不得把客户端 stream 透传给上游"
    # 上游 usage 只有 total_tokens（totals 口径为全零）：usage_missing，绝不 billed(0)
    assert _credit_status(admin, "sub2api", "fake-chat-1") == "usage_missing"


def test_usage_forwards_aggregate_and_lands_usage_missing(
    anon: httpx.Client, admin: httpx.Client, bearer_key: str, sub2api_on_stub: None
) -> None:
    STUB_HITS.clear()
    r = anon.get(
        "/v1/usage?start_date=2026-01-01&end_date=2026-01-31",
        headers=_auth(bearer_key, "sub2api"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["today"]["requests"] == 3
    assert STUB_HITS[-1]["method"] == "GET"
    # 日期窗口原样透传给上游聚合统计
    assert STUB_HITS[-1]["path"] == "/v1/usage?start_date=2026-01-01&end_date=2026-01-31"
    # 响应只有聚合数字，没有单次请求 usage：usage_missing（GET 记账 usage=None）
    assert _credit_status(admin, "sub2api", "") == "usage_missing"


def test_sub2api_videos_create_and_unimplemented_reads_answer_501(
    anon: httpx.Client, admin: httpx.Client, bearer_key: str, sub2api_on_stub: None
) -> None:
    STUB_HITS.clear()
    created = anon.post(
        "/v1/videos",
        headers=_auth(bearer_key, "sub2api"),
        json={"model": "fake-chat-1", "prompt": "a spinning cube"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["id"] == "vt-9"
    assert STUB_HITS[-1]["path"] == "/v1/videos"
    assert _credit_status(admin, "sub2api", "fake-chat-1") == "usage_missing"

    # 上游只注册了 POST /v1/videos（gateway.go:107-109）：查询明确 501 而不是 500
    task = anon.get("/v1/videos/vt-9", headers=_auth(bearer_key, "sub2api"))
    assert task.status_code == 501, task.text
    error = task.json()["error"]
    assert error["code"] == "capability_unsupported"
    assert error["provider"] == "sub2api"

    # images 同理：声明了创建但没有任务查询，读取端点也必须 501
    images_task = anon.get("/v1/images/tasks/it-9", headers=_auth(bearer_key, "sub2api"))
    assert images_task.status_code == 501, images_task.text
    assert images_task.json()["error"]["code"] == "capability_unsupported"


# ---------- muse：images / videos / media 透传 ----------


def test_muse_images_videos_media_flow(
    anon: httpx.Client, admin: httpx.Client, bearer_key: str, muse_on_stub: None
) -> None:
    STUB_HITS.clear()
    gen = anon.post(
        "/v1/images/generations",
        headers=_auth(bearer_key, "muse"),
        json={"model": "muse-image", "prompt": "a red cube"},
    )
    assert gen.status_code == 200, gen.text
    assert STUB_HITS[-1]["path"] == "/v1/images/generations"
    assert _credit_status(admin, "muse", "muse-image") == "usage_missing"

    task = anon.get("/v1/images/tasks/it-9", headers=_auth(bearer_key, "muse"))
    assert task.status_code == 200, task.text
    assert task.json()["status"] == "succeeded"

    video = anon.post(
        "/v1/videos",
        headers=_auth(bearer_key, "muse"),
        json={"model": "muse-video", "prompt": "a cube spinning"},
    )
    assert video.status_code == 200, video.text
    assert video.json()["id"] == "vt-9"
    assert STUB_HITS[-1]["path"] == "/v1/videos"
    assert _credit_status(admin, "muse", "muse-video") == "usage_missing"

    polled = anon.get("/v1/videos/vt-9", headers=_auth(bearer_key, "muse"))
    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == "succeeded"

    media = anon.get("/v1/media/pic.png", headers=_auth(bearer_key, "muse"))
    assert media.status_code == 200, media.text
    assert media.headers["content-type"] == "image/png"
    assert media.content == b"\x89PNG-stub-bytes"
    # 读取端点没有 model：同样落 usage_missing，不进 billed
    assert _credit_status(admin, "muse", "") == "usage_missing"


def test_images_task_poll_requires_x_provider_when_ambiguous(
    anon: httpx.Client, bearer_key: str, sub2api_on_stub: None, muse_on_stub: None
) -> None:
    # sub2api 与 muse 都声明 images：GET 无 model 可解析，多候选必须显式消歧
    r = anon.get("/v1/images/tasks/it-9", headers=_auth(bearer_key))
    assert r.status_code == 400, r.text
    error = r.json()["error"]
    assert error["code"] == "ambiguous_provider"
    assert "x-provider" in error["message"]

    picked = anon.get("/v1/images/tasks/it-9", headers=_auth(bearer_key, "muse"))
    assert picked.status_code == 200, picked.text


# ---------- 鉴权/配额复用 ----------


def test_extension_endpoint_reuses_quota_guard(
    anon: httpx.Client, admin: httpx.Client, sub2api_on_stub: None
) -> None:
    created = admin.post("/api/keys", json={"name": f"ext-quota-{uuid.uuid4().hex[:8]}"})
    assert created.status_code == 200, created.text
    key = created.json()
    policy = admin.post(
        "/api/quota/policies", json={"key_id": key["id"], "window": "day", "limit_requests": 1}
    )
    assert policy.status_code == 200, policy.text
    try:
        first = anon.post(
            "/v1/embeddings",
            headers=_auth(str(key["key"]), "sub2api"),
            json={"model": "fake-chat-1", "input": "a"},
        )
        assert first.status_code == 200, first.text

        second = anon.post(
            "/v1/embeddings",
            headers=_auth(str(key["key"]), "sub2api"),
            json={"model": "fake-chat-1", "input": "b"},
        )
        assert second.status_code == 429, second.text
        error = second.json()["error"]
        assert error["code"] == "quota_exceeded"
        assert error["type"] == "insufficient_quota"
        assert second.headers.get("retry-after") is not None
    finally:
        admin.delete(f"/api/quota/policies/{policy.json()['id']}")


def test_extension_capabilities_visible_on_provider_list(
    admin: httpx.Client,
) -> None:
    providers = {item["id"]: item for item in admin.get("/api/providers").json()}
    sub2api_caps = providers["sub2api"]["capabilities"]
    assert sub2api_caps["embeddings"] is True
    assert sub2api_caps["images"] is True
    assert sub2api_caps["count_tokens"] is True
    muse_caps = providers["muse"]["capabilities"]
    assert muse_caps["videos"] is True
    assert muse_caps["media"] is True
    # 源码核实过的三项（gateway.go:80/108/72）能力位对客户端外显
    assert sub2api_caps["rerank"] is True
    assert sub2api_caps["videos"] is True
    assert sub2api_caps["usage"] is True
