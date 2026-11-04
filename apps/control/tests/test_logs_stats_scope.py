"""请求日志 / 统计按 key 归属收窄的回归（2026-11-03 越权修复）。

数据直接写临时 SQLite，断言是确定性的；用户名、密钥名都带随机后缀，
用来判断响应体有没有把他人身份带出来。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
import uuid

import httpx

DENIED_DETAIL = "无权查看他人密钥的请求日志"


def _new_user(admin: httpx.Client) -> tuple[httpx.Client, str]:
    name = f"scope-viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users",
        json={"username": name, "password": "viewer-pass", "role": "viewer"},
    )
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return client, name


def _new_key(admin: httpx.Client, name: str, owner: str | None = None) -> dict:
    body: dict = {"name": name}
    if owner is not None:
        body["owner_user_id"] = owner
    created = admin.post("/api/keys", json=body)
    assert created.status_code == 200, created.text
    return created.json()


def _seed_logs(db_path: str, provider: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for row in rows:
            conn.execute(
                "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, "
                "completion_tokens, latency_ms, stream, error, key_id, key_name) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["ts"], provider, row.get("model", "scope-model"), row["status"],
                    row.get("prompt_tokens", 0), row.get("completion_tokens", 0),
                    100, 0, row.get("error"), row.get("key_id"), row.get("key_name", ""),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_ledger(db_path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for row in rows:
            conn.execute(
                "INSERT INTO billing_ledger(ts, key_id, key_name, provider, model, status, billed_micro) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    row["ts"], row.get("key_id"), row.get("key_name", ""), "scope",
                    "scope-model", row.get("status", 200), row["billed_micro"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _insert_orphan_key(db_path: str) -> int:
    """老库存量：owner_user_id 为 NULL，非 admin 不可见。"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO api_keys(name, key_hash, prefix, enabled, created_at, owner_user_id) "
            "VALUES(?,?,?,1,?,NULL)",
            (f"orphan-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex, "u2a_orphan", int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_viewer_logs_scoped_to_owned_keys(admin: httpx.Client, db_path: str) -> None:
    viewer, name = _new_user(admin)
    tag = uuid.uuid4().hex[:8]
    own = _new_key(admin, f"own-{name}-{tag}", owner=name)
    admin_key = _new_key(admin, f"admin-{tag}")
    provider = f"scope-provider-{tag}"
    base = int(time.time())
    _seed_logs(db_path, provider, [
        {"ts": base - 40, "status": 200, "key_id": own["id"], "key_name": own["name"]},
        {"ts": base - 30, "status": 200, "key_id": own["id"], "key_name": own["name"]},
        {"ts": base - 20, "status": 500, "key_id": own["id"], "key_name": own["name"]},
        {"ts": base - 10, "status": 200, "key_id": admin_key["id"], "key_name": admin_key["name"]},
        {"ts": base - 5, "status": 200, "key_id": admin_key["id"], "key_name": admin_key["name"]},
    ])
    try:
        got = viewer.get("/api/logs", params={"provider": provider, "limit": 50})
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["total"] == 3, body
        assert {row["key_id"] for row in body["items"]} == {own["id"]}
        assert {row["key_name"] for row in body["items"]} == {own["name"]}
        # 掩码前缀：只回 u2a_ 后 4 位；完整密钥与未掩码的 prefix 都不能出现
        assert {row["key_prefix"] for row in body["items"]} == {f"u2a_{own['key'][4:8]}****"}
        assert own["key"] not in got.text
        assert own["prefix"] not in got.text
        assert admin_key["name"] not in got.text
    finally:
        viewer.close()

    all_rows = admin.get("/api/logs", params={"provider": provider, "limit": 50})
    assert all_rows.status_code == 200, all_rows.text
    assert all_rows.json()["total"] == 5
    assert {row["key_id"] for row in all_rows.json()["items"]} == {own["id"], admin_key["id"]}


def test_viewer_cross_key_returns_structured_403(admin: httpx.Client, db_path: str) -> None:
    viewer, name = _new_user(admin)
    tag = uuid.uuid4().hex[:8]
    own = _new_key(admin, f"own-{name}-{tag}", owner=name)
    admin_key = _new_key(admin, f"admin-{tag}")
    orphan_id = _insert_orphan_key(db_path)
    try:
        denied = viewer.get("/api/logs", params={"key_id": admin_key["id"]})
        assert denied.status_code == 403, denied.text
        error = denied.json()["error"]
        assert error["type"] == "permission_denied"
        assert error["code"] == "insufficient_role"
        assert error["required_role"] == "admin"
        assert error["role"] == "viewer"
        assert denied.json()["detail"] == DENIED_DETAIL
        assert admin_key["name"] not in denied.text
        assert admin_key["key"] not in denied.text

        # NULL 归属的存量 key 同样不可读，不返回空集合假装成功
        orphan = viewer.get("/api/logs", params={"key_id": orphan_id})
        assert orphan.status_code == 403, orphan.text
        assert orphan.json()["error"]["type"] == "permission_denied"

        # 自己的 key 照常可读
        assert viewer.get("/api/logs", params={"key_id": own["id"]}).status_code == 200

        # admin 的过滤行为不变
        assert admin.get("/api/logs", params={"key_id": admin_key["id"]}).status_code == 200
        assert admin.get("/api/logs", params={"key_id": orphan_id}).status_code == 200
    finally:
        viewer.close()


def test_viewer_stats_aggregate_only_owned_keys(admin: httpx.Client, db_path: str) -> None:
    viewer, name = _new_user(admin)
    other_viewer, other_name = _new_user(admin)
    tag = uuid.uuid4().hex[:8]
    own = _new_key(admin, f"own-{name}-{tag}", owner=name)
    other = _new_key(admin, f"other-{other_name}-{tag}", owner=other_name)
    admin_key = _new_key(admin, f"admin-{tag}")
    base = int(time.time())
    _seed_logs(db_path, f"scope-stats-{tag}", [
        {"ts": base - 90, "status": 200, "model": "scope-alpha", "prompt_tokens": 10,
         "completion_tokens": 20, "key_id": own["id"], "key_name": own["name"]},
        {"ts": base - 60, "status": 200, "model": "scope-alpha", "prompt_tokens": 5,
         "completion_tokens": 7, "key_id": own["id"], "key_name": own["name"]},
        {"ts": base - 30, "status": 500, "model": "scope-alpha", "prompt_tokens": 3,
         "completion_tokens": 4, "key_id": own["id"], "key_name": own["name"]},
        {"ts": base - 50, "status": 200, "model": "scope-gamma", "prompt_tokens": 100,
         "completion_tokens": 200, "key_id": other["id"], "key_name": other["name"]},
        {"ts": base - 20, "status": 200, "model": "scope-gamma", "prompt_tokens": 100,
         "completion_tokens": 200, "key_id": other["id"], "key_name": other["name"]},
        {"ts": base - 15, "status": 200, "model": "scope-beta", "prompt_tokens": 1000,
         "completion_tokens": 2000, "key_id": admin_key["id"], "key_name": admin_key["name"]},
        {"ts": base - 10, "status": 200, "model": "scope-beta", "prompt_tokens": 1000,
         "completion_tokens": 2000, "key_id": admin_key["id"], "key_name": admin_key["name"]},
        {"ts": base - 5, "status": 200, "model": "scope-beta", "prompt_tokens": 1000,
         "completion_tokens": 2000, "key_id": admin_key["id"], "key_name": admin_key["name"]},
    ])
    _seed_ledger(db_path, [
        {"ts": base - 90, "key_id": own["id"], "key_name": own["name"], "billed_micro": 100},
        {"ts": base - 60, "key_id": own["id"], "key_name": own["name"], "billed_micro": 200},
        {"ts": base - 30, "key_id": own["id"], "key_name": own["name"], "billed_micro": 300},
        {"ts": base - 50, "key_id": other["id"], "key_name": other["name"], "billed_micro": 5000},
        {"ts": base - 10, "key_id": admin_key["id"], "key_name": admin_key["name"], "billed_micro": 9000},
    ])

    try:
        got = viewer.get("/api/stats/summary")
        assert got.status_code == 200, got.text
        stats = got.json()
        assert stats["total_requests"] == 3
        assert stats["total_tokens"] == 49
        assert stats["week_requests"] == 2
        assert stats["week_tokens"] == 42
        assert stats["today_requests"] == 2
        assert stats["today_tokens"] == 42
        assert stats["failures"]["today_5xx"] == 1
        assert stats["failures"]["week_5xx"] == 1
        # 100+200+300 micro-USD
        assert stats["total_credit"] == 0.0006
        assert stats["week_credit"] == 0.0006
        assert stats["today_credit"] == 0.0006
        assert stats["active_keys"] == 1
        assert stats["top_model"] == "scope-alpha"
        assert stats["usage_health"]["logs_today"] == 2
        # 他人用户名 / 他人模型 / 他人密钥名都不能出现在响应里
        assert other_name not in got.text
        assert "scope-gamma" not in got.text
        assert "scope-beta" not in got.text

        daily = viewer.get("/api/stats/daily", params={"days": 1})
        assert daily.status_code == 200, daily.text
        points = daily.json()
        assert len(points) == 1
        today_point = points[0]
        assert today_point["day"] == dt.date.today().isoformat()
        assert today_point["requests"] == 2
        assert today_point["prompt_tokens"] == 15
        assert today_point["completion_tokens"] == 27
        assert today_point["credit"] == 0.0006
        assert today_point["failed"] == 1

        # admin 仍是全站口径：数字不小于 viewer，且能看到别的模型的量
        admin_stats = admin.get("/api/stats/summary")
        assert admin_stats.status_code == 200, admin_stats.text
        site = admin_stats.json()
        assert site["total_requests"] >= 8
        assert site["total_requests"] > stats["total_requests"]
        assert site["total_credit"] > stats["total_credit"]
        admin_daily = admin.get("/api/stats/daily", params={"days": 1}).json()
        assert admin_daily[-1]["requests"] >= 7
        assert admin_daily[-1]["credit"] > 0.0006
    finally:
        viewer.close()
        other_viewer.close()


def test_viewer_without_keys_sees_empty_not_site_data(admin: httpx.Client) -> None:
    viewer, _ = _new_user(admin)
    try:
        logs = viewer.get("/api/logs", params={"limit": 50})
        assert logs.status_code == 200, logs.text
        assert logs.json() == {"total": 0, "items": []}

        stats = viewer.get("/api/stats/summary")
        assert stats.status_code == 200, stats.text
        body = stats.json()
        assert body["total_requests"] == 0
        assert body["total_tokens"] == 0
        assert body["active_keys"] == 0
        assert body["top_model"] is None
        assert body["total_credit"] == 0

        points = viewer.get("/api/stats/daily", params={"days": 1}).json()
        assert sum(point["requests"] for point in points) == 0
        assert sum(point["credit"] for point in points) == 0
    finally:
        viewer.close()
