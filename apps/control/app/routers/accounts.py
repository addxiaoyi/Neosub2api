import copy
import json
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..adapters.base import ProviderAdapter, ProviderError
from ..api_utils import mask_secret, mask_text, now, public_account, require_file
from ..security import admin_user, audit_event, current_user
from ..services import provider_probe

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# 同步节流：页面轮询很勤，上游 /api/accounts 会合并 /status，没必要每次都打
_SYNC_INTERVAL_SEC = 30
_last_sync: dict[str, float] = {}

# 以实际覆写判定能力，不看 spec.capabilities 声明；探测与定时检测共用同一实现
_implements = provider_probe.implements


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
    """账号归属：unified = 本页管理；sidecar = 自身控制台；quota = 配额域；
    demo = 演示种子数据；external = 未接入。"""
    if adapter.spec.id == "sub2api":
        return "quota"
    if adapter.spec.status == "sidecar":
        return "sidecar"
    # 演示账号是种子数据、直接写在本页 accounts 表里（demo.py 播种），列表能看到
    # 但不能扫码新增；落到 external 会显示「没有独立账号池」，与同屏的账号行矛盾。
    if adapter.spec.status == "demo":
        return "demo"
    return "unified" if ops["list"] else "external"


# 导入凭证类型（与前端下拉一一对应）：json 走语法校验，其余按纯文本落库
IMPORT_CREDENTIAL_TYPES = ("api_key", "bearer", "cookie", "json", "env")
# 上限与统一凭据库 create_credential 的 secret 上限保持同一个数，避免两边口径打架
_CREDENTIAL_MAX = 4096
# 本端登记行的标记：上游没有这份账号，删除时跳过上游、连带回收镜像凭据
_IMPORT_MARK = "imported"


class AccountImportIn(BaseModel):
    provider: str
    label: str = ""
    name: str = ""  # label 的别名，二选一
    credential_type: str = "api_key"
    credential: Any = None  # json 类型允许直接传结构化对象
    base_url: str = ""
    note: str = ""


def _meta_of(row: Any) -> dict[str, Any]:
    try:
        meta = json.loads(row["meta_json"] or "{}")
    except (json.JSONDecodeError, TypeError, IndexError, KeyError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _account_item(row: Any) -> dict[str, Any]:
    """GET 列表与导入响应共用的单条结构：public_account + 本端登记标记。"""
    item = public_account(row)
    item["local_only"] = bool(_meta_of(row).get(_IMPORT_MARK))
    return item


def _prepare_credential(ctype: str, raw: Any) -> str:
    """把入参凭证规整成待存储文本；不通过一律 400，错误定位到具体位置。"""
    if ctype == "json":
        if isinstance(raw, (dict, list)):
            text = json.dumps(raw, ensure_ascii=False)
        elif isinstance(raw, str):
            text = raw.strip()
        else:
            raise HTTPException(status_code=400, detail="json 凭证必须是字符串或对象/数组")
        if not text:
            raise HTTPException(status_code=400, detail="凭证不能为空")
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）",
            ) from None
    else:
        if not isinstance(raw, str):
            raise HTTPException(status_code=400, detail=f"{ctype} 凭证必须是字符串")
        text = raw.strip()
        if not text:
            raise HTTPException(status_code=400, detail="凭证不能为空")
    if len(text) > _CREDENTIAL_MAX:
        raise HTTPException(status_code=400, detail=f"凭证超过 {_CREDENTIAL_MAX} 字符")
    return text


def _provider_enabled(registry: Any, pid: str) -> bool:
    """服务商是否已启用（与前端分类判据同一份来源：providers 表的 enabled 列）。

    未启用的服务商不会被运行时选中，写进去的账号永远不参与转发；前端因此不给
    导入入口，后端也必须拒绝，否则绕过 UI 直接 POST 就能造出僵尸账号。
    """
    return bool((registry._rows().get(pid) or {}).get("enabled"))


def _prepare_import(request: Request, body: AccountImportIn) -> dict[str, Any]:
    """导入前置校验：provider 必须已注册、已启用且账号归本仓管，凭证类型/内容必须合法。"""
    registry = request.app.state.registry
    pid = (body.provider or "").strip()
    try:
        adapter = registry.get(pid)
    except ProviderError:
        raise HTTPException(status_code=400, detail=f"未知的服务商：{pid or '(空)'}") from None
    managed = _managed_by(adapter, _account_ops(adapter))
    # sidecar/demo 的账号生命周期不归本仓：写进来也永远不会被运行时消费，是假数据
    if managed == "sidecar":
        raise HTTPException(
            status_code=400,
            detail=f"{adapter.spec.name} 的账号由其 sidecar 自身管理，本页无法新增",
        )
    if managed == "demo":
        raise HTTPException(status_code=400, detail=f"{adapter.spec.name} 是演示账号（种子数据），不支持导入")
    ctype = (body.credential_type or "").strip()
    if ctype not in IMPORT_CREDENTIAL_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"credential_type 必须是 {'、'.join(IMPORT_CREDENTIAL_TYPES)} 之一",
        )
    text = _prepare_credential(ctype, body.credential)
    base_url = (body.base_url or "").strip()
    if base_url:
        try:
            base_url = registry._clean_base(pid, base_url)  # 与统一凭据库同一道地址守卫
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from None
    # 入参校验通过后再看服务商状态：未启用的 provider 写进去也不会被运行时选中，
    # 账号永远不参与转发（前端同判据不给导入入口，这里挡住绕过 UI 的直接 POST）。
    # 放在 sidecar/demo 分支之后：那两类合法的「不可导入」类型仍报各自原有的错。
    if not _provider_enabled(registry, pid):
        raise HTTPException(
            status_code=400,
            detail=f"该服务商（{adapter.spec.name}）未启用，请先在设置中启用",
        )
    return {
        "provider": pid,
        "adapter": adapter,
        "credential_type": ctype,
        "credential": text,
        "base_url": base_url,
        "label": (body.label or body.name or "").strip()[:64],
        "note": (body.note or "").strip()[:200],
    }


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
    # 上游 note 会原样进前端提示（「同步账号」按钮），凭据形态先脱敏
    note = mask_text(page.note or "")
    if page.note and not page.accounts:
        return {"synced": 0, "pool_available": page.pool_available, "note": note}
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
    return {"synced": len(seen), "pool_available": page.pool_available, "note": note}


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
        if user.get("role") != "admin":
            # 账号是资产清单：admin 看全量，非 admin 只看自己名下的（含无主 NULL 不可见）
            where.append("owner_user_id = ?")
            params.append(user.get("username") or "")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    items = [_account_item(row) for row in rows]
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
    """手动强制同步（绕过节流），供「同步账号」按钮与排障使用。"""
    registry = request.app.state.registry
    pids = [provider] if provider else [a.spec.id for a in registry.enabled()]
    results = {}
    for pid in pids:
        try:
            results[pid] = await _sync_provider(request, pid, force=True)
        except ProviderError as exc:
            # 单家上游挂了不能把整批 500 掉：失败家带 note 如实返回，其余家照常同步
            results[pid] = {"synced": 0, "pool_available": False, "note": mask_text(exc.message)}
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


@router.post("/import")
async def import_account(body: AccountImportIn, request: Request, user: dict = Depends(admin_user)):
    """导入一条账号（贴凭证式登记）。

    凭据镜像进统一凭据库（key 链的事实来源，参与转发轮换），账号行进本端 accounts
    表负责展示与归属；owner_user_id 写当前用户。响应与 GET 单条同构，凭据不回显明文。
    """
    fields = _prepare_import(request, body)
    registry = request.app.state.registry
    try:
        cred = registry.create_credential(
            fields["provider"],
            label=fields["label"],
            base_url=fields["base_url"],
            secret=fields["credential"],
            note=fields["note"],
        )
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from None
    file = f"import-{fields['provider']}-{uuid4().hex[:12]}"
    conn = request.app.state.db.connect()
    try:
        conn.execute(
            "INSERT INTO accounts(file, provider, uid, nickname, enterprise_id, realm, "
            "expires_at, credits, note, disabled, meta_json, created_at, owner_user_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                file,
                fields["provider"],
                mask_secret(fields["credential"]),
                fields["label"],
                "",
                "cn",
                0,
                None,
                fields["note"],
                0,
                json.dumps(
                    {
                        _IMPORT_MARK: True,
                        "credential_type": fields["credential_type"],
                        "credential_id": cred["id"],
                    },
                    ensure_ascii=False,
                ),
                now(),
                user["username"],
            ),
        )
        # 审计 detail 只记类型字段名，不碰凭据值
        audit_event(
            conn,
            user["username"],
            "accounts.import",
            f"{fields['provider']}#{file}",
            f"credential_type={fields['credential_type']}",
            request.client.host if request.client else "",
        )
        item = _account_item(conn.execute("SELECT * FROM accounts WHERE file = ?", (file,)).fetchone())
    finally:
        conn.close()
    return item


@router.post("/import/probe")
async def import_probe(body: AccountImportIn, request: Request, user: dict = Depends(admin_user)):
    """保存前用临时适配器试一次连通性；失败只报告、不阻断保存。

    临时实例 = 浅拷贝 + 重建 options，探测不落库也不改进程内共享适配器。
    """
    fields = _prepare_import(request, body)
    adapter = request.app.state.registry.get(fields["provider"])
    temp = copy.copy(adapter)
    temp.options = dict(adapter.options)
    temp.configure(api_key=fields["credential"], api_keys=[fields["credential"]])
    if fields["base_url"]:
        temp.configure(base_url=fields["base_url"])
    outcome = await provider_probe.probe(temp)
    return {
        # 账号导入沿用历史口径的 fail（前端已按 fail 上色），健康检测表则用 failed
        "status": "fail" if outcome.status == provider_probe.STATUS_FAILED else outcome.status,
        "connected": True if outcome.status == provider_probe.STATUS_OK else (
            False if outcome.status == provider_probe.STATUS_FAILED else None
        ),
        "message": outcome.message,
        "upstream_status": outcome.upstream_status,
    }


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


async def _delete_account_row(request: Request, file: str) -> None:
    """删一条账号：导入行跳过上游、回收镜像凭据；其余先删上游再删本地。

    单条端点与批量端点共用这一段，避免两套删除口径分叉。
    """
    row = require_file(request, file)
    registry = request.app.state.registry
    meta = _meta_of(row)
    if meta.get(_IMPORT_MARK):
        # 本端登记的行上游没有这份账号：跳过上游删除，只回收镜像进凭据库的那份凭据
        cid = meta.get("credential_id")
        if cid is not None:
            try:
                registry.delete_credential(int(cid))
            except ProviderError as exc:
                if exc.status_code != 404:
                    raise  # 凭据已单独删过（404）就照常删行，其余错误如实抛出
    else:
        await registry.get(row["provider"]).delete_account(file)  # 上游删不掉就抛错，不动本地记录
    conn = request.app.state.db.connect()
    try:
        conn.execute("DELETE FROM accounts WHERE file = ?", (file,))
        conn.commit()
    finally:
        conn.close()


@router.delete("/{file}")
async def remove_account(file: str, request: Request, user: dict = Depends(admin_user)):
    await _delete_account_row(request, file)
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


BATCH_ACCOUNT_ACTIONS = ("disable", "delete", "restart")
# 单次上限：与列表分页上限同量级，防止一条请求把整张表拖进上游超时
BATCH_MAX_FILES = 200


class BatchAccountsIn(BaseModel):
    action: str
    files: list[str] = Field(default_factory=list)
    disabled: bool = True  # 仅 disable 动作使用


async def _batch_disabled(request: Request, file: str, disabled: bool) -> dict[str, Any]:
    row = require_file(request, file)
    adapter = request.app.state.registry.get(row["provider"])
    upstream = await adapter.account_action(file, "disabled", {"disabled": disabled})
    # 与单条 /disabled 同一口径：上游答复后本地标记就落库，ok 只反映上游是否认可
    conn = request.app.state.db.connect()
    try:
        conn.execute("UPDATE accounts SET disabled = ? WHERE file = ?", (1 if disabled else 0, file))
        conn.commit()
    finally:
        conn.close()
    if bool(upstream.get("ok", True)):
        return {"id": file, "ok": True}
    return {"id": file, "ok": False, "error": str(upstream.get("message") or "上游拒绝了停用请求")}


async def _batch_restart(
    request: Request, file: str, runtimes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    row = require_file(request, file)
    adapter = request.app.state.registry.get(row["provider"])
    if _implements(adapter, "account_action"):
        result = await adapter.account_action(file, "restart")
        if bool(result.get("ok", True)):
            return {"id": file, "ok": True}
        return {"id": file, "ok": False, "error": str(result.get("message") or "上游拒绝了重启请求")}
    # 没有单账号重启的 provider：退回重启该家运行时，整批只重启一次
    if not _account_ops(adapter)["restart"]:
        return {"id": file, "ok": False, "error": f"{adapter.spec.name} 不支持重启"}
    pid = adapter.spec.id
    if pid not in runtimes:
        runtimes[pid] = await adapter.restart()
    result = runtimes[pid]
    if bool(result.get("ok", True)):
        return {"id": file, "ok": True}
    return {"id": file, "ok": False, "error": str(result.get("message") or "运行时重启失败")}


@router.post("/batch")
async def batch_accounts(body: BatchAccountsIn, request: Request, user: dict = Depends(admin_user)):
    """批量停用 / 删除 / 重启账号：逐条执行，单条失败只回填该条的 error。

    删除沿用单条口径（导入行回收镜像凭据）；restart 优先走单账号动作，
    适配器没有单账号动作时退回重启该家运行时且整批只重启一次。
    """
    action = (body.action or "").strip().lower()
    if action not in BATCH_ACCOUNT_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action 必须是 {'、'.join(BATCH_ACCOUNT_ACTIONS)} 之一",
        )
    files = list(dict.fromkeys(item.strip() for item in body.files if item and item.strip()))
    if not files:
        raise HTTPException(status_code=400, detail="files 不能为空")
    if len(files) > BATCH_MAX_FILES:
        raise HTTPException(status_code=400, detail=f"单次最多 {BATCH_MAX_FILES} 条")

    results: list[dict[str, Any]] = []
    runtimes: dict[str, dict[str, Any]] = {}
    for file in files:
        try:
            if action == "delete":
                await _delete_account_row(request, file)
                item = {"id": file, "ok": True}
            elif action == "disable":
                item = await _batch_disabled(request, file, body.disabled)
            else:
                item = await _batch_restart(request, file, runtimes)
        except HTTPException as exc:
            item = {"id": file, "ok": False, "error": str(exc.detail)}
        except ProviderError as exc:
            item = {"id": file, "ok": False, "error": exc.message}
        except Exception as exc:  # 上游异常形态不一，整批不能因为一条挂掉
            item = {"id": file, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        results.append(item)
    return {"ok": all(item["ok"] for item in results), "action": action, "results": results}
