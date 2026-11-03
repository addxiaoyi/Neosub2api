import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..adapters.base import ProviderError
from ..api_utils import mask_secret, now, parse_bool
from ..db import get_json_setting, put_settings, set_json_setting
from ..security import admin_user, audit_event, current_user, hash_password, revoke_user_sessions, verify_password

logger = logging.getLogger("unified.settings")


@asynccontextmanager
async def _settings_lifespan(app):
    """数据库里的运行参数在启动时覆盖 env 默认值；读库失败只告警，不阻塞启动。"""
    try:
        _apply_stored_runtime(app)
    except Exception:  # noqa: BLE001 - 配置读失败不能打挂控制面
        logger.warning("读取数据库运行参数失败，全部沿用环境变量默认值", exc_info=True)
    yield


router = APIRouter(prefix="/api", tags=["settings"], lifespan=_settings_lifespan)

CONFIG_GROUPS = (
    "schedule",
    "prompt",
    "cooldown",
    "pool",
    "features",
    "session_sticky",
    "server",
    "upstream",
    "global",
    "admin",
    # 下面三组是 settings 表里的独立键，不是 upstream_config 的字段
    "gateway",
    "model_map",
    "provider_config",
)

# 独立键组：读配置时按各自 settings 键保底，避免界面显示成空占位
DETACHED_GROUPS = ("gateway", "model_map", "provider_config")

# 会被脱敏的字段名（大小写不敏感；以 _key 结尾的也算，如 access_key）
SECRET_KEYS = frozenset({
    "api_key", "apikey", "token", "secret", "password",
    "access_token", "refresh_token", "cookie", "authorization", "client_secret",
})
REVEALABLE_ROOTS = frozenset({"provider_config", "upstream_config", "upstream", "upstash"})
MASK_ELLIPSIS = "…"


def _is_secret_key(key: str) -> bool:
    name = key.strip().lower()
    return name in SECRET_KEYS or name.endswith("_key") or name.endswith("_secret")


def _mask_tree(node: Any) -> Any:
    """递归给密钥字段换掩码，原字段直接移除，避免明文回显（S-01）。"""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            name = str(key)
            if _is_secret_key(name) and isinstance(value, str):
                out[f"{name}_masked"] = mask_secret(value)
                out[f"has_{name.lower()}"] = bool(value.strip())
                continue
            out[key] = _mask_tree(value)
        return out
    if isinstance(node, list):
        return [_mask_tree(item) for item in node]
    return node


def _is_mask(value: str) -> bool:
    return value == "***" or MASK_ELLIPSIS in value


def _strip_echo(node: Any) -> Any:
    """丢掉前端回传的掩码/派生字段，别把 "SECR…EY-42" 当新密钥写库。"""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            name = str(key)
            if name.endswith("_masked") or name.startswith("has_"):
                continue
            cleaned = _strip_echo(value)
            if cleaned is not _DROP:
                out[key] = cleaned
        return out
    if isinstance(node, list):
        return [item for item in (_strip_echo(v) for v in node) if item is not _DROP]
    if isinstance(node, str) and _is_mask(node):
        return _DROP
    return node


_DROP = object()


def _normalize_upstash(cfg: dict[str, Any]) -> None:
    node = cfg.get("upstash")
    if not isinstance(node, dict):
        cfg["upstash"] = {"url": "", "has_token": False, "token_masked": ""}
        return
    token = node.pop("token", None)
    if isinstance(token, str):
        node["token_masked"] = mask_secret(token)
        node["has_token"] = bool(token.strip())
    node.setdefault("url", "")
    node.setdefault("has_token", False)
    node.setdefault("token_masked", "")


def _deep_merge(base: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


# ===== 运行参数：非密钥、改后即时生效，数据库值优先于 env 默认值 =====

RUNTIME_KEY = "runtime_config"
GATEWAY_KEY = "gateway"  # 匿名开关沿用既有键，v1 网关直接读它

# 导出/导入明确排除的密钥来源（只是说明，结构上也没有读取路径）
SECRET_EXCLUSIONS = (
    "provider_credentials.secret",
    "providers 表与 provider_config 中的 api_key / token / secret / password",
    "upstream_config 中的密钥字段",
    "环境变量里的密钥（CODEBUDDY_API_KEY 等）",
)


@dataclass(frozen=True)
class RuntimeItem:
    """一条可在界面改的运行参数。store=gateway 的项沿用既有 key，其余进 runtime_config。"""

    key: str
    kind: str  # "int" | "bool"
    minimum: int | None = None
    maximum: int | None = None
    ui: bool = True
    store: str = "runtime"


RUNTIME_ITEMS: tuple[RuntimeItem, ...] = (
    RuntimeItem("models_cache_ttl_sec", "int", 0, 86400),
    RuntimeItem("failure_window_sec", "int", 0, 86400),
    RuntimeItem("failure_window_max", "int", 0, 100000),
    RuntimeItem("model_fallback_first", "bool"),
    RuntimeItem("sse_max_seconds", "int", 1, 86400),
    RuntimeItem("session_ttl_sec", "int", 60, 2592000),
    # 已有入口在「服务商」分组，这里只参与导出/导入，不重复放开关
    RuntimeItem("allow_anonymous_gateway", "bool", ui=False, store="gateway"),
)
RUNTIME_BY_KEY = {item.key: item for item in RUNTIME_ITEMS}

_INT_TEXT = re.compile(r"-?\d+$")


class _InvalidValue(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _coerce(item: RuntimeItem, raw: Any) -> bool | int:
    """严格校验：布尔只认 bool 与 true/false 字符串，整数只认整数（含数字字符串）。"""
    if item.kind == "bool":
        try:
            return parse_bool(raw, item.key)
        except HTTPException as exc:
            raise _InvalidValue("invalid_boolean", str(exc.detail)) from exc
    if isinstance(raw, bool):
        raise _InvalidValue("not_an_integer", f"{item.key} 必须是整数")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and _INT_TEXT.match(raw.strip()):
        value = int(raw.strip())
    else:
        raise _InvalidValue("not_an_integer", f"{item.key} 必须是整数")
    if item.minimum is not None and value < item.minimum:
        raise _InvalidValue("out_of_range", f"{item.key} 不能小于 {item.minimum}，当前 {value}")
    if item.maximum is not None and value > item.maximum:
        raise _InvalidValue("out_of_range", f"{item.key} 不能大于 {item.maximum}，当前 {value}")
    return value


def _stored_runtime(conn) -> tuple[dict[str, Any], dict[str, Any]]:
    runtime = get_json_setting(conn, RUNTIME_KEY, {})
    gateway = get_json_setting(conn, GATEWAY_KEY, {})
    return (
        runtime if isinstance(runtime, dict) else {},
        gateway if isinstance(gateway, dict) else {},
    )


def _stored_raw(item: RuntimeItem, runtime: dict[str, Any], gateway: dict[str, Any]) -> Any:
    if item.store == "gateway":
        return gateway.get("allow_anonymous")
    return runtime.get(item.key)


def _defaults(app) -> dict[str, bool | int]:
    """env 推导出来的默认值，启动时快照一份，避免被数据库覆盖后丢失基准。"""
    cached = getattr(app.state, "runtime_defaults", None)
    if isinstance(cached, dict) and cached:
        return cached
    snapshot = {item.key: getattr(app.state.settings, item.key) for item in RUNTIME_ITEMS}
    app.state.runtime_defaults = snapshot
    return snapshot


def _apply_stored_runtime(app) -> None:
    defaults = _defaults(app)
    conn = app.state.db.connect()
    try:
        runtime, gateway = _stored_runtime(conn)
    finally:
        conn.close()
    for item in RUNTIME_ITEMS:
        raw = _stored_raw(item, runtime, gateway)
        if raw is None:
            continue
        try:
            value = _coerce(item, raw)
        except _InvalidValue as exc:
            logger.warning("数据库里的 %s 不合法（%s），沿用默认值 %s", item.key, exc.message, defaults[item.key])
            continue
        # Settings 是 frozen dataclass：用 object.__setattr__ 就地改，持有同一实例的组件也能看到新值
        object.__setattr__(app.state.settings, item.key, value)


def _runtime_payload(app) -> dict[str, Any]:
    defaults = _defaults(app)
    conn = app.state.db.connect()
    try:
        runtime, gateway = _stored_runtime(conn)
    finally:
        conn.close()
    items: list[dict[str, Any]] = []
    for item in RUNTIME_ITEMS:
        raw = _stored_raw(item, runtime, gateway)
        source = "default"
        value = defaults[item.key]
        if raw is not None:
            try:
                value = _coerce(item, raw)
                source = "db"
            except _InvalidValue:
                source = "invalid"
        items.append(
            {
                "key": item.key,
                "type": item.kind,
                "value": value,
                "default": defaults[item.key],
                "source": source,
                "min": item.minimum,
                "max": item.maximum,
                "ui": item.ui,
            }
        )
    return {"ok": True, "items": items, "excluded_secrets": list(SECRET_EXCLUSIONS)}


def _bad_request(errors: list[dict[str, Any]], message: str = "配置校验失败") -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"type": "invalid_config", "message": message, "errors": errors}},
        status_code=400,
    )


def _write_runtime(request: Request, patch: Any, actor: dict, action: str) -> JSONResponse | dict[str, Any]:
    """校验全部通过后才写库；两项存储键在同一事务提交，失败整体回滚。"""
    if not isinstance(patch, dict) or not patch:
        return _bad_request([{"key": "", "reason": "empty_payload", "message": "没有可写入的配置项"}])
    clean: dict[str, bool | int] = {}
    errors: list[dict[str, Any]] = []
    for key, raw in patch.items():
        name = str(key).strip()
        item = RUNTIME_BY_KEY.get(name)
        if item is None:
            reason = "secret_not_allowed" if _is_secret_key(name) else "unknown_key"
            message = "密钥类配置请走凭据体系，不能从这里导入" if reason == "secret_not_allowed" else f"未知配置项：{name}"
            errors.append({"key": name, "value": raw, "reason": reason, "message": message})
            continue
        try:
            clean[name] = _coerce(item, raw)
        except _InvalidValue as exc:
            errors.append(
                {
                    "key": name,
                    "value": raw,
                    "reason": exc.reason,
                    "message": exc.message,
                    "min": item.minimum,
                    "max": item.maximum,
                }
            )
    if errors:
        return _bad_request(errors)

    app = request.app
    conn = app.state.db.connect()
    try:
        runtime, gateway = _stored_runtime(conn)
        touched_runtime = False
        touched_gateway = False
        for name, value in clean.items():
            if RUNTIME_BY_KEY[name].store == "gateway":
                gateway["allow_anonymous"] = value
                touched_gateway = True
            else:
                runtime[name] = value
                touched_runtime = True
        writes: dict[str, str] = {}
        if touched_runtime:
            writes[RUNTIME_KEY] = json.dumps(runtime, ensure_ascii=False)
        if touched_gateway:
            writes[GATEWAY_KEY] = json.dumps(gateway, ensure_ascii=False)
        try:
            put_settings(conn, writes)
            conn.commit()
        except Exception:  # noqa: BLE001 - 写失败整体回滚，不能留半套配置
            conn.rollback()
            logger.exception("运行参数写入失败，已回滚")
            return JSONResponse(
                {
                    "ok": False,
                    "error": {
                        "type": "write_failed",
                        "message": "写入数据库失败，本次改动已整体回滚",
                        "errors": [],
                    },
                },
                status_code=500,
            )
    finally:
        conn.close()

    # 落库成功后才改内存：请求处理链下一次读取就生效，无需重启
    for name, value in clean.items():
        object.__setattr__(app.state.settings, name, value)

    conn = app.state.db.connect()
    try:
        audit_event(
            conn,
            actor["username"],
            action,
            ",".join(sorted(clean)),
            json.dumps(clean, ensure_ascii=False),
            request.client.host if request.client else "",
        )
    finally:
        conn.close()
    payload = _runtime_payload(app)
    payload["saved"] = sorted(clean)
    return payload


@router.get("/settings/runtime-config")
def get_runtime_config(request: Request, user: dict = Depends(admin_user)):
    return _runtime_payload(request.app)


@router.post("/settings/runtime-config")
def save_runtime_config(
    body: dict[str, Any],
    request: Request,
    user: dict = Depends(admin_user),
):
    patch = body.get("items") if isinstance(body.get("items"), dict) else body
    return _write_runtime(request, patch, user, "settings.runtime_config")


@router.get("/settings/runtime-config/export")
def export_runtime_config(request: Request, user: dict = Depends(admin_user)):
    state = _runtime_payload(request.app)
    return {
        "kind": "unified-2api-runtime-config",
        "version": 1,
        "exported_at": now(),
        "items": {item["key"]: item["value"] for item in state["items"]},
        "excluded_secrets": state["excluded_secrets"],
        "note": "只含非密钥运行参数；凭据请在「服务商」页按 provider 管理。",
    }


@router.post("/settings/runtime-config/import")
def import_runtime_config(
    body: dict[str, Any],
    request: Request,
    user: dict = Depends(admin_user),
):
    if not isinstance(body, dict):
        return _bad_request([{"key": "", "reason": "invalid_shape", "message": "导入内容必须是 JSON 对象"}])
    kind = body.get("kind")
    if kind is not None and kind != "unified-2api-runtime-config":
        return _bad_request([{"key": "kind", "value": kind, "reason": "kind_mismatch", "message": "不是运行参数导出文件"}])
    version = body.get("version")
    if version is not None and version != 1:
        return _bad_request(
            [{"key": "version", "value": version, "reason": "unsupported_version", "message": "只支持 version=1"}]
        )
    items = body.get("items")
    if isinstance(items, dict):
        patch = items
    else:
        # 也接受扁平的 {key: value}；导出文件的元数据字段先剥掉
        meta = {"kind", "version", "exported_at", "excluded_secrets", "note"}
        patch = {k: v for k, v in body.items() if k not in meta}
    result = _write_runtime(request, patch, user, "settings.runtime_config.import")
    if isinstance(result, JSONResponse):
        return result
    result["imported"] = sorted(patch)
    return result


def _read_config(request: Request, *, reveal: bool = False) -> dict[str, Any]:
    """读上游配置。

    reveal=False 是唯一的对外形态：密钥字段换掩码；内部下发（push_config）才取明文。
    """
    conn = request.app.state.db.connect()
    try:
        stored = get_json_setting(conn, "upstream_config", {})
        detached = {group: get_json_setting(conn, group, {}) for group in DETACHED_GROUPS}
    finally:
        conn.close()
    cfg: dict[str, Any] = {
        "available": True,
        "config_path": "(控制面 SQLite)",
        "reload_hint": "保存后会下发到 CodeBuddy 管理端（写 workbuddy2api 的 config.json 并按其能力触发重载）；其余 provider 的设置项在 providers 页按适配器下发。",
    }
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k not in {"available", "config_path"}})
    for group in CONFIG_GROUPS:
        value = cfg.get(group)
        if group in detached:
            base = detached.get(group)
            merged = dict(base) if isinstance(base, dict) else {}
            # 深合并：upstream_config 里同名 provider 的局部字段不能把库里的 api_key 顶掉
            if isinstance(value, dict):
                merged = _deep_merge(merged, value)
            cfg[group] = merged
        elif not isinstance(value, dict):
            cfg[group] = {}
    if reveal:
        return cfg
    redacted = _mask_tree(cfg)
    _normalize_upstash(redacted)
    return redacted


@router.get("/settings/upstream")
def get_upstream_config(request: Request, user: dict = Depends(admin_user)):
    return _read_config(request)


class RevealBody(BaseModel):
    path: str


@router.post("/settings/secret/reveal")
def reveal_secret(body: RevealBody, request: Request, actor: dict = Depends(admin_user)):
    """显式查看明文：仅管理员，且每次调用都写审计。"""
    parts = [part for part in body.path.split(".") if part]
    if not parts or parts[0] not in REVEALABLE_ROOTS:
        raise HTTPException(400, "只能查看 provider_config / upstream 组内的密钥")
    node: Any = _read_config(request, reveal=True)
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            raise HTTPException(404, f"配置路径不存在：{body.path}")
        node = node[part]
    if not isinstance(node, str) or not node.strip():
        raise HTTPException(400, "目标字段不是密钥字符串")
    conn = request.app.state.db.connect()
    try:
        audit_event(
            conn,
            actor["username"],
            "settings.reveal_secret",
            body.path,
            "",
            request.client.host if request.client else "",
        )
    finally:
        conn.close()
    return {"path": body.path, "value": node}


@router.post("/settings/upstream")
async def save_upstream_config(
    patch: dict[str, Any],
    request: Request,
    user: dict = Depends(admin_user),
):
    cleaned = _strip_echo({k: v for k, v in patch.items() if k not in {"available", "config_path", "reload_hint", "push"}})
    conn = request.app.state.db.connect()
    try:
        stored = get_json_setting(conn, "upstream_config", {})
        if not isinstance(stored, dict):
            stored = {}
        set_json_setting(conn, "upstream_config", _deep_merge(stored, cleaned))
    finally:
        conn.close()

    # 真实下发：CodeBuddy 启用时把配置推到它的管理端（写上游 config.json 并重载）
    registry = request.app.state.registry
    enabled = {a.spec.id for a in registry.enabled()}
    push: dict[str, Any]
    if "codebuddy" in enabled:
        try:
            # 下发必须用明文，读接口的掩码会写出坏配置
            result = await registry.get("codebuddy").push_config(_read_config(request, reveal=True))
            push = {"ok": True, **{k: v for k, v in result.items() if k in ("reload_scheduled", "reload_hint")}}
        except ProviderError as exc:
            push = {"ok": False, "message": exc.message}
    else:
        push = {"ok": False, "message": "CodeBuddy 未启用，配置仅保存在控制面"}

    cfg = _read_config(request)
    cfg["push"] = {"codebuddy": push}
    return cfg


@router.get("/settings/gateway")
def get_gateway_config(request: Request, user: dict = Depends(admin_user)):
    conn = request.app.state.db.connect()
    try:
        stored = get_json_setting(conn, "gateway", {})
    finally:
        conn.close()
    if not isinstance(stored, dict):
        stored = {}
    allow = stored.get("allow_anonymous")
    if allow is None:
        # 没配过时回落到环境变量，让界面显示的是**实际生效**值
        allow = request.app.state.settings.allow_anonymous_gateway
    else:
        try:
            allow = parse_bool(allow, "allow_anonymous")
        except HTTPException:
            allow = False
    return {"allow_anonymous": allow, "configured": "allow_anonymous" in stored}


@router.post("/settings/gateway")
def save_gateway_config(
    body: dict[str, Any],
    request: Request,
    user: dict = Depends(admin_user),
):
    if "allow_anonymous" not in body:
        raise HTTPException(400, "缺少 allow_anonymous")
    allow = parse_bool(body.get("allow_anonymous"), "allow_anonymous")
    conn = request.app.state.db.connect()
    try:
        set_json_setting(conn, "gateway", {"allow_anonymous": allow})
        audit_event(
            conn,
            user["username"],
            "settings.gateway",
            "allow_anonymous",
            "true" if allow else "false",
            request.client.host if request.client else "",
        )
    finally:
        conn.close()
    return {"allow_anonymous": allow, "configured": True}


@router.get("/settings/model-map")
def get_model_map(request: Request, user: dict = Depends(admin_user)):
    conn = request.app.state.db.connect()
    try:
        value = get_json_setting(conn, "model_map", {})
    finally:
        conn.close()
    return value if isinstance(value, dict) else {}


@router.post("/settings/model-map")
def save_model_map(
    body: dict[str, Any],
    request: Request,
    user: dict = Depends(admin_user),
):
    mapping = {
        str(key).strip(): str(value).strip()
        for key, value in body.items()
        if str(key).strip() and str(value).strip()
    }
    conn = request.app.state.db.connect()
    try:
        set_json_setting(conn, "model_map", mapping)
    finally:
        conn.close()
    return mapping


@router.post("/settings/upstash/test")
def test_upstash(request: Request, user: dict = Depends(admin_user)):
    return {"ok": False, "message": "M1 未接入 Upstash：持久化由控制面 SQLite 负责"}


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class UserPatch(BaseModel):
    password: str | None = None
    role: str | None = None


def _check_role(role: str) -> str:
    if role not in {"admin", "viewer"}:
        raise HTTPException(400, "角色只能是 admin 或 viewer")
    return role


@router.get("/users")
def list_users(request: Request, user: dict = Depends(current_user)):
    conn = request.app.state.db.connect()
    try:
        rows = conn.execute("SELECT username, role FROM users ORDER BY created_at").fetchall()
    finally:
        conn.close()
    return [{"username": row["username"], "role": row["role"]} for row in rows]


@router.post("/users")
def add_user(body: UserCreate, request: Request, user: dict = Depends(admin_user)):
    username = body.username.strip()
    if not (3 <= len(username) <= 32):
        raise HTTPException(400, "用户名长度需为 3-32 个字符")
    if len(body.password) < 6:
        raise HTTPException(400, "密码至少 6 个字符")
    conn = request.app.state.db.connect()
    try:
        exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if exists:
            raise HTTPException(409, "用户名已存在")
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            (username, hash_password(body.password), _check_role(body.role), now()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"username": username, "role": body.role}


@router.patch("/users/{username}")
def update_user(
    username: str,
    body: UserPatch,
    request: Request,
    actor: dict = Depends(admin_user),
):
    if body.password is None and body.role is None:
        raise HTTPException(400, "密码与角色至少要改一项")
    if body.password is not None and len(body.password) < 6:
        raise HTTPException(400, "密码至少 6 个字符")
    conn = request.app.state.db.connect()
    try:
        row = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            raise HTTPException(404, "用户不存在")
        if body.role is not None and row["role"] == "admin" and body.role != "admin":
            admins = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'").fetchone()["n"]
            if int(admins) <= 1:
                raise HTTPException(400, "不能降级最后一个管理员")
        if body.password is not None:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (hash_password(body.password), username),
            )
            revoke_user_sessions(conn, username)
        if body.role is not None:
            conn.execute("UPDATE users SET role = ? WHERE username = ?", (_check_role(body.role), username))
        conn.commit()
        role = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()["role"]
    finally:
        conn.close()
    return {
        "username": username,
        "role": role,
        "sessions_revoked": body.password is not None,
        "relogin_required": body.password is not None and username == actor["username"],
    }


@router.delete("/users/{username}")
def remove_user(username: str, request: Request, actor: dict = Depends(admin_user)):
    if username == actor["username"]:
        raise HTTPException(400, "不能删除当前登录用户")
    conn = request.app.state.db.connect()
    try:
        row = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            raise HTTPException(404, "用户不存在")
        if row["role"] == "admin":
            admins = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'").fetchone()["n"]
            if int(admins) <= 1:
                raise HTTPException(400, "不能删除最后一个管理员")
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
        revoke_user_sessions(conn, username)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}
