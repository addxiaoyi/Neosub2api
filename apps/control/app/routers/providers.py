from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..adapters.base import ProviderError
from ..security import admin_user, audit_event, current_user

router = APIRouter(prefix="/api/providers", tags=["providers"])


class ProviderPatch(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None
    label: str | None = None
    # "***" = 保持不变；空串 = 清除已配置的 key
    api_key: str | None = None
    options: dict[str, Any] | None = None


@router.get("")
def list_providers(request: Request, user: dict = Depends(current_user)):
    return request.app.state.registry.list_public()


def _known_provider(request: Request, provider: str) -> bool:
    return any(item["id"] == provider for item in request.app.state.registry.list_public())


@router.get("/health")
def provider_health(
    request: Request,
    provider: str | None = None,
    limit: int = 20,
    user: dict = Depends(current_user),
):
    """当前每家的最近一次检测结果 + 最近若干条历史；viewer 可读，只观测不控制路由。"""
    if provider and not _known_provider(request, provider):
        raise HTTPException(status_code=404, detail=f"未知服务商：{provider}")
    return request.app.state.health_service.snapshot(provider=provider, limit=limit)


@router.post("/health/run")
async def run_provider_health(
    request: Request,
    provider: str | None = None,
    user: dict = Depends(admin_user),
):
    """手动立即跑一轮；与总开关无关（关着也能手动验一次）。"""
    if provider and not _known_provider(request, provider):
        raise HTTPException(status_code=404, detail=f"未知服务商：{provider}")
    result = await request.app.state.health_service.run_once(provider=provider)
    conn = request.app.state.db.connect()
    try:
        audit_event(
            conn,
            user["username"],
            "providers.health.run",
            provider or "*",
            f"checked={len(result['providers'])}",
            request.client.host if request.client else "",
        )
    finally:
        conn.close()
    return result


@router.patch("/{provider_id}")
def patch_provider(
    provider_id: str,
    body: ProviderPatch,
    request: Request,
    user: dict = Depends(admin_user),
):
    try:
        result = request.app.state.registry.update(
            provider_id,
            enabled=body.enabled,
            base_url=body.base_url,
            label=body.label,
            api_key=body.api_key,
            options=body.options,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    # 只记字段名，不落密钥值（S-21）
    fields = sorted(body.model_dump(exclude_none=True).keys())
    conn = request.app.state.db.connect()
    try:
        audit_event(
            conn,
            user["username"],
            "providers.patch",
            provider_id,
            ",".join(fields),
            request.client.host if request.client else "",
        )
    finally:
        conn.close()
    return result
