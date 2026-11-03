"""访问令牌面板的后端：薄封装既有的网关密钥（api_keys），不另建一套存储。

基准前端（apps/web 的 TokensPanel）要的是 ApiToken 结构，而本仓的密钥表
只有 name/prefix/enabled/created_at/last_used_at。这里做字段映射：
- scope 固定 readonly：u2a_ 是**下游调模型**的网关凭据，不具备管理接口权限，
  不能标成 admin 让人误以为能调管理面。
- expires_at / last_used_ip 恒为 null：表里没有这两个字段。前端传了非空值
  直接 400 说明不支持，不静默降级成「永不过期」的假承诺。
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..security import admin_user, current_user
from .keys import create_key_record

router = APIRouter(prefix="/api/tokens", tags=["tokens"])

TOKEN_SCOPE = "readonly"


def token_from_row(row) -> dict:
    """api_keys 行 → 前端 ApiToken 结构。缺失字段如实置空，不编造。"""
    return {
        "id": row["id"],
        "name": row["name"],
        "prefix": row["prefix"],
        "scope": TOKEN_SCOPE,
        "enabled": bool(row["enabled"]),
        "expires_at": None,
        "created_at": row["created_at"],
        "created_by": "",  # 密钥表不记录创建者
        "last_used_at": row["last_used_at"],
        "last_used_ip": None,
    }


class TokenCreate(BaseModel):
    name: str
    scope: str | None = None
    expires_at: int | None = None


class TokenPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    scope: str | None = None
    expires_at: int | None = None


def _reject_unsupported(scope: str | None, expires_at: int | None, *, has_scope: bool) -> None:
    if has_scope and scope not in (None, TOKEN_SCOPE):
        raise HTTPException(400, "网关密钥不支持 admin 作用域，这里只提供只读令牌")
    if expires_at is not None:
        raise HTTPException(400, "网关密钥不支持到期时间，创建后长期有效，需停用请直接禁用或删除")


@router.get("")
def list_tokens(request: Request, user: dict = Depends(current_user)):
    """读允许 viewer，但只列自己名下的 key（NULL 归属对非 admin 不可见）。"""
    conn = request.app.state.db.connect()
    try:
        if user.get("role") == "admin":
            rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM api_keys WHERE owner_user_id = ? ORDER BY created_at DESC",
                (user.get("username") or "",),
            ).fetchall()
    finally:
        conn.close()
    return [token_from_row(r) for r in rows]


@router.post("")
def create_token(body: TokenCreate, request: Request, user: dict = Depends(admin_user)):
    name = body.name.strip()
    if not (1 <= len(name) <= 64):
        raise HTTPException(400, "名称长度需为 1-64 个字符")
    _reject_unsupported(body.scope, body.expires_at, has_scope="scope" in body.model_fields_set)
    conn = request.app.state.db.connect()
    try:
        key_id, token = create_key_record(conn, name, owner_user_id=user["username"])
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    finally:
        conn.close()
    return {**token_from_row(row), "token": token}  # 明文只此一次


@router.patch("/{token_id}")
def update_token(token_id: int, body: TokenPatch, request: Request, user: dict = Depends(admin_user)):
    if body.name is None and body.enabled is None:
        raise HTTPException(400, "没有需要更新的字段")
    _reject_unsupported(body.scope, body.expires_at, has_scope="scope" in body.model_fields_set)
    conn = request.app.state.db.connect()
    try:
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (token_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "访问令牌不存在")
        if body.name is not None:
            name = body.name.strip()
            if not (1 <= len(name) <= 64):
                raise HTTPException(400, "名称长度需为 1-64 个字符")
            conn.execute("UPDATE api_keys SET name = ? WHERE id = ?", (name, token_id))
        if body.enabled is not None:
            conn.execute("UPDATE api_keys SET enabled = ? WHERE id = ?", (1 if body.enabled else 0, token_id))
        conn.commit()
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (token_id,)).fetchone()
    finally:
        conn.close()
    return token_from_row(row)


@router.delete("/{token_id}")
def revoke_token(token_id: int, request: Request, user: dict = Depends(admin_user)):
    """删除即吊销：哈希没了，这把 u2a_ 立刻无法通过 /v1 鉴权。"""
    conn = request.app.state.db.connect()
    try:
        cur = conn.execute("DELETE FROM api_keys WHERE id = ?", (token_id,))
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "访问令牌不存在")
    return {"ok": True}
