"""管理端可用化：分页/筛选/导出与显式运维动作。

只验证「看得到、筛得准、导得干净、动作有边界」，不在测试里真的打上游。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from typing import Any

import httpx
import pytest

CONSOLE_PROVIDERS = ("codebuddy", "muse", "gptgrok", "zcode", "trae", "qoder", "codar")

EXPECTED_ACTIONS = {
    "codebuddy": {"probe", "run_task"},
    "muse": {"probe", "keepalive", "clear_cooldown"},
    "gptgrok": {"probe", "cancel_task"},
    "zcode": {"probe", "claim", "clear_cooldown"},
    "trae": {"probe", "clear_cooldown"},
    "qoder": {"probe", "clear_cooldown", "reset_sessions"},
    "codar": {"probe", "clear_cooldown"},
}


def _seed_accounts(db_path: str, provider: str, *, owner: str | None = None, count: int = 5) -> None:
    conn = sqlite3.connect(db_path)
    stamp = int(time.time())
    try:
        for index in range(count):
            conn.execute(
                "INSERT OR REPLACE INTO accounts (file, provider, uid, nickname, enterprise_id, realm,"
                " expires_at, credits, note, disabled, created_at, owner_user_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"/srv/creds/{provider}-{index}.json",
                    provider,
                    f"uid-{provider}-{index}",
                    f"nick-{index}",
                    "",
                    "cn",
                    0 if index == 0 else stamp - 60,
                    12.5,
                    "",
                    1 if index == 1 else 0,
                    stamp + index,
                    owner,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_logs(db_path: str, provider: str, *, count: int = 6, key_id: int | None = None) -> None:
    conn = sqlite3.connect(db_path)
    stamp = int(time.time())
    try:
        for index in range(count):
            failed = index % 2 == 1
            conn.execute(
                "INSERT INTO request_logs (ts, provider, model, status, prompt_tokens, completion_tokens,"
                " latency_ms, stream, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stamp - index,
                    provider,
                    "model-a" if index % 2 == 0 else "model-b",
                    502 if failed else 200,
                    10,
                    5,
                    120,
                    0,
                    "upstream http://127.0.0.1:9000 boom" if failed else "",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _viewer(admin: httpx.Client) -> tuple[str, httpx.Client]:
    name = f"console-mgmt-{uuid.uuid4().hex[:8]}"
    created = admin.post("/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"})
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return name, client


def _csv_body(resp: httpx.Response) -> str:
    assert resp.status_code == 200, resp.text
    assert "text/csv" in resp.headers.get("content-type", "")
    assert "attachment" in resp.headers.get("content-disposition", "")
    return resp.text


@pytest.mark.parametrize("provider", CONSOLE_PROVIDERS)
def test_accounts_page_and_filters(admin: httpx.Client, db_path: str, provider: str) -> None:
    _seed_accounts(db_path, provider)
    first = admin.get(f"/api/providers/{provider}/console/accounts", params={"page": 1, "size": 2})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["size"] == 2 and body["page"] == 1
    assert body["total"] >= 5
    assert len(body["items"]) == 2
    assert "/" not in body["items"][0]["file"] and "\\" not in body["items"][0]["file"]

    disabled = admin.get(
        f"/api/providers/{provider}/console/accounts", params={"status": "disabled", "size": 100}
    )
    assert disabled.status_code == 200, disabled.text
    assert all(item["disabled"] for item in disabled.json()["items"])

    searched = admin.get(
        f"/api/providers/{provider}/console/accounts", params={"q": f"uid-{provider}-3"}
    )
    assert searched.status_code == 200, searched.text
    assert searched.json()["total"] == 1

    clamped = admin.get(f"/api/providers/{provider}/console/accounts", params={"size": 999})
    assert clamped.json()["size"] == 100

    bad = admin.get(f"/api/providers/{provider}/console/accounts", params={"status": "nope"})
    assert bad.status_code == 400


@pytest.mark.parametrize("provider", CONSOLE_PROVIDERS)
def test_accounts_export(admin: httpx.Client, db_path: str, provider: str) -> None:
    _seed_accounts(db_path, provider)
    body = _csv_body(admin.get(f"/api/providers/{provider}/console/accounts/export"))
    assert body.startswith("\ufeff")
    assert "UID" in body.splitlines()[0]
    assert "/srv/creds" not in body
    assert "\\" not in body
    assert "Bearer " not in body


@pytest.mark.parametrize("provider", CONSOLE_PROVIDERS)
def test_logs_page_and_filters(admin: httpx.Client, db_path: str, provider: str) -> None:
    _seed_logs(db_path, provider)
    page = admin.get(f"/api/providers/{provider}/console/logs", params={"size": 3})
    assert page.status_code == 200, page.text
    body = page.json()
    assert len(body["items"]) == 3
    assert body["pages"] >= 2

    failed = admin.get(f"/api/providers/{provider}/console/logs", params={"status": "failed", "size": 50})
    assert failed.status_code == 200, failed.text
    # 失败口径与 /api/logs 一致：4xx/5xx，或已出帧后断流的 2xx（只能靠 error 列区分）
    items = failed.json()["items"]
    assert items and all(item["status"] >= 400 or item["error"] for item in items)

    by_model = admin.get(f"/api/providers/{provider}/console/logs", params={"model": "model-b", "size": 50})
    assert by_model.status_code == 200, by_model.text
    assert all(item["model"] == "model-b" for item in by_model.json()["items"])

    assert admin.get(f"/api/providers/{provider}/console/logs", params={"status": "nope"}).status_code == 400


@pytest.mark.parametrize("provider", CONSOLE_PROVIDERS)
def test_logs_export_is_redacted(admin: httpx.Client, db_path: str, provider: str) -> None:
    _seed_logs(db_path, provider)
    body = _csv_body(admin.get(f"/api/providers/{provider}/console/logs/export"))
    assert "模型" in body
    assert "127.0.0.1" not in body
    assert "http://" not in body


@pytest.mark.parametrize("provider", CONSOLE_PROVIDERS)
def test_viewer_scope_and_readonly(admin: httpx.Client, db_path: str, provider: str) -> None:
    name, viewer = _viewer(admin)
    try:
        _seed_accounts(db_path, provider, count=2)  # 无主账号，viewer 不可见
        _seed_accounts(db_path, provider, owner=name, count=1)

        scoped = viewer.get(f"/api/providers/{provider}/console/accounts", params={"size": 100})
        assert scoped.status_code == 200, scoped.text
        assert scoped.json()["total"] == 1
        assert scoped.json()["items"][0]["uid"] == f"uid-{provider}-0"

        exported = _csv_body(viewer.get(f"/api/providers/{provider}/console/accounts/export"))
        assert exported.count("\n") <= 2  # 表头 + 1 行

        denied = viewer.post(
            f"/api/providers/{provider}/console/actions", json={"action": "clear_cooldown"}
        )
        assert denied.status_code in (400, 403), denied.text
    finally:
        viewer.close()


@pytest.mark.parametrize("provider", CONSOLE_PROVIDERS)
def test_actions_matrix(admin: httpx.Client, provider: str) -> None:
    snapshot = admin.get(f"/api/providers/{provider}/console")
    assert snapshot.status_code == 200, snapshot.text
    actions = set(snapshot.json()["actions"])
    assert actions == EXPECTED_ACTIONS[provider]


@pytest.mark.parametrize("provider", ["codebuddy", "gptgrok"])
def test_clear_cooldown_rejected_where_unavailable(admin: httpx.Client, provider: str) -> None:
    resp = admin.post(f"/api/providers/{provider}/console/actions", json={"action": "clear_cooldown"})
    assert resp.status_code == 400, resp.text


def test_clear_cooldown_and_reset_sessions_bounds(admin: httpx.Client) -> None:
    resp = admin.post("/api/providers/trae/console/actions", json={"action": "clear_cooldown"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "clear_cooldown"

    not_qoder = admin.post("/api/providers/trae/console/actions", json={"action": "reset_sessions"})
    assert not_qoder.status_code == 400

    qoder = admin.post("/api/providers/qoder/console/actions", json={"action": "reset_sessions"})
    assert qoder.status_code == 200, qoder.text
    assert qoder.json()["ok"] is True
    assert isinstance(qoder.json()["result"].get("cleared"), int)


def test_unknown_provider_export_404(admin: httpx.Client) -> None:
    resp = admin.get("/api/providers/nope/console/logs/export")
    assert resp.status_code == 404


def test_task_runs_export_admin_only(admin: httpx.Client) -> None:
    body = _csv_body(admin.get("/api/providers/codebuddy/tasks/export"))
    assert "任务" in body
    name, viewer = _viewer(admin)
    try:
        assert viewer.get("/api/providers/codebuddy/tasks/export").status_code == 403
    finally:
        viewer.close()
