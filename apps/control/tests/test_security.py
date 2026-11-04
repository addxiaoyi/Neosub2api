"""安全审计回归：S-01 脱敏/管理员校验、S-02 严格布尔、S-03 随机口令、S-04 地址守卫。"""

from __future__ import annotations

import sqlite3
import uuid
from urllib.parse import urlparse

import httpx
import pytest
from fastapi import HTTPException

from app.adapters.base import ProviderError
from app.adapters.sub2api_store import QuotaStore
from app.api_utils import mask_secret, parse_bool
from app.config import _admin_password, load_settings
from app.db import Database
from app.net_guard import UpstreamAddressError, guard_upstream_url
from app.registry import ProviderRegistry

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
                "SELECT ts, actor, action, target, COALESCE(detail, ''), COALESCE(ip, '') "
                "FROM audit_logs WHERE action = 'settings.reveal_secret' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "查看明文必须写审计日志"
        ts, actor, action, target, *_ = rows[-1]
        assert ts
        assert actor == "admin"
        assert action == "settings.reveal_secret"
        assert target == "provider_config.opencode.api_key"
        # 审计整行（谁/何时/哪条）都不得出现明文
        assert SECRET not in "|".join(str(part) for part in rows[-1])
    finally:
        admin.patch("/api/providers/opencode", json={"api_key": ""})


def test_non_admin_cannot_reveal(admin: httpx.Client) -> None:
    viewer = _login_viewer(admin)
    try:
        got = viewer.post("/api/settings/secret/reveal", json={"path": "provider_config.opencode.api_key"})
        assert got.status_code == 403
        error = got.json()["error"]
        assert error["type"] == "permission_denied"
        assert error["code"] == "insufficient_role"
        assert error["required_role"] == "admin"
        assert error["role"] == "viewer"
    finally:
        viewer.close()


# 值里同时有路径同形片段与特殊字符：弱脱敏（按字段名替换）会漏出尾巴
TRICKY_SECRET = "opencode-api_key'\";\n-- u2a_lookalike_9F3K"


def _audit_rows(db_path: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT ts, actor, action, target, COALESCE(detail, ''), COALESCE(ip, '') "
            "FROM audit_logs ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_reveal_audit_row_never_carries_plaintext(admin: httpx.Client, db_path: str) -> None:
    """审计边界：明文只回给当次响应，审计行 target/detail 在任何情况下都不带值。"""
    path = "provider_config.opencode.api_key"
    saved = admin.patch("/api/providers/opencode", json={"api_key": TRICKY_SECRET})
    assert saved.status_code == 200, saved.text
    try:
        got = admin.post("/api/settings/secret/reveal", json={"path": path})
        assert got.status_code == 200, got.text
        assert got.json()["value"] == TRICKY_SECRET

        # 失败路径也不能把明文顺带吐出来
        missing = admin.post(
            "/api/settings/secret/reveal", json={"path": "provider_config.opencode.no_such_key"}
        )
        assert missing.status_code == 404, missing.text
        assert TRICKY_SECRET not in missing.text
        bad_root = admin.post("/api/settings/secret/reveal", json={"path": "gateway.allow_anonymous"})
        assert bad_root.status_code == 400, bad_root.text
        assert TRICKY_SECRET not in bad_root.text

        rows = _audit_rows(db_path)
        assert rows, "查看明文必须写审计日志"
        for row in rows:
            line = "|".join(str(part) for part in row)
            assert TRICKY_SECRET not in line, f"审计行出现明文：{line}"
            assert "u2a_lookalike_9F3K" not in line, f"审计行出现明文片段：{line}"

        # 成功、404、400 三种结果各留一行，动作后缀直接读出结局
        actions = [row[2] for row in rows]
        assert "settings.reveal_secret" in actions, "成功调用要有审计行"
        assert "settings.reveal_secret.path_not_found" in actions, "404 失败也要有审计行"
        assert "settings.reveal_secret.bad_root" in actions, "400 失败也要有审计行"

        last = rows[-1]
        assert last[2] == "settings.reveal_secret.bad_root"
        assert last[3] == "gateway.allow_anonymous", "失败行 target 放被拒的配置路径"
        assert last[4] == "", f"detail 必须为空，实际 {last[4]!r}"
    finally:
        admin.patch("/api/providers/opencode", json={"api_key": ""})


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
        guard_upstream_url("http://127.0.0.1:8061/", allow_private=True) == "http://127.0.0.1:8061"
    )
    with pytest.raises(UpstreamAddressError):
        guard_upstream_url("ftp://example.com", allow_private=False)


def test_sub2_store_rejects_private_upstream(tmp_path) -> None:
    store = QuotaStore(tmp_path / "unified.db")
    store.upsert_group("g1", quota_limit=0)
    with pytest.raises(ProviderError) as ctx:
        store.add_account("g1", upstream_base="http://127.0.0.1:7899")
    assert ctx.value.status_code == 400


def _fresh_registry(monkeypatch: pytest.MonkeyPatch, tmp_path, *, allow_private: bool = False) -> ProviderRegistry:
    """按当前守卫默认配置建一个内存外的临时 registry（不依赖正在跑的控制面）。"""
    for var in (
        "TRAE_SIDECAR_BASE",
        "DEEPSEEK_SIDECAR_BASE",
        "QODER_SIDECAR_BASE",
        "CODAR_SIDECAR_BASE",
        "CODEBUDDY_BASE",
        "OPENCODE_UPSTREAM_BASE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("UNIFIED_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UNIFIED_DB_PATH", str(tmp_path / "unified.db"))
    monkeypatch.setenv("UNIFIED_ADMIN_PASSWORD", "test")
    if allow_private:
        monkeypatch.setenv("UNIFIED_ALLOW_PRIVATE_UPSTREAMS", "1")
    else:
        monkeypatch.delenv("UNIFIED_ALLOW_PRIVATE_UPSTREAMS", raising=False)
    settings = load_settings()
    return ProviderRegistry(Database(settings.db_path), settings)


def test_sidecar_loopback_base_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path, fake_base: str) -> None:
    """spec 默认地址是 loopback 的 provider，可以把地址改成假上游的 loopback 端口。"""
    registry = _fresh_registry(monkeypatch, tmp_path)
    for pid in ("trae_sidecar", "deepseek_sidecar", "qoder_sidecar", "codar_sidecar"):
        assert registry.allows_loopback(pid) is True, pid
        item = registry.update(pid, base_url=fake_base)
        assert item["base_url"] == fake_base
        assert item["allows_local_base_url"] is True


def test_non_loopback_provider_loopback_base_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path, fake_base: str
) -> None:
    """默认地址是公网的 provider 仍然拒绝 loopback，错误文案要点明这家未开放。"""
    registry = _fresh_registry(monkeypatch, tmp_path)
    for pid in ("opencode", "muse", "sub2api", "trae", "qoder"):
        assert registry.allows_loopback(pid) is False, pid
        with pytest.raises(ProviderError) as ctx:
            registry.update(pid, base_url=fake_base)
        assert ctx.value.status_code == 400
        assert "未开放本地地址" in ctx.value.message
        assert pid in ctx.value.message
        assert "UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1" in ctx.value.message


def test_sidecar_private_ranges_still_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path, fake_base: str
) -> None:
    """放行 loopback 不等于放行整个私网：10/172.16/192.168 仍然拒绝。"""
    port = urlparse(fake_base).port or 80
    registry = _fresh_registry(monkeypatch, tmp_path)
    for host in ("10.0.0.5", "172.16.0.9", "192.168.1.10"):
        with pytest.raises(ProviderError) as ctx:
            registry.update("trae_sidecar", base_url=f"http://{host}:{port}")
        assert ctx.value.status_code == 400
        assert "内网" in ctx.value.message


def test_metadata_address_never_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """云元数据地址即使 provider 允许 loopback、即使全局放开也始终拒绝。"""
    url = "http://169.254.169.254/latest/meta-data"
    for allow_private in (False, True):
        registry = _fresh_registry(monkeypatch, tmp_path, allow_private=allow_private)
        with pytest.raises(ProviderError) as ctx:
            registry.update("trae_sidecar", base_url=url)
        assert ctx.value.status_code == 400
        assert "元数据" in ctx.value.message
    with pytest.raises(UpstreamAddressError):
        guard_upstream_url(url, allow_private=True, allow_loopback=True, provider="trae_sidecar")


def test_public_address_unaffected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """公网地址不受新白名单影响，两类 provider 都能保存。"""
    registry = _fresh_registry(monkeypatch, tmp_path)
    public = "http://93.184.216.34:8080"
    for pid in ("trae_sidecar", "opencode"):
        assert registry.update(pid, base_url=public)["base_url"] == public


def test_guard_accepts_only_loopback_when_provider_allows(fake_base: str) -> None:
    port = urlparse(fake_base).port or 80
    loopbacks = (
        f"http://127.0.0.1:{port}",
        f"http://127.0.0.2:{port}",
        f"http://[::1]:{port}",
        f"http://localhost:{port}",
    )
    for url in loopbacks:
        assert guard_upstream_url(url, allow_loopback=True, provider="trae_sidecar") == url
        with pytest.raises(UpstreamAddressError):
            guard_upstream_url(url, allow_loopback=False, provider="opencode")


def test_providers_expose_local_base_allowance(admin: httpx.Client) -> None:
    items = {item["id"]: item for item in admin.get("/api/providers").json()}
    for pid in ("codebuddy", "trae_sidecar", "deepseek_sidecar", "qoder_sidecar", "codar_sidecar"):
        assert items[pid]["allows_local_base_url"] is True, pid
    for pid in ("opencode", "muse", "sub2api", "trae", "qoder"):
        assert items[pid]["allows_local_base_url"] is False, pid
