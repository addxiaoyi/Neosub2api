"""gptGrok2api 媒体子工程客户端。

图片/视频/文件/可编辑文件任务是 Go 侧独立子工程（media gateway），认证与 chat 通道
（ChatGPT/Grok Web 凭证）不同，因此这里单独建客户端，不混进 gptgrok 的会话逻辑。
二进制产物全部留在上游，本模块只收发 JSON 与透传字节。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

from ..adapters.base import ProviderError
from ..adapters.client import UnifiedApiClient
from ..api_utils import mask_text

# 连接/普通读/生成三档超时：生成接口上游要排队出图出片，不能用普通读的 60s
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 60.0
GENERATE_TIMEOUT = 180.0


def media_base(options: dict[str, Any] | None) -> str:
    return str((options or {}).get("media_base") or "").strip().rstrip("/")


def media_key(options: dict[str, Any] | None) -> str:
    return str((options or {}).get("media_api_key") or "").strip()


class MediaReply:
    """网关读取端点需要的响应形状；不直接暴露 httpx 对象。"""

    def __init__(self, status_code: int, *, body: bytes, content_type: str = "application/json") -> None:
        self.status_code = status_code
        self.body = body
        self.headers = {"content-type": content_type}

    async def iter_bytes(self):
        if self.body:
            yield self.body

    async def aclose(self) -> None:
        return None


class MediaClient:
    """只依赖 provider options 的媒体子工程调用层。"""

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        # 复用适配器层的连接池与共享 TLS 上下文：Windows 上每次新建 client 会重建证书库
        self._http = UnifiedApiClient(self.base_url, api_key, timeout=READ_TIMEOUT)

    def _headers(self, *, json_body: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {"accept": "application/json"}
        if json_body:
            headers["content-type"] = "application/json"
        if self._http.api_key:
            headers["authorization"] = f"Bearer {self._http.api_key}"
        return headers

    def _timeout(self, timeout: float | None) -> httpx.Timeout:
        return httpx.Timeout(timeout or READ_TIMEOUT, connect=CONNECT_TIMEOUT)

    @staticmethod
    def _error(resp: httpx.Response) -> ProviderError:
        code, message = "", ""
        try:
            raw = resp.json()
        except (ValueError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            error = raw.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or error.get("type") or "")
                message = str(error.get("message") or "")
            elif isinstance(error, str):
                message = error
            message = message or str(raw.get("message") or "")
        detail = message or mask_text(resp.text[:200])
        return ProviderError(
            resp.status_code,
            f"媒体子工程返回 {resp.status_code}：{detail}",
            upstream_code=code or None,
            upstream_message=message or None,
        )

    async def _json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        client = self._http._client()
        try:
            resp = await client.request(
                method, path, json=payload, headers=self._headers(), timeout=self._timeout(timeout)
            )
        except httpx.RequestError as exc:
            raise ProviderError(502, f"媒体子工程不可达：{mask_text(str(exc))}") from exc
        if resp.status_code >= 400:
            raise self._error(resp)
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(502, "媒体子工程返回非 JSON") from exc

    async def _raw(self, path: str, *, timeout: float | None = None) -> MediaReply:
        client = self._http._client()
        try:
            resp = await client.get(path, headers=self._headers(json_body=False), timeout=self._timeout(timeout))
        except httpx.RequestError as exc:
            raise ProviderError(502, f"媒体子工程不可达：{mask_text(str(exc))}") from exc
        if resp.status_code >= 400:
            raise self._error(resp)
        return MediaReply(
            resp.status_code,
            body=resp.content,
            content_type=resp.headers.get("content-type", "application/octet-stream"),
        )

    # ---------- 2-A1 图片 ----------

    async def images_generate(self, payload: dict[str, Any]) -> Any:
        return await self._json(
            "POST", "/v1/images/generations", payload, timeout=GENERATE_TIMEOUT
        )

    async def images_edit(self, payload: dict[str, Any]) -> Any:
        return await self._json("POST", "/v1/images/edits", payload, timeout=GENERATE_TIMEOUT)

    # ---------- 2-A2 视频 ----------

    @staticmethod
    def _video_fields(payload: dict[str, Any]) -> dict[str, str]:
        fields: dict[str, str] = {}
        for name in ("model", "prompt", "seconds", "size", "resolution_name", "preset"):
            value = payload.get(name)
            if value is None or value == "":
                continue
            fields[name] = str(value)
        return fields

    async def videos_create(self, payload: dict[str, Any]) -> Any:
        # 上游只收 multipart，网关对外仍是 JSON：字段逐个搬过去，不猜语义
        # files 里用 (None, value) 才能强制 multipart 文本段；data= 会被 httpx 编成 urlencoded
        client = self._http._client()
        fields = self._video_fields(payload)
        try:
            resp = await client.post(
                "/v1/videos",
                files={name: (None, value) for name, value in fields.items()},
                headers=self._headers(json_body=False),
                timeout=self._timeout(GENERATE_TIMEOUT),
            )
        except httpx.RequestError as exc:
            raise ProviderError(502, f"媒体子工程不可达：{mask_text(str(exc))}") from exc
        if resp.status_code >= 400:
            raise self._error(resp)
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(502, "媒体子工程返回非 JSON") from exc

    async def videos_get(self, task_id: str) -> Any:
        return await self._json("GET", f"/v1/videos/{task_id}")

    async def media_get(self, name: str) -> MediaReply:
        return await self._raw(f"/v1/media/{name}")

    # ---------- 2-A4 文件 ----------

    async def files_upload(
        self, *, filename: str, content_type: str, data: bytes, purpose: str = ""
    ) -> Any:
        client = self._http._client()
        form: dict[str, str] = {}
        if purpose:
            form["purpose"] = purpose
        files = {"file": (filename or "upload.bin", data, content_type or "application/octet-stream")}
        try:
            resp = await client.post(
                "/v1/files",
                files=files,
                data=form,
                headers=self._headers(json_body=False),
                timeout=self._timeout(None),
            )
        except httpx.RequestError as exc:
            raise ProviderError(502, f"媒体子工程不可达：{mask_text(str(exc))}") from exc
        if resp.status_code >= 400:
            raise self._error(resp)
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(502, "媒体子工程返回非 JSON") from exc

    async def files_list(self) -> Any:
        return await self._json("GET", "/v1/files")

    async def files_delete(self, file_id: str) -> Any:
        return await self._json("DELETE", f"/v1/files?id={quote(file_id, safe='')}")

    # ---------- 2-A3 可编辑文件任务 ----------

    async def editable_create(self, payload: dict[str, Any]) -> Any:
        return await self._json("POST", "/v1/editable-file-tasks", payload, timeout=GENERATE_TIMEOUT)

    async def editable_get(self, task_id: str) -> Any:
        return await self._json("GET", f"/v1/editable-file-tasks/{task_id}")

    async def editable_list(self, ids: str = "") -> Any:
        query = f"?ids={quote(ids, safe=',')}" if ids else ""
        return await self._json("GET", f"/v1/editable-file-tasks{query}")
