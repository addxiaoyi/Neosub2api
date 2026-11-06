"""ZCode 适配器（仓内自研，直连 zcode.z.ai）。

依据 docs/providers/zcode2api.md 的实测记录（上游 zcode2api 对 ZCode 客户端 3.16.3
的逆向结论）：唯一上游是 Anthropic Messages 端点，鉴权用账号 JWT 同时放
`authorization` 与 `x-api-key`，另带设备指纹头；system 必须是官方原文，否则回 3012。
本实现不复制上游源码，也不内置官方 system 原文（由 options.official_system 提供）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from ..api_utils import exc_summary, safe_url
from ..config import Settings
from .base import ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient
from .key_pool import DEFAULT_COOLDOWN_SEC
from ..services.rotation import CredentialPool, sticky_key_from_payload
from .stream_util import (
    DONE, Reply, content_text, iter_sse, openai_chunk, openai_completion, sse_frame,
)

MESSAGES_PATH = "/api/v1/zcode-plan/anthropic/v1/messages"
BALANCE_PATH = "/api/v1/zcode-plan/billing/balance"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_BETA = "mid-conversation-system-2026-04-07"
DEFAULT_MODELS = ("GLM-5.3-Flash", "GLM-5.3")
DEFAULT_MAX_TOKENS = 8192
MAX_TOKENS_LIMIT = 128_000
CLIENT_VERSION = "3.16.3"

RETRYABLE = {401, 402, 403, 408, 429, 500, 502, 503, 504}
POOL_COOLDOWN_STATUSES = RETRYABLE
REQUEST_ERRORS = {400, 422}
FINISH_REASON = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}

logger = logging.getLogger("unified.zcode")


def _official_system_ok(raw: Any) -> bool:
    """只判「有没有可用正文」：字符串非空白，或文本块数组里至少有一段非空 text。"""
    if isinstance(raw, str):
        return bool(raw.strip())
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                return True
            if isinstance(item, dict) and str(item.get("text") or "").strip():
                return True
    return False


class ZCodeAdapter(ProviderAdapter):
    """ZCode 上游的 OpenAI / Anthropic 双协议客户端。

    风控开关（providers 页 options，键名沿用代理版）：
    - system_prompt_override: 非空字符串时作为「应用层 system」注入（chat 协议）。
    - official_system / official_system_prompt: ZCode 官方 system 原文（str 或 Anthropic
      文本块数组）。上游做内容指纹校验，缺它就回 3012；本仓不内置该原文。
    """

    spec = ProviderSpec(
        id="zcode",
        name="ZCode 2API",
        vendor="addxiaoyi/ZCode2api（协议参考）",
        license="本仓自研（MIT）",
        runtime="主仓内置（直连 zcode.z.ai）",
        status="implemented",
        description="直连 zcode.z.ai 的 Anthropic Messages 端点，对外提供 OpenAI chat 与 Anthropic messages。",
        default_base_url="https://zcode.z.ai",
        capabilities=stub_capabilities(chat=True, messages=True, models=True),
        protocols=("openai.chat.completions", "anthropic.messages"),
        user_agent=f"ZCode/{CLIENT_VERSION}",
        readiness="needs_config",
        requires=(
            "official_system（须用户自备：官方 system 原文约 10701 字符，本仓不内置；"
            "缺失时上游回 3012，导入后发一条 chat 不再回 3012 即有效）",
        ),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        # 轮换状态属于本实例：游标、冷却表、粘性绑定都不跨实例共享
        self._keys_pool = CredentialPool()
        super().__init__(base_url)

    # ---------- 凭证 ----------

    def _cooldown_sec(self) -> float:
        return float(self.options.get("key_cooldown") or DEFAULT_COOLDOWN_SEC)

    def _all_keys_error(self, keys: list[str], last: ProviderError | None) -> ProviderError:
        reason = last.message if last else "全部密钥处于冷却中，请稍后重试"
        return ProviderError(
            last.status_code if last else 429,
            f"ZCode {len(keys)} 把密钥全部不可用（{reason}）",
            upstream_code=last.upstream_code if last else None,
            upstream_message=last.upstream_message if last else None,
        )

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

    def readiness_now(
        self,
        *,
        has_credential: bool,
        credential_problem: str,
        last_probe: dict[str, Any] | None,
        credential: str = "",
        options: dict[str, Any] | None = None,
    ) -> tuple[str, tuple[str, ...]]:
        options = options or {}
        missing: list[str] = []
        if not has_credential:
            missing.append("api_key（ZCode 账号 JWT；本仓不内置）")
        if credential_problem:
            missing.append(credential_problem)
        official = options.get("official_system") or options.get("official_system_prompt")
        if not _official_system_ok(official):
            missing.append("official_system（官方 system 原文缺失或为空；缺失时上游回 3012）")
        if missing:
            return "needs_config", tuple(missing)
        return "ready", ()

    @staticmethod
    def _jwt_sub(token: str) -> str:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            data = json.loads(base64.urlsafe_b64decode(payload))
        except (ValueError, json.JSONDecodeError):
            return ""
        if isinstance(data, dict):
            for field in ("sub", "user_id", "uid", "id"):
                if data.get(field):
                    return str(data[field])
        return ""

    def _uid(self, token: str) -> str:
        configured = str(self.options.get("uid") or "").strip()
        return configured or self._jwt_sub(token) or hashlib.sha256(token.encode()).hexdigest()[:16]

    @staticmethod
    def _device_id(uid: str) -> str:
        # 客户端按 machine + uid 派生，形态是 UUIDv4；本实现按同一输入稳定派生
        digest = hashlib.md5(f"machine:{uid}".encode()).hexdigest()
        return f"{digest[0:8]}-{digest[8:12]}-4{digest[13:16]}-{digest[16:20]}-{digest[20:32]}"

    @staticmethod
    def _session_id(uid: str) -> str:
        return "session_" + hashlib.sha256(f"session:{uid}".encode()).hexdigest()[:32]

    def _headers(self, token: str, *, stream: bool) -> dict[str, str]:
        uid = self._uid(token)
        headers = {
            "authorization": f"Bearer {token}",
            "x-api-key": token,
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": ANTHROPIC_BETA,
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
            "user-agent": self.client_user_agent() or f"ZCode/{CLIENT_VERSION}",
            "x-device-mid": self._device_id(uid),
            "x-session-id": self._session_id(uid),
            "x-query-id": uuid.uuid4().hex,
            "x-request-id": str(uuid.uuid4()),
            "x-zcode-trace-id": uuid.uuid4().hex,
            "x-platform": str(self.options.get("platform") or "win32"),
            "x-title": "zcode-cli",
            "x-release-channel": "stable",
        }
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

    # ---------- 错误分类 ----------

    def _classify(self, status: int, text: str) -> ProviderError:
        code: int | None = None
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            obj = None
        if isinstance(obj, dict):
            for value in (obj.get("code"), obj.get("biz_code"), (obj.get("error") or {}).get("biz_code")
                          if isinstance(obj.get("error"), dict) else None):
                if isinstance(value, int):
                    code = value
                    break
        if code == 3012:
            official = self._official_system()
            if official:
                message = (
                    "ZCode 3012 风控：已配置 official_system 但上游仍回 3012（system 指纹不匹配），"
                    "原文可能不是当前客户端版本，请核对后重新导入 options.official_system（本仓不内置）"
                )
            else:
                message = (
                    "ZCode 3012 风控：缺 official_system（options.official_system 未配置），上游回 3012。"
                    "请导入 ZCode 客户端官方 system 原文（约 10701 字符，本仓不内置）"
                )
            err = ProviderError(403, message)
            err.retryable = False  # type: ignore[attr-defined]
            return err.with_vendor_detail(text)
        if code in (1005, 3009):
            err = ProviderError(429, f"ZCode 上游限流（{code}）：{text[:160]}")
        elif code == 3001:
            err = ProviderError(400, "ZCode 3001：缺少或无效的 x-device-mid，请检查 options.uid 与账号匹配")
            err.retryable = False  # type: ignore[attr-defined]
        elif status == 401:
            err = ProviderError(401, f"ZCode 凭证失效或未登录：{text[:160]}")
        elif status == 429:
            err = ProviderError(429, f"ZCode 上游限流：{text[:160]}")
        elif status == 404:
            err = ProviderError(404, f"ZCode 上游端点不存在：{text[:160]}")
        elif status >= 500:
            err = ProviderError(502, f"ZCode 上游服务异常（{status}）：{text[:160]}")
        elif status in REQUEST_ERRORS:
            err = ProviderError(status, f"ZCode 请求被拒：{text[:160]}")
            err.retryable = False  # type: ignore[attr-defined]
        else:
            err = ProviderError(status, f"ZCode 上游错误（{status}）：{text[:160]}")
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

    # ---------- 发送与换 key ----------

    async def _send(
        self, path: str, payload: dict[str, Any] | None, *, stream: bool, sticky_key: str = ""
    ):
        keys = self._keys()
        if not keys:
            raise ProviderError(503, "ZCode 未配置账号凭证（服务商页 api_key 或 options.extra_keys）")
        # 权重与策略在每次请求前同步：options 改动后无需重建适配器
        self._keys_pool.set_weights(keys, self.options.get("key_weights"))
        attempts = self._attempts(len(keys))
        last: ProviderError | None = None
        for attempt in range(attempts):
            pick = await self._keys_pool.acquire(keys, sticky_key=sticky_key)
            if pick is None:
                raise self._all_keys_error(keys, last)
            token = pick.key
            if len(keys) > 1:
                logger.info("ZCode 选号 cred=%s 规则=%s %s", pick.cred_id, pick.rule, pick.note or "-")
            client = self._client(token, stream=stream)
            try:
                raw = b"" if payload is None else None
                reply = await client.forward(
                    path, payload, stream=stream, method="GET" if payload is None else "POST", raw=raw
                )
            except ProviderError as exc:
                last = exc
                if exc.status_code in REQUEST_ERRORS:
                    raise
                await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
                continue
            except httpx.HTTPError as exc:
                last = ProviderError(502, f"ZCode 上游连接失败（{safe_url(self.base_url)}）：{exc_summary(exc)}")
                await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
                continue
            if reply.status_code < 400:
                await self._keys_pool.report_success(token)
                if sticky_key:
                    await self._keys_pool.bind_sticky(sticky_key, token)
                return reply
            err = self._classify(reply.status_code, await self._read_error(reply))
            if err.status_code in (401, 403):
                logger.warning("ZCode 上游 %s：%s", reply.status_code, err.message)
            last = err
            if not getattr(err, "retryable", err.status_code in RETRYABLE):
                raise err
            # 401/402/403/408/429/5xx 都交给池分档冷却；唯一可用账号仍不冷却
            if err.status_code in POOL_COOLDOWN_STATUSES:
                others = [key for key in keys if key != token]
                if not await self._keys_pool.available(others):
                    # 没有可替代账号时不做冷却：把唯一一把 key 关进冷却只会让后续请求直接 429
                    raise self._all_keys_error(keys, err)
                await self._keys_pool.report_failure(
                    token, err.status_code, cooldown_sec=self._cooldown_sec()
                )
            await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
        if last is None:
            raise ProviderError(502, "ZCode 上游不可用")
        raise ProviderError(
            last.status_code,
            f"ZCode {len(keys)} 把密钥轮换后仍失败（{last.message}）",
            upstream_code=last.upstream_code,
            upstream_message=last.upstream_message,
        )

    # ---------- system 改写 ----------

    def _official_system(self) -> list[dict[str, Any]]:
        raw = self.options.get("official_system") or self.options.get("official_system_prompt")
        if isinstance(raw, str) and raw.strip():
            return [{"type": "text", "text": raw}]
        if isinstance(raw, list):
            blocks: list[dict[str, Any]] = []
            for item in raw:
                if isinstance(item, str) and item.strip():
                    blocks.append({"type": "text", "text": item})
                elif isinstance(item, dict) and item.get("text"):
                    block = {"type": "text", "text": str(item["text"])}
                    if item.get("cache_control"):
                        block["cache_control"] = item["cache_control"]
                    blocks.append(block)
            return blocks
        return []

    @staticmethod
    def _demote(text: str) -> str:
        return f"<pi_system_instructions>{text}</pi_system_instructions>"

    def _chat_body(self, payload: dict[str, Any], model: str, *, stream: bool) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        system_parts: list[str] = []
        for item in payload.get("messages") or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            text = content_text(item.get("content"))
            if role in ("system", "developer"):
                if text:
                    system_parts.append(text)
                continue
            if role in ("tool", "function"):
                role = "user"
                text = f"[tool result] {text}" if text else "[tool result]"
            target = role if role in ("user", "assistant") else "user"
            blocks = [{"type": "text", "text": text}] if text else []
            if messages and messages[-1]["role"] == target:
                messages[-1]["content"].extend(blocks)
            else:
                messages.append({"role": target, "content": blocks})
        if not messages:
            raise ProviderError(400, "ZCode 请求缺少 messages")

        override = str(self.options.get("system_prompt_override") or "").strip()
        app_system = override or "\n".join(part for part in system_parts if part)
        official = self._official_system()
        keys = self._keys()
        token = keys[0] if keys else ""
        uid = self._uid(token) if token else ""
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": self._max_tokens(payload),
            "stream": stream,
            "tool_choice": {"type": "auto"},
            "thinking": {"type": "enabled"},
            "output_config": {"effort": str(self.options.get("effort") or "low")},
            "metadata": {"user_id": json.dumps(
                {
                    "device_id": self._device_id(uid) if uid else "",
                    "account_uuid": "",
                    "session_id": self._session_id(uid) if uid else "",
                },
                separators=(",", ":"),
            )},
        }
        if official:
            body["system"] = official
            if app_system and messages[0]["role"] == "user":
                prefix = self._demote(app_system)
                messages[0]["content"].insert(0, {"type": "text", "text": prefix})
        elif app_system:
            body["system"] = app_system

        tools = payload.get("tools")
        if isinstance(tools, list) and tools:
            converted = []
            for tool in tools:
                fn = tool.get("function") if isinstance(tool, dict) else None
                if isinstance(fn, dict) and fn.get("name"):
                    converted.append({
                        "name": str(fn["name"]),
                        "description": str(fn.get("description") or ""),
                        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
                    })
            if converted:
                body["tools"] = converted
                choice = payload.get("tool_choice")
                if isinstance(choice, dict) and choice.get("function", {}).get("name"):
                    body["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
                elif choice == "required":
                    body["tool_choice"] = {"type": "any"}
                elif choice == "none":
                    body["tool_choice"] = {"type": "none"}
        return body

    def _max_tokens(self, payload: dict[str, Any]) -> int:
        raw = payload.get("max_tokens") or payload.get("max_completion_tokens") or self.options.get("max_tokens")
        try:
            value = int(raw or DEFAULT_MAX_TOKENS)
        except (TypeError, ValueError):
            value = DEFAULT_MAX_TOKENS
        return max(1, min(value, MAX_TOKENS_LIMIT))

    def _messages_body(self, payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
        body = {**payload, "stream": stream}
        official = self._official_system()
        if not official:
            return body
        app_system = content_text(payload.get("system"))
        body["system"] = official
        messages = [dict(item) for item in payload.get("messages") or [] if isinstance(item, dict)]
        if app_system and messages and str(messages[0].get("role")) == "user":
            prefix = {"type": "text", "text": self._demote(app_system)}
            original = messages[0].get("content")
            if isinstance(original, str):
                messages[0]["content"] = [prefix, {"type": "text", "text": original}]
            elif isinstance(original, list):
                messages[0]["content"] = [prefix, *original]
            else:
                messages[0]["content"] = [prefix]
        body["messages"] = messages
        return body

    # ---------- 响应转换 ----------

    @staticmethod
    def _usage(raw: dict[str, Any]) -> dict[str, Any]:
        prompt = int(raw.get("input_tokens") or 0)
        completion = int(raw.get("output_tokens") or 0)
        return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}

    def _anthropic_to_openai(self, data: dict[str, Any], model: str) -> dict[str, Any]:
        text: list[str] = []
        reasoning: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text.append(str(block.get("text") or ""))
            elif kind == "thinking":
                reasoning.append(str(block.get("thinking") or ""))
            elif kind == "tool_use":
                calls.append({
                    "id": str(block.get("id") or f"call_{uuid.uuid4().hex[:16]}"),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                })
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = calls
        finish = FINISH_REASON.get(str(data.get("stop_reason") or ""), "stop")
        return openai_completion(
            f"chatcmpl-{uuid.uuid4().hex[:20]}",
            model,
            int(time.time()),
            message,
            finish_reason=finish,
            usage=self._usage(data.get("usage") or {}),
        )

    async def _chat_stream(self, raw: AsyncIterator[bytes], model: str) -> AsyncIterator[bytes]:
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:20]}"
        created = int(time.time())
        yield openai_chunk(completion_id, model, created, {"role": "assistant"})
        blocks: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        finish = "stop"
        try:
            async for event, data in iter_sse(raw):
                if not data:
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                kind = str(obj.get("type") or event)
                if kind == "message_start":
                    message = obj.get("message") or {}
                    usage.update(self._usage(message.get("usage") or {}))
                elif kind == "content_block_start":
                    index = int(obj.get("index") or 0)
                    block = obj.get("content_block") or {}
                    blocks[index] = {
                        "type": block.get("type"),
                        "id": block.get("id") or f"call_{uuid.uuid4().hex[:16]}",
                        "name": block.get("name") or "",
                        "arguments": "",
                    }
                elif kind == "content_block_delta":
                    index = int(obj.get("index") or 0)
                    delta = obj.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta" and delta.get("text"):
                        yield openai_chunk(completion_id, model, created, {"content": str(delta["text"])})
                    elif dtype == "thinking_delta" and delta.get("thinking"):
                        yield openai_chunk(
                            completion_id, model, created, {"reasoning_content": str(delta["thinking"])}
                        )
                    elif dtype == "input_json_delta":
                        slot = blocks.setdefault(index, {"type": "tool_use", "id": "", "name": "", "arguments": ""})
                        slot["arguments"] += str(delta.get("partial_json") or "")
                elif kind == "content_block_stop":
                    index = int(obj.get("index") or 0)
                    slot = blocks.get(index) or {}
                    if slot.get("type") == "tool_use":
                        yield openai_chunk(completion_id, model, created, {"tool_calls": [{
                            "index": index,
                            "id": slot.get("id"),
                            "type": "function",
                            "function": {"name": slot.get("name"), "arguments": slot.get("arguments") or "{}"},
                        }]})
                elif kind == "message_delta":
                    delta = obj.get("delta") or {}
                    if delta.get("stop_reason"):
                        finish = FINISH_REASON.get(str(delta["stop_reason"]), "stop")
                    if isinstance(obj.get("usage"), dict):
                        usage.update(self._usage(obj["usage"]))
                elif kind == "message_stop":
                    break
                elif kind == "error":
                    yield sse_frame({"error": {
                        "message": str((obj.get("error") or {}).get("message") or "上游流内错误"),
                        "type": "server_error",
                        "code": 502,
                    }})
                    break
        finally:
            if hasattr(raw, "aclose"):
                await raw.aclose()
        yield openai_chunk(completion_id, model, created, {}, finish_reason=finish, usage=usage or None)
        yield DONE

    # ---------- 对外接口 ----------

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        model = str(payload.get("model") or DEFAULT_MODELS[0])
        body = self._chat_body(payload, model, stream=stream)
        reply = await self._send(
            MESSAGES_PATH, body, stream=stream, sticky_key=sticky_key_from_payload(payload)
        )
        if stream:
            return Reply(200, stream=self._chat_stream(reply.iter_bytes(), model), content_type="text/event-stream")
        try:
            data = json.loads(reply.body or b"{}")
        except json.JSONDecodeError as exc:
            raise ProviderError(502, f"ZCode 上游返回非 JSON：{exc}") from exc
        return Reply(200, body=json.dumps(self._anthropic_to_openai(data, model), ensure_ascii=False).encode())

    async def messages(self, payload: dict[str, Any], *, stream: bool):
        body = self._messages_body(payload, stream=stream)
        reply = await self._send(
            MESSAGES_PATH, body, stream=stream, sticky_key=sticky_key_from_payload(payload)
        )
        if stream:
            # Anthropic 协议原样透传：上游帧名、[DONE]、错误帧都保持
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
        return [{"id": model, "object": "model", "owned_by": "zcode"} for model in ids]

    async def health(self) -> dict[str, Any]:
        keys = self._keys()
        if not keys:
            return {"connected": False, "error": "未配置 ZCode 账号凭证"}
        try:
            reply = await self._send(f"{BALANCE_PATH}?app_version={CLIENT_VERSION}", None, stream=False)
        except ProviderError as exc:
            return {"connected": False, "error": exc.message}
        try:
            data = json.loads(reply.body or b"{}")
        except json.JSONDecodeError:
            return {"connected": True, "note": "凭证可用，但额度接口返回非 JSON"}
        balance = data.get("data") if isinstance(data.get("data"), dict) else data
        rotation = await self._keys_pool.snapshot(keys)
        return {
            "connected": True,
            "models": len(DEFAULT_MODELS),
            "balance": balance,
            # 选号规则与每把钥匙的冷却剩余进 health，排查时不用猜为什么用了某把号
            "rotation": rotation,
            "cooling": rotation.get("cooling", 0),
        }


def build(settings: Settings, base_url: str = "") -> ZCodeAdapter:
    return ZCodeAdapter(settings, base_url)
