"""Trae Solo / Work 通道适配器（clean-room 重写）。

协议契约见 docs/providers/trae2api.md：Solo 走单端点 llm_utils_chat，Work 走
create_agent_task + workflow/start + query/sync_history_state 的有状态桥。
设备头、设备 ID、JWT 解析均为本仓自写，不依赖上游实现或本地 sidecar。
"""

import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Any, AsyncIterator

from ..config import Settings
from ..services.rotation import CredentialPool, sticky_key_from_payload
from .base import ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient

logger = logging.getLogger("unified.gateway")

SOLO_HOST = "https://trae-api-cn.mchost.guru"
WORK_HOST = "https://api5-normal.mchost.guru"
OAUTH_HOST = "https://api.trae.com.cn"

CHAT_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
WORK_APP_ID = "931506"
SOLO_VERSION = "0.1.52"
# 429 重试的等待上限：超过就放弃重试直接按 429 返回，别让巨大的 Retry-After 占死工作线程
MAX_RETRY_WAIT_SEC = 5.0
SOLO_VERSION_CODE = "20260811"
WORK_VERSION = "0.1.63"
WORK_VERSION_CODE = "20260901"
DEVICE_BRAND = "Apple"
OS_VERSION = "macOS 15.7.4"
FUNCTION = "solo_work_lite"

EP_CHAT = "/api/agent/v3/llm_utils_chat"
EP_MODELS = "/api/ide/v1/get_detail_param"
EP_EXCHANGE = "/cloudide/api/v3/trae/oauth/ExchangeToken"
EP_CREATE_TASK = "/api/agent/v3/create_agent_task"
EP_WORKFLOW_START = "/api/agent/v3/workflow/start"
EP_QUERY_HISTORY = "/api/agent/v3/query_history_state"
EP_SYNC_HISTORY = "/api/agent/v3/sync_history_state"

DEFAULT_SOLO_MODEL = "glm-5.2"
DEFAULT_WORK_MODEL = "DeepSeek-V4-Flash-Official"

# 积分/套餐受限：Work 侧遇到这些码要降级到 Solo
CREDIT_CODES = {1005, 4008, 4001, 4000105}
SOFT_RATE_CODES = {4011}

# 这些状态码值得换号重试，并交给账号池分档冷却；其余错误直接抛给客户端
ROTATE_STATUSES = {401, 402, 403, 408, 429}


class _Reply:
    """网关需要的响应形状；流式和聚合后都用它，避免直接暴露 httpx 对象。"""

    def __init__(
        self,
        status_code: int,
        *,
        body: bytes | None = None,
        stream: AsyncIterator[bytes] | None = None,
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = {"content-type": content_type}
        self._stream = stream

    async def iter_bytes(self):
        if self._stream is None:
            return
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        self._stream = None


def _b64url(value: str) -> dict[str, Any]:
    pad = "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(value + pad)
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _chunk(model: str, cmpl_id: str, delta: dict[str, Any], finish: str | None, usage: dict[str, Any] | None) -> bytes:
    choice: dict[str, Any] = {"index": 0, "delta": delta}
    if finish:
        choice["finish_reason"] = finish
    obj: dict[str, Any] = {
        "id": cmpl_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
    }
    if usage is not None:
        obj["usage"] = usage
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"


async def _sse_events(source: AsyncIterator[bytes]) -> AsyncIterator[tuple[str, str]]:
    """把字节流切成 (event, data) 事件；event 缺省时为空串。"""
    buffer = ""
    event = ""
    data: list[str] = []
    async for raw in source:
        buffer += raw.decode("utf-8", "ignore")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                if event or data:
                    yield event, "\n".join(data)
                event, data = "", []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
    if event or data:
        yield event, "\n".join(data)


def _merge_tool_call(slots: dict[int, dict[str, Any]], delta: dict[str, Any]) -> None:
    idx = int(delta.get("index") or 0)
    slot = slots.setdefault(idx, {"index": idx, "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    if delta.get("id"):
        slot["id"] = delta["id"]
    fn = delta.get("function") or delta.get("function_call") or {}
    if isinstance(fn, dict):
        if fn.get("name"):
            slot["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]
    if not slot["id"]:
        slot["id"] = f"call_{idx}_{uuid.uuid4().hex[:8]}"


class TraeAdapter(ProviderAdapter):
    spec = ProviderSpec(
        id="trae",
        name="Trae（Solo / Work）",
        vendor="Trae2API（协议参考）",
        license="本仓自研（MIT）",
        runtime="主仓内置（直连 Solo / Work）",
        status="implemented",
        description="按协议契约重写的 Trae 客户端：Solo 单端点 chat + Work 有状态桥，积分不足自动降级 Solo。",
        default_base_url=SOLO_HOST,
        capabilities=stub_capabilities(chat=True, models=True),
        protocols=("openai.chat.completions",),
        user_agent=f"Trae/{SOLO_VERSION}",
        readiness="needs_config",
        requires=(
            "Trae 登录后的 Cloud-IDE JWT（Trae 客户端登录取 token，填 api_key）",
            "Work 通道另需 options.channel=work 且账号有积分；stateful 桥未真机验证",
        ),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        self._pool = CredentialPool()
        self._active_jwt: ContextVar[str] = ContextVar("trae_active_jwt", default="")
        super().__init__(base_url)

    # ---------- 身份与请求头 ----------

    def _keys(self) -> list[str]:
        raw: list[Any] = [*(self.api_keys or []), self.api_key, *(self.options.get("extra_keys") or [])]
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
        return max(1, min(configured, 10, max(key_count, 1)))

    def _jwt(self) -> str:
        raw = (self._active_jwt.get() or self.api_key).strip()
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        return raw

    @staticmethod
    def _rotatable(status: int) -> bool:
        return status in ROTATE_STATUSES or 500 <= status <= 599

    async def _note_failure(self, key: str, status: int, keys: list[str]) -> None:
        # 唯一可用账号不冷却，避免把真实的 401/402 伪装成全池 429
        others = [item for item in keys if item != key]
        if others and await self._pool.available(others):
            await self._pool.report_failure(key, status)

    def _all_keys_error(self, keys: list[str], last: ProviderError | None) -> ProviderError:
        if last is not None:
            return ProviderError(429, f"Trae {len(keys)} 个账号全部不可用：{last.message}")
        return ProviderError(429, f"Trae {len(keys)} 个账号全部处于冷却中，请稍后重试")

    def _tracked_stream(
        self, source: AsyncIterator[bytes], key: str, keys: list[str], sticky: str
    ) -> AsyncIterator[bytes]:
        """流式响应只有读到正常收尾才算成功；中途报错按非流式同一口径登记账号池。

        流对象一返回就给账号记成功，会把随后上游在流里下发的 error 事件掩盖掉：
        该冷却的账号不会被冷却，后续请求还会继续撞同一把坏号。
        """

        async def body():
            completed = False
            try:
                async for chunk in source:
                    yield chunk
                completed = True
            except ProviderError as exc:
                if self._rotatable(exc.status_code):
                    await self._note_failure(key, exc.status_code, keys)
                raise
            except Exception:  # noqa: BLE001 - 读流失败多是连接/出口问题，不判死账号
                await self._pool.report_network_error(key)
                raise
            finally:
                if completed:
                    await self._pool.report_success(key)
                    if sticky:
                        await self._pool.bind_sticky(sticky, key)

        return body()

    def _decode_claims(self) -> dict[str, Any]:
        parts = self._jwt().split(".")
        if len(parts) < 2:
            return {}
        try:
            return _b64url(parts[1])
        except (ValueError, json.JSONDecodeError):
            return {}

    def _uid(self) -> str:
        configured = str(self.options.get("uid") or "").strip()
        if configured:
            return configured
        claims = self._decode_claims()
        for key in ("uid", "user_id", "userId", "sub"):
            if claims.get(key):
                return str(claims[key])
        return hashlib.sha256(self._jwt().encode()).hexdigest()[:16]

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
        raw = credential.strip()
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        if not raw:
            return "needs_config", self.spec.requires
        parts = raw.split(".")
        if len(parts) != 3 or not parts[1]:
            return "needs_config", ("Trae Cloud-IDE JWT 格式不对（应为三段 base64url，从 Trae 客户端登录后复制）",)
        try:
            claims = _b64url(parts[1])
        except ValueError:
            return "needs_config", ("Trae Cloud-IDE JWT 无法解码（token 可能被截断），请重新复制完整 JWT",)
        if not claims:
            return "needs_config", ("Trae Cloud-IDE JWT 的 payload 不是对象，请确认粘贴的是登录 JWT",)
        return "ready", ()

    def _machine_id(self) -> str:
        configured = str(self.options.get("machine_id") or "").strip()
        return configured or hashlib.sha256(("trae-machine:" + self._jwt()).encode()).hexdigest()[:32]

    def _device_id(self) -> str:
        configured = str(self.options.get("device_id") or "").strip()
        return configured or hashlib.sha256(("trae-device:" + self._jwt()).encode()).hexdigest()[:32]

    def _solo_headers(self) -> dict[str, str]:
        jwt = self._jwt()
        return {
            "user-agent": f"Trae/{SOLO_VERSION}",
            "authorization": f"Cloud-IDE-JWT {jwt}",
            "x-cloudide-token": jwt,
            "x-ide-token": jwt,
            "x-uid": self._uid(),
            "x-app-id": CHAT_APP_ID,
            "x-app-version": "default",
            "x-ide-version": SOLO_VERSION,
            "x-ide-version-code": SOLO_VERSION_CODE,
            "x-app-version-code": SOLO_VERSION_CODE,
            "x-ide-version-type": "stable",
            "x-device-type": "macos",
            "x-os-version": OS_VERSION,
            "x-device-brand": DEVICE_BRAND,
            "request-traffic-type": "prod",
            "x-machine-id": self._machine_id(),
            "x-device-id": self._device_id(),
        }

    def _work_headers(self, *, chat_app_id: bool, stream: bool) -> dict[str, str]:
        jwt = self._jwt()
        return {
            "accept": "text/event-stream, application/json" if stream else "application/json",
            "user-agent": f"Trae/{WORK_VERSION}",
            "x-app-id": CHAT_APP_ID if chat_app_id else WORK_APP_ID,
            "x-ide-version": WORK_VERSION,
            "x-ide-version-code": WORK_VERSION_CODE,
            "x-app-version-code": WORK_VERSION_CODE,
            "x-version-code": WORK_VERSION_CODE,
            "x-device-type": "macos",
            "x-device-platform": "darwin",
            "x-platform": "darwin",
            "x-os": "darwin",
            "x-os-type": "darwin",
            "x-system": "darwin",
            "request-traffic-type": "prod",
            "authorization": f"Cloud-IDE-JWT {jwt}",
            "x-cloudide-token": jwt,
            "x-ide-token": jwt,
            "x-uid": self._uid(),
            "x-device-id": self._device_id(),
            "x-machine-id": self._machine_id(),
        }

    def _client(self, headers: dict[str, str], *, host: str | None = None) -> UnifiedApiClient:
        return UnifiedApiClient(
            host or self.base_url,
            api_key="",
            timeout=float(self.options.get("timeout") or 300.0),
            proxy=str(self.options.get("proxy") or ""),
            extra_headers=headers,
        )

    # ---------- 错误 ----------

    def _classify_error(self, status: int | None, code: int | None, message: str) -> ProviderError:
        """三类错误各归各位：套餐/积分受限 → 402，软限流 → 429，会话失效 → 401。

        - ErrPlanLimit：账号级配额用完，换 retry 没用，交给上层按 insufficient_quota 处理；
        - ErrSoftRate：短时限流，可带 Retry-After 让客户端退避；
        - ErrSessionDead：JWT 被拒，必须回到 401，不能伪装成 429 让客户端一直重试。
        """
        text = message or ""
        if status == 401:
            err = ProviderError(401, "Trae 会话已失效：JWT 被拒，请重新登录并更新 api_key")
            err.session_dead = True
        elif code in CREDIT_CODES or ("1005" in text and "plan" in text.lower()):
            err = ProviderError(402, f"Trae 积分/套餐受限：code={code} {text}".strip())
            err.plan_limit = True
        elif status == 429 or code in SOFT_RATE_CODES:
            err = ProviderError(429, f"Trae 触发限流：code={code} {text}".strip())
            err.soft_rate = True
        elif status == 404:
            err = ProviderError(404, f"Trae 上游端点不存在：{text}".strip())
        elif status is not None and status >= 500:
            err = ProviderError(502, f"Trae 上游服务异常（{status}）：{text}".strip())
        else:
            detail = text or (f"HTTP {status}" if status else "上游返回业务错误")
            err = ProviderError(400, f"Trae 请求被拒：{detail}")
        if code is not None:
            err.upstream_code = str(code)
        return err.with_vendor_detail(text)

    async def _reply_text(self, reply) -> str:
        if reply.body is not None:
            return reply.body.decode("utf-8", "ignore")
        if getattr(reply, "response", None) is not None:
            raw = await reply.response.aread()
            await reply.aclose()
            return raw.decode("utf-8", "ignore")
        return ""

    def _error_code(self, obj: dict[str, Any]) -> int | None:
        for key in ("code", "error_code", "err_code"):
            value = obj.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        err = obj.get("error")
        if isinstance(err, dict) and isinstance(err.get("code"), (int, float)):
            return int(err["code"])
        return None

    # ---------- Solo ----------

    def _solo_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.loads(json.dumps(payload, ensure_ascii=False))
        messages = body.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                if message.get("role") == "assistant" and isinstance(message.get("tool_calls"), list):
                    kept: list[dict[str, Any]] = []
                    for call in message["tool_calls"]:
                        if not isinstance(call, dict):
                            continue
                        fn = call.get("function")
                        if isinstance(fn, dict):
                            call["function_call"] = fn
                        name = str((call.get("function_call") or {}).get("name") or "").strip()
                        if name:
                            kept.append(call)
                    if kept:
                        message["tool_calls"] = kept
                    else:
                        message.pop("tool_calls", None)
                content = message.get("content")
                if isinstance(content, str):
                    message["content"] = [{"type": "text", "text": content}]

        model = str(body.get("model") or "").strip() or DEFAULT_SOLO_MODEL
        body["model"] = model
        body["config_name"] = model
        body["function"] = FUNCTION
        body["stream"] = True
        self._normalize_tool_choice(body)
        self._normalize_tools(body)
        return body

    def _normalize_tool_choice(self, body: dict[str, Any]) -> None:
        choice = body.get("tool_choice")
        if choice is None:
            return
        if isinstance(choice, str):
            if choice.strip().lower() == "none":
                body.pop("tool_choice", None)
                body.pop("tools", None)
                body.pop("functions", None)
            return
        if not isinstance(choice, dict):
            body.pop("tool_choice", None)
            return
        kind = str(choice.get("type") or "").strip().lower()
        if kind == "none":
            body.pop("tool_choice", None)
            body.pop("tools", None)
            body.pop("functions", None)
        elif kind in ("auto", "required"):
            body["tool_choice"] = kind
        elif kind == "function":
            name = str((choice.get("function") or {}).get("name") or choice.get("name") or "").strip()
            body["tool_choice"] = name or "auto"
        else:
            body.pop("tool_choice", None)

    def _normalize_tools(self, body: dict[str, Any]) -> None:
        tools = body.get("tools")
        if not isinstance(tools, list):
            return
        kept: list[dict[str, Any]] = []
        for item in tools:
            if not isinstance(item, dict) or not isinstance(item.get("function"), dict):
                continue
            fn = item["function"]
            if isinstance(fn.get("parameters"), dict):
                fn["parameters"] = json.dumps(fn["parameters"], ensure_ascii=False)
            kept.append(item)
        if kept:
            body["tools"] = kept
        else:
            body.pop("tools", None)

    def _retry_wait(self, reply) -> float | None:
        """只有上游明确给了 Retry-After 且不超过阈值才重试；超限直接按 429 返回。"""
        raw = (getattr(reply, "headers", None) or {}).get("retry-after")
        try:
            wait = float(raw)
        except (TypeError, ValueError):
            return None
        limit = float(self.options.get("retry_after_max_sec") or MAX_RETRY_WAIT_SEC)
        if wait > limit:
            logger.warning("Trae 429 Retry-After=%.1fs 超过阈值 %.1fs，放弃重试", wait, limit)
            return None
        return max(0.0, wait)

    async def _solo_chat(self, payload: dict[str, Any], *, stream: bool):
        if not self._jwt():
            raise ProviderError(401, "Trae 未配置 JWT（服务商页 api_key 填登录后的 access token）")
        body = self._solo_payload(payload)
        model = body["model"]
        client = self._client(self._solo_headers())
        reply = await client.forward(EP_CHAT, body, stream=True)
        wait = self._retry_wait(reply) if reply.status_code == 429 else None
        if wait is not None:
            await reply.aclose()
            await asyncio.sleep(wait)
            reply = await client.forward(EP_CHAT, body, stream=True)
        if reply.status_code >= 400:
            text = await self._reply_text(reply)
            code = None
            try:
                code = self._error_code(json.loads(text))
            except (json.JSONDecodeError, TypeError):
                code = None
            raise self._classify_error(reply.status_code, code, text)

        if stream:
            return _Reply(200, stream=self._render_solo_stream(reply, model), content_type="text/event-stream")
        data = await self._aggregate_solo(reply, model)
        return _Reply(200, body=json.dumps(data, ensure_ascii=False).encode())

    async def _render_solo_stream(self, reply, model: str) -> AsyncIterator[bytes]:
        cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        pending_usage: dict[str, Any] | None = None
        try:
            async for event, data in _sse_events(reply.iter_bytes()):
                if not data:
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if event == "output":
                    delta: dict[str, Any] = {}
                    if obj.get("response"):
                        delta["content"] = obj["response"]
                    if obj.get("reasoning_content"):
                        delta["reasoning_content"] = obj["reasoning_content"]
                    calls = obj.get("tool_calls")
                    if isinstance(calls, list) and calls:
                        delta["tool_calls"] = calls
                    if delta:
                        yield _chunk(model, cmpl_id, delta, None, None)
                elif event == "token_usage":
                    pending_usage = obj
                elif event == "done":
                    finish = str(obj.get("finish_reason") or "stop")
                    yield _chunk(model, cmpl_id, {}, finish, pending_usage)
                    yield b"data: [DONE]\n\n"
                    return
                elif event == "error":
                    err = self._classify_error(None, self._error_code(obj), str(obj.get("message") or obj.get("error") or ""))
                    yield f"event: error\ndata: {json.dumps({'error': {'message': err.message}}, ensure_ascii=False)}\n\n".encode()
                    raise err
            # 上游没有 done 就断了：补一个收尾，避免客户端一直挂
            yield _chunk(model, cmpl_id, {}, "stop", pending_usage)
            yield b"data: [DONE]\n\n"
        finally:
            await reply.aclose()

    async def _aggregate_solo(self, reply, model: str) -> dict[str, Any]:
        content: list[str] = []
        reasoning: list[str] = []
        slots: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] | None = None
        finish = "stop"
        try:
            async for event, data in _sse_events(reply.iter_bytes()):
                if not data:
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if event == "output":
                    if isinstance(obj.get("response"), str):
                        content.append(obj["response"])
                    if isinstance(obj.get("reasoning_content"), str):
                        reasoning.append(obj["reasoning_content"])
                    calls = obj.get("tool_calls")
                    if isinstance(calls, list):
                        for call in calls:
                            if isinstance(call, dict):
                                _merge_tool_call(slots, call)
                elif event == "token_usage":
                    usage = obj
                elif event == "done":
                    finish = str(obj.get("finish_reason") or finish)
                elif event == "error":
                    raise self._classify_error(None, self._error_code(obj), str(obj.get("message") or obj.get("error") or ""))
        finally:
            await reply.aclose()

        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if slots:
            message["tool_calls"] = [slots[idx] for idx in sorted(slots)]
        result: dict[str, Any] = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        }
        if usage:
            result["usage"] = usage
        return result

    # ---------- Work ----------

    def _work_host(self) -> str:
        return str(self.options.get("work_host") or WORK_HOST).rstrip("/")

    def _last_user_prompt(self, payload: dict[str, Any]) -> str:
        messages = payload.get("messages") or []
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [str(part.get("text") or "") for part in content if isinstance(part, dict)]
                if "".join(parts).strip():
                    return "".join(parts)
        return ""

    def _work_task_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = str(payload.get("model") or "").strip() or DEFAULT_WORK_MODEL
        prompt = self._last_user_prompt(payload)
        conv_id = str(payload.get("conversation_id") or uuid.uuid4())
        session_id = str(payload.get("session_id") or uuid.uuid4())
        query = json.dumps([{"type": "text", "data": {"content": prompt}}], ensure_ascii=False)
        return {
            "conversation_id": conv_id,
            "session_id": session_id,
            "user_id": self._uid(),
            "device_id": self._device_id(),
            "agent_type": FUNCTION,
            "model_name": model if model.endswith("__dev") else f"{model}__dev",
            "config_name": model,
            "ide_version": WORK_VERSION,
            "version_code": int(WORK_VERSION_CODE),
            "mode_type": 1,
            "plugin_channel": "stable",
            "history_id_list": [],
            "user_input": {
                "id": str(uuid.uuid4()),
                "query": query,
                "messages": [{"role": "user", "content": prompt}],
            },
        }

    async def _work_chat(self, payload: dict[str, Any], *, stream: bool):
        mode = str(self.options.get("work_mode") or "stream")
        if mode == "stateful":
            return await self._work_stateful(payload, stream=stream)
        return await self._work_native(payload, stream=stream)

    async def _work_native(self, payload: dict[str, Any], *, stream: bool):
        if not self._jwt():
            raise ProviderError(401, "Trae 未配置 JWT")
        model = str(payload.get("model") or "").strip() or DEFAULT_WORK_MODEL
        body = self._work_task_payload(payload)
        client = self._client(self._work_headers(chat_app_id=True, stream=True), host=self._work_host())
        reply = await client.forward(EP_CREATE_TASK, body, stream=True)
        if reply.status_code >= 400:
            text = await self._reply_text(reply)
            raise self._classify_error(reply.status_code, None, text)

        events = _sse_events(reply.iter_bytes())
        first: tuple[str, str] | None = None
        try:
            first = await events.__anext__()
        except StopAsyncIteration:
            first = None
        # Work 首帧就是错误（配额/配置）时，收掉连接降级 Solo
        if first is not None:
            _, data = first
            try:
                obj = json.loads(data) if data else {}
            except json.JSONDecodeError:
                obj = {}
            if isinstance(obj, dict):
                code = self._error_code(obj)
                if code in CREDIT_CODES:
                    await reply.aclose()
                    return await self._solo_chat(payload, stream=stream)

        async def chained() -> AsyncIterator[tuple[str, str]]:
            if first is not None:
                yield first
            async for item in events:
                yield item

        if stream:
            return _Reply(
                200,
                stream=self._render_work_stream(reply, chained(), model),
                content_type="text/event-stream",
            )
        data = await self._aggregate_work(reply, chained(), model)
        return _Reply(200, body=json.dumps(data, ensure_ascii=False).encode())

    def _work_event(self, event: str, data: str, obj: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        name = event or str(obj.get("event") or "")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            nested = obj.get("data")
            if isinstance(nested, dict):
                name = name or str(nested.get("event") or "")
                payload = nested.get("payload")
        if not isinstance(payload, dict) or not payload:
            # 有的 Work 网关帧是平的：字段直接铺在顶层，没有 payload 包装
            payload = obj
        return name, payload

    async def _render_work_stream(self, reply, events, model: str) -> AsyncIterator[bytes]:
        cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        pending_usage: dict[str, Any] | None = None
        full = ""
        try:
            async for event, data in events:
                if not data:
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                name, payload = self._work_event(event, data, obj)
                if name == "plan_item":
                    for text, key in (
                        (str(payload.get("reasoning_content") or ""), "reasoning_content"),
                        (str(payload.get("thought") or ""), "content"),
                        (str(((payload.get("tool_call_info") or {}).get("params") or {}).get("summary") or ""), "content"),
                    ):
                        if not text:
                            continue
                        delta_text, full = self._text_delta(text, full)
                        if delta_text:
                            yield _chunk(model, cmpl_id, {key: delta_text}, None, None)
                elif name == "output":
                    for choice in payload.get("choices") or []:
                        text = str((choice or {}).get("text") or "") if isinstance(choice, dict) else ""
                        if not text or text == "[]":
                            continue
                        delta_text, full = self._text_delta(text, full)
                        if delta_text:
                            yield _chunk(model, cmpl_id, {"content": delta_text}, None, None)
                elif name == "token_usage":
                    pending_usage = payload or None
                elif name == "done":
                    last = payload.get("last_assistant_response")
                    if isinstance(last, str) and last:
                        try:
                            arr = json.loads(last)
                        except json.JSONDecodeError:
                            arr = []
                        if isinstance(arr, list) and arr:
                            text = str(arr[0])
                            delta_text, full = self._text_delta(text, full)
                            if delta_text:
                                yield _chunk(model, cmpl_id, {"content": delta_text}, None, None)
                    yield _chunk(model, cmpl_id, {}, "stop", pending_usage)
                    yield b"data: [DONE]\n\n"
                    return
                elif name == "error":
                    err = self._classify_error(None, self._error_code(obj), str(obj.get("message") or ""))
                    yield f"event: error\ndata: {json.dumps({'error': {'message': err.message}}, ensure_ascii=False)}\n\n".encode()
                    raise err
            yield _chunk(model, cmpl_id, {}, "stop", pending_usage)
            yield b"data: [DONE]\n\n"
        finally:
            await reply.aclose()

    @staticmethod
    def _text_delta(text: str, full: str) -> tuple[str, str]:
        if not text:
            return "", full
        if text.startswith(full) and len(text) > len(full):
            return text[len(full):], text
        if text not in full:
            return text, full + text
        return "", full

    async def _aggregate_work(self, reply, events, model: str) -> dict[str, Any]:
        parts: list[str] = []
        usage: dict[str, Any] | None = None
        full = ""
        try:
            async for event, data in events:
                if not data:
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                name, payload = self._work_event(event, data, obj)
                if name == "plan_item":
                    for text in (
                        str(payload.get("thought") or ""),
                        str(((payload.get("tool_call_info") or {}).get("params") or {}).get("summary") or ""),
                    ):
                        delta_text, full = self._text_delta(text, full)
                        if delta_text:
                            parts.append(delta_text)
                elif name == "output":
                    for choice in payload.get("choices") or []:
                        text = str((choice or {}).get("text") or "") if isinstance(choice, dict) else ""
                        if text and text != "[]":
                            delta_text, full = self._text_delta(text, full)
                            if delta_text:
                                parts.append(delta_text)
                elif name == "token_usage":
                    usage = payload or None
                elif name == "done":
                    last = payload.get("last_assistant_response")
                    if isinstance(last, str) and last:
                        try:
                            arr = json.loads(last)
                        except json.JSONDecodeError:
                            arr = []
                        if isinstance(arr, list) and arr:
                            delta_text, full = self._text_delta(str(arr[0]), full)
                            if delta_text:
                                parts.append(delta_text)
                elif name == "error":
                    raise self._classify_error(None, self._error_code(obj), str(obj.get("message") or ""))
        finally:
            await reply.aclose()

        result: dict[str, Any] = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "".join(parts)}, "finish_reason": "stop"}
            ],
        }
        if usage:
            result["usage"] = usage
        return result

    async def _work_stateful(self, payload: dict[str, Any], *, stream: bool):
        """create_agent_task → workflow/start → query/sync_history_state 的轮询桥。

        上游该桥的请求体没有公开抓包，按契约字段尽力构造；未在真机验证时，
        失败走统一错误分类，不伪造结果。
        """
        if not self._jwt():
            raise ProviderError(401, "Trae 未配置 JWT")
        model = str(payload.get("model") or "").strip() or DEFAULT_WORK_MODEL
        task = self._work_task_payload(payload)
        client = self._client(self._work_headers(chat_app_id=True, stream=False), host=self._work_host())
        created = await client.post_json(EP_CREATE_TASK, task)
        ids = self._pick_ids(created)
        start = {
            "conversation_id": ids["conversation_id"],
            "session_id": ids["session_id"],
            "task_id": ids["task_id"],
            "user_input": task["user_input"],
        }
        started = await client.post_json(EP_WORKFLOW_START, start)
        limit = int(self.options.get("work_poll_limit") or 120)
        interval = float(self.options.get("work_poll_interval") or 0.5)
        text = ""
        usage: dict[str, Any] | None = None
        for _ in range(limit):
            state = await client.post_json(EP_QUERY_HISTORY, {"conversation_id": ids["conversation_id"], "session_id": ids["session_id"]})
            text = self._pick_text(state) or text
            usage = self._pick_usage(state) or usage
            if self._is_done(state):
                break
            sync = await client.post_json(EP_SYNC_HISTORY, {"conversation_id": ids["conversation_id"], "session_id": ids["session_id"]})
            text = self._pick_text(sync) or text
            usage = self._pick_usage(sync) or usage
            if self._is_done(sync):
                break
            await asyncio.sleep(interval)
        if not text:
            raise ProviderError(502, f"Trae Work 有状态桥未取到模型输出（workflow/start 返回：{str(started)[:120]}）")
        result: dict[str, Any] = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        }
        if usage:
            result["usage"] = usage
        if not stream:
            return _Reply(200, body=json.dumps(result, ensure_ascii=False).encode())
        cmpl_id = result["id"]

        async def body():
            yield _chunk(model, cmpl_id, {"content": text}, None, usage)
            yield _chunk(model, cmpl_id, {}, "stop", None)
            yield b"data: [DONE]\n\n"

        return _Reply(200, stream=body(), content_type="text/event-stream")

    @staticmethod
    def _pick_ids(obj: dict[str, Any]) -> dict[str, str]:
        source = obj.get("data") if isinstance(obj.get("data"), dict) else obj
        return {
            "conversation_id": str(source.get("conversation_id") or source.get("conv_id") or uuid.uuid4()),
            "session_id": str(source.get("session_id") or source.get("sess_id") or uuid.uuid4()),
            "task_id": str(source.get("task_id") or source.get("id") or uuid.uuid4()),
        }

    @staticmethod
    def _pick_text(obj: dict[str, Any]) -> str:
        for key in ("last_assistant_response", "content", "text", "answer"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                if key == "last_assistant_response":
                    try:
                        arr = json.loads(value)
                    except json.JSONDecodeError:
                        arr = []
                    if isinstance(arr, list) and arr:
                        return str(arr[0])
                return value
        messages = obj.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict) and isinstance(last.get("content"), str):
                return last["content"]
        return ""

    @staticmethod
    def _pick_usage(obj: dict[str, Any]) -> dict[str, Any] | None:
        usage = obj.get("token_usage") or obj.get("usage")
        return usage if isinstance(usage, dict) else None

    @staticmethod
    def _is_done(obj: dict[str, Any]) -> bool:
        for key in ("status", "state", "event"):
            if str(obj.get(key) or "").lower() in {"done", "completed", "finished", "success"}:
                return True
        return bool(obj.get("done"))

    # ---------- 对外 ----------

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        keys = self._keys()
        if not keys:
            raise ProviderError(401, "Trae 未配置 JWT（服务商页 api_key 或 options.extra_keys）")
        self._pool.set_weights(keys, self.options.get("key_weights"))
        sticky = sticky_key_from_payload(payload)
        channel = str(self.options.get("channel") or "solo").strip().lower()
        last: ProviderError | None = None

        for _ in range(self._attempts(len(keys))):
            pick = await self._pool.acquire(keys, sticky_key=sticky)
            if pick is None:
                raise self._all_keys_error(keys, last)
            if len(keys) > 1:
                logger.info("Trae 选号 cred=%s 规则=%s %s", pick.cred_id, pick.rule, pick.note or "-")
            handle = self._active_jwt.set(pick.key)
            try:
                if channel == "work":
                    result = await self._work_chat(payload, stream=stream)
                else:
                    result = await self._solo_chat(payload, stream=stream)
            except ProviderError as exc:
                last = exc
                if not self._rotatable(exc.status_code):
                    raise
                await self._note_failure(pick.key, exc.status_code, keys)
                continue
            else:
                stream_obj = getattr(result, "_stream", None) if stream else None
                if stream_obj is not None:
                    result._stream = self._tracked_stream(stream_obj, pick.key, keys, sticky)
                    return result
                await self._pool.report_success(pick.key)
                if sticky:
                    await self._pool.bind_sticky(sticky, pick.key)
                return result
            finally:
                self._active_jwt.reset(handle)
        raise last or self._all_keys_error(keys, None)

    async def fetch_models(self) -> list[dict[str, Any]]:
        if not self._jwt():
            raise ProviderError(401, "Trae 未配置 JWT（服务商页 api_key 或 options.extra_keys）")
        client = self._client(self._solo_headers())
        data = await client.post_json(EP_MODELS, {})
        items = data.get("models") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        models: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, str):
                models.append({"id": item})
            elif isinstance(item, dict):
                model_id = item.get("id") or item.get("config_name") or item.get("model")
                if model_id:
                    models.append({"id": str(model_id)})
        return models

    async def health(self) -> dict[str, Any]:
        keys = self._keys()
        if not keys:
            return {"connected": False, "error": "未配置 JWT"}
        channel = str(self.options.get("channel") or "solo")
        rotation = await self._pool.snapshot(keys)
        pick = await self._pool.acquire(keys)
        if pick is None:
            return {
                "connected": False,
                "error": "Trae 账号池全部冷却中",
                "channel": channel,
                "rotation": rotation,
                "cooling": rotation.get("cooling", 0),
            }
        handle = self._active_jwt.set(pick.key)
        try:
            models = await self.fetch_models()
            uid = self._uid()
        except ProviderError as exc:
            if self._rotatable(exc.status_code):
                await self._note_failure(pick.key, exc.status_code, keys)
            return {
                "connected": False,
                "error": exc.message,
                "channel": channel,
                "rotation": rotation,
                "cooling": rotation.get("cooling", 0),
            }
        finally:
            self._active_jwt.reset(handle)
        await self._pool.report_success(pick.key)
        return {
            "connected": True,
            "channel": channel,
            "work_mode": str(self.options.get("work_mode") or "stream"),
            "models": len(models),
            "uid": uid,
            "rotation": rotation,
            "cooling": rotation.get("cooling", 0),
        }


def build(settings: Settings, base_url: str = "") -> TraeAdapter:
    return TraeAdapter(settings, base_url)
