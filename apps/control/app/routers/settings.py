from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..adapters.base import ProviderError
from ..api_utils import mask_secret, now, parse_bool
from ..db import get_json_setting, set_json_setting
from ..security import admin_user, audit_event, current_user, hash_password, revoke_user_sessions, verify_password

router = APIRouter(prefix="/api", tags=["settings"])

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
