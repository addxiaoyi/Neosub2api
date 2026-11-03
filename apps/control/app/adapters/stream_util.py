"""三家自研适配器共用的分帧工具：SSE / NDJSON 增量解析、内容拍平、统一响应壳。

上游分帧各不相同（Anthropic `event:`+`data:`、ChatGPT 纯 `data:`、Grok 裸 JSON 行），
这里只做逐行切分，保证下游拿到的是增量分片而不是整包拼接。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

DONE = b"data: [DONE]\n\n"


class Reply:
    """与 client.UpstreamReply 同形的轻量响应壳，供网关与测试统一消费。"""

    def __init__(
        self,
        status_code: int,
        *,
        body: bytes | None = None,
        stream: AsyncIterator[bytes] | None = None,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = {"content-type": content_type, **(headers or {})}
        self._stream = stream

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        generator = self._stream
        if generator is None:
            return
        try:
            async for chunk in generator:
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        generator = self._stream
        self._stream = None
        if generator is not None and hasattr(generator, "aclose"):
            try:
                await generator.aclose()
            except Exception:
                pass


async def iter_sse(raw: AsyncIterator[bytes]) -> AsyncIterator[tuple[str, str]]:
    """按 SSE 规则切帧，产出 (event, data)；空帧（heartbeat）直接跳过。"""
    buffer = ""
    event = ""
    data: list[str] = []
    async for chunk in raw:
        buffer += chunk.decode("utf-8", "ignore")
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


async def iter_json_lines(raw: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """逐行产出 JSON 文本；兼容 Grok 的裸 JSON 行与 SSE 的 `data:` 前缀。"""
    buffer = ""
    async for chunk in raw:
        buffer += chunk.decode("utf-8", "ignore")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line:
                yield line
    tail = buffer.strip()
    if tail:
        if tail.startswith("data:"):
            tail = tail[5:].strip()
        if tail:
            yield tail


def content_text(content: Any) -> str:
    """OpenAI / Anthropic 两种 content 形态都拍平成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in ("text", "input_text", "output_text") and item.get("text") is not None:
                    parts.append(str(item["text"]))
                elif isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return "".join(parts)
    return ""


def sse_frame(obj: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"


def sse_error(obj: dict[str, Any], *, event: str = "") -> bytes:
    """错误帧：沿用 data-only 的 SSE 约定；Anthropic 通道额外带 `event: error`。"""
    prefix = f"event: {event}\n".encode() if event else b""
    return prefix + sse_frame(obj)


def openai_chunk(
    completion_id: str,
    model: str,
    created: int,
    delta: dict[str, Any],
    *,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> bytes:
    body: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = usage
    return sse_frame(body)


def openai_completion(
    completion_id: str,
    model: str,
    created: int,
    message: dict[str, Any],
    *,
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = usage
    return body
