"""RFC 6238 TOTP 与恢复码工具（只用标准库，避免为一个验证码引入依赖）。

取舍：
- 密钥用 base32 明文存库，理由是控制面库里本来就明文存上游密钥与会话，
  库文件已 0600；代码里不额外造一套自定义加密算法，避免「自己写密码学」。
- 校验接受当前时间窗及前后各一格，仅用于容忍客户端时钟漂移；
  重放防护由调用方持久化的 last_counter 判定。
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from .security import hash_password

STEP_SEC = 30
DIGITS = 6
# 时间窗容错：-1 / 0 / +1 三格，覆盖常见的客户端时钟偏差
WINDOW = 1
RECOVERY_CODE_COUNT = 8
_ISSUER = "WorkBuddy Manager"


def new_secret() -> str:
    # 160 bit（RFC 4226 推荐长度），base32 去掉填充符便于手输
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _normalize(code: str) -> str:
    return "".join(ch for ch in (code or "").strip() if ch not in " -")


def hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def counter_at(ts: float | None = None) -> int:
    return int((time.time() if ts is None else ts) // STEP_SEC)


def verify(secret: str, code: str, last_counter: int, ts: float | None = None) -> tuple[int | None, str]:
    """返回 (命中的 counter, "ok"|"replay"|"invalid")。

    先按由近及远匹配（当前 → 上一格 → 下一格），命中后再判断是否已用过：
    同窗重复使用与更早窗口的码都会得到 replay，调用方据此回 401。
    """
    text = _normalize(code)
    if not text.isdigit() or len(text) != DIGITS:
        return None, "invalid"
    base = counter_at(ts)
    matched: int | None = None
    for offset in (0, -WINDOW, WINDOW):
        counter = base + offset
        if counter < 0:
            continue
        if hmac.compare_digest(hotp(secret, counter), text):
            matched = counter
            break
    if matched is None:
        return None, "invalid"
    if matched <= last_counter:
        return None, "replay"
    return matched, "ok"


def otpauth_uri(username: str, secret: str) -> str:
    label = quote(f"{_ISSUER}:{username}", safe="")
    params = (
        f"secret={secret}&issuer={quote(_ISSUER, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SEC}"
    )
    return f"otpauth://totp/{label}?{params}"


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    # 12 位十六进制（48 bit 熵），四位一组便于抄写
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(6)
        codes.append("-".join(raw[i:i + 4] for i in range(0, 12, 4)))
    return codes


def hash_recovery_code(code: str) -> str:
    # 恢复码本身有 48 bit 熵，但沿用口令哈希参数，避免两套哈希口径
    return hash_password(normalize_recovery_code(code))


def normalize_recovery_code(code: str) -> str:
    return _normalize(code).lower()
