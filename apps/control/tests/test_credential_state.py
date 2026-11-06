"""凭据状态判定：有效 / 缺失 / 已失效 / 未知。

覆盖三件事：
1. 分类函数只按证据走，重新粘贴后的旧证据不能继续算数；
2. 探活能把上游的 401/403 变成凭据失效证据；
3. /api/providers 的条目带凭据状态，失效时 requires 说清「重新粘贴」。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest

from app.adapters.base import ProviderAdapter, ProviderError, ProviderSpec, jwt_expired_reason
from app.config import load_settings
from app.db import Database
from app.registry import ProviderRegistry
from app.services.credential_state import (
    INVALIDATED,
    MISSING,
    SUPPORT_PROBE,
    SUPPORT_REQUEST,
    UNKNOWN,
    VALID,
    classify,
    is_jwt,
)
from app.services.provider_probe import probe

pytestmark = pytest.mark.providers

NOW = 1_700_000_000


def _jwt(payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


# ---- 分类函数 ----


def test_missing_when_no_credential() -> None:
    state = classify(has_credential=False)
    assert state.state == MISSING
    assert state.reason == "未配置凭据"


def test_unknown_without_any_evidence() -> None:
    state = classify(has_credential=True)
    assert state.state == UNKNOWN
    assert state.evidence == "none"


def test_invalidated_by_expired_jwt() -> None:
    token = _jwt({"exp": NOW - 60})
    state = classify(has_credential=True, expiry_problem=jwt_expired_reason(token))
    assert state.state == INVALIDATED
    assert state.evidence == "expiry"
    assert "重新粘贴" in state.reason


def test_expired_jwt_future_is_not_invalidated() -> None:
    assert jwt_expired_reason(_jwt({"exp": int(time.time()) + 3600})) == ""
    assert jwt_expired_reason("sk-plain-key") == ""
    assert is_jwt("sk-plain-key") is False


def test_invalidated_by_probe_auth_failure() -> None:
    state = classify(
        has_credential=True,
        probe={"status": "failed", "auth_failed": True, "upstream_status": 401, "checked_at": NOW},
    )
    assert state.state == INVALIDATED
    assert state.evidence == "probe"
    assert "重新粘贴" in state.reason
    # 错误体只说「上游返回 401」，不带内部地址
    assert "127.0.0.1" not in state.reason and "http" not in state.reason


def test_probe_failure_without_auth_is_unknown_not_invalidated() -> None:
    state = classify(
        has_credential=True,
        probe={"status": "failed", "auth_failed": False, "error": "连接被拒绝", "checked_at": NOW},
    )
    assert state.state == UNKNOWN
    assert "连接被拒绝" in state.reason


def test_connected_but_auth_rejected_is_invalidated() -> None:
    """管理端在线但明确拒了 token：连通性 ok 也不能盖掉鉴权失败。"""
    state = classify(
        has_credential=True,
        probe={"status": "ok", "auth_failed": True, "upstream_status": 401, "checked_at": NOW},
    )
    assert state.state == INVALIDATED
    assert state.evidence == "probe"


def test_valid_after_successful_probe() -> None:
    state = classify(has_credential=True, probe={"status": "ok", "checked_at": NOW})
    assert state.state == VALID
    assert state.evidence == "probe"


def test_invalidated_by_real_request_auth_failure() -> None:
    state = classify(has_credential=True, auth_failure_at=NOW)
    assert state.state == INVALIDATED
    assert state.evidence == "request"
    assert "重新粘贴" in state.reason


def test_newer_success_overrides_older_auth_failure() -> None:
    state = classify(
        has_credential=True,
        probe={"status": "ok", "checked_at": NOW},
        auth_failure_at=NOW - 10,
    )
    assert state.state == VALID


def test_repaste_resets_old_evidence() -> None:
    state = classify(
        has_credential=True,
        probe={"status": "failed", "auth_failed": True, "checked_at": NOW},
        credential_updated_at=NOW + 1,
    )
    assert state.state == UNKNOWN
    assert "更新过" in state.reason


# ---- 探活把 401/403 变成凭据证据 ----


class _AuthStub(ProviderAdapter):
    spec = ProviderSpec(
        id="stub_auth",
        name="Stub",
        vendor="test",
        license="MIT",
        runtime="test",
        status="ok",
        description="test",
        default_base_url="http://127.0.0.1:9",
    )

    async def health(self) -> dict[str, Any]:
        return {"connected": False, "auth": "missing", "error": "上游返回 401"}


class _RaiseStub(ProviderAdapter):
    spec = ProviderSpec(
        id="stub_raise",
        name="Stub",
        vendor="test",
        license="MIT",
        runtime="test",
        status="ok",
        description="test",
        default_base_url="http://127.0.0.1:9",
    )

    async def fetch_models(self) -> list[dict[str, Any]]:
        raise ProviderError(403, "上游拒绝了这把 key")


class _ServerErrorStub(_RaiseStub):
    spec = ProviderSpec(
        id="stub_500",
        name="Stub",
        vendor="test",
        license="MIT",
        runtime="test",
        status="ok",
        description="test",
        default_base_url="http://127.0.0.1:9",
    )

    async def fetch_models(self) -> list[dict[str, Any]]:
        raise ProviderError(500, "上游炸了")


class _ConnectedAuthStub(_AuthStub):
    spec = ProviderSpec(
        id="stub_connected_auth",
        name="Stub",
        vendor="test",
        license="MIT",
        runtime="test",
        status="ok",
        description="test",
        default_base_url="http://127.0.0.1:9",
    )

    async def health(self) -> dict[str, Any]:
        # codebuddy 的真实形态：服务在线，但管理端 401 拒了 wbt token
        return {"connected": True, "auth": "missing", "error": "管理端在线，但 token 无效"}


async def test_probe_marks_auth_failure_even_when_connected() -> None:
    outcome = await probe(_ConnectedAuthStub())
    assert outcome.status == "ok"
    assert outcome.auth_failed is True


async def test_probe_marks_auth_failure_from_health() -> None:
    outcome = await probe(_AuthStub())
    assert outcome.status == "failed"
    assert outcome.auth_failed is True


async def test_probe_marks_auth_failure_from_status_code() -> None:
    outcome = await probe(_RaiseStub())
    assert outcome.auth_failed is True
    assert outcome.upstream_status == 403


async def test_probe_does_not_call_500_an_auth_failure() -> None:
    outcome = await probe(_ServerErrorStub())
    assert outcome.auth_failed is False
    assert outcome.upstream_status == 500


# ---- 注册表：只读接口上的凭据状态 ----


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch, tmp_path) -> ProviderRegistry:
    monkeypatch.setenv("UNIFIED_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UNIFIED_DB_PATH", str(tmp_path / "unified.db"))
    monkeypatch.setenv("UNIFIED_ADMIN_PASSWORD", "test")
    settings = load_settings()
    return ProviderRegistry(Database(settings.db_path), settings)


def _item(registry: ProviderRegistry, pid: str) -> dict[str, Any]:
    return next(item for item in registry.list_public() if item["id"] == pid)


def _insert_probe(
    registry: ProviderRegistry,
    pid: str,
    *,
    status: str = "failed",
    auth_failed: bool = False,
    upstream_status: int | None = None,
    finished_at: int,
) -> None:
    conn = registry.db.connect()
    try:
        conn.execute(
            "INSERT INTO provider_health_checks("
            "provider, started_at, finished_at, status, latency_ms, error, upstream_status, auth_failed"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (pid, finished_at, finished_at, status, 0, "脱敏摘要", upstream_status,
             1 if auth_failed else 0),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_request_log(registry: ProviderRegistry, pid: str, status: int, ts: int) -> None:
    conn = registry.db.connect()
    try:
        conn.execute(
            "INSERT INTO request_logs(ts, provider, model, status) VALUES(?,?,?,?)",
            (ts, pid, "m", status),
        )
        conn.commit()
    finally:
        conn.close()


def test_list_public_invalidated_by_probe_requires_repaste(registry: ProviderRegistry) -> None:
    cred = registry.create_credential("muse", secret="sk-old")
    _insert_probe(registry, "muse", auth_failed=True, upstream_status=401,
                  finished_at=int(cred["updated_at"]) + 5)

    item = _item(registry, "muse")
    assert item["credential_state"] == INVALIDATED
    assert item["credential_state_evidence"] == "probe"
    assert item["credential_state_checked_at"] == int(cred["updated_at"]) + 5
    assert item["readiness"] == "needs_config"
    assert "凭据已失效，需要重新粘贴" in item["requires"][0]
    # 明文凭据不得出现在只读条目里
    assert "sk-old" not in json.dumps(item, ensure_ascii=False)


def test_list_public_invalidated_by_real_request_log(registry: ProviderRegistry) -> None:
    cred = registry.create_credential("trae", secret="sk-trae")
    _insert_request_log(registry, "trae", 401, int(cred["updated_at"]) + 5)

    item = _item(registry, "trae")
    assert item["credential_state"] == INVALIDATED
    assert item["credential_state_evidence"] == "request"
    assert item["credential_state_support"] == SUPPORT_REQUEST
    assert "重新粘贴" in item["requires"][0]
    assert item["credential_state_note"], "只能靠请求留痕时要写清原因"


def test_list_public_repaste_resets_to_unknown(registry: ProviderRegistry) -> None:
    _insert_probe(registry, "codar", auth_failed=True, upstream_status=401, finished_at=NOW)
    registry.create_credential("codar", secret="sk-new")

    item = _item(registry, "codar")
    assert item["credential_state"] == UNKNOWN
    assert "更新过" in item["credential_state_reason"]


def test_list_public_unknown_when_never_probed(registry: ProviderRegistry) -> None:
    registry.create_credential("opencode", secret="sk-opencode")
    item = _item(registry, "opencode")
    assert item["credential_state"] == UNKNOWN
    assert item["credential_state_support"] == SUPPORT_REQUEST
    assert item["credential_state_note"]


def test_list_public_valid_after_ok_probe(registry: ProviderRegistry) -> None:
    cred = registry.create_credential("zcode", secret="sk-zcode")
    _insert_probe(registry, "zcode", status="ok", finished_at=int(cred["updated_at"]) + 5)
    item = _item(registry, "zcode")
    assert item["credential_state"] == VALID


def test_list_public_marks_auth_probe_provider(registry: ProviderRegistry) -> None:
    item = _item(registry, "codebuddy")
    assert item["credential_state_support"] == SUPPORT_PROBE
    assert item["credential_state_note"] == ""


def test_list_public_missing_and_unavailable_note(registry: ProviderRegistry) -> None:
    item = _item(registry, "grok-register")
    assert item["credential_state"] == MISSING
    assert item["credential_state_support"] == "unavailable"
    assert "人工确认" in item["credential_state_note"]


def test_list_public_format_problem_counts_as_invalidated(registry: ProviderRegistry) -> None:
    registry.create_credential("codebuddy", secret="sk-not-a-wbt-token")
    item = _item(registry, "codebuddy")
    assert item["credential_state"] == INVALIDATED
    assert item["credential_state_evidence"] == "format"
    assert "重新粘贴" in item["requires"][0]


def test_expired_jwt_does_not_duplicate_requires(registry: ProviderRegistry) -> None:
    # codar 自己的 readiness 也会说「过期」，叠加后只该留一条重新粘贴指引
    registry.create_credential("codar", secret=_jwt({"exp": NOW - 60}))
    item = _item(registry, "codar")
    assert item["credential_state"] == INVALIDATED
    assert item["credential_state_evidence"] == "expiry"
    assert sum(1 for text in item["requires"] if "重新粘贴" in text) == 1
