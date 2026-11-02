"""计费旁路：用量结构直通记录，不重算、不定价。

M2 只负责把上游返回的 usage 对象原样落库（usage_records.usage_json），
倍率/定价/对账留给 M3 的 sub2api sidecar；record_usage 就是留给它的接口。
"""

import json
import sqlite3
from typing import Any, Iterator


def record_usage(
    conn: sqlite3.Connection,
    *,
    key_id: int | None,
    key_name: str,
    provider: str,
    model: str,
    status: int,
    stream: bool,
    usage: dict[str, Any] | None,
    latency_ms: int,
    ts: int,
) -> None:
    # 匿名/失效密钥没有归属，显式写 NULL 而不是 0；聚合侧统一用 COALESCE 分桶
    normalized = int(key_id) if key_id is not None else None
    conn.execute(
        "INSERT INTO usage_records(ts, key_id, key_name, provider, model, status, stream, usage_json, latency_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            ts,
            normalized,
            key_name,
            provider,
            model,
            status,
            1 if stream else 0,
            json.dumps(usage, ensure_ascii=False) if usage else "",
            latency_ms,
        ),
    )


def usage_by_key(conn: sqlite3.Connection, *, since: int = 0) -> list[dict[str, Any]]:
    """按 key 归因用量。key_id 为 NULL 的记录单独归到「未归属」，不参与整数主键运算。"""
    rows = conn.execute(
        "SELECT CASE WHEN key_id IS NULL THEN NULL ELSE key_id END AS key_id, "
        "COALESCE(NULLIF(key_name, ''), CASE WHEN key_id IS NULL THEN '未归属' ELSE '已删除密钥' END) AS key_name, "
        "COUNT(*) AS requests "
        "FROM usage_records WHERE ts >= ? "
        "GROUP BY key_id, key_name ORDER BY requests DESC",
        (since,),
    ).fetchall()
    return [dict(row) for row in rows]


def _sse_objects(chunk: bytes) -> Iterator[dict[str, Any]]:
    """逐行解析 SSE 的 data: 负载，解析不成 JSON 的行直接丢掉。"""
    for line in chunk.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == b"[DONE]":
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict):
            yield data


def usage_in_chunk(chunk: bytes) -> dict[str, Any] | None:
    """一个 SSE 分片里的 usage：OpenAI 放顶层，Anthropic 放 message.usage / 顶层。

    必须先解析 JSON 再判断，不能靠正文里出现 "usage" 字样猜。
    """
    found: dict[str, Any] | None = None
    for data in _sse_objects(chunk):
        usage = data.get("usage")
        if not isinstance(usage, dict) or not usage:
            message = data.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict) and usage:
            # Anthropic 把 input_tokens 放 message_start、output_tokens 放 message_delta，合并才完整
            found = {**(found or {}), **usage}
    return found


def extract_usage(payload: bytes) -> dict[str, Any] | None:
    """非流式响应体里取原始 usage 对象（OpenAI 与 Anthropic 都叫 usage）。"""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    usage = data.get("usage") if isinstance(data, dict) else None
    return usage if isinstance(usage, dict) else None


def usage_from_stream(chunks: list[bytes]) -> dict[str, Any] | None:
    """流式响应里捞 usage：OpenAI 在末个 chunk（需 include_usage），Anthropic 在 message_start/message_delta。"""
    found: dict[str, Any] | None = None
    for chunk in chunks:
        part = usage_in_chunk(chunk)
        if part:
            found = {**(found or {}), **part}
    return found


def totals(usage: dict[str, Any] | None) -> tuple[int, int]:
    """从原始 usage 折算 prompt/completion 计数，供 request_logs 汇总列使用。"""
    if not usage:
        return 0, 0
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return prompt, completion
