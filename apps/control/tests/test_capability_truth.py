"""能力声明 vs 实际实现的真值审计。

断言方向：
- 声明 True 的能力格必须由子类真实覆写（协议方法 / 账号方法 / 模型目录来源）；
- 声明与实现不符的已知假能力逐格 xfail，修复后自动 XPASS；
- 「实现」格用 mock transport 验证真实外呼的路径与 header，「转发」格验证打到协议正确的上游路径。
"""

from __future__ import annotations

import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

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


# 能力位 → (匹配模式, 方法名组)：any=任一覆写即真，all=必须全部覆写
CAP_RULES: dict[str, tuple[str, tuple[str, ...]]] = {
    "chat": ("any", ("chat_completion",)),
    "messages": ("any", ("messages",)),
    "responses": ("any", ("responses",)),
    "models": ("any", ("fetch_models", "declared_models")),
    "oauth": ("all", ("start_login", "poll_login")),
    "checkin": ("all", ("checkin_all", "account_action")),
    "credits": ("all", ("refresh_credits",)),
}


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
    pytest.param("codebuddy", "messages", _messages_payload(), False, "/v1/messages", False, id="codebuddy-messages"),
    pytest.param("codar", "chat_completion", _chat_payload(), False, "/v1/chat/completions", False, id="codar-chat"),
    pytest.param("codar", "messages", _messages_payload(), False, "/v1/messages", True, id="codar-messages"),
    pytest.param("codar", "responses", _responses_payload(), False, "/v1/responses", False, id="codar-responses"),
    pytest.param("opencode", "chat_completion", _chat_payload(), False, "/v1/chat/completions", False, id="opencode-chat"),
    pytest.param("opencode", "messages", _messages_payload(), False, "/v1/messages", True, id="opencode-messages"),
    pytest.param("deepseek_sidecar", "messages", _messages_payload(), False, "/anthropic/v1/messages", False, id="deepseek-messages-nonstream"),
    pytest.param("codar_sidecar", "messages", _messages_payload(), False, "/v1/messages", False, id="codar-sidecar-messages"),
    pytest.param("trae_sidecar", "chat_completion", _chat_payload(), False, "/v1/chat/completions", False, id="trae-sidecar-chat"),
    pytest.param("qoder_sidecar", "messages", _messages_payload(stream=True), True, "/v1/messages", False, id="qoder-sidecar-messages-stream"),
    pytest.param("qoder_sidecar", "responses", _responses_payload(), False, "/v1/responses", False, id="qoder-sidecar-responses"),
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
    if pid in ("codar", "opencode"):
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
    if anthropic_header:
        assert request.headers.get("anthropic-version") == ANTHROPIC_VERSION
    if pid == "opencode" and anthropic_header:
        assert request.headers.get("x-api-key") == "test-key-123"


# ---------- 8. sub2api：路径表静态锚点（真实转发已有 test_adapters_real 覆盖） ----------


def test_sub2api_forward_path_table_is_protocol_correct() -> None:
    import app.adapters.sub2api as sub2api_module

    source = Path(sub2api_module.__file__).read_text(encoding="utf-8")
    assert '"/v1/chat/completions" if capability == "chat" else "/v1/messages"' in source


# ---------- 9. qoder messages：Anthropic 形状与完整 SSE 事件序列 ----------


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
    # 未配置执行器时明确失败，不假装成功；配置后 POST 外呼执行器
    assert "client.post(url, json=body)" in source
    assert "未配置注册执行器" in source


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
