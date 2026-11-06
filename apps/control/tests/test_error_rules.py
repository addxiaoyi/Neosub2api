"""2-D3：sub2api 错误透传规则表。

规则只改对外 status / code；upstream_status / upstream_code 必须原样留档。
规则来自 provider 配置的 options.error_rules，由 sub2api.configure 注册。
"""

from __future__ import annotations

import pytest

from app import gateway_errors
from app.adapters import sub2api
from app.adapters.base import ProviderError
from app.gateway_errors import (
    configure_error_rules,
    from_provider_error,
    from_upstream_reply,
)
from tests.test_vendor_adapters import _settings as adapter_settings


@pytest.fixture(autouse=True)
def _clear_rules():
    yield
    configure_error_rules("sub2api", [])


def _body(code: str, message: str) -> bytes:
    return ('{"error": {"code": "%s", "message": "%s"}}' % (code, message)).encode()


# ------------------------------- 规则匹配 -------------------------------


def test_status_and_code_are_rewritten_but_upstream_kept():
    configure_error_rules("sub2api", [
        {"upstream_status": 503, "code": "provider_overloaded", "status": 502},
    ])
    error = from_upstream_reply(
        status_code=503,
        body=_body("server_busy", "all accounts busy"),
        provider="sub2api",
    )
    assert error.status_code == 502
    assert error.error["code"] == "provider_overloaded"
    assert error.error["upstream_status"] == 503
    assert error.error["upstream_code"] == "server_busy"
    assert error.error["upstream_message"] == "all accounts busy"


def test_rule_matches_vendor_code():
    configure_error_rules("sub2api", [
        {"upstream_code": "model_not_found", "code": "model_unsupported"},
    ])
    error = from_upstream_reply(
        status_code=400,
        body=_body("model_not_found", "unknown model gpt-9"),
        provider="sub2api",
    )
    assert error.status_code == 400
    assert error.error["code"] == "model_unsupported"
    assert error.error["upstream_status"] == 400
    assert error.error["upstream_code"] == "model_not_found"


def test_rule_matches_message_substring():
    configure_error_rules("sub2api", [
        {"message_contains": "insufficient quota", "code": "quota_exhausted", "status": 402},
    ])
    error = from_provider_error(
        ProviderError(429, "sub2api 上游限流：insufficient quota for group"),
        provider="sub2api",
    )
    assert error.status_code == 402
    assert error.error["code"] == "quota_exhausted"
    assert error.error["upstream_status"] == 429


def test_unmatched_and_other_providers_keep_defaults():
    configure_error_rules("sub2api", [{"upstream_status": 503, "code": "only_503"}])
    error = from_upstream_reply(status_code=429, body=b"{}", provider="sub2api")
    assert error.status_code == 429
    assert error.error["code"] == "upstream_rate_limited"
    other = from_upstream_reply(status_code=503, body=b"{}", provider="zcode")
    assert other.error["code"] == "provider_unavailable"


def test_first_matching_rule_wins():
    configure_error_rules("sub2api", [
        {"upstream_status": 503, "code": "first"},
        {"upstream_status": 503, "code": "second"},
    ])
    error = from_upstream_reply(status_code=503, body=b"", provider="sub2api")
    assert error.error["code"] == "first"


def test_invalid_rules_are_ignored():
    configure_error_rules("sub2api", [
        {"upstream_status": 500, "status": 200},           # 不能改写成功
        {"upstream_status": 500},                          # 没有改写目标
        {"code": "x"},                                     # 没有匹配条件
        {"upstream_status": "oops", "code": "x"},          # 字段类型不对
        "not-a-dict",
    ])
    error = from_upstream_reply(status_code=500, body=b"", provider="sub2api")
    assert error.status_code == 502
    assert error.error["code"] == "upstream_unavailable"


def test_rewritten_status_drives_error_type_and_retry_after():
    configure_error_rules("sub2api", [
        {"upstream_status": 500, "code": "upstream_rate_limited", "status": 429},
    ])
    error = from_upstream_reply(
        status_code=500, body=b"", headers={"retry-after": "7"}, provider="sub2api"
    )
    assert error.status_code == 429
    assert error.error["type"] == "rate_limit_error"
    assert error.headers["Retry-After"] == "7"
    assert error.error["upstream_status"] == 500


def test_anthropic_shell_carries_rewritten_code():
    configure_error_rules("sub2api", [{"upstream_status": 503, "code": "queue_full"}])
    error = from_upstream_reply(status_code=503, body=b"", provider="sub2api")
    payload = error.anthropic_payload()
    assert payload["type"] == "error"
    assert payload["error"]["code"] == "queue_full"
    assert payload["error"]["upstream_status"] == 503


# ------------------------------- 配置来源 -------------------------------


def test_sub2api_configure_registers_rules(tmp_path):
    adapter = sub2api.build(adapter_settings(tmp_path))
    adapter.configure(
        api_key="tok-s",
        options={"error_rules": [{"upstream_status": 503, "code": "pool_overloaded"}]},
    )
    error = from_upstream_reply(status_code=503, body=b"", provider="sub2api")
    assert error.error["code"] == "pool_overloaded"

    adapter.configure(options={"error_rules": []})
    error = from_upstream_reply(status_code=503, body=b"", provider="sub2api")
    assert error.error["code"] == "provider_unavailable", "清空配置要撤掉旧规则"


def test_rule_payload_survives_gateway_error_shape():
    configure_error_rules("sub2api", [{"upstream_status": 401, "code": "key_revoked"}])
    error = from_provider_error(ProviderError(401, "sub2api 凭证失效"), provider="sub2api")
    payload = error.openai_payload()
    assert payload["error"]["code"] == "key_revoked"
    assert payload["error"]["upstream_status"] == 401
    assert gateway_errors.public_status(401) == 401
