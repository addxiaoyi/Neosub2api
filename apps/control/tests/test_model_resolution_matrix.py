"""模型解析全链路矩阵：x-provider / provider 前缀 / 别名 / 目录 / 兜底 × 三协议 × 冲突情形。

每条用例固定断言三件事：状态码、错误 code、命中的 provider——
- 200：拿 request_logs 里本次新增的最新一条定位承接方，同时回显校验上游收到的模型名；
- 错误：从 error 对象的 provider / candidates 反推解析结果。

契约口径（与实现同步）：头与前缀指向不同 provider → 400 conflicting_provider_hints，
同源则放行并剥离前缀；本地注册表未命中 → provider_not_found（真正的上游 404 才用
upstream_not_found）；Anthropic 壳与 OpenAI 壳必须带同一个 error.code。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import pytest

pytestmark = pytest.mark.gateway

CHAT = "/v1/chat/completions"
MSGS = "/v1/messages"
RESP = "/v1/responses"

MODEL = "fake-chat-1"  # 假上游目录里的模型；默认仅 codebuddy 启用 → 唯一命中
GHOST = "ghost"  # 注册表里不存在的 provider id
OPENCODE = "opencode"  # 种子默认停用的 provider，用来测「停用」分支
CATALOG_MISS = "definitely-not-a-model"
FALLBACK_MODEL = "mystery-model-xyz"
PFX = "ghost-prefix"  # 未知前缀，用来测前缀不被认出后落到哪条链路

CODEBUDDY = "codebuddy"
OPENCODE_KEY = "fake-key"  # 假上游不校验凭据，占位即可
LOG_WAIT_SEC = 6.0


@dataclass(frozen=True)
class Case:
    id: str
    path: str
    model: str
    status: int
    code: str | None = None  # 期望的 error.code；200 用例不填
    providers: tuple[str, ...] = ()  # 解析命中的 provider 集合
    x_provider: str | None = None
    setup: tuple[str, ...] = ()  # 见 _apply_setup
    aliases: dict[str, str] | None = None  # 非 None 时整体覆盖 model_map
    upstream_model: str | None = None  # 200 时上游应收到的 model（回显 + 日志双断言）
    detail_model: str | None = None  # 错误体里应报告的模型名
    detail_cycle: tuple[str, ...] | None = None  # 别名循环时 detail.cycle 的入环成员；() 表示必须没有循环字段
    message_has: tuple[str, ...] = ()  # error.message 必须点名的片段（如冲突的两个 provider）


CASES: tuple[Case, ...] = (
    # --- 显式 x-provider：正确 / 不存在 / 已停用 × 三协议 ---
    Case("explicit_ok_chat", CHAT, MODEL, 200, providers=(CODEBUDDY,),
         x_provider=CODEBUDDY, setup=("codebuddy_on",), upstream_model=MODEL),
    Case("explicit_ok_messages", MSGS, MODEL, 200, providers=(CODEBUDDY,),
         x_provider=CODEBUDDY, setup=("codebuddy_on",), upstream_model=MODEL),
    # codebuddy 已声明 responses：解析命中后直打到上游
    Case("explicit_ok_responses", RESP, MODEL, 200, providers=(CODEBUDDY,),
         x_provider=CODEBUDDY, setup=("codebuddy_on",), upstream_model=MODEL),
    Case("explicit_unknown_chat", CHAT, MODEL, 404, code="provider_not_found",
         providers=(GHOST,), x_provider=GHOST, setup=("codebuddy_on",)),
    Case("explicit_unknown_messages", MSGS, MODEL, 404, code="provider_not_found",
         providers=(GHOST,), x_provider=GHOST, setup=("codebuddy_on",)),
    Case("explicit_unknown_responses", RESP, MODEL, 404, code="provider_not_found",
         providers=(GHOST,), x_provider=GHOST, setup=("codebuddy_on",)),
    Case("explicit_disabled_chat", CHAT, MODEL, 503, code="provider_disabled",
         providers=(OPENCODE,), x_provider=OPENCODE, setup=("opencode_off",)),
    Case("explicit_disabled_messages", MSGS, MODEL, 503, code="provider_disabled",
         providers=(OPENCODE,), x_provider=OPENCODE, setup=("opencode_off",)),
    Case("explicit_disabled_responses", RESP, MODEL, 503, code="provider_disabled",
         providers=(OPENCODE,), x_provider=OPENCODE, setup=("opencode_off",)),

    # --- provider/model 前缀 ---
    Case("prefix_ok_chat", CHAT, f"{CODEBUDDY}/{MODEL}", 200, providers=(CODEBUDDY,),
         setup=("codebuddy_on",), upstream_model=MODEL),
    Case("prefix_unknown_provider", CHAT, f"{PFX}/{MODEL}", 404, code="model_not_found",
         detail_model=f"{PFX}/{MODEL}"),
    Case("prefix_disabled_provider", CHAT, f"{OPENCODE}/{MODEL}", 503, code="provider_disabled",
         providers=(OPENCODE,), setup=("opencode_off",)),
    # 冲突：头与前缀指向不同 provider → 显式 400，不静默取其一；error.provider 归头
    Case("conflict_header_wins", CHAT, f"{OPENCODE}/{MODEL}", 400,
         code="conflicting_provider_hints", providers=(CODEBUDDY,),
         x_provider=CODEBUDDY, setup=("codebuddy_on", "opencode_off"),
         message_has=(CODEBUDDY, OPENCODE)),
    # 冲突反向：头指向停用 provider、前缀指向可用 provider → 同样先报冲突，而不是 503
    Case("conflict_header_disabled_blocks_prefix", CHAT, f"{CODEBUDDY}/{MODEL}", 400,
         code="conflicting_provider_hints", providers=(OPENCODE,), x_provider=OPENCODE,
         setup=("codebuddy_on", "opencode_off"), message_has=(OPENCODE, CODEBUDDY)),
    # 同源的头 + 前缀：放行，且按前缀剥离后转发（不再整串透传）
    Case("conflict_same_provider_stripped", CHAT, f"{CODEBUDDY}/{MODEL}", 200,
         providers=(CODEBUDDY,), x_provider=CODEBUDDY, setup=("codebuddy_on",),
         upstream_model=MODEL),
    # 头指向不存在的 provider：本地存在性判定先于冲突判定 → 404 provider_not_found
    Case("explicit_unknown_with_prefix", CHAT, f"{CODEBUDDY}/{MODEL}", 404,
         code="provider_not_found", providers=(GHOST,), x_provider=GHOST,
         setup=("codebuddy_on",)),

    # --- 模型别名（settings.model_map）---
    Case("alias_to_prefix", CHAT, "mx-alias", 200, providers=(CODEBUDDY,),
         aliases={"mx-alias": f"{CODEBUDDY}/{MODEL}"}, setup=("codebuddy_on",),
         upstream_model=MODEL),
    Case("alias_to_bare_catalog", CHAT, "mx-bare", 200, providers=(CODEBUDDY,),
         aliases={"mx-bare": MODEL}, setup=("codebuddy_on",), upstream_model=MODEL),
    Case("alias_to_disabled_provider", CHAT, "mx-dead", 503, code="provider_disabled",
         providers=(OPENCODE,), aliases={"mx-dead": f"{OPENCODE}/{MODEL}"},
         setup=("opencode_off",)),
    # 别名循环 A→B→A：报请求入口 A 而不是链末端 B；detail.cycle 列出入环成员
    Case("alias_cycle_one_hop", CHAT, "mxCycleA", 404, code="model_not_found",
         aliases={"mxCycleA": "mxCycleB", "mxCycleB": "mxCycleA"},
         detail_model="mxCycleA", message_has=("别名循环",),
         detail_cycle=("mxCycleA", "mxCycleB")),
    # 三跳循环 A→B→C→A：同样报入口名，环成员按入环顺序列全
    Case("alias_cycle_three_hop", CHAT, "mxC3A", 404, code="model_not_found",
         aliases={"mxC3A": "mxC3B", "mxC3B": "mxC3C", "mxC3C": "mxC3A"},
         detail_model="mxC3A", message_has=("别名循环",),
         detail_cycle=("mxC3A", "mxC3B", "mxC3C")),
    # Anthropic 壳没有 detail：循环信息靠 message（入口名 + 环成员）传递
    Case("alias_cycle_messages_shell", MSGS, "mxCycleA", 404, code="model_not_found",
         aliases={"mxCycleA": "mxCycleB", "mxCycleB": "mxCycleA"},
         detail_model="mxCycleA", message_has=("别名循环", "mxCycleB")),
    # 自引用 A→A：配置层不校验，解析层仍当没有别名（终点=A 本身），不贴循环标签
    Case("alias_self_ignored", CHAT, "mxSelf", 404, code="model_not_found",
         aliases={"mxSelf": "mxSelf"}, detail_model="mxSelf", detail_cycle=()),
    # 非循环链 A→B→C：走到尽头报 C，不被误判成循环
    Case("alias_chain_three_missing_end", CHAT, "mxCtrlA", 404, code="model_not_found",
         aliases={"mxCtrlA": "mxCtrlB", "mxCtrlB": "mxCtrlC"},
         detail_model="mxCtrlC", detail_cycle=()),
    # 别名目标不存在：单跳后终点查目录无命中，报终点（与修复前一致）
    Case("alias_target_missing", CHAT, "mxGoneA", 404, code="model_not_found",
         aliases={"mxGoneA": "mxGoneZ"},
         detail_model="mxGoneZ", detail_cycle=()),
    # 长链（3 跳 4 节点）非循环且终点是真实模型：沿链解析后照常 200
    Case("alias_chain_deep_hits_catalog", CHAT, "mxDeepA", 200, providers=(CODEBUDDY,),
         aliases={"mxDeepA": "mxDeepB", "mxDeepB": "mxDeepC", "mxDeepC": MODEL},
         setup=("codebuddy_on",), upstream_model=MODEL),
    # 别名表里埋循环垃圾时，404 存在性 → 400 冲突 → 503 停用的判定顺序不受干扰
    Case("order_provider_not_found_beats_alias_cycle", CHAT, "mxOrdA", 404,
         code="provider_not_found", providers=(GHOST,), x_provider=GHOST,
         aliases={"mxOrdA": "mxOrdB", "mxOrdB": "mxOrdA"}),
    Case("order_conflict_beats_alias_cycle", CHAT, f"{OPENCODE}/{MODEL}", 400,
         code="conflicting_provider_hints", providers=(CODEBUDDY,),
         x_provider=CODEBUDDY, setup=("codebuddy_on", "opencode_off"),
         aliases={f"{OPENCODE}/{MODEL}": "mxCycQ", "mxCycQ": f"{OPENCODE}/{MODEL}"},
         message_has=(CODEBUDDY, OPENCODE)),
    Case("order_disabled_beats_alias_cycle", CHAT, "mxOrdD", 503,
         code="provider_disabled", providers=(OPENCODE,), x_provider=OPENCODE,
         setup=("opencode_off",),
         aliases={"mxOrdD": "mxOrdE", "mxOrdE": "mxOrdD"}),

    # --- 目录自动匹配 ---
    Case("catalog_unique_hit", CHAT, MODEL, 200, providers=(CODEBUDDY,),
         setup=("codebuddy_on",), upstream_model=MODEL),
    # 同名两家共有：报歧义 400 + 候选列表，而不是按优先级默默选一家
    Case("catalog_ambiguous", CHAT, MODEL, 400, code="ambiguous_model",
         providers=(CODEBUDDY, OPENCODE), setup=("codebuddy_on", "opencode_on")),
    Case("catalog_missing", CHAT, CATALOG_MISS, 404, code="model_not_found",
         detail_model=CATALOG_MISS),

    # --- 兜底开关 UNIFIED_MODEL_FALLBACK_FIRST / model_fallback_first ---
    Case("fallback_off_unknown", CHAT, FALLBACK_MODEL, 404, code="model_not_found",
         detail_model=FALLBACK_MODEL),
    Case("fallback_on_unknown", CHAT, FALLBACK_MODEL, 200, providers=(CODEBUDDY,),
         setup=("codebuddy_on", "fallback_on"), upstream_model=FALLBACK_MODEL),
    # 开关开 + 首家停用：兜底应跳过停用的 codebuddy，落到下一家启用且支持 chat 的 opencode
    Case("fallback_on_first_disabled", CHAT, FALLBACK_MODEL, 200, providers=(OPENCODE,),
         setup=("codebuddy_off", "opencode_on", "fallback_on"), upstream_model=FALLBACK_MODEL),
    Case("fallback_off_all_disabled", CHAT, FALLBACK_MODEL, 404, code="model_not_found",
         setup=("all_off",), detail_model=FALLBACK_MODEL),
    Case("fallback_on_all_disabled", CHAT, FALLBACK_MODEL, 404, code="model_not_found",
         setup=("all_off", "fallback_on")),

    # --- 模型名的边角：空格 / 大小写 / 外部命名风格 ---
    Case("spaces_around_stripped", CHAT, f"  {MODEL} ", 200, providers=(CODEBUDDY,),
         setup=("codebuddy_on",), upstream_model=MODEL),
    Case("spaces_inside_404", CHAT, "fake chat 1", 404, code="model_not_found",
         detail_model="fake chat 1"),
    Case("case_diff_404", CHAT, "FAKE-CHAT-1", 404, code="model_not_found",
         detail_model="FAKE-CHAT-1"),
    Case("prefix_case_diff_404", CHAT, f"CodeBuddy/{MODEL}", 404, code="model_not_found",
         detail_model=f"CodeBuddy/{MODEL}"),
    # 外部命名风格 openai/gpt-4o：注册表无 openai → 前缀不成立，应整串进目录链路报 404
    Case("external_style_prefix_404", CHAT, "openai/gpt-4o", 404, code="model_not_found",
         detail_model="openai/gpt-4o"),

    # --- 能力位 ---
    Case("capability_responses_catalog", RESP, MODEL, 200, providers=(CODEBUDDY,),
         setup=("codebuddy_on",), upstream_model=MODEL),
    # Anthropic 壳的 501（GatewayError 类）现在与 OpenAI 壳一样带 code
    Case("capability_messages_shell", MSGS, "trae_sidecar/fake-chat-1", 501,
         code="capability_unsupported", providers=("trae_sidecar",),
         setup=("trae_sidecar_on",)),
)


# ---------------- 基建 ----------------


@pytest.fixture(scope="module")
def gateway_key(admin: httpx.Client) -> str:
    r = admin.post("/api/keys", json={"name": "model-matrix-suite"})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()["key"]


def _providers(admin: httpx.Client) -> list[dict]:
    r = admin.get("/api/providers")
    assert r.status_code == 200, r.text
    return r.json()


def _set_provider(admin: httpx.Client, pid: str, *, enabled: bool | None = None,
                  api_key: str | None = None) -> None:
    patch: dict[str, object] = {}
    if enabled is not None:
        patch["enabled"] = enabled
    if api_key is not None:
        patch["api_key"] = api_key
    r = admin.patch(f"/api/providers/{pid}", json=patch)
    assert r.status_code == 200, f"{pid} {patch}: {r.status_code} {r.text}"


def _opencode_on(admin: httpx.Client) -> None:
    item = next(p for p in _providers(admin) if p["id"] == OPENCODE)
    # 只在原本没凭据时补占位 key：不覆盖别人已配好的真实凭据
    key = None if item.get("has_api_key") else OPENCODE_KEY
    _set_provider(admin, OPENCODE, enabled=True, api_key=key)


def _apply_setup(admin: httpx.Client, tokens: tuple[str, ...]) -> None:
    for token in tokens:
        if token == "codebuddy_on":
            _set_provider(admin, CODEBUDDY, enabled=True)
        elif token == "codebuddy_off":
            _set_provider(admin, CODEBUDDY, enabled=False)
        elif token == "opencode_off":
            _set_provider(admin, OPENCODE, enabled=False)
        elif token == "opencode_on":
            _opencode_on(admin)
        elif token == "trae_sidecar_on":
            _set_provider(admin, "trae_sidecar", enabled=True)
        elif token == "all_off":
            for item in _providers(admin):
                if item.get("enabled"):
                    _set_provider(admin, item["id"], enabled=False)
        elif token == "fallback_on":
            r = admin.post("/api/settings/runtime-config", json={"model_fallback_first": True})
            assert r.status_code == 200, r.text
        else:
            raise AssertionError(f"未知 setup：{token}")


def _log_total(admin: httpx.Client, model: str) -> int:
    r = admin.get("/api/logs", params={"model": model, "limit": 1})
    assert r.status_code == 200, r.text
    return int(r.json()["total"])


def _wait_new_log(admin: httpx.Client, model: str, before: int) -> dict:
    """日志是旁路异步落库：等到该模型的条数涨了再取最新一条当本次请求的落点。"""
    deadline = time.monotonic() + LOG_WAIT_SEC
    while time.monotonic() < deadline:
        r = admin.get("/api/logs", params={"model": model, "limit": 3})
        assert r.status_code == 200, r.text
        data = r.json()
        if int(data["total"]) > before:
            return data["items"][0]
        time.sleep(0.2)
    latest = admin.get("/api/logs", params={"limit": 3}).json()["items"]
    seen = [(row["provider"], row["model"], row["status"]) for row in latest]
    raise AssertionError(f"等 model={model!r} 的新日志超时（before={before}），最近日志：{seen}")


def _error_body(resp: httpx.Response, path: str) -> tuple[dict, dict]:
    body = resp.json()
    if path == MSGS:
        assert set(body) == {"type", "error"}, f"Anthropic 壳形状不对：{body}"
        assert body["type"] == "error", body
        return body["error"], body
    assert isinstance(body.get("error"), dict), f"OpenAI 壳缺 error 对象：{body}"
    assert "type" not in body, f"OpenAI 壳不应有顶层 type：{body}"
    return body["error"], body


def _hit_providers(err: dict, body: dict, path: str) -> set[str]:
    provider = err.get("provider")
    if isinstance(provider, str) and provider:
        return {provider}
    candidates = err.get("candidates")
    if not candidates and path != MSGS and isinstance(body.get("detail"), dict):
        candidates = body["detail"].get("candidates")
    if candidates:
        return {str(item["provider"]) for item in candidates}
    return set()


def _echoed_text(resp: httpx.Response, path: str) -> str:
    data = resp.json()
    if path == CHAT:
        return str(data["choices"][0]["message"]["content"])
    if path == MSGS:
        return str(data["content"][0]["text"])
    if path == RESP:
        return str(data["output"][0]["content"][0]["text"])
    raise AssertionError(f"没有为 {path} 定义 200 回显取法")


@pytest.fixture(autouse=True)
def restore_state(admin: httpx.Client):
    """每条用例前后对齐三块共享状态：provider 启用位、model_map、兜底开关。"""
    before = {p["id"]: bool(p["enabled"]) for p in _providers(admin)}
    opencode_key = bool(next(p for p in _providers(admin) if p["id"] == OPENCODE).get("has_api_key"))
    model_map = admin.get("/api/settings/model-map").json()
    runtime = {i["key"]: i["value"] for i in admin.get("/api/settings/runtime-config").json()["items"]}
    yield
    for item in _providers(admin):
        pid = item["id"]
        if bool(item.get("enabled")) != before[pid]:
            _set_provider(admin, pid, enabled=before[pid])
    opencode_key_now = bool(next(p for p in _providers(admin) if p["id"] == OPENCODE).get("has_api_key"))
    if opencode_key_now and not opencode_key:
        _set_provider(admin, OPENCODE, api_key="")  # 我们补的占位 key 收回
    if admin.get("/api/settings/model-map").json() != model_map:
        assert admin.post("/api/settings/model-map", json=model_map).status_code == 200
    runtime_now = {i["key"]: i["value"]
                   for i in admin.get("/api/settings/runtime-config").json()["items"]}
    if runtime_now["model_fallback_first"] != runtime["model_fallback_first"]:
        r = admin.post("/api/settings/runtime-config",
                       json={"model_fallback_first": runtime["model_fallback_first"]})
        assert r.status_code == 200, r.text


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_resolution_matrix(case: Case, admin: httpx.Client, anon: httpx.Client,
                           gateway_key: str) -> None:
    _apply_setup(admin, case.setup)
    if case.aliases is not None:
        r = admin.post("/api/settings/model-map", json=case.aliases)
        assert r.status_code == 200, r.text

    headers = {"authorization": f"Bearer {gateway_key}"}
    if case.x_provider is not None:
        headers["x-provider"] = case.x_provider
    before = _log_total(admin, case.upstream_model) if case.status == 200 else 0
    resp = anon.post(case.path, headers=headers,
                     json={"model": case.model, "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == case.status, f"[{case.id}] {resp.status_code} {resp.text}"

    if case.status == 200:
        row = _wait_new_log(admin, case.upstream_model or "", before)
        assert row["provider"] in case.providers, f"[{case.id}] 日志承接方：{row}"
        assert row["model"] == case.upstream_model, f"[{case.id}] 日志模型名：{row}"
        assert row["status"] == 200, f"[{case.id}] 日志状态：{row}"
        assert f"model={case.upstream_model}" in _echoed_text(resp, case.path), \
            f"[{case.id}] 上游收到的模型名不对：{resp.text[:300]}"
        return

    err, body = _error_body(resp, case.path)
    # 两种协议壳必须拿到同一个 error.code（契约对称性）
    assert err.get("code") == case.code, f"[{case.id}] code：{err}"
    assert _hit_providers(err, body, case.path) == set(case.providers), \
        f"[{case.id}] 命中 provider 不符：{err}"
    message = str(err.get("message") or "")
    for fragment in case.message_has:
        assert fragment in message, f"[{case.id}] message 未点名 {fragment!r}：{message}"
    if case.detail_model is not None:
        if case.path == MSGS:
            assert case.detail_model in str(err.get("message")), f"[{case.id}] {err}"
        else:
            assert body["detail"]["model"] == case.detail_model, f"[{case.id}] {body}"
    if case.detail_cycle is not None:
        assert isinstance(body.get("detail"), dict), f"[{case.id}] 期望 OpenAI detail 壳：{body}"
        assert tuple(body["detail"].get("cycle") or ()) == case.detail_cycle, \
            f"[{case.id}] detail.cycle：{body['detail']}"
