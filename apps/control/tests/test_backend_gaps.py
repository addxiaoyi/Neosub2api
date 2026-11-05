"""前端对照出的三个缺口在后端的支撑面。

- 账号批量动作：逐条 result，单条失败不整批 500；删除沿用单条口径回收镜像凭据。
- 登录失败定向清理：只清计数/冷却，不动审计证据，节流规则本身不变。
- Grok 任务详情：步骤来自事件表，不是按时间戳倒推的。

HTTP 用例走 conftest 的真实控制面；本文件自造自收（导入行、用户、grok 任务），
避免污染共享 SQLite 上其他文件的计数断言。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import httpx
import pytest

SECRET = "sk-gap-IMPORT-01"


@pytest.fixture(scope="module")
def stranger(base_url: str) -> Iterator[httpx.Client]:
    client = httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)
    yield client
    client.close()


@pytest.fixture(scope="module")
def viewer(admin: httpx.Client, db_path: str) -> Iterator[SimpleNamespace]:
    name = f"gap-view-{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users", json={"username": name, "password": "gap-view-pass", "role": "viewer"}
    )
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "gap-view-pass"})
    assert login.status_code == 200, login.text
    yield SimpleNamespace(client=client, name=name)
    client.close()
    # 走 DB 收尾：DELETE /api/users/{username} 会把路径模板写进 HTTP 指标，
    # 后面的 test_metrics_alerts 会断言输出里没有 "username" 字样
    _drop_user(db_path, name)


def _drop_user(db_path: str, name: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM users WHERE username = ?", (name,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module", autouse=True)
def _import_provider_enabled(admin: httpx.Client) -> Iterator[None]:
    """导入/删除机制用例需要 provider 已启用（后端现在会拒绝停用服务商的导入）。

    收尾还原停用态，避免污染后续依赖种子状态的用例。
    """
    was = next(p for p in admin.get("/api/providers").json() if p["id"] == "opencode").get("enabled")
    if not was:
        patched = admin.patch("/api/providers/opencode", json={"enabled": True})
        assert patched.status_code == 200, patched.text
    yield
    if not was:
        admin.patch("/api/providers/opencode", json={"enabled": False})


def _import(admin: httpx.Client, provider: str = "opencode") -> dict:
    label = f"gap-import-{uuid.uuid4().hex[:8]}"
    resp = admin.post(
        "/api/accounts/import",
        json={
            "provider": provider,
            "label": label,
            "credential_type": "api_key",
            "credential": SECRET,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rows(dicts: list[dict], file: str) -> dict:
    return next(item for item in dicts if item["id"] == file)


# --------------------------------------------------------------------------- #
# 1. 账号批量动作
# --------------------------------------------------------------------------- #


def test_batch_delete_partial_failure_and_credential_reclaim(admin: httpx.Client) -> None:
    first = _import(admin)
    second = _import(admin)
    missing = f"no-such-{uuid.uuid4().hex[:8]}"
    try:
        labels = {first["nickname"], second["nickname"]}
        before = admin.get("/api/provider-credentials", params={"provider": "opencode"}).json()
        assert {c["label"] for c in before} >= labels

        resp = admin.post(
            "/api/accounts/batch",
            json={"action": "delete", "files": [first["file"], missing, second["file"]]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["action"] == "delete"
        assert body["ok"] is False  # 有一条不存在，整批结果如实为不完整成功
        assert [item["ok"] for item in body["results"]] == [True, False, True]
        assert body["results"][1]["error"]

        files = {row["file"] for row in admin.get("/api/accounts").json()["accounts"]}
        assert first["file"] not in files and second["file"] not in files

        after = admin.get("/api/provider-credentials", params={"provider": "opencode"}).json()
        assert {c["label"] for c in after}.isdisjoint(labels), "删除账号没有回收镜像凭据"
    finally:
        for item in (first, second):
            admin.delete(f"/api/accounts/{item['file']}")


def test_batch_disable_and_restart_report_per_item_errors(admin: httpx.Client) -> None:
    item = _import(admin)
    try:
        # opencode 适配器没有单账号动作：失败必须落在该条上，而不是整批 500
        resp = admin.post(
            "/api/accounts/batch", json={"action": "disable", "files": [item["file"]]}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["results"][0]["id"] == item["file"]
        assert body["results"][0]["error"]
        assert "不支持" in body["results"][0]["error"]

        restart = admin.post(
            "/api/accounts/batch", json={"action": "restart", "files": [item["file"]]}
        )
        assert restart.status_code == 200, restart.text
        assert restart.json()["results"][0]["ok"] is False
        assert "不支持重启" in restart.json()["results"][0]["error"]
    finally:
        admin.delete(f"/api/accounts/{item['file']}")


def test_batch_validation_and_scope(
    admin: httpx.Client, viewer: SimpleNamespace, stranger: httpx.Client
) -> None:
    bad_action = admin.post("/api/accounts/batch", json={"action": "explode", "files": ["x"]})
    assert bad_action.status_code == 400
    empty = admin.post("/api/accounts/batch", json={"action": "delete", "files": []})
    assert empty.status_code == 400
    too_many = admin.post(
        "/api/accounts/batch",
        json={"action": "delete", "files": [f"f{i}" for i in range(201)]},
    )
    assert too_many.status_code == 400

    payload = {"action": "delete", "files": ["x"]}
    assert stranger.post("/api/accounts/batch", json=payload).status_code == 401
    assert viewer.client.post("/api/accounts/batch", json=payload).status_code == 403


# --------------------------------------------------------------------------- #
# 2. 登录失败定向清理
# --------------------------------------------------------------------------- #


def test_clear_login_failures_unlocks_user_and_keeps_audit(
    admin: httpx.Client, base_url: str, db_path: str
) -> None:
    name = f"gap-lock-{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users", json={"username": name, "password": "gap-lock-pass", "role": "viewer"}
    )
    assert created.status_code == 200, created.text
    bad = httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)
    try:
        for _ in range(5):
            failed = bad.post("/api/login", json={"username": name, "password": "wrong-pass"})
            assert failed.status_code == 401, failed.text
        locked = bad.post("/api/login", json={"username": name, "password": "wrong-pass"})
        assert locked.status_code == 429, locked.text

        site = admin.get("/api/security/login-failures", params={"limit": 200}).json()
        active = next(item for item in site["active"] if item["username"] == name)
        assert active["failures"] >= 5 and active["locked"] is True

        cleared = admin.post("/api/security/login-failures/clear", json={"username": name})
        assert cleared.status_code == 200, cleared.text
        body = cleared.json()
        assert body["ok"] is True
        assert body["cleared"]["users"] == 1
        assert body["cleared"]["throttle_keys"] >= 1

        after_clear = admin.get("/api/security/login-failures", params={"limit": 200}).json()
        assert all(item["username"] != name for item in after_clear["active"]), "计数没清干净"

        # 冷却已解除：再次失败回到 401 而不是 429，节流本身仍在（计数从 0 重新开始）
        unlocked = bad.post("/api/login", json={"username": name, "password": "wrong-pass"})
        assert unlocked.status_code == 401, unlocked.text

        audit = admin.get(
            "/api/audit-logs",
            params={"action": "security.login_failures_cleared", "limit": 50},
        ).json()
        assert any(row["target"] == name and row["actor"] == "admin" for row in audit["items"])
        # 清除是计数操作，不销毁登录失败证据
        failures = admin.get("/api/audit-logs", params={"action": "login_failed", "limit": 50}).json()
        assert any(row["actor"] == name for row in failures["items"])
    finally:
        bad.close()
        admin.post("/api/security/login-failures/clear", json={"username": name})
        _drop_user(db_path, name)


def test_clear_login_failures_requires_target_and_admin(
    admin: httpx.Client, viewer: SimpleNamespace, stranger: httpx.Client
) -> None:
    assert admin.post("/api/security/login-failures/clear", json={}).status_code == 400
    payload = {"ip": "203.0.113.250"}
    assert stranger.post("/api/security/login-failures/clear", json=payload).status_code == 401
    assert viewer.client.post("/api/security/login-failures/clear", json=payload).status_code == 403

    # 没有任何匹配时不报错，如实回 0（前端可据 cleared 显示「无可清理项」）
    none = admin.post("/api/security/login-failures/clear", json=payload)
    assert none.status_code == 200
    assert none.json()["cleared"] == {"throttle_keys": 0, "users": 0}


# --------------------------------------------------------------------------- #
# 3. Grok 任务详情
# --------------------------------------------------------------------------- #


def _wait_status(client: httpx.Client, task_id: int, wanted: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = client.get(f"/api/grok/register/tasks/{task_id}/detail")
        assert detail.status_code == 200, detail.text
        if detail.json()["status"] == wanted:
            return detail.json()
        time.sleep(0.2)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内进入 {wanted}")


def test_grok_task_detail_has_structured_steps(
    admin: httpx.Client, viewer: SimpleNamespace, db_path: str
) -> None:
    created = admin.post(
        "/api/grok/register/tasks", json={"count": 1, "max_attempts": 1, "workers": 1}
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]
    try:
        body = _wait_status(admin, task_id, "failed")
        assert body["error"], "失败任务必须带原因"
        steps = body["steps"]
        assert [step["step"] for step in steps][:3] == ["queued", "running", "failed"]
        assert all(isinstance(step["ts"], int) and step["step"] for step in steps)
        assert steps[-1]["status"] == "failed"
        assert body["timeline"]["created_at"] and body["timeline"]["started_at"]
        assert body["timeline"]["finished_at"]

        assert viewer.client.get(f"/api/grok/register/tasks/{task_id}/detail").status_code == 403
        missing = admin.get("/api/grok/register/tasks/999999/detail")
        assert missing.status_code == 404
    finally:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM grok_task_events WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM grok_tasks WHERE id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()
