"""上游没给 usage 时的兜底估算（默认关闭，口径写死并可回溯）。

只做一件事：把请求正文与响应正文折算成 prompt/completion token 数，交给
billing.settle 落成一笔可识别为「估算」的流水。估算结果绝不写进
usage_records，也不写 request_logs 的 token 汇总列——那两处只存上游真值。

口径 v1（ESTIMATE_VERSION 就是这条口径的版本号，note 里带上它）：
- 按 Unicode 码点计数：CJK 字符 1 个码点算 1 token，其余字符 4 个码点算 1 token，
  不足 4 个也向上取整到 1。
- 只取正文：system / prompt / input / messages 的 text、content、arguments 字段，
  请求 JSON 的键名、图片载荷不计入；响应侧只取可见正文与工具调用参数，reasoning 不计。
- 单请求上限：input 200000、output 100000 token，超出直接钳到上限。
- 估不出（正文为空、响应体不是合法 JSON、响应里没有任何可提取文本）返回 None，
  调用方必须保持 usage_missing，不许造 0 金额的假账。
"""

from __future__ import annotations

import json
from typing import Any

ESTIMATE_VERSION = "v1-chars-cjk1-latin4-cap200k-100k"
MAX_INPUT_TOKENS = 200_000
MAX_OUTPUT_TOKENS = 100_000
# 计数字符预算：上限 token × 4 是最费字符的情形（非 CJK），先截断再计数
_INPUT_CHAR_BUDGET = MAX_INPUT_TOKENS * 4
_OUTPUT_CHAR_BUDGET = MAX_OUTPUT_TOKENS * 4
_TEXT_KEYS = ("text", "content", "arguments")
# 图片/音频等二进制载荷字段：命中即整段跳过，避免把 base64 算成 token
_BINARY_KEYS = frozenset({"image_url", "image", "input_image", "input_audio", "source", "b64_json"})


def _is_cjk(ch: str) -> bool:
    """CJK 与日韩文字按 1 码点 1 token 估算（含全角标点与谚文）。"""
    code = ord(ch)
    return (
        0x1100 <= code <= 0x11FF  # Hangul Jamo
        or 0x2E80 <= code <= 0x2EFF  # CJK radicals
        or 0x3000 <= code <= 0x303F  # CJK punctuation
        or 0x3040 <= code <= 0x30FF  # Hiragana / Katakana
        or 0x3400 <= code <= 0x4DBF  # CJK ext A
        or 0x4E00 <= code <= 0x9FFF  # CJK unified
        or 0xAC00 <= code <= 0xD7AF  # Hangul syllables
        or 0xF900 <= code <= 0xFAFF  # compatibility ideographs
        or 0xFF00 <= code <= 0xFFEF  # fullwidth forms
        or 0x20000 <= code <= 0x2FA1F  # CJK ext B+
    )


def count_tokens(text: str, limit: int) -> int:
    """按口径 v1 计数，超过 limit 立即返回 limit（字数再多也不影响金额上限）。"""
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            other += 1
        if cjk + (other + 3) // 4 > limit:
            return limit
    return min(cjk + (other + 3) // 4, limit)


def _collect(node: Any, parts: list[str], budget: list[int], depth: int = 0) -> None:
    """只抓 text/content/arguments，遇到二进制字段整段跳过。"""
    if budget[0] <= 0 or depth > 8:
        return
    if isinstance(node, str):
        take = node[: budget[0]]
        if take:
            parts.append(take)
            budget[0] -= len(take)
        return
    if isinstance(node, list):
        for item in node:
            _collect(item, parts, budget, depth + 1)
        return
    if not isinstance(node, dict):
        return
    if any(key in node for key in _BINARY_KEYS):
        return
    for key in _TEXT_KEYS:
        if key in node:
            _collect(node[key], parts, budget, depth + 1)


def _join(parts: list[str]) -> str | None:
    text = "\n".join(parts)
    return text if text.strip() else None


def request_text(payload: Any) -> str | None:
    """请求侧正文：system / prompt / input / messages。估不出返回 None。"""
    if not isinstance(payload, dict):
        return None
    parts: list[str] = []
    budget = [_INPUT_CHAR_BUDGET]
    for key in ("system", "prompt", "input", "messages"):
        if key in payload:
            _collect(payload[key], parts, budget)
    return _join(parts)


def response_text(body: Any) -> str | None:
    """非流式响应正文：OpenAI chat / Anthropic messages / Responses 三种形状。"""
    if isinstance(body, (bytes, bytearray)):
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
    else:
        data = body
    if not isinstance(data, dict):
        return None
    parts: list[str] = []
    budget = [_OUTPUT_CHAR_BUDGET]
    # output_text 是 SDK 便利字段，和 output[] 同时出现时会重复计数，只取一处
    if not data.get("output"):
        _collect(data.get("output_text"), parts, budget)
    _collect(data.get("content"), parts, budget)
    for choice in data.get("choices") or []:
        if isinstance(choice, dict):
            _collect(choice.get("message"), parts, budget)
            _collect(choice.get("text"), parts, budget)
    for item in data.get("output") or []:
        if isinstance(item, dict):
            _collect(item.get("content"), parts, budget)
            _collect(item.get("arguments"), parts, budget)
    return _join(parts)


def _combine(payload: Any, output: str | None) -> dict[str, int] | None:
    source = request_text(payload)
    if source is None or output is None:
        return None
    prompt = count_tokens(source, MAX_INPUT_TOKENS)
    completion = count_tokens(output, MAX_OUTPUT_TOKENS)
    if prompt <= 0 and completion <= 0:
        return None
    return {"prompt_tokens": prompt, "completion_tokens": completion}


def estimate_from_body(payload: Any, body: Any) -> dict[str, int] | None:
    """非流式：请求正文 + 响应体 → 估算 usage；任一侧取不到正文即 None。"""
    return _combine(payload, response_text(body))


def estimate_from_text(payload: Any, output: str | None) -> dict[str, int] | None:
    """流式：调用方先把 SSE 正文攒成文本，再走同一口径。"""
    return _combine(payload, output)


class StreamText:
    """只在不落 usage 的流式响应里攒可见正文；默认关闭时不会创建实例。"""

    __slots__ = ("_parts", "_budget", "_pending")
    _MAX_LINE_BYTES = 1_000_000

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._budget = [_OUTPUT_CHAR_BUDGET]
        self._pending = b""

    def _handle_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        raw = line[5:].strip()
        if not raw or raw == b"[DONE]":
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(data, dict):
            return
        budget = self._budget
        for choice in data.get("choices") or []:
            if isinstance(choice, dict):
                _collect(choice.get("delta"), self._parts, budget)
                _collect(choice.get("message"), self._parts, budget)
                _collect(choice.get("text"), self._parts, budget)
        _collect(data.get("delta"), self._parts, budget)
        _collect(data.get("content"), self._parts, budget)

    def feed(self, chunk: bytes) -> None:
        if self._budget[0] <= 0 or not chunk:
            return
        # SSE 帧可能被切成多个 chunk：按行缓冲，半行留到下一次
        self._pending += chunk
        if len(self._pending) > self._MAX_LINE_BYTES:
            self._pending = self._pending[-self._MAX_LINE_BYTES:]
        *lines, self._pending = self._pending.split(b"\n")
        for line in lines:
            self._handle_line(line)

    def output(self) -> str | None:
        if self._pending:
            self._handle_line(self._pending)
            self._pending = b""
        return _join(self._parts)
