"""服务商管理台路由。

挂在 /api/providers 下，沿用统一错误壳与 u2a_ 脱敏口径：
- GET   /{provider_id}/console            管理台只读快照（所有登录角色，非 admin 只看自己名下数据）
- POST  /{provider_id}/console/actions    管理动作（admin），含通用 probe 与各家专属动作
- GET   /{provider_id}/console/media      gptgrok 媒体子工程文件/可编辑任务清单（admin）
- GET   /codebuddy/tasks                  任务运行台账（admin）
- POST  /codebuddy/tasks                  触发一次任务运行（admin）

页面不直接点上游：打开面板只读本地快照，外呼动作都是显式按钮。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..adapters.base import ProviderError
from ..api_utils import mask_secret, mask_text
from ..security import admin_user, audit_event, current_user
from ..services import provider_console as console
from ..services import provider_tasks
from ..services.gptgrok_media import MediaClient, media_base, media_key
from ..services.proxy_pool import ProxyPool
from ..services.task_queue import MediaStore, state_path
from ..services.video_tasks import list_tasks as list_video_tasks

router = APIRouter()

_PROBE_TIMEOUT = 45.0


class ActionIn(BaseModel):
    action: str
    task: str = ""
    task_id: str = ""
    force: bool = False


class TaskRunIn(BaseModel):
    task: str
    force: bool = False


def _store(request: Request) -> MediaStore:
    store = getattr(request.app.state, "media_store", None)
    if store is None:
        store = MediaStore(state_path(request.app.state.settings))
        request.app.state.media_store = store
    return store


def _adapter(request: Request, provider_id: str) -> Any:
    try:
        return request.app.state.registry.get(provider_id)
    except ProviderError as exc:
        raise HTTPException(exc.status_code, console.clean(exc.message)) from exc


def _db(request: Request):
    return request.app.state.db.connect()


def _public(request: Request, provider_id: str) -> dict[str, Any]:
    for item in request.app.state.registry.list_public():
        if item.get("id") == provider_id:
            return item
    raise HTTPException(404, f"未知的 provider：{console.clean(provider_id)}")


def _actor(user: dict) -> str:
    return str(user.get("username") or "")


def _health(request: Request, provider_id: str) -> dict[str, Any]:
    """最近一次巡检的只读投影；错误文案同样走脱敏，避免把上游地址带出去。"""
    service = request.app.state.health_service
    snapshot = service.snapshot(provider_id, limit=1)
    item = next(
        (entry for entry in snapshot.get("providers") or [] if entry.get("provider") == provider_id),
        {},
    )
    return {
        "status": item.get("status"),
        "checked_at": item.get("checked_at"),
        "latency_ms": item.get("latency_ms"),
        "error": console.clean(item.get("error") or "", 200),
        "enabled": bool(snapshot.get("enabled")),
        "interval_sec": snapshot.get("interval_sec"),
    }


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _media_items(payload: Any, limit: int = 50) -> list[dict[str, Any]]:
    """只回显文件元数据白名单，下载地址与内部 host 一律丢弃。"""
    if isinstance(payload, dict):
        for key in ("data", "items", "files", "object"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                payload = candidate
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        return []
    allowed = ("id", "object", "filename", "bytes", "created_at", "status", "purpose", "type", "updated_at")
    items = []
    for row in payload[: max(1, min(limit, 100))]:
        if not isinstance(row, dict):
            continue
        item = {key: console.sanitize(row[key]) for key in allowed if key in row}
        items.append(item)
    return items


def _log_error(exc: Exception) -> str:
    return console.clean(mask_text(str(exc)), 300)


def _task_stats(task: str, payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    stats: dict[str, Any] = {}
    for key in ("total", "succeeded", "already", "skipped", "updated", "refreshed"):
        value = data.get(key)
        if isinstance(value, (int, float, bool)):
            stats[key] = value
    failed = data.get("failed")
    if isinstance(failed, list):
        stats["failed"] = len(failed)
    elif isinstance(failed, (int, float)):
        stats["failed"] = int(failed)
    return stats


@router.get("/{provider_id}/console")
async def get_console(
    provider_id: str,
    request: Request,
    window: str = Query("7d"),
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    if provider_id not in console.CONSOLE_PROVIDERS:
        raise HTTPException(404, f"未知的 provider：{console.clean(provider_id)}")
    public = _public(request, provider_id)
    adapter = _adapter(request, provider_id)
    owner = console.owner_scope(user)
    is_admin = user.get("role") == "admin"

    conn = _db(request)
    try:
        usage = console.usage(conn, provider_id, window, owner)
        logs = console.recent_logs(conn, provider_id, owner)
        accounts = console.account_stats(conn, provider_id, owner)
        store = _store(request) if provider_id == "gptgrok" else None
        queue = await store.queue_stats() if store else None
        tasks = await store.admin_tasks(20) if store and is_admin else None
        video = []
        if is_admin:
            video = [
                {
                    "id": str(row.get("upstream_task_id") or ""),
                    "status": str(row.get("status") or ""),
                    "model": str(row.get("model") or ""),
                    "seconds": int(row.get("seconds") or 0),
                    "billed_micro": int(row.get("billed_micro") or 0),
                    "created_at": int(row.get("created_at") or 0),
                }
                for row in list_video_tasks(conn, provider=provider_id, limit=20)
            ]
        health = _health(request, provider_id)
    finally:
        conn.close()

    features: dict[str, Any] = {}
    if queue is not None:
        features["queue"] = queue
    if tasks is not None:
        features["media_tasks"] = [
            {
                "id": str(row.get("id") or ""),
                "kind": str(row.get("kind") or ""),
                "status": str(row.get("status") or ""),
                "owner": str(row.get("owner") or ""),
                "created_at": int(row.get("created_at") or 0),
                "updated_at": int(row.get("updated_at") or 0),
            }
            for row in tasks
        ]
    if video:
        features["video_tasks"] = video
    if provider_id == "muse":
        session = console.muse_sessions(provider_id, adapter, request.app.state.settings)
        if session is not None:
            features["sessions"] = session
    if provider_id == "zcode":
        claim = getattr(adapter, "_claim", None)
        if claim is not None:
            features["claim"] = claim.describe(adapter=adapter)
    if provider_id == "codar":
        proxy = ProxyPool.from_options(dict(getattr(adapter, "options", {}) or {}))
        features["proxy_pool"] = {"enabled": proxy.enabled, "mode": proxy.mode, "size": proxy.size}
        features["failure_actions"] = {
            "schedule_policy": str((adapter.options or {}).get("schedule_policy") or ""),
            "refresh": bool((adapter.options or {}).get("refresh", True)),
            "allow_external_refresh": bool((adapter.options or {}).get("allow_external_refresh", False)),
        }
    if provider_id == "codebuddy" and is_admin:
        features["task_runs"] = provider_tasks.list_runs(request.app.state.settings.data_dir, provider_id, 8)
    if provider_id in ("qoder", "qoder_sidecar") and hasattr(adapter, "_sessions"):
        cache = getattr(adapter, "_sessions", {}) or {}
        features["session_cache"] = {
            "entries": len(cache),
            "keys": [mask_secret(str(key)) for key in list(cache.keys())[:20]],
        }

    return {
        "provider": {
            "id": str(public.get("id") or provider_id),
            "label": str(public.get("label") or provider_id),
            "vendor": str(public.get("vendor") or ""),
            "runtime": str(public.get("runtime") or ""),
            "status": str(public.get("status") or ""),
            "risk_level": str(public.get("risk_level") or ""),
            "capabilities": dict(public.get("capabilities") or {}),
            "requires": list(public.get("requires") or []),
            "ready": public.get("readiness") == "ready",
            "readiness": str(public.get("readiness") or ""),
            "enabled": bool(public.get("enabled")),
        },
        "health": health,
        "rotation": await console.rotation_snapshot(adapter),
        "accounts": accounts,
        "credentials": console.credentials(request.app.state.registry, provider_id),
        "config": console.provider_config(provider_id, dict(getattr(adapter, "options", {}) or {})),
        "usage": usage,
        "logs": logs,
        "features": features,
        "readonly": not is_admin,
        "actions": list(console.ACTIONS.get(provider_id, ())) if is_admin else [],
    }


@router.post("/{provider_id}/console/actions")
async def run_action(
    provider_id: str,
    body: ActionIn,
    request: Request,
    user: dict = Depends(admin_user),
) -> dict[str, Any]:
    if provider_id not in console.CONSOLE_PROVIDERS:
        raise HTTPException(404, f"未知的 provider：{console.clean(provider_id)}")
    action = body.action.strip().lower()
    allowed = console.ACTIONS.get(provider_id, ())
    if action not in allowed:
        raise HTTPException(400, f"该服务商不支持动作：{console.clean(action)}")

    adapter = _adapter(request, provider_id)
    actor = _actor(user)
    started = int(time.time())
    result: dict[str, Any] = {}
    message = ""

    try:
        if action == "probe":
            detail = await asyncio.wait_for(adapter.health(), timeout=_PROBE_TIMEOUT)
            result = {"health": console.sanitize(detail)}
        elif action == "run_task":
            if provider_id != "codebuddy":
                raise HTTPException(400, "任务运行仅支持 codebuddy")
            task = body.task.strip().lower()
            if task not in provider_tasks.task_types(provider_id):
                raise HTTPException(400, f"未知任务：{console.clean(task)}")
            try:
                if task == "checkin_all":
                    payload = await adapter.checkin_all()
                else:
                    payload = await adapter.refresh_credits(force=body.force)
                run = provider_tasks.record(
                    request.app.state.settings.data_dir,
                    provider_id,
                    task,
                    status="succeeded",
                    actor=actor,
                    stats=_task_stats(task, payload),
                    started_at=started,
                )
                result = {"run": run}
            except ProviderError as exc:
                run = provider_tasks.record(
                    request.app.state.settings.data_dir,
                    provider_id,
                    task,
                    status="failed",
                    actor=actor,
                    message=exc.message,
                    started_at=started,
                )
                result = {"run": run}
                message = _log_error(exc)
                _audit(request, user, f"console.{provider_id}.{action}", task, f"failed: {message}")
                return {"ok": False, "action": action, "result": result, "message": message}
        elif action == "keepalive":
            from ..services import muse_session

            pool = muse_session.SessionPool.from_options(
                dict(getattr(adapter, "options", {}) or {}), request.app.state.settings
            )
            stats = await muse_session.keepalive(pool, adapter)
            result = {"sessions": stats}
        elif action == "claim":
            payload = await adapter.weekend_claim()
            result = {"claim": console.sanitize(payload)}
        elif action == "cancel_task":
            store = _store(request)
            cancelled = await store.cancel_any(body.task_id.strip())
            if cancelled is None:
                return {"ok": False, "action": action, "result": {}, "message": "任务不存在或已结束"}
            result = {"cancelled": True, "id": body.task_id}
        else:
            raise HTTPException(400, f"该服务商不支持动作：{console.clean(action)}")
    except HTTPException:
        raise
    except ProviderError as exc:
        message = _log_error(exc)
        _audit(request, user, f"console.{provider_id}.{action}", body.task or body.task_id, f"failed: {message}")
        return {"ok": False, "action": action, "result": result, "message": message}
    except asyncio.TimeoutError:
        message = "上游探测超时"
        _audit(request, user, f"console.{provider_id}.{action}", body.task or body.task_id, f"failed: {message}")
        return {"ok": False, "action": action, "result": result, "message": message}
    except Exception as exc:  # noqa: BLE001 - 统一收口成脱敏后的对外文案
        message = _log_error(exc)
        _audit(request, user, f"console.{provider_id}.{action}", body.task or body.task_id, f"failed: {message}")
        return {"ok": False, "action": action, "result": result, "message": message}

    _audit(request, user, f"console.{provider_id}.{action}", body.task or body.task_id, "ok")
    return {"ok": True, "action": action, "result": result, "message": ""}


@router.get("/{provider_id}/console/media")
async def media_inventory(
    provider_id: str,
    request: Request,
    kind: str = Query("files"),
    user: dict = Depends(admin_user),
) -> dict[str, Any]:
    if provider_id != "gptgrok":
        raise HTTPException(400, "媒体清单仅支持 gptgrok")
    adapter = _adapter(request, provider_id)
    options = dict(getattr(adapter, "options", {}) or {})
    base, key = media_base(options), media_key(options)
    if not base:
        return {"configured": False, "available": False, "items": [], "message": "未配置媒体子工程地址"}
    client = MediaClient(base, key)
    try:
        payload = await client.files_list() if kind == "files" else await client.editable_list()
    except ProviderError as exc:
        return {"configured": True, "available": False, "items": [], "message": _log_error(exc)}
    return {"configured": True, "available": True, "items": _media_items(payload), "message": ""}


@router.get("/codebuddy/tasks")
async def codebuddy_tasks(
    request: Request,
    limit: int = Query(20),
    user: dict = Depends(admin_user),
) -> dict[str, Any]:
    return {
        "items": provider_tasks.list_runs(request.app.state.settings.data_dir, "codebuddy", limit),
        "task_types": list(provider_tasks.task_types("codebuddy")),
    }


@router.post("/codebuddy/tasks")
async def codebuddy_task_run(
    body: TaskRunIn,
    request: Request,
    user: dict = Depends(admin_user),
) -> dict[str, Any]:
    adapter = _adapter(request, "codebuddy")
    task = body.task.strip().lower()
    if task not in provider_tasks.task_types("codebuddy"):
        raise HTTPException(400, f"未知任务：{console.clean(task)}")
    started = int(time.time())
    try:
        if task == "checkin_all":
            payload = await adapter.checkin_all()
        else:
            payload = await adapter.refresh_credits(force=body.force)
    except ProviderError as exc:
        run = provider_tasks.record(
            request.app.state.settings.data_dir,
            "codebuddy",
            task,
            status="failed",
            actor=_actor(user),
            message=exc.message,
            started_at=started,
        )
        _audit(request, user, "codebuddy.taskrun", task, f"failed: {_log_error(exc)}")
        return {"ok": False, "run": run, "message": _log_error(exc)}
    run = provider_tasks.record(
        request.app.state.settings.data_dir,
        "codebuddy",
        task,
        status="succeeded",
        actor=_actor(user),
        stats=_task_stats(task, payload),
        started_at=started,
    )
    _audit(request, user, "codebuddy.taskrun", task, "ok")
    return {"ok": True, "run": run, "message": ""}


def _audit(request: Request, user: dict, action: str, target: str, detail: str) -> None:
    conn = _db(request)
    try:
        audit_event(
            conn,
            _actor(user),
            action,
            console.clean(target, 80),
            console.clean(detail, 160),
            _ip(request),
        )
    finally:
        conn.close()


