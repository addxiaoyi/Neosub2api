import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..adapters.base import ProviderError
from ..api_utils import now, public_account, require_file
from ..security import admin_user, current_user

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# 同步节流：页面轮询很勤，上游 /api/accounts 会合并 /status，没必要每次都打
_SYNC_INTERVAL_SEC = 30
_last_sync: dict[str, float] = {}


async def _sync_provider(request: Request, pid: str, *, force: bool = False) -> dict[str, Any]:
    """拉取 provider 的账号列表并 upsert 进 accounts 表。失败不抛错，如实返回 note。"""
    adapter = request.app.state.registry.get(pid)
    if not force and time.monotonic() - _last_sync.get(pid, 0) < _SYNC_INTERVAL_SEC:
        return {"synced": -1, "pool_available": True, "note": ""}
    page = await adapter.sync_accounts()
    if page.note and not page.accounts:
        return {"synced": 0, "pool_available": page.pool_available, "note": page.note}
    conn = request.app.state.db.connect()
    try:
        seen = []
        for item in page.accounts:
            seen.append(item["file"])
            conn.execute(
                "INSERT INTO accounts(file, provider, uid, nickname, enterprise_id, realm, "
                "expires_at, credits, note, disabled, meta_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(file) DO UPDATE SET uid=excluded.uid, nickname=excluded.nickname, "
                "enterprise_id=excluded.enterprise_id, realm=excluded.realm, "
                "expires_at=excluded.expires_at, credits=excluded.credits, "
                "disabled=excluded.disabled, meta_json=excluded.meta_json",
                (
                    item["file"], pid, item["uid"], item["nickname"], item["enterprise_id"],
                    item["realm"], item["expires_at"], item["credits"], item["note"],
                    item["disabled"], item["meta_json"], now(),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    _last_sync[pid] = time.monotonic()
    return {"synced": len(seen), "pool_available": page.pool_available, "note": page.note}


async def _sync_targets(request: Request, provider: str | None) -> dict[str, Any]:
    """确定本次列表要同步哪些 provider：指定了就只同步它，否则同步全部已启用的。"""
    registry = request.app.state.registry
    if provider:
        pids = [provider]
    else:
        pids = [a.spec.id for a in registry.enabled()]
    result = {"synced": 0, "pool_available": True, "notes": []}
    for pid in pids:
        try:
            r = await _sync_provider(request, pid)
        except ProviderError as exc:
            r = {"synced": 0, "pool_available": False, "note": exc.message}
        if r["synced"] > 0:
            result["synced"] += r["synced"]
        if not r["pool_available"]:
            result["pool_available"] = False
        if r.get("note"):
            result["notes"].append(f"{pid}: {r['note']}")
    return result


@router.get("")
async def list_accounts(
    request: Request,
    provider: str | None = None,
    realm: str | None = None,
    user: dict = Depends(current_user),
):
    sync = await _sync_targets(request, provider)
    conn = request.app.state.db.connect()
    try:
        sql = "SELECT * FROM accounts"
        where: list[str] = []
        params: list[str] = []
        if provider:
            where.append("provider = ?")
            params.append(provider)
        if realm:
            where.append("realm = ?")
            params.append(realm)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    items = [public_account(row) for row in rows]
    out: dict[str, Any] = {
        "total": len(items),
        "accounts": items,
        "pool_synced": max(sync["synced"], 0),
        "pool_available": sync["pool_available"],
    }
    if sync["notes"]:
        out["sync_note"] = "；".join(sync["notes"])
    return out


@router.post("/sync")
async def sync_now(request: Request, provider: str | None = None, user: dict = Depends(admin_user)):
    """手动强制同步（绕过节流），供「立即刷新」按钮与排障使用。"""
    registry = request.app.state.registry
    pids = [provider] if provider else [a.spec.id for a in registry.enabled()]
    results = {}
    for pid in pids:
        results[pid] = await _sync_provider(request, pid, force=True)
    return results


@router.post("/refresh-credits")
async def refresh_credits(request: Request, force: bool = False, user: dict = Depends(admin_user)):
    adapter = request.app.state.registry.get("codebuddy")
    try:
        return await adapter.refresh_credits(force)
    except ProviderError as exc:
        return {"total": 0, "succeeded": 0, "credits": {}, "meta": {}, "failed": [], "message": exc.message}


@router.post("/checkin-all")
async def checkin_all(request: Request, user: dict = Depends(admin_user)):
    adapter = request.app.state.registry.get("codebuddy")
    try:
        return await adapter.checkin_all()
    except ProviderError as exc:
        return {"total": 0, "succeeded": 0, "already": 0, "skipped": 0, "results": [], "message": exc.message}


class NoteBody(BaseModel):
    note: str = ""


class DisabledBody(BaseModel):
    disabled: bool


@router.put("/{file}/note")
async def set_note(file: str, body: NoteBody, request: Request, user: dict = Depends(admin_user)):
    row = require_file(request, file)
    note = body.note.strip()[:200]
    adapter = request.app.state.registry.get(row["provider"])
    try:
        await adapter.set_note(file, note)  # type: ignore[attr-defined]
    except (ProviderError, AttributeError):
        pass  # 上游不支持备注就仅存控制面（codebuddy 管理端才是真备注的落点）
    conn = request.app.state.db.connect()
    try:
        conn.execute("UPDATE accounts SET note = ? WHERE file = ?", (note, file))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "uid": row["uid"], "note": note}


@router.post("/{file}/disabled")
async def set_disabled(file: str, body: DisabledBody, request: Request, user: dict = Depends(admin_user)):
    row = require_file(request, file)
    adapter = request.app.state.registry.get(row["provider"])
    upstream: dict[str, Any] = {}
    try:
        upstream = await adapter.account_action(file, "disabled", {"disabled": body.disabled})
    except ProviderError as exc:
        return {"ok": False, "file": file, "disabled": body.disabled, "message": exc.message}
    conn = request.app.state.db.connect()
    try:
        conn.execute("UPDATE accounts SET disabled = ? WHERE file = ?", (1 if body.disabled else 0, file))
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": bool(upstream.get("ok", True)),
        "file": file,
        "disabled": body.disabled,
        "reload_triggered": bool(upstream.get("reload_triggered", False)),
        "message": str(upstream.get("message") or ""),
    }


@router.delete("/{file}")
async def remove_account(file: str, request: Request, user: dict = Depends(admin_user)):
    row = require_file(request, file)
    adapter = request.app.state.registry.get(row["provider"])
    await adapter.delete_account(file)  # 上游删不掉就抛错，不动本地记录
    conn = request.app.state.db.connect()
    try:
        conn.execute("DELETE FROM accounts WHERE file = ?", (file,))
        conn.commit()
    finally:
        conn.close()
    return {"success": True}


async def _account_action(request: Request, file: str, action: str) -> dict[str, Any]:
    row = require_file(request, file)
    adapter = request.app.state.registry.get(row["provider"])
    try:
        result = await adapter.account_action(file, action)
    except ProviderError as exc:
        return {"ok": False, "message": exc.message}
    # 签到/刷新/清冷却会改变上游状态，顺手把该 provider 重新同步一次
    await _sync_provider(request, row["provider"], force=True)
    return result


@router.post("/{file}/checkin")
async def checkin(file: str, request: Request, user: dict = Depends(admin_user)):
    return await _account_action(request, file, "checkin")


@router.post("/{file}/test")
async def test_account(file: str, request: Request, user: dict = Depends(admin_user)):
    return await _account_action(request, file, "test")


@router.post("/{file}/refresh")
async def refresh_account(file: str, request: Request, user: dict = Depends(admin_user)):
    return await _account_action(request, file, "refresh")


@router.post("/{file}/clear-cooling")
async def clear_cooling(file: str, request: Request, user: dict = Depends(admin_user)):
    return await _account_action(request, file, "clear-cooling")


@router.post("/restart")
async def restart(request: Request, user: dict = Depends(admin_user)):
    registry = request.app.state.registry
    enabled = {a.spec.id for a in registry.enabled()}
    if "codebuddy" not in enabled:
        return {"ok": False, "message": "没有可重启的受管运行时：CodeBuddy 未启用；其余 provider 为独立部署，请在其自身处重启"}
    try:
        return await registry.get("codebuddy").restart()
    except ProviderError as exc:
        return {"ok": False, "message": exc.message}
