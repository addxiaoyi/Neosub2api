"""qoder_sidecar 会话导出/导入透传：上游状态码、content-type 与字节原样回传。"""

from __future__ import annotations

import asyncio

from app.adapters.client import UpstreamReply
from app.routers.qoder_sessions import _proxy

BODY = b'{"ok":true,"exported":3,"sessionIds":["s1","s2"]}'


class _StubClient:
    def __init__(self, reply: UpstreamReply) -> None:
        self.reply = reply
        self.calls: list[tuple[str, bytes | None]] = []

    async def forward(self, path, payload=None, *, stream, method="POST", raw=None):
        self.calls.append((path, raw))
        return self.reply


class _StubAdapter:
    def __init__(self, reply: UpstreamReply) -> None:
        self.client = _StubClient(reply)

    def _client(self):
        return self.client


def _run(reply: UpstreamReply, path: str, raw: bytes):
    adapter = _StubAdapter(reply)
    response = asyncio.run(_proxy(adapter, path, raw))
    return adapter, response


def test_proxy_preserves_status_content_type_and_bytes() -> None:
    adapter, response = _run(
        UpstreamReply(403, {"content-type": "application/json"}, body=b'{"error":"SessionCopy"}'),
        "/api/sessions/export",
        b'{"outPath":"/tmp/bundle.json"}',
    )
    assert adapter.client.calls == [("/api/sessions/export", b'{"outPath":"/tmp/bundle.json"}')]
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/json"
    assert response.body == b'{"error":"SessionCopy"}'


def test_proxy_does_not_wrap_binary_or_stream_content_types() -> None:
    _, response = _run(
        UpstreamReply(200, {"content-type": "application/x-ndjson"}, body=BODY),
        "/api/sessions/import",
        b'{"path":"/tmp/bundle.json"}',
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-ndjson"
    assert response.body == BODY
