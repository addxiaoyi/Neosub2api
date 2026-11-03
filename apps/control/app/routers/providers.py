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
