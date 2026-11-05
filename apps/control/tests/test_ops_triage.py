"""运维排障面（2026-11-05）：告警规则配置、告警状态机、失败请求排查标记。

三块都在真实 HTTP 上跑：规则配置走 /api/settings/runtime-config 的整组事务，
告警动作用 admin/viewer 两个身份交叉验证，日志标记前后直接对比 billing_ledger。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import uuid
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[3]
VIEWER_PASSWORD = "triage-viewer-pass"

ALERT_DEFAULTS = {
    "alert_provider_failure_enabled": True,
    "alert_provider_failure_streak": 3,
    "alert_provider_failure_window_sec": 86400,
    "alert_quota_remaining_enabled": True,
    "alert_quota_remaining_percent": 20,
    "alert_usage_missing_enabled": True,
    "alert_usage_missing_ratio_percent": 50,
    "alert_usage_missing_min_requests": 20,
    "alert_usage_missing_window_sec": 600,
}


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _save_rules(admin: httpx.Client, **patch) -> httpx.Response:
    return admin.post("/api/settings/runtime-config", json={"items": patch})


def _restore_rules(admin: httpx.Client) -> None:
    _save_rules(admin, **ALERT_DEFAULTS)


def _login(base_url: str, username: str, password: str) -> httpx.Client:
    client = httpx.Client(base_url=base_url, trust_env=False, timeout=20.0)
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture(scope="module")
def viewer(base_url: str, admin: httpx.Client):
    tag = uuid.uuid4().hex[:6]
    name = f"triage-view-{tag}"
    created = admin.post(
        "/api/users", json={"username": name, "password": VIEWER_PASSWORD, "role": "viewer"}
    )
    assert created.status_code == 200, created.text
    client = _login(base_url, name, VIEWER_PASSWORD)
    yield client
    client.close()


def _insert_alert(conn: sqlite3.Connection, subject: str, threshold: int, period: int) -> int:
    cur = conn.execute(
        "INSERT INTO quota_alerts(policy_id, threshold_percent, window, period_start, "
        "triggered_at, metric, used_requests, limit_requests, kind, subject) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (-900001, threshold, "streak", period, period, "requests", threshold, threshold,
         "provider_failure", subject),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_log(conn: sqlite3.Connection, provider: str, status: int, error: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, completion_tokens, "
        "latency_ms, stream, error) VALUES(?,?,?,?,?,?,?,?,?)",
        (period_now(), provider, "triage-model", status, 10, 5, 120, 0, error),
    )
    conn.commit()
    return int(cur.lastrowid)


def period_now() -> int:
    import time

    return int(time.time())


def _alert_item(admin: httpx.Client, alert_id: int) -> dict:
    rows = admin.get("/api/quota/alerts", params={"limit": 1000}).json()["items"]
    for item in rows:
        if int(item["id"]) == alert_id:
            return item
    raise AssertionError(f"alert {alert_id} not listed")


def test_alert_rule_items_are_clamped(admin: httpx.Client) -> None:
    payload = admin.get("/api/settings/runtime-config").json()
    by_key = {item["key"]: item for item in payload["items"]}
    assert by_key["alert_provider_failure_streak"]["min"] == 1
    assert by_key["alert_provider_failure_streak"]["max"] == 100
    assert by_key["alert_provider_failure_window_sec"]["min"] == 60
    assert by_key["alert_provider_failure_window_sec"]["max"] == 86400
    assert by_key["alert_usage_missing_min_requests"]["min"] == 10


@pytest.mark.parametrize(
    "patch,key,reason",
    [
        ({"alert_provider_failure_streak": 0}, "alert_provider_failure_streak", "out_of_range"),
        ({"alert_provider_failure_streak": 101}, "alert_provider_failure_streak", "out_of_range"),
        ({"alert_provider_failure_window_sec": 59}, "alert_provider_failure_window_sec", "out_of_range"),
        ({"alert_usage_missing_min_requests": 1}, "alert_usage_missing_min_requests", "out_of_range"),
        ({"alert_usage_missing_min_requests": 9}, "alert_usage_missing_min_requests", "out_of_range"),
        ({"alert_quota_remaining_percent": 100}, "alert_quota_remaining_percent", "out_of_range"),
    ],
)
def test_alert_rule_invalid_values_rejected(
    admin: httpx.Client, patch: dict, key: str, reason: str
) -> None:
    r = _save_rules(admin, **patch)
    assert r.status_code == 400, r.text
    error = r.json()["error"]
    assert error["errors"][0]["key"] == key
    assert error["errors"][0]["reason"] == reason
    # 拒绝时必须给出明确区间，而不是静默回落默认值
    assert error["errors"][0]["min"] is not None
    assert error["errors"][0]["max"] is not None


def test_alert_rule_group_write_is_atomic(admin: httpx.Client) -> None:
    before = admin.get("/api/settings/runtime-config").json()
    before_map = {item["key"]: item["value"] for item in before["items"]}
    try:
        # 一项非法 -> 同批其它合法项也不生效
        r = _save_rules(
            admin,
            alert_provider_failure_streak=7,
            alert_usage_missing_min_requests=1,
        )
        assert r.status_code == 400
        after = admin.get("/api/settings/runtime-config").json()
        after_map = {item["key"]: item["value"] for item in after["items"]}
        assert after_map["alert_provider_failure_streak"] == before_map["alert_provider_failure_streak"]

        ok = _save_rules(admin, **ALERT_DEFAULTS)
        assert ok.status_code == 200, ok.text
        saved = ok.json()
        assert sorted(saved["saved"]) == sorted(ALERT_DEFAULTS)
        assert saved["items"]
    finally:
        _restore_rules(admin)


def test_disabled_rule_is_not_evaluated(admin: httpx.Client, db_path: str) -> None:
    tag = f"off-{uuid.uuid4().hex[:6]}"
    conn = _conn(db_path)
    try:
        for _ in range(3):
            _insert_log(conn, tag, 502, "upstream boom")
        disabled = dict(ALERT_DEFAULTS, alert_provider_failure_enabled=False)
        _save_rules(admin, **disabled)
        admin.get("/api/metrics")
        rows = admin.get("/api/quota/alerts", params={"limit": 1000}).json()["items"]
        assert not [item for item in rows if item["provider"] == tag]

        _save_rules(admin, **ALERT_DEFAULTS)
        admin.get("/api/metrics")
        rows = admin.get("/api/quota/alerts", params={"limit": 1000}).json()["items"]
        assert [item for item in rows if item["provider"] == tag]
    finally:
        _restore_rules(admin)
        conn.execute("DELETE FROM request_logs WHERE provider = ?", (tag,))
        conn.execute("DELETE FROM quota_alerts WHERE subject = ?", (tag,))
        conn.execute("DELETE FROM audit_logs WHERE action LIKE 'settings.runtime_config' AND target LIKE '%alert_provider_failure%'")
        conn.commit()
        conn.close()


def test_alert_state_machine_idempotent_and_silence_expiry(
    admin: httpx.Client, viewer: httpx.Client, db_path: str
) -> None:
    conn = _conn(db_path)
    alert_id = _insert_alert(conn, "triage-state", 3, period_now())
    try:
        ack = admin.post(f"/api/quota/alerts/{alert_id}/ack")
        assert ack.status_code == 200, ack.text
        first_acked = ack.json()["item"]["acked_at"]
        assert ack.json()["item"]["status"] == "acked"

        again = admin.post(f"/api/quota/alerts/{alert_id}/ack")
        assert again.status_code == 200, again.text
        assert again.json()["item"]["acked_at"] == first_acked

        closed = admin.post(f"/api/quota/alerts/{alert_id}/close")
        assert closed.status_code == 200, closed.text
        first_closed = closed.json()["item"]["closed_at"]
        assert closed.json()["item"]["status"] == "closed"
        second_close = admin.post(f"/api/quota/alerts/{alert_id}/close")
        assert second_close.status_code == 200
        assert second_close.json()["item"]["closed_at"] == first_closed

        # closed 不能直接回 acked，必须先 reopen
        bad_ack = admin.post(f"/api/quota/alerts/{alert_id}/ack")
        assert bad_ack.status_code == 409

        reopened = admin.post(f"/api/quota/alerts/{alert_id}/reopen")
        assert reopened.status_code == 200
        assert reopened.json()["item"]["status"] == "open"
        assert admin.post(f"/api/quota/alerts/{alert_id}/reopen").status_code == 409

        silent = admin.post(f"/api/quota/alerts/{alert_id}/silence", json={"seconds": 3600})
        assert silent.status_code == 200, silent.text
        assert silent.json()["item"]["status"] == "silenced"
        until = int(silent.json()["item"]["silenced_until"])
        assert until > period_now() + 3000

        assert admin.post(f"/api/quota/alerts/{alert_id}/silence", json={"seconds": 0}).status_code == 400
        assert (
            admin.post(f"/api/quota/alerts/{alert_id}/silence", json={"seconds": 30 * 86400 + 1}).status_code
            == 400
        )
        assert admin.post(f"/api/quota/alerts/{alert_id}/silence", json={}).status_code == 422

        # 静默到期：读取端直接恢复为 open
        conn.execute("UPDATE quota_alerts SET silenced_until = ? WHERE id = ?", (period_now() - 1, alert_id))
        conn.commit()
        assert _alert_item(admin, alert_id)["status"] == "open"

        for action, body in (
            ("ack", None),
            ("silence", {"seconds": 3600}),
            ("close", None),
            ("reopen", None),
        ):
            url = f"/api/quota/alerts/{alert_id}/{action}"
            call = viewer.post(url, json=body) if body else viewer.post(url)
            assert call.status_code == 403, f"{action}: {call.text}"
            assert call.json()["error"]["required_role"] == "admin"

        audits = conn.execute(
            "SELECT actor, action, target, detail FROM audit_logs WHERE target = ?",
            (f"quota_alert:{alert_id}",),
        ).fetchall()
        assert [row["action"] for row in audits].count("alert.ack") == 2
        assert any(row["action"] == "alert.silence" for row in audits)
        assert any(row["action"] == "alert.close" for row in audits)
        assert any(row["action"] == "alert.reopen" for row in audits)
        assert all(row["actor"] == "admin" for row in audits)
        # 审计只留规则类型与状态变化，不抄上游错误正文
        assert all(row["detail"].startswith("kind=") for row in audits)
    finally:
        conn.execute("DELETE FROM quota_alerts WHERE id = ?", (alert_id,))
        conn.execute("DELETE FROM audit_logs WHERE target = ?", (f"quota_alert:{alert_id}",))
        conn.commit()
        conn.close()


def test_log_resolution_keeps_billing_untouched(
    admin: httpx.Client, viewer: httpx.Client, db_path: str
) -> None:
    tag = f"triage-{uuid.uuid4().hex[:6]}"
    conn = _conn(db_path)
    log_id = _insert_log(conn, tag, 502, "upstream boom sk-triageSecret123456")
    ok_id = _insert_log(conn, tag, 200, None)
    conn.execute(
        "INSERT INTO billing_ledger(ts, key_id, provider, model, status, input_tokens, output_tokens, "
        "billed_micro, note, log_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (period_now(), None, tag, "triage-model", 200, 10, 5, 42, "", ok_id),
    )
    conn.commit()

    def ledger_cut() -> tuple[int, int]:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(billed_micro), 0) AS total FROM billing_ledger"
        ).fetchone()
        return int(row["n"]), int(row["total"])

    before = ledger_cut()
    try:
        resolve = admin.post(f"/api/logs/{log_id}/resolve")
        assert resolve.status_code == 200, resolve.text
        assert resolve.json()["resolved"] is True
        stamp = resolve.json()["resolved_at"]
        assert stamp > 0
        assert resolve.json()["resolved_by"] == "admin"
        assert ledger_cut() == before

        again = admin.post(f"/api/logs/{log_id}/resolve")
        assert again.status_code == 200
        assert again.json()["resolved_at"] == stamp
        assert ledger_cut() == before

        logs = admin.get("/api/logs", params={"provider": tag, "limit": 50}).json()["items"]
        row = [item for item in logs if int(item["id"]) == log_id][0]
        assert row["resolved"] is True
        assert row["resolved_by"] == "admin"
        assert row["credit_status"] == "not_billed"

        unmark = admin.delete(f"/api/logs/{log_id}/resolve")
        assert unmark.status_code == 200
        assert unmark.json()["resolved"] is False
        assert admin.delete(f"/api/logs/{log_id}/resolve").status_code == 200
        assert ledger_cut() == before

        # 成功且无错误的请求不允许标记
        assert admin.post(f"/api/logs/{ok_id}/resolve").status_code == 400
        assert viewer.post(f"/api/logs/{log_id}/resolve").status_code == 403
        assert viewer.delete(f"/api/logs/{log_id}/resolve").status_code == 403

        audits = conn.execute(
            "SELECT action, detail FROM audit_logs WHERE target = ? ORDER BY id",
            (f"request_log:{log_id}",),
        ).fetchall()
        assert [row["action"] for row in audits] == [
            "logs.resolve",
            "logs.resolve",
            "logs.unresolve",
            "logs.unresolve",
        ]
        assert "upstream boom" not in " ".join(row["detail"] for row in audits)
        assert audits[0]["detail"] == "status=502"

        csv = admin.get("/api/logs/export", params={"provider": tag})
        assert csv.status_code == 200
        header = csv.text.splitlines()[0]
        assert "resolved_at" in header and "resolved_by" in header
        body = csv.text
        assert "sk-triageSecret123456" not in body
    finally:
        conn.execute("DELETE FROM billing_ledger WHERE provider = ?", (tag,))
        conn.execute("DELETE FROM request_logs WHERE provider = ?", (tag,))
        conn.execute("DELETE FROM audit_logs WHERE target LIKE ?", (f"request_log:{log_id}",))
        conn.execute("DELETE FROM audit_logs WHERE target LIKE ?", (f"request_log:{ok_id}",))
        conn.commit()
        conn.close()


def test_csv_formula_guard_still_applies(admin: httpx.Client, db_path: str) -> None:
    tag = f"formula-{uuid.uuid4().hex[:6]}"
    conn = _conn(db_path)
    cur = conn.execute(
        "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, completion_tokens, "
        "latency_ms, stream, error) VALUES(?,?,?,?,?,?,?,?,?)",
        (period_now(), tag, "=1+1", 500, 0, 0, 10, 0, "boom"),
    )
    conn.commit()
    try:
        csv = admin.get("/api/logs/export", params={"provider": tag})
        assert csv.status_code == 200
        assert "'=1+1" in csv.text
    finally:
        conn.execute("DELETE FROM request_logs WHERE provider = ?", (tag,))
        conn.commit()
        conn.close()


def _load_core():
    path = REPO / "scripts" / "backup_core.py"
    spec = importlib.util.spec_from_file_location("backup_core_triage", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_selfcheck_passes_on_new_schema(db_path: str, tmp_path: Path) -> None:
    core = _load_core()
    snap = tmp_path / "triage-snapshot.db"
    core.snapshot(Path(db_path), snap)
    conn = core.open_db(snap, readonly=True)
    try:
        manifest = {"tables": core.table_counts(conn), "demo": {}}
    finally:
        conn.close()
    result = core.selfcheck(snap, manifest)
    assert result["ok"] is True, result["checks"]
