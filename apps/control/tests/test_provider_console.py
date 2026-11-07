"""管理台只读快照与动作权限：页面不点就报错，viewer 不拿假按钮。"""

from __future__ import annotations

import json
import uuid

import httpx

CONSOLE_PROVIDERS = ("codebuddy", "muse", "gptgrok", "zcode", "trae", "qoder", "codar")


def _viewer(admin: httpx.Client) -> httpx.Client:
    name = f"console-viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post("/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"})
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return client


def test_console_snapshot_for_each_provider(admin: httpx.Client) -> None:
    for pid in CONSOLE_PROVIDERS:
        resp = admin.get(f"/api/providers/{pid}/console", params={"window": "7d"})
        assert resp.status_code == 200, f"{pid}: {resp.status_code} {resp.text}"
        body = resp.json()
        assert body["provider"]["id"] == pid
        assert body["usage"]["window"] == "7d"
        assert isinstance(body["credentials"], list)
        assert "readonly" in body and body["readonly"] is False

        raw = json.dumps(body, ensure_ascii=False)
        assert "Bearer " not in raw
        assert "127.0.0.1" not in raw
        assert "http://" not in raw


def test_console_viewer_is_readonly(admin: httpx.Client) -> None:
    viewer = _viewer(admin)
    try:
        resp = viewer.get("/api/providers/muse/console")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["readonly"] is True
        assert body["actions"] == []

        denied = viewer.post("/api/providers/muse/console/actions", json={"action": "probe"})
        assert denied.status_code == 403, denied.text

        tasks = viewer.get("/api/providers/codebuddy/tasks")
        assert tasks.status_code == 403, tasks.text
    finally:
        viewer.close()


def test_console_unknown_provider(admin: httpx.Client) -> None:
    resp = admin.get("/api/providers/not-a-provider/console")
    assert resp.status_code == 404
    assert "not-a-provider" in resp.json()["detail"]


def test_console_unsupported_action(admin: httpx.Client) -> None:
    resp = admin.post("/api/provider_console_probe_placeholder", json={})
    assert resp.status_code == 404

    resp = admin.post("/api/providers/trae/console/actions", json={"action": "keepalive"})
    assert resp.status_code == 400
    assert "keepalive" in resp.json()["detail"]


def test_codebuddy_task_log_surface(admin: httpx.Client) -> None:
    resp = admin.get("/api/providers/codebuddy/tasks")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_types"] == ["checkin_all", "refresh_credits"]
    assert isinstance(body["items"], list)

    bad = admin.post("/api/providers/codebuddy/tasks", json={"task": "drop_database"})
    assert bad.status_code == 400


def test_gptgrok_media_inventory_without_config(admin: httpx.Client) -> None:
    resp = admin.get("/api/providers/gptgrok/console/media", params={"kind": "files"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is False
    assert body["items"] == []

    denied = admin.get("/api/providers/muse/console/media")
    assert denied.status_code == 400
