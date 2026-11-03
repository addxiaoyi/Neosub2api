"""codar 仓内直连实现：调用 Codar 私有 API，不再经过 codar2api sidecar。

边界（证据见 docs/providers/codar2api.md）：
- 自研：/v1/models（读私有目录 /v1/codar/models）、chat/completions、messages、
  responses 的转发与 SSE 透传；多 token 轮换与 401 续期。
- 不实现：微信扫码/短信登录、新 IP 验证、每日签到、领免费模型。这些依赖浏览器或
  人机交互，且随 Codar 客户端版本变化，继续留在 codar2api sidecar。
- 鉴权：服务商页 api_key 或 options.tokens 填 Codar 账号 JWT，按 Bearer 注入。
"""

import asyncio
import logging
import re
from typing import Any

from ..config import Settings
from .base import ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient
from .openai_compat import OpenAICompatAdapter

logger = logging.getLogger("unified.codar")

CATALOG_PATH = "/v1/codar/models"
REFRESH_PATH = "/v1/token/refresh"
DEFAULT_PAY_BASE = "https://api.kuaipaper.com"
MODEL_USER_AGENT = "codar-desktop/0.3.3"
ANTHROPIC_VERSION = "2023-06-01"
# 与上游 codar2api 的默认单请求最多尝试账号数保持一致
MAX_TOKEN_ATTEMPTS = 3
_TOKEN_SPLIT = re.compile(r"[\s,;]+")


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _clean_token(value: Any) -> str:
    token = str(value or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


class CodarAdapter(OpenAICompatAdapter):
    spec = ProviderSpec(
        id="codar",
        name="Codar (仓内直连)",
        vendor="Codar 私有 API（行为参考 AiXiaoc/codar2api）",
        license="MIT（仅参考上游公开行为，未复制源码）",
        runtime="仓内 Python 适配器（无 sidecar）",
        status="implemented",
        description="直连 Codar 私有 API：models、chat/completions、messages、responses；"
        "扫码登录、短信登录与签到仍需 codar2api sidecar。",
        default_base_url="https://api.claw.speedai.com",
        capabilities=stub_capabilities(chat=True, messages=True, models=True, responses=True),
        protocols=("openai.chat.completions", "anthropic.messages", "openai.responses"),
        risk_level="safe",
        risk_note="只直连 Codar 私有接口，参考 MIT 上游的公开协议行为；"
        "Codar 平台限本人名下账号，转售/共享风险由部署方承担。",
        readiness="needs_config",
        requires=(
            "Codar 账号 JWT（扫码/短信登录仍需 codar2api sidecar）",
        ),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        self._tokens: list[str] = []
        self._config_signature: tuple[str, ...] = ()
        self._cursor = 0
        self._lock = asyncio.Lock()
        super().__init__(settings, base_url)

    def _configured_tokens(self) -> list[str]:
        """按 api_key → options.token → options.tokens 收集 token，去重保序。"""
        raw = self.options.get("tokens")
        collected: list[str] = []
        if isinstance(raw, str):
            collected.extend(_TOKEN_SPLIT.split(raw))
        elif isinstance(raw, (list, tuple)):
            collected.extend(str(item) for item in raw)
        collected.append(self.api_key)
        collected.append(str(self.options.get("token") or ""))
        tokens: list[str] = []
        for item in collected:
            token = _clean_token(item)
            if token and token not in tokens:
                tokens.append(token)
        return tokens

    async def _rotation(self) -> list[int]:
        """按配置指纹同步 token 列表，返回本次请求的轮询顺序（索引）。"""
        configured = tuple(self._configured_tokens())
        async with self._lock:
            if configured != self._config_signature:
                self._tokens = list(configured)
                self._config_signature = configured
                self._cursor = 0
            if not self._tokens:
                return []
            count = len(self._tokens)
            start = self._cursor % count
            self._cursor = (start + 1) % count
            return [(start + offset) % count for offset in range(count)]

    async def _current_token(self, index: int) -> str:
        async with self._lock:
            return self._tokens[index]

    def _client(
        self,
        token: str,
        *,
        base_url: str = "",
        timeout: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> UnifiedApiClient:
        headers = {"user-agent": str(self.options.get("user_agent") or MODEL_USER_AGENT)}
        configured = self.options.get("extra_headers")
        if isinstance(configured, dict):
            headers.update({str(key): str(value) for key, value in configured.items()})
        if extra_headers:
            headers.update(extra_headers)
        return UnifiedApiClient(
            base_url or self.base_url,
            api_key=token,
            timeout=timeout or float(self.options.get("timeout") or 120.0),
            proxy=str(self.options.get("proxy") or ""),
            extra_headers=headers,
        )

    async def _refresh(self, index: int, token: str) -> str:
        """POST {pay_base}/v1/token/refresh，响应取 data.token 或 token。

        依据：上游 internal/gateway/upstream.go 的 renew() 用账号 JWT 调付费服务刷新。
        非标准接口，失败时静默降级到下一个 token。
        """
        if not _enabled(self.options.get("refresh", True)):
            return ""
        async with self._lock:
            if index >= len(self._tokens) or self._tokens[index] != token:
                return self._tokens[index] if index < len(self._tokens) else ""
        pay_base = str(self.options.get("pay_base") or DEFAULT_PAY_BASE).rstrip("/")
        try:
            data = await self._client(token, base_url=pay_base, timeout=30.0).post_json(REFRESH_PATH, {})
        except ProviderError as exc:
            logger.info("Codar token 续期失败：%s", exc.message)
            return ""
        payload = data.get("data") if isinstance(data, dict) else None
        fresh = _clean_token(payload.get("token") if isinstance(payload, dict) else "")
        if not fresh and isinstance(data, dict):
            fresh = _clean_token(data.get("token"))
        if not fresh or fresh == token:
            return ""
        async with self._lock:
            if index >= len(self._tokens) or self._tokens[index] != token:
                return self._tokens[index] if index < len(self._tokens) else ""
            self._tokens[index] = fresh
        return fresh

    async def _forward_with_tokens(
        self,
        path: str,
        payload: dict[str, Any] | None,
        *,
        stream: bool,
        extra_headers: dict[str, str] | None = None,
    ):
        order = await self._rotation()
        if not order:
            raise ProviderError(
                401,
                "Codar 未配置账号 token：请在服务商页 api_key 或 options.tokens 填入 Codar JWT",
            )
        attempts = 0
        while order and attempts < MAX_TOKEN_ATTEMPTS:
            index = order.pop(0)
            attempts += 1
            token = await self._current_token(index)
            reply = await self._client(token, extra_headers=extra_headers).forward(
                path, payload, stream=stream
            )
            if reply.status_code != 401:
                return reply
            await reply.aclose()
            fresh = await self._refresh(index, token)
            if not fresh:
                continue
            attempts += 1
            if attempts > MAX_TOKEN_ATTEMPTS:
                break
            reply = await self._client(fresh, extra_headers=extra_headers).forward(
                path, payload, stream=stream
            )
            if reply.status_code != 401:
                return reply
            await reply.aclose()
        raise ProviderError(401, "Codar token 全部失效，且续期未成功")

    async def _json_with_tokens(self, path: str) -> dict[str, Any]:
        order = await self._rotation()
        if not order:
            raise ProviderError(
                401,
                "Codar 未配置账号 token：请在服务商页 api_key 或 options.tokens 填入 Codar JWT",
            )
        attempts = 0
        last_error: ProviderError | None = None
        while order and attempts < MAX_TOKEN_ATTEMPTS:
            index = order.pop(0)
            attempts += 1
            token = await self._current_token(index)
            try:
                return await self._client(token, timeout=30.0).request_json("GET", path)
            except ProviderError as exc:
                last_error = exc
                if exc.status_code != 401:
                    raise
                fresh = await self._refresh(index, token)
                if not fresh:
                    continue
                attempts += 1
                if attempts > MAX_TOKEN_ATTEMPTS:
                    break
                try:
                    return await self._client(fresh, timeout=30.0).request_json("GET", path)
                except ProviderError as inner:
                    last_error = inner
                    if inner.status_code != 401:
                        raise
        raise last_error or ProviderError(401, "Codar token 全部失效，且续期未成功")

    async def fetch_models(self) -> list[dict[str, Any]]:
        data = await self._json_with_tokens(CATALOG_PATH)
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [
            {"id": str(item["id"]), "object": "model", "owned_by": "codar"}
            for item in items
            if isinstance(item, dict) and item.get("id")
        ]

    async def health(self) -> dict[str, Any]:
        if not self._configured_tokens():
            return {"connected": False, "error": "未配置 Codar 账号 token（服务商页 api_key 或 options.tokens）"}
        try:
            data = await self._json_with_tokens(CATALOG_PATH)
        except ProviderError as exc:
            return {"connected": False, "error": exc.message}
        items = data.get("data") if isinstance(data, dict) else None
        count = len(items) if isinstance(items, list) else 0
        if count:
            return {"connected": True, "models": count}
        return {"connected": True, "models": 0, "note": "上游在线但模型目录为空"}

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        return await self._forward_with_tokens(
            "/v1/chat/completions", self._transform_chat(payload), stream=stream
        )

    async def messages(self, payload: dict[str, Any], *, stream: bool):
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        beta = str(self.options.get("anthropic_beta") or "").strip()
        if beta:
            headers["anthropic-beta"] = beta
        return await self._forward_with_tokens(
            "/v1/messages", payload, stream=stream, extra_headers=headers
        )

    async def responses(self, payload: dict[str, Any], *, stream: bool):
        return await self._forward_with_tokens("/v1/responses", payload, stream=stream)


def build(settings: Settings, base_url: str = "") -> CodarAdapter:
    return CodarAdapter(settings, base_url)
