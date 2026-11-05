"""在线更新接口：状态聚合、主动检测、执行、回滚。

所有返回都收口到同一个 UpdateStatus 结构（前端把它当成刷新面板的完整对象），
job 来自执行器（含 data/updates/job.json 里持久化的上一轮结果，重启后可读）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..api_utils import db_of, mask_text
from ..security import admin_user, audit_event, current_user
from ..services import ops_lock
from ..services.update_manifest import UpdateError
from ..services.update_feed import UpdateFeed

router = APIRouter(prefix="/api/system", tags=["system"])


class ApplyBody(BaseModel):
    # 逐字确认版本号，防手滑把旧版本覆盖回去；长度限制只是护栏，语义仍是全等比较
    confirm_version: str = Field(min_length=1, max_length=64)


def feed_of(request: Request) -> UpdateFeed:
    return request.app.state.update_feed


def status_payload(request: Request, *, force: bool = False) -> dict[str, Any]:
    feed = feed_of(request)
    payload = feed.summary(force=force)
    payload["job"] = request.app.state.update_executor.status()
    return payload


def _audit(request: Request, username: str, action: str, target: str, detail: str = "") -> None:
    conn = db_of(request)
    try:
        audit_event(
            conn,
            username,
            action,
            target,
            detail=mask_text(detail),
            ip=request.client.host if request.client else "",
        )
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"操作已执行，但审计日志写入失败：{exc}") from exc
    finally:
        conn.close()


@router.get("/update-status")
def update_status(request: Request, user: dict[str, Any] = Depends(current_user)):
    return status_payload(request)


@router.post("/update/check")
def update_check(request: Request, user: dict[str, Any] = Depends(current_user)):
    payload = status_payload(request, force=True)
    _audit(
        request,
        user["username"],
        "system.update.check",
        payload.get("latest_version") or "-",
        f"available={payload.get('available')} signature={payload.get('signature')}",
    )
    return payload


@router.post("/update/apply")
def update_apply(
    body: ApplyBody,
    request: Request,
    user: dict[str, Any] = Depends(admin_user),
):
    payload = status_payload(request, force=True)
    latest = str(payload.get("latest_version") or "")
    if body.confirm_version != latest:
        raise HTTPException(
            status_code=400,
            detail=f"confirm_version 必须与 latest_version 逐字一致（当前为 {latest}）",
        )
    if not payload.get("available"):
        raise HTTPException(status_code=409, detail=payload.get("reason") or "当前没有可用更新")
    if not payload.get("can_apply"):
        raise HTTPException(status_code=409, detail=payload.get("reason") or "本部署不可执行在线更新")
    executor = request.app.state.update_executor
    manifest = feed_of(request).last_manifest()
    if manifest is None:
        raise HTTPException(status_code=409, detail="发布物 manifest 已失效，请重新检测")
    try:
        job = executor.apply(manifest, str(request.app.state.settings.update_manifest_url))
    except ops_lock.OpsBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except UpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    _audit(request, user["username"], "system.update.apply", latest, f"job={job.get('state')}")
    return status_payload(request)


@router.post("/update/rollback")
def update_rollback(request: Request, user: dict[str, Any] = Depends(admin_user)):
    executor = request.app.state.update_executor
    try:
        job = executor.rollback()
    except ops_lock.OpsBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except UpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    _audit(request, user["username"], "system.update.rollback", "previous", f"job={job.get('state')}")
    return status_payload(request)
