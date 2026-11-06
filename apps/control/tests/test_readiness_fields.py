"""readiness 随前置字段变化：填对即 ready、缺项点名、删掉回 needs_config。

走真实 HTTP：PATCH 写库 -> GET /api/providers 让 registry 重新推导。
"""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

pytestmark = pytest.mark.providers

TOUCHED = ("zcode", "gptgrok", "codar", "opencode")


def _jwt(exp: int | None = None) -> str:
    def seg(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    claims = {"sub": "demo", "uid": "u-1"}
    if exp is not None:
        claims["exp"] = exp
    return f"{seg({'alg': 'none', 'typ': 'JWT'})}.{seg(claims)}.sig"


def _row(admin: httpx.Client, pid: str) -> dict:
    for item in admin.get("/api/providers").json():
        if item["id"] == pid:
            return item
    raise AssertionError(f"provider 列表里没有 {pid}")


def _patch(admin: httpx.Client, pid: str, body: dict) -> None:
    res = admin.patch(f"/api/providers/{pid}", json=body)
    assert res.status_code == 200, f"{pid}: {res.status_code} {res.text}"


def _clear(admin: httpx.Client, pid: str, option_keys: tuple[str, ...]) -> None:
    _patch(admin, pid, {"api_key": "", "options": {key: "" for key in option_keys}})


def _assert_missing(row: dict, keyword: str) -> None:
    assert row["readiness"] == "needs_config", row
    assert any(keyword in hint for hint in row["requires"]), row["requires"]


@pytest.fixture(scope="module", autouse=True)
def _clean_credentials(admin: httpx.Client):
    """会话级库里可能留着别的用例导入的凭证；先清空这四家，免得「填前」被误判成 ready。"""
    for pid in TOUCHED:
        for cred in admin.get("/api/provider-credentials", params={"provider": pid}).json():
            admin.delete(f"/api/provider-credentials/{cred['id']}")
    yield


def test_zcode_official_system_toggles_readiness(admin: httpx.Client) -> None:
    _clear(admin, "zcode", ("official_system", "official_system_prompt"))
    _assert_missing(_row(admin, "zcode"), "official_system")
    assert any("api_key" in hint for hint in _row(admin, "zcode")["requires"])

    _patch(
        admin,
        "zcode",
        {"api_key": "zcode-demo-jwt", "options": {"official_system": "official body"}},
    )
    ready = _row(admin, "zcode")
    assert ready["readiness"] == "ready", ready
    assert ready["requires"] == []

    _patch(admin, "zcode", {"api_key": "", "options": {"official_system": ""}})
    _assert_missing(_row(admin, "zcode"), "official_system")


def test_zcode_official_system_block_format(admin: httpx.Client) -> None:
    _patch(admin, "zcode", {"api_key": "zcode-demo-jwt", "options": {"official_system": "   "}})
    _assert_missing(_row(admin, "zcode"), "official_system")

    blocks = [{"type": "text", "text": "  "}, {"type": "text", "text": "real body"}]
    _patch(admin, "zcode", {"options": {"official_system": blocks}})
    assert _row(admin, "zcode")["readiness"] == "ready"

    _clear(admin, "zcode", ("official_system", "official_system_prompt"))


def test_gptgrok_sentinel_toggles_readiness(admin: httpx.Client) -> None:
    _clear(admin, "gptgrok", ("sentinel_token", "channel"))
    _assert_missing(_row(admin, "gptgrok"), "sentinel_token")
    assert any("api_key" in hint for hint in _row(admin, "gptgrok")["requires"])

    _patch(
        admin,
        "gptgrok",
        {"api_key": "gpt-demo", "options": {"sentinel_token": "token-value"}},
    )
    assert _row(admin, "gptgrok")["readiness"] == "ready"

    _patch(admin, "gptgrok", {"api_key": "", "options": {"sentinel_token": ""}})
    _assert_missing(_row(admin, "gptgrok"), "sentinel_token")


def test_gptgrok_grok_channel_needs_no_sentinel(admin: httpx.Client) -> None:
    _clear(admin, "gptgrok", ("sentinel_token", "channel"))
    _patch(admin, "gptgrok", {"api_key": "grok-demo", "options": {"channel": "grok"}})
    row = _row(admin, "gptgrok")
    assert row["readiness"] == "ready", row
    assert row["requires"] == []
    _clear(admin, "gptgrok", ("sentinel_token", "channel"))


def test_codar_jwt_toggles_readiness(admin: httpx.Client) -> None:
    _clear(admin, "codar", ("token", "tokens"))
    _assert_missing(_row(admin, "codar"), "JWT")

    token = _jwt(exp=int(time.time()) + 3600)
    _patch(admin, "codar", {"api_key": token})
    assert _row(admin, "codar")["readiness"] == "ready"

    _patch(admin, "codar", {"api_key": ""})
    _assert_missing(_row(admin, "codar"), "JWT")


def test_codar_expired_or_broken_jwt_stays_needs_config(admin: httpx.Client) -> None:
    _patch(admin, "codar", {"api_key": _jwt(exp=int(time.time()) - 60)})
    row = _row(admin, "codar")
    assert row["readiness"] == "needs_config", row
    assert any("过期" in hint for hint in row["requires"]), row["requires"]

    _patch(admin, "codar", {"api_key": "not-a-jwt"})
    _assert_missing(_row(admin, "codar"), "JWT")

    _clear(admin, "codar", ("token", "tokens"))


def test_opencode_api_key_toggles_readiness(admin: httpx.Client) -> None:
    _clear(admin, "opencode", ("extra_keys",))
    _assert_missing(_row(admin, "opencode"), "api_key")

    _patch(admin, "opencode", {"api_key": "sk-opencode"})
    assert _row(admin, "opencode")["readiness"] == "ready"

    _patch(admin, "opencode", {"api_key": ""})
    _assert_missing(_row(admin, "opencode"), "api_key")


def test_trae_and_qoder_ready_once_token_filled(admin: httpx.Client) -> None:
    _clear(admin, "trae", ())
    _patch(admin, "trae", {"api_key": _jwt(exp=int(time.time()) + 3600)})
    assert _row(admin, "trae")["readiness"] == "ready"
    _patch(admin, "trae", {"api_key": "broken-jwt"})
    _assert_missing(_row(admin, "trae"), "JWT")
    _patch(admin, "trae", {"api_key": ""})

    _clear(admin, "qoder", ())
    assert _row(admin, "qoder")["readiness"] == "needs_config"
    _patch(admin, "qoder", {"api_key": "qoder-access-token"})
    assert _row(admin, "qoder")["readiness"] == "ready"
    _patch(admin, "qoder", {"api_key": ""})
