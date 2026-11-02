"""OpenCode Zen/Go 网关适配器（clean-room 重写）。

按 docs/providers/opencode2api.md 提炼的协议契约独立实现：模型发现、Chat 与
Anthropic Messages 转发、SSE 直通、可重试状态码换 key。不依赖 opencode2api 进程。
"""

import asyncio
from typing import Any

import httpx

from ..config import Settings
from .base import ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient

# 请求级错误不重试，换 key/出口也改变不了结果
REQUEST_ERRORS = {400, 413, 422}
RETRYABLE = {401, 403, 408, 429, 500, 502, 503, 504}
# 网络层错误与可重试状态码走同一套换 key 退避；连接失败/超时不是密钥问题也是出口问题
NETWORK_ERRORS = (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ConnectTimeout)


class OpenCodeAdapter(ProviderAdapter):
    spec = ProviderSpec(
        id="opencode",
        name="OpenCode 2API",
        vendor="jasonxu114514/opencode2api（协议参考）",
        license="本仓自研（MIT）",
        runtime="主仓内置（直连 Zen/Go）",
        status="implemented",
        description="按公开协议契约重写的 Zen/Go 客户端：/v1/models 与 chat/messages 转发，SSE 直通。",
        default_base_url="https://opencode.ai/zen",
        capabilities=stub_capabilities(chat=True, messages=True, models=True),
        protocols=("openai.chat.completions", "anthropic.messages"),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        super().__init__(base_url)

    def _tier_prefix(self) -> str:
        tier = str(self.options.get("tier") or "zen").strip().lower()
        # base_url 已经写到 /zen/go 时不再重复拼 /go
        if tier == "go" and not self.base_url.rstrip("/").endswith("/go"):
            return "/go"
        return ""

    def _keys(self) -> list[str]:
        raw = [self.api_key, *(self.options.get("extra_keys") or [])]
        keys: list[str] = []
        for item in raw:
            key = str(item or "").strip()
            if key and key not in keys:
                keys.append(key)
        return keys

    def _attempts(self, key_count: int) -> int:
        configured = int(self.options.get("max_attempts") or 3)
        return max(1, min(configured, 10, max(key_count, 1) * 2))

    def _client(self, key: str, *, anthropic: bool, timeout: float | None = None) -> UnifiedApiClient:
        headers = dict(self.options.get("extra_headers") or {}) if isinstance(self.options.get("extra_headers"), dict) else {}
        if anthropic:
            headers.setdefault("anthropic-version", "2023-06-01")
            if key:
                headers.setdefault("x-api-key", key)
        return UnifiedApiClient(
            self.base_url,
            api_key=key,
            timeout=timeout or float(self.options.get("timeout") or 300.0),
            proxy=str(self.options.get("proxy") or ""),
            extra_headers=headers,
        )

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None, *, stream: bool, anthropic: bool):
        keys = self._keys()
        if not keys:
            raise ProviderError(503, "OpenCode 未配置上游密钥（服务商页填写 api_key 或 options.extra_keys）")
        attempts = self._attempts(len(keys))
        last: Any = None
        # 记录最后一次连接错误的原文，重试耗尽后如实抛给调用方，不伪造空响应
        network_error: ProviderError | None = None
        for attempt in range(attempts):
            key = keys[attempt % len(keys)]
            client = self._client(key, anthropic=anthropic)
            try:
                if method == "GET":
                    return await client.get_json(path)
                reply = await client.forward(path, payload or {}, stream=stream)
            except ProviderError as exc:
                last = exc
                network_error = exc if exc.status_code >= 500 else network_error
                if exc.status_code in REQUEST_ERRORS:
                    raise
            except NETWORK_ERRORS as exc:
                last = network_error = ProviderError(502, f"上游连接失败（{self.base_url}）：{exc!r}")
            else:
                if reply.status_code < 400 or reply.status_code not in RETRYABLE:
                    return reply
                last = reply
                if attempt == attempts - 1:
                    return reply
                await reply.aclose()
            # 500ms / 1s / 2s 退避，避免把上游限流打成雪崩
            await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
        if network_error is not None:
            raise ProviderError(
                502,
                f"{network_error.message}；已轮换 {attempts} 次密钥仍未连通",
            )
        if isinstance(last, ProviderError):
            raise last
        return last

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            data = await self._request("GET", f"{self._tier_prefix()}/v1/models", None, stream=False, anthropic=False)
        except ProviderError:
            return []
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict) and item.get("id")]

    async def health(self) -> dict[str, Any]:
        try:
            data = await self._request("GET", f"{self._tier_prefix()}/v1/models", None, stream=False, anthropic=False)
        except ProviderError as exc:
            return {"connected": False, "error": exc.message}
        items = data.get("data") if isinstance(data, dict) else []
        count = len(items) if isinstance(items, list) else 0
        result: dict[str, Any] = {"connected": True, "models": count, "tier": str(self.options.get("tier") or "zen")}
        if count == 0:
            result["note"] = "上游在线但模型列表为空（可能是密钥无可用模型）"
        return result

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        return await self._request(
            "POST", f"{self._tier_prefix()}/v1/chat/completions", payload, stream=stream, anthropic=False
        )

    async def messages(self, payload: dict[str, Any], *, stream: bool):
        return await self._request(
            "POST", f"{self._tier_prefix()}/v1/messages", payload, stream=stream, anthropic=True
        )


def build(settings: Settings, base_url: str = "") -> OpenCodeAdapter:
    return OpenCodeAdapter(settings, base_url or settings.opencode_upstream_base)
