"""P0/P1 缺陷回归：每条用例对应 docs/unification-audit.md 记录的一个已修问题。

断言的目标是「原始缺陷不再复现」：路径错位（调用方指向不存在的端点）、
被删的幽灵端点重新出现、协议外壳错位、统计口径与日志口径打架。
失败时结合用例 docstring 即可判断是代码回退还是测试写错。
"""

from __future__ import annotations

import uuid

import httpx
import pytest

# 原 P0-2 任务页 5 组调用共 9 条路径，随页面整体删除；后端从未实现过
REMOVED_TASK_PATHS = (
    "/api/checkin-logs",
    "/api/checkin-logs/clear",
    "/api/task-logs",
    "/api/task-logs/collect",
    "/api/task-logs/clear",
    "/api/upstream/logs",
    "/api/task-run",
    "/api/task-run/stop",
    "/api/task-claim-schedule",
)


@pytest.fixture(scope="module")
def bearer_key(admin: httpx.Client) -> str:
    r = admin.post("/api/keys", json={"name": f"regressions-{uuid.uuid4().hex[:6]}"})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()["key"]


@pytest.fixture(scope="module")
def zcode_on_fake(admin: httpx.Client, fake_base: str):
    """把 zcode 临时指到假上游（messages 能力走它），结束后原样还原。"""
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "zcode")
    patched = admin.patch(
        "/api/providers/zcode", json={"base_url": fake_base, "api_key": "zcode-fake-key"}
    )
    assert patched.status_code == 200, patched.text
    yield
    restore_key = "***" if before["has_api_key"] else ""
    admin.patch(
        "/api/providers/zcode", json={"base_url": before["base_url"], "api_key": restore_key}
    )


# ---------------- P0-1 模型页端点错位 ----------------


def test_model_catalog_is_not_used_and_models_has_aggregate_contract(admin: httpx.Client) -> None:
    """原缺陷：模型页挂载即调 /api/model-catalog，后端只有 /api/models，整页 404 无数据。

    修法是前端改指 /api/models 并扩展后端条目字段；这里同时锁住「旧路径不复活」
    和「新契约字段齐全」，避免有人为了兼容又补一个 catalog 端点。
    """
    assert admin.get("/api/model-catalog").status_code == 404, (
        "模型页应只走 /api/models；/api/model-catalog 若重新出现，说明改回了旧契约"
    )

    body = admin.get("/api/models", params={"provider": "codebuddy"}).json()
    assert body["models"], "假上游应返回模型，空列表说明聚合链路断了"
    assert {"models", "source", "count"} <= set(body)
    assert body["count"] == len(body["models"])
    for item in body["models"]:
        missing = {"provider", "providers", "provider_states", "readiness"} - set(item)
        assert not missing, f"条目缺少聚合契约字段 {missing}：{item}"
        assert item["provider"] in item["providers"]
        assert item["provider"] in item["provider_states"]


# ---------------- P0-2 任务页幽灵端点 ----------------


def test_removed_task_endpoints_stay_404(admin: httpx.Client) -> None:
    """原缺陷：任务页 9 条路径全部指向后端不存在的端点，三个日志区永远为空。

    页面已整体删除；这些路径若重新返回 2xx，说明幽灵端点又长回来了。
    """
    for path in REMOVED_TASK_PATHS:
        for method in ("get", "post"):
            r = admin.request(method.upper(), path)
            assert r.status_code == 404, f"{method.upper()} {path} 应不存在，实际 {r.status_code} {r.text}"


# ---------------- P0-3 OAuth 双入口 ----------------


def test_oauth_paths_removed_and_auth_start_poll_exist(admin: httpx.Client) -> None:
    """原缺陷：两个 OAuth 弹窗调 /api/oauth/*，后端只有 /api/auth/start|poll，点击必 404。"""
    for path in ("/api/oauth/start", "/api/oauth/batch", "/api/oauth/no-such-id/poll"):
        r = admin.post(path, json={})
        assert r.status_code == 404, f"{path} 应不存在，实际 {r.status_code} {r.text}"
    assert admin.delete("/api/oauth/no-such-id").status_code == 404

    poll = admin.get("/api/auth/poll")
    assert poll.status_code == 200, f"真实轮询端点不可用：{poll.status_code} {poll.text}"
    assert poll.json()["status"] == "invalid", "无 state 时应回 invalid，而不是打上游"


# ---------------- P1-3 重启路径 ----------------


def test_restart_path_is_under_accounts(admin: httpx.Client) -> None:
    """原缺陷：前端调 POST /api/restart，真实路由是 POST /api/accounts/restart，点击必 404。"""
    assert admin.post("/api/restart").status_code == 404, "/api/restart 不应存在"
    # 真路由只注册 POST：GET 应 405 而不是 404，用这个区分「存在但方法不对」与「根本没有」
    r = admin.get("/api/accounts/restart")
    assert r.status_code == 405, f"GET /api/accounts/restart 期望 405，实际 {r.status_code} {r.text}"


# ---------------- P1-6 /v1/messages 错误外壳 ----------------


def test_messages_error_uses_anthropic_envelope(
    anon: httpx.Client, bearer_key: str, zcode_on_fake: None
) -> None:
    """原缺陷：/v1/messages 的错误按 OpenAI 形状返回，Anthropic SDK 解析不出错误类型。"""
    headers = {"authorization": f"Bearer {bearer_key}", "x-provider": "zcode"}
    payload = {
        "model": "err-401",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = anon.post("/v1/messages", headers=headers, json=payload)
    assert r.status_code == 401, f"{r.status_code} {r.text}"
    body = r.json()
    assert set(body) == {"type", "error"}, f"Anthropic 外壳只允许 type + error，实际 {sorted(body)}"
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"
    assert "detail" not in body, "OpenAI 风格的 detail 不应出现在 messages 错误体里"


# ---------------- P0-4 断流不算成功 ----------------


def test_stream_break_not_counted_as_success(admin: httpx.Client, bearer_key: str) -> None:
    """原缺陷：流式响应对外的 HTTP 状态是 200，断流后被算进今日成功请求数，日志也算 ok。

    口径要求：stats 的成功数不增长，失败数 +1，且 /api/logs 的 ok 过滤查不到这条。
    """
    before = admin.get("/api/stats/summary").json()
    r = admin.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {bearer_key}", "x-provider": "codebuddy"},
        json={"model": "stream-break", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    assert "upstream_stream_broken" in r.text, "断流应补错误事件，否则调用方以为这句话说完了"
    after = admin.get("/api/stats/summary").json()

    assert after["today_requests"] == before["today_requests"], "断流不能计入成功请求数"
    assert after["failures"]["today_5xx"] == before["failures"]["today_5xx"] + 1

    ok_rows = admin.get("/api/logs", params={"status": "ok", "model": "stream-break"}).json()
    err_rows = admin.get("/api/logs", params={"status": "error", "model": "stream-break"}).json()
    assert ok_rows["total"] == 0, "stats 说失败，logs 的 ok 过滤却查得到，口径不一致"
    assert err_rows["total"] >= 1, "断流必须落在 error 过滤里"
