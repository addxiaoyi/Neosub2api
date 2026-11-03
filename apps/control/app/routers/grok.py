"""Grok 注册任务 API：入队、查看、取消、重试与队列状态。"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..security import admin_user, current_user

router = APIRouter(prefix="/api/grok/register", tags=["grok-register"])


class TaskCreate(BaseModel):
    count: int = Field(default=1, ge=1, le=1000)
    workers: int = Field(default=1, ge=1, le=32)
    email_provider: str = ""
    proxy: str = ""
    max_attempts: int = Field(default=3, ge=1, le=5)
    payload: dict[str, Any] = Field(default_factory=dict)


def _service(request: Request):
    return request.app.state.grok_service


@router.post("/tasks")
def create_task(body: TaskCreate, request: Request, user: dict = Depends(admin_user)):
    return _service(request).enqueue(
        count=body.count,
        workers=body.workers,
        email_provider=body.email_provider,
        proxy=body.proxy,
        max_attempts=body.max_attempts,
        payload=body.payload,
    )


@router.get("/tasks")
def list_tasks(request: Request, limit: int = 50, user: dict = Depends(current_user)):
    return _service(request).list_tasks(limit)


@router.get("/tasks/{task_id}")
def get_task(task_id: int, request: Request, user: dict = Depends(current_user)):
    try:
        return _service(request).get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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


@router.get("/worker")
def worker(request: Request, user: dict = Depends(current_user)):
    return _service(request).stats()
