"""F2 适配器回归：qoder 加密/协议转换/路由，trae Solo/Work 桥，网关 responses 路由。"""

import json
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from app.adapters import qoder, trae
from app.config import load_settings


class _FakeReply:
    """假上游：按给定 SSE 分片迭代，够用即可。"""

    def __init__(self, chunks: list[bytes], status_code: int = 200) -> None:
        self._chunks = chunks
        self.status_code = status_code
        self.body = b""
        self.headers = {"content-type": "text/event-stream"}

    async def iter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        pass


def _sse(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


async def _drain(stream) -> bytes:
    return b"".join([chunk async for chunk in stream])


class QoderCryptoTests(unittest.TestCase):
    """COSY 签名原语：用官方向量钉死，错一位就会静默 403。"""

    def test_custom_base64_matches_official_vectors(self):
        cases = {
            "": "",
            "a": "$p$#",
            "ab": "$SB#",
            "abc": "MSK#",
            "abcd": "$$KMD_#S",
            '{"k":"v"}': "J*KWBMn*mYKU",
            "你好世界": "SQQSVPSWQG*QG(&Q",
        }
        for plain, expected in cases.items():
            self.assertEqual(qoder.qoder_encode(plain.encode()), expected, plain)

    def test_custom_base64_round_trips(self):
        for plain in ["", "a", "abcd", "你好世界", "x" * 64]:
            self.assertEqual(qoder.qoder_decode(qoder.qoder_encode(plain.encode())), plain.encode())

    def test_sorted_compact_normalizes_null(self):
        raw = qoder.sorted_compact({"b": 1, "a": "x", "z": None, "n": "你好"})
        self.assertEqual(raw.decode(), '{"a":"x","b":1,"n":"你好","z":""}')
        self.assertEqual(qoder.sorted_compact({}).decode(), "{}")

    def test_aes_cbc_matches_official_vector(self):
        key = b"0123456789abcdef"
        cipher = qoder.aes_cbc_encrypt(b'{"name":"jackey","aid":"q1"}', key)
        self.assertEqual(
            cipher.hex(),
            "d2b4b6af77e5945ccb0e1c41f575720f5c4ddc4231efd8f41c1c294670db4621",
        )

    def test_signature_matches_official_vector(self):
        self.assertEqual(
            qoder.cosy_signature("cGF5bG9hZA==", "Q09TWUtFWQ==", "1789000000", "ENCODED_BODY", "/api/v2/model/list"),
            "93a5414037f64183e005a9577182e2ed",
        )

    def test_machine_identity_matches_official_vectors(self):
        identity = qoder.machine_identity("q1")
        self.assertEqual(identity["machine_id"], "abec85ce8c7ed7ebd0041ee4ca63e701")
        self.assertEqual(identity["machine_type"], "964afd94abdbefa690")
        self.assertEqual(identity["machine_token"], "2PJ8T1JfAGWIBuPPnEYyOGKqFw6DXwEMeAng__6fYlJ")

    def test_rsa_padding_never_emits_zero_bytes(self):
        cipher = qoder.rsa_pkcs1v15_encrypt(b"0123456789abcdef", qoder.SERVER_PUB_PEM)
        self.assertEqual(len(cipher), 128)
        self.assertNotEqual(cipher, b"0123456789abcdef")


class QoderRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = qoder.build(load_settings())

    def test_region_prefix_and_default(self):
        self.assertEqual(self.adapter._region("global:claude-4"), ("global", "claude-4"))
        self.assertEqual(self.adapter._region("cn:qwen3"), ("cn", "qwen3"))
        self.assertEqual(self.adapter._region("qwen3"), ("cn", "qwen3"))

    def test_product_prefix(self):
        self.assertEqual(self.adapter._product("workbuddy/gpt-4o"), ("workbuddy", "gpt-4o"))
        self.assertEqual(self.adapter._product("gpt-4o"), ("qoder", "gpt-4o"))

    def test_fallback_chain_per_region(self):
        self.assertEqual(self.adapter._hosts("cn"), ["https://gateway.qoder.com.cn"])
        self.assertEqual(
            self.adapter._hosts("global"),
            ["https://api1.qoder.sh", "https://api2.qoder.sh", "https://api3.qoder.sh"],
        )

    def test_direct_connection_note_is_explicit(self):
        self.assertIn("地域代理", self.adapter._direct_note("cn"))
        self.adapter.configure(options={"proxy": "http://127.0.0.1:7890"})
        self.assertEqual(self.adapter._direct_note("cn"), "")

    def test_error_classification(self):
        dead = self.adapter._classify(401, '{"code":12153}')
        self.assertEqual(dead.status_code, 429)
        self.assertTrue(getattr(dead, "session_dead", False))
        rate = self.adapter._classify(429, '{"code":429,"retry_after":12}')
        self.assertEqual(rate.retry_after, 12)
        server = self.adapter._classify(503, "gateway down")
        self.assertEqual(server.status_code, 502)

    def test_cooldown_blocks_second_call(self):
        self.adapter._cooldown["cn:q1"] = 1e18
        self.assertGreater(self.adapter._cooling("cn:q1"), 0)
        self.assertEqual(self.adapter._cooling("global:q1"), 0)


class QoderProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = qoder.build(load_settings())

    def test_anthropic_request_conversion(self):
        payload = {
            "model": "cn:qwen3",
            "system": [{"type": "text", "text": "听好"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "你好"}]}],
            "max_tokens": 64,
        }
        out = self.adapter._anthropic_to_openai(payload)
        self.assertEqual(out["messages"][0], {"role": "system", "content": "听好"})
        self.assertEqual(out["messages"][1], {"role": "user", "content": "你好"})
        self.assertEqual(out["max_tokens"], 64)

    def test_responses_request_conversion(self):
        payload = {
            "model": "global:qwen3",
            "instructions": "简洁",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "讲个笑话"}]}
            ],
            "max_output_tokens": 128,
        }
        out = self.adapter._responses_to_openai(payload)
        self.assertEqual([m["role"] for m in out["messages"]], ["system", "user"])
        self.assertEqual(out["max_tokens"], 128)

    async def test_envelope_stream_is_unwrapped(self):
        lines = [
            b'data: {"body": "{\\"choices\\":[{\\"index\\":0,\\"delta\\":{\\"content\\":\\"hi\\"}}]}", "statusCodeValue":200}\n\n',
            b'data: {"body":"[DONE]"}\n\n',
        ]
        reply = _FakeReply(lines)
        out = await _drain(self.adapter._render_chat(reply, "qwen3"))
        self.assertIn(b'"content": "hi"', out)
        self.assertTrue(out.endswith(b"data: [DONE]\n\n"))

    async def test_collect_aggregates_envelope(self):
        lines = [
            b'data: {"body": "{\\"choices\\":[{\\"index\\":0,\\"delta\\":{\\"content\\":\\"hi\\"}}]}", "statusCodeValue":200}\n\n',
            b'data: {"body": "{\\"choices\\":[{\\"index\\":0,\\"delta\\":{\\"content\\":\\" there\\"}}],\\"usage\\":{\\"prompt_tokens\\":3,\\"completion_tokens\\":2}}", "statusCodeValue":200}\n\n',
        ]
        data = await self.adapter._collect(_FakeReply(lines), "qwen3")
        self.assertEqual(data["choices"][0]["message"]["content"], "hi there")
        self.assertEqual(data["usage"]["prompt_tokens"], 3)

    async def test_anthropic_stream_event_sequence(self):
        reply = _FakeReply([b'data: {"body": "{\\"choices\\":[{\\"index\\":0,\\"delta\\":{\\"content\\":\\"yo\\"}}]}"}\n\n'])
        out = await _drain(self.adapter._render_anthropic(reply, "qwen3"))
        order = [name for name in (b"message_start", b"content_block_start", b"content_block_delta", b"content_block_stop", b"message_delta", b"message_stop")]
        positions = [out.find(name) for name in order]
        self.assertTrue(all(pos >= 0 for pos in positions))
        self.assertEqual(positions, sorted(positions))

    async def test_responses_stream_event_sequence(self):
        reply = _FakeReply([b'data: {"body": "{\\"choices\\":[{\\"index\\":0,\\"delta\\":{\\"content\\":\\"yo\\"}}]}"}\n\n'])
        out = await _drain(self.adapter._render_responses(reply, "qwen3"))
        self.assertIn(b"response.created", out)
        self.assertIn(b"response.output_text.delta", out)
        self.assertIn(b"response.completed", out)

    async def test_trae_product_line_is_delegated(self):
        with self.assertRaises(Exception) as ctx:
            await self.adapter.chat_completion({"model": "trae/solo", "messages": []}, stream=False)
        self.assertEqual(getattr(ctx.exception, "status_code", 0), 501)


class TraeSoloTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = trae.build(load_settings())
        self.adapter.configure(api_key="test.jwt.value")

    def test_payload_rewrite(self):
        payload = {
            "model": "glm-5.2",
            "messages": [
                {"role": "system", "content": "系统"},
                {"role": "user", "content": "你好"},
                {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
            ],
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
            "tool_choice": {"type": "function", "function": {"name": "f"}},
            "stream": False,
        }
        body = self.adapter._solo_payload(payload)
        self.assertTrue(body["stream"])
        self.assertEqual(body["function"], "solo_work_lite")
        self.assertEqual(body["config_name"], "glm-5.2")
        self.assertEqual(body["messages"][0]["content"], [{"type": "text", "text": "系统"}])
        self.assertEqual(body["messages"][2].get("content"), None)
        self.assertIsInstance(body["messages"][2]["tool_calls"][0]["function_call"], dict)
        self.assertIsInstance(body["tools"][0]["function"]["parameters"], str)
        self.assertEqual(body["tool_choice"], "f")

    def test_tool_choice_none_drops_tools(self):
        body = self.adapter._solo_payload(
            {"model": "glm-5.2", "messages": [], "tools": [{"type": "function", "function": {"name": "f"}}], "tool_choice": "none"}
        )
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)

    def test_solo_headers_are_contract_shaped(self):
        headers = self.adapter._solo_headers()
        self.assertEqual(headers["authorization"], "Cloud-IDE-JWT test.jwt.value")
        self.assertEqual(headers["x-app-id"], trae.CHAT_APP_ID)
        self.assertEqual(headers["user-agent"], "Trae/0.1.52")
        self.assertEqual(len(headers["x-machine-id"]), 32)


class TraeStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = trae.build(load_settings())
        self.adapter.configure(api_key="test.jwt.value")

    async def test_solo_stream_converts_and_emits_usage(self):
        reply = _FakeReply([
            _sse("output", {"response": "你", "reasoning_content": "想"}),
            _sse("output", {"response": "好"}),
            _sse("token_usage", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
            _sse("done", {"finish_reason": "stop"}),
        ])
        out = await _drain(self.adapter._render_solo_stream(reply, "glm-5.2"))
        self.assertIn('"content": "你"', out.decode())
        self.assertIn('"reasoning_content": "想"', out.decode())
        self.assertIn('"usage"', out.decode())
        self.assertIn('"finish_reason": "stop"', out.decode())
        self.assertTrue(out.endswith(b"data: [DONE]\n\n"))

    async def test_solo_stream_tolerates_missing_done(self):
        reply = _FakeReply([_sse("output", {"response": "半截"})])
        out = await _drain(self.adapter._render_solo_stream(reply, "glm-5.2"))
        self.assertIn('"finish_reason": "stop"', out.decode())
        self.assertTrue(out.endswith(b"data: [DONE]\n\n"))

    async def test_solo_aggregate(self):
        reply = _FakeReply([
            _sse("output", {"response": "你"}),
            _sse("output", {"response": "好"}),
            _sse("token_usage", {"prompt_tokens": 10, "completion_tokens": 5}),
            _sse("done", {"finish_reason": "stop"}),
        ])
        data = await self.adapter._aggregate_solo(reply, "glm-5.2")
        self.assertEqual(data["choices"][0]["message"]["content"], "你好")
        self.assertEqual(data["usage"]["prompt_tokens"], 10)

    async def test_work_credit_limit_falls_back_to_solo(self):
        self.adapter.configure(options={"channel": "work"})
        upstream = _FakeReply([b'data: {"code":1005,"message":"credits not enough"}\n\n'])
        fake_client = type("C", (), {"forward": AsyncMock(return_value=upstream)})()
        with patch.object(self.adapter, "_client", return_value=fake_client):
            with patch.object(self.adapter, "_solo_chat", AsyncMock(return_value="SOLO")):
                result = await self.adapter.chat_completion({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}, stream=True)
        self.assertEqual(result, "SOLO")

    async def test_work_native_stream(self):
        self.adapter.configure(options={"channel": "work"})
        upstream = _FakeReply([
            _sse("plan_item", {"thought": ""}),
            _sse("output", {"choices": [{"text": "你好"}]}),
            _sse("done", {"last_assistant_response": json.dumps(["你好"], ensure_ascii=False)}),
        ])
        fake_client = type("C", (), {"forward": AsyncMock(return_value=upstream)})()
        with patch.object(self.adapter, "_client", return_value=fake_client):
            reply = await self.adapter.chat_completion({"model": "DeepSeek-V4-Flash-Official", "messages": [{"role": "user", "content": "hi"}]}, stream=True)
            out = await _drain(reply.iter_bytes())
        self.assertIn('"content": "你好"', out.decode())
        self.assertTrue(out.endswith(b"data: [DONE]\n\n"))


class GatewayRouteTests(unittest.TestCase):
    def test_responses_route_is_registered(self):
        from app.routers import v1

        paths = {getattr(route, "path", "") for route in v1.router.routes}
        self.assertIn("/v1/responses", paths)
        self.assertIn("/v1/messages", paths)


if __name__ == "__main__":
    unittest.main()
