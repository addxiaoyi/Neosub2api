import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .base import ProviderError, parse_vendor_error

logger = logging.getLogger("unified.upstream")


@dataclass
class UpstreamReply:
    """上游响应。非流式直接带 body，流式带一个可迭代的 httpx.Response。"""

    status_code: int
    headers: dict[str, str]
    body: bytes | None = None
    response: httpx.Response | None = None
    client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self.response is not None:
            await self.response.aclose()
        if self.client is not None:
            await self.client.aclose()

    async def iter_bytes(self):
        if self.response is None:
            return
        try:
            async for chunk in self.response.aiter_bytes():
                yield chunk
        finally:
            await self.aclose()


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

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": httpx.Timeout(self.timeout, connect=10.0),
            # 不读系统代理：内网上游被系统代理劫持是常见坑
            "trust_env": False,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.AsyncClient(**kwargs)

    async def request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        client = self._client()
        try:
            resp = await client.request(method, path, json=payload, headers=self._headers())
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
        except httpx.RequestError as exc:
            logger.warning("上游 %s 不可达：%s", self.base_url, exc)
            raise ProviderError(502, "上游不可达") from exc
        finally:
            await client.aclose()

    async def post_json(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request_json("POST", path, payload if payload is not None else {})

    async def put_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request_json("PUT", path, payload)

    async def delete_json(self, path: str) -> dict[str, Any]:
        return await self.request_json("DELETE", path)

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        client = self._client()
        try:
            resp = await client.get(path, params=params, headers=self._headers())
            if resp.status_code >= 400:
                code, vendor_message = parse_vendor_error(resp.text)
                raise ProviderError(
                    resp.status_code,
                    f"上游 {path} 返回 {resp.status_code}",
                    upstream_code=code,
                    upstream_message=vendor_message,
                )
            return resp.json()
        except httpx.RequestError as exc:
            logger.warning("上游 %s 不可达：%s", self.base_url, exc)
            raise ProviderError(502, "上游不可达") from exc
        finally:
            await client.aclose()

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
            )
            response = await client.send(request, stream=stream)
        except httpx.RequestError as exc:
            await client.aclose()
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
                client=client,
            )

        try:
            body = await response.aread()
        finally:
            await response.aclose()
            await client.aclose()
        return UpstreamReply(
            status_code=response.status_code,
            headers={**passthrough, "content-type": response.headers.get("content-type", "application/json")},
            body=body,
        )
