"""qoder_sidecar 管理面原样透传：会话导出 / 导入（上游 issue #87 `SessionCopy`）。

上游契约（本地一手源码副本 commit 5cf62da，
`crates/buddy-switch-server/src/api.rs:259-264`）：

- `POST /api/sessions/export`  body `{uid?, sessionIds?, outPath, region?}`（只读，包写到 sidecar 主机上的 outPath）
- `POST /api/sessions/import`  body `{uid?, path, region?}`（加法写，重复导入产生副本）

两条都由 `require_capability(Product::Workbuddy, Capability::SessionCopy)` 门控，响应是 sidecar 自己的
JSON 壳。本层只搬字节：不改写请求体、不解析/包装成功响应，状态码与 content-type 原样回传。
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ..security import admin_user

router = APIRouter(prefix="/api/providers/qoder_sidecar/sessions", tags=["qoder-sessions"])

PROVIDER_ID = "qoder_sidecar"


async def _proxy(adapter, path: str, raw: bytes) -> Response:
    """转发一次管理面 POST；成功与失败都保留上游状态码/响应体/content-type。"""
    reply = await adapter._client().forward(path, raw=raw, stream=False, method="POST")
    headers = {"content-type": reply.headers.get("content-type") or "application/json"}
    if reply.headers.get("retry-after"):
        headers["retry-after"] = reply.headers["retry-after"]
    return Response(content=reply.body or b"", status_code=reply.status_code, headers=headers)


async def _pass_through(request: Request, path: str) -> Response:
    adapter = request.app.state.registry.get(PROVIDER_ID)
    raw = await request.body()
    return await _proxy(adapter, path, raw)


@router.post("/export")
async def export_sessions(request: Request, user: dict = Depends(admin_user)):
    return await _pass_through(request, "/api/sessions/export")


@router.post("/import")
async def import_sessions(request: Request, user: dict = Depends(admin_user)):
    return await _pass_through(request, "/api/sessions/import")
