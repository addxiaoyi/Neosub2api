"""gptgrok 工具调用（提示词注入）与图片输入的契约测试。

请求侧：monkeypatch UnifiedApiClient.forward 记录 payload——它就是被序列化进
HTTP 请求体的最终 body（client.forward 的 json=payload）。
响应侧：fake_upstream 不会回吐工具行，所以换成回放构造好的上游帧验证解析。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

CONTROL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROL))

from app.adapters import gptgrok  # noqa: E402
from app.adapters.base import ProviderError  # noqa: E402
from app.adapters.stream_util import Reply  # noqa: E402
from app.config import Settings  # noqa: E402


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "unified.db",
        web_dir=tmp_path / "web",
        admin_username="admin",
        admin_password="test",
        session_ttl_sec=3600,
        cookie_secure=False,
        codebuddy_base="http://127.0.0.1:7899",
        codebuddy_api_key="",
        opencode_upstream_base="http://127.0.0.1:7899",
        grok_register_executor_url="",
        grok_register_max_concurrency=1,
        grok_register_max_workers=2,
        allow_anonymous_gateway=False,
        cors_origins=(),
    )


@pytest.fixture
def wire(monkeypatch):
    """记录实际发往上游的请求体，默认透传到真假上游（fake_base）。"""
    sent: list[dict[str, Any]] = []
    original = gptgrok.UnifiedApiClient.forward

    async def recording(self, path, payload=None, *, stream, method="POST", raw=None, accept=None):
        sent.append({"path": path, "payload": payload})
        return await original(self, path, payload, stream=stream, method=method, raw=raw, accept=accept)

    monkeypatch.setattr(gptgrok.UnifiedApiClient, "forward", recording)
    return sent


@pytest.fixture
def canned(monkeypatch):
    """替换上游为回放器：记录请求体，回放 state["reply"] 的原始字节流。"""
    state: dict[str, Any] = {"sent": [], "reply": b""}

    async def replay(self, path, payload=None, *, stream, method="POST", raw=None, accept=None):
        state["sent"].append({"path": path, "payload": payload})
        frame_bytes = state["reply"]

        async def gen():
            yield frame_bytes

        return Reply(200, stream=gen(), content_type="text/event-stream")

    monkeypatch.setattr(gptgrok.UnifiedApiClient, "forward", replay)
    return state


TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的当前气温",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
    },
}


def _chatgpt_sse(cumulative: list[str]) -> bytes:
    """ChatGPT 帧的 parts 是累积文本，逐帧给前缀。"""
    out = b""
    for i, text in enumerate(cumulative):
        status = "finished_successfully" if i == len(cumulative) - 1 else "in_progress"
        obj = {"message": {"author": {"role": "assistant"},
                           "content": {"content_type": "text", "parts": [text]},
                           "status": status}}
        out += b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"
    return out + b"data: [DONE]\n\n"


def _grok_lines(tokens: list[str]) -> bytes:
    out = b""
    for token in tokens:
        out += json.dumps({"result": {"response": {"token": token}}}, ensure_ascii=False).encode() + b"\n"
    out += json.dumps({"result": {"response": {"isSoftStop": True}, "finalMetadata": {}}}).encode() + b"\n"
    return out


def _frames(raw: bytes) -> list[dict]:
    frames: list[dict] = []
    for block in raw.decode("utf-8", "ignore").split("\n\n"):
        block = block.strip()
        if not block.startswith("data:"):
            continue
        payload = block[5:].strip()
        if payload == "[DONE]" or not payload:
            continue
        frames.append(json.loads(payload))
    return frames


def _delta_text(frames: list[dict], key: str) -> str:
    return "".join(frame["choices"][0]["delta"].get(key) or "" for frame in frames)


async def _drain(reply) -> bytes:
    out = b""
    async for chunk in reply.iter_bytes():
        out += chunk
    return out


# ----------------------------- 请求侧：tools 注入探针 -----------------------------


async def test_tools_reach_body_nonstream(tmp_path, fake_base, wire):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g")
    payload = {
        "model": "gpt-5",
        "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "北京今天天气"}],
        "tools": [TOOL],
    }
    reply = await adapter.chat_completion(payload, stream=False)
    assert reply.status_code == 200
    body = wire[-1]["payload"]
    sent = json.dumps(body, ensure_ascii=False)
    print("PROBE nonstream body:", sent)
    assert wire[-1]["path"] == "/backend-api/conversation"
    # 工具定义必须进入实际发出的 body
    assert "get_weather" in sent, "tools 未注入上游请求体"
    assert "city" in sent, "参数 schema 未注入上游请求体"


async def test_tools_reach_body_stream(tmp_path, fake_base, wire):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g")
    payload = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "北京今天天气"}],
        "tools": [TOOL],
    }
    reply = await adapter.chat_completion(payload, stream=True)
    raw = await _drain(reply)
    assert raw.endswith(b"data: [DONE]\n\n")
    sent = json.dumps(wire[-1]["payload"], ensure_ascii=False)
    print("PROBE stream body:", sent)
    assert "get_weather" in sent, "流式请求的 tools 未注入上游请求体"


async def test_tools_reach_body_grok_channel(tmp_path, fake_base, wire):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g")
    payload = {
        "model": "grok-4.20-fast",
        "messages": [{"role": "user", "content": "北京今天天气"}],
        "tools": [TOOL],
    }
    reply = await adapter.chat_completion(payload, stream=False)
    assert reply.status_code == 200
    sent = json.dumps(wire[-1]["payload"], ensure_ascii=False)
    print("PROBE grok body:", sent)
    assert wire[-1]["path"] == "/rest/app-chat/conversations/new"
    assert "get_weather" in sent, "Grok transcript 未注入工具说明"


async def test_multiturn_tool_result_keeps_context(tmp_path, fake_base, wire):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g")
    payload = {
        "model": "gpt-5",
        "messages": [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "北京今天天气"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_abc123",
                "type": "function",
                "function": {"name": "get_weather", "arguments": "{\"city\": \"北京\"}"},
            }]},
            {"role": "tool", "tool_call_id": "call_abc123", "content": "{\"temp_c\": 21, \"cond\": \"sunny\"}"},
        ],
        "tools": [TOOL],
    }
    reply = await adapter.chat_completion(payload, stream=False)
    assert reply.status_code == 200
    body = wire[-1]["payload"]
    sent = json.dumps(body, ensure_ascii=False)
    print("PROBE multiturn body:", sent)
    # 多轮回填：调用 id、函数名、结果载荷都要在 body 里可读
    assert "call_abc123" in sent, "tool_call_id 丢失"
    assert "temp_c" in sent, "tool 结果载荷丢失"
    parts_text = "\n".join(
        str(part) for msg in body["messages"] for part in msg["content"]["parts"]
    )
    assert '{"city": "北京"}' in parts_text, "assistant 发起的工具调用参数丢失"


# ----------------------------- 响应侧：tool_calls 解析 -----------------------------


def _tool_reply_text() -> tuple[list[str], str]:
    """返回（ChatGPT 累积帧列表，完整回复文本）；切分点故意落在 marker 中间。"""
    full = '让我查一下。\n[tool_call] {"name": "get_weather", "arguments": {"city": "北京"}}\n收到结果再回答。'
    cut = full.index("[tool_call]") + len("[tool_c")
    return [full[:cut], full[:len(full) - 3], full], full


async def test_stream_tool_call_frames(tmp_path, canned):
    cuts, full = _tool_reply_text()
    canned["reply"] = _chatgpt_sse(cuts)
    adapter = gptgrok.build(_settings(tmp_path), "http://127.0.0.1:9")
    adapter.configure(api_key="tok-g")
    reply = await adapter.chat_completion(
        {"model": "gpt-5", "messages": [{"role": "user", "content": "天气"}], "tools": [TOOL]},
        stream=True,
    )
    raw = await _drain(reply)
    frames = _frames(raw)
    content = _delta_text(frames, "content")
    assert "[tool_call]" not in content, "工具行漏进了 content"
    assert "让我查一下。" in content
    assert "收到结果再回答。" in content, "工具行之后的内容没有继续流式下发"

    deltas = [d for f in frames for d in (f["choices"][0]["delta"].get("tool_calls") or [])]
    assert deltas, "流式响应里没有 tool_calls 帧"
    joined = "".join(d["function"]["arguments"] for d in deltas)
    names = {d["function"]["name"] for d in deltas}
    assert names == {"get_weather"}
    assert json.loads(joined) == {"city": "北京"}, "arguments 拼装后不是合法 JSON"
    assert all(d["type"] == "function" and d["id"] for d in deltas)
    assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert raw.endswith(b"data: [DONE]\n\n")


async def test_nonstream_tool_call_response(tmp_path, canned):
    cuts, _ = _tool_reply_text()
    canned["reply"] = _chatgpt_sse(cuts)
    adapter = gptgrok.build(_settings(tmp_path), "http://127.0.0.1:9")
    adapter.configure(api_key="tok-g")
    reply = await adapter.chat_completion(
        {"model": "gpt-5", "messages": [{"role": "user", "content": "天气"}], "tools": [TOOL]},
        stream=False,
    )
    data = json.loads(reply.body)
    choice = data["choices"][0]
    message = choice["message"]
    assert "[tool_call]" not in (message.get("content") or "")
    calls = message.get("tool_calls") or []
    assert len(calls) == 1, f"非流式响应未解析出 tool_calls: {message}"
    call = calls[0]
    assert call["type"] == "function"
    assert call["id"].startswith("call_")
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "北京"}
    assert choice["finish_reason"] == "tool_calls"


async def test_grok_stream_tool_call(tmp_path, canned):
    canned["reply"] = _grok_lines([
        "查天气。\n",
        '[tool_call] {"name": "get_weather", "arguments": {"city": "SH"}}',
        "\n完成。",
    ])
    adapter = gptgrok.build(_settings(tmp_path), "http://127.0.0.1:9")
    adapter.configure(api_key="tok-g")
    reply = await adapter.chat_completion(
        {"model": "grok-4.20-fast", "messages": [{"role": "user", "content": "天气"}], "tools": [TOOL]},
        stream=True,
    )
    frames = _frames(await _drain(reply))
    content = _delta_text(frames, "content")
    assert "[tool_call]" not in content
    assert "完成。" in content
    deltas = [d for f in frames for d in (f["choices"][0]["delta"].get("tool_calls") or [])]
    assert deltas and deltas[0]["function"]["name"] == "get_weather"
    assert json.loads("".join(d["function"]["arguments"] for d in deltas)) == {"city": "SH"}
    assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"


async def test_tool_choice_modes(tmp_path, fake_base, wire):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g")
    base = {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}], "tools": [TOOL]}

    await adapter.chat_completion({**base, "tool_choice": "none"}, stream=False)
    assert "[available tools]" not in json.dumps(wire[-1]["payload"], ensure_ascii=False)

    await adapter.chat_completion({**base, "tool_choice": "required"}, stream=False)
    assert "MUST call" in json.dumps(wire[-1]["payload"], ensure_ascii=False)

    await adapter.chat_completion(
        {**base, "tool_choice": {"type": "function", "function": {"name": "get_weather"}}},
        stream=False,
    )
    assert "get_weather" in json.dumps(wire[-1]["payload"], ensure_ascii=False)


# ----------------------------- 图片输入 -----------------------------


IMAGE_MESSAGES = [{
    "role": "user",
    "content": [
        {"type": "text", "text": "这两张图里有什么"},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
    ],
}]


async def test_image_url_passes_through_on_chatgpt(tmp_path, fake_base, wire):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g")
    reply = await adapter.chat_completion(
        {"model": "gpt-5", "messages": IMAGE_MESSAGES}, stream=False
    )
    assert reply.status_code == 200
    body = wire[-1]["payload"]
    sent = json.dumps(body, ensure_ascii=False)
    print("PROBE image body:", sent)
    parts = body["messages"][0]["content"]["parts"]
    image_parts = [p for p in parts if isinstance(p, dict)]
    assert len(image_parts) == 2, f"图片 part 被丢弃: {parts}"
    urls = [p["image_url"]["url"] for p in image_parts]
    assert "https://example.com/a.png" in urls, "URL 形态图片丢失"
    assert any(u.startswith("data:image/png;base64,") for u in urls), "base64 形态图片丢失"
    assert "这两张图里有什么" in sent


async def test_image_rejected_explicitly_on_grok(tmp_path, fake_base, wire):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g")
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion(
            {"model": "grok-4.20-fast", "messages": IMAGE_MESSAGES}, stream=False
        )
    assert caught.value.status_code == 400
    assert "该通道不支持图片输入" in caught.value.message
    assert wire == [], "被拒绝的请求不应发往上游"


# ----------------------------- 回归：无 tools / 无图片时行为不变 -----------------------------


async def test_plain_body_unchanged_without_tools(tmp_path, fake_base, wire):
    adapter = gptgrok.build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="tok-g")
    reply = await adapter.chat_completion(
        {"model": "gpt-5", "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]},
        stream=False,
    )
    data = json.loads(reply.body)
    body = wire[-1]["payload"]
    print("PROBE plain body:", json.dumps(body, ensure_ascii=False))
    assert set(body) == {
        "action", "messages", "model", "parent_message_id",
        "conversation_mode", "timezone_offset_min", "history_and_training_disabled",
    }
    message = body["messages"][0]
    assert message["author"] == {"role": "user"}
    assert message["content"] == {"content_type": "text", "parts": ["[system] SYS\n\nhi"]}
    assert "[available tools]" not in json.dumps(body, ensure_ascii=False)
    assert "authorization=Bearer tok-g" in data["choices"][0]["message"]["content"]
    assert data["choices"][0]["finish_reason"] == "stop"


# ----------------------------- /v1/responses 与 /v1/messages 协议面 -----------------------------


def _events(raw: bytes) -> list[tuple[str, dict]]:
    """解析 renderer 输出的 `event:` + `data:` 双行 SSE。"""
    parsed: list[tuple[str, dict]] = []
    for block in raw.decode("utf-8", "ignore").split("\n\n"):
        if not block.strip():
            continue
        name, data = "", {}
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        parsed.append((name, data))
    return parsed


def _openai_stream(chunks: list[dict]) -> Reply:
    """把 OpenAI chunk 列表包成与 chat_completion 流式返回同形的流，供 renderer 直接回放。"""
    raw = b"".join(
        b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n"
        for chunk in chunks
    ) + b"data: [DONE]\n\n"

    async def gen():
        yield raw

    return Reply(200, stream=gen(), content_type="text/event-stream")


async def _rendered_events(render, chunks: list[dict]) -> list[tuple[str, dict]]:
    raw = b""
    async for chunk in render(_openai_stream(chunks), "gpt-5"):
        raw += chunk
    return _events(raw)


ANTHROPIC_TOOL = {
    "name": "get_weather",
    "description": "查询指定城市的当前气温",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "城市名"}},
        "required": ["city"],
    },
}


def _adapter(tmp_path: Path) -> gptgrok.GptGrokAdapter:
    adapter = gptgrok.build(_settings(tmp_path), "http://127.0.0.1:9")
    adapter.configure(api_key="tok-g")
    return adapter


# ----------------------------- Responses：非流式与事件序 -----------------------------


async def test_responses_nonstream_instructions_becomes_system(tmp_path, canned):
    canned["reply"] = _chatgpt_sse(["你好"])
    adapter = _adapter(tmp_path)
    payload = {"model": "gpt-5", "input": "hi", "instructions": "SYS"}
    chat = gptgrok.GptGrokAdapter._responses_to_chat(payload)
    assert chat["messages"][0] == {"role": "system", "content": "SYS"}

    reply = await adapter.responses(payload, stream=False)
    body = json.loads(reply.body)
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["type"] == "output_text"
    first = canned["sent"][0]["payload"]["messages"][0]["content"]["parts"][0]
    assert first == "[system] SYS\n\nhi", "instructions 未作为 system 前缀落进上游 transcript"


async def test_responses_stream_event_order_without_done(tmp_path, canned):
    canned["reply"] = _chatgpt_sse(["你", "你好"])
    adapter = _adapter(tmp_path)
    reply = await adapter.responses({"model": "gpt-5", "input": "讲个笑话"}, stream=True)
    raw = await _drain(reply)
    assert b"[DONE]" not in raw, "Responses 流不应下发 OpenAI 的 [DONE]"
    names = [name for name, _ in _events(raw)]
    assert names == [
        "response.created",
        "response.output_item.added",
        "response.output_text.delta", "response.output_text.delta",
        "response.output_text.done",
        "response.completed",
    ]
    assert _events(raw)[-1][1]["response"]["status"] == "completed"


# ----------------------------- Responses：usage 三态 -----------------------------


async def test_responses_stream_usage_mapped_from_upstream_frames(tmp_path):
    adapter = _adapter(tmp_path)
    chunks = [
        {"id": "c1", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"id": "c1", "choices": [{"index": 0, "delta": {"content": "晴 25度"}}]},
        {"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}},
    ]
    events = await _rendered_events(adapter._render_responses, chunks)
    completed = [data for name, data in events if name == "response.completed"][0]
    assert completed["response"]["usage"] == {
        "input_tokens": 7, "output_tokens": 4, "total_tokens": 11,
    }


async def test_responses_stream_usage_zero_kept_as_zero(tmp_path):
    adapter = _adapter(tmp_path)
    chunks = [
        {"id": "c1", "choices": [{"index": 0, "delta": {"content": "好"}}]},
        {"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
    ]
    events = await _rendered_events(adapter._render_responses, chunks)
    completed = [data for name, data in events if name == "response.completed"][0]
    assert completed["response"]["usage"] == {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
    }


async def test_responses_stream_usage_absent_when_upstream_silent(tmp_path, canned):
    canned["reply"] = _chatgpt_sse(["你好"])
    adapter = _adapter(tmp_path)
    reply = await adapter.responses({"model": "gpt-5", "input": "hi"}, stream=True)
    events = _events(await _drain(reply))
    completed = [data for name, data in events if name == "response.completed"][0]
    assert "usage" not in completed["response"], "上游没给 usage 时不得造零"


# ----------------------------- Responses：工具调用 -----------------------------


async def test_responses_nonstream_function_call_output(tmp_path, canned):
    cuts, _ = _tool_reply_text()
    canned["reply"] = _chatgpt_sse(cuts)
    adapter = _adapter(tmp_path)
    reply = await adapter.responses(
        {"model": "gpt-5", "input": "天气", "tools": [TOOL]}, stream=False
    )
    body = json.loads(reply.body)
    assert body["output"][0]["type"] == "message"
    call = body["output"][1]
    assert call["type"] == "function_call"
    assert call["call_id"].startswith("call_")
    assert isinstance(call["arguments"], str)
    assert json.loads(call["arguments"]) == {"city": "北京"}
    assert call["status"] == "completed"


async def test_responses_stream_function_call_events(tmp_path, canned):
    cuts, _ = _tool_reply_text()
    canned["reply"] = _chatgpt_sse(cuts)
    adapter = _adapter(tmp_path)
    reply = await adapter.responses(
        {"model": "gpt-5", "input": "天气", "tools": [TOOL]}, stream=True
    )
    events = _events(await _drain(reply))
    names = [name for name, _ in events]
    assert "response.output_item.added" in names
    added = [
        data for name, data in events
        if name == "response.output_item.added" and data["item"]["type"] == "function_call"
    ]
    assert added and added[0]["item"]["call_id"].startswith("call_")
    args_deltas = [data for name, data in events if name == "response.function_call_arguments.delta"]
    assert args_deltas
    assert json.loads("".join(item["delta"] for item in args_deltas)) == {"city": "北京"}
    completed = [data for name, data in events if name == "response.completed"][0]
    fc = completed["response"]["output"][1]
    assert fc["type"] == "function_call"
    assert json.loads(fc["arguments"]) == {"city": "北京"}


async def test_responses_previous_response_id_rejected(tmp_path, canned):
    adapter = _adapter(tmp_path)
    with pytest.raises(ProviderError) as caught:
        await adapter.responses(
            {"model": "gpt-5", "input": "hi", "previous_response_id": "resp_1"},
            stream=False,
        )
    assert caught.value.status_code == 400
    assert "previous_response_id" in caught.value.message
    assert canned["sent"] == [], "被拒绝的请求不应发往上游"


# ----------------------------- Responses：入站转换 -----------------------------


async def test_responses_tool_roundtrip_reaches_upstream(tmp_path, canned):
    canned["reply"] = _chatgpt_sse(["好的"])
    adapter = _adapter(tmp_path)
    payload = {
        "model": "gpt-5",
        "input": [
            {"role": "user", "content": "北京天气"},
            {"type": "function_call", "call_id": "call_01", "name": "get_weather",
             "arguments": '{"city": "北京"}'},
            {"type": "function_call_output", "call_id": "call_01", "output": "晴 25度"},
        ],
        "tools": [TOOL],
    }
    chat = gptgrok.GptGrokAdapter._responses_to_chat(payload)
    assert chat["messages"][1] == {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_01", "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "北京"}'}}],
    }
    assert chat["messages"][2] == {"role": "tool", "tool_call_id": "call_01", "content": "晴 25度"}

    await adapter.responses(payload, stream=False)
    sent = json.dumps(canned["sent"][0]["payload"], ensure_ascii=False)
    assert "[assistant tool_call call_01]" in sent, "assistant 工具调用未进上游 transcript"
    assert "[tool result call_01]" in sent, "tool 结果消息未进上游 transcript"
    assert "晴 25度" in sent


def test_responses_max_tokens_and_flat_tools_conversion():
    schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    out = gptgrok.GptGrokAdapter._responses_to_chat({
        "model": "gpt-5",
        "input": "hi",
        "max_output_tokens": 256,
        "tools": [{"type": "function", "name": "get_weather", "description": "查天气",
                   "parameters": schema}],
    })
    assert out["max_tokens"] == 256
    assert out["tools"] == [
        {"type": "function",
         "function": {"name": "get_weather", "description": "查天气", "parameters": schema}}
    ]


# ----------------------------- Responses：图片分流 -----------------------------


async def test_responses_image_channel_split(tmp_path, canned):
    canned["reply"] = _chatgpt_sse(["图里有只猫"])
    adapter = _adapter(tmp_path)
    payload = {"model": "gpt-5", "input": [
        {"role": "user", "content": [
            {"type": "input_text", "text": "看图"},
            {"type": "input_image", "image_url": "https://example.com/a.png"},
        ]},
    ]}
    await adapter.responses(dict(payload), stream=False)
    parts = canned["sent"][0]["payload"]["messages"][0]["content"]["parts"]
    assert any(
        isinstance(part, dict) and part["image_url"]["url"] == "https://example.com/a.png"
        for part in parts
    ), "GPT 通道 input_image 丢失"

    canned["sent"].clear()
    with pytest.raises(ProviderError) as caught:
        await adapter.responses({**payload, "model": "grok-4.20-fast"}, stream=False)
    assert caught.value.status_code == 400
    assert "该通道不支持图片输入" in caught.value.message
    assert canned["sent"] == []


# ----------------------------- Anthropic：非流式与事件序 -----------------------------


async def test_messages_nonstream_text_shape(tmp_path, canned):
    canned["reply"] = _chatgpt_sse(["你好"])
    adapter = _adapter(tmp_path)
    reply = await adapter.messages({
        "model": "gpt-5",
        "system": "你是助手",
        "messages": [{"role": "user", "content": "北京天气"}],
    }, stream=False)
    body = json.loads(reply.body)
    assert body["type"] == "message"
    assert body["content"][0] == {"type": "text", "text": "你好"}
    assert body["stop_reason"] == "end_turn"
    # 非流式 _collect 不产出 usage → 三态之一：字段整体缺席，不造零
    assert "usage" not in body


async def test_messages_stream_full_event_sequence(tmp_path, canned):
    canned["reply"] = _chatgpt_sse(["你", "你好"])
    adapter = _adapter(tmp_path)
    reply = await adapter.messages(
        {"model": "gpt-5", "messages": [{"role": "user", "content": "讲个笑话"}]},
        stream=True,
    )
    names = [name for name, _ in _events(await _drain(reply))]
    assert names == [
        "message_start",
        "content_block_start",
        "content_block_delta", "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]


# ----------------------------- Anthropic：usage 两态 -----------------------------


async def test_messages_stream_usage_absent_when_upstream_silent(tmp_path, canned):
    canned["reply"] = _chatgpt_sse(["你好"])
    adapter = _adapter(tmp_path)
    reply = await adapter.messages(
        {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]},
        stream=True,
    )
    events = _events(await _drain(reply))
    start = events[0][1]["message"]
    delta = [data for name, data in events if name == "message_delta"][0]
    assert "usage" not in start, "上游没给 usage 时 message_start 不得造零"
    assert "usage" not in delta, "上游没给 usage 时 message_delta 不得造零"


async def test_messages_stream_usage_segmented_when_upstream_reports(tmp_path):
    adapter = _adapter(tmp_path)
    chunks = [
        {"id": "m1", "choices": [{"index": 0, "delta": {"content": "你好"}}],
         "usage": {"prompt_tokens": 11, "completion_tokens": 4}},
    ]
    events = await _rendered_events(adapter._render_anthropic, chunks)
    assert events[0][0] == "message_start"
    assert events[0][1]["message"]["usage"] == {"input_tokens": 11, "output_tokens": 0}
    delta = [data for name, data in events if name == "message_delta"][0]
    assert delta["usage"] == {"output_tokens": 4}


# ----------------------------- Anthropic：工具调用 -----------------------------


async def test_messages_nonstream_tool_use(tmp_path, canned):
    cuts, _ = _tool_reply_text()
    canned["reply"] = _chatgpt_sse(cuts)
    adapter = _adapter(tmp_path)
    reply = await adapter.messages({
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "天气"}],
        "tools": [ANTHROPIC_TOOL],
    }, stream=False)
    body = json.loads(reply.body)
    assert body["stop_reason"] == "tool_use"
    assert body["content"][0]["type"] == "text"
    tool = [block for block in body["content"] if block["type"] == "tool_use"][0]
    assert tool["name"] == "get_weather"
    assert tool["id"].startswith("call_")
    assert tool["input"] == {"city": "北京"}


async def test_messages_stream_tool_use_events(tmp_path, canned):
    cuts, _ = _tool_reply_text()
    canned["reply"] = _chatgpt_sse(cuts)
    adapter = _adapter(tmp_path)
    reply = await adapter.messages({
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "天气"}],
        "tools": [ANTHROPIC_TOOL],
    }, stream=True)
    events = _events(await _drain(reply))
    starts = [data for name, data in events if name == "content_block_start"]
    assert any(item["content_block"]["type"] == "tool_use" for item in starts)
    partials = [
        data["delta"]["partial_json"] for name, data in events
        if name == "content_block_delta" and data["delta"]["type"] == "input_json_delta"
    ]
    assert partials and json.loads("".join(partials)) == {"city": "北京"}
    delta = [data for name, data in events if name == "message_delta"][0]
    assert delta["delta"]["stop_reason"] == "tool_use"


# ----------------------------- Anthropic：tool_choice / 图片 / tools -----------------------------


async def test_messages_tool_choice_three_modes(tmp_path, canned):
    canned["reply"] = _chatgpt_sse(["好"])
    adapter = _adapter(tmp_path)
    base = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [ANTHROPIC_TOOL],
    }

    await adapter.messages({**base, "tool_choice": {"type": "auto"}}, stream=False)
    assert "MUST call" not in json.dumps(canned["sent"][-1]["payload"], ensure_ascii=False)

    await adapter.messages({**base, "tool_choice": {"type": "any"}}, stream=False)
    assert "You MUST call at least one tool" in json.dumps(
        canned["sent"][-1]["payload"], ensure_ascii=False
    )

    await adapter.messages(
        {**base, "tool_choice": {"type": "tool", "name": "get_weather"}}, stream=False
    )
    assert "You MUST call the tool 'get_weather'" in json.dumps(
        canned["sent"][-1]["payload"], ensure_ascii=False
    )


async def test_messages_image_channel_split(tmp_path, canned):
    canned["reply"] = _chatgpt_sse(["图里有只猫"])
    adapter = _adapter(tmp_path)
    payload = {"model": "gpt-5", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "iVBORw0KGgo="}},
    ]}]}
    await adapter.messages(dict(payload), stream=False)
    parts = canned["sent"][0]["payload"]["messages"][0]["content"]["parts"]
    assert any(
        isinstance(part, dict)
        and part["image_url"]["url"] == "data:image/png;base64,iVBORw0KGgo="
        for part in parts
    ), "GPT 通道 base64 图片未转成 data URI"

    canned["sent"].clear()
    with pytest.raises(ProviderError) as caught:
        await adapter.messages({**payload, "model": "grok-4.20-fast"}, stream=False)
    assert caught.value.status_code == 400
    assert "该通道不支持图片输入" in caught.value.message
    assert canned["sent"] == []


async def test_messages_stream_usage_zero_kept_as_zero(tmp_path):
    adapter = _adapter(tmp_path)
    chunks = [
        {"id": "m2", "choices": [{"index": 0, "delta": {"content": "好"}}],
         "usage": {"prompt_tokens": 0, "completion_tokens": 0}},
    ]
    events = await _rendered_events(adapter._render_anthropic, chunks)
    assert events[0][1]["message"]["usage"] == {"input_tokens": 0, "output_tokens": 0}
    delta = [data for name, data in events if name == "message_delta"][0]
    assert delta["usage"] == {"output_tokens": 0}


async def test_responses_stream_grok_channel_tool(tmp_path, canned):
    canned["reply"] = _grok_lines([
        "查一下。\n",
        '[tool_call] {"name": "get_weather", "arguments": {"city": "SH"}}',
    ])
    adapter = _adapter(tmp_path)
    reply = await adapter.responses(
        {"model": "grok-4.20-fast", "input": "天气", "tools": [TOOL]}, stream=True
    )
    raw = await _drain(reply)
    assert b"[DONE]" not in raw
    assert canned["sent"][0]["path"] == "/rest/app-chat/conversations/new"
    events = _events(raw)
    assert [name for name, _ in events if name == "response.completed"]
    completed = [data for name, data in events if name == "response.completed"][0]
    fc = completed["response"]["output"][1]
    assert json.loads(fc["arguments"]) == {"city": "SH"}


def test_messages_nested_tools_flatten_conversion():
    schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    out = gptgrok.GptGrokAdapter._anthropic_to_chat({
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "get_weather", "description": "查天气", "input_schema": schema}],
    })
    assert out["tools"] == [
        {"type": "function",
         "function": {"name": "get_weather", "description": "查天气", "parameters": schema}}
    ]
