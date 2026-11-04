import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..adapters.base import ProviderError
from ..api_utils import mask_secret, mask_text, now, parse_bool
from ..db import get_json_setting, put_settings, set_json_setting
from ..net_guard import BLOCKED_ADDRESSES, BLOCKED_HOSTNAMES
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


def _audit_reveal(request: Request, actor: dict, path: str, *, ok: bool, reason: str = "") -> None:
    """成功与失败都留痕；行内只写配置路径与原因码，绝不写密钥值。"""
    action = "settings.reveal_secret" if ok else f"settings.reveal_secret.{reason}"
    conn = request.app.state.db.connect()
    try:
        audit_event(
            conn,
            actor["username"],
            action,
            path,
            "",
            request.client.host if request.client else "",
        )
    finally:
        conn.close()


@router.post("/settings/secret/reveal")
def reveal_secret(body: RevealBody, request: Request, actor: dict = Depends(admin_user)):
    """显式查看明文：仅管理员，成功与失败都写审计，审计行不含明文值。"""
    parts = [part for part in body.path.split(".") if part]
    if not parts or parts[0] not in REVEALABLE_ROOTS:
        _audit_reveal(request, actor, body.path, ok=False, reason="bad_root")
        raise HTTPException(400, "只能查看 provider_config / upstream 组内的密钥")
    node: Any = _read_config(request, reveal=True)
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            _audit_reveal(request, actor, body.path, ok=False, reason="path_not_found")
            raise HTTPException(404, f"配置路径不存在：{body.path}")
        node = node[part]
    if not isinstance(node, str) or not node.strip():
        _audit_reveal(request, actor, body.path, ok=False, reason="not_a_secret")
        raise HTTPException(400, "目标字段不是密钥字符串")
    _audit_reveal(request, actor, body.path, ok=True)
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


# ===== Upstash 连通性测试：只读 PING 探测，不写任何数据 =====

_UPSTASH_TIMEOUT_SEC = 10.0
_UPSTASH_PIPELINE_PATH = "/pipeline"
_UPSTASH_DETAIL_MAX = 200
# PING 响应只有几十字节；超限立刻断，防恶意上游用超大响应体打爆内存
_UPSTASH_MAX_BODY = 64 * 1024
# 显式声明不跟随：跟随重定向等于把外网 302 变成通往内网的跳板
_UPSTASH_FOLLOW_REDIRECTS = False
_UPSTASH_HOST_RE = re.compile(r"[A-Za-z0-9.\-:_\[\]]+")
# 响应里可能顺带给出的元信息键；各家部署不统一，取到就带，取不到不编
_UPSTASH_INFO_KEYS = ("version", "redis_version", "region", "node")


class _UpstashProbeError(Exception):
    """探测失败的结构化错误：由路由统一翻译成 {ok:false, detail, error{type,code}} 外壳。"""

    def __init__(self, status_code: int, code: str, type_: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.type = type_
        self.message = message


def _upstash_addr(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """字面量 IP 解析；IPv4-mapped IPv6（::ffff:10.0.0.5）折回 IPv4，避免换壳绕过。"""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _upstash_forbidden(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """Upstash 是纯公网服务：环回 / 私网 / 链路本地 / 保留 / 元数据一律拒，不套 provider 那套内网白名单。"""
    if addr in BLOCKED_ADDRESSES:
        return "云元数据地址"
    if addr.is_loopback:
        return "环回地址"
    if addr.is_link_local:
        return "链路本地地址"
    if addr.is_private:  # 含 10./172.16-31./192.168. 与 IPv6 ULA fc00::/7
        return "内网地址"
    if addr.is_reserved or addr.is_unspecified or addr.is_multicast:
        return "保留地址"
    return ""


def _upstash_host_forbidden(host: str) -> str:
    """主机名红线先于 DNS 拦（localhost / 元数据域名），字面量 IP 当场判，解析结果在 guard 里判。"""
    name = host.lower().rstrip(".")
    if name in BLOCKED_HOSTNAMES:
        return f"受限主机名 {name}"
    addr = _upstash_addr(host)
    if addr is not None:
        reason = _upstash_forbidden(addr)
        if reason:
            return f"{reason} {addr}"
    return ""


async def _read_json_object(request: Request) -> dict[str, Any]:
    """空体 / 非对象体按 {} 处理：交给业务校验出 400 + code，不落到框架 422 文案。"""
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001 - 空 body 与坏 JSON 统一按缺失处理
        return {}
    return raw if isinstance(raw, dict) else {}


def _upstash_base(raw: Any) -> str:
    """校验并归一 Upstash 地址：https + 合法主机 + 不指向内网/保留目标，其余交给 guard 做解析后校验。"""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise _UpstashProbeError(400, "invalid_upstash_url", "invalid_config", "缺少 Upstash 地址")
    if any(ch.isspace() for ch in text):
        raise _UpstashProbeError(400, "invalid_upstash_url", "invalid_config", "Upstash 地址不能包含空白字符")
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        port = parts.port  # 非法端口（超界 / 非数字）在这里抛 ValueError
    except ValueError:
        raise _UpstashProbeError(400, "invalid_upstash_url", "invalid_config", "Upstash 地址格式非法") from None
    if parts.scheme != "https":
        raise _UpstashProbeError(400, "invalid_upstash_url", "invalid_config", "Upstash 地址必须是 https")
    if parts.username or parts.password:
        raise _UpstashProbeError(400, "invalid_upstash_url", "invalid_config", "Upstash 地址不能携带用户名或密码")
    if parts.query or parts.fragment:
        raise _UpstashProbeError(400, "invalid_upstash_url", "invalid_config", "Upstash 地址不能带 query 或 fragment")
    if not host or not _UPSTASH_HOST_RE.fullmatch(host):
        raise _UpstashProbeError(400, "invalid_upstash_url", "invalid_config", "Upstash 主机名不合法")
    reason = _upstash_host_forbidden(host)
    if reason:
        raise _UpstashProbeError(
            400,
            "upstash_target_forbidden",
            "invalid_config",
            f"Upstash 地址指向{reason}：控制面只探测公网 Upstash 服务，不接受内网 / 保留地址",
        )
    if port is not None:
        authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    else:
        authority = host
    return f"https://{authority}{parts.path.rstrip('/')}"


def _stored_upstash_token(request: Request) -> str:
    conn = request.app.state.db.connect()
    try:
        stored = get_json_setting(conn, "upstream_config", {})
    finally:
        conn.close()
    node = stored.get("upstash") if isinstance(stored, dict) else None
    token = node.get("token") if isinstance(node, dict) else None
    return token.strip() if isinstance(token, str) else ""


def _upstash_token(raw: Any, stored: str) -> str:
    """本次传值优先；留空或传来的是掩码就回退已保存值（前端约定「留空=用已保存」）。"""
    text = raw.strip() if isinstance(raw, str) else ""
    if text and not _is_mask(text):
        return text
    if stored:
        return stored
    raise _UpstashProbeError(400, "upstash_token_missing", "invalid_config", "缺少 Upstash token：先填写或先保存 token")


def _resolve_upstash(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """连接前先解析：域名指到内网（DNS rebinding）必须在发起请求前拦下，而不是只看字面量。"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise _UpstashProbeError(
            502, "upstash_unreachable", "upstream_error",
            f"无法解析 Upstash 主机 {host}：{mask_text(str(exc))[:_UPSTASH_DETAIL_MAX]}",
        ) from exc
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        addr = _upstash_addr(info[4][0])
        if addr is not None:
            addrs.append(addr)
    return addrs


async def _guard_upstash_target(base: str) -> None:
    """字面量 IP 已在 _upstash_base 拦过；域名走解析后校验，任一解析结果指向禁网段即整体拒绝。"""
    parts = urlsplit(base)
    host = parts.hostname or ""
    if _upstash_addr(host) is not None:
        return
    port = parts.port or 443
    addrs = await asyncio.to_thread(_resolve_upstash, host, port)
    if not addrs:
        raise _UpstashProbeError(502, "upstash_unreachable", "upstream_error", f"无法解析 Upstash 主机 {host}")
    for addr in addrs:
        reason = _upstash_forbidden(addr)
        if reason:
            raise _UpstashProbeError(
                400,
                "upstash_target_forbidden",
                "invalid_config",
                f"Upstash 主机 {host} 解析到{reason} {addr}：控制面不向内网 / 保留地址发起请求",
            )


def _upstash_client() -> httpx.AsyncClient:
    # trust_env=False：本机 HTTP_PROXY 之类的环境变量不该劫持管理面的主动探测
    return httpx.AsyncClient(
        trust_env=False,
        timeout=_UPSTASH_TIMEOUT_SEC,
        follow_redirects=_UPSTASH_FOLLOW_REDIRECTS,
    )


def _upstash_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001 - 非 JSON 响应交给调用方按形状判断
        return None


def _upstash_snippet(resp: httpx.Response) -> str:
    """上游错误体只抽一小段人读信息：掩码 + 截断，别把整页响应吐给前端。"""
    data = _upstash_json(resp)
    if isinstance(data, dict):
        for key in ("error", "message", "detail"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return f"：{mask_text(value.strip())[:_UPSTASH_DETAIL_MAX]}"
    text = " ".join((resp.text or "").split())
    return f"：{mask_text(text)[:_UPSTASH_DETAIL_MAX]}" if text else ""


def _upstash_outcome(resp: httpx.Response, latency_ms: int) -> dict[str, Any]:
    """按上游状态与响应形状分类：3xx → 不跟随，凭据 401/403 → 400，其余 4xx → 400，5xx 或形状不对 → 502。"""
    status = resp.status_code
    snippet = _upstash_snippet(resp)
    if 300 <= status < 400:
        raise _UpstashProbeError(
            502,
            "upstash_redirect_blocked",
            "upstream_error",
            f"上游返回重定向（{status}），探测不跟随跳转，请直接填写 Upstash 的目标地址{snippet}",
        )
    if status in (401, 403):
        raise _UpstashProbeError(
            400, "upstash_auth_rejected", "authentication_error", f"凭据被 Upstash 拒绝（上游 {status}）{snippet}"
        )
    if 400 <= status < 500:
        raise _UpstashProbeError(
            400, "upstash_rejected", "invalid_config", f"Upstash 拒绝了探测请求（上游 {status}）{snippet}"
        )
    if not 200 <= status < 300:
        raise _UpstashProbeError(
            502, "upstash_unavailable", "upstream_error", f"Upstash 返回异常状态（上游 {status}）{snippet}"
        )
    data = _upstash_json(resp)
    if isinstance(data, list):
        entry = data[0] if data else None
    elif isinstance(data, dict):
        entry = data
    else:
        raise _UpstashProbeError(502, "upstash_unavailable", "upstream_error", "Upstash 响应不是 REST JSON，无法确认连通性")
    if isinstance(entry, dict) and "error" in entry and "result" not in entry:
        reason = mask_text(str(entry.get("error")))[:_UPSTASH_DETAIL_MAX]
        raise _UpstashProbeError(400, "upstash_rejected", "invalid_config", f"Upstash 返回错误：{reason}")
    detail = "Upstash REST 连通正常，PING 探测通过"
    if isinstance(entry, dict) and isinstance(entry.get("result"), str):
        detail += f"（PING 返回 {entry['result'][:64]}）"
    payload: dict[str, Any] = {
        "ok": True,
        "verified": True,
        "latency_ms": latency_ms,
        "detail": detail,
        "message": detail,
    }
    if isinstance(data, dict):
        info = {key: data[key] for key in _UPSTASH_INFO_KEYS if isinstance(data.get(key), (str, int))}
        if info:
            payload["info"] = info
    return payload


async def _probe_upstash(base: str, token: str) -> dict[str, Any]:
    """Upstash REST 只读探测：pipeline 只跑一条 PING，流式读且限长，全程不写数据。"""
    started = time.perf_counter()
    status = 0
    headers: httpx.Headers | dict[str, str] = {}
    body = bytearray()
    try:
        async with _upstash_client() as client:
            async with client.stream(
                "POST",
                f"{base}{_UPSTASH_PIPELINE_PATH}",
                json=[["PING"]],
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                status = resp.status_code
                headers = {k: v for k, v in resp.headers.items() if k.lower() not in {"content-length", "content-encoding"}}
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _UPSTASH_MAX_BODY:
                        raise _UpstashProbeError(
                            502,
                            "upstash_response_too_large",
                            "upstream_error",
                            f"Upstash 响应超过 {_UPSTASH_MAX_BODY} 字节上限，已中止读取",
                        )
    except httpx.TimeoutException as exc:
        raise _UpstashProbeError(
            504, "upstash_timeout", "upstream_error", f"连接 Upstash 超时（{_UPSTASH_TIMEOUT_SEC:g} 秒）"
        ) from exc
    except httpx.InvalidURL as exc:
        raise _UpstashProbeError(400, "invalid_upstash_url", "invalid_config", "Upstash 地址不合法，无法发起请求") from exc
    except httpx.HTTPError as exc:
        reason = str(exc).strip() or type(exc).__name__
        raise _UpstashProbeError(
            502, "upstash_unreachable", "upstream_error", f"无法连接 Upstash：{mask_text(reason)[:_UPSTASH_DETAIL_MAX]}"
        ) from exc
    upstream = httpx.Response(status, headers=headers, content=bytes(body))
    return _upstash_outcome(upstream, round((time.perf_counter() - started) * 1000))


@router.post("/settings/upstash/test")
async def test_upstash(request: Request, user: dict = Depends(admin_user)):
    """真实连通性探测：只读 PING，不写库、不改配置；失败走 {detail, error{type,code}} 统一外壳。"""
    payload = await _read_json_object(request)
    try:
        base = _upstash_base(payload.get("url"))
        token = _upstash_token(payload.get("token"), _stored_upstash_token(request))
        await _guard_upstash_target(base)
        return await _probe_upstash(base, token)
    except _UpstashProbeError as exc:
        return JSONResponse(
            {
                "ok": False,
                "detail": exc.message,
                "error": {"type": exc.type, "code": exc.code, "message": exc.message},
            },
            status_code=exc.status_code,
        )


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
