"""能力声明 vs 实际实现的真值审计。

断言方向：
- 声明 True 的能力格必须由子类真实覆写（协议方法 / 账号方法 / 模型目录来源）；
- 声明与实现不符的已知假能力逐格 xfail，修复后自动 XPASS；
- 「实现」格用 mock transport 验证真实外呼的路径与 header，「转发」格验证打到协议正确的上游路径。
"""

from __future__ import annotations

import inspect
import json
import re
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator

import httpx
import pytest

from app.adapters import BUILDERS
from app.adapters.base import ProviderAdapter, ProviderError
from app.adapters.client import UnifiedApiClient
from app.adapters.codar import ANTHROPIC_VERSION
from app.adapters.qoder import QoderAdapter
from app.routers.accounts import _account_ops, _implements

pytestmark = pytest.mark.providers

# ---------- 构造：全量 14 家适配器（模块级，供参数化与静态断言复用） ----------


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
        allow_private_upstreams=False,
    )


_MODULE_SETTINGS = _settings(Path(tempfile.mkdtemp(prefix="cap-truth-")))
BUILD_BY_ID: dict[str, Any] = {}
ADAPTERS: dict[str, Any] = {}
for _build in BUILDERS:
    _adapter = _build(_MODULE_SETTINGS)
    BUILD_BY_ID[_adapter.spec.id] = _build
    ADAPTERS[_adapter.spec.id] = _adapter


def _fresh(pid: str) -> Any:
    return BUILD_BY_ID[pid](_MODULE_SETTINGS)


def _real_method(adapter: Any, name: str) -> bool:
    impl = getattr(type(adapter), name, None)
    base = getattr(ProviderAdapter, name, None)
    return callable(impl) and impl is not base


def test_every_forwarding_provider_declares_client_user_agent() -> None:
    """裸 HTTP 外呼的每家都必须有明确 UA 常量，禁止退回 httpx 默认 UA。"""
    forwarding = [pid for pid in ADAPTERS if pid != "grok-register"]
    assert len(forwarding) == 13
    for pid in forwarding:
        user_agent = ADAPTERS[pid].spec.user_agent
        assert user_agent, f"{pid} 缺 user_agent"
        assert "python-httpx" not in user_agent.lower(), f"{pid} 退回默认 UA：{user_agent}"
    assert ADAPTERS["gptgrok"].spec.user_agent.startswith("Mozilla/5.0"), "gptgrok 必须保留 Chrome UA"


def test_trae_retry_after_is_bounded() -> None:
    """上游给巨大 Retry-After 时必须放弃重试，不能把工作线程占死。"""

    class _Reply:
        def __init__(self, value: str) -> None:
            self.headers = {"retry-after": value}

    adapter = _fresh("trae")
    assert adapter._retry_wait(_Reply("7")) is None, "7s 已超过默认 5s 阈值，应直接按 429 返回"
    assert adapter._retry_wait(_Reply("3")) == 3.0
    adapter.configure(options={"retry_after_max_sec": "10"})
    assert adapter._retry_wait(_Reply("7")) == 7.0, "阈值可配置后再允许 7s"


# 能力位 → (匹配模式, 方法名组)：any=任一覆写即真，all=必须全部覆写
CAP_RULES: dict[str, tuple[str, tuple[str, ...]]] = {
    "chat": ("any", ("chat_completion",)),
    "messages": ("any", ("messages",)),
    "responses": ("any", ("responses",)),
    "search": ("any", ("search",)),
    "models": ("any", ("fetch_models", "declared_models")),
    "oauth": ("all", ("start_login", "poll_login")),
    "checkin": ("all", ("checkin_all", "account_action")),
    "credits": ("all", ("refresh_credits",)),
    # 扩展位与主链路同口径：声明 True 必须有真实覆写
    "embeddings": ("any", ("embeddings",)),
    "images": ("all", ("image_generation", "image_edit")),
    "videos": ("any", ("video_create",)),
    "media": ("any", ("media",)),
    "rerank": ("any", ("rerank",)),
    "usage": ("any", ("usage",)),
    "count_tokens": ("any", ("count_tokens",)),
}

# 扩展位（与 base.CAPABILITY_KEYS 的主链路位并列，声明散落在各 adapter 的 capabilities 里）
EXTENSION_CAPS = frozenset({"embeddings", "images", "videos", "media", "rerank", "usage", "count_tokens"})

# 扩展位全是透传端点：覆写方法体必须真有上游外呼，空壳覆写不算实现。
# sidecar 型 adapter 的扩展位（如 codar_sidecar 的 count_tokens）属于「经 sidecar 转发、
# 本进程不实现业务逻辑」的合法形态，判据是方法体里有 forward/_forward/_forward_with_tokens/_send 调用，
# 而不是要求本地重写一遍算法。
UPSTREAM_CALL_MARKERS = (".forward(", "_forward(", "_forward_with_tokens(", "_send(")

# 读端点：images/videos 位同时放行 POST 创建与 GET 查询（routers/v1.py 的 _proxy_get）
READ_ENDPOINTS: dict[str, str] = {"images": "image_task", "videos": "video_task"}
# 上游只注册创建、没有查询端点的合法情形：必须显式登记，并单独用网关行为测试背书 501
READ_ENDPOINT_EXEMPT: dict[tuple[str, str, str], str] = {
    ("sub2api", "images", "image_task"): "上游只注册 POST /v1/images/generations，无任务查询端点",
    ("sub2api", "videos", "video_task"): "上游只注册 POST /v1/videos（gateway.go:107-109），无任务查询端点",
}

# 扩展位转发路径的静态锚点：(provider, 方法) -> (匹配方式, 期望路径)
# exact = 能从方法体解出完整路径；prefix = 路径由常量 + 变量拼接，只校验前缀常量
EXTENSION_STATIC_PATHS: dict[tuple[str, str], tuple[str, str]] = {
    ("muse", "image_generation"): ("exact", "/v1/images/generations"),
    ("muse", "image_edit"): ("exact", "/v1/images/edits"),
    ("muse", "video_create"): ("exact", "/v1/videos"),
    ("muse", "media"): ("prefix", "/v1/media"),
    ("muse", "image_task"): ("prefix", "/v1/images/tasks"),
    ("muse", "video_task"): ("prefix", "/v1/videos"),
    ("sub2api", "embeddings"): ("exact", "/v1/embeddings"),
    ("sub2api", "image_generation"): ("exact", "/v1/images/generations"),
    ("sub2api", "image_edit"): ("exact", "/v1/images/edits"),
    ("sub2api", "video_create"): ("exact", "/v1/videos"),
    ("sub2api", "rerank"): ("exact", "/v1/rerank"),
    ("sub2api", "count_tokens"): ("exact", "/v1/messages/count_tokens"),
    ("sub2api", "usage"): ("prefix", "/v1/usage"),
    ("codebuddy", "count_tokens"): ("exact", "/v1/messages/count_tokens"),
    ("codar", "count_tokens"): ("exact", "/v1/messages/count_tokens"),
    ("codar_sidecar", "count_tokens"): ("exact", "/v1/messages/count_tokens"),
}

# 静态规则确实覆盖不到、只由桩上游实测覆盖的方法；当前没有，出现时必须在这里写明原因，
# 覆盖率测试会逼着新方法二选一（补锚点或登记豁免），不允许静默漏检
STATIC_PATH_EXEMPT: dict[tuple[str, str], str] = {}


def _has_upstream_call(adapter: Any, name: str) -> bool:
    impl = getattr(type(adapter), name, None)
    if impl is None:
        return False
    try:
        source = inspect.getsource(impl)
    except (OSError, TypeError):
        return False
    return any(marker in source for marker in UPSTREAM_CALL_MARKERS)


def _path_evidence(adapter: Any, name: str) -> set[str]:
    """静态收集方法体里的上游路径：字面量、模块级 *_PATH 常量、_FORWARD_PATHS 键映射。"""
    impl = getattr(type(adapter), name, None)
    if impl is None:
        return set()
    try:
        source = inspect.getsource(impl)
    except (OSError, TypeError):
        return set()
    module = sys.modules.get(type(adapter).__module__)
    evidence = set(re.findall(r'"(/v1[^"]*)"', source))
    for const in re.findall(r"\b([A-Z][A-Z0-9_]*_PATH)\b", source):
        value = getattr(module, const, None) if module else None
        if isinstance(value, str):
            evidence.add(value)
    table = getattr(module, "_FORWARD_PATHS", {}) if module else {}
    for key in re.findall(r'_forward\(\s*"([^"]+)"', source):
        if key in table:
            evidence.add(table[key])
    return evidence


def _declared_pairs() -> list[pytest.ParameterSet]:
    return [
        pytest.param(pid, cap, id=f"{pid}-{cap}")
        for pid, adapter in sorted(ADAPTERS.items())
        for cap, on in sorted(adapter.spec.capabilities.items())
        if on and cap in CAP_RULES
    ]


# ---------- 上游 mock：接管 UnifiedApiClient 的共享 client ----------


@asynccontextmanager
async def _upstream(
    handler: Callable[[httpx.Request], httpx.Response], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://mock.local",
        trust_env=False,
    )
    monkeypatch.setattr(UnifiedApiClient, "_client", lambda self, *args, **kwargs: client)
    try:
        yield
    finally:
        await client.aclose()


def _json_recorder(recorded: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json={"ok": True})

    return handler


def _sse_recorder(recorded: list[httpx.Request], body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return handler


def _chat_payload(stream: bool = False) -> dict[str, Any]:
    return {"model": "demo", "messages": [{"role": "user", "content": "hi"}], "stream": stream}


def _messages_payload(stream: bool = False) -> dict[str, Any]:
    return {
        "model": "demo",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
        "stream": stream,
    }


def _responses_payload(stream: bool = False) -> dict[str, Any]:
    return {"model": "demo", "input": "ping", "stream": stream}


# ---------- 1. 声明 True ⟹ 方法体真实（全量参数化，已知假能力 xfail） ----------


@pytest.mark.parametrize("pid,cap", _declared_pairs())
def test_declared_capabilities_have_real_method_bodies(pid: str, cap: str) -> None:
    mode, names = CAP_RULES[cap]
    adapter = ADAPTERS[pid]
    missing = {name for name in names if not _real_method(adapter, name)}
    if mode == "all":
        assert not missing, f"{pid} 声明 {cap}=True，但未覆写: {sorted(missing)}"
    else:
        assert not missing == set(names), f"{pid} 声明 {cap}=True，但方法全部未覆写: {sorted(missing)}"
    if cap in EXTENSION_CAPS and mode == "all":
        hollow = {name for name in names if _real_method(adapter, name) and not _has_upstream_call(adapter, name)}
        assert not hollow, f"{pid} 声明 {cap}=True，但覆写没有真实外呼: {sorted(hollow)}"
    elif cap in EXTENSION_CAPS:
        assert any(_has_upstream_call(adapter, name) for name in names), (
            f"{pid} 声明 {cap}=True，但没有任何真实外呼的覆写"
        )


# ---------- 1b. sidecar 转发型扩展位：允许本进程只转发，但必须是真外呼 ----------


def test_sidecar_forwarded_extension_cap_counts_as_real_implementation() -> None:
    # codar_sidecar 的 count_tokens 业务在 sidecar 进程里，主仓只做 HTTP 透传；
    # 这类「本进程不实现」不是缺陷，判据落到真实转发调用上
    adapter = ADAPTERS["codar_sidecar"]
    assert adapter.spec.status == "sidecar"
    assert adapter.spec.capabilities["count_tokens"] is True
    assert _real_method(adapter, "count_tokens")
    assert _has_upstream_call(adapter, "count_tokens")


# ---------- 1c. 读端点：能力位 ⇔ 真转发 / 501 兜底 ----------


def test_extension_read_endpoint_static_truth() -> None:
    problems: list[str] = []
    for pid, adapter in sorted(ADAPTERS.items()):
        for cap, read_method in READ_ENDPOINTS.items():
            if adapter.spec.capabilities.get(cap) is not True:
                continue
            implemented = _real_method(adapter, read_method)
            exempt = (pid, cap, read_method) in READ_ENDPOINT_EXEMPT
            if implemented and not _has_upstream_call(adapter, read_method):
                problems.append(f"{pid}.{read_method}: 有覆写但没有真实外呼")
            if not implemented and not exempt:
                problems.append(f"{pid}.{read_method}: 声明 {cap}=True 但读端点无实现，也未登记 501 豁免")
            if implemented and exempt:
                problems.append(f"{pid}.{read_method}: 已有真实实现，却仍登记为 501 豁免")
    assert not problems, problems


_EXT_READ_HITS: list[tuple[str, str]] = []


class _ExtReadHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        _EXT_READ_HITS.append(("GET", self.path))
        if self.path.startswith("/v1/images/tasks/"):
            self._json(200, {"id": self.path.rsplit("/", 1)[-1], "status": "succeeded"})
        elif self.path.startswith("/v1/videos/"):
            self._json(200, {"id": self.path.rsplit("/", 1)[-1], "status": "succeeded"})
        elif self.path.startswith("/v1/media/"):
            raw = b"\x89PNG-stub-bytes"
            self.send_response(200)
            self.send_header("content-type", "image/png")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self._json(404, {"error": {"message": f"stub 未实现 {self.path}"}})


@pytest.fixture(scope="module")
def ext_read_stub() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ExtReadHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _provider_get(anon: httpx.Client, key: str, provider: str, path: str) -> httpx.Response:
    return anon.get(path, headers={"authorization": f"Bearer {key}", "x-provider": provider})


def test_extension_read_endpoints_forward_or_501(
    anon: httpx.Client, admin: httpx.Client, bearer_key: str, ext_read_stub: str
) -> None:
    # muse 两个读端点都有真实覆写：必须真转发，不能空响应
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "muse")
    patched = admin.patch(
        "/api/providers/muse",
        json={"enabled": True, "base_url": ext_read_stub, "api_key": "muse-read-key"},
    )
    assert patched.status_code == 200, patched.text
    try:
        _EXT_READ_HITS.clear()
        for path in ("/v1/images/tasks/it-9", "/v1/videos/vt-9"):
            reply = _provider_get(anon, bearer_key, "muse", path)
            assert reply.status_code == 200, f"{path} {reply.status_code} {reply.text}"
            assert reply.json()["status"] == "succeeded"
            assert _EXT_READ_HITS[-1] == ("GET", path)
    finally:
        restored = admin.patch(
            "/api/providers/muse",
            json={
                "enabled": bool(before["enabled"]),
                "base_url": before["base_url"],
                "api_key": "***" if before["has_api_key"] else "",
            },
        )
        assert restored.status_code == 200, restored.text

    # sub2api 只有创建没有查询：读端点必须 501 capability_unsupported，不许 500/空响应
    gid = "g-read-truth"
    group = admin.post("/api/sub2api/groups", json={"id": gid, "name": "read-endpoint-truth"})
    assert group.status_code == 200, group.text
    account = admin.post(
        "/api/sub2api/accounts",
        json={"group_id": gid, "name": "a-read", "upstream_base": ext_read_stub, "api_key": "stub-key"},
    )
    assert account.status_code == 200, account.text
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "sub2api")
    patched = admin.patch("/api/providers/sub2api", json={"enabled": True, "options": {"group": gid}})
    assert patched.status_code == 200, patched.text
    try:
        for path in ("/v1/images/tasks/it-9", "/v1/videos/vt-9"):
            reply = _provider_get(anon, bearer_key, "sub2api", path)
            assert reply.status_code == 501, f"{path} {reply.status_code} {reply.text}"
            error = reply.json()["error"]
            assert error["code"] == "capability_unsupported"
            assert error["provider"] == "sub2api"
            assert error["message"]
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


# ---------- 1d. 转发路径静态锚点（对不上表就红，覆盖不到必须显式登记） ----------


@pytest.mark.parametrize(
    "pid,method,mode,expected",
    [
        pytest.param(pid, method, mode, expected, id=f"{pid}-{method}")
        for (pid, method), (mode, expected) in sorted(EXTENSION_STATIC_PATHS.items())
    ],
)
def test_extension_forward_path_static_anchor(pid: str, method: str, mode: str, expected: str) -> None:
    evidence = _path_evidence(ADAPTERS[pid], method)
    if mode == "exact":
        assert expected in evidence, f"{pid}.{method} 路径证据 {sorted(evidence)} 不含 {expected}"
    else:
        assert any(item == expected or item.startswith(expected) for item in evidence), (
            f"{pid}.{method} 拼接路径前缀 {sorted(evidence)} 与 {expected} 不符"
        )


def test_extension_forward_path_coverage_is_explicit() -> None:
    uncovered: list[str] = []
    for pid, adapter in sorted(ADAPTERS.items()):
        for cap, (_, names) in CAP_RULES.items():
            if cap not in EXTENSION_CAPS or adapter.spec.capabilities.get(cap) is not True:
                continue
            for name in names:
                if (
                    _real_method(adapter, name)
                    and (pid, name) not in EXTENSION_STATIC_PATHS
                    and (pid, name) not in STATIC_PATH_EXEMPT
                ):
                    uncovered.append(f"{pid}.{name}")
            read = READ_ENDPOINTS.get(cap)
            if (
                read
                and _real_method(adapter, read)
                and (pid, read) not in EXTENSION_STATIC_PATHS
                and (pid, read) not in STATIC_PATH_EXEMPT
            ):
                uncovered.append(f"{pid}.{read}")
    assert not uncovered, (
        f"以下扩展位方法没有静态路径锚点：{uncovered}；要么补 EXTENSION_STATIC_PATHS，"
        "要么在 STATIC_PATH_EXEMPT 写明只由桩上游实测覆盖"
    )


# ---------- 2. _implements() 判据可靠性（基类默认实现不得误判为已实现） ----------


def test_implements_judgement_rejects_base_defaults() -> None:
    # 基类有默认实现、子类未覆写 → False（getattr 陷阱）
    assert _implements(ADAPTERS["muse"], "delete_account") is False
    assert _implements(ADAPTERS["grok-register"], "sync_accounts") is False
    # 基类不存在的方法：未覆写 → False；覆写 → True
    assert _implements(ADAPTERS["trae_sidecar"], "refresh_credits") is False
    assert _implements(ADAPTERS["codebuddy"], "refresh_credits") is True
    # 真实覆写（含继承中间层的覆写）→ True
    assert _implements(ADAPTERS["codebuddy"], "start_login") is True
    assert _implements(ADAPTERS["qoder_sidecar"], "messages") is True
    assert _implements(ADAPTERS["qoder_sidecar"], "fetch_models") is True


# ---------- 3. /api/accounts/capabilities 输出 = 覆写真值 ----------


def test_account_ops_endpoint_matches_actual_overrides(admin: httpx.Client) -> None:
    response = admin.get("/api/accounts/capabilities")
    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()["providers"]}
    assert set(rows) == set(ADAPTERS)
    for pid, adapter in ADAPTERS.items():
        ops = rows[pid]["ops"]
        # 服务进程输出与本进程按同一覆写事实计算的结果必须一致
        assert ops == _account_ops(adapter), pid
        # ops=True 的每一格背后必须是真实方法体
        if ops["oauth"]:
            assert _real_method(adapter, "start_login") and _real_method(adapter, "poll_login"), pid
        if ops["credits"]:
            assert _real_method(adapter, "refresh_credits"), pid
        if ops["list"]:
            assert _real_method(adapter, "sync_accounts"), pid
        if ops["delete"]:
            assert _real_method(adapter, "delete_account"), pid
        if ops["restart"]:
            assert _real_method(adapter, "restart"), pid


# ---------- 4. trae_sidecar 声明位为 False，三接口口径一致 ----------


def test_trae_sidecar_declared_false_and_interfaces_agree(admin: httpx.Client) -> None:
    caps = ADAPTERS["trae_sidecar"].spec.capabilities
    assert caps.get("oauth") is False
    assert caps.get("credits") is False
    # /api/providers：按声明位透出
    providers = {item["id"]: item for item in admin.get("/api/providers").json()}
    pcaps = providers["trae_sidecar"]["capabilities"]
    assert pcaps["oauth"] is False
    assert pcaps["credits"] is False
    assert providers["trae_sidecar"]["limitations"] == {}
    # /api/accounts/capabilities：按覆写计算的 ops 说 False
    rows = {row["id"]: row for row in admin.get("/api/accounts/capabilities").json()["providers"]}
    ops = rows["trae_sidecar"]["ops"]
    assert ops["oauth"] is False
    assert ops["credits"] is False
    # /api/models：provider 状态不得出现 oauth/credits 为 True 的残留
    models = admin.get("/api/models", params={"provider": "trae_sidecar"}).json()["models"]
    for row in models:
        state = row["provider_states"]["trae_sidecar"]
        assert state["limitations"] == {}
        assert "oauth" not in state and "credits" not in state


# ---------- 5. deepseek 流式限制：显式声明，且 501 与限制同一句话 ----------


async def test_deepseek_stream_limitation_is_declared_and_501(admin: httpx.Client) -> None:
    from app.adapters.deepseek_sidecar import MESSAGES_STREAM_LIMIT
    from app.model_catalog import _state

    adapter = _fresh("deepseek_sidecar")
    # 非流式是真转发，messages 保留 True；流式限制必须挂在 ProviderSpec 上可见
    assert adapter.spec.capabilities["messages"] is True
    assert adapter.spec.limitations.get("messages_stream") == MESSAGES_STREAM_LIMIT
    # /api/providers 与 /api/models 的 provider 状态都透出同一句话
    providers = {item["id"]: item for item in admin.get("/api/providers").json()}
    assert providers["deepseek_sidecar"]["limitations"]["messages_stream"] == MESSAGES_STREAM_LIMIT
    state = _state(adapter, True, "declared", "")
    assert state["limitations"]["messages_stream"] == MESSAGES_STREAM_LIMIT
    models = admin.get("/api/models", params={"provider": "deepseek_sidecar"}).json()["models"]
    for row in models:
        assert row["provider_states"]["deepseek_sidecar"]["limitations"] == dict(adapter.spec.limitations)
    # 501 与限制声明同一句话
    with pytest.raises(ProviderError) as excinfo:
        await adapter.messages(_messages_payload(stream=True), stream=True)
    assert excinfo.value.status_code == 501
    assert excinfo.value.message == MESSAGES_STREAM_LIMIT


# ---------- 6. codebuddy 账号能力：真实 HTTP 路径与非空拉取 ----------


async def test_codebuddy_account_ops_hit_real_http_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _fresh("codebuddy")
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        path = request.url.path
        if path == "/api/accounts" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {"uid": "1001", "file": "1001.json", "nickname": "Ada", "realm": "cn"}
                    ],
                    "pool_available": True,
                },
            )
        if path == "/api/auth/start":
            return httpx.Response(
                200, json={"authUrl": "http://qr.local/login", "state": "st-9", "realm": "cn"}
            )
        return httpx.Response(200, json={"ok": True})

    async with _upstream(handler, monkeypatch):
        page = await adapter.sync_accounts()
        login = await adapter.start_login()
        await adapter.poll_login(login["state"])
        await adapter.checkin_all()
        await adapter.refresh_credits()
        await adapter.restart()
        await adapter.delete_account("1001.json")

    # sync_accounts 必须真拉取：解析出非空账号页，而不是空列表当成功
    assert page.accounts and page.accounts[0]["uid"] == "1001"
    assert login["authUrl"] == "http://qr.local/login"
    assert [(r.method, r.url.path) for r in recorded] == [
        ("GET", "/api/accounts"),
        ("POST", "/api/auth/start"),
        ("GET", "/api/auth/poll"),
        ("POST", "/api/accounts/checkin-all"),
        ("POST", "/api/accounts/refresh-credits"),
        ("POST", "/api/restart"),
        ("DELETE", "/api/accounts/1001.json"),
    ]


# ---------- 7. 转发格：打到协议正确的上游路径与 header ----------


FORWARD_CASES = [
    pytest.param("codebuddy", "chat_completion", _chat_payload(), False, "/v1/chat/completions", False, id="codebuddy-chat"),
    pytest.param("codebuddy", "messages", _messages_payload(), False, "/v1/messages", True, id="codebuddy-messages"),
    pytest.param("codebuddy", "responses", _responses_payload(), False, "/v1/responses", False, id="codebuddy-responses"),
    pytest.param("codebuddy", "count_tokens", _messages_payload(), False, "/v1/messages/count_tokens", True, id="codebuddy-count-tokens"),
    pytest.param("codar", "chat_completion", _chat_payload(), False, "/v1/chat/completions", False, id="codar-chat"),
    pytest.param("codar", "messages", _messages_payload(), False, "/v1/messages", True, id="codar-messages"),
    pytest.param("codar", "responses", _responses_payload(), False, "/v1/responses", False, id="codar-responses"),
    pytest.param("opencode", "chat_completion", _chat_payload(), False, "/v1/chat/completions", False, id="opencode-chat"),
    pytest.param("opencode", "messages", _messages_payload(), False, "/v1/messages", True, id="opencode-messages"),
    pytest.param("deepseek_sidecar", "messages", _messages_payload(), False, "/anthropic/v1/messages", True, id="deepseek-messages-nonstream"),
    pytest.param("codar_sidecar", "messages", _messages_payload(), False, "/v1/messages", True, id="codar-sidecar-messages"),
    pytest.param("trae_sidecar", "chat_completion", _chat_payload(), False, "/v1/chat/completions", False, id="trae-sidecar-chat"),
    pytest.param("qoder_sidecar", "messages", _messages_payload(stream=True), True, "/v1/messages", True, id="qoder-sidecar-messages-stream"),
    pytest.param("qoder_sidecar", "responses", _responses_payload(), False, "/v1/responses", False, id="qoder-sidecar-responses"),
    pytest.param("codar_sidecar", "responses", _responses_payload(), False, "/v1/responses", False, id="codar-sidecar-responses"),
    pytest.param("muse", "responses", _responses_payload(), False, "/v1/responses", False, id="muse-responses"),
]


@pytest.mark.parametrize("pid,method,payload,stream,expect_suffix,anthropic_header", FORWARD_CASES)
async def test_protocol_forwards_hit_correct_paths(
    pid: str,
    method: str,
    payload: dict[str, Any],
    stream: bool,
    expect_suffix: str,
    anthropic_header: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _fresh(pid)
    adapter.configure(api_key="test-key-123")
    recorded: list[httpx.Request] = []
    reply_body = (
        _sse_recorder(recorded, b"data: [DONE]\n\n")
        if stream
        else _json_recorder(recorded)
    )
    async with _upstream(reply_body, monkeypatch):
        reply = await getattr(adapter, method)(payload, stream=stream)
        if stream:
            await reply.aclose()
    request = recorded[-1]
    assert request.url.path.endswith(expect_suffix), request.url.path
    # 每家外呼都要带贴近真实客户端的 UA，不能暴露 python-httpx
    user_agent = request.headers.get("user-agent", "")
    assert user_agent == adapter.spec.user_agent, f"{pid} UA={user_agent!r}"
    assert "python-httpx" not in user_agent.lower()
    if anthropic_header:
        assert request.headers.get("anthropic-version") == ANTHROPIC_VERSION
        assert request.headers.get("x-api-key"), f"{pid} 走 anthropic messages 但缺 x-api-key"
    if pid == "opencode" and anthropic_header:
        assert request.headers.get("x-api-key") == "test-key-123"
    if pid == "muse":
        assert request.headers.get("authorization") == "Bearer test-key-123"


# ---------- 8. sub2api：路径表静态锚点（真实转发另有 section 13 网关实测） ----------


def test_sub2api_forward_path_table_is_protocol_correct() -> None:
    import app.adapters.sub2api as sub2api_module

    source = Path(sub2api_module.__file__).read_text(encoding="utf-8")
    for capability, path in (
        ("chat", "/v1/chat/completions"),
        ("messages", "/v1/messages"),
        ("responses", "/v1/responses"),
    ):
        assert f'"{capability}": "{path}"' in source, f"缺 {capability} -> {path}"
    assert "_FORWARD_PATHS[capability]" in source


# ---------- 9. qoder messages：Anthropic 形状与完整 SSE 事件序列 ----------


def test_qoder_upstream_auth_status_is_not_rewritten() -> None:
    """上游 401/403 原样透传；只有业务码判死且 HTTP 2xx 时才兜底 401。"""
    adapter = _fresh("qoder")
    err = adapter._classify(401, '{"code":12153,"message":"session expired"}')
    assert err.status_code == 401, f"401 被改写成 {err.status_code}"
    assert getattr(err, "session_dead", False)
    forbidden = adapter._classify(403, '{"code":403,"message":"forbidden"}')
    assert forbidden.status_code == 403, f"403 被改写成 {forbidden.status_code}"
    biz_only = adapter._classify(200, '{"code":12153,"message":"session expired"}')
    assert biz_only.status_code == 401


async def test_qoder_messages_renders_full_anthropic_sse(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _fresh("qoder")
    adapter.configure(api_key="fake-access-token")
    upstream_sse = (
        b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"Hel"}}]}\n\n'
        b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"lo"}}],'
        b'"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )
    recorded: list[httpx.Request] = []
    async with _upstream(_sse_recorder(recorded, upstream_sse), monkeypatch):
        stream_reply = await adapter.messages(_messages_payload(stream=True), stream=True)
        raw = b"".join([chunk async for chunk in stream_reply.iter_bytes()])
        plain_reply = await adapter.messages(_messages_payload(stream=False), stream=False)

    events = [line[7:].decode() for line in raw.split(b"\n") if line.startswith(b"event: ")]
    assert events == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    body = json.loads(plain_reply.body or b"{}")
    assert body["type"] == "message"
    assert body["content"] == [{"type": "text", "text": "Hello"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 5, "output_tokens": 2}
    assert len(recorded) == 2


# ---------- 10. qoder responses：input[] → output[] 映射 ----------


async def test_qoder_responses_maps_input_to_output(monkeypatch: pytest.MonkeyPatch) -> None:
    converted = QoderAdapter._responses_to_openai(
        {
            "model": "demo",
            "instructions": "be brief",
            "input": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
    )
    assert converted["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]

    adapter = _fresh("qoder")
    adapter.configure(api_key="fake-access-token")
    upstream_sse = (
        b'data: {"choices":[{"delta":{"content":"pong"}}],'
        b'"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n'
        b"data: [DONE]\n\n"
    )
    recorded: list[httpx.Request] = []
    async with _upstream(_sse_recorder(recorded, upstream_sse), monkeypatch):
        reply = await adapter.responses(_responses_payload(stream=False), stream=False)

    body = json.loads(reply.body or b"{}")
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output"][0]["type"] == "message"
    assert body["output"][0]["content"][0]["text"] == "pong"
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}
    assert len(recorded) == 1


# ---------- 11. register 能力：services 层真实外呼执行器 ----------


def test_grok_register_capability_backed_by_real_executor_call() -> None:
    import app.services.grok_register as register_service

    source = Path(register_service.__file__).read_text(encoding="utf-8")
    # 未配置执行器时明确失败，不假装成功；配置后按协议 v1 提交 job 并轮询结果
    assert 'await client.post(f"{url}/jobs", json=body)' in source
    assert "await client.get(job_url)" in source
    assert "未配置注册执行器" in source
    assert register_service.PROTOCOL == "grok-register-executor/v1"


# ---------- 12. 跨家不变式：账号能力声明位 = 覆写真实结果（14 家通跑） ----------


def test_account_capability_bits_match_actual_overrides() -> None:
    problems: list[str] = []
    for pid, adapter in sorted(ADAPTERS.items()):
        caps = adapter.spec.capabilities
        # 声明位 ↔ 覆写事实，双向：声明虚高与漏声明都算破
        truth = {
            "oauth": _real_method(adapter, "start_login") and _real_method(adapter, "poll_login"),
            "checkin": _real_method(adapter, "checkin_all") and _real_method(adapter, "account_action"),
            "credits": _real_method(adapter, "refresh_credits"),
        }
        for cap, real in truth.items():
            declared = bool(caps.get(cap))
            if declared != real:
                problems.append(f"{pid}.{cap}: 声明={declared} 真实={real}")
        # 无声明位的账号操作：ops 输出必须等于覆写事实
        ops = _account_ops(adapter)
        for op, method in (("list", "sync_accounts"), ("delete", "delete_account"), ("restart", "restart")):
            real = _real_method(adapter, method)
            if bool(ops[op]) != real:
                problems.append(f"{pid}.{op}: ops={ops[op]} 真实={real}")
    assert not problems, problems


# ---------- 13. /v1/responses 三家网关实测：非流式 200 形状 + 流式 response.* 事件序列 ----------


def _responses_req(
    anon: httpx.Client,
    key: str,
    model: str,
    provider: str,
    *,
    stream: bool = False,
) -> httpx.Response:
    return anon.post(
        "/v1/responses",
        headers={"authorization": f"Bearer {key}", "x-provider": provider},
        json={"model": model, "input": "ping", "stream": stream},
    )


def _responses_stream_raw(anon: httpx.Client, key: str, model: str, provider: str) -> str:
    with anon.stream(
        "POST",
        "/v1/responses",
        headers={"authorization": f"Bearer {key}", "x-provider": provider},
        json={"model": model, "input": "ping", "stream": True},
    ) as stream:
        raw = "".join(stream.iter_text())
        assert stream.status_code == 200, f"{stream.status_code} {raw}"
    return raw


def _assert_plain_responses(body: dict[str, Any], model: str) -> None:
    assert body["object"] == "response"
    assert body["id"] == "resp_fake"
    assert body["model"] == model
    text = body["output"][0]["content"][0]["text"]
    assert text == f"[fake] responses model={model}"


def _assert_response_events(raw: str) -> None:
    events = [line[7:] for line in raw.splitlines() if line.startswith("event: ")]
    assert events == ["response.created", "response.output_text.delta", "response.completed"]
    assert '"type": "response.completed"' in raw


@pytest.fixture(scope="module")
def bearer_key(admin: httpx.Client) -> str:
    created = admin.post("/api/keys", json={"name": "capability-truth-responses"})
    assert created.status_code == 200, created.text
    return str(created.json()["key"])


@pytest.fixture(scope="module")
def codar_sidecar_on_fake(admin: httpx.Client, fake_base: str):
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "codar_sidecar")
    patched = admin.patch(
        "/api/providers/codar_sidecar",
        json={"enabled": True, "base_url": fake_base, "api_key": "sidecar-fake-key"},
    )
    assert patched.status_code == 200, patched.text
    try:
        yield
    finally:
        restored = admin.patch(
            "/api/providers/codar_sidecar",
            json={
                "enabled": bool(before["enabled"]),
                "base_url": before["base_url"],
                "api_key": "***" if before["has_api_key"] else "",
            },
        )
        assert restored.status_code == 200, restored.text


@pytest.fixture(scope="module")
def muse_on_fake(admin: httpx.Client, fake_base: str):
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "muse")
    patched = admin.patch(
        "/api/providers/muse",
        json={"enabled": True, "base_url": fake_base, "api_key": "tok-muse-fake"},
    )
    assert patched.status_code == 200, patched.text
    try:
        yield
    finally:
        restored = admin.patch(
            "/api/providers/muse",
            json={
                "enabled": bool(before["enabled"]),
                "base_url": before["base_url"],
                "api_key": "***" if before["has_api_key"] else "",
            },
        )
        assert restored.status_code == 200, restored.text


@pytest.fixture
def sub2api_on_fake(admin: httpx.Client, fake_base: str):
    gid = "g-resp-truth"
    group = admin.post("/api/sub2api/groups", json={"id": gid, "name": "responses-capability"})
    assert group.status_code == 200, group.text
    account = admin.post(
        "/api/sub2api/accounts",
        json={"group_id": gid, "name": "a-resp", "upstream_base": fake_base, "api_key": "sub-fake-key"},
    )
    assert account.status_code == 200, account.text
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "sub2api")
    patched = admin.patch(
        "/api/providers/sub2api",
        json={"enabled": True, "options": {"group": gid}},
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


def test_sub2api_responses_gateway_plain_and_stream(anon, bearer_key, sub2api_on_fake) -> None:
    plain = _responses_req(anon, bearer_key, "fake-chat-1", "sub2api")
    assert plain.status_code == 200, plain.text
    _assert_plain_responses(plain.json(), "fake-chat-1")
    _assert_response_events(_responses_stream_raw(anon, bearer_key, "fake-chat-1", "sub2api"))


def test_codar_sidecar_responses_gateway_plain_and_stream(anon, bearer_key, codar_sidecar_on_fake) -> None:
    plain = _responses_req(anon, bearer_key, "fake-chat-1", "codar_sidecar")
    assert plain.status_code == 200, plain.text
    _assert_plain_responses(plain.json(), "fake-chat-1")
    _assert_response_events(_responses_stream_raw(anon, bearer_key, "fake-chat-1", "codar_sidecar"))


def test_muse_responses_gateway_plain_and_stream(anon, bearer_key, muse_on_fake) -> None:
    plain = _responses_req(anon, bearer_key, "muse-spark", "muse")
    assert plain.status_code == 200, plain.text
    _assert_plain_responses(plain.json(), "muse-spark")
    _assert_response_events(_responses_stream_raw(anon, bearer_key, "muse-spark", "muse"))


# ---------- 14. /v1/responses 回归：未声明的家仍 501 + OpenAI 错误壳 ----------


async def test_undeclared_responses_adapters_still_raise_501() -> None:
    problems: list[str] = []
    for pid, adapter in sorted(ADAPTERS.items()):
        if adapter.spec.capabilities.get("responses"):
            continue
        fresh = _fresh(pid)
        try:
            await fresh.responses(_responses_payload(), stream=False)
        except ProviderError as exc:
            if exc.status_code != 501:
                problems.append(f"{pid}: status={exc.status_code}")
        else:
            problems.append(f"{pid}: 未声明却没有抛 501")
    assert not problems, problems


def test_undeclared_capability_gate_501_openai_shell(anon, bearer_key) -> None:
    # codebuddy 未声明 embeddings：能力位门槛先于上游调用，壳必须是 OpenAI 形状
    r = anon.post(
        "/v1/embeddings",
        headers={"authorization": f"Bearer {bearer_key}", "x-provider": "codebuddy"},
        json={"model": "fake-chat-1", "input": "hello"},
    )
    assert r.status_code == 501, f"{r.status_code} {r.text}"
    body = r.json()
    assert "type" not in body, "OpenAI 协议错误壳不得带顶层 type"
    error = body["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "capability_unsupported"
    assert error["provider"] == "codebuddy"
    assert body["detail"]


def test_responses_upstream_401_openai_shell(anon, bearer_key, codar_sidecar_on_fake) -> None:
    r = _responses_req(anon, bearer_key, "err-401", "codar_sidecar")
    assert r.status_code == 401, f"{r.status_code} {r.text}"
    body = r.json()
    assert "type" not in body
    error = body["error"]
    assert error["type"] == "authentication_error"
    assert error["code"] == "upstream_auth_failed"
    assert error["provider"] == "codar_sidecar"
    assert error["upstream_status"] == 401
    assert body["detail"]


def test_responses_upstream_503_maps_provider_unavailable(anon, bearer_key, codar_sidecar_on_fake) -> None:
    # 既有映射：上游 503 → OpenAI 壳 code=provider_unavailable / type=api_error，不新造错误码
    r = _responses_req(anon, bearer_key, "err-503", "codar_sidecar")
    assert r.status_code == 503, f"{r.status_code} {r.text}"
    body = r.json()
    assert "type" not in body
    error = body["error"]
    assert error["code"] == "provider_unavailable"
    assert error["type"] == "api_error"
    assert error["upstream_status"] == 503
    assert body["detail"]
