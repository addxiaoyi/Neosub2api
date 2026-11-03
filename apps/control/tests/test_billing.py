"""M3-1 计费内核：token 拆分、价格解析、整数金额、流式 settle、快照一致性、API 接通。

单元用例直接连临时 SQLite（不启进程），集成用例走 conftest 起的控制面。
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.billing import (
    TOKEN_CLASSES,
    compute_cost_micro,
    import_price_books,
    resolve_prices,
    settle,
    split_usage,
)
from app.db import Database

REPO = Path(__file__).resolve().parents[3]
PRICES_YAML = REPO / "config" / "prices.yaml"


class SplitTests(unittest.TestCase):
    def test_openai_cached_and_reasoning_split(self):
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 10},
        }
        parts = split_usage(usage)
        self.assertEqual(parts["input"], 60, "prompt_tokens 含缓存命中，必须扣出来")
        self.assertEqual(parts["cache_read"], 40)
        self.assertEqual(parts["output"], 40, "completion_tokens 含推理 token，必须扣出来")
        self.assertEqual(parts["reasoning"], 10)
        self.assertEqual(parts["cache_write"], 0)

    def test_openai_responses_details(self):
        usage = {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 30},
            "output_tokens_details": {"reasoning_tokens": 5},
        }
        parts = split_usage(usage)
        self.assertEqual((parts["input"], parts["cache_read"]), (70, 30))
        self.assertEqual((parts["output"], parts["reasoning"]), (15, 5))

    def test_anthropic_cache_not_double_counted(self):
        usage = {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 10,
        }
        parts = split_usage(usage)
        self.assertEqual(parts["input"], 100, "Anthropic 的 input_tokens 本身不含缓存")
        self.assertEqual(parts["cache_read"], 50)
        self.assertEqual(parts["cache_write"], 10)
        self.assertEqual(parts["output"], 20)

    def test_missing_usage_is_all_zero(self):
        self.assertEqual(split_usage(None), {name: 0 for name in TOKEN_CLASSES})
        self.assertEqual(split_usage({}), {name: 0 for name in TOKEN_CLASSES})

    def test_negative_or_garbage_values_clamped(self):
        parts = split_usage({"prompt_tokens": -5, "completion_tokens": "abc"})
        self.assertEqual(parts["input"], 0)
        self.assertEqual(parts["output"], 0)


class CostTests(unittest.TestCase):
    def test_rounds_half_up_to_one_micro(self):
        # 3 token × 0.5 micro/token = 1.5 micro → 2
        total, unpriced = compute_cost_micro({"input": 3}, {"input": {"price_micro_per_1m": 500_000}})
        self.assertEqual(total, 2)
        self.assertEqual(unpriced, [])

    def test_reasoning_falls_back_to_output_price(self):
        total, unpriced = compute_cost_micro(
            {"reasoning": 100}, {"output": {"price_micro_per_1m": 10_000_000}}
        )
        self.assertEqual(total, 1000)
        self.assertEqual(unpriced, [])

    def test_unpriced_classes_reported(self):
        total, unpriced = compute_cost_micro(
            {"input": 11, "output": 7}, {"input": {"price_micro_per_1m": 2_000_000}}
        )
        self.assertEqual(total, 22)
        self.assertEqual(unpriced, ["output"])

    def test_gpt4o_reference_amount(self):
        # 真实例子：gpt-4o 输入 2.50 美元/百万、输出 10 美元/百万；用量 11/7
        total, _ = compute_cost_micro(
            {"input": 11, "output": 7},
            {
                "input": {"price_micro_per_1m": 2_500_000},
                "output": {"price_micro_per_1m": 10_000_000},
            },
        )
        self.assertEqual(total, 98)


class _DbCase:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "unified.db")

    def tearDown(self):
        self.tmp.cleanup()

    def conn(self) -> sqlite3.Connection:
        return self.db.connect()

    def add_price(self, provider: str, model: str, token_class: str, micro: int, at: int = 0) -> None:
        conn = self.conn()
        try:
            conn.execute(
                "INSERT INTO price_books(provider, model, token_class, price_micro_per_1m, "
                "effective_from) VALUES(?,?,?,?,?)",
                (provider, model, token_class, micro, at),
            )
            conn.commit()
        finally:
            conn.close()


class ResolveTests(_DbCase, unittest.TestCase):
    def test_exact_provider_beats_wildcard(self):
        self.add_price("*", "m", "input", 1_000_000)
        self.add_price("codebuddy", "m", "input", 2_000_000)
        conn = self.conn()
        try:
            lines = resolve_prices(conn, provider="codebuddy", model="m", at=100)
        finally:
            conn.close()
        self.assertEqual(lines["input"]["price_micro_per_1m"], 2_000_000)
        self.assertEqual(lines["input"]["provider"], "codebuddy")

    def test_wildcard_row_used_when_no_exact(self):
        self.add_price("*", "m", "output", 8_000_000)
        conn = self.conn()
        try:
            lines = resolve_prices(conn, provider="codebuddy", model="m", at=100)
        finally:
            conn.close()
        self.assertEqual(lines["output"]["price_micro_per_1m"], 8_000_000)

    def test_newest_effective_from_wins(self):
        self.add_price("*", "m", "input", 1_000_000, at=100)
        self.add_price("*", "m", "input", 3_000_000, at=200)
        conn = self.conn()
        try:
            old = resolve_prices(conn, provider="p", model="m", at=150)
            new = resolve_prices(conn, provider="p", model="m", at=200)
        finally:
            conn.close()
        self.assertEqual(old["input"]["price_micro_per_1m"], 1_000_000)
        self.assertEqual(new["input"]["price_micro_per_1m"], 3_000_000)

    def test_future_row_not_visible(self):
        self.add_price("*", "m", "input", 1_000_000, at=1000)
        conn = self.conn()
        try:
            lines = resolve_prices(conn, provider="p", model="m", at=999)
        finally:
            conn.close()
        self.assertEqual(lines, {})


class SettleTests(_DbCase, unittest.TestCase):
    def _pricing(self, conn, provider: str, model: str, at: int) -> dict:
        return {
            "provider": provider,
            "model": model,
            "at": at,
            "lines": resolve_prices(conn, provider=provider, model=model, at=at),
        }

    def _last_ledger(self) -> sqlite3.Row:
        conn = self.conn()
        try:
            return conn.execute("SELECT * FROM billing_ledger ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()

    def test_writes_ledger_and_snapshot(self):
        self.add_price("*", "m", "input", 2_000_000)
        self.add_price("*", "m", "output", 8_000_000)
        conn = self.conn()
        try:
            pricing = self._pricing(conn, "p", "m", 500)
            settle(
                conn, ts=600, key_id=7, key_name="k", provider="p", model="m", status=200,
                usage={"prompt_tokens": 11, "completion_tokens": 7}, pricing=pricing,
            )
            conn.commit()
        finally:
            conn.close()
        row = self._last_ledger()
        self.assertEqual(row["billed_micro"], 78)
        self.assertEqual(row["input_tokens"], 11)
        self.assertEqual(row["output_tokens"], 7)
        self.assertEqual(row["estimated"], 0)
        self.assertIsNotNone(row["price_snapshot_id"])
        conn = self.conn()
        try:
            snap = conn.execute(
                "SELECT * FROM price_book_snapshots WHERE id = ?", (row["price_snapshot_id"],)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(snap["resolved_at"], 500, "快照时间必须是准入时刻，不是落库时刻")
        self.assertIn("input", snap["lines_json"])

    def test_price_change_during_request_does_not_change_bill(self):
        """准入时已解析并冻结：请求进行中插入更晚生效的新价，settle 仍按旧价记账。"""
        self.add_price("codebuddy", "m", "input", 2_000_000, at=0)
        conn = self.conn()
        try:
            pricing = self._pricing(conn, "codebuddy", "m", 1_000)
            # 模拟请求进行中有人改价
            conn.execute(
                "INSERT INTO price_books(provider, model, token_class, price_micro_per_1m, effective_from) "
                "VALUES(?, ?, ?, ?, ?)",
                ("codebuddy", "m", "input", 9_000_000, 1001),
            )
            conn.commit()
            settle(
                conn, ts=1005, key_id=1, key_name="k", provider="codebuddy", model="m", status=200,
                usage={"prompt_tokens": 10}, pricing=pricing,
            )
            conn.commit()
            billed = conn.execute(
                "SELECT billed_micro FROM billing_ledger ORDER BY id DESC LIMIT 1"
            ).fetchone()["billed_micro"]
            # 新价对更晚的请求可见
            later = resolve_prices(conn, provider="codebuddy", model="m", at=1005)
        finally:
            conn.close()
        self.assertEqual(billed, 20, "在途请求按准入快照 2.00 计价")
        self.assertEqual(later["input"]["price_micro_per_1m"], 9_000_000)

    def test_unpriced_model_marks_estimated(self):
        conn = self.conn()
        try:
            settle(
                conn, ts=1, key_id=1, key_name="k", provider="p", model="kimi-k2.7", status=200,
                usage={"prompt_tokens": 10, "completion_tokens": 3}, pricing=None,
            )
            conn.commit()
        finally:
            conn.close()
        row = self._last_ledger()
        self.assertEqual(row["billed_micro"], 0)
        self.assertEqual(row["estimated"], 1)
        self.assertEqual(row["note"], "unpriced:input,output")
        self.assertIsNone(row["price_snapshot_id"], "没有价格行就不写空快照")

    def test_usage_missing_noted_without_amount(self):
        conn = self.conn()
        try:
            settle(
                conn, ts=1, key_id=None, key_name="anonymous", provider="p", model="m",
                status=200, usage=None, pricing=None,
            )
            conn.commit()
        finally:
            conn.close()
        row = self._last_ledger()
        self.assertEqual(row["billed_micro"], 0)
        self.assertEqual(row["note"], "usage_missing")
        self.assertIsNone(row["key_id"])

    def test_all_zero_usage_noted_with_provider(self):
        conn = self.conn()
        try:
            settle(
                conn, ts=1, key_id=1, key_name="k", provider="muse", model="gpt-4o-mini",
                status=200,
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                pricing=None,
            )
            conn.commit()
        finally:
            conn.close()
        row = self._last_ledger()
        self.assertEqual(row["billed_micro"], 0)
        self.assertEqual(row["estimated"], 0, "全零是确定的 0，不是估值")
        self.assertEqual(row["note"], "usage_zero:muse")

    def test_zero_usage_and_missing_usage_are_noted_differently(self):
        conn = self.conn()
        try:
            settle(
                conn, ts=1, key_id=1, key_name="k", provider="muse", model="m", status=200,
                usage={"prompt_tokens": 0, "completion_tokens": 0}, pricing=None,
            )
            settle(
                conn, ts=2, key_id=1, key_name="k", provider="muse", model="m", status=200,
                usage=None, pricing=None,
            )
            conn.commit()
            notes = [row["note"] for row in conn.execute(
                "SELECT note FROM billing_ledger ORDER BY id"
            )]
        finally:
            conn.close()
        self.assertEqual(notes, ["usage_zero:muse", "usage_missing"])

    def test_failed_request_without_usage_is_not_billed(self):
        conn = self.conn()
        try:
            result = settle(
                conn, ts=1, key_id=1, key_name="k", provider="p", model="m", status=502,
                usage=None, pricing=None,
            )
            conn.commit()
            count = conn.execute("SELECT COUNT(*) AS n FROM billing_ledger").fetchone()["n"]
        finally:
            conn.close()
        self.assertFalse(result["written"])
        self.assertEqual(count, 0)


class ImportTests(_DbCase, unittest.TestCase):
    def test_import_is_idempotent_and_keeps_unpriced_unpriced(self):
        conn = self.conn()
        try:
            first = import_price_books(conn, PRICES_YAML)
            conn.commit()
            second = import_price_books(conn, PRICES_YAML)
            conn.commit()
            counted = conn.execute("SELECT COUNT(*) AS n FROM price_books").fetchone()["n"]
            leaked = conn.execute(
                "SELECT COUNT(*) AS n FROM price_books WHERE model IN "
                "('kimi-k2.7','hy3','hy3-preview','DeepSeek-V4-Flash-Official')"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertGreaterEqual(first["total"], 30, "种子价应有 30 条以上")
        self.assertEqual(first["inserted"], first["total"])
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["skipped"], first["total"])
        self.assertEqual(counted, first["total"])
        self.assertEqual(leaked, 0, "未定价模型不许编价入库")
        self.assertEqual(
            sorted(first["unpriced"]),
            sorted(["kimi-k2.7", "hy3", "hy3-preview", "DeepSeek-V4-Flash-Official"]),
        )

    def test_dry_run_writes_nothing(self):
        conn = self.conn()
        try:
            result = import_price_books(conn, PRICES_YAML, dry_run=True)
            conn.commit()
            counted = conn.execute("SELECT COUNT(*) AS n FROM price_books").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(counted, 0)
        self.assertTrue(result["dry_run"])
        self.assertGreater(result["inserted"], 0)


# ===== 集成用例：走真实网关进程 =====

@pytest.fixture(scope="module")
def billing_key(admin: httpx.Client) -> str:
    r = admin.post("/api/keys", json={"name": "billing-suite"})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()["key"]


@pytest.fixture(scope="module", autouse=True)
def seed_model_prices(admin: httpx.Client) -> None:
    """给 codebuddy/billing-m 录输入 2.00、输出 8.00（micro 整数）。"""
    for token_class, micro in (("input", 2_000_000), ("output", 8_000_000)):
        body = {
            "provider": "codebuddy",
            "model": "billing-m",
            "token_class": token_class,
            "price_micro_per_1m": micro,
            "effective_from": 0,
        }
        r = admin.post("/api/pricing/books", json=body)
        assert r.status_code in (200, 409), f"{r.status_code} {r.text}"


def _last_ledger(db_path: str, after_id: int = 0) -> sqlite3.Row | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM billing_ledger WHERE id > ? ORDER BY id DESC LIMIT 1", (after_id,)
        ).fetchone()
    finally:
        conn.close()


def _max_ledger_id(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM billing_ledger").fetchone()
        return int(row[0])
    finally:
        conn.close()


def _wait_ledger(db_path: str, after_id: int, timeout: float = 3.0) -> sqlite3.Row:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = _last_ledger(db_path, after_id)
        if row is not None:
            return row
        time.sleep(0.1)
    raise AssertionError(f"billing_ledger 在 {timeout}s 内没有新增行")


def _gateway_headers(billing_key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {billing_key}", "x-provider": "codebuddy"}


@pytest.mark.usage
def test_non_stream_request_writes_ledger(anon: httpx.Client, billing_key: str, db_path: str):
    before = _max_ledger_id(db_path)
    r = anon.post(
        "/v1/chat/completions",
        headers=_gateway_headers(billing_key),
        json={"model": "billing-m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    row = _wait_ledger(db_path, before)
    assert row["model"] == "billing-m"
    assert (row["input_tokens"], row["output_tokens"]) == (11, 7), "假上游固定 usage 11/7"
    assert row["billed_micro"] == 78, f"11×2 + 7×8 应为 78 micro，拿到 {row['billed_micro']}"
    assert row["estimated"] == 0
    assert row["price_snapshot_id"] is not None


@pytest.mark.usage
def test_stream_request_writes_ledger(anon: httpx.Client, billing_key: str, db_path: str):
    before = _max_ledger_id(db_path)
    with anon.stream(
        "POST",
        "/v1/chat/completions",
        headers=_gateway_headers(billing_key),
        json={"model": "billing-m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as resp:
        assert resp.status_code == 200
        for _ in resp.iter_bytes():
            pass
    row = _wait_ledger(db_path, before)
    assert (row["input_tokens"], row["output_tokens"]) == (11, 7)
    assert row["billed_micro"] == 78
    assert row["price_snapshot_id"] is not None, "流式 settle 也要落快照"


@pytest.mark.usage
def test_stats_credit_matches_ledger(admin: httpx.Client, db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        total_micro = int(
            conn.execute("SELECT COALESCE(SUM(billed_micro), 0) FROM billing_ledger").fetchone()[0]
        )
    finally:
        conn.close()
    r = admin.get("/api/stats/summary")
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    assert r.json()["total_credit"] == pytest.approx(total_micro / 1_000_000, abs=1e-6)


@pytest.mark.usage
def test_pricing_api_append_only(admin: httpx.Client):
    body = {
        "provider": "codebuddy",
        "model": "billing-m",
        "token_class": "input",
        "price_micro_per_1m": 3_000_000,
        "effective_from": 123,
    }
    created = admin.post("/api/pricing/books", json=body)
    assert created.status_code == 200, f"{created.status_code} {created.text}"
    assert created.json()["price_micro_per_1m"] == 3_000_000

    duplicate = admin.post("/api/pricing/books", json=body)
    assert duplicate.status_code == 409, "同一版本重复写入必须拒绝，不允许原地改价"

    listed = admin.get("/api/pricing/books", params={"model": "billing-m"})
    assert listed.status_code == 200
    rows = [row for row in listed.json() if row["token_class"] == "input"]
    assert len(rows) == 2, f"两版价格应同时保留：{[r['effective_from'] for r in rows]}"

    resolved = admin.get("/api/pricing/resolve", params={"provider": "codebuddy", "model": "billing-m"})
    assert resolved.status_code == 200
    assert "input" in resolved.json()["lines"]


@pytest.mark.usage
def test_pricing_write_requires_admin(anon: httpx.Client, base_url: str):
    viewer = httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)
    created = httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)
    try:
        # 用独立 client 建 viewer，避免共用 anon 的 admin 会话 cookie
        login = created.post("/api/login", json={"username": "admin", "password": "test"})
        assert login.status_code == 200
        made = created.post(
            "/api/users",
            json={"username": "billing-viewer", "password": "viewer-pass", "role": "viewer"},
        )
        assert made.status_code in (200, 409), f"{made.status_code} {made.text}"
        assert viewer.post(
            "/api/login", json={"username": "billing-viewer", "password": "viewer-pass"}
        ).status_code == 200

        assert viewer.get("/api/pricing/books").status_code == 200, "viewer 可读"
        denied = viewer.post(
            "/api/pricing/books",
            json={"model": "billing-m", "token_class": "output", "price_micro_per_1m": 1, "effective_from": 456},
        )
        assert denied.status_code == 403, f"viewer 写价格应 403：{denied.status_code}"
    finally:
        viewer.close()
        created.close()


@pytest.mark.usage
def test_pricing_import_dry_run(admin: httpx.Client):
    r = admin.post("/api/pricing/import", params={"dry_run": True})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    assert body["total"] >= 30
    assert "kimi-k2.7" in body["unpriced"]
    assert body["path"].endswith("prices.yaml")


@pytest.mark.usage
def test_unpriced_model_request_is_flagged(anon: httpx.Client, billing_key: str, db_path: str):
    """prices.yaml 里明确未定价的模型，请求不编价：金额 0 + estimated=1 + note。"""
    before = _max_ledger_id(db_path)
    r = anon.post(
        "/v1/chat/completions",
        headers=_gateway_headers(billing_key),
        json={"model": "kimi-k2.7", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    row = _wait_ledger(db_path, before)
    assert row["billed_micro"] == 0
    assert row["estimated"] == 1
    assert row["note"].startswith("unpriced:")