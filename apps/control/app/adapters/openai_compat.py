"""OpenAI 兼容上游的通用转发基类：muse2api / gptGrok2api / ZCode2api 共用。

可配置项（providers 页 PATCH 的 options）：
- proxy: 显式出口代理（如 http://127.0.0.1:7890），只作用于该 provider 的上游请求
- extra_headers: 注入到上游请求的自定义头（指纹头等），dict
- timeout: 上游超时秒数（默认 120）
"""

from typing import Any

from ..config import Settings
from .base import ProviderAdapter, ProviderError
from .client import UnifiedApiClient


class OpenAICompatAdapter(ProviderAdapter):
    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        super().__init__(base_url)

    def _extra_headers(self) -> dict[str, str] | None:
        headers = self.options.get("extra_headers")
        return headers if isinstance(headers, dict) else None

    def _client(self, timeout: float | None = None) -> UnifiedApiClient:
        return UnifiedApiClient(
            self.base_url,
            api_key=self.api_key,
            timeout=timeout or float(self.options.get("timeout") or 120.0),
            proxy=str(self.options.get("proxy") or ""),
            extra_headers=self._extra_headers(),
        )

    def _transform_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """转发前的请求改写钩子，子类按需覆盖（风控开关落这里）。"""
        return payload

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            data = await self._client(timeout=30.0).get_json("/v1/models")
        except ProviderError:
            return []
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict) and item.get("id")]

    async def health(self) -> dict[str, Any]:
        try:
            models = await self.list_models()
        except Exception as exc:
            return {"connected": False, "error": str(exc)}
        if models:
            return {"connected": True, "models": len(models)}
        # /v1/models 可达但为空：服务在线、账号池可能是空的，如实区分
        try:
            await self._client(timeout=15.0).get_json("/v1/models")
        except ProviderError as exc:
            return {"connected": False, "error": str(exc)}
        return {"connected": True, "models": 0, "note": "上游在线但模型列表为空"}

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        return await self._client().forward("/v1/chat/completions", self._transform_chat(payload), stream=stream)
