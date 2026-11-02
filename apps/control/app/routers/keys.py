"""网关密钥管理：创建（明文只返回一次）/吊销/启停/改名。

存 sha256 哈希，不落明文。配额/模型白名单属 M3 计费范畴，M2 不收录字段，
避免「界面能配、网关不执行」的假承诺。
"""

import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..api_utils import now
from ..security import admin_user, current_user

router = APIRouter(prefix="/api/keys", tags=["keys"])

KEY_PREFIX = "u2a_"


def _row_to_public(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "prefix": row["prefix"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
    }


@router.get("")
def list_keys(request: Request, user: dict = Depends(current_user)):
    conn = request.app.state.db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_public(r) for r in rows]


class KeyCreate(BaseModel):
    name: str


@router.post("")
def create_key(body: KeyCreate, request: Request, user: dict = Depends(admin_user)):
    name = body.name.strip()
    if not (1 <= len(name) <= 64):
        raise HTTPException(400, "名称长度需为 1-64 个字符")
    token = KEY_PREFIX + secrets.token_urlsafe(24)
    digest = hashlib.sha256(token.encode()).hexdigest()
    conn = request.app.state.db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO api_keys(name, key_hash, prefix, enabled, created_at) VALUES(?,?,?,1,?)",
            (name, digest, token[:10], now()),
        )
        conn.commit()
        key_id = cur.lastrowid
    finally:
        conn.close()
    return {
        "id": key_id,
        "name": name,
        "prefix": token[:10],
        "enabled": True,
        "created_at": now(),
        "last_used_at": None,
        "key": token,  # 只此一次
    }


class KeyPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None


@router.patch("/{key_id}")
def update_key(key_id: int, body: KeyPatch, request: Request, user: dict = Depends(admin_user)):
    if body.name is None and body.enabled is None:
        raise HTTPException(400, "没有需要更新的字段")
    conn = request.app.state.db.connect()
    try:
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "密钥不存在")
        if body.name is not None:
            name = body.name.strip()
            if not (1 <= len(name) <= 64):
                raise HTTPException(400, "名称长度需为 1-64 个字符")
            conn.execute("UPDATE api_keys SET name = ? WHERE id = ?", (name, key_id))
        if body.enabled is not None:
            conn.execute("UPDATE api_keys SET enabled = ? WHERE id = ?", (1 if body.enabled else 0, key_id))
        conn.commit()
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_public(row)


@router.delete("/{key_id}")
def revoke_key(key_id: int, request: Request, user: dict = Depends(admin_user)):
    """吊销 = 删除记录：哈希没了，这把 key 立刻无法通过 /v1 鉴权。"""
    conn = request.app.state.db.connect()
    try:
        cur = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "密钥不存在")
    return {"ok": True}
