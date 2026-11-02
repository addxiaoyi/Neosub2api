"""账号/统计等跨 router 共用的小工具。"""

import json
import time
from typing import Any

from fastapi import HTTPException, Request


def now() -> int:
    return int(time.time())


def parse_bool(value: Any, field: str) -> bool:
    """严格布尔解析：只认真布尔与 "true"/"false" 字符串，其余一律 400。

    bool("false") 是真值，直接把字符串塞进开关会静默打开匿名网关（S-02）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "true":
            return True
        if text == "false":
            return False
    raise HTTPException(status_code=400, detail=f"{field} 必须是布尔值 true/false")


def mask_secret(value: str) -> str:
    """密钥掩码：只留前 4 位和后 4 位，短于 9 位整体隐藏。"""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}…{text[-4:]}"


def db_of(request: Request):
    return request.app.state.db.connect()


def public_account(row: Any) -> dict[str, Any]:
    expires_at = int(row["expires_at"] or 0)
    remain = max(expires_at - now(), 0) if expires_at else 0
    meta: dict[str, Any] = {}
    try:
        meta = json.loads(row["meta_json"] or "{}")
        if not isinstance(meta, dict):
            meta = {}
    except (json.JSONDecodeError, TypeError, IndexError, KeyError):
        meta = {}
    out = {
        "file": row["file"],
        "uid": row["uid"],
        "nickname": row["nickname"],
        "enterprise_id": row["enterprise_id"],
        "provider": row["provider"],
        "realm": row["realm"],
        "expires_at": expires_at,
        "is_expired": bool(expires_at and expires_at < now()),
        "remain_seconds": remain,
        "credits": row["credits"],
        "note": row["note"],
        "disabled": bool(row["disabled"]),
        "source": "file",
        "healthy": meta.get("healthy"),
        "poolUnknown": False,
    }
    # 上游运行时信息（签到状态/冷却等）平铺出去，前端直接读
    for key in ("checkin_today", "cooling", "cooldown_remaining", "status"):
        if key in meta:
            out[key] = meta[key]
    return out


def require_file(request: Request, file: str):
    conn = request.app.state.db.connect()
    try:
        row = conn.execute("SELECT * FROM accounts WHERE file = ?", (file,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return row
