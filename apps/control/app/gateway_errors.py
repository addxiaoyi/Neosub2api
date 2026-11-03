"""/v1 网关对外错误契约：provider 失败先归一，再按端点协议出外壳。

chat / responses 用 OpenAI 形状，messages 用 Anthropic 形状；
上游原始错误码/描述放在 error.upstream_code / error.upstream_message，便于排查；
状态码映射表见 docs/API-REFERENCE.md，改动这里必须同步文档与 tests/test_gateway_errors.py。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from .adapters.base import ProviderError, parse_vendor_error
from .api_utils import mask_text

DEFAULT_RETRY_AFTER = 60
_MAX_DETAIL = 200

# 上游状态码 → 对外状态码：未列出的 4xx 归 400，其余 5xx 归 502
_UPSTREAM_TO_PUBLIC = {
    400: 400,
    401: 401,
    402: 402,
    403: 401,
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
    404: "invalid_request_error",
    429: "rate_limit_error",
    501: "invalid_request_error",
}

_ERROR_CODES = {
    400: "upstream_bad_request",
    401: "upstream_auth_failed",
    402: "upstream_billing",
    404: "upstream_not_found",
    429: "upstream_rate_limited",
    501: "capability_unsupported",
    502: "upstream_unavailable",
    503: "provider_unavailable",
    504: "upstream_timeout",
}

# Anthropic 协议的错误类型枚举（/v1/messages 用；没有 billing 对应，402 归 invalid_request_error）
_ANTHROPIC_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "invalid_request_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
    501: "invalid_request_error",
    503: "overloaded_error",
    504: "timeout_error",
}

_EXTRA_KEYS = ("provider", "upstream_status", "upstream_code", "upstream_message")


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
        if upstream_status is not None:
            self.error["upstream_status"] = upstream_status
        if upstream_code is not None:
            self.error["upstream_code"] = upstream_code
        if upstream_message is not None:
            self.error["upstream_message"] = upstream_message

    def extra_fields(self) -> dict[str, Any]:
        return {key: self.error[key] for key in _EXTRA_KEYS if key in self.error}

    def openai_payload(self) -> dict[str, Any]:
        """OpenAI 协议（chat / responses）：error 对象 + 兼容旧前端的 detail。"""
        return {"error": self.error, "detail": self.detail}

    def anthropic_payload(self) -> dict[str, Any]:
        """Anthropic 协议（messages）：顶层 type=error，厂商信息作为 error 的附加键。"""
        inner = {
            "type": _ANTHROPIC_TYPES.get(self.status_code, "api_error"),
            "message": self.error["message"],
            **self.extra_fields(),
        }
        return {"type": "error", "error": inner}


def from_provider_error(exc: ProviderError, provider: str = "") -> GatewayError:
    """适配器抛出的失败：状态码走统一映射，文案与厂商原始信息原样带上。"""
    status = public_status(exc.status_code)
    message = mask_text(exc.message)[:_MAX_DETAIL]
    upstream_code = mask_text(exc.upstream_code)[:_MAX_DETAIL] if exc.upstream_code else None
    upstream_message = mask_text(exc.upstream_message)[:_MAX_DETAIL] if exc.upstream_message else None
    return GatewayError(
        status,
        message,
        type_=error_type(status),
        code=error_code(status),
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
    if upstream_message:
        message = f"上游返回 {status_code}：{upstream_message}"
    else:
        message = f"上游返回 {status_code}"
    retry_after = (headers or {}).get("retry-after") if status == 429 else None
    return GatewayError(
        status,
        message,
        type_=error_type(status),
        code=error_code(status),
        provider=provider,
        upstream_status=status_code,
        upstream_code=upstream_code,
        upstream_message=upstream_message,
        retry_after=retry_after,
    )
