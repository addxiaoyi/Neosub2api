"""api_keys 批量操作：逐条回执、200 上限、admin-only、审计、响应体不泄密钥。"""

from __future__ import annotations

import sqlite3
import uuid

import httpx
import pytest

pytestmark = pytest.mark.keys

VIEWER_PASSWORD = "batch-viewer-pass"


@pytest.fixture
def viewer_key(admin: httpx.Client, base_url: str):
    """独立 viewer 账号（批量端口必须 403；用会话登录而不是令牌）。"""
    name = f"batchview-{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users",
        json={"username": name, "password": VIEWER_PASSWORD, "role": "viewer"},
    )
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": VIEWER_PASSWORD})
    assert login.status_code == 200, login.text
    yield client
    client.close()


@pytest.fixture
def make_key(admin: httpx.Client):
    def _make(name: str) -> dict:
        r = admin.post("/api/keys", json={"name": name})
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        return r.json()

    return _make


def _create_token(admin: httpx.Client, name: str) -> dict:
    r = admin.post("/api/tokens", json={"name": name})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()


def _key_enabled(admin: httpx.Client, key_id: int) -> bool:
    for item in admin.get("/api/keys").json():
        if item["id"] == key_id:
            return bool(item["enabled"])
    raise AssertionError(f"密钥 {key_id} 不在列表里")


def test_batch_disable_all_ok(admin: httpx.Client, make_key) -> None:
    one = make_key("batch-ok-1")
    two = make_key("batch-ok-2")
    r = admin.post("/api/keys/batch", json={"action": "disable", "ids": [str(one["id"]), str(two["id"])]})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "disable"
    assert [row["id"] for row in body["results"]] == [str(one["id"]), str(two["id"])]
    assert all(row["ok"] for row in body["results"])
    # 逐条语义与单条 PUT 一致：列表里两把都变成停用
    assert _key_enabled(admin, one["id"]) is False
    assert _key_enabled(admin, two["id"]) is False


def test_batch_partial_failure_keeps_going(admin: httpx.Client, make_key) -> None:
    good = make_key("batch-partial-good")
    missing_id = 9_999_999
    r = admin.post(
        "/api/keys/batch",
        json={"action": "disable", "ids": [str(good["id"]), str(missing_id), "not-a-number"]},
    )
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    assert body["ok"] is False, "有一条失败就不能把整体报成成功"
    rows = {row["id"]: row for row in body["results"]}
    assert rows[str(good["id"])]["ok"] is True
    assert rows[str(missing_id)]["ok"] is False
    assert rows[str(missing_id)]["error"] == "密钥不存在"
    assert rows["not-a-number"]["ok"] is False
    assert rows["not-a-number"]["error"] == "ID 必须是整数"
    # 失败的两条没有阻断第一条的写入
    assert _key_enabled(admin, good["id"]) is False


def test_batch_delete_removes_rows_and_quota(admin: httpx.Client, make_key, db_path: str) -> None:
    doomed = make_key("batch-delete")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO quota_policies(key_id, provider, model, window, limit_micro, "
            "limit_requests, enabled, created_at, updated_at) VALUES(?, '', '', 'day', 0, 1, 1, 0, 0)",
            (doomed["id"],),
        )
        conn.commit()
        policy_before = conn.execute(
            "SELECT COUNT(*) FROM quota_policies WHERE key_id = ?", (doomed["id"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert policy_before == 1

    r = admin.post("/api/keys/batch", json={"action": "delete", "ids": [str(doomed["id"])]})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    assert r.json()["results"][0]["ok"] is True

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM api_keys WHERE id = ?", (doomed["id"],)).fetchone()[0] == 0
    finally:
        conn.close()


def test_batch_over_limit_is_400(admin: httpx.Client) -> None:
    ids = [str(10_000 + i) for i in range(201)]
    r = admin.post("/api/keys/batch", json={"action": "disable", "ids": ids})
    assert r.status_code == 400, f"超限必须拒绝而不是截断：{r.status_code} {r.text}"
    assert "200" in r.text


def test_batch_rejects_empty_and_unknown_action(admin: httpx.Client) -> None:
    r = admin.post("/api/keys/batch", json={"action": "disable", "ids": []})
    assert r.status_code == 400, r.text
    r = admin.post("/api/keys/batch", json={"action": "restart", "ids": ["1"]})
    assert r.status_code == 400, r.text


def test_batch_is_admin_only(viewer_key: httpx.Client, make_key) -> None:
    key = make_key("batch-viewer-target")
    r = viewer_key.post("/api/keys/batch", json={"action": "disable", "ids": [str(key["id"])]})
    assert r.status_code == 403, f"{r.status_code} {r.text}"
    assert r.json()["error"]["required_role"] == "admin"
    r = viewer_key.post("/api/tokens/batch", json={"action": "disable", "ids": [str(key["id"])]})
    assert r.status_code == 403, f"{r.status_code} {r.text}"


def test_batch_response_carries_no_secret(admin: httpx.Client, make_key) -> None:
    key = make_key("batch-secret")
    plaintext = key["key"]
    r = admin.post("/api/keys/batch", json={"action": "disable", "ids": [str(key["id"])]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "action", "results"}
    for row in body["results"]:
        assert set(row) <= {"id", "ok", "error"}
        assert "key" not in row and "key_hash" not in row
    # 明文、哈希、前缀都不得出现在响应里
    assert plaintext not in r.text
    assert key["prefix"] not in r.text
    assert "key_hash" not in r.text


def test_batch_writes_one_audit_row(admin: httpx.Client, make_key, db_path: str) -> None:
    one = make_key("batch-audit-1")
    two = make_key("batch-audit-2")
    conn = sqlite3.connect(db_path)
    try:
        before = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'keys.batch'").fetchone()[0]
    finally:
        conn.close()

    r = admin.post("/api/keys/batch", json={"action": "disable", "ids": [str(one["id"]), str(two["id"])]})
    assert r.status_code == 200, r.text

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        after = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'keys.batch'").fetchone()[0]
        row = conn.execute(
            "SELECT actor, action, target, detail FROM audit_logs "
            "WHERE action = 'keys.batch' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    assert after == before + 1, f"一次批量只能落一条审计，实际新增 {after - before} 条"
    assert row["actor"] == "admin"
    assert row["target"] == "disable"
    assert "count=2" in row["detail"]


def test_tokens_batch_disable_and_no_secret(admin: httpx.Client) -> None:
    token = _create_token(admin, f"batch-token-{uuid.uuid4().hex[:6]}")
    plaintext = token["token"]
    r = admin.post("/api/tokens/batch", json={"action": "disable", "ids": [str(token["id"])]})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    assert body["results"][0]["ok"] is True
    assert plaintext not in r.text
    listed = next(item for item in admin.get("/api/tokens").json() if item["id"] == token["id"])
    assert listed["enabled"] is False


def test_token_delete_cascades_quota(admin: httpx.Client, db_path: str) -> None:
    """既有缺陷回归：令牌删除原先只删 api_keys，配额策略会变成孤儿。"""
    token = _create_token(admin, f"batch-token-cascade-{uuid.uuid4().hex[:6]}")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO quota_policies(key_id, provider, model, window, limit_micro, "
            "limit_requests, enabled, created_at, updated_at) VALUES(?, '', '', 'day', 0, 1, 1, 0, 0)",
            (token["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    r = admin.delete(f"/api/tokens/{token['id']}")
    assert r.status_code == 200, f"{r.status_code} {r.text}"

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM api_keys WHERE id = ?", (token["id"],)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_policies WHERE key_id = ?", (token["id"],)
        ).fetchone()[0] == 0
    finally:
        conn.close()
