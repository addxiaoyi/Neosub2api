"""鉴权头变体：Bearer / x-api-key / 两者一致 / 两者冲突 / 都不带，以及密钥不落日志。"""

from __future__ import annotations

import sqlite3
import time

import httpx
import pytest

pytestmark = pytest.mark.auth


@pytest.fixture(scope="module")
def gateway_key(admin: httpx.Client) -> dict:
    r = admin.post("/api/keys", json={"name": "auth-header-variants"})
    assert r.status_code == 200, r.text
    body = r.json()
    return {"id": body["id"], "key": body["key"]}


def _chat(anon: httpx.Client, headers: dict[str, str]) -> httpx.Response:
    return anon.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]},
    )


def _messages(anon: httpx.Client, headers: dict[str, str]) -> httpx.Response:
    return anon.post(
        "/v1/messages",
        headers={**headers, "x-provider": "codebuddy"},
        json={"model": "fake-chat-1", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
    )


def test_authorization_bearer_accepted(anon: httpx.Client, gateway_key: dict) -> None:
    r = _chat(anon, {"authorization": f"Bearer {gateway_key['key']}"})
    assert r.status_code == 200, f"{r.status_code} {r.text}"


def test_x_api_key_accepted(anon: httpx.Client, gateway_key: dict) -> None:
    """Anthropic 官方 SDK 默认发 x-api-key，必须和 Bearer 等价。"""
    r = _chat(anon, {"x-api-key": gateway_key["key"]})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    r = _messages(anon, {"x-api-key": gateway_key["key"]})
    if r.status_code == 501:
        pytest.skip(f"provider 未实现 messages 能力：{r.text}")
    assert r.status_code == 200, f"{r.status_code} {r.text}"


def test_both_headers_same_value_accepted(anon: httpx.Client, gateway_key: dict) -> None:
    r = _chat(
        anon,
        {"authorization": f"Bearer {gateway_key['key']}", "x-api-key": gateway_key["key"]},
    )
    assert r.status_code == 200, f"{r.status_code} {r.text}"


def test_conflicting_headers_rejected(anon: httpx.Client, gateway_key: dict) -> None:
    r = _chat(
        anon,
        {"authorization": f"Bearer {gateway_key['key']}", "x-api-key": "u2a_other_key_value"},
    )
    assert r.status_code == 401, f"{r.status_code} {r.text}"
    error = r.json()["error"]
    assert error["type"] == "authentication_error"
    assert error["code"] == "conflicting_auth_headers"
    assert "冲突" in error["message"]
    assert gateway_key["key"] not in r.text, "错误响应不能回显完整密钥"


def test_conflicting_headers_rejected_anthropic_shell(anon: httpx.Client, gateway_key: dict) -> None:
    r = _messages(
        anon,
        {"authorization": f"Bearer {gateway_key['key']}", "x-api-key": "u2a_other_key_value"},
    )
    assert r.status_code == 401, f"{r.status_code} {r.text}"
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"
    assert "冲突" in body["error"]["message"]


def test_no_headers_rejected_openai_shell(anon: httpx.Client) -> None:
    r = anon.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert r.status_code == 401, f"{r.status_code} {r.text}"
    error = r.json()["error"]
    assert error["type"] == "authentication_error"
    assert error["code"] == "invalid_api_key"
    assert "x-api-key" in error["message"], "缺失提示要写明两种头都接受"


def test_no_headers_rejected_anthropic_shell(anon: httpx.Client) -> None:
    r = anon.post(
        "/v1/messages",
        json={"model": "m", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401, f"{r.status_code} {r.text}"
    body = r.json()
    assert body["type"] == "error", f"messages 必须用 Anthropic 外壳：{body}"
    assert body["error"]["type"] == "authentication_error"
    assert body["error"]["message"]


def test_no_headers_rejected_responses_shell(anon: httpx.Client) -> None:
    r = anon.post("/v1/responses", json={"model": "m", "input": "hi"})
    assert r.status_code == 401, f"{r.status_code} {r.text}"
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_invalid_x_api_key_rejected(anon: httpx.Client) -> None:
    r = _chat(anon, {"x-api-key": "u2a_fakefakefakefakefakefakefake"})
    assert r.status_code == 401, f"{r.status_code} {r.text}"
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_key_not_persisted_in_logs(
    anon: httpx.Client, admin: httpx.Client, gateway_key: dict, db_path: str
) -> None:
    r = _chat(anon, {"x-api-key": gateway_key["key"]})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    assert gateway_key["key"] not in r.text

    time.sleep(0.2)  # 控制面进程异步写 request_logs

    logs = admin.get("/api/logs", params={"key_id": gateway_key["id"]}).text
    assert gateway_key["key"] not in logs, "request_logs 出口不能出现完整密钥"

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT provider, model, error, key_name FROM request_logs "
            "WHERE key_id = ? ORDER BY id DESC LIMIT 1",
            (gateway_key["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "x-api-key 请求也要落 request_logs 并归属到 key_id"
    for cell in row:
        assert gateway_key["key"] not in str(cell or ""), f"日志字段不能落完整密钥：{cell}"
