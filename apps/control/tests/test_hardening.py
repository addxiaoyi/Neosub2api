"""审计加固回归：S-09 文档/健康检查、S-11/S-16 服务端响应头、S-12 登录节流、S-14 上游错误脱敏。"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import uuid

import httpx
import pytest

from app.adapters.base import ProviderError
from app.adapters.client import UnifiedApiClient

pytestmark = pytest.mark.security


def test_docs_disabled_and_healthz_hides_db(anon: httpx.Client) -> None:
    """S-09：未认证的接口文档与部署路径都不该暴露。"""
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert anon.get(path).status_code == 404, path
    body = anon.get("/healthz")
    assert body.status_code == 200
    assert "db" not in body.json()


def test_security_headers_present(anon: httpx.Client) -> None:
    """S-16：服务端兜底安全头，反代没配时也生效。"""
    r = anon.get("/healthz")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")


def test_cors_methods_are_explicit(anon: httpx.Client) -> None:
    """S-11：CORS 方法/头不再通配。"""
    r = anon.options(
        "/api/settings/gateway",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-methods") != "*"
    assert r.headers.get("access-control-allow-headers") != "*"


def test_login_is_throttled(anon: httpx.Client) -> None:
    """S-12：同一 IP + 用户名的连续失败会被短暂锁定。"""
    username = f"ghost-{uuid.uuid4().hex[:8]}"
    client = httpx.Client(base_url=str(anon.base_url), trust_env=False, timeout=10.0)
    try:
        for _ in range(5):
            assert client.post("/api/login", json={"username": username, "password": "bad"}).status_code == 401
        locked = client.post("/api/login", json={"username": username, "password": "bad"})
        assert locked.status_code == 429
    finally:
        client.close()


def test_upstream_error_does_not_leak_base_url_or_body() -> None:
    """S-14：ProviderError 只给状态码，上游响应体/内部地址只进日志。"""
    secret = "SECRET-UPSTREAM-KEY-42"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"{secret} at http://10.0.0.9")

    class MockClient(UnifiedApiClient):
        def _client(self) -> httpx.AsyncClient:
            return httpx.AsyncClient(base_url=self.base_url, transport=httpx.MockTransport(handler), trust_env=False)

    client = MockClient("http://127.0.0.1:8061")
    with pytest.raises(ProviderError) as caught:
        asyncio.run(client.post_json("/v1/chat"))
    message = caught.value.message
    assert "500" in message
    assert secret not in message
    assert "10.0.0.9" not in message
    assert "127.0.0.1" not in message


def test_weak_admin_password_is_warned(caplog) -> None:
    """S-03：库里沿用的弱口令要在启动日志提示，而不是悄悄继续用。"""
    from types import SimpleNamespace

    from app.main import _warn_if_weak

    with caplog.at_level("WARNING", logger="unified.security"):
        _warn_if_weak(SimpleNamespace(admin_password="unified-admin"))
        _warn_if_weak(SimpleNamespace(admin_password="1234"))
        _warn_if_weak(SimpleNamespace(admin_password="a-long-enough-secret"))
    assert sum("偏弱" in record.getMessage() for record in caplog.records) == 2


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="[env] Windows 文件系统没有 POSIX 权限位，0600/0700 只能在不含 win32 的平台上验证",
)
def test_db_file_is_owner_only(tmp_path) -> None:
    """S-05：库文件（上游 key / session token）不应对同机其他用户可读。"""
    from app.db import Database

    db = Database(tmp_path / "unified.db")
    conn = db.connect()
    conn.close()
    assert stat.S_IMODE(os.stat(tmp_path / "unified.db").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700
