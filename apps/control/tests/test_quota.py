"""M3-2 / M3-3：配额拦截与阈值告警。

单元用例直接连临时 SQLite（不启进程），集成用例走 conftest 起的控制面 + 假上游。
假上游 chat 固定 usage 11/7；把 quota-m 定价为 input 2.00 / output 8.00，单请求 78 micro。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import httpx
import pytest

from app.api_utils import mask_access_args, mask_secret, mask_text
from app.billing import (
    check_failure_rate,
    check_quota,
    evaluate_alerts,
    period_bounds,
    settle,
    window_usage,
)
from app.db import Database

TZ8 = dt.timezone(dt.timedelta(hours=8))
MODEL = "quota-m"
PER_REQUEST_MICRO = 78


# ===== 单元用例 =====


def _tz(*args: int) -> int:
    return int(dt.datetime(*args, tzinfo=TZ8).timestamp())


class PeriodTests(unittest.TestCase):
    def test_day_boundary_is_utc8(self):
        # 2026-11-02 23:30 UTC+8 仍属于 11-02
        at = _tz(2026, 11, 2, 23, 30)
        start, end = period_bounds("day", at)
        self.assertEqual(start, _tz(2026, 11, 2, 0, 0))
        self.assertEqual(end, _tz(2026, 11, 3, 0, 0))

    def test_month_boundary_is_utc8(self):
        at = _tz(2026, 11, 2, 23, 30)
        start, end = period_bounds("month", at)
        self.assertEqual(start, _tz(2026, 11, 1, 0, 0))
        self.assertEqual(end, _tz(2026, 12, 1, 0, 0))

    def test_unknown_window_rejected(self):
        with self.assertRaises(ValueError):
            period_bounds("total", 0)


class _DbCase:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "unified.db")
        conn = self.conn()
        try:
            conn.execute(
                "INSERT INTO api_keys(name, key_hash, prefix, enabled, created_at) "
                "VALUES('k','h','u2a_',1,0)"
            )
            conn.commit()
            self.key_id = int(conn.execute("SELECT id FROM api_keys").fetchone()["id"])
            conn.execute(
                "INSERT INTO quota_policies(key_id, provider, model, window, limit_micro, "
                "limit_requests, enabled, created_at, updated_at) VALUES(?, '', '', 'day', 100, 0, 1, 0, 0)",
                (self.key_id,),
            )
            self.policy_id = int(conn.execute("SELECT id FROM quota_policies").fetchone()["id"])
            conn.execute(
                "INSERT INTO quota_alert_thresholds(policy_id, percent, created_at) VALUES(?, 80, 0)",
                (self.policy_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def conn(self) -> sqlite3.Connection:
        return self.db.connect()

    def add_bill(self, ts: int, micro: int, provider: str = "codebuddy", model: str = MODEL) -> None:
        conn = self.conn()
        try:
            conn.execute(
                "INSERT INTO billing_ledger(ts, key_id, key_name, provider, model, status, billed_micro) "
                "VALUES(?,?,?,?,?,200,?)",
                (ts, self.key_id, "k", provider, model, micro),
            )
            conn.commit()
        finally:
            conn.close()

    def policy(self) -> sqlite3.Row:
        conn = self.conn()
        try:
            return conn.execute("SELECT * FROM quota_policies WHERE id = ?", (self.policy_id,)).fetchone()
        finally:
            conn.close()


class CheckQuotaTests(_DbCase, unittest.TestCase):
    def test_under_amount_limit_passes(self):
        self.add_bill(_tz(2026, 11, 2, 9, 0), 40)
        conn = self.conn()
        try:
            self.assertIsNone(
                check_quota(conn, key_id=self.key_id, provider="codebuddy", model=MODEL,
                            at=_tz(2026, 11, 2, 10, 0))
            )
        finally:
            conn.close()

    def test_amount_limit_denial_has_full_fields(self):
        for _ in range(2):
            self.add_bill(_tz(2026, 11, 2, 9, 0), 50)
        at = _tz(2026, 11, 2, 10, 0)
        conn = self.conn()
        try:
            denial = check_quota(conn, key_id=self.key_id, provider="codebuddy", model=MODEL, at=at)
        finally:
            conn.close()
        self.assertIsNotNone(denial)
        assert denial is not None
        self.assertEqual(denial["type"], "quota_exceeded")
        self.assertEqual(denial["window"], "day")
        self.assertEqual(denial["metric"], "micro")
        self.assertEqual((denial["used"], denial["limit"]), (100, 100))
        self.assertEqual(denial["reset_at"], _tz(2026, 11, 3, 0, 0))
        self.assertGreaterEqual(denial["reset_after"], 0)
        self.assertIn("上限 100", denial["message"])

    def test_request_limit_denial(self):
        conn = self.conn()
        try:
            conn.execute("UPDATE quota_policies SET limit_micro = 0, limit_requests = 1 WHERE id = ?",
                         (self.policy_id,))
            conn.commit()
        finally:
            conn.close()
        self.add_bill(_tz(2026, 11, 2, 9, 0), 0)
        conn = self.conn()
        try:
            denial = check_quota(conn, key_id=self.key_id, provider="codebuddy", model=MODEL,
                                 at=_tz(2026, 11, 2, 10, 0))
        finally:
            conn.close()
        assert denial is not None
        self.assertEqual(denial["metric"], "requests")
        self.assertEqual((denial["used"], denial["limit"]), (1, 1))

    def test_usage_is_scoped_by_provider_and_model(self):
        self.add_bill(_tz(2026, 11, 2, 9, 0), 60, provider="other", model="other-m")
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE quota_policies SET provider = 'codebuddy', model = ? WHERE id = ?",
                (MODEL, self.policy_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM quota_policies WHERE id = ?", (self.policy_id,)).fetchone()
            usage = window_usage(conn, row, _tz(2026, 11, 2, 10, 0))
            denial = check_quota(conn, key_id=self.key_id, provider="codebuddy", model=MODEL,
                                 at=_tz(2026, 11, 2, 10, 0))
        finally:
            conn.close()
        self.assertEqual(usage["used_micro"], 0, "别的 provider/模型不计入本策略")
        self.assertIsNone(denial)

    def test_anonymous_key_id_is_never_blocked(self):
        self.add_bill(_tz(2026, 11, 2, 9, 0), 100)
        conn = self.conn()
        try:
            self.assertIsNone(check_quota(conn, key_id=None, provider="codebuddy", model=MODEL,
                                          at=_tz(2026, 11, 2, 10, 0)))
        finally:
            conn.close()


class AlertTests(_DbCase, unittest.TestCase):
    def test_alert_fires_once_per_threshold_per_period(self):
        day = _tz(2026, 11, 2, 9, 0)
        self.add_bill(day, 80)
        conn = self.conn()
        try:
            first = evaluate_alerts(conn, key_id=self.key_id, provider="codebuddy", model=MODEL, at=day)
            conn.commit()
            second = evaluate_alerts(conn, key_id=self.key_id, provider="codebuddy", model=MODEL, at=day)
            conn.commit()
            count = conn.execute("SELECT COUNT(*) AS n FROM quota_alerts").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], "同一周期同一阈值只告警一次")
        self.assertEqual(count, 1)
        self.assertEqual(first[0]["threshold_percent"], 80)

    def test_period_flip_allows_new_alert(self):
        day1 = _tz(2026, 11, 2, 9, 0)
        day2 = _tz(2026, 11, 3, 9, 0)
        self.add_bill(day1, 80)
        self.add_bill(day2, 80)
        conn = self.conn()
        try:
            a = evaluate_alerts(conn, key_id=self.key_id, provider="codebuddy", model=MODEL, at=day1)
            conn.commit()
            b = evaluate_alerts(conn, key_id=self.key_id, provider="codebuddy", model=MODEL, at=day2)
            conn.commit()
            starts = [
                row["period_start"]
                for row in conn.execute("SELECT period_start FROM quota_alerts ORDER BY id")
            ]
        finally:
            conn.close()
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertEqual(len(set(starts)), 2, "周期翻转后应重新告警，period_start 不同")


class _EmptyCursor:
    """查重被绕过时的空结果：fetchone 返回 None 等价于「本周期还没告警过」。"""

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FlakyAlertsConn:
    """把 quota_alerts 的查重查询替换成指定故障，注入单条阈值的评估失败。

    mode='miss'：查重永远查无，随后的 INSERT 会真实撞 UNIQUE 约束；
    mode='raise'：这条查询本身抛错。percent=None 表示对全部阈值生效。
    """

    def __init__(self, real: sqlite3.Connection, *, percent: int | None, mode: str) -> None:
        self._real = real
        self._percent = percent
        self._mode = mode

    def execute(self, sql: str, params: tuple = ()):
        if "SELECT id FROM quota_alerts" in sql and (
            self._percent is None or int(params[1]) == self._percent
        ):
            if self._mode == "raise":
                raise sqlite3.OperationalError("database is locked")
            return _EmptyCursor()
        return self._real.execute(sql, params)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class AlertIsolationTests(_DbCase, unittest.TestCase):
    """逐阈值隔离：单条阈值评估抛异常，不得带走同策略的其它阈值。"""

    def setUp(self):
        super().setUp()
        conn = self.conn()
        try:
            # _DbCase 只建了 80，补 50 / 100，凑齐一次跨三档
            for percent in (50, 100):
                conn.execute(
                    "INSERT INTO quota_alert_thresholds(policy_id, percent, created_at) VALUES(?, ?, 0)",
                    (self.policy_id, percent),
                )
            conn.commit()
        finally:
            conn.close()

    def _alert_percents(self) -> list[int]:
        conn = self.conn()
        try:
            return [
                int(row["threshold_percent"])
                for row in conn.execute(
                    "SELECT threshold_percent FROM quota_alerts ORDER BY threshold_percent"
                )
            ]
        finally:
            conn.close()

    def _seed_alert(self, percent: int, period_start: int) -> None:
        """预置一条告警，模拟并发赢家已抢先写入这一档。"""
        conn = self.conn()
        try:
            conn.execute(
                "INSERT INTO quota_alerts(policy_id, threshold_percent, window, period_start, "
                "triggered_at, metric, used_micro, limit_micro, used_requests, limit_requests) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (self.policy_id, percent, "day", period_start, 1_000_000, "micro", 100, 100, 0, 0),
            )
            conn.commit()
        finally:
            conn.close()

    def test_integrity_error_on_first_threshold_blocks_no_others(self):
        day = _tz(2026, 11, 2, 9, 0)
        self.add_bill(day, 100)  # 上限 100、已用 100 → 50 / 80 / 100 三档全跨
        self._seed_alert(50, period_bounds("day", day)[0])
        conn = self.conn()
        try:
            flaky = _FlakyAlertsConn(conn, percent=50, mode="miss")
            with self.assertLogs("unified.billing", level="ERROR") as logs:
                created = evaluate_alerts(
                    flaky, key_id=self.key_id, provider="codebuddy", model=MODEL, at=day
                )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(
            [item["threshold_percent"] for item in created],
            [80, 100],
            "50 档撞 UNIQUE 后，80 / 100 必须继续评估并写入",
        )
        self.assertEqual(self._alert_percents(), [50, 80, 100], "50 只留赢家那一行，不许重复")
        failures = [r for r in logs.records if "配额告警阈值评估失败" in r.getMessage()]
        self.assertEqual(len(failures), 1, "失败阈值要留一条 ERROR 日志")
        for field in (
            f"policy_id={self.policy_id}",
            "threshold=50",
            "provider=codebuddy",
            f"model={MODEL}",
            "window=day",
        ):
            self.assertIn(field, failures[0].getMessage())
        self.assertIsInstance(failures[0].exc_info[1], sqlite3.IntegrityError)
        self.assertIn("quota_alerts", str(failures[0].exc_info[1]))

    def test_failed_threshold_retried_on_next_evaluation(self):
        day = _tz(2026, 11, 2, 9, 0)
        self.add_bill(day, 100)
        conn = self.conn()
        try:
            flaky = _FlakyAlertsConn(conn, percent=50, mode="raise")
            with self.assertLogs("unified.billing", level="ERROR"):
                first = evaluate_alerts(
                    flaky, key_id=self.key_id, provider="codebuddy", model=MODEL, at=day
                )
            conn.commit()
            second = evaluate_alerts(
                conn, key_id=self.key_id, provider="codebuddy", model=MODEL, at=day
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual([item["threshold_percent"] for item in first], [80, 100])
        self.assertEqual(
            [item["threshold_percent"] for item in second],
            [50],
            "失败阈值本轮跳过，下一轮重评补发，不记失败状态",
        )
        self.assertEqual(self._alert_percents(), [50, 80, 100], "重试不产生重复行")

    def test_every_threshold_failing_still_settles(self):
        day = _tz(2026, 11, 2, 9, 0)
        self.add_bill(day, 100)
        conn = self.conn()
        try:
            flaky = _FlakyAlertsConn(conn, percent=None, mode="raise")
            with self.assertLogs("unified.billing", level="ERROR") as logs:
                result = settle(
                    flaky, ts=day, key_id=self.key_id, key_name="k", provider="codebuddy",
                    model=MODEL, status=200, usage={"prompt_tokens": 11, "completion_tokens": 7},
                )
            conn.commit()
            ledger = conn.execute(
                "SELECT COUNT(*) AS n FROM billing_ledger WHERE key_id = ?", (self.key_id,)
            ).fetchone()["n"]
            alerts = conn.execute("SELECT COUNT(*) AS n FROM quota_alerts").fetchone()["n"]
        finally:
            conn.close()
        self.assertTrue(result["written"], "告警全失败不能带走已写入的流水")
        self.assertEqual(
            len([r for r in logs.records if "配额告警阈值评估失败" in r.getMessage()]),
            3,
            "三档各自记录失败，一条都不能吞掉不报",
        )
        self.assertEqual(alerts, 0, "全部失败时不写告警")
        self.assertEqual(ledger, 2, "种子 1 条 + 本次 settle 1 条")


class LegacyApiKeysMigrationTests(unittest.TestCase):
    def test_legacy_table_gets_owner_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(path)
            try:
                # 模拟老库：api_keys 没有 owner_user_id，且已有存量行
                conn.execute(
                    "CREATE TABLE api_keys(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                    "key_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL, "
                    "enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, last_used_at INTEGER)"
                )
                conn.execute(
                    "INSERT INTO api_keys(name, key_hash, prefix, enabled, created_at) "
                    "VALUES('old','oldhash','u2a_old',1,0)"
                )
                conn.commit()
            finally:
                conn.close()

            Database(path)  # 启动即幂等补列
            db = Database(path)  # 再开一次不能报错（幂等）
            conn = db.connect()
            try:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)")}
                self.assertIn("owner_user_id", columns)
                owner = conn.execute(
                    "SELECT owner_user_id FROM api_keys WHERE name = 'old'"
                ).fetchone()["owner_user_id"]
                self.assertIsNone(owner, "存量 key 应保持无主（NULL）")
            finally:
                conn.close()


class FailureRateTests(_DbCase, unittest.TestCase):
    def add_log(self, ts: int, status: int, error: str = "") -> None:
        conn = self.conn()
        try:
            conn.execute(
                "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, completion_tokens, "
                "latency_ms, stream, error, key_id, key_name) VALUES(?,?,?,?,0,0,0,0,?,?,?)",
                (ts, "p", "m", status, error, self.key_id, "k"),
            )
            conn.commit()
        finally:
            conn.close()

    def check(self, at: int, window: int = 60, limit: int = 3, key_id: object = "self"):
        conn = self.conn()
        try:
            return check_failure_rate(
                conn,
                key_id=self.key_id if key_id == "self" else key_id,
                window_sec=window,
                max_failures=limit,
                at=at,
            )
        finally:
            conn.close()

    def test_success_logs_do_not_count(self):
        for _ in range(5):
            self.add_log(990, 200)
        self.assertIsNone(self.check(at=1000))

    def test_threshold_counts_4xx_5xx_and_error_field(self):
        self.add_log(900, 500, "old failure")  # 窗口外，不计
        self.add_log(980, 500, "boom")
        self.add_log(980, 400, "bad request")
        self.add_log(985, 200, "stream error")  # status 正常但 error 非空也算失败
        denial = self.check(at=1000)
        assert denial is not None
        self.assertEqual(denial["type"], "failure_rate_limited")
        self.assertEqual(denial["metric"], "failures")
        self.assertEqual(denial["unit"], "requests")
        self.assertEqual((denial["used"], denial["limit"]), (3, 3))
        self.assertEqual(denial["reset_after"], 40, "最早一条 980 + 60 - 1000")
        self.assertIn("上限 3 次", denial["message"])

    def test_old_failures_expire_automatically(self):
        for _ in range(3):
            self.add_log(910, 500, "boom")
        self.assertIsNone(self.check(at=1000), "窗外失败不占名额")

    def test_gateway_429_rejections_do_not_count(self):
        # 失败限流 / 配额拒绝本身落 status=429；若计入窗口，客户端持续重试就会自锁
        for index in range(3):
            self.add_log(990 + index, 429, '{"type": "failure_rate_limited"}')
        self.assertIsNone(self.check(at=1000), "429 拒绝不得占失败名额")

    def test_disabled_when_limit_or_key_absent(self):
        for _ in range(5):
            self.add_log(990, 500, "boom")
        self.assertIsNone(self.check(at=1000, limit=0))
        self.assertIsNone(self.check(at=1000, window=0))
        self.assertIsNone(self.check(at=1000, key_id=None))


# ===== 集成用例：真实网关进程 =====


_CREATED_KEYS: list[int] = []


@pytest.fixture(scope="module", autouse=True)
def seed_quota_prices(admin: httpx.Client):
    for token_class, micro in (("input", 2_000_000), ("output", 8_000_000)):
        r = admin.post(
            "/api/pricing/books",
            json={
                "provider": "*",
                "model": MODEL,
                "token_class": token_class,
                "price_micro_per_1m": micro,
                "effective_from": 0,
            },
        )
        assert r.status_code in (200, 409), f"{r.status_code} {r.text}"
    yield
    # 模块结束回收本文件建的密钥；否则它们的策略会留给后面的用例（admin 策略列表是全局的）
    for key_id in _CREATED_KEYS:
        admin.delete(f"/api/keys/{key_id}")
    _CREATED_KEYS.clear()


def _new_key(admin: httpx.Client, owner: str | None = None) -> tuple[int, str]:
    payload: dict = {"name": f"quota-{uuid.uuid4().hex[:8]}"}
    if owner is not None:
        payload["owner_user_id"] = owner
    r = admin.post("/api/keys", json=payload)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    _CREATED_KEYS.append(int(body["id"]))
    return int(body["id"]), body["key"]


def _policy(admin: httpx.Client, key_id: int, **fields) -> dict:
    body = {"key_id": key_id, "window": "day", **fields}
    r = admin.post("/api/quota/policies", json=body)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()


def _chat(client: httpx.Client, token: str, model: str = MODEL) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {token}", "x-provider": "codebuddy"},
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


def _ledger_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM billing_ledger").fetchone()[0])
    finally:
        conn.close()


def _wait_ledger_count(db_path: str, expected: int, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _ledger_count(db_path) >= expected:
            return
        time.sleep(0.1)
    raise AssertionError(f"billing_ledger 在 {timeout}s 内没有达到 {expected} 行")


@pytest.mark.usage
def test_amount_over_limit_returns_429_with_full_detail(
    admin: httpx.Client, anon: httpx.Client, db_path: str
):
    key_id, token = _new_key(admin)
    policy = _policy(admin, key_id, limit_micro=50)
    assert policy["limit_micro"] == 50

    before = _ledger_count(db_path)
    allowed = _chat(anon, token)
    assert allowed.status_code == 200, f"{allowed.status_code} {allowed.text}"
    _wait_ledger_count(db_path, before + 1)

    rejected = _chat(anon, token)
    assert rejected.status_code == 429, f"{rejected.status_code} {rejected.text}"
    detail = rejected.json()["detail"]
    assert detail["type"] == "quota_exceeded"
    assert detail["layer"] == "key"
    assert detail["window"] == "day"
    assert detail["metric"] == "micro"
    assert detail["used"] == PER_REQUEST_MICRO
    assert detail["limit"] == 50
    assert detail["unit"] == "micro_usd"
    assert detail["period_start"] > 0
    assert detail["reset_at"] > detail["period_start"]
    assert detail["reset_after"] >= 0
    assert "上限 50" in detail["message"], "文案要写清上限，不能只说配额不足"
    assert int(rejected.headers["retry-after"]) >= 0

    assert _ledger_count(db_path) == before + 1, "被拒请求不能写 billing_ledger"

    logs = admin.get("/api/logs", params={"key_id": key_id, "status": "error"})
    assert logs.status_code == 200, f"{logs.status_code} {logs.text}"
    items = logs.json()["items"]
    rejections = [item for item in items if item["status"] == 429]
    assert rejections, "拒绝记录必须能在现有日志端点看到"
    assert "quota_exceeded" in (rejections[0]["error"] or "")


@pytest.mark.usage
def test_request_count_over_limit_returns_429(admin: httpx.Client, anon: httpx.Client, db_path: str):
    key_id, token = _new_key(admin)
    _policy(admin, key_id, limit_requests=1)

    before = _ledger_count(db_path)
    assert _chat(anon, token).status_code == 200
    _wait_ledger_count(db_path, before + 1)

    rejected = _chat(anon, token)
    assert rejected.status_code == 429, f"{rejected.status_code} {rejected.text}"
    detail = rejected.json()["detail"]
    assert detail["metric"] == "requests"
    assert detail["used"] == 1
    assert detail["limit"] == 1
    assert detail["unit"] == "requests"
    assert "请求数" in detail["message"]
    assert _ledger_count(db_path) == before + 1


@pytest.mark.usage
def test_under_limit_allowed_and_price_snapshot_kept(
    admin: httpx.Client, anon: httpx.Client, db_path: str
):
    key_id, token = _new_key(admin)
    _policy(admin, key_id, limit_micro=1000, limit_requests=10)

    for _ in range(2):
        r = _chat(anon, token)
        assert r.status_code == 200, f"{r.status_code} {r.text}"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT billed_micro, price_snapshot_id FROM billing_ledger WHERE key_id = ?", (key_id,)
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    assert all(int(row["billed_micro"]) == PER_REQUEST_MICRO for row in rows)
    assert all(row["price_snapshot_id"] is not None for row in rows), "额度校验不能替代价格冻结"


def test_key_ref_numeric_query_and_secret_header_agree(admin: httpx.Client):
    """数字 query 行为不变；完整密钥只从请求头/body 进，解析到同一条记录。"""
    key_id, token = _new_key(admin)
    policy = _policy(admin, key_id, limit_micro=1000, limit_requests=10)

    by_id = admin.get("/api/quota/policies", params={"key_id": key_id})
    by_header = admin.get("/api/quota/policies", headers={"X-Api-Key": token})
    assert by_id.status_code == by_header.status_code == 200
    assert [item["id"] for item in by_id.json()] == [policy["id"]]
    assert [item["id"] for item in by_header.json()] == [policy["id"]]
    assert by_header.json()[0]["key_id"] == key_id, "出参主体仍是数字 key_id"
    assert by_header.json()[0]["key_name"]

    status_id = admin.get("/api/quota/status", params={"key_id": key_id})
    status_header = admin.get("/api/quota/status", headers={"X-Api-Key": token})
    assert status_id.status_code == status_header.status_code == 200
    assert [item["policy"]["id"] for item in status_id.json()["items"]] == [policy["id"]]
    assert [item["policy"]["id"] for item in status_header.json()["items"]] == [policy["id"]]

    created = admin.post(
        "/api/quota/policies",
        json={"key_id": token, "window": "month", "limit_micro": 500},
    )
    assert created.status_code == 200, f"{created.status_code} {created.text}"
    assert created.json()["key_id"] == key_id

    alerts = admin.get("/api/quota/alerts", headers={"X-Api-Key": token})
    assert alerts.status_code == 200
    assert alerts.json()["total"] == 0, "新密钥没有告警，但过滤条件必须被解析而不是报错"

    both = admin.get(
        "/api/quota/policies", params={"key_id": key_id}, headers={"X-Api-Key": token}
    )
    assert both.status_code == 400, "数字 id 与密钥头不能同时使用"


def test_query_rejects_full_key_and_does_not_echo_it(admin: httpx.Client):
    _, token = _new_key(admin)

    for path in ("/api/quota/policies", "/api/quota/status", "/api/quota/alerts"):
        rejected = admin.get(path, params={"key_id": token})
        assert rejected.status_code == 400, rejected.text
        assert token not in rejected.text, "400 文案不能回显完整密钥"
        detail = rejected.json()["detail"]
        assert "密钥不要放在 URL" in detail and "X-Api-Key" in detail, detail


def test_key_ref_not_found_and_malformed(admin: httpx.Client):
    malformed = admin.get("/api/quota/policies", params={"key_id": "abc"})
    assert malformed.status_code == 400, malformed.text

    missing_numeric = admin.get("/api/quota/policies", params={"key_id": 999_999})
    assert missing_numeric.status_code == 404, missing_numeric.text

    missing_header = admin.get(
        "/api/quota/policies", headers={"X-Api-Key": "u2a_deadbeefdeadbeef"}
    )
    assert missing_header.status_code == 404, missing_header.text

    bad_header = admin.get("/api/quota/status", headers={"X-Api-Key": "not-a-key"})
    assert bad_header.status_code == 400, bad_header.text

    bad_post = admin.post(
        "/api/quota/policies",
        json={"key_id": "abc", "window": "day", "limit_micro": 100},
    )
    assert bad_post.status_code == 400, bad_post.text

    absent_post = admin.post(
        "/api/quota/policies",
        json={"key_id": 999_999, "window": "day", "limit_micro": 100},
    )
    assert absent_post.status_code == 404, absent_post.text


def test_policy_and_alert_do_not_leak_full_key(admin: httpx.Client):
    key_id, token = _new_key(admin)
    policy = _policy(admin, key_id, limit_micro=100)

    listed = admin.get("/api/quota/policies", headers={"X-Api-Key": token})
    assert listed.status_code == 200
    item = listed.json()[0]
    assert item["key_prefix"].startswith("u2a_"), item
    assert item["key_prefix"].endswith("****"), item
    assert token not in listed.text, "出参不能包含完整密钥"
    assert token not in item["key_prefix"]

    alerts = admin.get("/api/quota/alerts", params={"policy_id": policy["id"]})
    assert alerts.status_code == 200
    assert token not in alerts.text
    assert set(alerts.json()) == {"total", "items"}


def test_mask_text_masks_embedded_u2a_keys():
    token = "u2a_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    masked = mask_text(f"upstream error key={token}&retry=1")
    assert token not in masked
    assert "u2a_…" in masked, "保留前缀与末几位"


def test_mask_access_args_covers_query_string():
    token = "u2a_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    args = ("127.0.0.1", "GET", f"/api/quota/policies?key_id={token}", "1.1", 200)
    masked = mask_access_args(args)
    assert token not in " ".join(str(part) for part in masked)
    assert masked[2].startswith("/api/quota/policies?key_id=u2a_…")


@pytest.mark.usage
def test_request_logs_mask_key_material(admin: httpx.Client, anon: httpx.Client, db_path: str):
    key_id, token = _new_key(admin)
    patched = admin.patch(f"/api/keys/{key_id}", json={"name": token})
    assert patched.status_code == 200, patched.text
    _policy(admin, key_id, limit_requests=1)

    before = _ledger_count(db_path)
    assert _chat(anon, token).status_code == 200
    _wait_ledger_count(db_path, before + 1)
    assert _chat(anon, token).status_code == 429

    logs = admin.get("/api/logs", params={"key_id": key_id})
    assert logs.status_code == 200, logs.text
    assert token not in logs.text, "request_logs 新写入不能出现完整密钥"
    names = {item["key_name"] for item in logs.json()["items"]}
    assert mask_secret(token) in names, "密钥名称字段应在落库前完成掩码"


def _login_viewer(admin: httpx.Client) -> tuple[str, httpx.Client]:
    name = f"quota-viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users",
        json={"username": name, "password": "viewer-pass", "role": "viewer"},
    )
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return name, client


def test_create_key_defaults_owner_to_current_user(admin: httpx.Client):
    """建 key 不传归属就归当前登录用户；显式指定他人时按用户名落库。"""
    mine = admin.post("/api/keys", json={"name": f"own-{uuid.uuid4().hex[:8]}"})
    assert mine.status_code == 200, mine.text
    assert mine.json()["owner_user_id"] == "admin"

    name, viewer = _login_viewer(admin)
    try:
        for_other = admin.post(
            "/api/keys", json={"name": f"for-{uuid.uuid4().hex[:8]}", "owner_user_id": name}
        )
        assert for_other.status_code == 200, for_other.text
        assert for_other.json()["owner_user_id"] == name

        missing = admin.post(
            "/api/keys", json={"name": "ghost", "owner_user_id": "no-such-user"}
        )
        assert missing.status_code == 400, missing.text
        assert "归属用户不存在" in missing.json()["detail"]
    finally:
        viewer.close()


def test_viewer_only_sees_own_keys_in_list(admin: httpx.Client):
    """非 admin 的 keys 列表只含自有 key。"""
    name, viewer = _login_viewer(admin)
    own_id, _ = _new_key(admin, owner=name)
    other_id, _ = _new_key(admin)  # 归 admin
    try:
        listed = viewer.get("/api/keys")
        assert listed.status_code == 200, listed.text
        ids = [item["id"] for item in listed.json()]
        assert own_id in ids
        assert other_id not in ids
    finally:
        viewer.close()


def test_viewer_only_sees_own_quota(admin: httpx.Client, db_path: str):
    name, viewer = _login_viewer(admin)
    own_id, own_token = _new_key(admin, owner=name)
    own_policy = _policy(admin, own_id, limit_micro=100)
    other_id, other_token = _new_key(admin)  # 归 admin
    other_policy = _policy(admin, other_id, limit_micro=100)
    try:
        policies = viewer.get("/api/quota/policies")
        assert policies.status_code == 200, policies.text
        assert [item["id"] for item in policies.json()] == [own_policy["id"]]

        status = viewer.get("/api/quota/status")
        assert status.status_code == 200, status.text
        assert [item["policy"]["id"] for item in status.json()["items"]] == [own_policy["id"]]

        alerts = viewer.get("/api/quota/alerts")
        assert alerts.status_code == 200, alerts.text
        assert alerts.json()["total"] == 0, "别人的告警不能被计入"

        by_id = viewer.get("/api/quota/policies", params={"key_id": own_id})
        by_header = viewer.get("/api/quota/policies", headers={"X-Api-Key": own_token})
        assert by_id.status_code == by_header.status_code == 200
        assert [item["id"] for item in by_id.json()] == [own_policy["id"]]
        assert [item["id"] for item in by_header.json()] == [own_policy["id"]]
        assert own_token not in by_header.text, "响应不能回显完整密钥"

        for path in ("/api/quota/policies", "/api/quota/status", "/api/quota/alerts"):
            for kwargs in (
                {"params": {"key_id": other_id}},
                {"headers": {"X-Api-Key": other_token}},
            ):
                denied = viewer.get(path, **kwargs)
                assert denied.status_code == 403, f"{path} {kwargs}: {denied.text}"
                error = denied.json()["error"]
                assert error["type"] == "permission_denied"
                assert error["code"] == "insufficient_role"
                assert other_token not in denied.text, "403 文案不能回显密钥"

        # NULL 归属的老 key 对非 admin 不可见（admin 清空归属来模拟老库存量）
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE api_keys SET owner_user_id = NULL WHERE id = ?", (other_id,))
            conn.commit()
        finally:
            conn.close()
        after = viewer.get("/api/quota/policies")
        assert after.status_code == 200
        assert [item["id"] for item in after.json()] == [own_policy["id"]]
        hidden = viewer.get("/api/quota/policies", params={"key_id": other_id})
        assert hidden.status_code == 403, "NULL 归属对非 admin 也不能开口子"
    finally:
        viewer.close()

    # admin 行为不变：仍可查全部、可按 key 过滤
    assert admin.get("/api/quota/policies").json()
    assert [
        item["id"]
        for item in admin.get("/api/quota/policies", params={"key_id": other_id}).json()
    ] == [other_policy["id"]]


def _seed_failures(db_path: str, key_id: int, count: int, at: int | None = None) -> None:
    stamp = int(time.time()) if at is None else at
    conn = sqlite3.connect(db_path)
    try:
        for i in range(count):
            conn.execute(
                "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, completion_tokens, "
                "latency_ms, stream, error, key_id, key_name) VALUES(?,?,?,?,0,0,0,0,?,?,?)",
                (stamp, "codebuddy", "seed-fail", 500, f"seed-{i}", key_id, "seed"),
            )
        conn.commit()
    finally:
        conn.close()


def _usage_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0])
    finally:
        conn.close()


@pytest.mark.usage
def test_failure_window_triggers_429_without_billing(admin: httpx.Client, anon: httpx.Client, db_path: str):
    key_id, token = _new_key(admin)
    _seed_failures(db_path, key_id, 20)

    before_ledger = _ledger_count(db_path)
    before_usage = _usage_count(db_path)
    blocked = _chat(anon, token)
    assert blocked.status_code == 429, f"{blocked.status_code} {blocked.text}"
    detail = blocked.json()["detail"]
    assert detail["type"] == "failure_rate_limited"
    assert detail["layer"] == "key"
    assert detail["metric"] == "failures"
    assert detail["unit"] == "requests"
    assert detail["used"] == 20
    assert detail["limit"] == 20
    assert detail["window"] == "sliding"
    assert detail["reset_after"] >= 1
    assert int(blocked.headers["retry-after"]) == detail["reset_after"]
    assert "失败请求已达" in detail["message"]
    assert _ledger_count(db_path) == before_ledger, "被限流请求不能写 billing_ledger"
    assert _usage_count(db_path) == before_usage, "被限流请求不能写 usage_records"

    logs = admin.get("/api/logs", params={"key_id": key_id})
    assert logs.status_code == 200, logs.text
    rejections = [item for item in logs.json()["items"] if item["status"] == 429]
    assert rejections, "限流拒绝要留 request_logs"
    assert "failure_rate_limited" in (rejections[0]["error"] or "")
    assert token not in logs.text


@pytest.mark.usage
def test_failure_window_expires_and_recovers(admin: httpx.Client, anon: httpx.Client, db_path: str):
    key_id, token = _new_key(admin)
    _seed_failures(db_path, key_id, 20)
    assert _chat(anon, token).status_code == 429

    # 把该 key 的失败记录（含刚才的 429 拒绝）整体推到窗口外，等价于 60s 过去
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE request_logs SET ts = ts - 120 WHERE key_id = ?", (key_id,))
        conn.commit()
    finally:
        conn.close()

    before = _ledger_count(db_path)
    ok = _chat(anon, token)
    assert ok.status_code == 200, f"窗口过期后必须自动恢复：{ok.status_code} {ok.text}"
    _wait_ledger_count(db_path, before + 1)


def _seed_alerts(db_path: str, policy_id: int, count: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for i in range(count):
            conn.execute(
                "INSERT INTO quota_alerts(policy_id, threshold_percent, window, period_start, "
                "triggered_at, metric, used_micro, limit_micro, used_requests, limit_requests) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (policy_id, i + 1, "day", 10_000 + i, 10_000 + i, "micro", 10, 100, 0, 0),
            )
        conn.commit()
    finally:
        conn.close()


def test_alerts_pagination_and_true_total(admin: httpx.Client, db_path: str):
    key_id, token = _new_key(admin)
    policy = _policy(admin, key_id, limit_micro=100)
    _seed_alerts(db_path, int(policy["id"]), 7)

    params = {"policy_id": policy["id"]}
    default = admin.get("/api/quota/alerts", params=params)
    assert default.status_code == 200
    body = default.json()
    assert set(body) == {"total", "items"}, "不传分页参数时响应形状保持不变"
    assert body["total"] == 7, "total 必须是真实总数，不是截断后的条数"
    assert len(body["items"]) == 7

    first = admin.get("/api/quota/alerts", params={**params, "limit": 3, "offset": 0}).json()
    second = admin.get("/api/quota/alerts", params={**params, "limit": 3, "offset": 3}).json()
    third = admin.get("/api/quota/alerts", params={**params, "limit": 3, "offset": 6}).json()
    beyond = admin.get("/api/quota/alerts", params={**params, "limit": 3, "offset": 99}).json()

    assert [first["total"], second["total"], third["total"], beyond["total"]] == [7, 7, 7, 7]
    assert [len(page["items"]) for page in (first, second, third, beyond)] == [3, 3, 1, 0]
    ids = [item["id"] for page in (first, second, third) for item in page["items"]]
    assert len(ids) == len(set(ids)) == 7, "offset/limit 分页不能重复或漏项"

    by_token = admin.get(
        "/api/quota/alerts", params={**params, "limit": 2}, headers={"X-Api-Key": token}
    ).json()
    by_numeric = admin.get(
        "/api/quota/alerts", params={"key_id": key_id, **params, "limit": 2}
    ).json()
    assert by_token["total"] == by_numeric["total"] == 7
    assert [item["id"] for item in by_token["items"]] == [
        item["id"] for item in by_numeric["items"]
    ]


@pytest.mark.usage
def test_threshold_alerts_once_and_readable_from_status(
    admin: httpx.Client, anon: httpx.Client, db_path: str
):
    key_id, token = _new_key(admin)
    policy = _policy(admin, key_id, limit_micro=100, thresholds=[50])

    before = _ledger_count(db_path)
    assert _chat(anon, token).status_code == 200
    _wait_ledger_count(db_path, before + 1)
    # 78 >= 50% 阈值，第一条告警落库
    first = admin.get("/api/quota/alerts", params={"policy_id": policy["id"]})
    assert first.status_code == 200
    assert len(first.json()["items"]) == 1
    assert first.json()["items"][0]["threshold_percent"] == 50

    # 第二次仍在上限内（78 < 100），用量到 156；阈值告警不能重复刷
    assert _chat(anon, token).status_code == 200
    _wait_ledger_count(db_path, before + 2)
    again = admin.get("/api/quota/alerts", params={"policy_id": policy["id"]})
    assert len(again.json()["items"]) == 1, "同一周期同一阈值只报一次"

    status = admin.get("/api/quota/status", params={"key_id": key_id})
    assert status.status_code == 200, f"{status.status_code} {status.text}"
    items = status.json()["items"]
    assert len(items) == 1
    assert items[0]["used_micro"] == 2 * PER_REQUEST_MICRO
    assert items[0]["policy"]["limit_micro"] == 100
    assert items[0]["reset_at"] > status.json()["at"]
    assert len(items[0]["alerts"]) == 1
    assert items[0]["exceeded"] is True
