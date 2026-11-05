"""网关密钥管理：创建（明文只返回一次）/吊销/启停/改名。

存 sha256 哈希，不落明文。配额/模型白名单属 M3 计费范畴，M2 不收录字段，
避免「界面能配、网关不执行」的假承诺。

归属（owner_user_id）沿用 users 主键：users.username 是 TEXT 主键，所以这里存用户名。
admin 可查全部、可建任意归属的 key；非 admin 只列自己的 key。
"""

import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..api_utils import now
from ..key_prefix import KEY_PREFIX, STORED_PREFIX_LEN
from ..security import admin_user, audit_event, current_user

router = APIRouter(prefix="/api/keys", tags=["keys"])


def _row_to_public(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "prefix": row["prefix"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "owner_user_id": row["owner_user_id"],
    }


@router.get("")
def list_keys(request: Request, user: dict = Depends(current_user)):
    conn = request.app.state.db.connect()
    try:
        if user.get("role") == "admin":
            rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
        else:
            # NULL 归属的老 key 对非 admin 不可见
            rows = conn.execute(
                "SELECT * FROM api_keys WHERE owner_user_id = ? ORDER BY created_at DESC",
                (user.get("username") or "",),
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_public(r) for r in rows]


class KeyCreate(BaseModel):
    name: str
    # 仅 admin 生效：不传就归当前登录用户；显式指定则归他人
    owner_user_id: str | None = None


def create_key_record(conn, name: str, owner_user_id: str | None = None) -> tuple[int, str]:
    """建一把网关密钥，返回 (id, 明文)。明文只在该请求里回传一次。"""
    token = KEY_PREFIX + secrets.token_urlsafe(24)
    digest = hashlib.sha256(token.encode()).hexdigest()
    cur = conn.execute(
        "INSERT INTO api_keys(name, key_hash, prefix, enabled, created_at, owner_user_id) "
        "VALUES(?,?,?,1,?,?)",
        (name, digest, token[:STORED_PREFIX_LEN], now(), owner_user_id or None),
    )
    conn.commit()
    return cur.lastrowid, token


@router.post("")
def create_key(body: KeyCreate, request: Request, user: dict = Depends(admin_user)):
    name = body.name.strip()
    if not (1 <= len(name) <= 64):
        raise HTTPException(400, "名称长度需为 1-64 个字符")
    owner = (body.owner_user_id or "").strip() or user["username"]
    conn = request.app.state.db.connect()
    try:
        if owner != user["username"]:
            exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (owner,)).fetchone()
            if exists is None:
                raise HTTPException(400, f"归属用户不存在：{owner}")
        key_id, token = create_key_record(conn, name, owner_user_id=owner)
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    finally:
        conn.close()
    return {**_row_to_public(row), "key": token}  # 只此一次


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


def delete_key_cascade(conn, key_id: int) -> int:
    """删密钥并回收它的配额策略/告警；返回 api_keys 实际删除行数（0 = 不存在）。"""
    # 密钥没了，它的配额策略与告警就是孤儿；留着会让 admin 的 /api/quota/policies 列出无主策略
    policy_ids = [
        int(row["id"])
        for row in conn.execute("SELECT id FROM quota_policies WHERE key_id = ?", (key_id,)).fetchall()
    ]
    if policy_ids:
        marks = ",".join("?" for _ in policy_ids)
        conn.execute(f"DELETE FROM quota_alerts WHERE policy_id IN ({marks})", tuple(policy_ids))
        conn.execute(f"DELETE FROM quota_alert_thresholds WHERE policy_id IN ({marks})", tuple(policy_ids))
        conn.execute(f"DELETE FROM quota_policies WHERE id IN ({marks})", tuple(policy_ids))
    cur = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    return cur.rowcount


@router.delete("/{key_id}")
def revoke_key(key_id: int, request: Request, user: dict = Depends(admin_user)):
    """吊销 = 删除记录：哈希没了，这把 key 立刻无法通过 /v1 鉴权。"""
    conn = request.app.state.db.connect()
    try:
        removed = delete_key_cascade(conn, key_id)
        conn.commit()
    finally:
        conn.close()
    if removed == 0:
        raise HTTPException(404, "密钥不存在")
    return {"ok": True}


BATCH_KEY_ACTIONS = ("enable", "disable", "delete")
# 与列表分页上限同量级：一条请求不把整张表拖进单次连接超时
BATCH_MAX_IDS = 200


class BatchKeysIn(BaseModel):
    action: str
    ids: list[str] = Field(default_factory=list)


def apply_key_batch(conn, ids: list[str], action: str, missing: str = "密钥不存在") -> list[dict]:
    """逐条执行 enable/disable/delete，单条失败只回填该条，不牵连其余。

    返回体只允许 id / ok / error：密钥哈希与明文都不进响应。
    """
    results: list[dict] = []
    for raw in ids:
        try:
            try:
                key_id = int(raw)
            except ValueError:
                results.append({"id": raw, "ok": False, "error": "ID 必须是整数"})
                continue
            row = conn.execute("SELECT id FROM api_keys WHERE id = ?", (key_id,)).fetchone()
            if row is None:
                results.append({"id": raw, "ok": False, "error": missing})
                continue
            if action == "delete":
                if delete_key_cascade(conn, key_id) == 0:
                    results.append({"id": raw, "ok": False, "error": missing})
                    continue
            else:
                conn.execute(
                    "UPDATE api_keys SET enabled = ? WHERE id = ?",
                    (1 if action == "enable" else 0, key_id),
                )
            conn.commit()
            results.append({"id": raw, "ok": True})
        except Exception as exc:  # 单条写失败（如锁冲突）不能中断整批
            conn.rollback()
            results.append({"id": raw, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
    return results


def batch_ids(body: BatchKeysIn) -> tuple[str, list[str]]:
    """去重但保序；keys 与 tokens 两处端点共用同一套边界校验。"""
    action = (body.action or "").strip().lower()
    if action not in BATCH_KEY_ACTIONS:
        raise HTTPException(400, f"action 必须是 {'、'.join(BATCH_KEY_ACTIONS)} 之一")
    ids = list(dict.fromkeys(item.strip() for item in body.ids if item and item.strip()))
    if not ids:
        raise HTTPException(400, "ids 不能为空")
    if len(ids) > BATCH_MAX_IDS:
        raise HTTPException(400, f"单次最多 {BATCH_MAX_IDS} 条")
    return action, ids


@router.post("/batch")
def batch_keys(body: BatchKeysIn, request: Request, user: dict = Depends(admin_user)):
    """批量启停/删除：admin-only，逐条回执；一次批量只落一条审计（不逐条刷屏）。"""
    action, ids = batch_ids(body)
    conn = request.app.state.db.connect()
    try:
        results = apply_key_batch(conn, ids, action)
        ok_count = sum(1 for item in results if item["ok"])
        audit_event(
            conn,
            user["username"],
            "keys.batch",
            action,
            f"count={len(ids)} ok={ok_count}",
            request.client.host if request.client else "",
        )
    finally:
        conn.close()
    return {"ok": all(item["ok"] for item in results), "action": action, "results": results}
