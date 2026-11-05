"""账号/统计等跨 router 共用的小工具。"""

import json
import re
import time
from typing import Any

from fastapi import HTTPException, Request

from .key_prefix import KEY_PREFIX


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


_KEY_IN_TEXT = re.compile(re.escape(KEY_PREFIX) + r"[A-Za-z0-9_-]{4,}")


# 通用凭据形态。每条都要求「已知前缀 + 足够长的随机体」，长度下限就是误杀闸门。
def _keep_lead_mask_value(match: re.Match[str]) -> str:
    # 查询参数只换取值，?key= 这类键名留着才好排障
    return f"{match.group('lead')}{match.group('name')}=***"


_CREDENTIAL_RULES: tuple[tuple[re.Pattern[str], Any], ...] = (
    # Bearer 后的 opaque token：16 位起步，放过 "Bearer certificates" 这类正常英文搭配
    (re.compile(r"(?<![A-Za-z0-9])(Bearer\s+)[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE), r"\1***"),
    # JWT 三段式：头固定 eyJ，三段各 8 位以上，普通 a.b.c 不会被当成 token
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "***"),
    # URL 查询参数：只吃敏感参数名的取值，保留 ?key= 前缀便于排障；取值 6 位起步
    (
        re.compile(
            r"(?P<lead>[?&]|^|[\s,\"\'])(?P<name>client_secret|access_token|refresh_token"
            r"|api_key|apikey|secret|token|password|key)=(?P<value>[^\s&\"'<>…]{6,})"
        ),
        _keep_lead_mask_value,
    ),
    # OpenAI 风格：左边界断言排除 risk-/task- 这种普通英文里的 sk-，8 位起步
    (re.compile(r"(?<![A-Za-z0-9])sk[-_][A-Za-z0-9_-]{8,}"), "***"),
    # 常见厂商前缀：固定尾随长度，单独出现的前缀字样不命中
    (re.compile(r"(?<![A-Za-z0-9])(?:ghp_[A-Za-z0-9]{20,}|xoxb-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{16})"), "***"),
)


# 对外文案里的 URL：userinfo 可能是 user:pass@，query 可能带 key=，都必须丢掉
_URL_IN_TEXT = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<userinfo>[^/@\s]*@)?"
    r"(?P<host>[^/\s?#]+)(?P<path>/[^\s?#]*)?(?P<tail>[?#][^\s]*)?"
)


def safe_url(url: str) -> str:
    """上游地址对外展示前去掉 userinfo 与 query/fragment，只留 scheme://host[:port]/path。"""
    cleaned = _URL_IN_TEXT.sub(
        lambda match: f"{match.group('scheme')}{match.group('host')}{match.group('path') or ''}",
        str(url or ""),
    )
    return mask_text(cleaned)


def exc_summary(exc: BaseException, *, limit: int = 160) -> str:
    """对外错误只留异常类型名 + 脱敏后的 str(exc)；repr 会带完整 URL/请求对象，不原样透出。"""
    detail = mask_text(safe_url(str(exc) or "").strip())[:limit].strip()
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name


def mask_text(value: str) -> str:
    """日志/审计落库前的统一脱敏：u2a_ 掩码口径不变，其余凭据形态一律换成 ***。"""
    text = "" if value is None else str(value)
    # u2a_ 必须先跑：它保留前缀方便定位，跑在后面会被通用规则连前缀一起吃掉
    text = _KEY_IN_TEXT.sub(lambda match: mask_secret(match.group(0)), text)
    for pattern, repl in _CREDENTIAL_RULES:
        text = pattern.sub(repl, text)
    return text


def mask_access_args(args: Any) -> Any:
    """uvicorn.access 的 record.args 是元组，逐项脱敏以覆盖查询串里的密钥。"""
    if not isinstance(args, tuple):
        return args
    return tuple(mask_text(item) if isinstance(item, str) else item for item in args)


def db_of(request: Request):
    return request.app.state.db.connect()


def log_success_sql() -> str:
    """request_logs 的成功口径：2xx 且没有 error 事件。

    流式中途断流时 HTTP 层已经出了首帧、状态码只能是 200，只能靠 error 列区分。
    """
    return "status < 400 AND COALESCE(error, '') = ''"


def log_failure_sql() -> str:
    """失败口径与 log_success_sql 互补：4xx/5xx，或 2xx 但带 error。"""
    return "status >= 400 OR COALESCE(error, '') <> ''"


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
