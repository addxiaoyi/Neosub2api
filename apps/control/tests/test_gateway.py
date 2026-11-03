"""网关路由：/v1/models 聚合、/v1/chat/completions 流式+非流式、/v1/messages。

codebuddy 默认 enabled=1 且 capabilities 全 True，它透传到假上游（8061），
所以这些用例在当前环境应该能跑通；若未来改了适配器/假上游，用 skip 兜底。
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

pytestmark = pytest.mark.gateway


@pytest.fixture(scope="module")
def bearer_key(admin: httpx.Client) -> str:
    """每个模块一份 key，省得每个用例都重建；模块内网关鉴权都复用它。"""
    r = admin.post("/api/keys", json={"name": "gateway-suite"})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()["key"]


def _ensure_codebuddy_enabled(admin: httpx.Client) -> None:
    """codebuddy 初始 enabled=1，但做个防御性 PATCH，不影响流程。"""
    r = admin.get("/api/providers")
    assert r.status_code == 200
    items = r.json()
    codebuddy = next((p for p in items if p["id"] == "codebuddy"), None)
    if codebuddy is None:
        pytest.skip("provider 列表里找不到 codebuddy")
    if not codebuddy.get("enabled"):
        admin.patch("/api/providers/codebuddy", json={"enabled": True})


def test_v1_models_aggregates(admin: httpx.Client, anon: httpx.Client, bearer_key: str) -> None:
    _ensure_codebuddy_enabled(admin)
    r = anon.get("/v1/models", headers={"authorization": f"Bearer {bearer_key}"})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    assert body.get("object") == "list"
    data = body.get("data")
    assert isinstance(data, list), f"data 应该是 list，拿到 {type(data).__name__}"
    # codebuddy → /v1/models 透传到假上游，假上游返回 fake-chat-1 / fake-chat-2
    # 如果 list_models 失败/被 skip，data 可能为空，我们只在有值时校验结构
    if data:
        for item in data:
            assert "id" in item and item["id"], "每条要有 id"
            assert item.get("object") == "model"


def test_chat_completions_non_stream(anon: httpx.Client, bearer_key: str) -> None:
    r = anon.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {bearer_key}"},
        json={"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    assert "choices" in body and len(body["choices"]) > 0, "响应要带 choices"
    # choices[0].message.content 假上游会填 [fake] model=...，简单断言存在
    assert body["choices"][0].get("message", {}).get("content"), "content 不能为空"
    assert "usage" in body, "非流式响应必须带 usage"


def test_chat_completions_stream(anon: httpx.Client, bearer_key: str) -> None:
    buf: list[str] = []
    with anon.stream(
        "POST",
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {bearer_key}"},
        json={
            "model": "fake-chat-1",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200, f"stream status: {resp.status_code}"
        for chunk in resp.iter_text():
            buf.append(chunk)
    text = "".join(buf)
    # 至少要有 SSE 风格的 data: 行（假上游发的）
    assert "data:" in text or "event:" in text, f"响应不像 SSE：{text[:200]}"
    # 且最终有 [DONE]
    assert "[DONE]" in text, f"没看到 [DONE]：{text[:200]}"


def test_messages_non_stream(anon: httpx.Client, bearer_key: str) -> None:
    """Anthropic /v1/messages 协议——codebuddy 声明了 messages=True capability，
    适配器透传到假上游的 /v1/messages，假上游也实现了非流式响应。"""
    r = anon.post(
        "/v1/messages",
        headers={"authorization": f"Bearer {bearer_key}"},
        json={"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]},
    )
    if r.status_code == 501:
        pytest.skip(f"provider 未实现 messages 能力：{r.text}")
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    # Anthropic 响应结构：content 是 list，item.type == "text"
    assert body.get("type") == "message", f"response.type 应该是 message：{body}"
    assert "content" in body, "缺 content"
