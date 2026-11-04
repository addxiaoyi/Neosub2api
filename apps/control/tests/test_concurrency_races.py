"""并发与竞态回归：配额阈值告警唯一性 / 失败限流滑动窗口边界 / KeyPool 冷却。

单元用例直连临时 SQLite（不起进程），用线程与 asyncio 任务制造真实竞争；
集成用例走 conftest 起的控制面与假上游（端口取自 UNIFIED_TEST_CTRL_PORT /
UNIFIED_TEST_FAST_PORT，约定落在 8380-8390）。httpx 一律 trust_env=False。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from app import billing
from app.adapters import gptgrok
from app.adapters.base import ProviderError
from app.adapters.key_pool import COOLDOWN_STATUSES, KeyPool
from app.billing import check_failure_rate, evaluate_alerts, period_bounds, settle
from app.db import Database
from tests.test_vendor_adapters import USERS, _settings as adapter_settings

TZ8 = dt.timezone(dt.timedelta(hours=8))
GATEWAY_MODEL = "race-m"
CONCURRENT_BURST = 10
FAILURE_BURST = 6
WINDOW_SEC = 60
WINDOW_MAX = 20


def _tz(*parts: int) -> int:
    return int(dt.datetime(*parts, tzinfo=TZ8).timestamp())


def _seed_bill(db: Database, key_id: int, ts: int) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO billing_ledger(ts, key_id, key_name, provider, model, status, billed_micro) "
            "VALUES(?,?,?,?,?,?,?)",
            (ts, key_id, "race", "codebuddy", GATEWAY_MODEL, 200, 78),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_failure(db: Database, key_id: int, ts: int, status: int = 500, error: str | None = "boom") -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, completion_tokens, latency_ms, stream, error, key_id, key_name) "
            "VALUES(?,?,?,?,0,0,0,0,?,?,?)",
            (ts, "codebuddy", GATEWAY_MODEL, status, error, key_id, "race"),
        )
        conn.commit()
    finally:
        conn.close()


def _raw_count(db: Database, sql: str, params: tuple) -> int:
    conn = db.connect()
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def _alert_rows(db: Database, policy_id: int) -> list[sqlite3.Row]:
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM quota_alerts WHERE policy_id = ? ORDER BY id", (policy_id,)
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def race_db(tmp_path):
    """一把 key + 一条按次数的 10% 阈值策略：落一条流水即跨过阈值。"""
    db = Database(tmp_path / "race.db")
    conn = db.connect()
    try:
        key_id = int(
            conn.execute(
                "INSERT INTO api_keys(name, key_hash, prefix, enabled, created_at) VALUES('race','race-hash','u2a_',1,0)"
            ).lastrowid
        )
        policy_id = int(
            conn.execute(
                "INSERT INTO quota_policies(key_id, provider, model, window, limit_micro, limit_requests, enabled, created_at, updated_at) "
                "VALUES(?, '', '', 'day', 0, 10, 1, 0, 0)",
                (key_id,),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO quota_alert_thresholds(policy_id, percent, created_at) VALUES(?, 10, 0)",
            (policy_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return SimpleNamespace(db=db, key_id=key_id, policy_id=policy_id)


# ---------------------------------------------------------------------------
# 1. 配额阈值告警唯一性
# ---------------------------------------------------------------------------


def test_alert_race_hits_unique_index_not_insert(race_db, caplog):
    """两个连接都在「查无告警」后插同一 (policy, threshold, period)：后到者撞 UNIQUE。"""
    caplog.set_level(logging.ERROR, logger="unified.billing")
    db, key_id, policy_id = race_db.db, race_db.key_id, race_db.policy_id
    at = _tz(2026, 11, 4, 10, 0)
    _seed_bill(db, key_id, at - 60)

    winner = db.connect()
    created = evaluate_alerts(
        winner, key_id=key_id, provider="codebuddy", model=GATEWAY_MODEL, at=at
    )
    assert len(created) == 1  # 未提交，写锁攥在赢家手里，告警对其他连接不可见

    loser_created: list[list] = []
    loser_error: list[BaseException] = []
    reaching_insert = threading.Event()

    def _race() -> None:
        conn = db.connect()
        try:
            reaching_insert.set()
            loser_created.append(
                evaluate_alerts(
                    conn, key_id=key_id, provider="codebuddy", model=GATEWAY_MODEL, at=at
                )
            )
        except BaseException as exc:  # noqa: BLE001 -要抓的就是冲突本身
            loser_error.append(exc)
        finally:
            conn.close()

    thread = threading.Thread(target=_race, name="alert-loser")
    thread.start()
    assert reaching_insert.wait(2)
    time.sleep(0.3)  # 让输家走到 INSERT 并卡在写锁上
    assert thread.is_alive(), "输家应阻塞在写锁上，说明竞争窗口确实被打开"
    winner.commit()
    winner.close()
    thread.join(timeout=10)
    assert not thread.is_alive(), "赢家提交后输家必须立刻解除阻塞"

    # 逐阈值隔离后，UNIQUE 冲突被该条阈值吞掉，只留日志，不得外抛
    assert not loser_error, f"冲突不得出栈：{loser_error}"
    assert loser_created == [[]], f"输家本轮不写告警，实际：{loser_created}"
    conflicts = [
        record
        for record in caplog.records
        if "配额告警阈值评估失败" in record.getMessage()
    ]
    assert conflicts, "冲突必须落 ERROR 日志，不能只吞不报"
    assert isinstance(conflicts[0].exc_info[1], sqlite3.IntegrityError)
    assert "quota_alerts" in str(conflicts[0].exc_info[1])
    assert len(_alert_rows(db, policy_id)) == 1  # 冲突被拒，库里仍是唯一一条


def test_settle_swallows_alert_integrity_error(race_db, monkeypatch):
    """settle 内告警插入撞 UNIQUE 时异常被吞：流水照写、错误不出栈（网关侧不会变 500）。"""
    db, key_id, policy_id = race_db.db, race_db.key_id, race_db.policy_id
    at = _tz(2026, 11, 4, 10, 0)
    _seed_bill(db, key_id, at - 60)
    seen: list[sqlite3.IntegrityError] = []

    def _conflict(*args, **kwargs) -> None:
        err = sqlite3.IntegrityError(
            "UNIQUE constraint failed: quota_alerts.policy_id, quota_alerts.threshold_percent, quota_alerts.period_start"
        )
        seen.append(err)
        raise err

    monkeypatch.setattr(billing, "evaluate_alerts", _conflict)
    conn = db.connect()
    try:
        result = settle(
            conn,
            ts=at,
            key_id=key_id,
            key_name="race",
            provider="codebuddy",
            model=GATEWAY_MODEL,
            status=200,
            usage={"prompt_tokens": 11, "completion_tokens": 7},
        )
        conn.commit()
    finally:
        conn.close()

    assert seen, "告警评估必须真的被执行到，否则本用例是空转"
    assert result["written"] is True  # 异常没往外抛，否则这里根本拿不到返回值
    assert _raw_count(db, "SELECT COUNT(*) FROM billing_ledger WHERE key_id = ?", (key_id,)) == 2


def test_cross_period_concurrent_alerts_stay_independent(race_db):
    """并发评估两个不同 period_start：各发一条，互不冲突也不合并。"""
    db, key_id, policy_id = race_db.db, race_db.key_id, race_db.policy_id
    day1 = _tz(2026, 11, 4, 9, 0)
    day2 = _tz(2026, 11, 5, 9, 0)
    _seed_bill(db, key_id, day1)
    _seed_bill(db, key_id, day2)

    gate = threading.Barrier(2, timeout=5)
    created: dict[str, list] = {}
    errors: dict[str, str] = {}

    def _eval(tag: str, at: int) -> None:
        conn = db.connect()
        try:
            gate.wait()
            created[tag] = evaluate_alerts(
                conn, key_id=key_id, provider="codebuddy", model=GATEWAY_MODEL, at=at
            )
            conn.commit()
        except BaseException as exc:  # noqa: BLE001 -线程里异常要捞回主线程
            errors[tag] = f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()

    workers = [
        threading.Thread(target=_eval, args=("d1", day1)),
        threading.Thread(target=_eval, args=("d2", day2)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert not worker.is_alive()

    assert not errors, errors
    assert len(created["d1"]) == 1 and len(created["d2"]) == 1, created
    rows = _alert_rows(db, policy_id)
    assert len(rows) == 2, [dict(row) for row in rows]
    assert {row["period_start"] for row in rows} == {
        period_bounds("day", day1)[0],
        period_bounds("day", day2)[0],
    }


@pytest.mark.usage
def test_concurrent_gateway_requests_crossing_threshold_write_single_alert(
    admin, base_url, db_path
):
    """10 个请求同时跨过同一阈值：告警只落一条，且全程没有 5xx。"""
    created = admin.post("/api/keys", json={"name": f"race-alert-{uuid.uuid4().hex[:8]}"})
    assert created.status_code == 200, created.text
    key = created.json()
    try:
        policy_resp = admin.post(
            "/api/quota/policies",
            json={"key_id": key["id"], "window": "day", "limit_requests": 20, "thresholds": [10]},
        )
        assert policy_resp.status_code == 200, policy_resp.text
        policy_id = int(policy_resp.json()["id"])

        # 跨阈值点 = used*100 >= limit_requests*percent → 用到第 2 条流水
        gate = threading.Barrier(CONCURRENT_BURST, timeout=10)

        def _hit(_: int) -> httpx.Response:
            gate.wait()
            with httpx.Client(base_url=base_url, trust_env=False, timeout=30.0) as client:
                return client.post(
                    "/v1/chat/completions",
                    headers={
                        "authorization": f"Bearer {key['key']}",
                        "x-provider": "codebuddy",
                        "content-type": "application/json",
                    },
                    json={
                        "model": GATEWAY_MODEL,
                        "messages": [{"role": "user", "content": "race"}],
                    },
                )

        with ThreadPoolExecutor(max_workers=CONCURRENT_BURST) as pool:
            responses = list(pool.map(_hit, range(CONCURRENT_BURST)))

        codes = [response.status_code for response in responses]
        assert all(code < 500 for code in codes), f"并发下不允许出现 5xx：{codes}"
        assert all(code == 200 for code in codes), codes

        rows = _wait_alert_rows(db_path, policy_id, expect=1)
        assert rows[0]["threshold_percent"] == 10, dict(rows[0])
        assert rows[0]["period_start"] == period_bounds("day", int(time.time()))[0]
        assert rows[0]["used_requests"] >= 2, dict(rows[0])
    finally:
        admin.delete(f"/api/keys/{key['id']}")


def _wait_alert_rows(db_path, policy_id: int, expect: int, timeout: float = 5.0):
    """流水是旁路落库的：先等到告警出现，再断言数量恰好为 1。"""
    deadline = time.monotonic() + timeout
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        while True:
            rows = conn.execute(
                "SELECT * FROM quota_alerts WHERE policy_id = ? ORDER BY id", (policy_id,)
            ).fetchall()
            if len(rows) >= expect or time.monotonic() > deadline:
                return rows
            time.sleep(0.05)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. 失败限流滑动窗口边界
# ---------------------------------------------------------------------------


def test_window_left_edge_failure_still_counts(race_db):
    """刚好压在窗口最左边界（at - window）的失败仍算窗内。"""
    db, key_id = race_db.db, race_db.key_id
    at = 1_000_000
    _seed_failure(db, key_id, at - WINDOW_SEC)

    conn = db.connect()
    try:
        detail = check_failure_rate(
            conn,
            key_id=key_id,
            window_sec=WINDOW_SEC,
            max_failures=1,
            at=at,
        )
    finally:
        conn.close()
    assert detail is not None
    assert detail["type"] == "failure_rate_limited"
    assert detail["used"] == 1 and detail["limit"] == 1
    assert detail["window"] == "sliding" and detail["window_sec"] == WINDOW_SEC


def test_failure_just_outside_window_dropped(race_db):
    """滑出窗外一格（at - window - 1）的失败不再计数。"""
    db, key_id = race_db.db, race_db.key_id
    at = 1_000_000
    _seed_failure(db, key_id, at - WINDOW_SEC - 1)

    conn = db.connect()
    try:
        detail = check_failure_rate(
            conn, key_id=key_id, window_sec=WINDOW_SEC, max_failures=1, at=at
        )
    finally:
        conn.close()
    assert detail is None


def test_window_just_full_denies_one_short_passes(race_db):
    """窗口刚好满触发限流；差一条则放行。"""
    db, key_id = race_db.db, race_db.key_id
    at = 1_000_000
    for offset in (5, 15, 30):
        _seed_failure(db, key_id, at - offset)

    conn = db.connect()
    try:
        full = check_failure_rate(
            conn, key_id=key_id, window_sec=WINDOW_SEC, max_failures=3, at=at
        )
        short = check_failure_rate(
            conn, key_id=key_id, window_sec=WINDOW_SEC, max_failures=4, at=at
        )
    finally:
        conn.close()
    assert full is not None and full["used"] == 3 and full["limit"] == 3
    assert full["type"] == "failure_rate_limited" and full["layer"] == "key"
    assert short is None


def test_empty_window_returns_no_denial(race_db):
    """空窗口（无失败、只有干净的成功请求）都不限流。"""
    db, key_id = race_db.db, race_db.key_id
    at = 1_000_000
    _seed_failure(db, key_id, at - 10, status=200, error=None)

    conn = db.connect()
    try:
        with_success_only = check_failure_rate(
            conn, key_id=key_id, window_sec=WINDOW_SEC, max_failures=1, at=at
        )
    finally:
        conn.close()
    assert with_success_only is None

    empty_db = Database(db.path.parent / "empty-window.db")
    empty_conn = empty_db.connect()
    try:
        empty = check_failure_rate(
            empty_conn, key_id=999_999, window_sec=WINDOW_SEC, max_failures=1, at=at
        )
    finally:
        empty_conn.close()
    assert empty is None


def test_reset_after_tracks_earliest_failure_in_window(race_db):
    """reset_after 从窗内最早一条失败算起，且不小于 1 秒。"""
    db, key_id = race_db.db, race_db.key_id
    at = 1_000_000
    for offset in (10, 40, 50):  # 最早一条在 at-50
        _seed_failure(db, key_id, at - offset)

    conn = db.connect()
    try:
        detail = check_failure_rate(
            conn, key_id=key_id, window_sec=WINDOW_SEC, max_failures=3, at=at
        )
    finally:
        conn.close()
    assert detail is not None
    assert detail["reset_after"] == 10, detail
    assert detail["reset_at"] == at + 10
    assert "10 秒后重试" in detail["message"]

    edge_db = Database(db.path.parent / "reset-edge.db")
    _seed_failure(edge_db, key_id, at - WINDOW_SEC)  # 恰好在窗内最左
    edge_conn = edge_db.connect()
    try:
        edge = check_failure_rate(
            edge_conn, key_id=key_id, window_sec=WINDOW_SEC, max_failures=1, at=at
        )
    finally:
        edge_conn.close()
    assert edge is not None and edge["reset_after"] == 1


def test_concurrent_failure_writes_counted_exactly_once(race_db):
    """8 线程各写 5 条失败：真实行数与 window_usage 计数都恰好 40（不重不漏）。"""
    db, key_id = race_db.db, race_db.key_id
    at = 1_000_000
    threads, per_thread = 8, 5
    total = threads * per_thread
    errors: list[str] = []

    def _write(_: int) -> None:
        conn = db.connect()
        try:
            for _ in range(per_thread):
                conn.execute(
                    "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, completion_tokens, latency_ms, stream, error, key_id, key_name) "
                    "VALUES(?,?,?,?,0,0,0,0,?,?,?)",
                    (at - 30, "codebuddy", GATEWAY_MODEL, 500, "boom", key_id, "race"),
                )
            conn.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(_write, range(threads)))
    assert not errors, errors

    raw = _raw_count(
        db, "SELECT COUNT(*) FROM request_logs WHERE key_id = ? AND ts >= ?", (key_id, at - WINDOW_SEC)
    )
    assert raw == total, raw

    conn = db.connect()
    try:
        detail = check_failure_rate(
            conn, key_id=key_id, window_sec=WINDOW_SEC, max_failures=total, at=at
        )
    finally:
        conn.close()
    assert detail is not None and detail["used"] == total, detail


def test_concurrent_probes_are_read_only_and_agree(race_db):
    """并发探测只读：计数一致、不把探测自己算进去。"""
    db, key_id = race_db.db, race_db.key_id
    at = 1_000_000
    seeded = 20
    for offset in range(seeded):
        _seed_failure(db, key_id, at - 1 - offset)

    results: list[dict | None] = []
    lock = threading.Lock()
    errors: list[str] = []

    def _probe(_: int) -> None:
        conn = db.connect()
        try:
            for _ in range(10):
                detail = check_failure_rate(
                    conn, key_id=key_id, window_sec=WINDOW_SEC, max_failures=WINDOW_MAX, at=at
                )
                with lock:
                    results.append(detail)
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_probe, range(8)))
    assert not errors, errors
    assert len(results) == 80
    assert all(detail is not None for detail in results), "已满窗，探测必须全部给出限流"
    assert {detail["used"] for detail in results} == {seeded}
    assert {detail["type"] for detail in results} == {"failure_rate_limited"}

    raw = _raw_count(
        db, "SELECT COUNT(*) FROM request_logs WHERE key_id = ? AND ts >= ?", (key_id, at - WINDOW_SEC)
    )
    assert raw == seeded, f"探测不应产生写入：{raw}"


@pytest.mark.usage
def test_concurrent_gateway_requests_all_get_failure_rate_limited(admin, base_url, db_path):
    """窗口已满时并发请求全部 429 failure_rate_limited，没有 500 也没有漏判。"""
    created = admin.post("/api/keys", json={"name": f"race-fail-{uuid.uuid4().hex[:8]}"})
    assert created.status_code == 200, created.text
    key = created.json()
    now = int(time.time())
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        for offset in range(WINDOW_MAX):
            conn.execute(
                "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, completion_tokens, latency_ms, stream, error, key_id, key_name) "
                "VALUES(?,?,?,?,0,0,0,0,?,?,?)",
                (now - 1 - offset, "codebuddy", GATEWAY_MODEL, 500, "boom", key["id"], key["name"]),
            )
        conn.commit()
    finally:
        conn.close()

    gate = threading.Barrier(FAILURE_BURST, timeout=10)

    def _hit(_: int) -> httpx.Response:
        gate.wait()
        with httpx.Client(base_url=base_url, trust_env=False, timeout=30.0) as client:
            return client.post(
                "/v1/chat/completions",
                headers={
                    "authorization": f"Bearer {key['key']}",
                    "x-provider": "codebuddy",
                    "content-type": "application/json",
                },
                json={"model": GATEWAY_MODEL, "messages": [{"role": "user", "content": "race"}]},
            )

    try:
        with ThreadPoolExecutor(max_workers=FAILURE_BURST) as pool:
            responses = list(pool.map(_hit, range(FAILURE_BURST)))

        codes = [response.status_code for response in responses]
        assert all(code < 500 for code in codes), f"并发下不允许出现 5xx：{codes}"
        assert all(code == 429 for code in codes), codes
        details = [response.json()["detail"] for response in responses]
        assert {detail["type"] for detail in details} == {"failure_rate_limited"}, details
        assert all(detail["limit"] == WINDOW_MAX for detail in details), details
        assert all(detail["used"] >= WINDOW_MAX for detail in details), details
        assert all(response.headers.get("retry-after") for response in responses)
    finally:
        admin.delete(f"/api/keys/{key['id']}")


# ---------------------------------------------------------------------------
# 3. KeyPool 冷却并发
# ---------------------------------------------------------------------------


async def test_concurrent_cool_records_single_entry():
    """同一把钥匙被 10 个任务同时打挂：冷却表只有一条、且只影响这一把。"""
    pool = KeyPool()
    await asyncio.gather(*[pool.cool("k1", 0.3) for _ in range(10)])

    assert list(pool._cooling) == ["k1"]
    assert await pool.available(["k1", "k2"]) == ["k2"]


async def test_cooled_key_skipped_until_expiry_then_joins_rotation():
    """冷却期内绝不再拿同一把；冷却结束后回到轮转池。"""
    pool = KeyPool()
    keys = ["k0", "k1", "k2"]
    assert await pool.pick(keys) == "k0"

    await pool.cool("k0", 0.15)
    during = [await pool.pick(keys) for _ in range(4)]
    assert "k0" not in during, during
    assert set(during) == {"k1", "k2"}, during  # 轮转继续走，不是只咬住一把
    assert await pool.available(keys) == ["k1", "k2"]

    await asyncio.sleep(0.2)
    after = [await pool.pick(keys) for _ in range(6)]
    assert set(after) == set(keys), after  # 冷却过期后三把都在轮转里
    assert await pool.available(keys) == keys


async def test_concurrent_failures_keep_pool_serving():
    """并发打挂两把钥匙：剩下两把始终可用，冷却到期后整池恢复，全程无死锁。"""
    pool = KeyPool()
    keys = ["k0", "k1", "k2", "k3"]
    survivors = ["k2", "k3"]

    async def _fail(idx: int) -> None:
        await pool.pick(keys)
        await pool.cool(keys[idx % 2], 0.25)

    async def _probe() -> list[str | None]:
        picked: list[str | None] = []
        for _ in range(20):
            picked.append(await pool.pick(keys))
            await asyncio.sleep(0)
        return picked

    tasks = [_fail(i) for i in range(8)] + [_probe(), _probe()]
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)

    for picked in results[-2:]:
        assert None not in picked, f"风暴期间幸存钥匙不能缺席：{picked}"
    assert set(pool._cooling) == {"k0", "k1"}, list(pool._cooling)
    assert await pool.available(keys) == survivors

    await asyncio.sleep(0.3)
    assert await pool.available(keys) == keys, "冷却到期后整池必须恢复"
    assert await pool.pick(keys) in keys


async def test_adapter_concurrent_401_cools_bad_key_once(tmp_path, fake_base):
    """6 个任务并发撞同一把坏钥匙（401）：只记一次冷却，好钥匙照常服务。"""
    adapter = gptgrok.build(adapter_settings(tmp_path), fake_base)
    adapter.configure(
        api_key="bad-key",
        options={"extra_keys": ["tok-good"], "max_attempts": 2, "key_cooldown": 600},
    )
    keys = adapter._keys()

    replies = await asyncio.wait_for(
        asyncio.gather(
            *[
                adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=False)
                for _ in range(6)
            ]
        ),
        timeout=30,
    )

    bodies = [reply.body.decode("utf-8", "ignore") for reply in replies]
    assert all("authorization=Bearer tok-good" in body for body in bodies), bodies
    assert set(adapter._keys_pool._cooling) == {"bad-key"}, list(adapter._keys_pool._cooling)
    assert await adapter._keys_pool.available(keys) == ["tok-good"]


@pytest.mark.parametrize("status", sorted(COOLDOWN_STATUSES))
async def test_adapter_concurrent_status_errors_do_not_freeze_pool(
    status, tmp_path, fake_base
):
    """单钥匙池并发吃到 401/403/429：不给最后一把钥匙上冷却，池子不被锁死。"""
    adapter = gptgrok.build(adapter_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-only", options={"max_attempts": 1, "key_cooldown": 600})
    keys = adapter._keys()

    results = await asyncio.wait_for(
        asyncio.gather(
            *[
                adapter.chat_completion(
                    {"model": f"gpt-5-err-{status}", "messages": USERS}, stream=False
                )
                for _ in range(4)
            ],
            return_exceptions=True,
        ),
        timeout=30,
    )

    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(failures) == 4, results
    assert all(isinstance(item, ProviderError) for item in failures), failures
    assert {item.status_code for item in failures} <= {status, 429}, failures
    # 冷却表为空 = 最后一把钥匙没被冻上，否则并发失败会把整池锁死
    assert list(adapter._keys_pool._cooling) == []
    assert await adapter._keys_pool.available(keys) == keys

    reply = await asyncio.wait_for(
        adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=False), timeout=30
    )
    assert reply.status_code == 200, reply.body
