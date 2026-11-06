"""代理池出口轮换（2-C1）与受限指纹短路（2-C2）。

假代理只记录请求行并回 200；上游域名刻意写成不可解析，出口命中与否完全由代理计数决定。
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import pytest

from app.adapters import codar
from app.config import Settings
from app.services.proxy_pool import MODE_FIXED, MODE_ROUND_ROBIN, ProxyPool, mask_proxy

pytestmark = pytest.mark.providers

HITS: dict[str, list[str]] = {}

CHAT_OK: dict[str, Any] = {
    "id": "chatcmpl-fake",
    "object": "chat.completion",
    "model": "fake-chat-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
}


class _Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        HITS.setdefault(self.server.stub_name, []).append(self.path)
        status, payload = self.server.stub_reply
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def _serve(name: str, reply: tuple[int, dict[str, Any]]) -> Iterator[str]:
    HITS[name] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    server.stub_name = name
    server.stub_reply = reply
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _settings(tmp: Path) -> Settings:
    return Settings(
        data_dir=tmp,
        db_path=tmp / "unified.db",
        web_dir=tmp / "web",
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


def _adapter(
    base: str,
    tmp: Path,
    *,
    tokens: tuple[str, ...] = ("codar-token-1",),
    options: dict[str, Any] | None = None,
) -> codar.CodarAdapter:
    adapter = codar.build(_settings(tmp), base_url=base)
    adapter.configure(options={"tokens": list(tokens), **(options or {})})
    return adapter


def _chat() -> dict[str, Any]:
    return {"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}], "stream": False}


def test_from_options_normalizes_sources() -> None:
    pool = ProxyPool.from_options(
        {"proxy_pool": {"mode": "bogus", "entries": [" http://a:1 ", "", "http://a:1", "http://b:2"]}}
    )
    assert pool.mode == MODE_FIXED, "非法 mode 必须回退 fixed"
    assert pool.entries == ("http://a:1", "http://b:2")
    assert pool.enabled and pool.size == 2
    assert pool.next() == "http://a:1"

    assert ProxyPool.from_options({"proxy_pool": ["http://c:3"]}).entries == ("http://c:3",)
    assert ProxyPool.from_options({"proxies": ["http://d:4", "http://d:4"]}).entries == ("http://d:4",)
    assert ProxyPool.from_options({"proxy": "http://e:5"}).entries == ("http://e:5",)
    assert ProxyPool.from_options({}).next() == ""
    assert ProxyPool.from_options({"proxy_pool": {"mode": MODE_ROUND_ROBIN, "entries": ["http://f:6"]}}).mode == (
        MODE_ROUND_ROBIN
    )


def test_mask_proxy_hides_credentials() -> None:
    assert mask_proxy("http://user:pass@127.0.0.1:7890") == "http://***@127.0.0.1:7890"
    assert mask_proxy("socks5://u:p@proxy.local:1080") == "socks5://***@proxy.local:1080"
    assert mask_proxy("http://127.0.0.1:7890") == "http://127.0.0.1:7890"


def test_round_robin_wraps_and_describe_masks() -> None:
    entries = ("http://u:p@h1:1", "http://u:p@h2:2")
    pool = ProxyPool.from_options({"proxy_pool": {"mode": MODE_ROUND_ROBIN, "entries": list(entries)}})
    assert [pool.next() for _ in range(3)] == [entries[0], entries[1], entries[0]]
    info = pool.describe()
    assert info["mode"] == MODE_ROUND_ROBIN and info["size"] == 2
    assert "u:p@" not in json.dumps(info)


async def test_round_robin_rotates_exit_across_requests(tmp_path: Path) -> None:
    with _serve("proxy-a", (200, CHAT_OK)) as proxy_a, _serve("proxy-b", (200, CHAT_OK)) as proxy_b:
        adapter = _adapter(
            "http://codar.upstream.invalid",
            tmp_path,
            options={"proxy_pool": {"mode": MODE_ROUND_ROBIN, "entries": [proxy_a, proxy_b]}},
        )
        for _ in range(2):
            reply = await adapter.chat_completion(_chat(), stream=False)
            assert reply.status_code == 200
    assert len(HITS["proxy-a"]) == 1
    assert len(HITS["proxy-b"]) == 1


async def test_fixed_always_uses_first_proxy(tmp_path: Path) -> None:
    with _serve("proxy-a", (200, CHAT_OK)) as proxy_a, _serve("proxy-b", (200, CHAT_OK)) as proxy_b:
        adapter = _adapter(
            "http://codar.upstream.invalid",
            tmp_path,
            options={"proxy_pool": {"mode": MODE_FIXED, "entries": [proxy_a, proxy_b]}},
        )
        for _ in range(2):
            reply = await adapter.chat_completion(_chat(), stream=False)
            assert reply.status_code == 200
    assert len(HITS["proxy-a"]) == 2
    assert HITS["proxy-b"] == []


async def test_restricted_signal_stops_rotation(tmp_path: Path) -> None:
    with _serve("restricted", (403, {"error": {"code": "restricted_request"}})) as upstream:
        adapter = _adapter(upstream, tmp_path, tokens=("codar-token-1", "codar-token-2"))
        with patch.object(adapter._scheduler, "note_failure", AsyncMock()) as note:
            reply = await adapter.chat_completion(_chat(), stream=False)
        assert reply.status_code == 403
        assert b"restricted_request" in (reply.body or b"")
        assert note.await_count == 0
    assert len(HITS["restricted"]) == 1


async def test_non_restricted_codes_keep_original_rotation(tmp_path: Path) -> None:
    # 403 不在 codar 重试表（_retryable 只认 0/401/402/408/429/5xx）：非受限 403 与现状一致，原样透传一次
    with _serve("quota-403", (403, {"error": {"code": "quota"}})) as upstream:
        adapter = _adapter(upstream, tmp_path, tokens=("codar-token-1", "codar-token-2"))
        reply = await adapter.chat_completion(_chat(), stream=False)
        assert reply.status_code == 403
    assert len(HITS["quota-403"]) == 1

    # 可重试状态下非受限码不得短路，仍按账号轮换继续打上游
    with _serve("quota-429", (429, {"error": {"code": "quota"}})) as upstream:
        adapter = _adapter(upstream, tmp_path, tokens=("codar-token-1", "codar-token-2"))
        reply = await adapter.chat_completion(_chat(), stream=False)
        assert reply.status_code == 429
    assert len(HITS["quota-429"]) == 2
