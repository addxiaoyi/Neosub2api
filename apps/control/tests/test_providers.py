"""provider 注册表管理：list 的风险标注与就绪状态、PATCH enable/disable、PATCH base_url 立即生效。"""

from __future__ import annotations

import httpx
import pytest

from app.adapters.base import ProviderSpec

pytestmark = pytest.mark.providers

# 清单依用户核实结果：needs_config 的 11 家有真实卡点，其余 3 家填好 key/地址即可用
EXPECTED_READINESS = {
    "codebuddy": "needs_config",
    "muse": "needs_config",
    "grok-register": "needs_config",
    "gptgrok": "needs_config",
    "zcode": "needs_config",
    "opencode": "ready",
    "sub2api": "needs_config",
    "trae": "ready",
    "trae_sidecar": "needs_config",
    "deepseek_sidecar": "needs_config",
    "qoder": "ready",
    "qoder_sidecar": "needs_config",
    "codar": "needs_config",
    "codar_sidecar": "needs_config",
}

# needs_config 必须点出关键配置项，方便前端直接显示缺什么
EXPECTED_REQUIRES_HINTS = {
    "codebuddy": ("7864", "wbt_"),
    "grok-register": ("GROK_REGISTER_EXECUTOR_URL",),
    "sub2api": ("分组", "503"),
    "zcode": ("official_system", "3012"),
    "gptgrok": ("sentinel_token", "403"),
    "muse": ("api_key",),
    "trae_sidecar": ("sidecar", "7871"),
    "deepseek_sidecar": ("sidecar", "5001"),
    "qoder_sidecar": ("sidecar", "7873"),
    "codar": ("JWT",),
    "codar_sidecar": ("sidecar", "8801"),
}


def _by_id(items: list[dict], pid: str) -> dict:
    for it in items:
        if it.get("id") == pid:
            return it
    raise KeyError(pid)


@pytest.fixture
def restore_codebuddy_base_url(admin: httpx.Client):
    """收尾把 codebuddy.base_url 拉回进用例前的值，避免污染同 session 后续用例。"""
    before = _by_id(admin.get("/api/providers").json(), "codebuddy").get("base_url") or ""
    try:
        yield
    finally:
        admin.patch("/api/providers/codebuddy", json={"base_url": before})


def test_spec_to_public_exposes_readiness() -> None:
    spec = ProviderSpec(
        id="demo",
        name="Demo",
        vendor="demo",
        license="MIT",
        runtime="内置",
        status="implemented",
        description="demo",
        default_base_url="",
        readiness="needs_config",
        requires=("sidecar 进程",),
    )
    public = spec.to_public(enabled=False, base_url="", label="")
    assert public["readiness"] == "needs_config"
    assert public["requires"] == ["sidecar 进程"]


def test_spec_rejects_needs_config_without_requires() -> None:
    with pytest.raises(ValueError):
        ProviderSpec(
            id="demo",
            name="Demo",
            vendor="demo",
            license="MIT",
            runtime="内置",
            status="implemented",
            description="demo",
            default_base_url="",
            readiness="needs_config",
        )


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
        assert item.get("readiness") in {"ready", "needs_config"}, f"{item['id']} readiness 非法"
        assert isinstance(item.get("requires"), list), f"{item['id']} 缺 requires 列表"
        if item["readiness"] == "ready":
            assert item["requires"] == [], f"{item['id']} 标 ready 却列了 requires"
        else:
            assert item["requires"], f"{item['id']} 标 needs_config 却没列 requires"
            assert all(isinstance(x, str) and len(x) > 4 for x in item["requires"])


def test_list_public_readiness_matches_checklist(admin: httpx.Client) -> None:
    items = admin.get("/api/providers").json()
    got = {item["id"]: item["readiness"] for item in items}
    assert got == EXPECTED_READINESS

    for pid, hints in EXPECTED_REQUIRES_HINTS.items():
        text = " ".join(_by_id(items, pid)["requires"])
        for hint in hints:
            assert hint in text, f"{pid} 的 requires 少了 {hint}：{text}"


def test_patch_enable_disable(admin: httpx.Client) -> None:
    # codebuddy 由 seed 默认 enabled=1
    before = admin.get("/api/providers")
    codebuddy = _by_id(before.json(), "codebuddy")
    was_enabled = bool(codebuddy.get("enabled"))
    was_opencode = bool(_by_id(before.json(), "opencode").get("enabled"))

    try:
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
    finally:
        admin.patch("/api/providers/codebuddy", json={"enabled": was_enabled})
        admin.patch("/api/providers/opencode", json={"enabled": was_opencode})


def test_patch_enable_reports_readiness_without_blocking(admin: httpx.Client) -> None:
    """允许先启用后配前置：PATCH 仍 200，但响应必须如实带 readiness / requires。"""
    before = _by_id(admin.get("/api/providers").json(), "codebuddy")
    was_enabled = bool(before.get("enabled"))

    try:
        r = admin.patch("/api/providers/codebuddy", json={"enabled": True})
        assert r.status_code == 200, f"patch enable: {r.status_code} {r.text}"
        body = r.json()
        assert body["enabled"] is True
        # 启用动作不改变就绪判据，也不能把缺前置说成已就绪
        assert body["readiness"] == "needs_config"
        assert body["requires"], "needs_config 必须在响应里列出缺项"

        listed = _by_id(admin.get("/api/providers").json(), "codebuddy")
        assert listed["readiness"] == body["readiness"]
        assert listed["requires"] == body["requires"]
    finally:
        admin.patch("/api/providers/codebuddy", json={"enabled": was_enabled})


def test_patch_base_url_takes_effect(
    admin: httpx.Client, fake_base: str, restore_codebuddy_base_url: None
) -> None:
    # codebuddy 初始 base_url 在环境变量里已经指向 fake_base；
    # 我们改成一个假的占位，再改回来。registry.update() 立即应用到进程内适配器。
    placeholder = "http://127.0.0.1:19999"
    changed = admin.patch("/api/providers/codebuddy", json={"base_url": placeholder})
    assert changed.status_code == 200, f"patch base_url: {changed.status_code} {changed.text}"
    assert changed.json()["base_url"] == placeholder

    # list 里也应该看到
    body = admin.get("/api/providers").json()
    assert _by_id(body, "codebuddy")["base_url"] == placeholder

    # 改回环境变量地址；用例前的库内状态由 restore_codebuddy_base_url 收尾还原
    restore = admin.patch("/api/providers/codebuddy", json={"base_url": fake_base})
    assert restore.status_code == 200
    assert restore.json()["base_url"] == fake_base
