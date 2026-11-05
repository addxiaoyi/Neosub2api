"""Responses 协议计费贯通：流式 response.usage、非流式顶层 usage、分段合并与 usage_missing 落点。

改造前实证（直连 scripts/fake_upstream.py）：/v1/responses 的三帧
（response.created / response.output_text.delta / response.completed）修复前 usage_in_chunk
全返回 None；按 OpenAI Responses 公开协议把 usage 挂进 response.completed 的标准帧同样返回 None，
即整段漏算。本文件单元层复现该帧形状，集成层用自带 mock 上游走真实网关断言记账结果。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.billing import (
    TOKEN_CLASSES,
    compute_cost_micro,
    extract_usage,
    settle,
    split_usage,
    usage_from_stream,
    usage_in_chunk,
)
from app.db import Database
from app.routers.logs import _classify_credit

pytestmark = pytest.mark.usage

INPUT_MICRO = 2_000_000
OUTPUT_MICRO = 8_000_000
# usage 11/7 × 输入 2.00 / 输出 8.00 每百万 token → 78 micro-USD
EXPECTED_MICRO = 78
ZERO_TOKENS = {name: 0 for name in TOKEN_CLASSES}

# 协议标准流式末帧：usage 在 response 对象下（OpenAI Responses 公开协议）
_COMPLETED_WITH_USAGE = (
    b'event: response.created\n'
    b'data: {"type":"response.created","response":{"id":"resp_mock","model":"m"}}\n\n'
    b'event: response.output_text.delta\n'
    b'data: {"type":"response.output_text.delta","delta":"pong"}\n\n'
    b'event: response.completed\n'
    b'data: {"type":"response.completed","response":{"id":"resp_mock","model":"m",'
    b'"usage":{"input_tokens":11,"output_tokens":7,"total_tokens":18,'
    b'"input_tokens_details":{"cached_tokens":0},'
    b'"output_tokens_details":{"reasoning_tokens":0}}}}\n\n'
)
RESPONSES_USAGE = {
    "input_tokens": 11,
    "output_tokens": 7,
    "total_tokens": 18,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens_details": {"reasoning_tokens": 0},
}

# 假上游 /v1/responses 的真实帧（三帧均无 usage）：解析结果只能是 None
_FAKE_RESPONSES_FRAMES = [
    b'event: response.created\n'
    b'data: {"type": "response.created", "response": {"id": "resp_fake", "model": "fake-chat-1"}}\n\n',
    b'event: response.output_text.delta\n'
    b'data: {"type": "response.output_text.delta", "delta": "[fake] responses model=fake-chat-1"}\n\n',
    b'event: response.completed\n'
    b'data: {"type": "response.completed", "response": {"id": "resp_fake", "model": "fake-chat-1"}}\n\n',
]


# ---------- 单元：三种协议的 usage 解析 ----------


def test_responses_stream_usage_found_on_response_object():
    frames = [part + b"\n\n" for part in _COMPLETED_WITH_USAGE.split(b"\n\n") if part]
    assert len(frames) == 3
    created, delta, completed = frames
    assert usage_in_chunk(created) is None, "created 不带 usage"
    assert usage_in_chunk(delta) is None, "delta 不带 usage"
    assert usage_in_chunk(completed) == RESPONSES_USAGE
    assert usage_from_stream(frames) == RESPONSES_USAGE


def test_responses_stream_without_usage_stays_none():
    # 假上游真实帧无 usage：解析不出就该是 None（落 usage_missing），不许按 0 记账
    assert usage_from_stream(_FAKE_RESPONSES_FRAMES) is None
    for frame in _FAKE_RESPONSES_FRAMES:
        assert usage_in_chunk(frame) is None


def test_responses_non_stream_top_level_usage():
    body = json.dumps({"id": "resp_x", "object": "response", "usage": RESPONSES_USAGE}).encode()
    assert extract_usage(body) == RESPONSES_USAGE
    assert extract_usage(json.dumps({"id": "resp_x", "object": "response"}).encode()) is None


def test_responses_usage_maps_to_micro_amount():
    usage = usage_in_chunk(_COMPLETED_WITH_USAGE)
    parts = split_usage(usage)
    assert parts == {"input": 11, "output": 7, "cache_read": 0, "cache_write": 0, "reasoning": 0}
    total, unpriced = compute_cost_micro(
        parts,
        {"input": {"price_micro_per_1m": INPUT_MICRO}, "output": {"price_micro_per_1m": OUTPUT_MICRO}},
    )
    assert total == EXPECTED_MICRO
    assert unpriced == []


def test_anthropic_stream_segments_merge_to_full_usage():
    # 真实 Anthropic：message_delta 只带 output_tokens，input 必须从 message_start 保留
    start = b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":25,"output_tokens":0}}}\n\n'
    delta = b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":15}}\n\n'
    assert usage_from_stream([start, delta]) == {"input_tokens": 25, "output_tokens": 15}
    # 假上游形态：message_delta 重复带全量 input+output，后帧覆盖后仍是终值
    fake_start = b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":11,"output_tokens":0}}}\n\n'
    fake_delta = b'event: message_delta\ndata: {"type":"message_delta","usage":{"input_tokens":11,"output_tokens":7}}\n\n'
    assert usage_from_stream([fake_start, fake_delta]) == {"input_tokens": 11, "output_tokens": 7}


def test_chat_stream_final_chunk_path_unchanged():
    chunk = b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7}}\n\ndata: [DONE]\n\n'
    assert usage_in_chunk(chunk) == {"prompt_tokens": 11, "completion_tokens": 7}
    # 正文里出现 usage 字样不许误计
    assert usage_in_chunk(b'data: {"choices":[{"delta":{"content":"usage"}}]}\n\n') is None


# ---------- 单元：settle 落点（credit_status 五档中与 responses 相关的档） ----------


@pytest.fixture()
def billing_db(tmp_path: Path):
    return Database(tmp_path / "unified.db")


def _pricing(model: str = "m") -> dict:
    return {
        "at": 1,
        "provider": "*",
        "model": model,
        "lines": {
            "input": {"price_id": 1, "provider": "*", "model": model, "price_micro_per_1m": INPUT_MICRO},
            "output": {"price_id": 2, "provider": "*", "model": model, "price_micro_per_1m": OUTPUT_MICRO},
        },
    }


def _settle(db: Database, *, model: str, usage, pricing: dict | None, status: int = 200) -> sqlite3.Row:
    conn = db.connect()
    try:
        settle(
            conn, ts=1, key_id=7, key_name="k", provider="codar_sidecar", model=model,
            status=status, usage=usage, pricing=pricing, log_id=1,
        )
        conn.commit()
        return conn.execute("SELECT * FROM billing_ledger ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()


def test_settle_responses_usage_lands_billed(billing_db: Database):
    usage = usage_in_chunk(_COMPLETED_WITH_USAGE)
    row = _settle(billing_db, model="m", usage=usage, pricing=_pricing())
    assert row["billed_micro"] == EXPECTED_MICRO
    assert row["note"] == ""
    assert (row["input_tokens"], row["output_tokens"]) == (11, 7)
    assert _classify_credit(row["billed_micro"], row["note"]) == (EXPECTED_MICRO, "billed")


def test_settle_responses_usage_without_price_lands_unpriced(billing_db: Database):
    row = _settle(billing_db, model="unpriced-m", usage=RESPONSES_USAGE, pricing=None)
    assert row["billed_micro"] == 0
    assert row["estimated"] == 1
    assert row["note"] == "unpriced:input,output"
    assert _classify_credit(row["billed_micro"], row["note"]) == (None, "unpriced")


def test_settle_responses_missing_usage_lands_usage_missing(billing_db: Database):
    # 宁可 usage_missing，也不能把没解析到的 usage 当 0 元正常账
    assert usage_from_stream(_FAKE_RESPONSES_FRAMES) is None
    row = _settle(billing_db, model="m", usage=None, pricing=_pricing())
    assert row["billed_micro"] == 0
    assert row["note"] == "usage_missing"
    assert _classify_credit(row["billed_micro"], row["note"]) == (None, "usage_missing")


def test_settle_failed_responses_request_not_written(billing_db: Database):
    conn = billing_db.connect()
    try:
        result = settle(
            conn, ts=1, key_id=7, key_name="k", provider="codar_sidecar", model="m",
            status=502, usage=None, pricing=_pricing(), log_id=1,
        )
        count = conn.execute("SELECT COUNT(*) AS n FROM billing_ledger").fetchone()["n"]
    finally:
        conn.close()
    assert result["written"] is False
    assert count == 0, "失败请求不写流水 → /api/logs 侧落 not_billed"


# ---------- 集成：真实网关 + 自带 mock 上游（复现带 usage 的 Responses 流） ----------

RESP_MODEL = f"resp-bill-{uuid.uuid4().hex[:6]}"
NOUSAGE_MODEL = f"resp-nou-{uuid.uuid4().hex[:6]}"
PROVIDERS = ("codar_sidecar", "muse", "sub2api")
SUB2API_GROUP = f"g-resp-{uuid.uuid4().hex[:6]}"


@pytest.fixture(scope="module")
def mock_responses():
    """本地上游：POST /v1/responses 流式发 response.* 事件，usage 挂 response.completed。"""
    app = FastAPI()

    @app.post("/v1/responses")
    async def responses(request: Request):
        payload = await request.json()
        model = str(payload.get("model") or "")
        has_usage = model != NOUSAGE_MODEL
        resp_obj = {"id": "resp_mock", "model": model}
        if has_usage:
            resp_obj["usage"] = dict(RESPONSES_USAGE)
        if payload.get("stream"):
            events = [
                ("response.created", {"type": "response.created", "response": {"id": "resp_mock", "model": model}}),
                ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "[mock] pong"}),
                ("response.completed", {"type": "response.completed", "response": resp_obj}),
            ]

            async def gen():
                for etype, obj in events:
                    yield f"event: {etype}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()
            return StreamingResponse(gen(), media_type="text/event-stream")
        body = {
            "id": "resp_mock", "object": "response", "model": model,
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "[mock] pong"}]}],
        }
        if has_usage:
            body["usage"] = dict(RESPONSES_USAGE)
        return JSONResponse(body)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "mock 上游 10s 内未启动"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def providers_on_mock(admin: httpx.Client, mock_responses: str):
    """三家 responses provider 指向 mock；退出时按快照还原。"""
    before = {p["id"]: p for p in admin.get("/api/providers").json()}

    for pid, api_key in (("codar_sidecar", "mock-sidecar-key"), ("muse", "tok-muse-mock")):
        patched = admin.patch(
            f"/api/providers/{pid}",
            json={"enabled": True, "base_url": mock_responses, "api_key": api_key},
        )
        assert patched.status_code == 200, f"{pid}: {patched.status_code} {patched.text}"

    group = admin.post("/api/sub2api/groups", json={"id": SUB2API_GROUP, "name": "responses-billing"})
    assert group.status_code in (200, 409), group.text
    account = admin.post(
        "/api/sub2api/accounts",
        json={"group_id": SUB2API_GROUP, "name": "resp-bill", "upstream_base": mock_responses, "api_key": "sub-mock"},
    )
    assert account.status_code in (200, 409), account.text
    patched = admin.patch("/api/providers/sub2api", json={"enabled": True, "options": {"group": SUB2API_GROUP}})
    assert patched.status_code == 200, patched.text

    try:
        yield mock_responses
    finally:
        for pid in ("codar_sidecar", "muse"):
            snap = before[pid]
            admin.patch(
                f"/api/providers/{pid}",
                json={
                    "enabled": bool(snap["enabled"]),
                    "base_url": snap["base_url"],
                    "api_key": "***" if snap.get("has_api_key") else "",
                },
            )
        snap = before["sub2api"]
        admin.patch(
            "/api/providers/sub2api",
            json={
                "enabled": bool(snap["enabled"]),
                "options": {"group": str((snap.get("options") or {}).get("group", "default"))},
            },
        )
        listed = admin.get("/api/sub2api/accounts", params={"group_id": SUB2API_GROUP})
        if listed.status_code == 200:
            for row in listed.json():
                admin.delete(f"/api/sub2api/accounts/{row.get('id')}")
        admin.delete(f"/api/sub2api/groups/{SUB2API_GROUP}")


@pytest.fixture(scope="module", autouse=True)
def seed_responses_prices(admin: httpx.Client) -> None:
    """三家各自录价：输入 2.00 / 输出 8.00（micro 整数），同 11/7 用量应记 78。"""
    for provider in PROVIDERS:
        for token_class, micro in (("input", INPUT_MICRO), ("output", OUTPUT_MICRO)):
            r = admin.post(
                "/api/pricing/books",
                json={
                    "provider": provider, "model": RESP_MODEL, "token_class": token_class,
                    "price_micro_per_1m": micro, "effective_from": 0,
                },
            )
            assert r.status_code in (200, 409), f"{provider}: {r.status_code} {r.text}"


@pytest.fixture(scope="module")
def resp_key(admin: httpx.Client) -> str:
    r = admin.post("/api/keys", json={"name": "responses-billing"})
    assert r.status_code == 200, r.text
    return str(r.json()["key"])


def _max_id(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _wait_ledger(db_path: str, after_id: int, timeout: float = 5.0) -> sqlite3.Row:
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM billing_ledger WHERE id > ? ORDER BY id DESC LIMIT 1", (after_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            return row
        time.sleep(0.1)
    raise AssertionError(f"billing_ledger 在 {timeout}s 内没有新增行")


def _wait_log_item(admin: httpx.Client, *, model: str, provider: str, after_id: int, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = admin.get("/api/logs", params={"model": model, "limit": 100})
        assert got.status_code == 200, got.text
        for item in got.json()["items"]:
            if item["model"] == model and item["provider"] == provider and item["id"] > after_id:
                return item
        time.sleep(0.2)
    raise AssertionError(f"/api/logs 在 {timeout}s 内没出现 {provider}/{model} 的新记录")


def _gateway(key: str, provider: str):
    return {"authorization": f"Bearer {key}", "x-provider": provider}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_responses_stream_bills_usage(
    anon: httpx.Client, admin: httpx.Client, resp_key: str, providers_on_mock, db_path: str, provider: str
):
    """流式 responses：response.completed 的 usage 必须变成真金白银（不是 usage_missing）。"""
    before_ledger = _max_id(db_path, "billing_ledger")
    before_log = _max_id(db_path, "request_logs")
    with anon.stream(
        "POST", "/v1/responses", headers=_gateway(resp_key, provider),
        json={"model": RESP_MODEL, "input": "ping", "stream": True},
    ) as resp:
        raw = "".join(resp.iter_text())
    assert resp.status_code == 200, raw
    assert '"type": "response.completed"' in raw and '"usage"' in raw, raw

    row = _wait_ledger(db_path, before_ledger)
    assert row["provider"] == provider
    assert (row["input_tokens"], row["output_tokens"]) == (11, 7), "usage 必须拆进 token 列"
    assert row["billed_micro"] == EXPECTED_MICRO, f"11×2 + 7×8 应为 78，拿到 {row['billed_micro']}"
    assert row["note"] == ""
    assert row["estimated"] == 0

    item = _wait_log_item(admin, model=RESP_MODEL, provider=provider, after_id=before_log)
    assert item["credit_status"] == "billed"
    assert item["credit"] == EXPECTED_MICRO
    assert (item["prompt_tokens"], item["completion_tokens"]) == (11, 7)


def test_responses_non_stream_bills_usage(
    anon: httpx.Client, admin: httpx.Client, resp_key: str, providers_on_mock, db_path: str
):
    """非流式 responses：响应体顶层 usage → 同样记 78 micro。"""
    provider = "codar_sidecar"
    before_ledger = _max_id(db_path, "billing_ledger")
    before_log = _max_id(db_path, "request_logs")
    r = anon.post(
        "/v1/responses", headers=_gateway(resp_key, provider),
        json={"model": RESP_MODEL, "input": "ping"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["usage"]["input_tokens"] == 11

    row = _wait_ledger(db_path, before_ledger)
    assert (row["input_tokens"], row["output_tokens"]) == (11, 7)
    assert row["billed_micro"] == EXPECTED_MICRO
    assert row["note"] == ""

    item = _wait_log_item(admin, model=RESP_MODEL, provider=provider, after_id=before_log)
    assert item["credit_status"] == "billed"
    assert item["credit"] == EXPECTED_MICRO


def test_responses_stream_without_usage_lands_usage_missing(
    anon: httpx.Client, admin: httpx.Client, resp_key: str, providers_on_mock, db_path: str
):
    """上游一帧 usage 都没给：落 usage_missing，credit 为 null，绝不按 0 元记正常账。"""
    provider = "codar_sidecar"
    before_ledger = _max_id(db_path, "billing_ledger")
    before_log = _max_id(db_path, "request_logs")
    with anon.stream(
        "POST", "/v1/responses", headers=_gateway(resp_key, provider),
        json={"model": NOUSAGE_MODEL, "input": "ping", "stream": True},
    ) as resp:
        raw = "".join(resp.iter_text())
    assert resp.status_code == 200, raw
    assert '"type": "response.completed"' in raw
    assert '"usage"' not in raw, "该模型的 mock 帧不应携带 usage"

    row = _wait_ledger(db_path, before_ledger)
    assert row["billed_micro"] == 0
    assert row["note"] == "usage_missing"
    assert (row["input_tokens"], row["output_tokens"]) == (0, 0)

    item = _wait_log_item(admin, model=NOUSAGE_MODEL, provider=provider, after_id=before_log)
    assert item["credit_status"] == "usage_missing"
    assert item["credit"] is None
