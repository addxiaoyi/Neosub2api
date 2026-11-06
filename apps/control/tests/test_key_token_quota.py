"""2-D6：密钥级 token 配额（假上游 chat 固定 usage 11/7，单次合计 18 token）。"""

from __future__ import annotations

import uuid

import httpx
import pytest

MODEL = "quota-m"
TOKENS_PER_CALL = 18


@pytest.fixture(scope="module")
def token_keys(admin: httpx.Client):
    created: list[int] = []

    def make() -> tuple[int, str]:
        r = admin.post("/api/keys", json={"name": f"tok-{uuid.uuid4().hex[:8]}"})
        assert r.status_code == 200, r.text
        created.append(int(r.json()["id"]))
        return int(r.json()["id"]), r.json()["key"]

    yield make

    for key_id in created:
        admin.delete(f"/api/keys/{key_id}")


def _chat(client: httpx.Client, token: str, model: str = MODEL) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {token}", "x-provider": "codebuddy"},
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


def test_token_quota_blocks_after_limit(
    admin: httpx.Client, anon: httpx.Client, token_keys
) -> None:
    key_id, token = token_keys()
    policy = admin.post(
        "/api/quota/policies",
        json={"key_id": key_id, "window": "day", "limit_tokens": TOKENS_PER_CALL + 2},
    )
    assert policy.status_code == 200, policy.text
    assert policy.json()["limit_tokens"] == TOKENS_PER_CALL + 2

    # 单次 18 token，上限 20：第一次放行、第二次开始已用 18 仍放行，第三次累计 36 触发拦截
    first = _chat(anon, token)
    second = _chat(anon, token)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    blocked = _chat(anon, token)
    assert blocked.status_code == 429, blocked.text
    detail = blocked.json()["detail"]
    assert detail["type"] == "quota_exceeded"
    assert detail["metric"] == "tokens"
    assert detail["unit"] == "tokens"
    assert detail["limit"] == TOKENS_PER_CALL + 2
    assert detail["used"] >= 2 * TOKENS_PER_CALL

    status = admin.get("/api/quota/status", params={"key_id": key_id})
    assert status.status_code == 200, status.text
    rows = status.json()["items"]
    assert rows and rows[0]["used_tokens"] >= 2 * TOKENS_PER_CALL
    assert rows[0]["policy"]["limit_tokens"] == TOKENS_PER_CALL + 2
    assert rows[0]["exceeded"] is True


def test_without_token_limit_stays_open(admin: httpx.Client, anon: httpx.Client, token_keys) -> None:
    key_id, token = token_keys()
    policy = admin.post(
        "/api/quota/policies",
        json={"key_id": key_id, "window": "day", "limit_requests": 50},
    )
    assert policy.status_code == 200, policy.text
    assert policy.json()["limit_tokens"] == 0

    for _ in range(3):
        allowed = _chat(anon, token)
        assert allowed.status_code == 200, allowed.text


def test_negative_token_limit_rejected(admin: httpx.Client, token_keys) -> None:
    key_id, _ = token_keys()
    bad = admin.post(
        "/api/quota/policies",
        json={"key_id": key_id, "window": "day", "limit_tokens": -1},
    )
    assert bad.status_code == 400, bad.text
