"""请求日志 / 安全面只读端点的回归测试。

数据由测试直接写临时 SQLite（db_path fixture），不依赖网关先跑出流量，
这样分页/过滤的断言是确定性的。
"""

from __future__ import annotations

import sqlite3
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.logs


def _login_viewer(admin: httpx.Client) -> httpx.Client:
    name = f"logs-viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post("/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"})
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return client


def _login_named_viewer(admin: httpx.Client) -> tuple[httpx.Client, str]:
    """建一个 viewer 并登录，返回 client 和用户名（审计按 actor=用户名 过滤）。"""
    name = f"logs-scope-{uuid.uuid4().hex[:8]}"
    created = admin.post("/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"})
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return client, name


def _seed_audit(db_path: str, actor: str, count: int, tag: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        base = int(time.time())
        for i in range(count):
            conn.execute(
                "INSERT INTO audit_logs(ts, actor, action, target, detail, ip) VALUES(?,?,?,?,?,?)",
                (base + i, actor, f"test.{tag}", "", "", "127.0.0.1"),
            )
        conn.commit()
    finally:
        conn.close()


def _audit_count(db_path: str, actor: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE actor = ?", (actor,)).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _seed_login_failed(db_path: str, actor: str, count: int, ip: str) -> None:
    """直接写 audit_logs：actor 是「被尝试的用户名」，与 auth.login 的写入点一致。"""
    conn = sqlite3.connect(db_path)
    try:
        base = int(time.time()) - 5
        for i in range(count):
            conn.execute(
                "INSERT INTO audit_logs(ts, actor, action, target, detail, ip) VALUES(?,?,?,?,?,?)",
                (base + i, actor, "login_failed", "", "用户名或密码错误", ip),
            )
        conn.commit()
    finally:
        conn.close()


def _seed(db_path: str, provider: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for row in rows:
            conn.execute(
                "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, "
                "completion_tokens, latency_ms, stream, error, key_id, key_name) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["ts"], provider, row.get("model", "gpt-test"), row["status"],
                    row.get("prompt_tokens", 10), row.get("completion_tokens", 20),
                    row.get("latency_ms", 100), row.get("stream", 0), row.get("error"),
                    row.get("key_id"), row.get("key_name", ""),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _tag() -> str:
    return f"logs-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def seeded(db_path: str) -> tuple[str, int, list[dict]]:
    provider = _tag()
    base = int(time.time())
    rows = [
        {"ts": base - 300, "status": 200, "model": "alpha-model", "key_id": 1, "key_name": "k1"},
        {"ts": base - 240, "status": 502, "model": "beta-model", "key_id": 1, "key_name": "k1", "error": "upstream"},
        {"ts": base - 180, "status": 200, "model": "alpha-model", "key_id": 2, "key_name": "k2", "stream": 1},
        {"ts": base - 120, "status": 429, "model": "gamma-model", "key_id": 2, "key_name": "k2"},
        {"ts": base - 60, "status": 200, "model": "beta-model", "key_id": 3, "key_name": "k3"},
    ]
    _seed(db_path, provider, rows)
    return provider, base, rows


def test_log_rows_expose_contract_fields(admin: httpx.Client, seeded) -> None:
    """字段契约用 admin 校验；viewer 的归属收窄见 test_logs_stats_scope.py。"""
    provider, _, _ = seeded
    got = admin.get("/api/logs", params={"provider": provider, "limit": 2})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    item = body["items"][0]
    # 字段名与 apps/web/lib/types.ts 的 RequestLog 对齐（表里没有的列给 null）
    for key in (
        "id", "ts", "key_id", "key_name", "key_prefix", "model", "status",
        "prompt_tokens", "completion_tokens", "latency_ms", "stream", "error",
        "first_token_ms", "cache_hit_tokens", "cache_miss_tokens",
        "cache_write_tokens", "credit", "account",
    ):
        assert key in item, key
    assert item["first_token_ms"] is None
    assert item["cache_hit_tokens"] is None
    assert item["provider"] == provider


def test_pagination_is_descending_and_stable(admin: httpx.Client, seeded) -> None:
    provider, _, _ = seeded
    first = admin.get("/api/logs", params={"provider": provider, "limit": 2, "offset": 0}).json()
    second = admin.get("/api/logs", params={"provider": provider, "limit": 2, "offset": 2}).json()
    assert first["total"] == second["total"] == 5
    stamps = [row["ts"] for row in first["items"] + second["items"]]
    assert stamps == sorted(stamps, reverse=True)
    assert {row["id"] for row in first["items"]}.isdisjoint({row["id"] for row in second["items"]})
    # 前端用的是 page/size，两种写法必须等价
    paged = admin.get("/api/logs", params={"provider": provider, "page": 2, "size": 2}).json()
    assert [row["id"] for row in paged["items"]] == [row["id"] for row in second["items"]]


def test_filters(admin: httpx.Client, seeded) -> None:
    provider, base, _ = seeded
    by_key = admin.get("/api/logs", params={"provider": provider, "key_id": 2}).json()
    assert by_key["total"] == 2

    by_model = admin.get("/api/logs", params={"provider": provider, "model": "alpha"}).json()
    assert by_model["total"] == 2

    ok_only = admin.get("/api/logs", params={"provider": provider, "status": "ok"}).json()
    assert ok_only["total"] == 3
    assert all(row["status"] < 400 for row in ok_only["items"])

    err_only = admin.get("/api/logs", params={"provider": provider, "status": "error"}).json()
    assert err_only["total"] == 2
    assert all(row["status"] >= 400 for row in err_only["items"])

    exact = admin.get("/api/logs", params={"provider": provider, "status": 502}).json()
    assert exact["total"] == 1

    # 时间范围：截止到 base-200，只剩最早两条
    ranged = admin.get("/api/logs", params={"provider": provider, "end": base - 200}).json()
    assert ranged["total"] == 2

    after = admin.get("/api/logs", params={"provider": provider, "start": base - 200}).json()
    assert after["total"] == 3


def test_empty_result_is_not_500(admin: httpx.Client) -> None:
    missing = admin.get("/api/logs", params={"provider": "no-such-provider"}).json()
    assert missing == {"total": 0, "items": []}


def test_logs_require_auth_and_clear_is_admin_only(admin: httpx.Client, base_url: str) -> None:
    # conftest 的 anon fixture 与 admin 是同一个 client（登录后带 cookie），
    # 这里要真正的未认证请求，另开一个干净 client
    stranger = httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)
    try:
        assert stranger.get("/api/logs").status_code == 401
    finally:
        stranger.close()
    viewer = _login_viewer(admin)
    try:
        assert viewer.post("/api/logs/clear").status_code == 403
    finally:
        viewer.close()


def test_security_read_endpoints(admin: httpx.Client) -> None:
    viewer = _login_viewer(admin)
    try:
        guard = viewer.get("/api/security/guard")
        assert guard.status_code == 200, guard.text
        body = guard.json()
        assert "allow_private_upstreams" in body
        assert "169.254.169.254" in body["blocked_addresses"]
        assert "localhost" in body["blocked_hostnames"]

        failures = viewer.get("/api/security/login-failures")
        assert failures.status_code == 200, failures.text
        payload = failures.json()
        assert {"window_sec", "max_failures", "active", "stats", "recent"} <= set(payload)
        assert isinstance(payload["active"], list)
        assert payload["stats"]["total"] >= 0

        audit = viewer.get("/api/security/audit")
        assert audit.status_code == 200, audit.text
        assert {"items", "total"} == set(audit.json())

        alias = viewer.get("/api/audit-logs", params={"limit": 5})
        assert alias.status_code == 200, alias.text
        assert alias.json()["total"] >= 0
    finally:
        viewer.close()


def test_viewer_audit_is_scoped_to_own_actor(admin: httpx.Client, db_path: str) -> None:
    """越权修复：非 admin 调 /api/audit-logs 只能看到 actor=自己 的记录，total 是过滤后真实数。"""
    viewer, name = _login_named_viewer(admin)
    tag = uuid.uuid4().hex[:8]
    other = f"logs-other-{tag}"
    try:
        _seed_audit(db_path, name, 7, tag)
        _seed_audit(db_path, other, 9, tag)
        own_total = _audit_count(db_path, name)

        first = viewer.get("/api/audit-logs", params={"limit": 3, "offset": 0})
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["total"] == own_total, f"total 应只统计自己的 {own_total} 条，实际 {body['total']}"
        assert len(body["items"]) == 3
        assert {row["actor"] for row in body["items"]} == {name}
        assert other not in first.text, "非 admin 不应看到他人记录"

        # 翻页时 total 仍是过滤后的总数，不随 offset 缩水
        second = viewer.get("/api/audit-logs", params={"limit": 3, "offset": 3})
        assert second.status_code == 200, second.text
        assert second.json()["total"] == own_total

        # 显式传自己的 actor 仍然可用；别名端点同一口径
        own_filter = viewer.get("/api/audit-logs", params={"actor": name})
        assert own_filter.status_code == 200, own_filter.text
        assert own_filter.json()["total"] == own_total
        alias = viewer.get("/api/security/audit", params={"limit": 3})
        assert alias.status_code == 200, alias.text
        assert alias.json()["total"] == own_total
    finally:
        viewer.close()


def test_admin_audit_sees_all_actors(admin: httpx.Client, db_path: str) -> None:
    tag = uuid.uuid4().hex[:8]
    actor_a, actor_b = f"logs-seed-a-{tag}", f"logs-seed-b-{tag}"
    _seed_audit(db_path, actor_a, 2, tag)
    _seed_audit(db_path, actor_b, 3, tag)

    all_rows = admin.get("/api/audit-logs", params={"action": f"test.{tag}", "limit": 500})
    assert all_rows.status_code == 200, all_rows.text
    body = all_rows.json()
    assert body["total"] == 5
    assert {row["actor"] for row in body["items"]} == {actor_a, actor_b}

    # admin 仍可按任意 actor 过滤
    only_a = admin.get("/api/audit-logs", params={"actor": actor_a})
    assert only_a.status_code == 200, only_a.text
    assert only_a.json()["total"] == 2


def test_viewer_denied_when_filtering_other_actor(admin: httpx.Client) -> None:
    viewer, _ = _login_named_viewer(admin)
    try:
        for path in ("/api/audit-logs", "/api/security/audit"):
            denied = viewer.get(path, params={"actor": "admin"})
            assert denied.status_code == 403, f"{path}: {denied.status_code} {denied.text}"
            error = denied.json()["error"]
            assert error["type"] == "permission_denied"
            assert error["code"] == "insufficient_role"
            assert error["required_role"] == "admin"
            assert error["role"] == "viewer"
            assert "无权查看他人审计日志" == denied.json()["detail"]
    finally:
        viewer.close()


def test_login_failures_admin_sees_site_wide(admin: httpx.Client, db_path: str) -> None:
    """admin 行为不变：全站节流状态 + 全站历史统计 + 最近记录。"""
    tag = uuid.uuid4().hex[:8]
    victim_a, victim_b = f"lf-a-{tag}", f"lf-b-{tag}"
    _seed_login_failed(db_path, victim_a, 2, ip="203.0.113.7")
    _seed_login_failed(db_path, victim_b, 3, ip="198.51.100.9")

    got = admin.get("/api/security/login-failures", params={"limit": 200})
    assert got.status_code == 200, got.text
    payload = got.json()
    assert payload["scope"] == "all"
    assert {"window_sec", "max_failures", "active", "stats", "recent"} <= set(payload)
    assert {victim_a, victim_b} <= {row["actor"] for row in payload["recent"]}
    assert payload["stats"]["total"] >= 5


def test_login_failures_viewer_gets_own_aggregate_only(admin: httpx.Client, db_path: str) -> None:
    """非 admin 只拿到自己账号的聚合数字：没有他人 IP / 用户名，也没有明细行。"""
    viewer, name = _login_named_viewer(admin)
    tag = uuid.uuid4().hex[:8]
    other = f"lf-other-{tag}"
    other_ip = "203.0.113.42"
    try:
        _seed_login_failed(db_path, name, 2, ip="203.0.113.11")
        _seed_login_failed(db_path, other, 4, ip=other_ip)
        _seed_login_failed(db_path, "anonymous", 3, ip="203.0.113.13")

        got = viewer.get("/api/security/login-failures", params={"limit": 50})
        assert got.status_code == 200, got.text
        payload = got.json()
        assert payload["scope"] == "self"
        assert set(payload) == {
            "scope", "window_sec", "max_failures", "active", "stats", "recent",
            "window_failures", "last_failed_ts",
        }
        assert payload["active"] == [] and payload["recent"] == []
        assert payload["stats"]["total"] == 2
        assert payload["last_failed_ts"] > 0
        # 响应体里不能出现别人的用户名 / 任何 IP（自己的也不行，省得猜来源）
        raw = got.text
        assert other not in raw
        assert other_ip not in raw
        assert "203.0.113.11" not in raw
        assert "anonymous" not in raw
    finally:
        viewer.close()


def test_login_failures_requires_login(base_url: str) -> None:
    stranger = httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)
    try:
        assert stranger.get("/api/security/login-failures").status_code == 401
    finally:
        stranger.close()


def test_admin_can_clear_logs(admin: httpx.Client, seeded) -> None:
    """放在最后：清空会删掉本次会话里所有 request_logs，前面用例已全部跑完。"""
    provider, _, _ = seeded
    got = admin.post("/api/logs/clear")
    assert got.status_code == 200, got.text
    assert got.json()["ok"] is True
    assert admin.get("/api/logs", params={"provider": provider}).json()["total"] == 0
