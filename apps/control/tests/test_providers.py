"""provider 注册表管理：list 的风险标注、PATCH enable/disable、PATCH base_url 立即生效。"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.providers


def _by_id(items: list[dict], pid: str) -> dict:
    for it in items:
        if it.get("id") == pid:
            return it
    raise KeyError(pid)


def test_list_public_includes_risk_level(admin: httpx.Client) -> None:
    r = admin.get("/api/providers")
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    items = r.json()
    assert isinstance(items, list) and items, "provider 列表不应为空"
    for item in items:
        assert "id" in item
        assert "risk_level" in item, f"{item['id']} 缺 risk_level"
        # risk_note 在 safe 时可空，但字段必须在
        assert "risk_note" in item, f"{item['id']} 缺 risk_note"


def test_patch_enable_disable(admin: httpx.Client) -> None:
    # codebuddy 由 seed 默认 enabled=1
    before = admin.get("/api/providers")
    codebuddy = _by_id(before.json(), "codebuddy")
    was_enabled = bool(codebuddy.get("enabled"))

    # 关掉
    off = admin.patch("/api/providers/codebuddy", json={"enabled": False})
    assert off.status_code == 200, f"patch off: {off.status_code} {off.text}"
    assert off.json()["enabled"] is False

    check = admin.get("/api/providers")
    assert _by_id(check.json(), "codebuddy")["enabled"] is False

    # 开回原值
    on = admin.patch("/api/providers/codebuddy", json={"enabled": was_enabled})
    assert on.status_code == 200
    assert bool(on.json()["enabled"]) is was_enabled

    # 验证 opencode（初始 enabled=0）也能翻
    off2 = admin.patch("/api/providers/opencode", json={"enabled": True})
    assert off2.status_code == 200, f"opencode patch: {off2.status_code} {off2.text}"
    assert off2.json()["enabled"] is True
    admin.patch("/api/providers/opencode", json={"enabled": False})


def test_patch_base_url_takes_effect(admin: httpx.Client, fake_base: str) -> None:
    # codebuddy 初始 base_url 在环境变量里已经指向 fake_base；
    # 我们改成一个假的占位，再改回来。registry.update() 立即应用到进程内适配器。
    snapshot = admin.get("/api/providers").json()
    codebuddy = _by_id(snapshot, "codebuddy")
    original = codebuddy.get("base_url") or fake_base

    placeholder = "http://127.0.0.1:19999"
    changed = admin.patch("/api/providers/codebuddy", json={"base_url": placeholder})
    assert changed.status_code == 200, f"patch base_url: {changed.status_code} {changed.text}"
    assert changed.json()["base_url"] == placeholder

    # list 里也应该看到
    body = admin.get("/api/providers").json()
    assert _by_id(body, "codebuddy")["base_url"] == placeholder

    # 改回原值（环境变量 fake_base）
    restore = admin.patch("/api/providers/codebuddy", json={"base_url": fake_base})
    assert restore.status_code == 200
    assert restore.json()["base_url"] == fake_base
