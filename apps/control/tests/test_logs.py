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


def test_viewer_can_read_logs(admin: httpx.Client, seeded) -> None:
    provider, _, _ = seeded
    viewer = _login_viewer(admin)
    try:
        got = viewer.get("/api/logs", params={"provider": provider, "limit": 2})
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2
        item = body["items"][0]
        # 字段名与 apps/web/lib/types.ts 的 RequestLog 对齐（表里没有的列给 null）
        for key in (
            "id", "ts", "key_id", "key_name", "model", "status", "prompt_tokens",
            "completion_tokens", "latency_ms", "stream", "error", "first_token_ms",
            "cache_hit_tokens", "cache_miss_tokens", "cache_write_tokens", "credit", "account",
        ):
            assert key in item, key
        assert item["first_token_ms"] is None
        assert item["cache_hit_tokens"] is None
        assert item["provider"] == provider
    finally:
        viewer.close()


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


def test_admin_can_clear_logs(admin: httpx.Client, seeded) -> None:
    """放在最后：清空会删掉本次会话里所有 request_logs，前面用例已全部跑完。"""
    provider, _, _ = seeded
    got = admin.post("/api/logs/clear")
    assert got.status_code == 200, got.text
    assert got.json()["ok"] is True
    assert admin.get("/api/logs", params={"provider": provider}).json()["total"] == 0
