"""未定价/无 usage 的估算计费：默认关闭时逐字段不变，开启后落「可识别为估算」的第四种流水。

单元层直接连临时 SQLite 验证 settle 落点与估算口径；集成层用自带 mock 上游走真实网关，
断言开关关闭/开启、usage_zero/正常 usage 优先、估不出保持 usage_missing、超长正文被钳住、
logs/stats/CSV 三处都能把估算与真值分开。
"""

from __future__ import annotations

import csv
import importlib.util
import io
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

from app.billing import settle
from app.db import Database
from app.routers.logs import _classify_credit, _row_to_public
from app.usage_estimate import (
    ESTIMATE_VERSION,
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    StreamText,
    count_tokens,
    estimate_from_body,
    estimate_from_text,
)

REPO = Path(__file__).resolve().parents[3]
INPUT_MICRO = 2_000_000
OUTPUT_MICRO = 8_000_000
# 估算一条 "hi" → "pong"：input 1 token、output 1 token → 1×2 + 1×8 = 10 micro
TINY_MICRO = 10


def _load_core():
    path = REPO / "scripts" / "backup_core.py"
    spec = importlib.util.spec_from_file_location("backup_core_estimate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


# ===== 单元：估算口径 =====


def test_token_convention_cjk_and_latin():
    assert count_tokens("abcd", MAX_INPUT_TOKENS) == 1  # 4 个非 CJK 码点 = 1 token
    assert count_tokens("abcde", MAX_INPUT_TOKENS) == 2  # 向上取整
    assert count_tokens("你好", MAX_INPUT_TOKENS) == 2  # CJK 每字 1 token
    assert count_tokens("你a好bcd", MAX_INPUT_TOKENS) == 3  # 2 个 CJK + 4 个非 CJK 凑 1 个 token
    assert count_tokens("", MAX_INPUT_TOKENS) == 0


def test_estimate_from_three_response_protocols():
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    chat = b'{"choices":[{"message":{"content":"pong"}}]}'
    assert estimate_from_body(payload, chat) == {"prompt_tokens": 1, "completion_tokens": 1}

    anthropic = b'{"content":[{"type":"text","text":"pong"}]}'
    assert estimate_from_body(payload, anthropic) == {"prompt_tokens": 1, "completion_tokens": 1}

    responses = b'{"output":[{"type":"message","content":[{"type":"output_text","text":"pong"}]}]}'
    assert estimate_from_body(payload, responses) == {"prompt_tokens": 1, "completion_tokens": 1}


def test_estimate_returns_none_when_unavailable():
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    assert estimate_from_body(payload, b"{}") is None, "响应没有正文 → 估不出"
    assert estimate_from_body(payload, b"not-json") is None
    assert estimate_from_body(payload, b'{"choices":[{"message":{"content":""}}]}') is None
    image_only = {
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}]
    }
    assert estimate_from_body(image_only, b'{"choices":[{"message":{"content":"pong"}}]}') is None, \
        "只有图片、没有可算的正文 → 估不出"


def test_estimate_clamps_huge_text():
    payload = {"messages": [{"role": "user", "content": "x" * 1_000_000}]}
    out = "y" * 500_000
    est = estimate_from_text(payload, out)
    assert est == {"prompt_tokens": MAX_INPUT_TOKENS, "completion_tokens": MAX_OUTPUT_TOKENS}


def test_stream_text_accumulates_across_protocols():
    acc = StreamText()
    acc.feed(b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n')
    acc.feed(b'data: {"choices":[{"delta":{"content":"po"}}]}\n\n')
    acc.feed(b'data: {"choices":[{"delta":{"content":"ng"}}]}\n\n')
    acc.feed(b"data: [DONE]\n\n")
    assert acc.output() == "po\nng"

    acc2 = StreamText()
    acc2.feed(b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"pong"}}\n\n')
    assert acc2.output() == "pong"

    acc3 = StreamText()
    acc3.feed(b'data: {"type":"response.output_text.delta","delta":"pong"}\n\n')
    assert acc3.output() == "pong"

    empty = StreamText()
    empty.feed(b'data: {"choices":[]}\n\n')
    assert empty.output() is None


def test_stream_text_handles_split_chunks_and_missing_newline():
    frame = b'data: {"choices":[{"delta":{"content":"pong"}}]}'
    acc = StreamText()
    acc.feed(frame[:19])
    acc.feed(frame[19:])
    assert acc.output() == "pong", "半行要跨 chunk 缓冲，末尾没有换行也不能丢"


# ===== 单元：settle 落点 =====


@pytest.fixture()
def billing_db(tmp_path: Path):
    return Database(tmp_path / "unified.db")


def _pricing(model: str = "m", provider: str = "p") -> dict:
    return {
        "at": 1, "provider": provider, "model": model,
        "lines": {
            "input": {"price_id": 1, "provider": provider, "model": model,
                      "price_micro_per_1m": INPUT_MICRO},
            "output": {"price_id": 2, "provider": provider, "model": model,
                       "price_micro_per_1m": OUTPUT_MICRO},
        },
    }


def _settle(db: Database, *, usage=None, estimated_usage=None, pricing=None, failed=False):
    conn = db.connect()
    try:
        result = settle(
            conn, ts=1, key_id=7, key_name="k", provider="p", model="m", status=200,
            usage=usage, pricing=pricing, log_id=1, failed=failed,
            estimated_usage=estimated_usage,
        )
        conn.commit()
        return result, conn.execute("SELECT * FROM billing_ledger ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()


def test_estimate_off_is_field_for_field_unchanged(billing_db: Database):
    """关闭时（调用方不传 estimated_usage）：与改动前完全相同的一笔 usage_missing。"""
    result, row = _settle(billing_db, usage=None, estimated_usage=None, pricing=None)
    assert result["written"] is True
    assert row["billed_micro"] == 0
    assert row["note"] == "usage_missing"
    assert row["estimated"] == 0
    assert row["usage_estimated"] == 0
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"],
            row["cache_write_tokens"], row["reasoning_tokens"]) == (0, 0, 0, 0, 0)
    assert row["price_snapshot_id"] is None
    assert _classify_credit(row["billed_micro"], row["note"]) == (None, "usage_missing")


def test_estimated_row_is_distinguishable(billing_db: Database):
    result, row = _settle(billing_db, usage=None, estimated_usage={"prompt_tokens": 3,
                                                                    "completion_tokens": 5},
                          pricing=_pricing())
    assert row["billed_micro"] == 3 * 2 + 5 * 8
    assert row["usage_estimated"] == 1
    assert row["estimated"] == 0, "估算 ≠ 未定价，两个标记互不兼职"
    assert row["note"] == f"usage_estimated:{ESTIMATE_VERSION}"
    assert (row["input_tokens"], row["output_tokens"]) == (3, 5)
    assert result["usage_estimated"] == 1
    assert _classify_credit(row["billed_micro"], row["note"]) == (row["billed_micro"], "estimated")


def test_usage_zero_wins_over_estimate(billing_db: Database):
    _, row = _settle(billing_db, usage={"prompt_tokens": 0, "completion_tokens": 0},
                     estimated_usage={"prompt_tokens": 3, "completion_tokens": 5},
                     pricing=_pricing())
    assert row["billed_micro"] == 0
    assert row["note"] == "usage_zero:p"
    assert row["usage_estimated"] == 0
    assert _classify_credit(row["billed_micro"], row["note"]) == (0, "billed")


def test_real_usage_wins_over_estimate(billing_db: Database):
    _, row = _settle(billing_db, usage={"prompt_tokens": 11, "completion_tokens": 7},
                     estimated_usage={"prompt_tokens": 3, "completion_tokens": 5},
                     pricing=_pricing())
    assert row["billed_micro"] == 78
    assert row["note"] == ""
    assert row["usage_estimated"] == 0
    assert _classify_credit(row["billed_micro"], row["note"]) == (78, "billed")


def test_estimated_with_unpriced_stays_estimated_and_marked_partial(billing_db: Database):
    _, row = _settle(billing_db, usage=None,
                     estimated_usage={"prompt_tokens": 3, "completion_tokens": 5}, pricing=None)
    assert row["billed_micro"] == 0
    assert row["usage_estimated"] == 1
    assert row["estimated"] == 1, "同时命中未定价档时两个标记都为真，语义不冲突"
    assert row["note"] == f"usage_estimated:{ESTIMATE_VERSION};unpriced:input,output"
    assert _classify_credit(row["billed_micro"], row["note"]) == (0, "estimated")


def test_failed_request_never_estimates(billing_db: Database):
    result, row = _settle(billing_db, usage=None,
                          estimated_usage={"prompt_tokens": 3, "completion_tokens": 5},
                          pricing=_pricing(), failed=True)
    assert result["written"] is False
    assert row is None, "失败请求不写流水，估算也不能例外"


def test_estimate_bounded_amount(billing_db: Database):
    _, row = _settle(billing_db, usage=None,
                     estimated_usage={"prompt_tokens": MAX_INPUT_TOKENS,
                                      "completion_tokens": MAX_OUTPUT_TOKENS},
                     pricing=_pricing())
    assert row["billed_micro"] == MAX_INPUT_TOKENS * 2 + MAX_OUTPUT_TOKENS * 8


def test_logs_row_exposes_estimated_flag(billing_db: Database):
    conn = billing_db.connect()
    try:
        settle(conn, ts=1, key_id=7, key_name="k", provider="p", model="m", status=200,
               usage=None, pricing=_pricing(), log_id=1,
               estimated_usage={"prompt_tokens": 1, "completion_tokens": 1})
        conn.commit()
        ledger = conn.execute("SELECT log_id, billed_micro, note FROM billing_ledger").fetchone()
        conn.execute(
            "INSERT INTO request_logs(id, ts, provider, model, status, prompt_tokens, "
            "completion_tokens, latency_ms, stream, error) VALUES(1, 1, 'p', 'm', 200, 0, 0, 10, 0, NULL)"
        )
        conn.commit()
        log_row = conn.execute("SELECT * FROM request_logs WHERE id = 1").fetchone()
    finally:
        conn.close()
    credit = _classify_credit(ledger["billed_micro"], ledger["note"])
    item = _row_to_public(log_row, None, credit)
    assert item["credit_status"] == "estimated"
    assert item["credit_estimated"] is True
    assert item["credit"] == TINY_MICRO


# ===== 迁移：老库补 usage_estimated 列，连开两次一致 =====


def test_legacy_ledger_gains_usage_estimated_column_idempotently(tmp_path: Path):
    fresh = Database(tmp_path / "fresh.db")
    fresh_conn = fresh.connect()
    try:
        schema = fresh_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='billing_ledger'"
        ).fetchone()["sql"]
    finally:
        fresh_conn.close()
    legacy_sql = "\n".join(
        line for line in schema.splitlines() if "usage_estimated" not in line
    )
    assert "usage_estimated" not in legacy_sql

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute(legacy_sql)
    conn.execute(
        "INSERT INTO billing_ledger(ts, provider, model, status) VALUES(1, 'p', 'old', 200)"
    )
    conn.commit()
    conn.close()

    Database(legacy)
    Database(legacy)  # 第二启动必须幂等

    conn = sqlite3.connect(legacy)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO users(username, password_hash, role, created_at) VALUES('admin', 'x', 'admin', 0)"
    )
    conn.commit()
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(billing_ledger)")}
        old = conn.execute("SELECT * FROM billing_ledger WHERE model = 'old'").fetchone()
    finally:
        conn.close()
    assert "usage_estimated" in cols
    assert int(old["usage_estimated"]) == 0, "历史行没有估算过，补列后必须是 0"

    core = _load_core()
    snap = tmp_path / "legacy-snapshot.db"
    core.snapshot(legacy, snap)
    conn = core.open_db(snap, readonly=True)
    try:
        manifest = {"tables": core.table_counts(conn), "demo": {}}
    finally:
        conn.close()
    assert core.selfcheck(snap, manifest)["ok"] is True


# ===== 集成：真实网关 + mock 上游（chat 端点，可控 usage 形状） =====

SUFFIX = uuid.uuid4().hex[:6]
MODEL_OFF = f"est-off-{SUFFIX}"
MODEL_ON = f"est-on-{SUFFIX}"
MODEL_ZERO = f"est-zero-{SUFFIX}"
MODEL_REAL = f"est-real-{SUFFIX}"
MODEL_EMPTY = f"est-empty-{SUFFIX}"
MODEL_LONG = f"est-long-{SUFFIX}"
ALL_MODELS = (MODEL_OFF, MODEL_ON, MODEL_ZERO, MODEL_REAL, MODEL_EMPTY, MODEL_LONG)
REAL_USAGE = {"prompt_tokens": 11, "completion_tokens": 7}


def _chat_body(model: str, *, stream: bool) -> dict:
    body = {
        "id": "chatcmpl-mock", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"},
                     "finish_reason": "stop"}],
    }
    if model == MODEL_ZERO:
        body["usage"] = {"prompt_tokens": 0, "completion_tokens": 0}
    elif model == MODEL_REAL:
        body["usage"] = dict(REAL_USAGE)
    return body


@pytest.fixture(scope="module")
def mock_chat():
    """本地上游：纯 OpenAI chat 形状，按模型控制 usage 有无/全零/正常。"""
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        payload = await request.json()
        model = str(payload.get("model") or "")
        if model == MODEL_EMPTY:
            return JSONResponse({})
        if not payload.get("stream"):
            return JSONResponse(_chat_body(model, stream=False))

        usage = None
        if model == MODEL_ZERO:
            usage = {"prompt_tokens": 0, "completion_tokens": 0}
        elif model == MODEL_REAL:
            usage = dict(REAL_USAGE)

        async def gen():
            yield b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"pong"}}]}\n\n'
            if usage is not None:
                yield ("data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

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
def codebuddy_on_mock(admin: httpx.Client, mock_chat: str):
    before = {p["id"]: p for p in admin.get("/api/providers").json()}["codebuddy"]
    patched = admin.patch(
        "/api/providers/codebuddy",
        json={"enabled": True, "base_url": mock_chat, "api_key": "mock-key"},
    )
    assert patched.status_code == 200, patched.text
    try:
        yield mock_chat
    finally:
        admin.patch(
            "/api/providers/codebuddy",
            json={
                "enabled": bool(before["enabled"]),
                "base_url": before["base_url"],
                "api_key": "***" if before.get("has_api_key") else "",
            },
        )


@pytest.fixture(scope="module", autouse=True)
def seed_estimate_prices(admin: httpx.Client) -> None:
    for model in ALL_MODELS:
        for token_class, micro in (("input", INPUT_MICRO), ("output", OUTPUT_MICRO)):
            r = admin.post(
                "/api/pricing/books",
                json={
                    "provider": "codebuddy", "model": model, "token_class": token_class,
                    "price_micro_per_1m": micro, "effective_from": 0,
                },
            )
            assert r.status_code in (200, 409), f"{model}: {r.status_code} {r.text}"


@pytest.fixture(scope="module")
def est_key(admin: httpx.Client) -> str:
    r = admin.post("/api/keys", json={"name": f"estimate-{SUFFIX}"})
    assert r.status_code == 200, r.text
    return str(r.json()["key"])


def _set_estimate(admin: httpx.Client, value: bool) -> None:
    r = admin.post("/api/settings/runtime-config", json={"items": {"estimate_usage": value}})
    assert r.status_code == 200, f"{r.status_code} {r.text}"


@pytest.fixture(scope="module", autouse=True)
def _reset_estimate(admin: httpx.Client):
    _set_estimate(admin, False)
    yield
    _set_estimate(admin, False)


@pytest.fixture()
def estimate_on(admin: httpx.Client):
    _set_estimate(admin, True)
    try:
        yield
    finally:
        _set_estimate(admin, False)


def _headers(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}", "x-provider": "codebuddy"}


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


def _wait_log(admin: httpx.Client, *, model: str, after_id: int, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = admin.get("/api/logs", params={"model": model, "limit": 50})
        assert got.status_code == 200, got.text
        for item in got.json()["items"]:
            if item["model"] == model and item["id"] > after_id:
                return item
        time.sleep(0.2)
    raise AssertionError(f"/api/logs 在 {timeout}s 内没出现 {model} 的新记录")


def _send(anon: httpx.Client, key: str, model: str, *, stream: bool = False,
          content: str = "hi") -> None:
    body = {"model": model, "messages": [{"role": "user", "content": content}]}
    if stream:
        body["stream"] = True
        with anon.stream("POST", "/v1/chat/completions", headers=_headers(key), json=body) as resp:
            assert resp.status_code == 200
            for _ in resp.iter_bytes():
                pass
        return
    r = anon.post("/v1/chat/completions", headers=_headers(key), json=body)
    assert r.status_code == 200, f"{r.status_code} {r.text}"


def test_gateway_estimate_off_keeps_missing_field_by_field(
    anon: httpx.Client, admin: httpx.Client, est_key: str, codebuddy_on_mock, db_path: str
):
    before_ledger = _max_id(db_path, "billing_ledger")
    before_log = _max_id(db_path, "request_logs")
    _send(anon, est_key, MODEL_OFF)

    row = _wait_ledger(db_path, before_ledger)
    assert row["note"] == "usage_missing"
    assert row["billed_micro"] == 0
    assert row["usage_estimated"] == 0
    assert row["estimated"] == 0
    assert (row["input_tokens"], row["output_tokens"]) == (0, 0)

    item = _wait_log(admin, model=MODEL_OFF, after_id=before_log)
    assert item["credit_status"] == "usage_missing"
    assert item["credit"] is None
    assert item["credit_estimated"] is False


def test_gateway_estimate_on_non_stream_lands_estimated(
    anon: httpx.Client, admin: httpx.Client, est_key: str, codebuddy_on_mock,
    db_path: str, estimate_on,
):
    before_ledger = _max_id(db_path, "billing_ledger")
    before_log = _max_id(db_path, "request_logs")
    _send(anon, est_key, MODEL_ON)

    row = _wait_ledger(db_path, before_ledger)
    assert row["usage_estimated"] == 1
    assert row["note"] == f"usage_estimated:{ESTIMATE_VERSION}"
    assert (row["input_tokens"], row["output_tokens"]) == (1, 1)
    assert row["billed_micro"] == TINY_MICRO

    item = _wait_log(admin, model=MODEL_ON, after_id=before_log)
    assert item["credit_status"] == "estimated"
    assert item["credit_estimated"] is True
    assert item["credit"] == TINY_MICRO
    # request_logs 的 token 列只存上游真值，估算不写进去（避免和真值混在一处）
    assert (item["prompt_tokens"], item["completion_tokens"]) == (0, 0)


def test_gateway_estimate_on_stream_lands_estimated(
    anon: httpx.Client, est_key: str, codebuddy_on_mock, db_path: str, estimate_on
):
    before_ledger = _max_id(db_path, "billing_ledger")
    _send(anon, est_key, MODEL_ON, stream=True)

    row = _wait_ledger(db_path, before_ledger)
    assert row["usage_estimated"] == 1
    assert (row["input_tokens"], row["output_tokens"]) == (1, 1)
    assert row["billed_micro"] == TINY_MICRO
    assert row["note"] == f"usage_estimated:{ESTIMATE_VERSION}"


def test_gateway_usage_zero_beats_estimate(
    anon: httpx.Client, admin: httpx.Client, est_key: str, codebuddy_on_mock,
    db_path: str, estimate_on,
):
    before_ledger = _max_id(db_path, "billing_ledger")
    before_log = _max_id(db_path, "request_logs")
    _send(anon, est_key, MODEL_ZERO)

    row = _wait_ledger(db_path, before_ledger)
    assert row["usage_estimated"] == 0
    assert row["billed_micro"] == 0
    assert row["note"] == "usage_zero:codebuddy"

    item = _wait_log(admin, model=MODEL_ZERO, after_id=before_log)
    assert item["credit_status"] == "billed"
    assert item["credit_estimated"] is False


def test_gateway_real_usage_beats_estimate(
    anon: httpx.Client, admin: httpx.Client, est_key: str, codebuddy_on_mock,
    db_path: str, estimate_on,
):
    before_ledger = _max_id(db_path, "billing_ledger")
    before_log = _max_id(db_path, "request_logs")
    _send(anon, est_key, MODEL_REAL)

    row = _wait_ledger(db_path, before_ledger)
    assert row["usage_estimated"] == 0
    assert (row["input_tokens"], row["output_tokens"]) == (11, 7)
    assert row["billed_micro"] == 78
    assert row["note"] == ""

    item = _wait_log(admin, model=MODEL_REAL, after_id=before_log)
    assert item["credit_status"] == "billed"
    assert item["credit_estimated"] is False


def test_gateway_estimate_skipped_when_no_output_text(
    anon: httpx.Client, est_key: str, codebuddy_on_mock, db_path: str, estimate_on
):
    before_ledger = _max_id(db_path, "billing_ledger")
    _send(anon, est_key, MODEL_EMPTY)

    row = _wait_ledger(db_path, before_ledger)
    assert row["note"] == "usage_missing"
    assert row["billed_micro"] == 0
    assert row["usage_estimated"] == 0, "估不出必须保持 usage_missing，不许造 0 金额的估算账"


def test_gateway_estimate_clamped_for_huge_prompt(
    anon: httpx.Client, est_key: str, codebuddy_on_mock, db_path: str, estimate_on
):
    before_ledger = _max_id(db_path, "billing_ledger")
    _send(anon, est_key, MODEL_LONG, content="x" * 1_000_000)

    row = _wait_ledger(db_path, before_ledger)
    assert row["usage_estimated"] == 1
    assert row["input_tokens"] == MAX_INPUT_TOKENS, "超长正文必须被上限钳住"
    assert row["output_tokens"] == 1
    assert row["billed_micro"] == MAX_INPUT_TOKENS * 2 + 8


def test_stats_summary_separates_estimated(
    anon: httpx.Client, admin: httpx.Client, est_key: str, codebuddy_on_mock,
    db_path: str, estimate_on,
):
    def summary() -> dict:
        r = admin.get("/api/stats/summary")
        assert r.status_code == 200, r.text
        return r.json()

    before = summary()
    _send(anon, est_key, MODEL_ON)
    after = summary()

    conn = sqlite3.connect(db_path)
    try:
        expected = conn.execute(
            "SELECT COALESCE(SUM(usage_estimated), 0), COALESCE(SUM(billed_micro), 0) "
            "FROM billing_ledger WHERE usage_estimated = 1"
        ).fetchone()
        all_micro = conn.execute(
            "SELECT COALESCE(SUM(billed_micro), 0) FROM billing_ledger"
        ).fetchone()[0]
    finally:
        conn.close()
    assert after["total_estimated_requests"] == int(expected[0])
    assert after["total_estimated_requests"] >= before["total_estimated_requests"] + 1
    assert after["total_estimated_credit"] == pytest.approx(expected[1] / 1_000_000)
    assert after["total_credit"] == pytest.approx(all_micro / 1_000_000), "总额仍含估算，便于对账"
    assert after["today_estimated_credit"] > 0
    assert after["week_estimated_credit"] > 0


def test_csv_export_marks_estimated_rows(
    anon: httpx.Client, admin: httpx.Client, est_key: str, codebuddy_on_mock,
    db_path: str, estimate_on,
):
    _send(anon, est_key, MODEL_ON)
    r = admin.get("/api/logs/export", params={"model": MODEL_ON})
    assert r.status_code == 200, r.text
    rows = list(csv.reader(io.StringIO(r.text)))
    header, data = rows[0], rows[1:]
    assert "credit_estimated" in header
    idx = header.index("credit_estimated")
    assert data, "导出应包含刚写入的估算请求"
    assert data[0][idx] == "true", data[0]
