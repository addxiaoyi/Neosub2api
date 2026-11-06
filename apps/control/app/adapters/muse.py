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
import logging
from typing import Any

import httpx

from ..api_utils import exc_summary, safe_url
from ..config import Settings
from ..services.rotation import CredentialPool, sticky_key_from_payload
from .base import ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient
from .stream_util import Reply

logger = logging.getLogger("unified.upstream")

CHAT_PATH = "/v1/chat/completions"
RESPONSES_PATH = "/v1/responses"
CLIENT_UA = "muse2api/1.6.6"
MODELS_PATH = "/v1/models"
IMAGES_GENERATIONS_PATH = "/v1/images/generations"
IMAGES_EDITS_PATH = "/v1/images/edits"
IMAGES_TASKS_PATH = "/v1/images/tasks"
VIDEOS_PATH = "/v1/videos"
MEDIA_PATH = "/v1/media"
DEFAULT_MODELS = ("muse-spark", "muse-image", "muse-video")
# 上游建视频任务的默认等待是 600s，客户端超时必须更长（muse2api-protocols.md §5）
VIDEO_TIMEOUT = 660.0

# 与池分档同集：401/403 鉴权、402 余额、408 超时、429 限流、5xx 服务端，全部换号并登记冷却
POOL_COOLDOWN_STATUSES = {401, 402, 403, 408, 429, 500, 502, 503, 504}
RETRYABLE = POOL_COOLDOWN_STATUSES
REQUEST_ERRORS = {400, 413, 422}


class _TrackedReply:
    """流式响应包装：读到正常收尾才回写健康度，中途断流只解绑粘性。

    拿到 200 响应头就记成功，会把随后上游断开的事件掩盖掉：该冷却的账号不会被
    冷却，后续请求还会继续撞同一把坏号（与 trae 流式口径一致）。
    """

    def __init__(self, inner, on_done, on_break) -> None:
        self.status_code = inner.status_code
        self.headers = inner.headers
        self.body = inner.body
        self.response = inner.response
        self._inner = inner
        self._on_done = on_done
        self._on_break = on_break

    async def iter_bytes(self):
        completed = False
        try:
            async for chunk in self._inner.iter_bytes():
                yield chunk
            completed = True
        except Exception:  # noqa: BLE001 - 断流/连接错误按出口问题处理，不判死账号
            await self._on_break()
            raise
        finally:
            if completed:
                await self._on_done()

    async def aclose(self) -> None:
        await self._inner.aclose()


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
        description="直连 Muse 官方 OpenAI 兼容 API，chat 与 responses 流式逐帧直通，"
        "images / videos / media 透传，支持多 key 轮换。",
        default_base_url="https://api.meta.ai",
        capabilities=stub_capabilities(
            chat=True,
            models=True,
            responses=True,
            images=True,
            videos=True,
            media=True,
        ),
        protocols=("openai.chat.completions", "openai.responses"),
        user_agent=CLIENT_UA,
        readiness="needs_config",
        requires=(
            "官方 OpenAI 兼容入口的 api_key：在 api.meta.ai 创建 API key 后导入；"
            "网页 hatch_sess 账号没有 HTTP 对话端点，不能用于本仓转发",
        ),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        # 账号池：健康度、分档冷却、粘性与权重都在 services/rotation.py 的统一口径里
        self._pool = CredentialPool()
        super().__init__(base_url)

    def credential_problem(self, credential_type: str, text: str) -> str:
        if credential_type not in ("api_key", "bearer"):
            return (
                f"Muse2API 只接受官方入口的 api_key；{credential_type} 类型不能用于 HTTP 对话"
                "（网页 hatch_sess Cookie 只能管网页会话）"
            )
        raw = text.strip()
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        if not raw:
            return "Muse2API 需要 api.meta.ai 的 api_key（网页 hatch_sess 会话没有 HTTP 对话端点）"
        if "hatch_sess=" in raw:
            return (
                "这是网页会话 Cookie（hatch_sess），Muse 没有对应的 HTTP 对话端点；"
                "请到 api.meta.ai 创建 api_key 后导入"
            )
        return ""

    def readiness_now(
        self,
        *,
        has_credential: bool,
        credential_problem: str,
        last_probe: dict[str, Any] | None,
        credential: str = "",
        options: dict[str, Any] | None = None,
    ) -> tuple[str, tuple[str, ...]]:
        if credential_problem:
            return "needs_config", (credential_problem,)
        if has_credential:
            return "ready", ()
        return "needs_config", self.spec.requires

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
            "user-agent": self.client_user_agent() or CLIENT_UA,
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

    async def _note_failure(self, key: str, status: int, keys: list[str]) -> bool:
        """登记分档冷却；没有别的可用账号时不冷却，避免把唯一账号关进冷却后请求全 429。"""
        others = [item for item in keys if item != key]
        if others and await self._pool.available(others):
            await self._pool.report_failure(key, status)
            return True
        return False

    def _tracked(self, inner, key: str, sticky: str):
        async def done() -> None:
            await self._pool.report_success(key)
            if sticky:
                await self._pool.bind_sticky(sticky, key)

        async def broke() -> None:
            await self._pool.report_network_error(key)

        return _TrackedReply(inner, done, broke)

    async def _send(
        self,
        path: str,
        payload: dict[str, Any] | None,
        *,
        stream: bool,
        timeout: float | None = None,
    ):
        keys = self._keys()
        if not keys:
            raise ProviderError(503, "Muse 未配置凭证（服务商页 api_key 或 options.extra_keys）")
        # 权重每次请求前同步：options 改动后无需重建适配器
        self._pool.set_weights(keys, self.options.get("key_weights"))
        sticky = sticky_key_from_payload(payload)
        attempts = self._attempts(len(keys))
        last: ProviderError | None = None
        for attempt in range(attempts):
            pick = await self._pool.acquire(keys, sticky_key=sticky)
            if pick is None:
                raise ProviderError(429, f"Muse {len(keys)} 个账号全部处于冷却中，请稍后重试")
            token = pick.key
            if len(keys) > 1:
                logger.info("Muse 选号 cred=%s 规则=%s %s", pick.cred_id, pick.rule, pick.note or "-")
            client = self._client(token, stream=stream, timeout=timeout)
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
                # client 抛出的 ProviderError 是连接层失败：只解绑粘性，不进冷却
                await self._pool.report_network_error(token)
            except httpx.HTTPError as exc:
                last = ProviderError(502, f"Muse 上游连接失败（{safe_url(self.base_url)}）：{exc_summary(exc)}")
                await self._pool.report_network_error(token)
            else:
                if reply.status_code < 400:
                    if stream:
                        # 流式成功只在读完后再回写，避免把中途断流记成健康
                        return self._tracked(reply, token, sticky)
                    await self._pool.report_success(token)
                    if sticky:
                        await self._pool.bind_sticky(sticky, token)
                    return reply
                err = self._classify(reply.status_code, await self._read_error(reply))
                last = err
                if not getattr(err, "retryable", err.status_code in RETRYABLE):
                    raise err
                # 冷却按事实登记：即便这是最后一次尝试，健康度也要如实反映坏号
                cooled = await self._note_failure(token, err.status_code, keys)
                if attempt == attempts - 1:
                    raise err
                # 没有替代账号时不换号也不冷却，直接抛这次错误
                if not cooled:
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

    async def responses(self, payload: dict[str, Any], *, stream: bool):
        # 官方入口确有 /v1/responses：promptfoo Meta Model API 文档列明 api.meta.ai/v1 同时提供 /responses、/chat/completions、/messages（2026-11-04 核实）
        model = str(payload.get("model") or DEFAULT_MODELS[0])
        body = {**payload, "model": model, "stream": stream}
        reply = await self._send(RESPONSES_PATH, body, stream=stream)
        if stream:
            # 上游已是 OpenAI response.* 事件：逐 chunk 直通，不重排、不拼接
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

    async def image_generation(self, payload: dict[str, Any], *, stream: bool):
        return await self._send(IMAGES_GENERATIONS_PATH, payload, stream=stream)

    async def image_edit(self, payload: dict[str, Any], *, stream: bool):
        return await self._send(IMAGES_EDITS_PATH, payload, stream=stream)

    async def image_task(self, task_id: str):
        return await self._send(f"{IMAGES_TASKS_PATH}/{task_id}", None, stream=False)

    async def video_create(self, payload: dict[str, Any], *, stream: bool):
        return await self._send(VIDEOS_PATH, payload, stream=stream, timeout=VIDEO_TIMEOUT)

    async def video_task(self, task_id: str):
        return await self._send(f"{VIDEOS_PATH}/{task_id}", None, stream=False)

    async def media(self, name: str):
        return await self._send(f"{MEDIA_PATH}/{name}", None, stream=False)

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
        rotation = await self._pool.snapshot(keys)
        if not keys:
            return {
                "connected": False,
                "error": "未配置 Muse 凭证：请在 api.meta.ai 创建 api_key 后导入",
                "rotation": rotation,
                "cooling": rotation.get("cooling", 0),
            }
        try:
            reply = await self._send(self._path("health_path", MODELS_PATH), None, stream=False)
        except ProviderError as exc:
            return {
                "connected": False,
                "error": exc.message,
                "rotation": rotation,
                "cooling": rotation.get("cooling", 0),
            }
        try:
            data = json.loads(reply.body or b"{}")
        except json.JSONDecodeError:
            return {
                "connected": True,
                "note": "凭证可用，但模型接口返回非 JSON",
                "rotation": rotation,
                "cooling": rotation.get("cooling", 0),
            }
        items = data.get("data") if isinstance(data.get("data"), list) else None
        return {
            "connected": True,
            "models": len(items) if items else len(DEFAULT_MODELS),
            "rotation": rotation,
            "cooling": rotation.get("cooling", 0),
        }


def build(settings: Settings, base_url: str = "") -> MuseAdapter:
    return MuseAdapter(settings, base_url)
