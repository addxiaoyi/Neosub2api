"""GPT/Grok 适配器（仓内自研，直连 ChatGPT Web 与 Grok Web）。

依据 docs/providers/gptgrok2api.md 的实测记录：ChatGPT 走
`POST /backend-api/conversation`（Bearer JWT + OAI 指纹头，SSE `data:` 帧、`[DONE]` 收尾）；
Grok 走 `POST /rest/app-chat/conversations/new`（`sso` Cookie，逐行 JSON，
`result.response.token` 增量、`isSoftStop`/`finalMetadata` 收尾）。
本实现不复制上游源码；两通道都把上游帧增量翻译成 OpenAI chunk。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from ..config import Settings
from .base import ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient
from .key_pool import COOLDOWN_STATUSES, DEFAULT_COOLDOWN_SEC, KeyPool
from .stream_util import (
    DONE, Reply, content_text, iter_json_lines, openai_chunk, openai_completion,
)

CHATGPT_PATH = "/backend-api/conversation"
CHATGPT_HEALTH = "/backend-api/me"
GROK_PATH = "/rest/app-chat/conversations/new"
GROK_HEALTH = "/rest/models"
BASE_URLS = {"chatgpt": "https://chatgpt.com", "grok": "https://grok.com"}

CHATGPT_MODELS = ("gpt-5", "gpt-5-1", "gpt-5-2", "gpt-5-3", "gpt-5-mini", "gpt-5-3-mini")
GROK_MODELS = ("grok-4.20-fast", "grok-4.20-reasoning", "grok-4.3-fast", "grok-4.3-beta")
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
RETRYABLE = {401, 403, 408, 429, 500, 502, 503, 504}
REQUEST_ERRORS = {400, 413, 422}
FINISHED = {"finished_successfully", "finished_details", "finished"}


def _stable_id(token: str, salt: str) -> str:
    digest = hashlib.md5(f"{salt}:{token}".encode()).hexdigest()
    return f"{digest[0:8]}-{digest[8:12]}-4{digest[13:16]}-{digest[16:20]}-{digest[20:32]}"


class GptGrokAdapter(ProviderAdapter):
    """ChatGPT / Grok 双通道客户端，按模型名前缀分流（grok-* → Grok）。"""

    spec = ProviderSpec(
        id="gptgrok",
        name="GPT/Grok 2API",
        vendor="addxiaoyi/gptGrok2api（协议参考）",
        license="本仓自研（MIT）",
        runtime="主仓内置（直连 ChatGPT / Grok Web）",
        status="implemented",
        description="直连 ChatGPT Web 与 Grok Web 的 OpenAI chat 客户端，支持多凭证轮换与指纹头开关。",
        default_base_url="https://chatgpt.com",
        capabilities=stub_capabilities(chat=True, models=True),
        protocols=("openai.chat.completions",),
        readiness="needs_config",
        requires=(
            "sentinel_token（ChatGPT sentinel token，本地无法生成；缺失时上游可能 403）",
        ),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        # 轮换状态属于本实例；同一进程内多个适配器互不影响
        self._keys_pool = KeyPool()
        super().__init__(base_url)

    # ---------- 通道与凭证 ----------

    def _cooldown_sec(self) -> float:
        return float(self.options.get("key_cooldown") or DEFAULT_COOLDOWN_SEC)

    def _all_keys_error(self, channel: str, keys: list[str], last: ProviderError | None) -> ProviderError:
        reason = last.message if last else "全部凭证处于冷却中，请稍后重试"
        return ProviderError(
            last.status_code if last else 429,
            f"GPT/Grok {channel} 通道 {len(keys)} 把凭证全部不可用（{reason}）",
            upstream_code=last.upstream_code if last else None,
            upstream_message=last.upstream_message if last else None,
        )

    def _channel(self, model: str) -> str:
        configured = str(self.options.get("channel") or "").strip().lower()
        if configured in BASE_URLS:
            return configured
        return "grok" if str(model).lower().startswith("grok") else "chatgpt"

    def _base(self, channel: str) -> str:
        override = str(self.options.get(f"{channel}_base") or "").strip()
        if override:
            return override.rstrip("/")
        default = BASE_URLS[channel]
        # 用户显式改过 base_url 时按同一入口处理；否则两通道回各自官方域
        if self.base_url and self.base_url.rstrip("/") != self.spec.default_base_url.rstrip("/"):
            return self.base_url.rstrip("/")
        return default

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

    def _proxy(self) -> str:
        return str(self.options.get("proxy") or "")

    def _timeout(self) -> float:
        return float(self.options.get("timeout") or 180.0)

    def _apply_fingerprint(self, headers: dict[str, str], token: str) -> dict[str, str]:
        headers.setdefault("user-agent", str(self.options.get("user_agent") or DEFAULT_UA))
        headers.setdefault("oai-device-id", _stable_id(token, "device"))
        headers.setdefault("oai-session-id", _stable_id(token, "session"))
        for option, header in (
            ("oai_client_version", "oai-client-version"),
            ("oai_client_build_number", "oai-client-build-number"),
        ):
            value = str(self.options.get(option) or "").strip()
            if value:
                headers[header] = value
        sentinel = str(self.options.get("sentinel_token") or "").strip()
        if sentinel:
            headers["openai-sentinel-chat-requirements-token"] = sentinel
        extra = self.options.get("extra_headers")
        if isinstance(extra, dict):
            headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    def _chatgpt_headers(self, token: str, *, stream: bool) -> dict[str, str]:
        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
            "x-openai-target-path": CHATGPT_PATH,
            "x-openai-target-route": CHATGPT_PATH,
        }
        # 指纹开关沿用代理版语义：fingerprint 关闭时不注入任何自定义/指纹头
        if self.options.get("fingerprint"):
            headers = self._apply_fingerprint(headers, token)
        return headers

    def _grok_headers(self, token: str) -> dict[str, str]:
        cookie = token if ("sso=" in token or ";" in token) else f"sso={token}; sso-rw={token}"
        headers = {
            "cookie": cookie,
            "content-type": "application/json",
            "accept": "application/json, text/plain",
            "user-agent": str(self.options.get("user_agent") or DEFAULT_UA),
        }
        if self.options.get("fingerprint"):
            extra = self.options.get("extra_headers")
            if isinstance(extra, dict):
                headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    # ---------- 请求体 ----------

    @staticmethod
    def _turn(payload: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
        system: list[str] = []
        turns: list[dict[str, str]] = []
        for item in payload.get("messages") or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            text = content_text(item.get("content"))
            if role in ("system", "developer"):
                if text:
                    system.append(text)
                continue
            if role in ("tool", "function"):
                role = "user"
                text = f"[tool result] {text}" if text else "[tool result]"
            turns.append({"role": role if role in ("user", "assistant") else "user", "content": text})
        if not turns:
            raise ProviderError(400, "GPT/Grok 请求缺少 messages")
        if system and turns[0]["role"] == "user":
            turns[0]["content"] = "[system] " + "\n".join(system) + "\n\n" + turns[0]["content"]
        return "\n\n".join(f"[{turn['role']}]: {turn['content']}" for turn in turns), turns

    def _chatgpt_body(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        _, turns = self._turn(payload)
        return {
            "action": "next",
            "messages": [
                {
                    "id": str(uuid.uuid4()),
                    "author": {"role": turn["role"]},
                    "content": {"content_type": "text", "parts": [turn["content"]]},
                }
                for turn in turns
            ],
            "model": model,
            "parent_message_id": "client-created-root",
            "conversation_mode": {"kind": "primary_assistant"},
            "timezone_offset_min": -480,
            "history_and_training_disabled": False,
        }

    def _grok_body(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        message, _ = self._turn(payload)
        haystack = model.lower()
        if "fast" in haystack:
            mode = "fast"
        elif "expert" in haystack:
            mode = "expert"
        elif "heavy" in haystack:
            mode = "heavy"
        else:
            mode = "auto"
        return {
            "message": message,
            "modelName": model,
            "modeId": mode,
            "temporary": False,
            "disableMemory": False,
            "disableSearch": False,
            "disableTextFollowUps": False,
            "enableImageGeneration": False,
            "returnImageBytes": False,
            "returnRawGrokInXaiRequest": False,
            "enableImageStreaming": False,
            "imageGenerationCount": 0,
            "forceSideBySide": False,
            "isAsyncChat": False,
            "sendFinalMetadata": True,
            "isReasoning": "reasoning" in haystack,
            "deviceEnvInfo": {},
        }

    # ---------- 错误分类 ----------

    @staticmethod
    def _classify(status: int, text: str, channel: str) -> ProviderError:
        if status == 401:
            err = ProviderError(401, f"{channel} 凭证失效或未登录：{text[:160]}")
        elif status == 403:
            err = ProviderError(403, f"{channel} 拒绝访问（可能触发风控/需要 sentinel）：{text[:160]}")
        elif status == 429:
            err = ProviderError(429, f"{channel} 上游限流：{text[:160]}")
        elif status == 404:
            err = ProviderError(404, f"{channel} 上游端点不存在：{text[:160]}")
        elif status >= 500:
            err = ProviderError(status, f"{channel} 上游服务异常（{status}）：{text[:160]}")
        elif status in REQUEST_ERRORS:
            err = ProviderError(status, f"{channel} 请求被拒：{text[:160]}")
            err.retryable = False  # type: ignore[attr-defined]
        else:
            err = ProviderError(status, f"{channel} 上游错误（{status}）：{text[:160]}")
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

    # ---------- 上游连接（换 key） ----------

    async def _open(self, channel: str, body: dict[str, Any]):
        keys = self._keys()
        if not keys:
            raise ProviderError(503, f"{channel} 未配置凭证（服务商页 api_key 或 options.extra_keys）")
        path = CHATGPT_PATH if channel == "chatgpt" else GROK_PATH
        attempts = self._attempts(len(keys))
        last: ProviderError | None = None
        for attempt in range(attempts):
            token = await self._keys_pool.pick(keys)
            if token is None:
                raise self._all_keys_error(channel, keys, last)
            headers = self._chatgpt_headers(token, stream=True) if channel == "chatgpt" else self._grok_headers(token)
            client = UnifiedApiClient(
                self._base(channel),
                timeout=self._timeout(),
                proxy=self._proxy(),
                extra_headers=headers,
            )
            try:
                reply = await client.forward(path, body, stream=True)
            except ProviderError as exc:
                last = exc
                if exc.status_code in REQUEST_ERRORS:
                    raise
                await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
                continue
            except httpx.HTTPError as exc:
                last = ProviderError(502, f"{channel} 上游连接失败：{exc!r}")
                await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
                continue
            if reply.status_code < 400:
                return reply, token
            err = self._classify(reply.status_code, await self._read_error(reply), channel)
            last = err
            if not getattr(err, "retryable", err.status_code in RETRYABLE):
                raise err
            # 凭证级失败（401/403/429）冷却该 key；5xx 只换 key 不退避冷却
            if err.status_code in COOLDOWN_STATUSES:
                others = await self._keys_pool.available([key for key in keys if key != token])
                if not others:
                    # 冷却唯一可用的 key 会让后续请求全部变成冷却错误，直接明确失败
                    raise self._all_keys_error(channel, keys, err)
                await self._keys_pool.cool(token, self._cooldown_sec())
            await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
        if last is None:
            raise ProviderError(502, f"{channel} 上游不可用")
        raise ProviderError(
            last.status_code,
            f"GPT/Grok {channel} 通道 {len(keys)} 把凭证轮换后仍失败（{last.message}）",
            upstream_code=last.upstream_code,
            upstream_message=last.upstream_message,
        )

    # ---------- 上游帧 → 增量 ----------

    @staticmethod
    async def _chatgpt_deltas(raw: AsyncIterator[bytes]) -> AsyncIterator[tuple[str, str]]:
        last = ""
        async for line in iter_json_lines(raw):
            if line == "[DONE]":
                return
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("error"):
                error = obj["error"] if isinstance(obj["error"], dict) else {"message": str(obj["error"])}
                raw_message = str(error.get("message") or error)
                raise ProviderError(
                    502,
                    f"ChatGPT 流内错误：{raw_message[:160]}",
                    upstream_code=str(error["code"]) if error.get("code") is not None else None,
                    upstream_message=raw_message,
                )
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            author = message.get("author") if isinstance(message.get("author"), dict) else {}
            if str(author.get("role") or "") != "assistant":
                continue
            if obj.get("o") == "append":
                text = last + str(obj.get("v") or "")
            else:
                content = message.get("content") if isinstance(message.get("content"), dict) else {}
                parts = content.get("parts") if isinstance(content.get("parts"), list) else []
                text = "".join(str(part) for part in parts if part is not None)
            delta = text[len(last):] if text.startswith(last) else text
            last = text
            status = str(message.get("status") or "")
            if delta:
                yield delta, "content"
            if status in FINISHED:
                return

    @staticmethod
    async def _grok_deltas(raw: AsyncIterator[bytes]) -> AsyncIterator[tuple[str, str]]:
        async for line in iter_json_lines(raw):
            if line == "[DONE]":
                return
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            error = obj.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error)
                code = error.get("code")
                if str(code) == "8" or "rate limit" in message.lower() or "too many" in message.lower():
                    raise ProviderError(429, f"Grok 上游限流：{message[:160]}",
                                        upstream_code=str(code) if code is not None else None,
                                        upstream_message=message)
                raise ProviderError(502, f"Grok 流内错误：{message[:160]}",
                                    upstream_code=str(code) if code is not None else None,
                                    upstream_message=message)
            result = obj.get("result") if isinstance(obj.get("result"), dict) else {}
            response = result.get("response") if isinstance(result.get("response"), dict) else {}
            token = response.get("token")
            if token:
                yield str(token), "thinking" if response.get("isThinking") else "content"
            if response.get("isSoftStop") or result.get("finalMetadata") or obj.get("finalMetadata"):
                return

    def _deltas(self, raw: AsyncIterator[bytes], channel: str):
        return self._chatgpt_deltas(raw) if channel == "chatgpt" else self._grok_deltas(raw)

    async def _stream(self, raw: AsyncIterator[bytes], channel: str, model: str) -> AsyncIterator[bytes]:
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:20]}"
        created = int(time.time())
        yield openai_chunk(completion_id, model, created, {"role": "assistant"})
        try:
            async for delta, kind in self._deltas(raw, channel):
                key = "reasoning_content" if kind == "thinking" else "content"
                yield openai_chunk(completion_id, model, created, {key: delta})
        finally:
            if hasattr(raw, "aclose"):
                await raw.aclose()
        yield openai_chunk(completion_id, model, created, {}, finish_reason="stop")
        yield DONE

    async def _collect(self, raw: AsyncIterator[bytes], channel: str, model: str) -> dict[str, Any]:
        content: list[str] = []
        reasoning: list[str] = []
        try:
            async for delta, kind in self._deltas(raw, channel):
                (reasoning if kind == "thinking" else content).append(delta)
        finally:
            if hasattr(raw, "aclose"):
                await raw.aclose()
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        return openai_completion(
            f"chatcmpl-{uuid.uuid4().hex[:20]}", model, int(time.time()), message, finish_reason="stop"
        )

    # ---------- 对外接口 ----------

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        model = str(payload.get("model") or CHATGPT_MODELS[0])
        channel = self._channel(model)
        body = self._chatgpt_body(payload, model) if channel == "chatgpt" else self._grok_body(payload, model)
        reply, _ = await self._open(channel, body)
        if stream:
            return Reply(200, stream=self._stream(reply.iter_bytes(), channel, model), content_type="text/event-stream")
        data = await self._collect(reply.iter_bytes(), channel, model)
        return Reply(200, body=json.dumps(data, ensure_ascii=False).encode())

    def declared_models(self) -> list[dict[str, Any]]:
        configured = self.options.get("models")
        ids: list[str] = []
        if isinstance(configured, list) and configured:
            ids = [str(item).strip() for item in configured if str(item).strip()]
        else:
            ids = [*CHATGPT_MODELS, *GROK_MODELS]
        channel = str(self.options.get("channel") or "").strip().lower()
        if channel == "grok":
            ids = [item for item in ids if item.startswith("grok")]
        elif channel == "chatgpt":
            ids = [item for item in ids if not item.startswith("grok")]
        return [{"id": item, "object": "model", "owned_by": "gptgrok"} for item in ids]

    async def health(self) -> dict[str, Any]:
        keys = self._keys()
        if not keys:
            return {"connected": False, "error": "未配置 ChatGPT / Grok 凭证"}
        channel = str(self.options.get("health_channel") or self.options.get("channel") or "chatgpt").strip().lower()
        if channel not in BASE_URLS:
            channel = "chatgpt"
        path = str(self.options.get("health_path") or (CHATGPT_HEALTH if channel == "chatgpt" else GROK_HEALTH))
        token = await self._keys_pool.peek(keys)
        if token is None:
            return {"connected": False, "channel": channel, "error": "全部凭证处于冷却中，请稍后重试"}
        headers = self._chatgpt_headers(token, stream=False) if channel == "chatgpt" else self._grok_headers(token)
        client = UnifiedApiClient(self._base(channel), timeout=20.0, proxy=self._proxy(), extra_headers=headers)
        try:
            reply = await client.forward(path, None, stream=False, method="GET", raw=b"")
        except ProviderError as exc:
            return {"connected": False, "error": exc.message, "channel": channel}
        if reply.status_code >= 400:
            return {
                "connected": False,
                "channel": channel,
                "error": f"{channel} 探测返回 {reply.status_code}",
            }
        return {"connected": True, "channel": channel, "models": len(await self.list_models())}


def build(settings: Settings, base_url: str = "") -> GptGrokAdapter:
    return GptGrokAdapter(settings, base_url)
