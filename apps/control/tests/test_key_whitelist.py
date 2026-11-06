"""2-D6：密钥级模型/IP 白名单（纯函数口径 + 网关执行）。"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.routers.v1 import _ip_allowed, _model_allowed

MODEL = "quota-m"


@pytest.fixture(scope="module")
def wl_key(admin: httpx.Client):
    r = admin.post("/api/keys", json={"name": f"wl-{uuid.uuid4().hex[:8]}"})
    assert r.status_code == 200, r.text
    key_id, token = int(r.json()["id"]), r.json()["key"]
    yield key_id, token
    admin.delete(f"/api/keys/{key_id}")


def _chat(client: httpx.Client, token: str, model: str = MODEL) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {token}", "x-provider": "codebuddy"},
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


def test_model_allow_predicate() -> None:
    assert _model_allowed("", MODEL) is True
    assert _model_allowed("[]", MODEL) is True
    assert _model_allowed('["quota-m"]', MODEL) is True
    assert _model_allowed('["other"]', MODEL) is False
    assert _model_allowed('["quo*"]', MODEL) is True
    assert _model_allowed('["QUOTA-M"]', MODEL) is True
    # 没有模型维度的端点不适用白名单
    assert _model_allowed('["other"]', "") is True
    # 配置坏成非 JSON：拒绝，不静默放行
    assert _model_allowed("{bad", MODEL) is False


def test_ip_allow_predicate() -> None:
    assert _ip_allowed("", "10.1.2.3") is True
    assert _ip_allowed("[]", "10.1.2.3") is True
    assert _ip_allowed('["10.0.0.0/8"]', "10.1.2.3") is True
    assert _ip_allowed('["10.0.0.0/8"]', "192.168.0.1") is False
    assert _ip_allowed('["10.1.2.3"]', "10.1.2.3") is True
    assert _ip_allowed('["10.0.0.0/8"]', "10.1.2.3, 10.9.9.9") is False  # 不解析 XFF 串
    assert _ip_allowed('["10.0.0.0/8"]', "testclient") is False  # 取不到来源地址就拒绝
    assert _ip_allowed("{bad", "10.1.2.3") is False


def test_patch_rejects_bad_ip(admin: httpx.Client, wl_key) -> None:
    key_id, _ = wl_key
    bad = admin.patch(f"/api/keys/{key_id}", json={"ip_allow": ["10.0.0.0/99"]})
    assert bad.status_code == 400, bad.text


def test_model_whitelist_enforced(admin: httpx.Client, anon: httpx.Client, wl_key) -> None:
    key_id, token = wl_key
    patched = admin.patch(f"/api/keys/{key_id}", json={"model_allow": [MODEL]})
    assert patched.status_code == 200, patched.text
    assert patched.json()["model_allow"] == [MODEL]

    allowed = _chat(anon, token, MODEL)
    assert allowed.status_code == 200, allowed.text

    denied = _chat(anon, token, "quota-other")
    assert denied.status_code == 403, denied.text
    error = denied.json()["error"]
    assert error["code"] == "model_not_allowed"
    assert error["type"] == "permission_error"


def test_ip_whitelist_enforced(admin: httpx.Client, anon: httpx.Client, wl_key) -> None:
    key_id, token = wl_key
    patched = admin.patch(f"/api/keys/{key_id}", json={"ip_allow": ["203.0.113.0/24"]})
    assert patched.status_code == 200, patched.text

    denied = _chat(anon, token, MODEL)
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "ip_not_allowed"

    cleared = admin.patch(f"/api/keys/{key_id}", json={"ip_allow": []})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["ip_allow"] == []
    allowed = _chat(anon, token, MODEL)
    assert allowed.status_code == 200, allowed.text
