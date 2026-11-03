import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..adapters.base import ProviderAdapter, ProviderError
from ..api_utils import now, public_account, require_file
from ..security import admin_user, current_user

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# 同步节流：页面轮询很勤，上游 /api/accounts 会合并 /status，没必要每次都打
_SYNC_INTERVAL_SEC = 30
_last_sync: dict[str, float] = {}


def _implements(adapter: ProviderAdapter, method: str) -> bool:
    """适配器是否**真的**覆盖了基类的账号方法。

    能力位（spec.capabilities）是给转发/模型用的声明，不能直接当作账号管理能力：
    trae_sidecar 声明了 oauth/credits，但账号在 sidecar 内部，主仓这两个方法
    一个都没实现。前端若按声明画按钮，点下去就是 501，所以这里以实际覆写为准。
    """
    impl = getattr(type(adapter), method, None)
    base = getattr(ProviderAdapter, method, None)
    return callable(impl) and (base is None or impl is not base)


def _account_ops(adapter: ProviderAdapter) -> dict[str, bool]:
    """该 provider 在统一账号页上真实可用的操作集合。"""
    caps = adapter.spec.capabilities
    has_action = _implements(adapter, "account_action")
    return {
        "list": _implements(adapter, "sync_accounts"),
        "oauth": _implements(adapter, "start_login") and _implements(adapter, "poll_login"),
        "checkin": bool(caps.get("checkin")) and has_action,
        "credits": bool(caps.get("credits")) and _implements(adapter, "refresh_credits"),
        "test": has_action,
        "refresh": has_action,
        "clear_cooling": has_action,
        "disabled": has_action,
        "delete": _implements(adapter, "delete_account"),
        # 备注只落本端库，不依赖上游，任何 provider 都能写
        "note": True,
        "restart": _implements(adapter, "restart"),
    }


def _managed_by(adapter: ProviderAdapter, ops: dict[str, bool]) -> str:
    """账号归属：unified = 本页管理；sidecar = 自身控制台；quota = 配额域；external = 未接入。"""
    if adapter.spec.id == "sub2api":
        return "quota"
    if adapter.spec.status == "sidecar":
        return "sidecar"
    return "unified" if ops["list"] else "external"


def _pick_adapter(request: Request, provider: str | None, op: str, label: str) -> ProviderAdapter:
    """按 provider 取适配器；未指定时取第一家真正支持该操作且已启用的。"""
    registry = request.app.state.registry
    if provider:
        adapter = registry.get(provider)  # 未知 pid 抛 404
        if not _account_ops(adapter)[op]:
            raise HTTPException(status_code=400, detail=f"{adapter.spec.name} 不支持{label}")
        return adapter
    for candidate in registry.enabled():
        if _account_ops(candidate)[op]:
            return candidate
    raise HTTPException(status_code=503, detail=f"没有已启用的服务商支持{label}")


async def _sync_provider(request: Request, pid: str, *, force: bool = False) -> dict[str, Any]:
    """拉取 provider 的账号列表并 upsert 进 accounts 表。失败不抛错，如实返回 note。"""
    adapter = request.app.state.registry.get(pid)
    # 没实现账号同步的 provider 不参与同步：否则每次列表都会把「未实现」当成
    # 上游不可达（pool_available=False），混合多家时整页被误报成故障。
    if not _account_ops(adapter)["list"]:
        return {"synced": 0, "pool_available": True, "note": ""}
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
        # 只同步真正实现账号列表的 provider，避免「未实现」被误报成上游不可达
        pids = [a.spec.id for a in registry.enabled() if _account_ops(a)["list"]]
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


@router.get("/capabilities")
def account_capabilities(request: Request, user: dict = Depends(current_user)):
    """每家 provider 的账号管理能力（以适配器实际覆写为准，不看声明位）。

    前端据此决定画哪些按钮、哪些家只给出「账号在哪里管理」的说明，
    避免出现点下去必然 501 的假按钮。
    """
    registry = request.app.state.registry
    rows = {item["id"]: item for item in registry.list_public()}
    out: list[dict[str, Any]] = []
    for adapter in registry._adapters.values():
        ops = _account_ops(adapter)
        info = rows.get(adapter.spec.id, {})
        out.append(
            {
                "id": adapter.spec.id,
                "label": info.get("label") or adapter.spec.name,
                "enabled": bool(info.get("enabled")),
                "status": adapter.spec.status,
                "readiness": adapter.spec.readiness,
                "requires": list(adapter.spec.requires),
                "managed": _managed_by(adapter, ops),
                "ops": ops,
            }
        )
    return {"providers": out}


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
async def refresh_credits(
    request: Request,
    force: bool = False,
    provider: str | None = None,
    user: dict = Depends(admin_user),
):
    """刷新积分：provider 不传时取第一家支持积分的已启用服务商（当前即 CodeBuddy）。"""
    adapter = _pick_adapter(request, provider, "credits", "刷新积分")
    try:
        return await adapter.refresh_credits(force)
    except ProviderError as exc:
        return {"total": 0, "succeeded": 0, "credits": {}, "meta": {}, "failed": [], "message": exc.message}


@router.post("/checkin-all")
async def checkin_all(
    request: Request,
    provider: str | None = None,
    user: dict = Depends(admin_user),
):
    adapter = _pick_adapter(request, provider, "checkin", "批量签到")
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
    if _account_ops(adapter)["list"]:
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
async def restart(request: Request, provider: str | None = None, user: dict = Depends(admin_user)):
    registry = request.app.state.registry
    if provider:
        adapter = registry.get(provider)
        if not _account_ops(adapter)["restart"]:
            return {"ok": False, "message": f"{adapter.spec.name} 的运行时不归本控制面托管，请在其自身部署处重启"}
    else:
        candidates = [a for a in registry.enabled() if _account_ops(a)["restart"]]
        if not candidates:
            return {"ok": False, "message": "没有可重启的受管运行时：CodeBuddy 未启用；其余 provider 为独立部署，请在其自身处重启"}
        adapter = candidates[0]
    try:
        return await adapter.restart()
    except ProviderError as exc:
        return {"ok": False, "message": exc.message}
