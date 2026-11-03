"""统一凭据库 API：读 viewer、写 admin。

密钥只落库、只在服务端流转；任何响应/审计/日志都只出现掩码或布尔位，
不出现明文（复用 api_utils.mask_secret，与 settings 的脱敏口径一致）。
"""

from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..adapters.base import ProviderError
from ..api_utils import mask_secret
from ..security import admin_user, audit_event, current_user

router = APIRouter(prefix="/api/provider-credentials", tags=["provider-credentials"])

# 写审计时只记字段名，绝不记值
AUDIT_FIELDS = ("label", "base_url", "secret", "enabled", "sort_order", "note")


def _public(row: dict[str, Any]) -> dict[str, Any]:
    secret = str(row.get("secret") or "")
    return {
        "id": int(row["id"]),
        "provider": row["provider"],
        "label": row["label"],
        "base_url": row["base_url"],
        "secret_masked": mask_secret(secret),
        "has_secret": bool(secret.strip()),
        "enabled": bool(row["enabled"]),
        "sort_order": int(row["sort_order"]),
        "note": row["note"],
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
    }


def _fail(exc: ProviderError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _audit(request: Request, actor: dict, action: str, target: str, detail: str) -> None:
    conn = request.app.state.db.connect()
    try:
        audit_event(
            conn,
            actor["username"],
            action,
            target,
            detail,
            request.client.host if request.client else "",
        )
    finally:
        conn.close()


def _detail(body: BaseModel) -> str:
    fields = sorted(body.model_dump(exclude_none=True).keys())
    return ",".join(field for field in fields if field in AUDIT_FIELDS)


@router.get("")
def list_credentials(
    request: Request,
    provider: str | None = None,
    user: dict = Depends(current_user),
):
    rows = request.app.state.registry.credentials(provider)
    return [_public(row) for row in rows]


class CredentialIn(BaseModel):
    provider: str
    label: str = ""
    base_url: str = ""
    # 空串 = 暂不填密钥（如 clash 类 sidecar 无需鉴权）
    secret: str = ""
    enabled: bool = True
    sort_order: int | None = None
    note: str = ""


@router.post("")
def create_credential(
    body: CredentialIn,
    request: Request,
    user: dict = Depends(admin_user),
):
    try:
        row = request.app.state.registry.create_credential(
            body.provider,
            label=body.label,
            base_url=body.base_url,
            secret=body.secret,
            enabled=body.enabled,
            sort_order=body.sort_order,
            note=body.note,
        )
    except ProviderError as exc:
        _fail(exc)
    _audit(
        request,
        user,
        "provider_credentials.create",
        f"{row['provider']}#{row['id']}",
        _detail(body),
    )
    return _public(row)


class CredentialPatch(BaseModel):
    label: str | None = None
    base_url: str | None = None
    # "***" = 保持不变；空串 = 清除
    secret: str | None = None
    enabled: bool | None = None
    sort_order: int | None = None
    note: str | None = None


@router.patch("/{credential_id}")
def update_credential(
    credential_id: int,
    body: CredentialPatch,
    request: Request,
    user: dict = Depends(admin_user),
):
    try:
        row = request.app.state.registry.update_credential(
            credential_id,
            label=body.label,
            base_url=body.base_url,
            secret=body.secret,
            enabled=body.enabled,
            sort_order=body.sort_order,
            note=body.note,
        )
    except ProviderError as exc:
        _fail(exc)
    _audit(
        request,
        user,
        "provider_credentials.update",
        f"{row['provider']}#{row['id']}",
        _detail(body),
    )
    return _public(row)


@router.delete("/{credential_id}")
def delete_credential(
    credential_id: int,
    request: Request,
    user: dict = Depends(admin_user),
):
    try:
        row = request.app.state.registry.delete_credential(credential_id)
    except ProviderError as exc:
        _fail(exc)
    _audit(
        request,
        user,
        "provider_credentials.delete",
        f"{row['provider']}#{row['id']}",
        "",
    )
    return {"ok": True, "id": credential_id}
