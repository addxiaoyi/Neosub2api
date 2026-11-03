"""Muse 适配器（仓内自研，不再依赖 muse2api sidecar）。

依据 docs/providers/muse2api.md 的实测记录：muse2api 本身是把 muse 网页账号的浏览器
操作（Chromium + CDP，engine.py）反代成 OpenAI 兼容接口；网页端只有 `hatch_sess`
Cookie 的会话管理（GET /api/session、POST /api/hatch/vm/wake），**没有 HTTP 对话端点**
（对话走 DOM 或 Noise-XX WebSocket）。所以本仓改为直连 Muse 官方 OpenAI 兼容入口
（Bearer API key），逐 chunk 直通 SSE；也仍可把 base_url 指回旧 sidecar 保持兼容。
本实现不复制上游源码。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from ..config import Settings
from .base import ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient
from .stream_util import Reply

CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"
DEFAULT_MODELS = ("muse-spark", "muse-image", "muse-video")

RETRYABLE = {401, 403, 408, 429, 500, 502, 503, 504}
REQUEST_ERRORS = {400, 413, 422}


class MuseAdapter(ProviderAdapter):
    """Muse 官方 OpenAI 兼容端点客户端。

    选项键名沿用代理版：
    - timeout: 单次请求超时（秒），默认 300。
    - cookie: 旧网页会话 Cookie（如 `hatch_sess=...`），仅当 base_url 指回网页网关时才有意义。
    - extra_keys / max_attempts: 多 key 轮换。
    - chat_path / models_path / health_path: 端点覆盖（私有网关或联调用）。
    """

    spec = ProviderSpec(
        id="muse",
        name="Muse2API",
        vendor="czg86389-hub/muse2api（协议参考）",
        license="本仓自研（MIT）",
        runtime="主仓内置（直连 Muse 官方 OpenAI 兼容入口）",
        status="implemented",
        description="直连 Muse 官方 OpenAI 兼容 API，chat 流式逐帧直通，支持多 key 轮换。",
        default_base_url="https://api.meta.ai",
        capabilities=stub_capabilities(chat=True, models=True),
        protocols=("openai.chat.completions",),
        readiness="needs_config",
        requires=(
            "官方 OpenAI 兼容入口的 api_key（网页 hatch_sess 账号没有 HTTP 对话端点）",
        ),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        super().__init__(base_url)

    # ---------- 凭证 ----------

    def _keys(self) -> list[str]:
        raw = [self.api_key, *(self.options.get("extra_keys") or [])]
        keys: list[str] = []
        for item in raw:
            token = str(item or "").strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            if token and token not in keys:
                keys.append(token)
        return keys

    def _attempts(self, key_count: int) -> int:
        configured = int(self.options.get("max_attempts") or 3)
        return max(1, min(configured, 10, max(key_count, 1) * 2))

    def _headers(self, token: str, *, stream: bool) -> dict[str, str]:
        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
        }
        cookie = str(self.options.get("cookie") or "").strip()
        if cookie:
            headers["cookie"] = cookie
        extra = self.options.get("extra_headers")
        if isinstance(extra, dict):
            headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    def _client(self, token: str, *, stream: bool, timeout: float | None = None) -> UnifiedApiClient:
        return UnifiedApiClient(
            self.base_url,
            timeout=timeout or float(self.options.get("timeout") or 300.0),
            proxy=str(self.options.get("proxy") or ""),
            extra_headers=self._headers(token, stream=stream),
        )

    def _path(self, key: str, default: str) -> str:
        value = str(self.options.get(key) or "").strip()
        return value or default

    # ---------- 错误分类 ----------

    @staticmethod
    def _classify(status: int, text: str) -> ProviderError:
        if status == 401:
            err = ProviderError(401, f"Muse 凭证失效：{text[:160]}")
        elif status == 403:
            err = ProviderError(403, f"Muse 拒绝访问（账号可能受限）：{text[:160]}")
        elif status == 429:
            err = ProviderError(429, f"Muse 上游限流：{text[:160]}")
        elif status == 404:
            err = ProviderError(404, f"Muse 上游端点不存在：{text[:160]}")
        elif status in REQUEST_ERRORS:
            err = ProviderError(status, f"Muse 请求被拒：{text[:160]}")
            err.retryable = False  # type: ignore[attr-defined]
        else:
            err = ProviderError(status, f"Muse 上游错误（{status}）：{text[:160]}")
        return err.with_vendor_detail(text)

    @staticmethod
    async def _read_error(reply) -> str:
        if reply.response is not None:
            try:
                raw = await reply.response.aread()
            finally:
                await reply.aclose()
            return raw.decode("utf-8", "ignore")
        return (reply.body or b"").decode("utf-8", "ignore")

    async def _send(self, path: str, payload: dict[str, Any] | None, *, stream: bool):
        keys = self._keys()
        if not keys:
            raise ProviderError(503, "Muse 未配置凭证（服务商页 api_key 或 options.extra_keys）")
        attempts = self._attempts(len(keys))
        last: ProviderError | None = None
        for attempt in range(attempts):
            token = keys[attempt % len(keys)]
            client = self._client(token, stream=stream)
            try:
                reply = await client.forward(
                    path,
                    payload,
                    stream=stream,
                    method="GET" if payload is None else "POST",
                    raw=b"" if payload is None else None,
                )
            except ProviderError as exc:
                last = exc
                if exc.status_code in REQUEST_ERRORS:
                    raise
                await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
                continue
            except httpx.HTTPError as exc:
                last = ProviderError(502, f"Muse 上游连接失败（{self.base_url}）：{exc!r}")
                await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
                continue
            if reply.status_code < 400:
                return reply
            err = self._classify(reply.status_code, await self._read_error(reply))
            last = err
            if not getattr(err, "retryable", err.status_code in RETRYABLE):
                raise err
            await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
        raise last or ProviderError(502, "Muse 上游不可用")

    # ---------- 请求体 ----------

    def _body(self, payload: dict[str, Any], model: str, *, stream: bool) -> dict[str, Any]:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            prompt = str(payload.get("prompt") or "").strip()
            if not prompt:
                raise ProviderError(400, "Muse 请求缺少 messages 或 prompt")
            messages = [{"role": "user", "content": prompt}]
        body = {**payload, "model": model, "stream": stream, "messages": messages}
        return body

    # ---------- 对外接口 ----------

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        model = str(payload.get("model") or DEFAULT_MODELS[0])
        body = self._body(payload, model, stream=stream)
        reply = await self._send(self._path("chat_path", CHAT_PATH), body, stream=stream)
        if stream:
            # 上游已是 OpenAI 帧：逐 chunk 直通，不重排、不拼接
            return Reply(
                reply.status_code,
                stream=reply.iter_bytes(),
                content_type=reply.headers.get("content-type", "text/event-stream"),
            )
        return Reply(
            reply.status_code,
            body=reply.body,
            content_type=reply.headers.get("content-type", "application/json"),
        )

    def declared_models(self) -> list[dict[str, Any]]:
        extra = self.options.get("models")
        ids = list(DEFAULT_MODELS)
        for item in extra if isinstance(extra, list) else []:
            value = str(item or "").strip()
            if value and value not in ids:
                ids.append(value)
        return [{"id": model, "object": "model", "owned_by": "muse"} for model in ids]

    async def fetch_models(self) -> list[dict[str, Any]]:
        reply = await self._send(self._path("models_path", MODELS_PATH), None, stream=False)
        try:
            data = json.loads(reply.body or b"{}")
        except json.JSONDecodeError as exc:
            raise ProviderError(502, "Muse 模型目录返回非 JSON") from exc
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict) and item.get("id")]

    async def list_models(self) -> list[dict[str, Any]]:
        # 旧接口保持声明口径；聚合目录走 fetch_models，失败才回落这里
        return self.declared_models()

    async def health(self) -> dict[str, Any]:
        keys = self._keys()
        if not keys:
            return {"connected": False, "error": "未配置 Muse 凭证"}
        try:
            reply = await self._send(self._path("health_path", MODELS_PATH), None, stream=False)
        except ProviderError as exc:
            return {"connected": False, "error": exc.message}
        try:
            data = json.loads(reply.body or b"{}")
        except json.JSONDecodeError:
            return {"connected": True, "note": "凭证可用，但模型接口返回非 JSON"}
        items = data.get("data") if isinstance(data.get("data"), list) else None
        return {"connected": True, "models": len(items) if items else len(DEFAULT_MODELS)}


def build(settings: Settings, base_url: str = "") -> MuseAdapter:
    return MuseAdapter(settings, base_url)
