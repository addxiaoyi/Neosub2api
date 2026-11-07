"""客户端断连噪声的过滤：正常事件不再打 Traceback，服务端异常照旧可见。"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.main import silence_client_disconnects


class _FakeLoop:
    """只实现异常处理器接口，避免为单个处理器起真实事件循环。"""

    def __init__(self, previous=None):
        self.handler = previous
        self.default_calls: list[dict] = []

    def get_exception_handler(self):
        return self.handler

    def set_exception_handler(self, handler):
        self.handler = handler

    def default_exception_handler(self, context):
        self.default_calls.append(context)


@pytest.mark.parametrize(
    "exc",
    [ConnectionResetError(10054, "reset"), ConnectionAbortedError(10053, "aborted"), BrokenPipeError(32, "pipe")],
)
def test_client_disconnect_skips_previous_handler(exc, caplog):
    seen = []
    loop = _FakeLoop(previous=lambda _loop, ctx: seen.append(ctx))
    silence_client_disconnects(loop)

    with caplog.at_level(logging.DEBUG, logger="unified.security"):
        loop.handler(loop, {"message": "connection lost", "exception": exc})

    assert seen == []
    assert loop.default_calls == []
    assert any("客户端已断开连接" in record.message for record in caplog.records)


def test_runtime_error_still_reaches_previous_handler():
    seen = []
    loop = _FakeLoop(previous=lambda _loop, ctx: seen.append(ctx))
    silence_client_disconnects(loop)

    exc = RuntimeError("transport bug")
    loop.handler(loop, {"message": "boom", "exception": exc})

    assert seen == [{"message": "boom", "exception": exc}]


def test_runtime_error_falls_back_to_default_handler():
    loop = _FakeLoop()
    silence_client_disconnects(loop)

    context = {"message": "boom", "exception": RuntimeError("transport bug")}
    loop.handler(loop, context)

    assert loop.default_calls == [context]


def test_context_without_exception_is_not_swallowed():
    """没有 exception 字段的告警（如回调未完成）不该被当成断连静音。"""
    seen = []
    loop = _FakeLoop(previous=lambda _loop, ctx: seen.append(ctx))
    silence_client_disconnects(loop)

    loop.handler(loop, {"message": "task was destroyed but it is pending"})

    assert seen == [{"message": "task was destroyed but it is pending"}]


def test_real_loop_chains_handler():
    loop = asyncio.new_event_loop()
    seen = []
    loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))
    try:
        silence_client_disconnects(loop)
        loop.call_exception_handler({"message": "reset", "exception": ConnectionResetError(10054, "reset")})
        loop.call_exception_handler({"message": "boom", "exception": RuntimeError("bug")})
    finally:
        loop.close()

    assert [ctx["message"] for ctx in seen] == ["boom"]
