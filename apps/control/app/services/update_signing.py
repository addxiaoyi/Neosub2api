"""Ed25519 detached 签名：优先用 cryptography，缺失时回落 RFC 8032 纯标准库实现。

运行期只用 verify：发布 manifest 的签名必须能用仓库内置公钥验过，验不过就中止。
签名函数只给 scripts/release_manifest.py 用（那里持有私钥），进程内不参与任何判定。

两条实现产出/接受的签名格式都是 64 字节 `R || S`，公钥都是 32 字节裸公钥，
所以换实现不影响任何已发布签名。cryptography 缺失、版本过低或安装损坏时回落
纯标准库实现，并打日志 + 在更新状态里暴露 `implementation`，不静默切换。

PEM/DER 的读写仍留在标准库：那是对外格式约束（RFC 8410 前缀逐字节校验），
不是密码学运算，交给库反而会改变错误语义。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, P - 2, P) % P
_SQRT_M1 = pow(2, (P - 1) // 4, P)

# Ed25519 SPKI DER 前缀与 PKCS#8 前缀（RFC 8410），按字节比对而不是解析 ASN.1
SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")

Point = tuple[int, int, int, int]  # x, y, z, t（扩展坐标）

# cryptography 41 起 Ed25519 与异常类型才处于持续安全维护的版本带
LIB_MIN_VERSION = (41, 0)

logger = logging.getLogger("unified.update.signing")

_lib_probed = False
_lib_ok = False
_lib_version = ""
_lib_reason = ""
_logged_backend = ""


class SigningError(ValueError):
    """公钥/私钥/签名格式不合法。调用方据此给出可读原因，不静默当成验签失败。"""


class SigningBackendUnavailable(RuntimeError):
    """显式点名要 lib 实现但它不可用；生产路径走 auto，永远不会抛这个。"""


def _probe_lib() -> None:
    """只探测一次：导入失败、版本过低、API 缺失都记下原因供状态接口展示。"""
    global _lib_probed, _lib_ok, _lib_version, _lib_reason
    if _lib_probed:
        return
    _lib_probed = True
    try:
        import cryptography
        from cryptography.exceptions import InvalidSignature  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: F401
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )

        version = str(getattr(cryptography, "__version__", ""))
        _lib_version = version
        match = re.match(r"(\d+)\.(\d+)", version)
        if not match:
            _lib_reason = f"cryptography 版本号无法识别：{version or '(空)'}"
            return
        if (int(match.group(1)), int(match.group(2))) < LIB_MIN_VERSION:
            _lib_reason = (
                f"cryptography {version} 低于最低可用版本 "
                f"{LIB_MIN_VERSION[0]}.{LIB_MIN_VERSION[1]}"
            )
            return
        _lib_ok = True
    except Exception as exc:  # 安装损坏（缺 _rust/缺 cffi）同样要能回落，不能把启动带崩
        _lib_reason = f"导入 cryptography 失败：{type(exc).__name__}: {exc}"


def lib_available() -> bool:
    _probe_lib()
    return _lib_ok


def backend() -> str:
    """返回 `lib` 或 `stdlib`；首次判定时把结果打进日志，避免静默切换。"""
    global _logged_backend
    _probe_lib()
    name = "lib" if _lib_ok else "stdlib"
    if not _logged_backend:
        _logged_backend = name
        if _lib_ok:
            logger.info("manifest 验签使用 cryptography %s（lib 实现）", _lib_version)
        else:
            logger.warning("manifest 验签回落纯标准库实现：%s", _lib_reason)
    return name


def backend_info() -> dict[str, str]:
    """给更新状态用：implementation 只能是 lib/stdlib，reason 说明为什么回落。"""
    name = backend()
    return {
        "implementation": name,
        "library_version": _lib_version if name == "lib" else "",
        "reason": "" if name == "lib" else _lib_reason,
    }


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _inv(x: int) -> int:
    return pow(x, P - 2, P)


def _add(p: Point, q: Point) -> Point:
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = t1 * 2 * _D * t2 % P
    dd = z1 * 2 * z2 % P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(scalar: int, point: Point) -> Point:
    result: Point = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _equal(p: Point, q: Point) -> bool:
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    return x1 * z2 % P == x2 * z1 % P and y1 * z2 % P == y2 * z1 % P


def _compress(point: Point) -> bytes:
    x, y, z, _ = point
    zinv = _inv(z)
    x, y = x * zinv % P, y * zinv % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _base_point() -> Point:
    y = 4 * _inv(5) % P
    x = _sqrt_ratio(y * y - 1, _D * y * y + 1)
    if x & 1:
        x = P - x
    return (x, y, 1, x * y % P)


def _sqrt_ratio(numerator: int, denominator: int) -> int:
    x2 = numerator * _inv(denominator) % P
    x = pow(x2, (P + 3) // 8, P)
    if x * x % P != x2:
        x = x * _SQRT_M1 % P
    if x * x % P != x2:
        raise SigningError("点不在 Ed25519 曲线上")
    return x


def _decompress(raw: bytes) -> Point:
    if len(raw) != 32:
        raise SigningError("Ed25519 点长度必须是 32 字节")
    value = int.from_bytes(raw, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    if y >= P:
        raise SigningError("Ed25519 点 y 坐标越界")
    x = _sqrt_ratio(y * y - 1, _D * y * y + 1) if y != 1 else 0
    if x == 0 and sign:
        raise SigningError("Ed25519 点 x=0 不能带符号位")
    if x & 1 != sign:
        x = P - x
    return (x, y, 1, x * y % P)


def sign(seed: bytes, message: bytes) -> bytes:
    """按当前环境选实现；两条实现产出同一份 RFC 8032 签名格式。"""
    return sign_with(seed, message, backend())


def sign_with(seed: bytes, message: bytes, implementation: str) -> bytes:
    """显式点名实现，只给测试与发布工具排障用；生产入口是 sign。"""
    if implementation == "lib":
        if not lib_available():
            raise SigningBackendUnavailable(f"lib 实现不可用：{_lib_reason}")
        return _sign_lib(seed, message)
    if implementation == "stdlib":
        return _sign_stdlib(seed, message)
    raise ValueError(f"未知的签名实现：{implementation}")


def _sign_lib(seed: bytes, message: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if len(seed) != 32:
        raise SigningError("Ed25519 私钥种子必须是 32 字节")
    return Ed25519PrivateKey.from_private_bytes(seed).sign(message)


def _sign_stdlib(seed: bytes, message: bytes) -> bytes:
    """RFC 8032 §5.1.6：返回 64 字节签名；只给发布工具用。"""
    if len(seed) != 32:
        raise SigningError("Ed25519 私钥种子必须是 32 字节")
    digest = _sha512(seed)
    a = int.from_bytes(digest[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    public = _compress(_mul(a, _base_point()))
    r = int.from_bytes(_sha512(digest[32:] + message), "little") % L
    r_point = _compress(_mul(r, _base_point()))
    h = int.from_bytes(_sha512(r_point + public + message), "little") % L
    return r_point + ((r + h * a) % L).to_bytes(32, "little")


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """按当前环境选实现；两条实现都拒绝 S >= L 的可塑签名。"""
    return verify_with(public_key, message, signature, backend())


def verify_with(
    public_key: bytes, message: bytes, signature: bytes, implementation: str
) -> bool:
    """显式点名实现，只给测试与排障用；生产入口是 verify。"""
    if implementation == "lib":
        if not lib_available():
            raise SigningBackendUnavailable(f"lib 实现不可用：{_lib_reason}")
        return _verify_lib(public_key, message, signature)
    if implementation == "stdlib":
        return _verify_stdlib(public_key, message, signature)
    raise ValueError(f"未知的验签实现：{implementation}")


def _verify_lib(public_key: bytes, message: bytes, signature: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def _verify_stdlib(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 §5.1.7：任何格式问题都返回 False，调用方按「验签失败」处理。"""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        a_point = _decompress(public_key)
        r_point = _decompress(signature[:32])
    except SigningError:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:  # 防可塑性：S 必须落在 [0, L)
        return False
    h = int.from_bytes(_sha512(signature[:32] + public_key + message), "little") % L
    return _equal(_mul(s, _base_point()), _add(r_point, _mul(h, a_point)))


def _pem_body(text: str, label: str) -> bytes:
    header = f"-----BEGIN {label}-----"
    footer = f"-----END {label}-----"
    if header not in text or footer not in text:
        raise SigningError(f"PEM 缺少 {header} / {footer} 头尾")
    body = text.split(header, 1)[1].split(footer, 1)[0]
    try:
        return base64.b64decode("".join(body.split()), validate=True)
    except ValueError as exc:
        raise SigningError(f"PEM 正文不是合法 base64：{exc}") from exc


def load_public_key(text: str) -> bytes:
    """从 PEM 文本取 32 字节公钥；也接受裸 base64 的 32 字节公钥。"""
    if "-----BEGIN" in text:
        der = _pem_body(text, "PUBLIC KEY")
        if len(der) != 44 or not der.startswith(SPKI_PREFIX):
            raise SigningError("不是 Ed25519 公钥（期望 SPKI DER，44 字节）")
        return der[len(SPKI_PREFIX):]
    raw = base64.b64decode("".join(text.split()), validate=True)
    if len(raw) != 32:
        raise SigningError("裸公钥必须是 32 字节的 base64")
    return raw


def load_seed(text: str) -> bytes:
    """从 PKCS#8 PEM 取 32 字节种子；私钥只在发布机构上用，不落仓库。"""
    der = _pem_body(text, "PRIVATE KEY")
    if len(der) != 48 or not der.startswith(PKCS8_PREFIX):
        raise SigningError("不是 Ed25519 私钥（期望 PKCS#8 DER，48 字节）")
    return der[len(PKCS8_PREFIX):]


def public_pem(seed: bytes) -> str:
    """由种子推导公钥 PEM —— 生成 deploy/release-pubkey.pem 时用。"""
    digest = _sha512(seed)
    a = int.from_bytes(digest[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    der = SPKI_PREFIX + _compress(_mul(a, _base_point()))
    body = base64.b64encode(der).decode()
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"


def decode_signature(raw: str | bytes) -> bytes:
    """manifest.sig 是单行 base64，允许尾部换行。"""
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    try:
        data = base64.b64decode("".join(text.split()), validate=True)
    except ValueError as exc:
        raise SigningError(f"签名字符串不是合法 base64：{exc}") from exc
    if len(data) != 64:
        raise SigningError(f"Ed25519 签名必须是 64 字节，实际 {len(data)}")
    return data
