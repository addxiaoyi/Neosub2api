"""安全审计回归：S-01 脱敏/管理员校验、S-02 严格布尔、S-03 随机口令、S-04 地址守卫。"""

from __future__ import annotations

import sqlite3
import uuid

import httpx
import pytest
from fastapi import HTTPException

from app.adapters.base import ProviderError
from app.adapters.sub2api_store import QuotaStore
from app.api_utils import mask_secret, parse_bool
from app.config import _admin_password
from app.net_guard import UpstreamAddressError, guard_upstream_url

pytestmark = pytest.mark.security

SECRET = "sk-live-SECRET-42"
MASKED = "sk-l…T-42"


def _login_viewer(admin: httpx.Client) -> httpx.Client:
    name = f"viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"}
    )
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return client


def test_viewer_cannot_read_settings(admin: httpx.Client) -> None:
    """S-01：/api/settings/* 读写一律管理员，viewer 必须 403。"""
    viewer = _login_viewer(admin)
    try:
        for path in ("/api/settings/upstream", "/api/settings/gateway", "/api/settings/model-map"):
            assert viewer.get(path).status_code == 403, path
    finally:
        viewer.close()


def test_admin_settings_masks_upstream_key(admin: httpx.Client) -> None:
    """S-01：管理员读到的是掩码，不是明文。"""
    saved = admin.patch("/api/providers/opencode", json={"api_key": SECRET})
    assert saved.status_code == 200, saved.text
    try:
        body = admin.get("/api/settings/upstream")
        assert body.status_code == 200, body.text
        assert SECRET not in body.text
        entry = body.json()["provider_config"]["opencode"]
        assert entry["api_key_masked"] == MASKED
        assert entry["has_api_key"] is True
        assert "api_key" not in entry
        # 掩码回传不能覆盖库里真值
        echoed = admin.post("/api/settings/upstream", json={"provider_config": body.json()["provider_config"]})
        assert echoed.status_code == 200, echoed.text
        assert SECRET not in echoed.text
    finally:
        admin.patch("/api/providers/opencode", json={"api_key": ""})


def test_reveal_returns_plaintext_and_writes_audit(admin: httpx.Client, db_path: str) -> None:
    """S-01：显式查看明文是单独动作，且必须落审计日志。"""
    admin.patch("/api/providers/opencode", json={"api_key": SECRET})
    try:
        got = admin.post("/api/settings/secret/reveal", json={"path": "provider_config.opencode.api_key"})
        assert got.status_code == 200, got.text
        assert got.json()["value"] == SECRET

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT actor, action, target, detail FROM audit_logs "
                "WHERE action = 'settings.reveal_secret' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "查看明文必须写审计日志"
        actor, action, target, detail = rows[-1]
        assert actor == "admin"
        assert target == "provider_config.opencode.api_key"
        assert SECRET not in (detail or "")
    finally:
        admin.patch("/api/providers/opencode", json={"api_key": ""})


def test_non_admin_cannot_reveal(admin: httpx.Client) -> None:
    viewer = _login_viewer(admin)
    try:
        got = viewer.post("/api/settings/secret/reveal", json={"path": "provider_config.opencode.api_key"})
        assert got.status_code == 403
    finally:
        viewer.close()


def test_string_false_is_rejected_and_gateway_stays_closed(admin: httpx.Client, anon: httpx.Client) -> None:
    """S-02：字符串 "false" 必须解析成 False（不是真值），且匿名实际仍关闭。"""
    lowered = admin.post("/api/settings/gateway", json={"allow_anonymous": "false"})
    assert lowered.status_code == 200, lowered.text
    assert lowered.json()["allow_anonymous"] is False
    assert admin.get("/api/settings/gateway").json()["allow_anonymous"] is False
    # 匿名实际不可用：无 Authorization 的网关请求仍是 401
    assert anon.get("/v1/models").status_code == 401

    # 大小写不敏感
    upper = admin.post("/api/settings/gateway", json={"allow_anonymous": "FALSE"})
    assert upper.status_code == 200 and upper.json()["allow_anonymous"] is False

    for junk in (1, 0, None, "yes", "", [True]):
        rejected = admin.post("/api/settings/gateway", json={"allow_anonymous": junk})
        assert rejected.status_code == 400, f"{junk!r} 不该被接受"


def test_bool_and_mask_helpers() -> None:
    assert parse_bool(True, "x") is True
    assert parse_bool("TRUE", "x") is True
    assert parse_bool(" False ", "x") is False
    for junk in ("1", "0", "yes", "", None, 1, []):
        with pytest.raises(HTTPException):
            parse_bool(junk, "x")
    assert mask_secret(SECRET) == MASKED
    assert mask_secret("short") == "***"
    assert mask_secret("") == ""


def test_default_admin_password_is_generated(tmp_path, monkeypatch) -> None:
    """S-03：默认口令不再固定，首次启动随机生成并落盘。"""
    monkeypatch.setenv("UNIFIED_ADMIN_PASSWORD", "unified-admin")
    password, source = _admin_password(tmp_path)
    assert source == "generated"
    assert password != "unified-admin"
    assert len(password) >= 20
    stored = (tmp_path / "admin-password.txt").read_text(encoding="utf-8").strip()
    assert stored == password
    assert _admin_password(tmp_path) == (password, "file")


def test_private_upstreams_are_blocked() -> None:
    """S-04：base_url / upstream_base 不能指向环回、私网、链路本地或元数据地址。"""
    for url in (
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5",
        "http://192.168.1.10",
        "http://[::1]:8080",
    ):
        with pytest.raises(UpstreamAddressError):
            guard_upstream_url(url, allow_private=False)

    assert guard_upstream_url("http://93.184.216.34", allow_private=False) == "http://93.184.216.34"
    assert (
        guard_upstream_url("http://127.0.0.1:8021/", allow_private=True) == "http://127.0.0.1:8021"
    )
    with pytest.raises(UpstreamAddressError):
        guard_upstream_url("ftp://example.com", allow_private=False)


def test_sub2_store_rejects_private_upstream(tmp_path) -> None:
    store = QuotaStore(tmp_path / "unified.db")
    store.upsert_group("g1", quota_limit=0)
    with pytest.raises(ProviderError) as ctx:
        store.add_account("g1", upstream_base="http://127.0.0.1:7899")
    assert ctx.value.status_code == 400
