"""UX 走查回归（U4）：扫码登录链路必须存在，且未启用上游时给出可读错误而非 405。"""

import httpx
import pytest


def _set_enabled(admin, enabled: bool) -> None:
    r = admin.patch("/api/providers/codebuddy", json={"enabled": enabled})
    assert r.status_code == 200, r.text


@pytest.mark.ux
def test_auth_start_route_exists_and_reports_unconfigured_upstream(admin):
    """服务商未启用时：路由存在（不是 405），返回中文可读 503。"""
    _set_enabled(admin, False)
    try:
        r = admin.post("/api/auth/start", json={"realm": "cn"})
        assert r.status_code == 503, r.text
        detail = r.json().get("detail", "")
        assert "扫码登录不可用" in detail
        assert "CodeBuddy" in detail
    finally:
        _set_enabled(admin, True)


@pytest.mark.ux
def test_auth_start_and_poll_bridge_through_adapter(admin):
    """启用时走适配器到上游：start 返回 {state, authUrl, realm}，poll 透传状态。"""
    _set_enabled(admin, True)
    r = admin.post("/api/auth/start", json={"realm": "global"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "fake-state"
    assert body["authUrl"].startswith("https://example.com/scan")
    assert body["realm"] == "global"

    p = admin.get("/api/auth/poll", params={"state": body["state"], "realm": "global", "region": "HK"})
    assert p.status_code == 200, p.text
    assert p.json()["status"] == "success"

    # state 为空与上游同口径：回 invalid，不再打上游
    empty = admin.get("/api/auth/poll", params={"state": ""})
    assert empty.status_code == 200
    assert empty.json() == {"status": "invalid"}


@pytest.mark.ux
def test_auth_start_requires_admin(base_url):
    # anon/admin 是同一个会话级 client，这里单独开一个干净客户端
    with httpx.Client(base_url=base_url, trust_env=False, timeout=10.0) as guest:
        r = guest.post("/api/auth/start", json={"realm": "cn"})
    assert r.status_code == 401
