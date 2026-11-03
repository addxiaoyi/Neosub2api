"""系统信息面板（更新日志 / 更新状态）与访问令牌薄封装的回归。

权限口径：GET 允许 viewer（与 /api/keys 现状一致），写操作一律 admin。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from app import __version__
from app.routers import system

pytestmark = pytest.mark.system


def _login_viewer(admin: httpx.Client) -> httpx.Client:
    name = f"sys-viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"}
    )
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    assert client.post("/api/login", json={"username": name, "password": "viewer-pass"}).status_code == 200
    return client


def test_viewer_reads_tokens_but_cannot_write(admin: httpx.Client) -> None:
    created = admin.post("/api/tokens", json={"name": "viewer-probe"})
    assert created.status_code == 200, created.text
    token_id = created.json()["id"]
    viewer = _login_viewer(admin)
    try:
        assert viewer.get("/api/tokens").status_code == 200, "viewer 可读列表"
        assert viewer.post("/api/tokens", json={"name": "nope"}).status_code == 403
        assert viewer.patch(f"/api/tokens/{token_id}", json={"enabled": False}).status_code == 403
        assert viewer.delete(f"/api/tokens/{token_id}").status_code == 403
    finally:
        viewer.close()
        admin.delete(f"/api/tokens/{token_id}")


def test_admin_token_lifecycle_maps_into_keys(admin: httpx.Client, anon: httpx.Client) -> None:
    """创建返回明文、列表不含明文、删除后同一把 u2a_ 在 /v1 立即 401。"""
    r = admin.post("/api/tokens", json={"name": "ci-token", "expires_at": None})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith("u2a_")
    assert body["scope"] == "readonly"
    assert body["expires_at"] is None
    assert body["created_by"] == ""
    token_id, plaintext = body["id"], body["token"]

    listed = next(it for it in admin.get("/api/tokens").json() if it["id"] == token_id)
    assert "token" not in listed, "列表不得回传明文"
    assert listed["prefix"] == plaintext[:10]

    patch = admin.patch(f"/api/tokens/{token_id}", json={"name": "ci-renamed", "enabled": False})
    assert patch.status_code == 200
    assert patch.json()["name"] == "ci-renamed" and patch.json()["enabled"] is False

    assert admin.delete(f"/api/tokens/{token_id}").status_code == 200
    assert all(it["id"] != token_id for it in admin.get("/api/tokens").json())

    hit = anon.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {plaintext}"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert hit.status_code == 401, f"删除令牌应等同吊销密钥：{hit.status_code}"


def test_token_rejects_admin_scope_and_expiry(admin: httpx.Client) -> None:
    """不支持的能力直接 400，不静默降级成假承诺。"""
    assert admin.post("/api/tokens", json={"name": "x", "scope": "admin"}).status_code == 400
    assert admin.post("/api/tokens", json={"name": "x", "expires_at": 1700000000}).status_code == 400

    created = admin.post("/api/tokens", json={"name": "scope-probe"}).json()
    try:
        assert admin.patch(f"/api/tokens/{created['id']}", json={"scope": "admin"}).status_code == 400
    finally:
        admin.delete(f"/api/tokens/{created['id']}")


def test_changelog_parses_repo_file(admin: httpx.Client) -> None:
    r = admin.get("/api/system/changelog")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["current"] == __version__
    assert body["versions"], "仓库根 CHANGELOG.md 应该有内容"
    first = body["versions"][0]
    assert first["version"] == "0.1.0"
    assert first["date"] == "2026-11-02"
    titles = [s["title"] for s in first["sections"]]
    assert "安全" in titles and "新增" in titles
    assert all(item["level"] == 0 for s in first["sections"] for item in s["items"])


def test_changelog_missing_file_is_not_500(tmp_path: Path) -> None:
    """解析层直接给一个不存在的路径：返回空列表 + available=false。"""
    missing = tmp_path / "CHANGELOG.md"
    body = system.read_changelog(missing)
    assert body["available"] is False
    assert body["versions"] == []
    assert body["current"] == __version__
    assert "不存在" in body["error"]


def test_update_status_is_honest(admin: httpx.Client) -> None:
    r = admin.get("/api/system/update-status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert body["current_version"] == __version__
    assert body["version"] == __version__
    assert body["reason"]
    assert body["running"] is False and body["logs"] == []
    assert body["updater_found"] is False


def test_versions_shape_and_viewer_can_read(admin: httpx.Client) -> None:
    """单版本部署：manager 是真实版本，不编造多版本列表。"""
    r = admin.get("/api/system/versions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["manager"] == __version__
    assert isinstance(body["upstream_connected"], bool)
    assert body["upstream_accounts"] is None or body["upstream_accounts"] >= 0
    assert body["upstream_dir"] == ""
    assert body["upstream_connected"] == bool(body["upstream_accounts"])

    viewer = _login_viewer(admin)
    try:
        assert viewer.get("/api/system/versions").status_code == 200
    finally:
        viewer.close()


def test_check_update_never_invents_a_new_version(admin: httpx.Client) -> None:
    viewer = _login_viewer(admin)
    try:
        r = viewer.get("/api/system/check-update")
        force = viewer.get("/api/system/check-update", params={"force": "true"})
    finally:
        viewer.close()
    assert r.status_code == 200, r.text
    assert force.status_code == 200, "force 也应可用，只是结论不变"

    body = r.json()
    assert body["has_any"] is False
    assert body["current_version"] == __version__
    assert body["manager"]["current"] == __version__
    assert body["manager"]["latest"] == __version__, "没有远端时不得报出更高的版本号"
    assert body["manager"]["has_update"] is False
    assert body["upstream"]["has_update"] is False
    assert body["upstream"]["latest"] == ""
    # 与 update-status 同源：同一句原因，避免两套语义
    status = admin.get("/api/system/update-status").json()
    assert body["reason"] == status["reason"]
    assert body["manager"]["error"] == status["reason"]
    assert body["upstream"]["error"] == status["reason"]
    assert body["checked_at"] > 0 and body["cached"] is False
    assert force.json()["has_any"] is False
