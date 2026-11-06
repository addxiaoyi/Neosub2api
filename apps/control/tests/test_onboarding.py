"""首次启动向导状态：默认态、持久化/去重、完成清空、重置、写权限。"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.auth


@pytest.fixture(autouse=True)
def clean(admin: httpx.Client):
    """每个用例从「未完成」开始，避免共享库里的状态串味。"""
    admin.post("/api/onboarding/reset")
    yield
    admin.post("/api/onboarding/reset")


def test_default_state_is_not_completed(admin: httpx.Client) -> None:
    r = admin.get("/api/onboarding")
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    assert body["completed"] is False
    assert body["current_step"] == 0
    assert body["skipped_steps"] == []
    assert body["updated_at"] is None


def test_save_merges_dedupes_and_drops_unknown_steps(admin: httpx.Client) -> None:
    r = admin.post(
        "/api/onboarding",
        json={"current_step": 3, "skipped_steps": ["provider", "credential", "provider", "bogus"]},
    )
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    assert body["current_step"] == 3
    assert body["skipped_steps"] == ["provider", "credential"], "应去重并丢弃非法 step"
    assert body["completed"] is False
    assert isinstance(body["updated_at"], int)

    # 二次读回必须是持久化后的结果，而不是内存默认值
    again = admin.get("/api/onboarding").json()
    assert again["current_step"] == 3
    assert again["skipped_steps"] == ["provider", "credential"]


def test_current_step_is_clamped(admin: httpx.Client) -> None:
    body = admin.post("/api/onboarding", json={"current_step": 99}).json()
    assert body["current_step"] == 5, "越界步号应夹到最后一页"
    body = admin.post("/api/onboarding", json={"current_step": -3}).json()
    assert body["current_step"] == 0


def test_complete_clears_skipped_items(admin: httpx.Client) -> None:
    admin.post("/api/onboarding", json={"skipped_steps": ["smoke", "key"]})
    body = admin.post("/api/onboarding", json={"completed": True}).json()
    assert body["completed"] is True
    assert body["skipped_steps"] == [], "完成即不应再有待办项"
    # 完成后再刷新页面不该重新进入向导——接口侧的事实就是 completed=True
    assert admin.get("/api/onboarding").json()["completed"] is True


def test_reset_returns_default(admin: httpx.Client) -> None:
    admin.post("/api/onboarding", json={"completed": True})
    body = admin.post("/api/onboarding/reset").json()
    assert body == {"completed": False, "current_step": 0, "skipped_steps": [], "updated_at": None}


def test_reset_reopens_wizard_gate(admin: httpx.Client) -> None:
    """reset 后必须回到「未完成」，这正是前端自动弹向导的唯一判据。"""
    assert admin.post("/api/onboarding", json={"completed": True}).json()["completed"] is True
    admin.post("/api/onboarding/reset")
    state = admin.get("/api/onboarding").json()
    assert state["completed"] is False, "reset 后 completed 必须变回 false"
    assert state["current_step"] == 0
    assert state["skipped_steps"] == []


def test_anonymous_cannot_read(anon: httpx.Client) -> None:
    r = httpx.get(f"{anon.base_url}/api/onboarding", trust_env=False)
    assert r.status_code in (401, 403), f"未登录不该读到向导状态：{r.status_code}"


def test_viewer_cannot_write(admin: httpx.Client, base_url: str) -> None:
    username = "onb_viewer"
    admin.post("/api/users", json={"username": username, "password": "viewer-pw", "role": "viewer"})
    viewer = httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)
    try:
        login = viewer.post("/api/login", json={"username": username, "password": "viewer-pw"})
        assert login.status_code == 200, f"viewer 登录失败：{login.status_code} {login.text}"
        r = viewer.post("/api/onboarding", json={"completed": True})
        assert r.status_code == 403, f"viewer 不该能写：{r.status_code} {r.text}"
        # 读仍然允许（首页待办条对 viewer 不展示，但接口不额外设限）
        assert viewer.get("/api/onboarding").status_code == 200
    finally:
        viewer.close()
        admin.delete(f"/api/users/{username}")
