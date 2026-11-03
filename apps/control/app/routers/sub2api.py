"""sub2api 配额域管理 API：分组、账号池、流水与额度重置。"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..adapters.base import ProviderError
from ..adapters.sub2api_store import build_store
from ..security import admin_user, quota_admin

router = APIRouter(prefix="/api/sub2api", tags=["sub2api"])


def _store(request: Request):
    return build_store(request.app.state.settings)


class GroupCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = ""
    platform: str = "openai"
    quota_limit: float = 0


class GroupPatch(BaseModel):
    name: str | None = None
    platform: str | None = None
    quota_limit: float | None = None
    enabled: bool | None = None
    reset_quota: bool = False


class AccountCreate(BaseModel):
    group_id: str
    name: str = ""
    upstream_base: str = ""
    api_key: str = ""
    weight: int = 1
    quota_limit: float = 0


class AccountPatch(BaseModel):
    name: str | None = None
    upstream_base: str | None = None
    api_key: str | None = None
    weight: int | None = None
    quota_limit: float | None = None
    enabled: bool | None = None
    clear_cooldown: bool = False


def _guard(exc: ProviderError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get("/groups")
def list_groups(request: Request, user: dict = Depends(quota_admin)):
    return _store(request).list_groups()


@router.post("/groups")
def create_group(body: GroupCreate, request: Request, user: dict = Depends(admin_user)):
    try:
        return _store(request).upsert_group(
            body.id, name=body.name, platform=body.platform, quota_limit=body.quota_limit
        )
    except ProviderError as exc:
        raise _guard(exc) from exc


@router.patch("/groups/{group_id}")
def patch_group(group_id: str, body: GroupPatch, request: Request, user: dict = Depends(admin_user)):
    try:
        return _store(request).update_group(
            group_id,
            name=body.name,
            platform=body.platform,
            quota_limit=body.quota_limit,
            enabled=body.enabled,
            reset_quota=body.reset_quota,
        )
    except ProviderError as exc:
        raise _guard(exc) from exc


@router.delete("/groups/{group_id}")
def delete_group(group_id: str, request: Request, user: dict = Depends(admin_user)):
    store = _store(request)
    if not store.get_group(group_id):
        raise HTTPException(status_code=404, detail=f"未知的配额分组：{group_id}")
    store.delete_group(group_id)
    return {"ok": True}


@router.get("/accounts")
def list_accounts(request: Request, group_id: str | None = None, user: dict = Depends(quota_admin)):
    return _store(request).list_accounts(group_id)


@router.post("/accounts")
def create_account(body: AccountCreate, request: Request, user: dict = Depends(admin_user)):
    try:
        return _store(request).add_account(
            body.group_id,
            name=body.name,
            upstream_base=body.upstream_base,
            api_key=body.api_key,
            weight=body.weight,
            quota_limit=body.quota_limit,
        )
    except ProviderError as exc:
        raise _guard(exc) from exc


@router.patch("/accounts/{account_id}")
def patch_account(account_id: int, body: AccountPatch, request: Request, user: dict = Depends(admin_user)):
    try:
        return _store(request).update_account(
            account_id,
            name=body.name,
            upstream_base=body.upstream_base,
            api_key=body.api_key,
            weight=body.weight,
            quota_limit=body.quota_limit,
            enabled=body.enabled,
            clear_cooldown=body.clear_cooldown,
        )
    except ProviderError as exc:
        raise _guard(exc) from exc


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, request: Request, user: dict = Depends(admin_user)):
    try:
        _store(request).delete_account(account_id)
    except ProviderError as exc:
        raise _guard(exc) from exc
    return {"ok": True}


@router.post("/accounts/{account_id}/reset")
def reset_account(account_id: int, request: Request, user: dict = Depends(admin_user)):
    try:
        return _store(request).reset_account_quota(account_id)
    except ProviderError as exc:
        raise _guard(exc) from exc


@router.get("/ledger")
def ledger(
    request: Request,
    group_id: str | None = None,
    limit: int = 50,
    user: dict = Depends(quota_admin),
):
    data: list[dict[str, Any]] = _store(request).ledger(group_id, limit)
    return data
