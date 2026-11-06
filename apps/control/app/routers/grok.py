"""Grok 注册任务 API：入队、查看、取消、重试与队列状态。"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..security import admin_user
from ..services.grok_register import JOB_TYPES

router = APIRouter(prefix="/api/grok/register", tags=["grok-register"])


class TaskCreate(BaseModel):
    count: int = Field(default=1, ge=1, le=1000)
    workers: int = Field(default=1, ge=1, le=32)
    email_provider: str = ""
    proxy: str = ""
    max_attempts: int = Field(default=3, ge=1, le=5)
    payload: dict[str, Any] = Field(default_factory=dict)
    job_type: str = "register"


def _service(request: Request):
    return request.app.state.grok_service


def _session_present(payload: dict[str, Any]) -> bool:
    """relogin 必须带现成会话：只认非空字符串或非空对象/数组。"""
    for key in ("sso", "cookies"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (dict, list)) and value:
            return True
    return False


@router.post("/tasks")
def create_task(body: TaskCreate, request: Request, user: dict = Depends(admin_user)):
    job_type = body.job_type.strip().lower()
    if job_type not in JOB_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"未知的任务类型：{body.job_type or '(空)'}（可选 register / relogin）",
        )
    if job_type == "relogin" and not _session_present(body.payload or {}):
        raise HTTPException(
            status_code=400,
            detail="relogin 任务必须带 payload.sso 或 payload.cookies（现有登录会话），否则执行器无号可登",
        )
    return _service(request).enqueue(
        count=body.count,
        workers=body.workers,
        email_provider=body.email_provider,
        proxy=body.proxy,
        max_attempts=body.max_attempts,
        payload=body.payload,
        job_type=job_type,
    )


# 任务行含 proxy / payload_json / result_json，读侧与写侧同一口径：仅 admin
@router.get("/tasks")
def list_tasks(request: Request, limit: int = 50, user: dict = Depends(admin_user)):
    return _service(request).list_tasks(limit)


@router.get("/tasks/{task_id}")
def get_task(task_id: int, request: Request, user: dict = Depends(admin_user)):
    try:
        return _service(request).get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/detail")
def task_detail(task_id: int, request: Request, user: dict = Depends(admin_user)):
    """任务详情：状态机步骤 + 时间线（步骤取自 grok_task_events，不是前端推测）。"""
    try:
        return _service(request).detail(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/credentials")
def export_task_credentials(
    task_id: int, request: Request, fmt: str = "json", user: dict = Depends(admin_user)
):
    """导出该任务落库的账号与登录凭据（json / csv）。

    文件里 cookie/token 是明文，只为「拿到产物直接登录」这一条用途；与任务读侧
    同口径，仅 admin 可用，且禁止中间层缓存。
    """
    try:
        body, media_type, filename = _service(request).export_credentials(task_id, fmt)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, request: Request, user: dict = Depends(admin_user)):
    try:
        return _service(request).cancel(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, request: Request, user: dict = Depends(admin_user)):
    try:
        return _service(request).retry(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# 队列状态只给 GrokRegisterTasks.tsx 用，而该组件挂在 ProvidersSection 的 isAdmin 分支下，
# 消费方与端点同一口径：仅 admin
@router.get("/worker")
def worker(request: Request, user: dict = Depends(admin_user)):
    return _service(request).stats()
