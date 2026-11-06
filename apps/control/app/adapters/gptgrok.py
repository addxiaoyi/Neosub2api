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
import logging
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from ..api_utils import exc_summary
from ..config import Settings
from .base import ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient
from .key_pool import COOLDOWN_STATUSES, DEFAULT_COOLDOWN_SEC, KeyPool
from .stream_util import (
    DONE, Reply, iter_json_lines, openai_chunk, openai_completion,
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

# 两家上游都没有原生 tools 字段，走提示词注入（行为对齐上游 gptGrok2api 的做法，
# 格式为本仓自定：行首 [tool_call] + 单行 JSON，避免与上游实现产生表达纠缠）
TOOL_MARK = "[tool_call] "

logger = logging.getLogger("unified.gptgrok")


def _stable_id(token: str, salt: str) -> str:
    digest = hashlib.md5(f"{salt}:{token}".encode()).hexdigest()
    return f"{digest[0:8]}-{digest[8:12]}-4{digest[13:16]}-{digest[16:20]}-{digest[20:32]}"


def _is_image_part(part: dict[str, Any]) -> bool:
    if str(part.get("type") or "") in ("image_url", "image", "input_image"):
        return True
    return isinstance(part.get("image_url"), (str, dict))


def _content_parts(content: Any) -> list[Any]:
    """content 拍平成有序 parts：文本进 str，图片块原样保留。"""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    parts: list[Any] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            if _is_image_part(item):
                parts.append(item)
            elif item.get("type") in ("text", "input_text", "output_text") and item.get("text") is not None:
                parts.append(str(item["text"]))
            elif isinstance(item.get("text"), str):
                parts.append(item["text"])
    return parts


def _has_image(payload: dict[str, Any]) -> bool:
    for item in payload.get("messages") or []:
        if not isinstance(item, dict):
            continue
        for part in _content_parts(item.get("content")):
            if isinstance(part, dict) and _is_image_part(part):
                return True
    return False


def _tool_choice_mode(tool_choice: Any) -> str:
    """tool_choice 归一成 auto / none / required / required:<函数名>。"""
    if tool_choice is None or tool_choice == "auto":
        return "auto"
    if tool_choice == "none":
        return "none"
    if tool_choice in ("required", "any"):
        return "required"
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") if isinstance(tool_choice.get("function"), dict) else tool_choice
        name = str(fn.get("name") or "").strip()
        if tool_choice.get("type") == "function" or name:
            return f"required:{name}" if name else "required"
    return "auto"


def _tool_brief(payload: dict[str, Any]) -> str:
    """把 tools / tool_choice 翻成一段拼进系统提示的工具说明；无工具或 none 时为空。"""
    mode = _tool_choice_mode(payload.get("tool_choice"))
    if mode == "none":
        return ""
    tools: list[dict[str, Any]] = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        tools.append({
            "name": name,
            "description": str(fn.get("description") or ""),
            "parameters": fn.get("parameters") or {},
        })
    if not tools:
        return ""
    lines = [
        "[available tools]",
        json.dumps({"tools": tools}, ensure_ascii=False, indent=2),
        "[tool call protocol]",
        f"When you need a tool, start a line with {TOOL_MARK}followed by JSON on the same line, e.g.:",
        TOOL_MARK + '{"name": "<name from the list above>", "arguments": {<object matching its parameters>}}',
        "One call per line, no code fences, no other text on those lines; "
        "emit the call line(s), then stop and wait for the tool results.",
        "Only use tool names from the list above.",
    ]
    if mode == "required":
        lines.append("You MUST call at least one tool in this turn.")
    elif mode.startswith("required:"):
        lines.append(f"You MUST call the tool {mode[len('required:'):]!r} in this turn.")
    return "\n".join(lines)


class _ToolCallScanner:
    """从流式文本里摘出 [tool_call] 行，其余原样当 content 下发。

    marker 只在行首识别；未见到换行的调用行一直扣着（flush 时再定夺），
    保证半截 JSON 不会漏进 content，也不用等整段回复收完才出工具事件。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_call = False
        self._line_start = True

    def feed(self, text: str) -> list[tuple[str, Any]]:
        self._buf += text
        return self._drain()

    def flush(self) -> list[tuple[str, Any]]:
        events = self._drain()
        if self._buf:
            if self._in_call:
                # 流结束时还没等到换行：解析得通就当工具调用，解析不通直接丢弃
                events.extend(self._finish(self._buf))
            else:
                events.append(("content", self._buf))
            self._buf = ""
            self._in_call = False
            self._line_start = True
        return events

    def _drain(self) -> list[tuple[str, Any]]:
        events: list[tuple[str, Any]] = []
        while self._buf:
            if self._in_call:
                nl = self._buf.find("\n")
                if nl < 0:
                    break
                line, self._buf = self._buf[:nl], self._buf[nl + 1:]
                self._in_call = False
                self._line_start = True
                events.extend(self._finish(line))
                continue
            if not self._line_start:
                nl = self._buf.find("\n")
                if nl < 0:
                    events.append(("content", self._buf))
                    self._buf = ""
                    break
                events.append(("content", self._buf[:nl + 1]))
                self._buf = self._buf[nl + 1:]
                self._line_start = True
                continue
            limit = min(len(self._buf), len(TOOL_MARK))
            matched = 0
            while matched < limit and self._buf[matched] == TOOL_MARK[matched]:
                matched += 1
            if matched == len(self._buf) < len(TOOL_MARK):
                break  # 可能是 marker 的开头，扣住等后续分片
            if matched == len(TOOL_MARK):
                self._buf = self._buf[len(TOOL_MARK):]
                self._in_call = True
                self._line_start = False
                continue
            if matched == 0:
                # 行首就不是 marker，本行不再可能起 marker，整行直通
                nl = self._buf.find("\n")
                if nl < 0:
                    events.append(("content", self._buf))
                    self._buf = ""
                    self._line_start = False
                    break
                events.append(("content", self._buf[:nl + 1]))
                self._buf = self._buf[nl + 1:]
                continue
            # 分叉点之前的字符确定是普通文本
            events.append(("content", self._buf[:matched]))
            self._buf = self._buf[matched:]
            self._line_start = False
        return events

    @staticmethod
    def _finish(line: str) -> list[tuple[str, Any]]:
        try:
            call = json.loads(line.strip())
        except json.JSONDecodeError:
            return []  # 非法/残缺调用行丢弃，不能把半截 JSON 漏成内容
        if not isinstance(call, dict) or not str(call.get("name") or "").strip():
            return []
        return [("tool", call)]


def _tool_call_item(index: int, call: dict[str, Any]) -> dict[str, Any]:
    arguments = call.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    return {
        "index": index,
        "id": str(call.get("id") or "") or f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {"name": str(call["name"]), "arguments": arguments},
    }


def _content_text(content: Any) -> str:
    return "".join(part for part in _content_parts(content) if isinstance(part, str))


def _anthropic_image_url(block: dict[str, Any]) -> str:
    source = block.get("source")
    if not isinstance(source, dict):
        return ""
    if source.get("type") == "base64":
        data = str(source.get("data") or "")
        return f"data:{source.get('media_type') or 'image/png'};base64,{data}" if data else ""
    if source.get("type") == "url":
        return str(source.get("url") or "")
    return ""


def _tool_result_text(block: dict[str, Any]) -> str:
    inner = block.get("content")
    if isinstance(inner, list):
        for part in inner:
            if isinstance(part, dict) and part.get("type") == "image":
                raise ProviderError(400, "tool_result 中的图片无法转换（OpenAI 协议的 tool 消息不支持图片）")
    return _content_text(inner)


def _tool_use_call(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(block.get("id") or "") or f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": str(block.get("name") or ""),
            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
        },
    }


def _tool_call_to_tool_use(call: dict[str, Any]) -> dict[str, Any]:
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    raw = str(fn.get("arguments") or "").strip()
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        parsed = {"_raw": raw}
    return {
        "type": "tool_use",
        "id": str(call.get("id") or "") or f"toolu_{uuid.uuid4().hex[:24]}",
        "name": str(fn.get("name") or ""),
        "input": parsed,
    }


def _tool_call_to_function_item(call: dict[str, Any]) -> dict[str, Any]:
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    return {
        "id": f"fc_{uuid.uuid4().hex[:24]}",
        "type": "function_call",
        "call_id": str(call.get("id") or "") or f"call_{uuid.uuid4().hex[:24]}",
        "name": str(fn.get("name") or ""),
        "arguments": str(fn.get("arguments") or ""),
        "status": "completed",
    }


def _responses_usage(usage: Any) -> dict[str, Any]:
    # usage 三态的映射分支：只搬上游确认过的键，缺项不补零
    if not isinstance(usage, dict) or not usage:
        return {}
    out: dict[str, Any] = {}
    if "prompt_tokens" in usage:
        out["input_tokens"] = int(usage["prompt_tokens"] or 0)
    if "completion_tokens" in usage:
        out["output_tokens"] = int(usage["completion_tokens"] or 0)
    if "total_tokens" in usage:
        out["total_tokens"] = int(usage["total_tokens"] or 0)
    return out


def _anthropic_usage(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict) or not usage:
        return {}
    out: dict[str, Any] = {}
    if "prompt_tokens" in usage:
        out["input_tokens"] = int(usage["prompt_tokens"] or 0)
    if "completion_tokens" in usage:
        out["output_tokens"] = int(usage["completion_tokens"] or 0)
    return out


def _anthropic_usage_head(usage: Any) -> dict[str, Any] | None:
    # message_start 真实起头形态：output 恒 0 起步，input 只在上游给过时才带
    if not isinstance(usage, dict) or not usage:
        return None
    if "prompt_tokens" in usage:
        return {"input_tokens": int(usage["prompt_tokens"] or 0), "output_tokens": 0}
    return {"output_tokens": 0}


def _anthropic_usage_tail(usage: Any) -> dict[str, Any] | None:
    # message_delta 只收输出侧，计费把首尾两段合并才是完整 usage
    if not isinstance(usage, dict) or "completion_tokens" not in usage:
        return None
    return {"output_tokens": int(usage["completion_tokens"] or 0)}


async def _openai_frames(reply: Reply) -> AsyncIterator[dict[str, Any]]:
    try:
        async for line in iter_json_lines(reply.iter_bytes()):
            if line == "[DONE]":
                return
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj
    finally:
        await reply.aclose()


def _frame_usage(obj: dict[str, Any]) -> dict[str, Any] | None:
    usage = obj.get("usage")
    return usage if isinstance(usage, dict) and usage else None


def _frame_has_payload(obj: dict[str, Any]) -> bool:
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            continue
        text = delta.get("content")
        if isinstance(text, str) and text:
            return True
        if delta.get("tool_calls"):
            return True
    return False


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
        capabilities=stub_capabilities(chat=True, models=True, responses=True, messages=True),
        protocols=("openai.chat.completions", "openai.responses", "anthropic.messages"),
        readiness="needs_config",
        requires=(
            "sentinel_token（须用户自备：本地无法生成，由官方 "
            "/backend-api/sentinel/chat-requirements 换取后导入 options.sentinel_token；"
            "缺失时上游可能 403，导入后发一条 gpt-* 请求不再 403 即有效）",
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
        channel = str(options.get("channel") or "").strip().lower()
        missing: list[str] = []
        if not has_credential:
            missing.append("api_key（ChatGPT / Grok 会话凭证；服务商页 api_key 或 options.extra_keys）")
        if credential_problem:
            missing.append(credential_problem)
        # Grok 通道不走 ChatGPT sentinel：显式 channel=grok 时只看凭证
        if channel != "grok" and not str(options.get("sentinel_token") or "").strip():
            missing.append(
                "sentinel_token（ChatGPT 通道必需：由 /backend-api/sentinel/chat-requirements "
                "换取后导入 options.sentinel_token；缺失时上游可能 403；channel=grok 时不需要）"
            )
        if missing:
            return "needs_config", tuple(missing)
        return "ready", ()

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
    def _turn(payload: dict[str, Any], tools_text: str = "") -> tuple[str, list[dict[str, Any]]]:
        system: list[str] = []
        if tools_text:
            system.append(tools_text)
        turns: list[dict[str, Any]] = []
        for item in payload.get("messages") or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            parts = _content_parts(item.get("content"))
            text = "".join(part for part in parts if isinstance(part, str))
            if role in ("system", "developer"):
                if text:
                    system.append(text)
                continue
            if role in ("tool", "function"):
                # 带上函数名与 tool_call_id，模型才能把结果对上自己发起的那次调用
                tag = "[tool result"
                name = str(item.get("name") or "").strip()
                call_id = str(item.get("tool_call_id") or "").strip()
                if name:
                    tag += f" {name}"
                if call_id:
                    tag += f" {call_id}"
                text = f"{tag}] {text}" if text else f"{tag}]"
                turns.append({"role": "user", "content": text, "parts": [text]})
                continue
            if role == "assistant":
                calls = item.get("tool_calls")
                heads: list[str] = []
                for call in calls if isinstance(calls, list) else []:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                    heads.append(
                        f"[assistant tool_call {call.get('id') or ''}] "
                        f"{str(fn.get('name') or '')} {str(fn.get('arguments') or '')}".rstrip()
                    )
                if heads:
                    head = "\n".join(heads)
                    text = f"{head}\n{text}" if text else head
                    parts = [text]
            turns.append({"role": role if role in ("user", "assistant") else "user",
                          "content": text, "parts": parts or [text]})
        if not turns:
            raise ProviderError(400, "GPT/Grok 请求缺少 messages")
        if system:
            prefix = "[system] " + "\n".join(system) + "\n\n"
            if turns[0]["role"] == "user":
                first = turns[0]["parts"]
                if first and isinstance(first[0], str):
                    first[0] = prefix + first[0]
                else:
                    first.insert(0, prefix)
                turns[0]["content"] = "".join(part for part in first if isinstance(part, str))
            elif tools_text:
                # 首轮不是 user 时原逻辑会丢 system；带工具时不能把工具说明弄丢
                head = prefix.strip()
                turns.insert(0, {"role": "user", "content": head, "parts": [head]})
        return "\n\n".join(f"[{turn['role']}]: {turn['content']}" for turn in turns), turns

    def _chatgpt_body(self, payload: dict[str, Any], model: str, tools_text: str = "") -> dict[str, Any]:
        _, turns = self._turn(payload, tools_text)
        return {
            "action": "next",
            "messages": [
                {
                    "id": str(uuid.uuid4()),
                    "author": {"role": turn["role"]},
                    "content": {"content_type": "text", "parts": turn["parts"]},
                }
                for turn in turns
            ],
            "model": model,
            "parent_message_id": "client-created-root",
            "conversation_mode": {"kind": "primary_assistant"},
            "timezone_offset_min": -480,
            "history_and_training_disabled": False,
        }

    def _grok_body(self, payload: dict[str, Any], model: str, tools_text: str = "") -> dict[str, Any]:
        message, _ = self._turn(payload, tools_text)
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

    def _classify(self, status: int, text: str, channel: str) -> ProviderError:
        if status == 401:
            err = ProviderError(401, f"{channel} 凭证失效或未登录：{text[:160]}")
        elif status == 403:
            sentinel = str(self.options.get("sentinel_token") or "").strip()
            if sentinel:
                err = ProviderError(
                    403,
                    f"{channel} 403：已配置 sentinel_token 但上游仍拒绝（可能已过期或与会话不匹配），"
                    f"请重新换取后导入 options.sentinel_token：{text[:160]}",
                )
            else:
                err = ProviderError(
                    403,
                    f"{channel} 403：缺 sentinel_token（options.sentinel_token 未配置），"
                    f"上游可能因 sentinel 校验拒绝访问；请导入后重试：{text[:160]}",
                )
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
                last = ProviderError(502, f"{channel} 上游连接失败：{exc_summary(exc)}")
                await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
                continue
            if reply.status_code < 400:
                return reply, token
            err = self._classify(reply.status_code, await self._read_error(reply), channel)
            if reply.status_code in (401, 403):
                logger.warning("GPT/Grok %s 上游 %s：%s", channel, reply.status_code, err.message)
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
        scanner = _ToolCallScanner()
        tool_index = 0
        finish_reason = "stop"

        def pack(events: list[tuple[str, Any]]) -> list[bytes]:
            nonlocal tool_index, finish_reason
            chunks: list[bytes] = []
            for event, value in events:
                if event == "content":
                    chunks.append(openai_chunk(completion_id, model, created, {"content": value}))
                    continue
                finish_reason = "tool_calls"
                chunks.append(openai_chunk(
                    completion_id, model, created,
                    {"tool_calls": [_tool_call_item(tool_index, value)]},
                ))
                tool_index += 1
            return chunks

        try:
            async for delta, kind in self._deltas(raw, channel):
                if kind == "thinking":
                    yield openai_chunk(completion_id, model, created, {"reasoning_content": delta})
                    continue
                for chunk in pack(scanner.feed(delta)):
                    yield chunk
            for chunk in pack(scanner.flush()):
                yield chunk
        finally:
            if hasattr(raw, "aclose"):
                await raw.aclose()
        yield openai_chunk(completion_id, model, created, {}, finish_reason=finish_reason)
        yield DONE

    async def _collect(self, raw: AsyncIterator[bytes], channel: str, model: str) -> dict[str, Any]:
        content: list[str] = []
        reasoning: list[str] = []
        calls: list[dict[str, Any]] = []
        scanner = _ToolCallScanner()
        try:
            async for delta, kind in self._deltas(raw, channel):
                if kind == "thinking":
                    reasoning.append(delta)
                    continue
                for event, value in scanner.feed(delta):
                    (content if event == "content" else calls).append(value)
            for event, value in scanner.flush():
                (content if event == "content" else calls).append(value)
        finally:
            if hasattr(raw, "aclose"):
                await raw.aclose()
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = [
                _tool_call_item(index, call) for index, call in enumerate(calls)
            ]
        return openai_completion(
            f"chatcmpl-{uuid.uuid4().hex[:20]}",
            model,
            int(time.time()),
            message,
            finish_reason="tool_calls" if calls else "stop",
        )

    # ---------- 对外接口 ----------

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        model = str(payload.get("model") or CHATGPT_MODELS[0])
        channel = self._channel(model)
        if channel == "grok" and _has_image(payload):
            # Grok 的请求体只有文本 transcript，没有图片字段；宁可显式 400 也不静默丢图
            raise ProviderError(
                400,
                "该通道不支持图片输入：Grok 通道请求体只有文本 transcript，"
                "请改用 ChatGPT 通道模型（gpt-5*）或将图片转为文字描述",
            )
        tools_text = _tool_brief(payload)
        body = (
            self._chatgpt_body(payload, model, tools_text)
            if channel == "chatgpt"
            else self._grok_body(payload, model, tools_text)
        )
        reply, _ = await self._open(channel, body)
        if stream:
            return Reply(200, stream=self._stream(reply.iter_bytes(), channel, model), content_type="text/event-stream")
        data = await self._collect(reply.iter_bytes(), channel, model)
        return Reply(200, body=json.dumps(data, ensure_ascii=False).encode())

    # ---------- Responses / Anthropic 本地协议转换 ----------

    @staticmethod
    def _responses_to_chat(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("previous_response_id"):
            raise ProviderError(
                400,
                "gptGrok 通道不支持 previous_response_id（本通道无服务端会话状态），请把历史完整放进 input",
            )
        source = payload.get("input")
        if not source:
            raise ProviderError(400, "Responses 请求缺少 input")
        messages: list[dict[str, Any]] = []
        instructions = payload.get("instructions")
        if instructions:
            messages.append({"role": "system", "content": _content_text(instructions)})
        if isinstance(source, str):
            messages.append({"role": "user", "content": source})
        elif isinstance(source, list):
            for item in source:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "function_call":
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": str(item.get("call_id") or "") or f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": {
                                "name": str(item.get("name") or ""),
                                "arguments": str(item.get("arguments") or ""),
                            },
                        }],
                    })
                    continue
                if item.get("type") == "function_call_output":
                    output = item.get("output")
                    if isinstance(output, str):
                        text = output
                    elif isinstance(output, list):
                        text = _content_text(output)
                    elif output is None:
                        text = ""
                    else:
                        text = json.dumps(output, ensure_ascii=False)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(item.get("call_id") or ""),
                        "content": text,
                    })
                    continue
                role = str(item.get("role") or "user")
                content = item.get("content")
                if isinstance(content, str):
                    messages.append({"role": role, "content": content})
                    continue
                if not isinstance(content, list):
                    messages.append({"role": role, "content": _content_text(content)})
                    continue
                texts: list[str] = []
                images: list[dict[str, Any]] = []
                for part in content:
                    if isinstance(part, str):
                        texts.append(part)
                        continue
                    if not isinstance(part, dict):
                        continue
                    kind = part.get("type")
                    if kind in ("input_text", "output_text", "text"):
                        texts.append(str(part.get("text") or ""))
                    elif kind in ("input_image", "image_url"):
                        url = part.get("image_url")
                        if isinstance(url, dict):
                            url = url.get("url")
                        url = str(url or "")
                        if not url:
                            raise ProviderError(400, "图片缺少 image_url，无法转换")
                        images.append({"type": "image_url", "image_url": {"url": url}})
                if images:
                    parts = ([{"type": "text", "text": "".join(texts)}] if texts else []) + images
                    messages.append({"role": role, "content": parts})
                else:
                    messages.append({"role": role, "content": "".join(texts)})
        out: dict[str, Any] = {"model": payload.get("model"), "messages": messages}
        limit = payload.get("max_output_tokens")
        if isinstance(limit, int) and limit > 0:
            out["max_tokens"] = limit
        if payload.get("temperature") is not None:
            out["temperature"] = payload["temperature"]
        if payload.get("top_p") is not None:
            out["top_p"] = payload["top_p"]
        choice = payload.get("tool_choice")
        if isinstance(choice, str):
            out["tool_choice"] = choice
        elif isinstance(choice, dict) and choice.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": str(choice["name"])}}
        tools = payload.get("tools")
        if isinstance(tools, list) and tools:
            mapped = []
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                # Responses 的 function 工具是 name/parameters 同级的扁平形状，Chat 体要嵌一层 function
                if "name" in tool and "parameters" in tool:
                    fn = {
                        key: tool[key]
                        for key in ("name", "description", "parameters", "strict")
                        if key in tool
                    }
                    mapped.append({"type": "function", "function": fn})
                else:
                    mapped.append(tool)
            if mapped:
                out["tools"] = mapped
        return out

    @staticmethod
    def _anthropic_to_chat(payload: dict[str, Any]) -> dict[str, Any]:
        raw = payload.get("messages")
        if not isinstance(raw, list) or not raw:
            raise ProviderError(400, "Anthropic 请求缺少 messages")
        messages: list[dict[str, Any]] = []
        system = payload.get("system")
        if system:
            messages.append({"role": "system", "content": _content_text(system)})
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            content = item.get("content")
            if isinstance(content, str):
                messages.append({"role": role, "content": content})
                continue
            blocks = content if isinstance(content, list) else []
            if role == "assistant":
                texts: list[str] = []
                calls: list[dict[str, Any]] = []
                for block in blocks:
                    if isinstance(block, str):
                        texts.append(block)
                        continue
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        texts.append(str(block.get("text") or ""))
                    elif block.get("type") == "tool_use":
                        calls.append(_tool_use_call(block))
                message: dict[str, Any] = {"role": "assistant", "content": "".join(texts)}
                if calls:
                    message["tool_calls"] = calls
                messages.append(message)
                continue
            texts = []
            images: list[dict[str, Any]] = []
            tool_messages: list[dict[str, Any]] = []
            for block in blocks:
                if isinstance(block, str):
                    texts.append(block)
                    continue
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    texts.append(str(block.get("text") or ""))
                elif kind == "tool_result":
                    tool_messages.append({
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id") or ""),
                        "name": str(block.get("name") or ""),
                        "content": _tool_result_text(block),
                    })
                elif kind == "image":
                    url = _anthropic_image_url(block)
                    if not url:
                        raise ProviderError(400, "图片 source 无法识别（支持 base64 与 url 两种形态）")
                    images.append({"type": "image_url", "image_url": {"url": url}})
            messages.extend(tool_messages)
            # 纯 tool_result 轮次由 tool 消息承载，不再补一条空 user 消息
            if tool_messages and not texts and not images:
                continue
            if images:
                parts = ([{"type": "text", "text": "".join(texts)}] if texts else []) + images
                messages.append({"role": role, "content": parts})
            else:
                messages.append({"role": role, "content": "".join(texts)})
        out: dict[str, Any] = {"model": payload.get("model"), "messages": messages}
        if payload.get("max_tokens") is not None:
            out["max_tokens"] = payload["max_tokens"]
        if payload.get("temperature") is not None:
            out["temperature"] = payload["temperature"]
        if payload.get("top_p") is not None:
            out["top_p"] = payload["top_p"]
        choice = payload.get("tool_choice")
        if isinstance(choice, dict):
            kind = choice.get("type")
            if kind == "auto":
                out["tool_choice"] = "auto"
            elif kind == "any":
                out["tool_choice"] = "required"
            elif kind == "tool" and choice.get("name"):
                out["tool_choice"] = {"type": "function", "function": {"name": str(choice["name"])}}
        tools = payload.get("tools")
        if isinstance(tools, list) and tools:
            mapped = []
            for tool in tools:
                if not isinstance(tool, dict) or not tool.get("name"):
                    continue
                # Anthropic 工具是 input_schema 内嵌形状，Chat 体收进 function.parameters
                schema = tool.get("input_schema") or tool.get("parameters")
                mapped.append({
                    "type": "function",
                    "function": {
                        "name": str(tool["name"]),
                        "description": str(tool.get("description") or ""),
                        "parameters": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
                    },
                })
            if mapped:
                out["tools"] = mapped
        return out

    async def _render_responses(self, reply: Reply, model: str) -> AsyncIterator[bytes]:
        resp_id = f"resp_{uuid.uuid4().hex[:20]}"
        item_id = f"msg_{uuid.uuid4().hex[:20]}"

        def event(name: str, data: dict[str, Any]) -> bytes:
            return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

        yield event("response.created", {
            "type": "response.created",
            "response": {
                "id": resp_id, "object": "response", "status": "in_progress",
                "model": model, "output": [],
            },
        })
        yield event("response.output_item.added", {
            "type": "response.output_item.added", "output_index": 0,
            "item": {"id": item_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []},
        })
        usage: dict[str, Any] = {}
        full: list[str] = []
        # function_call 输出项按 OpenAI tool_calls.index 建，与 message 项（index 0）并列
        fc_items: dict[int, dict[str, Any]] = {}
        fc_order: list[int] = []
        try:
            async for obj in _openai_frames(reply):
                seen = _frame_usage(obj)
                if seen:
                    usage = {**usage, **seen}
                for choice in obj.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        continue
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        full.append(text)
                        yield event("response.output_text.delta", {
                            "type": "response.output_text.delta", "item_id": item_id,
                            "output_index": 0, "content_index": 0, "delta": text,
                        })
                    for call in delta.get("tool_calls") or []:
                        if not isinstance(call, dict):
                            continue
                        idx = int(call.get("index") or 0)
                        fn = call.get("function") or {}
                        args = str(fn.get("arguments") or "")
                        item = fc_items.get(idx)
                        if item is None:
                            item = {
                                "id": f"fc_{uuid.uuid4().hex[:24]}",
                                "type": "function_call",
                                "call_id": str(call.get("id") or f"call_{uuid.uuid4().hex[:24]}"),
                                "name": str(fn.get("name") or ""),
                                "arguments": "",
                                "status": "in_progress",
                            }
                            fc_items[idx] = item
                            fc_order.append(idx)
                            yield event("response.output_item.added", {
                                "type": "response.output_item.added",
                                "output_index": fc_order.index(idx) + 1,
                                "item": dict(item),
                            })
                        if args:
                            item["arguments"] += args
                            yield event("response.function_call_arguments.delta", {
                                "type": "response.function_call_arguments.delta",
                                "item_id": item["id"],
                                "output_index": fc_order.index(idx) + 1,
                                "delta": args,
                            })
        finally:
            await reply.aclose()
        yield event("response.output_text.done", {
            "type": "response.output_text.done", "item_id": item_id,
            "output_index": 0, "content_index": 0, "text": "".join(full),
        })
        for position, idx in enumerate(fc_order, start=1):
            item = fc_items[idx]
            yield event("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": item["id"], "output_index": position,
                "name": item["name"], "arguments": item["arguments"],
            })
            yield event("response.output_item.done", {
                "type": "response.output_item.done", "output_index": position,
                "item": {**item, "status": "completed"},
            })
        output: list[dict[str, Any]] = [{
            "id": item_id, "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "".join(full), "annotations": []}],
        }]
        output.extend({**fc_items[idx], "status": "completed"} for idx in fc_order)
        response: dict[str, Any] = {
            "id": resp_id, "object": "response", "status": "completed", "model": model,
            "output": output,
        }
        mapped = _responses_usage(usage)
        if mapped:
            response["usage"] = mapped
        yield event("response.completed", {"type": "response.completed", "response": response})

    async def _render_anthropic(self, reply: Reply, model: str) -> AsyncIterator[bytes]:
        msg_id = f"msg_{uuid.uuid4().hex[:20]}"

        def event(name: str, data: dict[str, Any]) -> bytes:
            return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

        usage: dict[str, Any] = {}
        started = False
        start_bare = False

        def start_frame() -> bytes:
            # message_start 等开头的 usage 落定再发：input 侧赶不上就由 message_delta 兜回计费
            nonlocal started, start_bare
            started = True
            message: dict[str, Any] = {
                "id": msg_id, "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None,
            }
            head = _anthropic_usage_head(usage)
            if head is None:
                start_bare = True
            else:
                message["usage"] = head
            return event("message_start", {"type": "message_start", "message": message})

        # 块按需开启：Anthropic 要求 content block 严格串行，切类型先关上一块再开新块
        block_index = -1
        block_kind = ""
        next_index = 0
        tool_blocks: dict[int, int] = {}
        try:
            async for obj in _openai_frames(reply):
                seen = _frame_usage(obj)
                if seen:
                    usage = {**usage, **seen}
                if not started and (usage or _frame_has_payload(obj)):
                    yield start_frame()
                if not started:
                    continue
                for choice in obj.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        continue
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        if block_kind != "text":
                            if block_kind:
                                yield event("content_block_stop", {"type": "content_block_stop", "index": block_index})
                            block_index = next_index
                            next_index += 1
                            block_kind = "text"
                            yield event("content_block_start", {
                                "type": "content_block_start", "index": block_index,
                                "content_block": {"type": "text", "text": ""},
                            })
                        yield event("content_block_delta", {
                            "type": "content_block_delta", "index": block_index,
                            "delta": {"type": "text_delta", "text": text},
                        })
                    for call in delta.get("tool_calls") or []:
                        if not isinstance(call, dict):
                            continue
                        idx = int(call.get("index") or 0)
                        fn = call.get("function") or {}
                        if idx not in tool_blocks:
                            if block_kind:
                                yield event("content_block_stop", {"type": "content_block_stop", "index": block_index})
                            block_index = next_index
                            next_index += 1
                            tool_blocks[idx] = block_index
                            block_kind = "tool"
                            yield event("content_block_start", {
                                "type": "content_block_start", "index": block_index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": str(call.get("id") or f"toolu_{uuid.uuid4().hex[:16]}"),
                                    "name": str(fn.get("name") or ""),
                                    "input": {},
                                },
                            })
                        arguments = fn.get("arguments")
                        if arguments and block_index == tool_blocks[idx]:
                            yield event("content_block_delta", {
                                "type": "content_block_delta", "index": block_index,
                                "delta": {"type": "input_json_delta", "partial_json": str(arguments)},
                            })
        finally:
            await reply.aclose()
        if not started:
            yield start_frame()
        if block_kind:
            yield event("content_block_stop", {"type": "content_block_stop", "index": block_index})
        else:
            # 全程无输出也保留一个空文本块，与既有空内容行为一致
            yield event("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""},
            })
            yield event("content_block_stop", {"type": "content_block_stop", "index": 0})
        delta_frame: dict[str, Any] = {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use" if tool_blocks else "end_turn", "stop_sequence": None},
        }
        usage_out = _anthropic_usage_tail(usage) or {}
        if start_bare and "prompt_tokens" in usage:
            # message_start 没赶上 usage，input 侧并进 message_delta，计费合并才不丢输入
            usage_out.setdefault("input_tokens", int(usage["prompt_tokens"] or 0))
        if usage_out:
            delta_frame["usage"] = usage_out
        yield event("message_delta", delta_frame)
        yield event("message_stop", {"type": "message_stop"})

    async def responses(self, payload: dict[str, Any], *, stream: bool) -> Reply:
        chat = self._responses_to_chat(payload)
        model = str(chat.get("model") or CHATGPT_MODELS[0])
        reply = await self.chat_completion(chat, stream=stream)
        if stream:
            return Reply(200, stream=self._render_responses(reply, model), content_type="text/event-stream")
        data = json.loads(reply.body)
        message = data["choices"][0]["message"]
        text = str(message.get("content") or "")
        tool_calls = [call for call in (message.get("tool_calls") or []) if isinstance(call, dict)]
        output: list[dict[str, Any]] = [{
            "type": "message",
            "role": "assistant",
            "index": 0,
            "content": [{"type": "output_text", "text": text}],
        }]
        output.extend(_tool_call_to_function_item(call) for call in tool_calls)
        body: dict[str, Any] = {
            "id": f"resp_{uuid.uuid4().hex[:20]}",
            "object": "response",
            "created_at": int(time.time()),
            "model": model,
            "output": output,
            "status": "completed",
        }
        usage = _responses_usage(data.get("usage"))
        if usage:
            body["usage"] = usage
        return Reply(200, body=json.dumps(body, ensure_ascii=False).encode())

    async def messages(self, payload: dict[str, Any], *, stream: bool) -> Reply:
        chat = self._anthropic_to_chat(payload)
        model = str(chat.get("model") or CHATGPT_MODELS[0])
        reply = await self.chat_completion(chat, stream=stream)
        if stream:
            return Reply(200, stream=self._render_anthropic(reply, model), content_type="text/event-stream")
        data = json.loads(reply.body)
        message = data["choices"][0]["message"]
        text = str(message.get("content") or "")
        tool_calls = [call for call in (message.get("tool_calls") or []) if isinstance(call, dict)]
        content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
        content.extend(_tool_call_to_tool_use(call) for call in tool_calls)
        body: dict[str, Any] = {
            "id": f"msg_{uuid.uuid4().hex[:20]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": "tool_use" if tool_calls else "end_turn",
            "stop_sequence": None,
        }
        usage = _anthropic_usage(data.get("usage"))
        if usage:
            body["usage"] = usage
        return Reply(200, body=json.dumps(body, ensure_ascii=False).encode())

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
