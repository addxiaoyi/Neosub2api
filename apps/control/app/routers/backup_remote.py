"""远端备份的管理端点：连接参数、手动上传、远端列表、恢复与删除。

安全口径：S3 凭据只从环境变量读（不进库、不进响应、不进日志）；加密密钥不在
任何响应里出现，只回 `encryption_key_configured`。上传失败不会动本地归档，
恢复失败只清理本次下载产物，活库不动。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..api_utils import db_of, mask_text
from ..security import admin_user, audit_event
from ..services.backup_remote import CREDENTIAL_HINT, BackupRemoteService, ConfigError, RemoteFetchError

router = APIRouter(prefix="/api/system", tags=["system"])

# 远端读取失败 → HTTP 语义：凭据/未配置 409、对象缺失 404、校验失败 400、
# 上游致命或可重试 502、本地落盘失败 500。与上传端点的分类保持一致。
_REMOTE_STATUS = {
    "not_configured": 409,
    "key_missing": 409,
    "key_invalid": 409,
    "object_missing": 404,
    "sidecar_missing": 400,
    "checksum_mismatch": 400,
    "decrypt_failed": 400,
    "auth_rejected": 502,
    "retryable": 502,
    "file_error": 500,
}


class UploadIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)


class RestoreRemoteIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    confirm: str = ""


class RemoteConfigIn(BaseModel):
    endpoint: str | None = None
    region: str | None = None
    bucket: str | None = None
    access_key: str | None = None
    secret_key: str | None = None
    session_token: str | None = None
    path_style: bool | None = None
    prefix: str | None = None


def _service(request: Request) -> BackupRemoteService:
    return request.app.state.backup_remote


def _audit(request: Request, username: str, action: str, target: str, detail: str) -> None:
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


def _settings_flags(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "enabled": bool(getattr(settings, "backup_remote_enabled", False)),
        "interval_sec": int(getattr(settings, "backup_remote_interval_sec", 86400)),
        "auto_upload": bool(getattr(settings, "backup_remote_auto_upload", False)),
        "keep": int(getattr(settings, "backup_remote_keep", 14)),
    }


@router.get("/backup-remote")
def read_remote_config(request: Request, user=Depends(admin_user)):
    service = _service(request)
    return {"ok": True, **service.public_config(), "settings": _settings_flags(request)}


@router.post("/backup-remote")
def save_remote_config(request: Request, payload: RemoteConfigIn, user=Depends(admin_user)):
    service = _service(request)
    patch = payload.model_dump(exclude_unset=True)
    if {"access_key", "secret_key", "session_token"} & patch.keys():
        raise HTTPException(status_code=400, detail=CREDENTIAL_HINT)
    try:
        public = service.save_config(patch)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    _audit(
        request,
        user["username"],
        "backup.remote.config",
        public.get("bucket") or "backup-remote",
        f"fields={','.join(sorted(patch))} configured={public.get('configured')}",
    )
    return {"ok": True, **public, "settings": _settings_flags(request)}


@router.get("/backups/remote")
def list_remote_backups(request: Request, user=Depends(admin_user)):
    service = _service(request)
    public = service.public_config()
    listing = service.list_remote()
    payload = {
        "ok": listing.get("ok", False),
        "configured": listing.get("configured", False),
        "access_key_id_masked": public["access_key_id_masked"],
        "credential_source": public["credential_source"],
        "encryption_key_configured": public["encryption_key_configured"],
        "crypto_backend": public["crypto_backend"],
        "s3_reachable": listing.get("s3_reachable"),
        "state": service.state(),
        "objects": listing.get("objects", []),
        "count": listing.get("count", 0),
        "settings": _settings_flags(request),
    }
    if not listing.get("ok"):
        raise HTTPException(
            status_code=502,
            detail={**payload, "error": listing.get("error")},
        )
    return payload


@router.post("/backups/upload")
def upload_backup(request: Request, payload: UploadIn | None = None, user=Depends(admin_user)):
    service = _service(request)
    name = payload.name if payload else None
    # 手动动作不陪着退避 30s/2m/10m：几次连续尝试后立即返回，失败留给调度器按 next_retry_at 重试
    result = service.upload(name=name, retry_delays=())
    _audit(
        request,
        user["username"],
        "backup.remote.upload",
        str(result.get("object") or name or "-"),
        f"ok={result.get('ok')} reason={result.get('reason')} attempts={result.get('attempts', 0)}",
    )
    if result.get("ok"):
        return {"ok": True, **result}

    reason = result.get("reason")
    if reason in {"key_missing", "key_invalid", "not_configured", "busy"}:
        raise HTTPException(status_code=409, detail=result.get("message") or reason)
    if reason == "no_archive":
        raise HTTPException(status_code=404, detail=result.get("message") or reason)
    if reason == "local_backup_failed":
        raise HTTPException(status_code=500, detail=result.get("message") or reason)
    raise HTTPException(status_code=502, detail=result.get("message") or reason or "upload_failed")


@router.post("/backups/restore-remote")
def restore_remote_backup(
    request: Request, payload: RestoreRemoteIn, user=Depends(admin_user)
):
    """从远端下载 → 先验旁注 → 解密落盘 → 复用本地恢复链。

    顺序固定：白名单 + 逐字 confirm → 共享目录锁 → 下载到 `.part` → 验旁注 →
    解密原子落名 → 本地恢复。任何一步失败都只清理本次产物，活库不动。
    """
    from . import backup as backup_router

    service = _service(request)
    settings = request.app.state.settings
    core = backup_router.core_module()
    name = payload.name
    if not core.ARCHIVE_RE.match(name):
        raise HTTPException(status_code=400, detail="备份文件名不合法")
    if payload.confirm != name:
        raise HTTPException(
            status_code=400,
            detail="确认串必须与备份名逐字一致（confirm 与 name 不同），已中止恢复",
        )

    directory = backup_router._backup_dir(settings)  # noqa: SLF001 - 与本地恢复同一目录口径
    directory.mkdir(parents=True, exist_ok=True)
    with backup_router._busy_guard(directory, f"restore-remote:{name}"):  # noqa: SLF001
        try:
            item = service.download_archive(name=name, directory=directory)
        except RemoteFetchError as exc:
            raise HTTPException(
                status_code=_REMOTE_STATUS.get(exc.reason, 502), detail=exc.message
            ) from None
        try:
            body = backup_router.restore_archive(
                request,
                user["username"],
                name=name,
                archive=item.path,
                action="backup.restore_remote",
                target=item.object_key,
                detail_extra=f"object={item.object_key} sha256={item.sha256} size={item.size_bytes}",
            )
        except Exception:
            service.discard_download(item)
            raise
        service.commit_download(item)

    return {
        **body,
        "source": "remote",
        "object": item.object_key,
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
    }


@router.delete("/backups/remote/{name}")
def delete_remote_backup(request: Request, name: str, user=Depends(admin_user)):
    """删远端一份归档及其旁注；只接受白名单名字派生的本 prefix 对象。"""
    from . import backup as backup_router

    service = _service(request)
    core = backup_router.core_module()
    if not core.ARCHIVE_RE.match(name):
        raise HTTPException(status_code=400, detail="备份文件名不合法")
    try:
        result = service.delete_remote(name)
    except RemoteFetchError as exc:
        raise HTTPException(
            status_code=_REMOTE_STATUS.get(exc.reason, 502), detail=exc.message
        ) from None
    _audit(
        request,
        user["username"],
        "backup.remote.delete",
        result["object"],
        f"ok=True sidecar_removed={not bool(result['sidecar_error'])}",
    )
    return {"ok": True, **result}
