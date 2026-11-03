"""统一凭据库：CRUD、脱敏、优先级（db > env > default）、多凭据轮换、viewer 权限。

HTTP 侧复用 conftest 的临时控制面（UNIFIED_TEST_CTRL_PORT / UNIFIED_TEST_FAST_PORT），
优先级与轮换用进程内 registry + 临时库，避免污染会话级测试库。
"""

from __future__ import annotations

import sqlite3
import uuid

import httpx
import pytest

from app.config import ENV_BASE_VARS, load_settings
from app.db import Database
from app.registry import ProviderRegistry

pytestmark = pytest.mark.providers

SECRET = "sk-live-CRED-42"
MASKED = "sk-l…D-42"


def _login_viewer(admin: httpx.Client) -> httpx.Client:
    name = f"cred-viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post("/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"})
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return client


def _drop(admin: httpx.Client, cid: int) -> None:
    admin.delete(f"/api/provider-credentials/{cid}")


def test_crud_masks_secret_everywhere(admin: httpx.Client, db_path: str) -> None:
    created = admin.post(
        "/api/provider-credentials",
        json={"provider": "opencode", "label": "主账号", "secret": SECRET, "note": "smoke"},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    cid = body["id"]
    try:
        assert "secret" not in body, "响应不能回显明文 secret"
        assert body["secret_masked"] == MASKED
        assert body["has_secret"] is True
        assert body["provider"] == "opencode"

        listing = admin.get("/api/provider-credentials")
        assert listing.status_code == 200
        assert SECRET not in listing.text
        assert any(item["id"] == cid for item in listing.json())

        providers = admin.get("/api/providers")
        assert providers.status_code == 200
        assert SECRET not in providers.text
        opencode = next(item for item in providers.json() if item["id"] == "opencode")
        assert opencode["credential_count"] == 1
        assert opencode["has_api_key"] is True
        assert opencode["credentials"][0]["secret_masked"] == MASKED

        # "***" = 不改密钥；空串 = 清除
        kept = admin.patch(f"/api/provider-credentials/{cid}", json={"label": "备用号", "secret": "***"})
        assert kept.status_code == 200, kept.text
        assert kept.json()["secret_masked"] == MASKED
        assert kept.json()["label"] == "备用号"

        cleared = admin.patch(f"/api/provider-credentials/{cid}", json={"secret": ""})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["has_secret"] is False
        assert cleared.json()["secret_masked"] == ""

        # 审计只记字段名，不落密钥值
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT detail FROM audit_logs WHERE action = 'provider_credentials.create' ORDER BY id DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "创建凭据必须写审计"
        assert "secret" in rows[0][0]
        assert SECRET not in rows[0][0]
    finally:
        _drop(admin, cid)


def test_unknown_provider_and_bad_url_rejected(admin: httpx.Client) -> None:
    unknown = admin.post("/api/provider-credentials", json={"provider": "nope", "secret": SECRET})
    assert unknown.status_code == 404, unknown.text
    bad = admin.post(
        "/api/provider-credentials",
        json={"provider": "opencode", "base_url": "ftp://example.com"},
    )
    assert bad.status_code == 400, bad.text
    missing = admin.patch("/api/provider-credentials/999999", json={"label": "x"})
    assert missing.status_code == 404, missing.text


def test_db_credential_overrides_env_base(admin: httpx.Client, fake_base: str) -> None:
    created = admin.post(
        "/api/provider-credentials",
        json={"provider": "codebuddy", "label": "临时", "base_url": "http://127.0.0.1:19090"},
    )
    assert created.status_code == 200, created.text
    cid = created.json()["id"]
    try:
        items = admin.get("/api/providers").json()
        codebuddy = next(item for item in items if item["id"] == "codebuddy")
        assert codebuddy["base_url"] == "http://127.0.0.1:19090"
        assert codebuddy["base_url_source"] == "db"
    finally:
        _drop(admin, cid)

    # 删掉凭据后回落到 env（conftest 用 CODEBUDDY_BASE 指向假上游）
    items = admin.get("/api/providers").json()
    codebuddy = next(item for item in items if item["id"] == "codebuddy")
    assert codebuddy["base_url"] == fake_base
    assert codebuddy["base_url_source"] == "env"


def test_viewer_can_read_but_not_write(admin: httpx.Client) -> None:
    viewer = _login_viewer(admin)
    try:
        assert viewer.get("/api/provider-credentials").status_code == 200
        assert viewer.post(
            "/api/provider-credentials", json={"provider": "opencode", "secret": SECRET}
        ).status_code == 403
        assert viewer.patch("/api/provider-credentials/1", json={"label": "x"}).status_code == 403
        assert viewer.delete("/api/provider-credentials/1").status_code == 403
    finally:
        viewer.close()


def _registry(tmp_path, monkeypatch, env: dict[str, str] | None = None) -> ProviderRegistry:
    for var in ENV_BASE_VARS.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("UNIFIED_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UNIFIED_DB_PATH", str(tmp_path / "unified.db"))
    monkeypatch.setenv("UNIFIED_ADMIN_PASSWORD", "test")
    monkeypatch.setenv("UNIFIED_ALLOW_PRIVATE_UPSTREAMS", "1")
    for var, value in (env or {}).items():
        monkeypatch.setenv(var, value)
    settings = load_settings()
    return ProviderRegistry(Database(settings.db_path), settings)


def test_base_url_priority_default_then_env_then_db(tmp_path, monkeypatch) -> None:
    registry = _registry(tmp_path, monkeypatch)
    view = registry.resolved("trae_sidecar")
    assert view.base_url == "http://127.0.0.1:7871"
    assert view.base_url_source == "default"

    registry = _registry(tmp_path, monkeypatch, {"TRAE_SIDECAR_BASE": "http://127.0.0.1:9777"})
    view = registry.resolved("trae_sidecar")
    assert view.base_url == "http://127.0.0.1:9777"
    assert view.base_url_source == "env"

    registry.create_credential("trae_sidecar", label="db", base_url="http://127.0.0.1:9666")
    view = registry.resolved("trae_sidecar")
    assert view.base_url == "http://127.0.0.1:9666"
    assert view.base_url_source == "db"


def test_credentials_rotate_in_declared_order(tmp_path, monkeypatch) -> None:
    registry = _registry(tmp_path, monkeypatch)
    registry.create_credential("opencode", label="second", secret="key-second", sort_order=2)
    registry.create_credential("opencode", label="first", secret="key-first", sort_order=1)
    registry.create_credential("opencode", label="off", secret="key-off", enabled=False, sort_order=0)

    view = registry.resolved("opencode")
    assert view.api_keys == ("key-first", "key-second"), "禁用凭据不参与轮换"
    adapter = registry.get("opencode")
    assert adapter.api_key == "key-first"
    assert adapter.api_keys == ["key-first", "key-second"]
    assert adapter._keys() == ["key-first", "key-second"]

    # 改顺序后立即生效
    second = next(row for row in registry.credentials("opencode") if row["label"] == "second")
    registry.update_credential(second["id"], sort_order=0)
    assert registry.resolved("opencode").api_keys[0] == "key-second"
