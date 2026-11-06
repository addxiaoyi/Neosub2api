"""批一 B 组新增协议能力的真实链路回归。

不走 mock transport：全部经 conftest 起好的控制面 + `scripts/fake_upstream.py`，
请求发出后回读假上游记录的路径、头与 body，证明能力位背后确实有外呼。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.adapters.codebuddy import CLIENT_UA

pytestmark = pytest.mark.providers


def _reset(fake_base: str) -> None:
    r = httpx.delete(f"{fake_base}/__fake/last", trust_env=False, timeout=5.0)
    assert r.status_code == 200, r.text


def _last(fake_base: str) -> dict[str, Any]:
    r = httpx.get(f"{fake_base}/__fake/last", trust_env=False, timeout=5.0)
    assert r.status_code == 200, r.text
    return r.json()


def _gateway(anon: httpx.Client, key: str, provider: str, path: str, payload: dict[str, Any]):
    return anon.post(
        path,
        headers={"authorization": f"Bearer {key}", "x-provider": provider},
        json=payload,
    )


@pytest.fixture(scope="module")
def gateway_key(admin: httpx.Client) -> str:
    created = admin.post("/api/keys", json={"name": "new-capabilities"})
    assert created.status_code == 200, created.text
    return str(created.json()["key"])


@pytest.fixture(scope="module")
def codebuddy_on_fake(admin: httpx.Client, fake_base: str):
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "codebuddy")
    patched = admin.patch(
        "/api/providers/codebuddy",
        json={"enabled": True, "base_url": fake_base, "api_key": "wbt_fake_admin_token_123"},
    )
    assert patched.status_code == 200, patched.text
    try:
        yield
    finally:
        restored = admin.patch(
            "/api/providers/codebuddy",
            json={
                "enabled": bool(before["enabled"]),
                "base_url": before["base_url"],
                "api_key": "***" if before["has_api_key"] else "",
            },
        )
        assert restored.status_code == 200, restored.text


@pytest.fixture(scope="module")
def gptgrok_on_fake(admin: httpx.Client, fake_base: str):
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "gptgrok")
    patched = admin.patch(
        "/api/providers/gptgrok",
        json={
            "enabled": True,
            "api_key": "tok-gptgrok",
            "options": {"chatgpt_base": fake_base},
        },
    )
    assert patched.status_code == 200, patched.text
    try:
        yield
    finally:
        restored = admin.patch(
            "/api/providers/gptgrok",
            json={
                "enabled": bool(before["enabled"]),
                "api_key": "***" if before["has_api_key"] else "",
                "options": dict(before.get("options") or {}),
            },
        )
        assert restored.status_code == 200, restored.text


@pytest.fixture
def sub2api_on_fake(admin: httpx.Client, fake_base: str):
    gid = "g-new-caps"
    group = admin.post("/api/sub2api/groups", json={"id": gid, "name": "new-capabilities"})
    assert group.status_code == 200, group.text
    account = admin.post(
        "/api/sub2api/accounts",
        json={"group_id": gid, "name": "a-new", "upstream_base": fake_base, "api_key": "sub-fake-key"},
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


# ---------- B-1 codebuddy /v1/responses ----------


def test_codebuddy_responses_forward(
    anon: httpx.Client, gateway_key: str, fake_base: str, codebuddy_on_fake
) -> None:
    _reset(fake_base)
    r = _gateway(
        anon, gateway_key, "codebuddy", "/v1/responses",
        {"model": "fake-chat-1", "input": "ping"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "response"
    assert body["output"][0]["content"][0]["text"] == "[fake] responses model=fake-chat-1"

    hit = _last(fake_base)
    assert hit["path"] == "/v1/responses"
    assert hit["headers"]["authorization"] == "Bearer wbt_fake_admin_token_123"
    assert hit["headers"]["user-agent"] == CLIENT_UA
    assert json.loads(hit["body"])["model"] == "fake-chat-1"


def test_codebuddy_responses_stream(
    anon: httpx.Client, gateway_key: str, fake_base: str, codebuddy_on_fake
) -> None:
    _reset(fake_base)
    r = _gateway(
        anon, gateway_key, "codebuddy", "/v1/responses",
        {"model": "fake-chat-1", "input": "ping", "stream": True},
    )
    assert r.status_code == 200, r.text
    events = [line[7:] for line in r.text.splitlines() if line.startswith("event: ")]
    assert events == ["response.created", "response.output_text.delta", "response.completed"]

    hit = _last(fake_base)
    assert hit["path"] == "/v1/responses"
    assert json.loads(hit["body"])["stream"] is True


# ---------- B-2 codebuddy /v1/messages/count_tokens ----------


def test_codebuddy_count_tokens_forward(
    anon: httpx.Client, gateway_key: str, fake_base: str, codebuddy_on_fake
) -> None:
    _reset(fake_base)
    r = _gateway(
        anon, gateway_key, "codebuddy", "/v1/messages/count_tokens",
        {"model": "fake-chat-1", "messages": [{"role": "user", "content": "hello world"}]},
    )
    assert r.status_code == 200, r.text
    assert int(r.json()["input_tokens"]) >= 1

    hit = _last(fake_base)
    assert hit["path"] == "/v1/messages/count_tokens"
    assert hit["headers"]["user-agent"] == CLIENT_UA
    assert hit["headers"]["x-api-key"] == "wbt_fake_admin_token_123"
    assert hit["headers"]["anthropic-version"]
    sent = json.loads(hit["body"])
    assert sent["model"] == "fake-chat-1"
    assert sent["messages"][0]["content"] == "hello world"


# ---------- B-3 gptgrok /v1/models/{id} 静态声明 ----------


def test_gptgrok_model_detail_from_declared_catalog(
    anon: httpx.Client, gateway_key: str, fake_base: str, gptgrok_on_fake
) -> None:
    _reset(fake_base)
    r = anon.get(
        "/v1/models/gpt-5",
        headers={"authorization": f"Bearer {gateway_key}", "x-provider": "gptgrok"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "gpt-5", "object": "model", "owned_by": "gptgrok"}
    assert _last(fake_base) == {}, "静态声明命中时不该再打上游"

    missing = anon.get(
        "/v1/models/no-such-model",
        headers={"authorization": f"Bearer {gateway_key}", "x-provider": "gptgrok"},
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["code"] == "model_not_found"


# ---------- B-4 gptgrok /v1/search（语义等价，不是原生检索） ----------


def test_gptgrok_search_forwards_prompt_to_chat_channel(
    anon: httpx.Client, gateway_key: str, fake_base: str, gptgrok_on_fake
) -> None:
    _reset(fake_base)
    r = _gateway(
        anon, gateway_key, "gptgrok", "/v1/search",
        {"prompt": "hello search", "model": "gpt-5"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["sources"] == []
    assert body["model"] == "gpt-5"
    assert "model=gpt-5" in body["answer"]

    hit = _last(fake_base)
    assert hit["path"] == "/backend-api/conversation"
    assert hit["headers"]["authorization"] == "Bearer tok-gptgrok"
    assert hit["headers"]["x-openai-target-path"] == "/backend-api/conversation"
    sent = json.loads(hit["body"])
    assert sent["model"] == "gpt-5"
    assert sent["messages"][0]["content"]["parts"][0] == "hello search"


def test_gptgrok_search_defaults_model_when_omitted(
    anon: httpx.Client, gateway_key: str, fake_base: str, gptgrok_on_fake
) -> None:
    _reset(fake_base)
    r = _gateway(anon, gateway_key, "gptgrok", "/v1/search", {"prompt": "no model"})
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "gpt-4o-search-preview"

    sent = json.loads(_last(fake_base)["body"])
    assert sent["model"] == "gpt-4o-search-preview"


# ---------- B-5 sub2api /v1/models/{id} 走聚合目录 ----------


def test_sub2api_model_detail_from_upstream_catalog(
    anon: httpx.Client, gateway_key: str, fake_base: str, sub2api_on_fake
) -> None:
    _reset(fake_base)
    r = anon.get(
        "/v1/models/fake-chat-1",
        headers={"authorization": f"Bearer {gateway_key}", "x-provider": "sub2api"},
    )
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["id"] == "fake-chat-1"
    assert item["object"] == "model"
    assert item["owned_by"]

    hit = _last(fake_base)
    assert hit["path"] == "/v1/models"
    assert hit["headers"]["authorization"] == "Bearer sub-fake-key"

    missing = anon.get(
        "/v1/models/no-such-model",
        headers={"authorization": f"Bearer {gateway_key}", "x-provider": "sub2api"},
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["code"] == "model_not_found"
