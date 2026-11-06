"""G3：网关请求级限流（每密钥每分钟请求数 + 进程内全局并发上限）。

和已有的几套「限流」刻意分开，互不串味：

- 失败预算（``billing.check_failure_rate``）按 ``(key, provider)`` 读 ``request_logs``，
  只数失败；本模块只在内存里数「打进来的请求」，不读库也不写库，成功/失败一视同仁。
- 账号冷却是 provider 账号池的可用性状态，与调用方密钥无关，本模块不碰 registry。
- 登录节流在 ``/api/login``，本模块只拦 ``/v1/*``，429 不会计入登录失败窗口。

两个开关默认都是 0（关闭）：不配置时连一个计数分支都不会走到，行为与接线前一致。
多 worker 部署时每个进程各算一份；需要跨进程统一额度请前置网关或换共享存储。
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi.responses import JSONResponse

from ..gateway_errors import PROTOCOL_ANTHROPIC, GatewayError, protocol_for_path

logger = logging.getLogger("unified.ratelimit")

# 进程内运行时计数，供 /api/metrics 读取：中间件实例被 add_middleware 收走后
# 外部拿不到引用，所以计数放在模块级。与 G3 的两把闸同生命周期（每进程一份）。
_runtime_lock = threading.Lock()
_runtime_counts: dict[str, int] = {
    "inflight": 0,
    "key_rejected": 0,
    "concurrency_rejected": 0,
}


def runtime_snapshot() -> dict[str, int]:
    """网关运行时快照：当前在途请求数 + 两类限流命中次数。"""
    with _runtime_lock:
        return dict(_runtime_counts)


def _bump(name: str, delta: int = 1) -> None:
    with _runtime_lock:
        _runtime_counts[name] = _runtime_counts.get(name, 0) + delta


# 固定窗口长度：需求口径就是「每分钟」，不单独开 env 旋钮
RATE_WINDOW_SEC = 60
# 网关前缀：只拦 /v1，/healthz、/api/* 与静态产物一律原样放行
GATEWAY_PREFIX = "/v1"
# 窗口字典的清理节奏：每 N 次判定扫一次，避免每个请求都全量遍历
_PRUNE_EVERY = 256


@dataclass(frozen=True)
class RateLimitConfig:
    """两把闸的配置；0 表示该闸关闭（默认即全关）。"""

    rpm: int = 0
    max_concurrency: int = 0
    window_sec: int = RATE_WINDOW_SEC


def _env_int(name: str, default: int = 0) -> int:
    """非法值/负数按关闭处理：限流配错不应该让控制面起不来。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，按关闭处理", name, raw)
        return default
    if value < 0:
        logger.warning("%s=%s 为负数，按关闭处理", name, value)
        return 0
    return value


def load_rate_limit_config() -> RateLimitConfig:
    return RateLimitConfig(
        rpm=_env_int("UNIFIED_RATE_LIMIT_RPM", 0),
        max_concurrency=_env_int("UNIFIED_RATE_LIMIT_MAX_CONCURRENCY", 0),
    )


def _is_gateway_path(path: str) -> bool:
    return path == GATEWAY_PREFIX or path.startswith(GATEWAY_PREFIX + "/")


def _header(scope: dict[str, Any], name: bytes) -> str:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1").strip()
    return ""


def _caller_token(scope: dict[str, Any]) -> str:
    """按 v1 ``_gateway_key`` 的口径取 token，再哈希成计数键。

    只留摘要：内存里不存明文 key，日志与错误体也不可能带出凭据。
    两个头同值时与 v1 等价；冲突时这里不做判定，仍交给 v1 出 401。
    """
    auth = _header(scope, b"authorization")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else auth
    if not token:
        token = _header(scope, b"x-api-key")
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RateLimitMiddleware:
    """纯 ASGI 中间件：并发占用要覆盖流式响应的全过程。

    BaseHTTPMiddleware 在 ``call_next`` 返回时就释放了，流还没吐完就腾出了并发名额；
    这里直接等内层 ASGI 应用跑完再减计数，长流才真正占住名额。
    """

    def __init__(self, app: Any, *, config: RateLimitConfig | None = None) -> None:
        self.app = app
        self.config = config or RateLimitConfig()
        # token 摘要 -> (窗口起点, 已用次数)；固定窗口，过窗重建
        self._windows: dict[str, tuple[float, int]] = {}
        self._inflight = 0
        self._checks = 0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        # 非 HTTP（lifespan/websocket）与探活、管理面一律不经过闸门
        if scope.get("type") != "http" or not _is_gateway_path(path):
            await self.app(scope, receive, send)
            return
        # 浏览器预检不是真实调用，不该消耗密钥额度
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # 在途计数覆盖流式响应的全过程：放行与拒绝都要记，闸门关闭时也要能看见负载
        _bump("inflight")
        try:
            await self._dispatch(scope, receive, send, path)
        finally:
            _bump("inflight", -1)

    async def _dispatch(self, scope: dict[str, Any], receive: Any, send: Any, path: str) -> None:
        cfg = self.config
        if cfg.max_concurrency > 0 and self._inflight >= cfg.max_concurrency:
            _bump("concurrency_rejected")
            await self._reject(
                scope,
                receive,
                send,
                path,
                status=503,
                message="网关并发已满，请稍后重试",
                type_="server_error",
                code="gateway_overloaded",
                detail={
                    "type": "gateway_overloaded",
                    "layer": "gateway",
                    "metric": "concurrency",
                    "limit": cfg.max_concurrency,
                },
            )
            return

        token = _caller_token(scope)
        if cfg.rpm > 0 and token:
            allowed, retry_after = self._take(token, time.monotonic(), cfg.rpm)
            if not allowed:
                _bump("key_rejected")
                await self._reject(
                    scope,
                    receive,
                    send,
                    path,
                    status=429,
                    message="密钥请求频率超限，请稍后重试",
                    type_="rate_limit_error",
                    code="key_rate_limited",
                    detail={
                        "type": "key_rate_limited",
                        "layer": "key",
                        "metric": "requests",
                        "window": "fixed",
                        "unit": "requests",
                        "used": cfg.rpm,
                        "limit": cfg.rpm,
                        "reset_after": retry_after,
                    },
                    retry_after=retry_after,
                )
                return

        if cfg.max_concurrency > 0:
            self._inflight += 1
            try:
                await self.app(scope, receive, send)
            finally:
                self._inflight -= 1
            return
        await self.app(scope, receive, send)

    def _take(self, token: str, now: float, rpm: int) -> tuple[bool, int]:
        """返回 (是否放行, 超限时的 Retry-After 秒数)。"""
        self._checks += 1
        if self._checks % _PRUNE_EVERY == 0:
            self._prune(now)
        window = self._windows.get(token)
        if window is None or now - window[0] >= self.config.window_sec:
            self._windows[token] = (now, 1)
            return True, 0
        started, used = window
        if used >= rpm:
            retry = max(1, math.ceil(self.config.window_sec - (now - started)))
            return False, retry
        self._windows[token] = (started, used + 1)
        return True, 0

    def _prune(self, now: float) -> None:
        expired = [
            token
            for token, (started, _) in self._windows.items()
            if now - started >= self.config.window_sec
        ]
        for token in expired:
            self._windows.pop(token, None)

    async def _reject(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        path: str,
        *,
        status: int,
        message: str,
        type_: str,
        code: str,
        detail: dict[str, Any],
        retry_after: int = 0,
    ) -> None:
        """拒绝也走统一错误壳：messages 出 Anthropic，其余 /v1 出 OpenAI。

        GatewayError 自带凭据掩码与端点脱敏；这里只喂静态文案和计数，不含上游地址。
        """
        err = GatewayError(
            status,
            message,
            type_=type_,
            code=code,
            detail=detail,
            retry_after=str(retry_after) if retry_after else None,
        )
        payload = (
            err.anthropic_payload()
            if protocol_for_path(path) == PROTOCOL_ANTHROPIC
            else err.openai_payload()
        )
        response = JSONResponse(payload, status_code=status, headers=dict(err.headers or {}))
        await response(scope, receive, send)
