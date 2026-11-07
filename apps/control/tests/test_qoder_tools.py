"""qoder 协议升级：Anthropic/Responses 工具映射、图片显式拒绝、workbuddy messages。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters import qoder
from app.adapters.base import ProviderError
from app.adapters.client import UnifiedApiClient
from app.config import load_settings

TOOL_DEFS = [
    {
        "name": "get_weather",
        "description": "查天气",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
    }
]


def _fresh() -> qoder.QoderAdapter:
    adapter = qoder.build(load_settings())
    adapter.configure(api_key="fake-access-token")
    return adapter


def _envelope(inner: dict) -> bytes:
    payload = json.dumps({"body": json.dumps(inner, ensure_ascii=False), "statusCodeValue": 200}, ensure_ascii=False)
    return f"data: {payload}\n\n".encode()


def _cosy_tool_sse() -> bytes:
    """COSY 包裹 SSE：一句文本 + 两帧参数的 tool_calls + usage。"""
    text_frame = _envelope({
        "id": "c1",
        "choices": [{"index": 0, "delta": {"content": "查一下"}}],
    })
    tool_open = _envelope({
        "id": "c1",
        "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": "call_7f3a", "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":'},
        }]}}],
    })
    tool_more = _envelope({
        "id": "c1",
        "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": '"北京"}'},
        }]}}],
    })
    finish = _envelope({
        "id": "c1",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    })
    return text_frame + tool_open + tool_more + finish + b'data: {"body":"[DONE]"}\n\n'


def _cosy_text_sse() -> bytes:
    return (
        _envelope({"id": "c1", "choices": [{"index": 0, "delta": {"content": "Hello "}}]})
        + _envelope({"id": "c1", "choices": [{"index": 0, "delta": {"content": "world"}}]})
        + _envelope({"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
        + b'data: {"body":"[DONE]"}\n\n'
    )


def _plain_tool_sse() -> bytes:
    """workbuddy 是 OpenAI 直通，SSE 是裸 OpenAI 形状。"""
    return (
        'data: {"id":"w1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_w1","type":"function",'
        '"function":{"name":"get_weather","arguments":"{\\"city\\":\\"北京\\"}"}}]}}]}\n\n'
        'data: {"id":"w1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],'
        '"usage":{"prompt_tokens":7,"completion_tokens":4}}\n\n'
        "data: [DONE]\n\n"
    ).encode()


def _plain_text_sse() -> bytes:
    return (
        'data: {"id":"w1","choices":[{"index":0,"delta":{"content":"北京晴 25度"}}]}\n\n'
        'data: {"id":"w1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":7,"completion_tokens":4}}\n\n'
        "data: [DONE]\n\n"
    ).encode()


def _record(monkeypatch, upstream: bytes) -> list[httpx.Request]:
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, content=upstream, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://mock.local", trust_env=False
    )
    monkeypatch.setattr(UnifiedApiClient, "_client", lambda self, *args, **kwargs: client)
    return recorded


def _cosy_body(request: httpx.Request) -> dict:
    return json.loads(qoder.qoder_decode(request.content.decode()))


def _events(raw: bytes) -> list[tuple[str, dict]]:
    parsed: list[tuple[str, dict]] = []
    for block in raw.decode("utf-8").split("\n\n"):
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


def _anthropic_tool_payload(**overrides) -> dict:
    payload = {
        "model": "cn:qwen3",
        "system": "你是助手",
        "tools": TOOL_DEFS,
        "messages": [
            {"role": "user", "content": "北京天气"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "查一下"},
                {"type": "tool_use", "id": "toolu_01", "name": "get_weather", "input": {"city": "北京"}},
            ]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "晴 25度"}]},
        ],
        "max_tokens": 64,
    }
    payload.update(overrides)
    return payload


async def test_messages_tools_reach_cosy_upstream(monkeypatch):
    """入站 Anthropic tools/tool_use/tool_result 必须原样抵达 COSY 请求体。"""
    recorded = _record(monkeypatch, _cosy_tool_sse())
    reply = await _fresh().messages(_anthropic_tool_payload(), stream=False)
    assert reply.status_code == 200

    body = _cosy_body(recorded[0])
    assert body["tools"] == [
        {"type": "function", "function": {
            "name": "get_weather", "description": "查天气",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }}
    ]
    assistant = body["messages"][2]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "toolu_01"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"city": "北京"}
    assert body["messages"][3] == {"role": "tool", "content": "晴 25度", "tool_call_id": "toolu_01"}


async def test_messages_nonstream_returns_tool_use(monkeypatch):
    """上游回 tool_calls 时，/v1/messages 响应必须带 tool_use 块与 tool_use 停止原因。"""
    _record(monkeypatch, _cosy_tool_sse())
    reply = await _fresh().messages(_anthropic_tool_payload(), stream=False)
    body = json.loads(reply.body)

    assert body["stop_reason"] == "tool_use"
    tool_use = body["content"][1]
    assert tool_use == {
        "type": "tool_use", "id": "call_7f3a", "name": "get_weather", "input": {"city": "北京"},
    }
    assert body["usage"] == {"input_tokens": 5, "output_tokens": 3}


async def test_messages_stream_tool_use_event_sequence(monkeypatch):
    """流式 tool_use 事件序列：文本块收口后开 tool_use 块，参数走 input_json_delta。"""
    _record(monkeypatch, _cosy_tool_sse())
    reply = await _fresh().messages(_anthropic_tool_payload(), stream=True)
    raw = b"".join([chunk async for chunk in reply.iter_bytes()])
    events = _events(raw)

    names = [name for name, _ in events]
    assert names == [
        "message_start",
        "content_block_start", "content_block_delta",
        "content_block_stop",
        "content_block_start", "content_block_delta", "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    tool_start = events[4][1]
    assert tool_start["index"] == 1
    assert tool_start["content_block"] == {"type": "tool_use", "id": "call_7f3a", "name": "get_weather", "input": {}}
    assert events[5][1]["delta"] == {"type": "input_json_delta", "partial_json": '{"city":'}
    assert events[6][1]["delta"] == {"type": "input_json_delta", "partial_json": '"北京"}'}
    assert events[8][1]["delta"]["stop_reason"] == "tool_use"


async def test_messages_without_tools_keeps_old_shape(monkeypatch):
    """无工具的既有行为不回归：纯文本非流式响应与入站转换都不新增字段。"""
    _record(monkeypatch, _cosy_text_sse())
    payload = {
        "model": "cn:qwen3",
        "messages": [{"role": "user", "content": "打招呼"}],
        "max_tokens": 16,
    }
    reply = await _fresh().messages(dict(payload), stream=False)
    body = json.loads(reply.body)
    assert body["content"] == [{"type": "text", "text": "Hello world"}]
    assert body["stop_reason"] == "end_turn"

    converted = qoder.QoderAdapter._anthropic_to_openai(dict(payload))
    assert "tools" not in converted
    assert converted["messages"] == [{"role": "user", "content": "打招呼"}]


async def test_messages_stream_text_only_event_sequence(monkeypatch):
    """无工具流式仍是一个文本块的旧事件序列。"""
    _record(monkeypatch, _cosy_text_sse())
    reply = await _fresh().messages(
        {"model": "cn:qwen3", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
        stream=True,
    )
    raw = b"".join([chunk async for chunk in reply.iter_bytes()])
    events = _events(raw)
    assert [name for name, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta", "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[1][1]["index"] == 0
    assert events[-2][1]["delta"]["stop_reason"] == "end_turn"


def test_responses_inbound_tools_and_tool_roundtrip():
    """Responses 扁平 tools → OpenAI 嵌套；function_call/output 往返 → tool_calls/tool 消息。"""
    converted = qoder.QoderAdapter._responses_to_openai({
        "model": "cn:qwen3",
        "input": [
            {"role": "user", "content": "北京天气"},
            {"type": "function_call", "call_id": "call_01", "name": "get_weather", "arguments": '{"city":"北京"}'},
            {"type": "function_call_output", "call_id": "call_01", "output": "晴 25度"},
        ],
        "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
        "stream": False,
    })
    assert converted["tools"] == [
        {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}
    ]
    assert converted["messages"][1] == {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_01", "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}],
    }
    assert converted["messages"][2] == {"role": "tool", "tool_call_id": "call_01", "content": "晴 25度"}


async def test_responses_nonstream_function_call_output(monkeypatch):
    """/v1/responses 带工具：上游 tool_calls → output 里的 function_call 项。"""
    recorded = _record(monkeypatch, _cosy_tool_sse())
    reply = await _fresh().responses({
        "model": "cn:qwen3",
        "input": "北京天气",
        "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
        "max_output_tokens": 64,
    }, stream=False)
    body = json.loads(reply.body)

    assert body["output"][0]["type"] == "message"
    call = body["output"][1]
    assert call["type"] == "function_call"
    assert call["call_id"] == "call_7f3a"
    assert call["name"] == "get_weather"
    assert call["arguments"] == '{"city":"北京"}'
    assert call["status"] == "completed"

    upstream = _cosy_body(recorded[0])
    assert upstream["tools"][0]["function"]["name"] == "get_weather"


async def test_responses_stream_function_call_events(monkeypatch):
    """流式 /v1/responses 的 function_call 事件：added → arguments.delta → done → completed。"""
    _record(monkeypatch, _cosy_tool_sse())
    reply = await _fresh().responses({
        "model": "cn:qwen3",
        "input": "北京天气",
        "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
    }, stream=True)
    raw = b"".join([chunk async for chunk in reply.iter_bytes()])
    events = _events(raw)
    names = [name for name, _ in events]

    assert "response.output_item.added" in names
    assert "response.function_call_arguments.delta" in names
    assert "response.function_call_arguments.done" in names
    assert "response.output_item.done" in names
    added = [data for name, data in events if name == "response.output_item.added" and data["output_index"] == 1]
    assert added and added[0]["item"]["type"] == "function_call"
    assert added[0]["item"]["call_id"] == "call_7f3a"

    args_done = [data for name, data in events if name == "response.function_call_arguments.done"]
    assert args_done[0]["name"] == "get_weather"
    assert args_done[0]["arguments"] == '{"city":"北京"}'

    completed = [data for name, data in events if name == "response.completed"][0]
    assert completed["response"]["output"][1]["type"] == "function_call"
    assert completed["response"]["output"][1]["arguments"] == '{"city":"北京"}'


async def test_responses_stream_text_only_not_regressed(monkeypatch):
    """无工具的 Responses 流式不回归：只有 message 项。"""
    _record(monkeypatch, _cosy_text_sse())
    reply = await _fresh().responses(
        {"model": "cn:qwen3", "input": "hi", "max_output_tokens": 16},
        stream=True,
    )
    raw = b"".join([chunk async for chunk in reply.iter_bytes()])
    events = _events(raw)
    assert [name for name, _ in events] == [
        "response.created",
        "response.output_item.added",
        "response.output_text.delta", "response.output_text.delta",
        "response.output_text.done",
        "response.completed",
    ]
    completed = events[-1][1]
    assert [item["type"] for item in completed["response"]["output"]] == ["message"]


async def test_cosy_messages_image_rejected(monkeypatch):
    """COSY 线带图必须显式 400，不再静默压成文本。"""
    _record(monkeypatch, _cosy_text_sse())
    payload = {
        "model": "cn:qwen3",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}},
        ]}],
        "max_tokens": 8,
    }
    with pytest.raises(ProviderError) as excinfo:
        await _fresh().messages(payload, stream=False)
    assert excinfo.value.status_code == 400
    assert "不支持图片" in excinfo.value.message


async def test_cosy_chat_completion_image_rejected(monkeypatch):
    """OpenAI chat 直连 COSY 带 image_url 同样显式 400。"""
    _record(monkeypatch, _cosy_text_sse())
    payload = {
        "model": "cn:qwen3",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
        ]}],
        "max_tokens": 8,
    }
    with pytest.raises(ProviderError) as excinfo:
        await _fresh().chat_completion(payload, stream=False)
    assert excinfo.value.status_code == 400
    assert "不支持图片" in excinfo.value.message


async def test_responses_input_image_rejected(monkeypatch):
    """Responses 的 input_image 最终落到 COSY 线，也必须 400。"""
    _record(monkeypatch, _cosy_text_sse())
    with pytest.raises(ProviderError) as excinfo:
        await _fresh().responses({
            "model": "cn:qwen3",
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "看图"},
                {"type": "input_image", "image_url": "https://example.com/cat.png"},
            ]}],
            "max_output_tokens": 8,
        }, stream=False)
    assert excinfo.value.status_code == 400
    assert "不支持图片" in excinfo.value.message


async def test_workbuddy_messages_image_passthrough_base64(monkeypatch):
    """workbuddy 是 OpenAI 直通：base64 图片转成 data URL 图片段透传给上游。"""
    recorded = _record(monkeypatch, _plain_text_sse())
    reply = await _fresh().messages({
        "model": "workbuddy/gpt-4o",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}},
        ]}],
        "max_tokens": 8,
    }, stream=False)

    assert reply.status_code == 200
    upstream = json.loads(recorded[0].content)
    assert upstream["model"] == "gpt-4o"
    assert upstream["messages"][0]["content"] == [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
    ]


async def test_workbuddy_messages_image_passthrough_remote_url(monkeypatch):
    """远程 URL source 原样透传。"""
    recorded = _record(monkeypatch, _plain_text_sse())
    await _fresh().messages({
        "model": "workbuddy/gpt-4o",
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "url", "url": "https://example.com/cat.png"}},
        ]}],
        "max_tokens": 8,
    }, stream=False)
    upstream = json.loads(recorded[0].content)
    assert upstream["messages"][0]["content"] == [
        {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
    ]


async def test_workbuddy_messages_nonstream_success(monkeypatch):
    """workbuddy 线 /v1/messages 返回真实 Anthropic 形状响应，不再是 501。"""
    _record(monkeypatch, _plain_text_sse())
    reply = await _fresh().messages({
        "model": "workbuddy/gpt-4o",
        "messages": [{"role": "user", "content": "北京天气"}],
        "max_tokens": 8,
    }, stream=False)
    body = json.loads(reply.body)
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == "gpt-4o"
    assert body["content"] == [{"type": "text", "text": "北京晴 25度"}]
    assert body["stop_reason"] == "end_turn"


async def test_workbuddy_messages_stream_success(monkeypatch):
    """workbuddy 线流式：裸 OpenAI SSE 正确渲染成 Anthropic 事件序列。"""
    _record(monkeypatch, _plain_tool_sse())
    reply = await _fresh().messages({
        "model": "workbuddy/gpt-4o",
        "messages": [{"role": "user", "content": "北京天气"}],
        "tools": TOOL_DEFS,
        "max_tokens": 8,
    }, stream=True)
    raw = b"".join([chunk async for chunk in reply.iter_bytes()])
    events = _events(raw)
    names = [name for name, _ in events]
    assert names == [
        "message_start",
        "content_block_start", "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[1][1]["content_block"] == {
        "type": "tool_use", "id": "call_w1", "name": "get_weather", "input": {},
    }
    assert events[2][1]["delta"] == {"type": "input_json_delta", "partial_json": '{"city":"北京"}'}
    assert events[4][1]["delta"]["stop_reason"] == "tool_use"


async def test_trae_product_messages_still_501(monkeypatch):
    """不在支持范围内的 trae 产品线保持 501。"""
    _record(monkeypatch, _plain_text_sse())
    with pytest.raises(ProviderError) as excinfo:
        await _fresh().messages(
            {"model": "trae/anthropic", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
            stream=False,
        )
    assert excinfo.value.status_code == 501
    assert "不支持 Anthropic 协议" in excinfo.value.message


async def test_tool_result_with_image_rejected():
    """tool_result 内的图片无法进 OpenAI tool 消息，必须显式 400。"""
    payload = _anthropic_tool_payload()
    payload["messages"] = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": [
            {"type": "text", "text": "图"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}},
        ]}]}
    ]
    with pytest.raises(ProviderError) as excinfo:
        qoder.QoderAdapter._anthropic_to_openai(payload)
    assert excinfo.value.status_code == 400


def test_tool_call_with_broken_arguments_kept_verbatim():
    """上游参数不是合法 JSON 时保原文，不静默吞掉。"""
    block = qoder._tool_call_to_tool_use({"id": "call_x", "function": {"name": "fn", "arguments": "{broken"}})
    assert block["input"] == {"_raw": "{broken"}


# ---------- Responses 命名空间工具：摊平 / 还原 ----------


def _namespaced_tools() -> list[dict]:
    return [
        {"type": "function", "name": "plain", "parameters": {"type": "object"}},
        {"type": "namespace", "namespace": "mcp__search", "tools": [
            {"type": "function", "name": "search", "description": "搜", "parameters": {"type": "object"}},
            {"type": "custom", "name": "apply_patch", "description": "改文件"},
        ]},
        {"type": "namespace", "name": "mcp__repo", "tools": [
            {"type": "function", "name": "search", "parameters": {"type": "object"}},
        ]},
    ]


def test_namespace_tools_flattened_with_distinct_names():
    """同 namespace 多工具 + 跨 namespace 同名工具：扁平名带命名空间，互不碰撞。"""
    converted = qoder.QoderAdapter._responses_to_openai({
        "model": "cn:qwen3", "input": "hi", "tools": _namespaced_tools(), "stream": False,
    })
    names = [tool["function"]["name"] for tool in converted["tools"]]
    assert names == [
        "plain",
        "mcp__search__ns__search",
        "mcp__search__ns__apply_patch",
        "mcp__repo__ns__search",
    ]
    assert len(set(names)) == len(names)
    # 原始 namespace 工具不许原样透传给上游
    assert all(tool["function"] for tool in converted["tools"])


def test_namespace_name_prefers_field_then_falls_back_to_name():
    """namespace 缺 `namespace` 字段时回落 `name`，两者都空则整组跳过。"""
    converted = qoder.QoderAdapter._responses_to_openai({
        "model": "cn:qwen3", "input": "hi", "stream": False,
        "tools": [
            {"type": "namespace", "name": "mcp__fallback", "tools": [
                {"type": "function", "name": "read"},
            ]},
            {"type": "namespace", "namespace": "", "name": "", "tools": [
                {"type": "function", "name": "dropped"},
            ]},
            {"type": "namespace", "namespace": "mcp__noinner"},
        ],
    })
    assert [tool["function"]["name"] for tool in converted["tools"]] == ["mcp__fallback__ns__read"]


def test_namespace_collision_rule_is_deterministic():
    """命名空间本身含分隔符时按「第一个分隔符」切分，规则确定且可预测。"""
    mangled = qoder._mangle_namespaced("a__ns__b", "tool")
    item: dict = {"type": "function_call", "name": mangled}
    qoder._restore_tool_identity(item, mangled)
    assert item["namespace"] == "a"
    assert item["name"] == "b__ns__tool"
    assert mangled == "a__ns__b__ns__tool"


def test_non_namespace_tool_names_unchanged():
    """非 namespace 工具名与还原行为不变（含 freeform 降级的单 input 参数形状）。"""
    converted = qoder.QoderAdapter._responses_to_openai({
        "model": "cn:qwen3", "input": "hi", "stream": False,
        "tools": [
            {"type": "function", "name": "get_weather", "parameters": {"type": "object"}},
            {"type": "custom", "name": "apply_patch", "description": "改文件"},
        ],
    })
    weather, patch = converted["tools"]
    assert weather["function"]["name"] == "get_weather"
    assert set(patch["function"]["parameters"]["properties"]) == {"input"}
    assert patch["function"]["parameters"]["required"] == ["input"]
    item = {"type": "function_call", "name": "get_weather", "arguments": "{}"}
    qoder._restore_tool_identity(item, "get_weather")
    assert item == {"type": "function_call", "name": "get_weather", "arguments": "{}"}


def test_namespaced_history_items_are_remangled():
    """出站还原过身份 ⇒ 下一轮历史项要拼回扁平名，否则上游工具清单对不上。"""
    converted = qoder.QoderAdapter._responses_to_openai({
        "model": "cn:qwen3", "stream": False,
        "input": [
            {"role": "user", "content": "搜一下"},
            {"type": "function_call", "call_id": "c1", "name": "search",
             "namespace": "mcp__search", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            {"type": "custom_tool_call", "call_id": "c2", "name": "apply_patch",
             "namespace": "mcp__search", "input": "PATCH"},
            {"type": "custom_tool_call_output", "call_id": "c2", "output": "done"},
        ],
    })
    calls = [row["tool_calls"][0]["function"] for row in converted["messages"] if row.get("tool_calls")]
    assert calls[0]["name"] == "mcp__search__ns__search"
    assert json.loads(calls[1]["arguments"]) == {"input": "PATCH"}
    assert calls[1]["name"] == "mcp__search__ns__apply_patch"


async def test_responses_nonstream_restores_namespace_identity(monkeypatch):
    """上游回扁平名 ⇒ 非流式 output 还原成原名 + namespace。"""
    mangled_sse = (
        'data: {"body": "{\\"id\\":\\"c1\\",\\"choices\\":[{\\"index\\":0,\\"delta\\":{\\"tool_calls\\":'
        '[{\\"index\\":0,\\"id\\":\\"call_ns\\",\\"type\\":\\"function\\",\\"function\\":'
        '{\\"name\\":\\"mcp__search__ns__search\\",\\"arguments\\":\\"{}\\"}}]}}]}"}\n\n'
        'data: {"body":"[DONE]"}\n\n'
    ).encode()
    _record(monkeypatch, mangled_sse)
    reply = await _fresh().responses({
        "model": "cn:qwen3", "input": "搜一下", "tools": _namespaced_tools(),
    }, stream=False)
    call = json.loads(reply.body)["output"][1]
    assert call["type"] == "function_call"
    assert call["name"] == "search"
    assert call["namespace"] == "mcp__search"
    assert "arguments" in call


async def test_responses_stream_restores_namespace_identity(monkeypatch):
    """流式三处（added/done/completed）都要还原身份。"""
    mangled_sse = (
        'data: {"body": "{\\"id\\":\\"c1\\",\\"choices\\":[{\\"index\\":0,\\"delta\\":{\\"tool_calls\\":'
        '[{\\"index\\":0,\\"id\\":\\"call_ns\\",\\"type\\":\\"function\\",\\"function\\":'
        '{\\"name\\":\\"mcp__search__ns__search\\",\\"arguments\\":\\"{}\\"}}]}}]}"}\n\n'
        'data: {"body":"[DONE]"}\n\n'
    ).encode()
    _record(monkeypatch, mangled_sse)
    reply = await _fresh().responses({
        "model": "cn:qwen3", "input": "搜一下", "tools": _namespaced_tools(),
    }, stream=True)
    events = _events(b"".join([chunk async for chunk in reply.iter_bytes()]))
    call_events = [data for name, data in events if name == "response.output_item.added"
                   and data["item"]["type"] == "function_call"]
    done = [data for name, data in events if name == "response.output_item.done"][0]
    completed = [data for name, data in events if name == "response.completed"][0]
    assert call_events[0]["item"]["name"] == "search"
    assert call_events[0]["item"]["namespace"] == "mcp__search"
    assert done["item"]["namespace"] == "mcp__search"
    assert completed["response"]["output"][1]["name"] == "search"
    assert completed["response"]["output"][1]["namespace"] == "mcp__search"


async def test_responses_stream_freeform_call_uses_input_field(monkeypatch):
    """命名空间里的 freeform 工具：输出是 custom_tool_call + 拆好的原文，不发 JSON 增量。"""
    freeform_sse = (
        'data: {"body": "{\\"id\\":\\"c1\\",\\"choices\\":[{\\"index\\":0,\\"delta\\":{\\"tool_calls\\":'
        '[{\\"index\\":0,\\"id\\":\\"call_ns\\",\\"type\\":\\"function\\",\\"function\\":'
        '{\\"name\\":\\"mcp__search__ns__apply_patch\\",\\"arguments\\":\\"{\\\\\\"input\\\\\\":\\\\\\"PATCH\\\\\\"}\\"}}]}}]}"}\n\n'
        'data: {"body":"[DONE]"}\n\n'
    ).encode()
    _record(monkeypatch, freeform_sse)
    reply = await _fresh().responses({
        "model": "cn:qwen3", "input": "改文件", "tools": _namespaced_tools(),
    }, stream=True)
    events = _events(b"".join([chunk async for chunk in reply.iter_bytes()]))
    names = [name for name, _ in events]
    assert "response.function_call_arguments.delta" not in names
    assert "response.custom_tool_call_input.done" in names
    done = [data for name, data in events if name == "response.custom_tool_call_input.done"][0]
    assert done["input"] == "PATCH"
    item = [data for name, data in events if name == "response.output_item.done"][0]["item"]
    assert item["type"] == "custom_tool_call"
    assert item["name"] == "apply_patch"
    assert item["namespace"] == "mcp__search"
