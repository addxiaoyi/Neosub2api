"""上游 HTTP 客户端层：按 (base_url, 凭据指纹, 代理, 附加头) 复用 AsyncClient。

Windows 上 httpx.AsyncClient 构造会重建 SSLContext（加载系统证书库约 4~5s），
每请求新建会把网关自身开销完全淹没。这里把 TLS 上下文与 client（含连接池）
都做成进程级共享，按事件循环隔离（httpx 连接池绑定创建它的事件循环）；
关闭时由 aclose_clients() / atexit 统一释放。
"""

import asyncio
import atexit
import hashlib
import logging
import os
import ssl
import threading
import weakref
from dataclasses import dataclass
from typing import Any

import httpx

from .base import ProviderError, parse_vendor_error

logger = logging.getLogger("unified.upstream")

# 每个上游 client 的连接上限；并发高时可调大，避免排队而不是新建 client
MAX_CONNECTIONS_ENV = "UNIFIED_UPSTREAM_MAX_CONNECTIONS"
MAX_KEEPALIVE_ENV = "UNIFIED_UPSTREAM_MAX_KEEPALIVE"
DEFAULT_MAX_CONNECTIONS = 100
DEFAULT_MAX_KEEPALIVE = 50

_ssl_context: ssl.SSLContext | None = None
_ssl_guard = threading.Lock()

# loop -> {(base_url, 凭据指纹, 代理, 附加头): client}
# WeakKeyDictionary：事件循环结束后自动断掉引用，避免测试/多循环场景泄漏 key
_pool: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[Any, ...], httpx.AsyncClient]]" = (
    weakref.WeakKeyDictionary()
)
_pool_guard = threading.Lock()


@dataclass
class UpstreamReply:
    """上游响应。非流式直接带 body，流式带一个可迭代的 httpx.Response。"""

    status_code: int
    headers: dict[str, str]
    body: bytes | None = None
    response: httpx.Response | None = None

    async def aclose(self) -> None:
        if self.response is not None:
            await self.response.aclose()

    async def iter_bytes(self):
        if self.response is None:
            return
        try:
            async for chunk in self.response.aiter_bytes():
                yield chunk
        finally:
            await self.aclose()


def _tls_context() -> ssl.SSLContext:
    """进程内共用一份 SSLContext：证书库只加载一次。"""
    global _ssl_context
    if _ssl_context is None:
        with _ssl_guard:
            if _ssl_context is None:
                _ssl_context = ssl.create_default_context()
    return _ssl_context


def _limits() -> httpx.Limits:
    max_connections = int(os.getenv(MAX_CONNECTIONS_ENV, str(DEFAULT_MAX_CONNECTIONS)))
    max_keepalive = int(os.getenv(MAX_KEEPALIVE_ENV, str(DEFAULT_MAX_KEEPALIVE)))
    return httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive,
    )


def _client_key(
    base_url: str, api_key: str, proxy: str, extra_headers: dict[str, str]
) -> tuple[Any, ...]:
    # 凭据/头只留指纹：池 key 不进日志、不含明文密钥
    header_digest = hashlib.sha256(
        repr(sorted(extra_headers.items())).encode()
    ).hexdigest()[:16]
    fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:16] if api_key else ""
    return (base_url, fingerprint, proxy, header_digest)


def _shared_client(
    base_url: str,
    api_key: str,
    proxy: str,
    extra_headers: dict[str, str],
    timeout: float,
) -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    key = _client_key(base_url, api_key, proxy, extra_headers)
    with _pool_guard:
        bucket = _pool.get(loop)
        if bucket is None:
            bucket = {}
            _pool[loop] = bucket
        client = bucket.get(key)
        if client is not None:
            return client

        kwargs: dict[str, Any] = {
            "base_url": base_url,
            "timeout": httpx.Timeout(timeout, connect=10.0),
            # 不读系统代理：内网上游被系统代理劫持是常见坑
            "trust_env": False,
            "limits": _limits(),
            # 即使目标是 HTTP 上游，也复用共享 TLS 上下文，杜绝每次重建证书库
            "verify": _tls_context(),
        }
        if proxy:
            kwargs["proxy"] = proxy
        client = httpx.AsyncClient(**kwargs)
        bucket[key] = client
        return client


async def aclose_clients() -> None:
    """关闭当前事件循环下的所有上游 client（应用关停时调用）。"""
    loop = asyncio.get_running_loop()
    with _pool_guard:
        bucket = _pool.pop(loop, {})
    for client in bucket.values():
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - 关停阶段尽力释放，单条失败不影响其余
            logger.debug("上游 client 关闭失败", exc_info=True)


async def _aclose_all(clients: list[httpx.AsyncClient]) -> None:
    for client in clients:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass


def _close_pool_at_exit() -> None:
    """解释器退出兜底：尽力关闭所有残留连接。"""
    with _pool_guard:
        buckets = list(_pool.values())
        _pool.clear()
    clients = [client for bucket in buckets for client in bucket.values()]
    if not clients:
        return
    try:
        asyncio.run(_aclose_all(clients))
    except Exception:  # noqa: BLE001 - 退出阶段事件循环可能已关闭，socket 交由 OS 回收
        pass


atexit.register(_close_pool_at_exit)


class UnifiedApiClient:
    """所有 provider 适配器共用的 HTTP 客户端层：鉴权注入、超时、连通性错误统一成 ProviderError。"""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 120.0,
        proxy: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.proxy = proxy
        self.extra_headers = dict(extra_headers or {})

    def _headers(self, accept: str | None = None) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if accept:
            headers["accept"] = accept
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.timeout, connect=10.0)

    def _client(self) -> httpx.AsyncClient:
        return _shared_client(self.base_url, self.api_key, self.proxy, self.extra_headers, self.timeout)

    async def request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        client = self._client()
        try:
            resp = await client.request(
                method, path, json=payload, headers=self._headers(), timeout=self._timeout()
            )
        except httpx.RequestError as exc:
            logger.warning("上游 %s 不可达：%s", self.base_url, exc)
            raise ProviderError(502, "上游不可达") from exc
        try:
            if resp.status_code >= 400:
                # 完整响应体只进服务端日志；对外只给解析后的厂商 code/描述（掩码后）与状态码
                logger.warning("上游 %s%s 返回 %s：%s", self.base_url, path, resp.status_code, resp.text[:200])
                code, vendor_message = parse_vendor_error(resp.text)
                raise ProviderError(
                    resp.status_code,
                    f"上游 {path} 返回 {resp.status_code}",
                    upstream_code=code,
                    upstream_message=vendor_message,
                )
            return resp.json()
        finally:
            # 连接归还连接池，client 本身保持复用
            await resp.aclose()

    async def post_json(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request_json("POST", path, payload if payload is not None else {})

    async def put_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request_json("PUT", path, payload)

    async def delete_json(self, path: str) -> dict[str, Any]:
        return await self.request_json("DELETE", path)

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        client = self._client()
        try:
            resp = await client.get(path, params=params, headers=self._headers(), timeout=self._timeout())
        except httpx.RequestError as exc:
            logger.warning("上游 %s 不可达：%s", self.base_url, exc)
            raise ProviderError(502, "上游不可达") from exc
        try:
            if resp.status_code >= 400:
                code, vendor_message = parse_vendor_error(resp.text)
                raise ProviderError(
                    resp.status_code,
                    f"上游 {path} 返回 {resp.status_code}",
                    upstream_code=code,
                    upstream_message=vendor_message,
                )
            return resp.json()
        finally:
            await resp.aclose()

    async def forward(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        stream: bool,
        method: str = "POST",
        raw: bytes | None = None,
        accept: str | None = None,
    ) -> UpstreamReply:
        """转发一次请求。`raw` 给那些要自己编码请求体的上游（如 COSY），此时忽略 payload。"""
        client = self._client()
        accept = accept or ("text/event-stream" if stream else "application/json")
        try:
            request = client.build_request(
                method,
                path,
                content=raw,
                json=None if raw is not None else (payload if payload is not None else {}),
                headers=self._headers(accept),
                timeout=self._timeout(),
            )
            response = await client.send(request, stream=stream)
        except httpx.RequestError as exc:
            logger.warning("上游 %s 不可达：%s", self.base_url, exc)
            raise ProviderError(502, "上游不可达") from exc

        # 429 的 Retry-After 要原样带回调用方，否则限流节奏只能靠猜
        passthrough = {"content-type": response.headers.get("content-type", "text/event-stream")}
        if response.headers.get("retry-after"):
            passthrough["retry-after"] = response.headers["retry-after"]

        if stream:
            return UpstreamReply(
                status_code=response.status_code,
                headers=passthrough,
                response=response,
            )

        try:
            body = await response.aread()
        finally:
            # 读完立刻归还连接；client 由连接池长期持有
            await response.aclose()
        return UpstreamReply(
            status_code=response.status_code,
            headers={**passthrough, "content-type": response.headers.get("content-type", "application/json")},
            body=body,
        )
