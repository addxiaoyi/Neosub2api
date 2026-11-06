"""/v1 网关对外错误契约：provider 失败先归一，再按端点协议出外壳。

chat / responses 用 OpenAI 形状，messages 用 Anthropic 形状；
上游原始错误码/描述放在 error.upstream_code / error.upstream_message，便于排查；
状态码映射表见 docs/API-REFERENCE.md，改动这里必须同步文档与 tests/test_gateway_errors.py。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from .adapters.base import ProviderError, parse_vendor_error
from .api_utils import mask_text, redact_endpoints

logger = logging.getLogger("unified.gateway")

DEFAULT_RETRY_AFTER = 60
_MAX_DETAIL = 200

# 上游状态码 → 对外状态码：未列出的 4xx 归 400，其余 5xx 归 502
_UPSTREAM_TO_PUBLIC = {
    400: 400,
    401: 401,
    402: 402,
    403: 403,
    404: 404,
    408: 504,
    409: 400,
    413: 400,
    415: 400,
    422: 400,
    429: 429,
    501: 501,
    503: 503,
    504: 504,
}

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "insufficient_quota",
    403: "permission_error",
    404: "invalid_request_error",
    429: "rate_limit_error",
    501: "invalid_request_error",
}

_ERROR_CODES = {
    400: "upstream_bad_request",
    401: "upstream_auth_failed",
    402: "upstream_billing",
    403: "upstream_forbidden",
    404: "upstream_not_found",
    429: "upstream_rate_limited",
    501: "capability_unsupported",
    502: "upstream_unavailable",
    503: "provider_unavailable",
    504: "upstream_timeout",
}

PROTOCOL_OPENAI = "openai"
PROTOCOL_ANTHROPIC = "anthropic"

# 框架级错误（未匹配路由、方法不允许）的对外文案；细节留在服务端日志里
_FRAMEWORK_MESSAGES = {
    404: "接口不存在，请检查请求路径",
    405: "该接口不支持当前请求方法",
}
_FRAMEWORK_CODES = {
    404: "route_not_found",
    405: "method_not_allowed",
}

# Anthropic 协议的错误类型枚举（/v1/messages 用；没有 billing 对应，402 归 invalid_request_error）
_ANTHROPIC_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "invalid_request_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    501: "invalid_request_error",
    503: "overloaded_error",
    504: "timeout_error",
}

_EXTRA_KEYS = ("provider", "upstream_status", "upstream_code", "upstream_message")

# provider 级的对外错误改写规则（来自 provider options.error_rules）。
# 只改对外 status / code，upstream_status / upstream_code 永远保留上游原值。
_RULES: dict[str, list[dict[str, Any]]] = {}
_MATCH_FIELDS = ("upstream_status", "upstream_code", "message_contains")


def _clean_rule(item: dict[str, Any]) -> dict[str, Any] | None:
    rule: dict[str, Any] = {}
    raw_upstream = item.get("upstream_status")
    if raw_upstream is not None:
        try:
            rule["upstream_status"] = int(raw_upstream)
        except (TypeError, ValueError):
            return None
    upstream_code = str(item.get("upstream_code") or "").strip()
    if upstream_code:
        rule["upstream_code"] = upstream_code
    needle = str(item.get("message_contains") or "").strip()
    if needle:
        rule["message_contains"] = needle
    code = str(item.get("code") or "").strip()
    if code:
        rule["code"] = code
    raw_status = item.get("status")
    if raw_status is not None:
        try:
            status = int(raw_status)
        except (TypeError, ValueError):
            return None
        # 只允许改成错误状态码，防止规则把失败改写成 2xx
        if not 400 <= status <= 599:
            return None
        rule["status"] = status
    if "code" not in rule and "status" not in rule:
        return None
    if not any(key in rule for key in _MATCH_FIELDS):
        return None
    return rule


def configure_error_rules(provider: str, rules: Any) -> None:
    """注册某 provider 的错误改写规则；传空即清除。规则条目非法时整条忽略。"""
    if isinstance(rules, dict):
        rules = [rules]
    cleaned = [
        rule
        for rule in (_clean_rule(item) for item in (rules or []) if isinstance(item, dict))
        if rule
    ]
    if cleaned:
        _RULES[provider] = cleaned
    else:
        _RULES.pop(provider, None)


def _rule_matches(
    rule: dict[str, Any],
    *,
    upstream_status: int | None,
    upstream_code: str | None,
    message: str,
) -> bool:
    if "upstream_status" in rule and rule["upstream_status"] != upstream_status:
        return False
    if "upstream_code" in rule and rule["upstream_code"] != (upstream_code or ""):
        return False
    if "message_contains" in rule and rule["message_contains"] not in message:
        return False
    return True


def apply_error_rules(
    provider: str,
    *,
    status: int,
    code: str,
    upstream_status: int | None,
    upstream_code: str | None,
    message: str,
) -> tuple[int, str]:
    """按 provider 规则改写对外 (status, code)；未命中或未注册时原样返回。"""
    for rule in _RULES.get(provider, ()):
        if _rule_matches(
            rule,
            upstream_status=upstream_status,
            upstream_code=upstream_code,
            message=message,
        ):
            return int(rule.get("status") or status), str(rule.get("code") or code)
    return status, code


def public_status(upstream_status: int | None) -> int:
    """上游状态码映射成稳定的对外状态码。"""
    if upstream_status is None:
        return 502
    if upstream_status in _UPSTREAM_TO_PUBLIC:
        return _UPSTREAM_TO_PUBLIC[upstream_status]
    if 400 <= upstream_status < 500:
        return 400
    return 502


def error_type(status_code: int) -> str:
    return _ERROR_TYPES.get(status_code, "api_error")


def error_code(status_code: int) -> str:
    return _ERROR_CODES.get(status_code, "upstream_error")


def protocol_for_path(path: str) -> str:
    """按路径判协议族：只有 /v1/messages 说 Anthropic，其余 /v1 一律 OpenAI。

    404/405 这类框架级错误也必须遵守同一套外壳，否则 SDK 拿不到结构化 error。
    """
    return PROTOCOL_ANTHROPIC if path.startswith("/v1/messages") else PROTOCOL_OPENAI


def framework_payload(path: str, status_code: int, detail: Any = None) -> dict[str, Any]:
    """框架级错误（未匹配路由 404、方法不允许 405）按端点协议出壳。

    OpenAI 壳保留兼容字段 detail；Anthropic 壳只给顶层 type + error，两种壳不混用。
    """
    message = _FRAMEWORK_MESSAGES.get(status_code)
    if message is None:
        message = detail if isinstance(detail, str) and detail else "请求失败"
    code = _FRAMEWORK_CODES.get(status_code, "route_error")
    if protocol_for_path(path) == PROTOCOL_ANTHROPIC:
        inner_type = "not_found_error" if status_code == 404 else "invalid_request_error"
        return {"type": "error", "error": {"type": inner_type, "message": message, "code": code}}
    error = {"message": message, "type": "invalid_request_error", "param": None, "code": code}
    return {"error": error, "detail": message}


def parse_upstream_error(body: bytes) -> tuple[str | None, str | None]:
    """从上游错误体抽 (code, message)，交给适配器层的解析器后再统一掩码。"""
    code, message = parse_vendor_error((body or b"").decode("utf-8", "ignore"))
    clean_message = mask_text(message)[:_MAX_DETAIL] if message is not None else None
    clean_code = mask_text(code)[:_MAX_DETAIL] if code is not None else None
    return clean_code, clean_message


class GatewayError(HTTPException):
    """带结构化 error 的 HTTPException，按端点协议决定外层形状。

    内部字段（provider / upstream_*）不变；openai_payload 给 chat/responses，
    anthropic_payload 给 messages。detail 保留原人读文案（配额拒绝时是结构化 dict）。
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        type_: str = "api_error",
        code: str = "upstream_error",
        detail: Any = None,
        provider: str = "",
        upstream_status: int | None = None,
        upstream_code: str | None = None,
        upstream_message: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        # 对外错误的统一出口：先掩码凭据，再抹掉内部端点；原始信息只留服务端日志
        message = redact_endpoints(mask_text(message))
        detail = redact_endpoints(mask_text(detail)) if isinstance(detail, str) else detail
        headers = {"Retry-After": str(retry_after or DEFAULT_RETRY_AFTER)} if status_code == 429 else None
        super().__init__(status_code=status_code, detail=detail if detail is not None else message, headers=headers)
        self.error: dict[str, Any] = {
            "message": message,
            "type": type_,
            "param": None,
            "code": code,
        }
        if provider:
            self.error["provider"] = provider
        elif isinstance(detail, dict) and isinstance(detail.get("provider"), str) and detail["provider"]:
            # Anthropic 壳没有 detail，OpenAI 壳的 detail.provider 要对称下沉到 error.provider
            self.error["provider"] = detail["provider"]
        if upstream_status is not None:
            self.error["upstream_status"] = upstream_status
        if upstream_code is not None:
            self.error["upstream_code"] = upstream_code
        if upstream_message is not None:
            self.error["upstream_message"] = redact_endpoints(mask_text(upstream_message))

    def extra_fields(self) -> dict[str, Any]:
        return {key: self.error[key] for key in _EXTRA_KEYS if key in self.error}

    def openai_payload(self) -> dict[str, Any]:
        """OpenAI 协议（chat / responses）：error 对象 + 兼容旧前端的 detail。"""
        return {"error": self.error, "detail": self.detail}

    def anthropic_payload(self) -> dict[str, Any]:
        """Anthropic 协议（messages）：顶层 type=error，厂商信息作为 error 的附加键。

        code 与 OpenAI 壳对称：两种协议的同一错误必须能拿到同一个 error.code。
        """
        # provider 被停用是配置问题而非过载，重试无用，不能映射成 overloaded_error
        if self.status_code == 503 and self.error.get("code") == "provider_disabled":
            inner_type = "api_error"
        else:
            inner_type = _ANTHROPIC_TYPES.get(self.status_code, "api_error")
        inner = {
            "type": inner_type,
            "message": self.error["message"],
            "code": self.error["code"],
            **self.extra_fields(),
        }
        return {"type": "error", "error": inner}


def from_provider_error(exc: ProviderError, provider: str = "") -> GatewayError:
    """适配器抛出的失败：状态码走统一映射，文案与厂商原始信息原样带上。"""
    status = public_status(exc.status_code)
    # 上游原文（含内部地址）只进服务端日志，出壳前由 GatewayError 统一脱敏
    logger.warning(
        "上游失败 provider=%s upstream_status=%s message=%s upstream_message=%s",
        provider or "-", exc.status_code, mask_text(exc.message), mask_text(exc.upstream_message or ""),
    )
    message = redact_endpoints(mask_text(exc.message))[:_MAX_DETAIL]
    upstream_code = mask_text(exc.upstream_code)[:_MAX_DETAIL] if exc.upstream_code else None
    upstream_message = redact_endpoints(mask_text(exc.upstream_message))[:_MAX_DETAIL] if exc.upstream_message else None
    status, code = apply_error_rules(
        provider,
        status=status,
        code=error_code(status),
        upstream_status=exc.status_code,
        upstream_code=exc.upstream_code,
        message=exc.message,
    )
    return GatewayError(
        status,
        message,
        type_=error_type(status),
        code=code,
        provider=provider,
        upstream_status=exc.status_code,
        upstream_code=upstream_code,
        upstream_message=upstream_message,
    )


def from_upstream_reply(
    *,
    status_code: int,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    provider: str = "",
) -> GatewayError:
    """上游原样返回的失败：解析厂商 code/描述塞进子字段，状态码走统一映射。"""
    status = public_status(status_code)
    upstream_code, upstream_message = parse_upstream_error(body)
    # 原始厂商错误只进服务端日志，方便按 provider/状态码回溯；对外走脱敏后的文案
    logger.warning(
        "上游拒绝 provider=%s upstream_status=%s upstream_code=%s upstream_message=%s",
        provider or "-", status_code, upstream_code or "", upstream_message or "",
    )
    if upstream_message:
        message = f"上游返回 {status_code}：{upstream_message}"
    else:
        message = f"上游返回 {status_code}"
    status, code = apply_error_rules(
        provider,
        status=status,
        code=error_code(status),
        upstream_status=status_code,
        upstream_code=upstream_code,
        message=upstream_message or "",
    )
    retry_after = (headers or {}).get("retry-after") if status == 429 else None
    return GatewayError(
        status,
        message,
        type_=error_type(status),
        code=code,
        provider=provider,
        upstream_status=status_code,
        upstream_code=upstream_code,
        upstream_message=upstream_message,
        retry_after=retry_after,
    )
