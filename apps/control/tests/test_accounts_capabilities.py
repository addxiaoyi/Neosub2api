"""统一账号页的能力矩阵。

这些用例锁的是「界面能画什么」的唯一事实来源：能力必须按适配器**实际覆写**的方法
计算，而不是 spec.capabilities 的声明位。历史上 trae_sidecar 声明了 oauth/credits，
主仓却没实现，前端照着声明画按钮，点下去只能 501。
"""

from __future__ import annotations

import httpx


def _rows(admin: httpx.Client) -> dict[str, dict]:
    r = admin.get("/api/accounts/capabilities")
    assert r.status_code == 200, r.text
    return {p["id"]: p for p in r.json()["providers"]}


def test_capabilities_cover_every_registered_provider(admin):
    rows = _rows(admin)
    # 14 家适配器都要出现，否则前端筛选会凭空少几家
    assert {
        "codebuddy",
        "muse",
        "grok-register",
        "gptgrok",
        "zcode",
        "opencode",
        "sub2api",
        "trae",
        "trae_sidecar",
        "deepseek_sidecar",
        "qoder",
        "qoder_sidecar",
        "codar",
        "codar_sidecar",
    } <= set(rows)


def test_codebuddy_is_the_only_unified_account_provider(admin):
    rows = _rows(admin)
    cb = rows["codebuddy"]
    assert cb["managed"] == "unified"
    for op in ("list", "oauth", "checkin", "credits", "test", "refresh", "disabled", "delete"):
        assert cb["ops"][op] is True, f"codebuddy 应支持 {op}"


def test_sidecar_does_not_advertise_unimplemented_account_ops(admin):
    rows = _rows(admin)
    ts = rows["trae_sidecar"]
    # 声明位有 oauth/credits，但主仓没实现 —— 必须如实为 False
    assert ts["ops"]["oauth"] is False
    assert ts["ops"]["credits"] is False
    assert ts["ops"]["checkin"] is False
    assert ts["ops"]["list"] is False
    assert ts["managed"] == "sidecar"


def test_sub2api_accounts_are_managed_by_quota_domain(admin):
    sub = _rows(admin)["sub2api"]
    assert sub["managed"] == "quota"
    assert sub["ops"]["list"] is False


def test_write_on_unsupported_provider_returns_readable_400(admin):
    r = admin.post("/api/accounts/refresh-credits", params={"provider": "muse"})
    assert r.status_code == 400
    assert "不支持" in r.json()["detail"]

    r2 = admin.post("/api/accounts/checkin-all", params={"provider": "trae_sidecar"})
    assert r2.status_code == 400
    assert "不支持" in r2.json()["detail"]


def test_write_on_unknown_provider_returns_404(admin):
    r = admin.post("/api/accounts/refresh-credits", params={"provider": "no-such-provider"})
    assert r.status_code == 404


def test_restart_on_unmanaged_provider_is_rejected_with_message(admin):
    r = admin.post("/api/accounts/restart", params={"provider": "muse"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["message"]
