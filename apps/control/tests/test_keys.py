"""密钥生命周期：创建（明文只返回一次）、列表（隐藏明文）、吊销/停用后网关立即 401。"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.keys


@pytest.fixture(scope="function")
def key(admin: httpx.Client) -> dict:
    """创建一把临时 key，函数级 fixture——每个用例拿到独立的 key。"""
    r = admin.post("/api/keys", json={"name": "t-lifecycle"})
    assert r.status_code == 200, f"create key: {r.status_code} {r.text}"
    return r.json()


def test_create_key_returns_plaintext_once(admin: httpx.Client) -> None:
    r = admin.post("/api/keys", json={"name": "t1"})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    assert "key" in body, "创建响应必须带明文 key"
    assert body["key"].startswith("u2a_"), f"前缀不对：{body['key']}"
    assert body["enabled"] is True
    # name / prefix / id 这些也得在
    assert body["name"] == "t1"
    assert body["prefix"].startswith("u2a_")
    assert isinstance(body["id"], int)


def test_list_key_hides_plaintext(admin: httpx.Client, key: dict) -> None:
    r = admin.get("/api/keys")
    assert r.status_code == 200
    items = r.json()
    assert isinstance(items, list)
    hit = next((it for it in items if it["id"] == key["id"]), None)
    assert hit is not None, "刚创建的 key 应该出现在列表里"
    # 列表条目里绝对不能出现明文 key 字段
    assert "key" not in hit
    # 但 prefix 可以有（仅前 10 字符，方便运维辨认）
    assert "prefix" in hit


def test_revoke_key_kills_401(admin: httpx.Client, anon: httpx.Client, key: dict) -> None:
    token = key["key"]
    # 吊销前——密钥存在，hash 匹配，enabled=1，应该过网关（这里不关心下游返回啥，只要不是 401）
    r_before = anon.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {token}"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r_before.status_code != 401, f"吊销前不该 401：{r_before.status_code} {r_before.text}"

    revoke = admin.delete(f"/api/keys/{key['id']}")
    assert revoke.status_code == 200, f"revoke: {revoke.status_code} {revoke.text}"

    r_after = anon.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {token}"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r_after.status_code == 401, f"吊销后应 401：{r_after.status_code} {r_after.text}"


def test_disabled_key_kills_401(admin: httpx.Client, anon: httpx.Client, key: dict) -> None:
    token = key["key"]
    # 停用：enabled=false
    patch = admin.patch(f"/api/keys/{key['id']}", json={"enabled": False})
    assert patch.status_code == 200, f"patch: {patch.status_code} {patch.text}"
    assert patch.json()["enabled"] is False

    r = anon.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {token}"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401, f"停用后应 401：{r.status_code} {r.text}"

    # 恢复启用
    restore = admin.patch(f"/api/keys/{key['id']}", json={"enabled": True})
    assert restore.status_code == 200
    assert restore.json()["enabled"] is True

    r_back = anon.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {token}"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r_back.status_code != 401, f"重开后不该 401：{r_back.status_code} {r_back.text}"


def test_anonymous_gate_effect(admin: httpx.Client, anon: httpx.Client) -> None:
    """匿名开关：POST /api/settings/gateway {allow_anonymous:true} → 不带 Authorization 应 200；
    再关掉 → 应 401。控制面进程每次请求读 DB settings 表，同 sqlite 文件直接生效。"""
    try:
        open_r = admin.post("/api/settings/gateway", json={"allow_anonymous": True})
        assert open_r.status_code == 200, f"开匿名: {open_r.status_code} {open_r.text}"

        hit = anon.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert hit.status_code != 401, f"开了匿名应放行：{hit.status_code} {hit.text}"

        close_r = admin.post("/api/settings/gateway", json={"allow_anonymous": False})
        assert close_r.status_code == 200

        blocked = anon.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert blocked.status_code == 401, f"关匿名应 401：{blocked.status_code} {blocked.text}"
    finally:
        # 无论如何都回写 false，不污染后续用例
        admin.post("/api/settings/gateway", json={"allow_anonymous": False})
