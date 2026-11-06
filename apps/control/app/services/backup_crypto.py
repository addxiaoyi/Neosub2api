"""备份归档的客户端加密：AES-256-GCM，优先用 cryptography，缺失时回落纯标准库。

两条实现互操作：文件格式固定为 `nonce(12) || ciphertext || tag(16)`，AAD 是常量
`unified-2api-backup-v1`，所以装不装 cryptography 都不改变对外格式。

优先级取舍：装了合格版本的 cryptography（见 LIB_MIN_VERSION）就用它（OpenSSL
后端，快一到两个数量级、不承担自研实现的侧信道风险）；没装/版本过低/安装损坏
就回落本文件的纯标准库实现，并打日志 + 在状态接口里暴露 `implementation`，
不静默切换。自研实现按 NIST SP 800-38D 写，T 表 + 16 位查表 GHASH 加速。

nonce 策略：每次加密独立 `os.urandom(12)`，绝不复用、绝不从计数器推导。
96 位随机 nonce 在单密钥下累计 q 条消息的碰撞概率约 q²/2^97（q=2^32 时
约 2^-33），配合「一个密钥只用于本部署的备份归档」这一使用边界足够安全；
禁止在同一 nonce 上重复加密同一条数据（GCM 下这会直接泄露明文异或）。

密钥只来自环境变量，绝不落库、绝不返回给前端。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
import struct
from pathlib import Path

KEY_ENV = "UNIFIED_BACKUP_ENCRYPTION_KEY"
AAD = b"unified-2api-backup-v1"
NONCE_BYTES = 12
TAG_BYTES = 16
# 分块大小必须是 16 的整数倍：CTR 计数器要跨块连续，末尾不足一块的单独处理
CHUNK_BYTES = 65536
# 低于这个版本不做 lib 加速：Ed25519/GCM 流式 API 稳定之外，老版本已停止安全维护
LIB_MIN_VERSION = (41, 0)

logger = logging.getLogger("unified.backup.crypto")

_lib_probed = False
_lib_ok = False
_lib_version = ""
_lib_reason = ""
_logged_backend = ""


class CryptoKeyMissing(Exception):
    """环境变量没配：调用方必须据此拒绝上传，绝不退回明文。"""


class CryptoKeyInvalid(Exception):
    """base64 解不出 32 字节：同样拒绝上传，不猜、不截断。"""


class CryptoVerifyError(Exception):
    """GCM tag 不匹配：密文被改过或密钥不对。"""


class CryptoBackendUnavailable(RuntimeError):
    """显式点名要 lib 实现但它不可用；生产路径走 auto，永远不会抛这个。"""


def _probe_lib() -> None:
    """只探测一次：导入失败、版本过低、API 缺失都记下原因供状态接口展示。"""
    global _lib_probed, _lib_ok, _lib_version, _lib_reason
    if _lib_probed:
        return
    _lib_probed = True
    try:
        import cryptography
        from cryptography.exceptions import InvalidTag  # noqa: F401
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: F401

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
            logger.info("备份加密使用 cryptography %s（lib 实现）", _lib_version)
        else:
            logger.warning("备份加密回落纯标准库实现：%s", _lib_reason)
    return name


def backend_info() -> dict[str, str]:
    """给状态接口用：implementation 只能是 lib/stdlib，reason 说明为什么回落。"""
    name = backend()
    return {
        "implementation": name,
        "library_version": _lib_version if name == "lib" else "",
        "reason": "" if name == "lib" else _lib_reason,
    }


_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)

_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40)


def _xtime(value: int) -> int:
    return ((value << 1) ^ 0x1B) & 0xFF if value & 0x80 else (value << 1) & 0xFF


def _build_tables() -> tuple[list[int], list[int], list[int], list[int]]:
    t0: list[int] = []
    for s in _SBOX:
        t0.append((_xtime(s) << 24) | (s << 16) | (s << 8) | (s ^ _xtime(s)))
    rot = lambda word, bits: ((word >> bits) | (word << (32 - bits))) & 0xFFFFFFFF  # noqa: E731
    return t0, [rot(w, 8) for w in t0], [rot(w, 16) for w in t0], [rot(w, 24) for w in t0]


_T0, _T1, _T2, _T3 = _build_tables()

# GF(2^128) 的约简常量：GHASH 的多项式是 x^128 + x^7 + x^2 + x + 1
_REDUCE = 0xE1000000000000000000000000000000


def _gf_mult(x: int, y: int) -> int:
    """GHASH 的 128 位乘法（bit 127 是最高次项），与 SP 800-38D 的定义一致。"""
    z = 0
    v = y
    for i in range(128):
        if x & (1 << (127 - i)):
            z ^= v
        v = ((v >> 1) ^ _REDUCE) if v & 1 else (v >> 1)
    return z


_TABLE_CACHE: dict[bytes, list[list[int]]] = {}


def _ghash_table(h: bytes) -> list[list[int]]:
    """按字节位置预计算 16×256 项：每块 16 次查表代替 128 次位运算。

    线性性保证了 X*H = XOR(byte_p*2^(8*(15-p))*H)，逐字节查表即可。
    """
    cached = _TABLE_CACHE.get(h)
    if cached is not None:
        return cached
    hj = int.from_bytes(h, "big")
    table = [
        [_gf_mult(value << (8 * (15 - pos)), hj) for value in range(256)]
        for pos in range(16)
    ]
    _TABLE_CACHE[h] = table
    return table


def _ghash_blocks(state: int, data: bytes, table: list[list[int]]) -> int:
    for offset in range(0, len(data) - len(data) % 16, 16):
        x = state ^ int.from_bytes(data[offset : offset + 16], "big")
        acc = 0
        for pos in range(16):
            acc ^= table[pos][(x >> (8 * (15 - pos))) & 0xFF]
        state = acc
    return state


class _AES256:
    """AES-256 只有加密方向：GCM 全程用正向分组函数，不需要逆 S 盒。"""

    __slots__ = ("_round_keys",)

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-256 需要 32 字节密钥")
        self._round_keys = self._expand(key)

    @staticmethod
    def _expand(key: bytes) -> list[int]:
        words = [int.from_bytes(key[i : i + 4], "big") for i in range(0, 32, 4)]
        for i in range(8, 60):
            temp = words[i - 1]
            if i % 8 == 0:
                temp = ((temp << 8) | (temp >> 24)) & 0xFFFFFFFF  # RotWord
                temp = (
                    (_SBOX[(temp >> 24) & 0xFF] << 24)
                    | (_SBOX[(temp >> 16) & 0xFF] << 16)
                    | (_SBOX[(temp >> 8) & 0xFF] << 8)
                    | _SBOX[temp & 0xFF]
                ) ^ (_RCON[i // 8] << 24)
            elif i % 8 == 4:
                temp = (
                    (_SBOX[(temp >> 24) & 0xFF] << 24)
                    | (_SBOX[(temp >> 16) & 0xFF] << 16)
                    | (_SBOX[(temp >> 8) & 0xFF] << 8)
                    | _SBOX[temp & 0xFF]
                )
            words.append(words[i - 8] ^ temp)
        return words

    def encrypt_block(self, block: bytes) -> bytes:
        rk = self._round_keys
        s0 = int.from_bytes(block[0:4], "big") ^ rk[0]
        s1 = int.from_bytes(block[4:8], "big") ^ rk[1]
        s2 = int.from_bytes(block[8:12], "big") ^ rk[2]
        s3 = int.from_bytes(block[12:16], "big") ^ rk[3]
        for rnd in range(1, 14):
            base = rnd * 4
            t0 = _T0[s0 >> 24] ^ _T1[(s1 >> 16) & 0xFF] ^ _T2[(s2 >> 8) & 0xFF] ^ _T3[s3 & 0xFF] ^ rk[base]
            t1 = _T0[s1 >> 24] ^ _T1[(s2 >> 16) & 0xFF] ^ _T2[(s3 >> 8) & 0xFF] ^ _T3[s0 & 0xFF] ^ rk[base + 1]
            t2 = _T0[s2 >> 24] ^ _T1[(s3 >> 16) & 0xFF] ^ _T2[(s0 >> 8) & 0xFF] ^ _T3[s1 & 0xFF] ^ rk[base + 2]
            t3 = _T0[s3 >> 24] ^ _T1[(s0 >> 16) & 0xFF] ^ _T2[(s1 >> 8) & 0xFF] ^ _T3[s2 & 0xFF] ^ rk[base + 3]
            s0, s1, s2, s3 = t0, t1, t2, t3
        base = 56
        o0 = (
            (_SBOX[s0 >> 24] << 24)
            | (_SBOX[(s1 >> 16) & 0xFF] << 16)
            | (_SBOX[(s2 >> 8) & 0xFF] << 8)
            | _SBOX[s3 & 0xFF]
        ) ^ rk[base]
        o1 = (
            (_SBOX[s1 >> 24] << 24)
            | (_SBOX[(s2 >> 16) & 0xFF] << 16)
            | (_SBOX[(s3 >> 8) & 0xFF] << 8)
            | _SBOX[s0 & 0xFF]
        ) ^ rk[base + 1]
        o2 = (
            (_SBOX[s2 >> 24] << 24)
            | (_SBOX[(s3 >> 16) & 0xFF] << 16)
            | (_SBOX[(s0 >> 8) & 0xFF] << 8)
            | _SBOX[s1 & 0xFF]
        ) ^ rk[base + 2]
        o3 = (
            (_SBOX[s3 >> 24] << 24)
            | (_SBOX[(s0 >> 16) & 0xFF] << 16)
            | (_SBOX[(s1 >> 8) & 0xFF] << 8)
            | _SBOX[s2 & 0xFF]
        ) ^ rk[base + 3]
        return struct.pack(">IIII", o0, o1, o2, o3)


def _counter_block(nonce: bytes, counter: int) -> bytes:
    return nonce + struct.pack(">I", counter & 0xFFFFFFFF)


def _xor_stream(data: bytes, keystream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, keystream))


def _keystream(aes: _AES256, nonce: bytes, counter: int, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        block = aes.encrypt_block(_counter_block(nonce, counter))
        out.extend(block)
        counter = (counter + 1) & 0xFFFFFFFF
    return bytes(out[:size])


def _ghash_padded(state: int, data: bytes, table: list[list[int]]) -> int:
    """任意长度输入：整块直接算，末尾不足 16 字节按补零规则参与 GHASH。"""
    full = len(data) - len(data) % 16
    state = _ghash_blocks(state, data[:full], table)
    tail = data[full:]
    if tail:
        state = _ghash_blocks(state, tail + bytes(16 - len(tail)), table)
    return state


def encrypt_file(source: Path, target: Path, key: bytes) -> dict[str, object]:
    """按当前环境选实现：装了合格的 cryptography 走 lib，否则走纯标准库。"""
    return encrypt_file_with(source, target, key, backend())


def encrypt_file_with(
    source: Path, target: Path, key: bytes, implementation: str
) -> dict[str, object]:
    """显式点名实现，只给测试与故障排查用；生产入口是 encrypt_file。"""
    if implementation == "lib":
        if not lib_available():
            raise CryptoBackendUnavailable(f"lib 实现不可用：{_lib_reason}")
        return _encrypt_file_lib(source, target, key)
    if implementation == "stdlib":
        return _encrypt_file_stdlib(source, target, key)
    raise ValueError(f"未知的加密实现：{implementation}")


def _encrypt_file_stdlib(source: Path, target: Path, key: bytes) -> dict[str, object]:
    """流式读原文、写 `nonce || ciphertext || tag`；返回密文摘要与大小。

    nonce 每次 `os.urandom(12)`（策略见模块 docstring），不复用、不从计数器推导。
    """
    aes = _AES256(key)
    table = _ghash_table(aes.encrypt_block(bytes(16)))
    nonce = os.urandom(NONCE_BYTES)
    state = _ghash_padded(0, AAD, table)
    counter = 2  # J0 = nonce||1，GCM 的第一个计数器是 J0+1
    total = 0
    tail = b""
    digest = hashlib.sha256()
    with source.open("rb") as src, target.open("wb") as dst:
        dst.write(nonce)
        digest.update(nonce)
        while True:
            chunk = src.read(CHUNK_BYTES)
            if not chunk:
                break
            padded = tail + chunk
            body_len = len(padded) - len(padded) % 16
            head, tail = padded[:body_len], padded[body_len:]
            if body_len:
                encrypted = _xor_stream(head, _keystream(aes, nonce, counter, body_len))
                counter = (counter + body_len // 16) & 0xFFFFFFFF
                state = _ghash_blocks(state, encrypted, table)
                dst.write(encrypted)
                digest.update(encrypted)
                total += body_len
        if tail:
            encrypted = _xor_stream(tail, _keystream(aes, nonce, counter, len(tail)))
            state = _ghash_padded(state, encrypted, table)
            dst.write(encrypted)
            digest.update(encrypted)
            total += len(tail)
        state ^= int.from_bytes(struct.pack(">QQ", len(AAD) * 8, total * 8), "big")
        acc = 0
        for pos in range(16):
            acc ^= table[pos][(state >> (8 * (15 - pos))) & 0xFF]
        mask = aes.encrypt_block(_counter_block(nonce, 1))
        final = bytes(
            a ^ b
            for a, b in zip(
                struct.pack(">Q", acc >> 64) + struct.pack(">Q", acc & 0xFFFFFFFFFFFFFFFF), mask
            )
        )
        dst.write(final)
        digest.update(final)
    return {
        "sha256": digest.hexdigest(),
        "size_bytes": total + NONCE_BYTES + TAG_BYTES,
        "nonce": nonce,
    }


def _encrypt_file_lib(source: Path, target: Path, key: bytes) -> dict[str, object]:
    """OpenSSL 后端的流式 GCM：分块 update，tag 由库生成，格式与标准库实现一致。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(key) != 32:
        raise ValueError("AES-256 需要 32 字节密钥")
    nonce = os.urandom(NONCE_BYTES)  # 每条消息一个新 nonce，策略见模块 docstring
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(AAD)
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as src, target.open("wb") as dst:
        dst.write(nonce)
        digest.update(nonce)
        while True:
            chunk = src.read(CHUNK_BYTES)
            if not chunk:
                break
            encrypted = encryptor.update(chunk)
            dst.write(encrypted)
            digest.update(encrypted)
            total += len(chunk)
        encryptor.finalize()
        tag = encryptor.tag
        dst.write(tag)
        digest.update(tag)
    return {
        "sha256": digest.hexdigest(),
        "size_bytes": total + NONCE_BYTES + TAG_BYTES,
        "nonce": nonce,
    }


def decrypt_bytes(payload: bytes, key: bytes) -> bytes:
    """解密并校验 tag；只用于回读校验与测试，未接入任何恢复路径。"""
    return decrypt_bytes_with(payload, key, backend())


def decrypt_bytes_with(payload: bytes, key: bytes, implementation: str) -> bytes:
    """显式点名实现，供互通性测试与被工具复用。"""
    if implementation == "lib":
        if not lib_available():
            raise CryptoBackendUnavailable(f"lib 实现不可用：{_lib_reason}")
        return _decrypt_bytes_lib(payload, key)
    if implementation == "stdlib":
        return _decrypt_bytes_stdlib(payload, key)
    raise ValueError(f"未知的解密实现：{implementation}")


def _decrypt_bytes_lib(payload: bytes, key: bytes) -> bytes:
    """GCM 解密：tag 交给库校验，篡改/错密钥都会抛出统一的 CryptoVerifyError。"""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(key) != 32:
        raise ValueError("AES-256 需要 32 字节密钥")
    if len(payload) < NONCE_BYTES + TAG_BYTES:
        raise CryptoVerifyError("密文长度不足，缺少 nonce 或 tag")
    nonce = payload[:NONCE_BYTES]
    ciphertext = payload[NONCE_BYTES:-TAG_BYTES]
    tag = payload[-TAG_BYTES:]
    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
    decryptor.authenticate_additional_data(AAD)
    try:
        return decryptor.update(ciphertext) + decryptor.finalize()
    except InvalidTag as exc:
        raise CryptoVerifyError("GCM 校验失败：密钥不对或密文被篡改") from exc


def _decrypt_bytes_stdlib(payload: bytes, key: bytes) -> bytes:
    """纯标准库 GCM 解密，行为与 lib 实现逐字节等价。"""
    if len(payload) < NONCE_BYTES + TAG_BYTES:
        raise CryptoVerifyError("密文长度不足，缺少 nonce 或 tag")
    nonce = payload[:NONCE_BYTES]
    ciphertext = payload[NONCE_BYTES:-TAG_BYTES]
    tag = payload[-TAG_BYTES:]
    aes = _AES256(key)
    table = _ghash_table(aes.encrypt_block(bytes(16)))
    state = _ghash_padded(0, AAD, table)
    state = _ghash_padded(state, ciphertext, table)
    state ^= int.from_bytes(struct.pack(">QQ", len(AAD) * 8, len(ciphertext) * 8), "big")
    acc = 0
    for pos in range(16):
        acc ^= table[pos][(state >> (8 * (15 - pos))) & 0xFF]
    mask = aes.encrypt_block(_counter_block(nonce, 1))
    expected = bytes(
        a ^ b
        for a, b in zip(
            struct.pack(">Q", acc >> 64) + struct.pack(">Q", acc & 0xFFFFFFFFFFFFFFFF), mask
        )
    )
    if not hmac.compare_digest(expected, tag):
        raise CryptoVerifyError("GCM 校验失败：密钥不对或密文被篡改")
    return _xor_stream(ciphertext, _keystream(aes, nonce, 2, len(ciphertext)))


# ---------------------------------------------------------------- 字段级封装
# 本地归档里的上游密钥（settings.provider_config、provider_credentials.secret）
# 以 `u2a-enc:v1:` 前缀 + 同一套 AES-256-GCM 形式落库：nonce(12) || ciphertext || tag(16)。
# AAD 用独立常量，和归档文件加密分开，避免同一密钥在两个用途之间互相拼接密文。

SEAL_PREFIX = "u2a-enc:v1:"
SECRET_AAD = b"unified-2api-provider-secret-v1"


def is_sealed(value: object) -> bool:
    return isinstance(value, str) and value.startswith(SEAL_PREFIX)


def seal_text(text: str, key: bytes, *, implementation: str | None = None) -> str:
    """把单个密钥字段封装成可落库的字符串；明文为空时原样返回，不产出无意义密文。"""
    payload = _seal_bytes_with(
        text.encode("utf-8"), key, SECRET_AAD, implementation or backend()
    )
    return SEAL_PREFIX + base64.b64encode(payload).decode("ascii")


def unseal_text(token: str, key: bytes, *, implementation: str | None = None) -> str:
    """还原 `seal_text` 的结果；格式不对抛 ValueError，tag 不对抛 CryptoVerifyError。"""
    if not is_sealed(token):
        raise ValueError("不是本工具封装的字段（缺少 u2a-enc:v1: 前缀）")
    try:
        payload = base64.b64decode(token[len(SEAL_PREFIX):], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"封装字段不是合法 base64：{exc}") from exc
    plain = _unseal_bytes_with(payload, key, SECRET_AAD, implementation or backend())
    return plain.decode("utf-8")


def _seal_bytes_with(data: bytes, key: bytes, aad: bytes, implementation: str) -> bytes:
    if implementation == "lib":
        if not lib_available():
            raise CryptoBackendUnavailable(f"lib 实现不可用：{_lib_reason}")
        return _seal_bytes_lib(data, key, aad)
    if implementation == "stdlib":
        return _seal_bytes_stdlib(data, key, aad)
    raise ValueError(f"未知的加密实现：{implementation}")


def _unseal_bytes_with(payload: bytes, key: bytes, aad: bytes, implementation: str) -> bytes:
    if implementation == "lib":
        if not lib_available():
            raise CryptoBackendUnavailable(f"lib 实现不可用：{_lib_reason}")
        return _unseal_bytes_lib(payload, key, aad)
    if implementation == "stdlib":
        return _unseal_bytes_stdlib(payload, key, aad)
    raise ValueError(f"未知的解密实现：{implementation}")


def _seal_bytes_lib(data: bytes, key: bytes, aad: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(key) != 32:
        raise ValueError("AES-256 需要 32 字节密钥")
    nonce = os.urandom(NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(aad)
    ciphertext = encryptor.update(data) + encryptor.finalize()
    return nonce + ciphertext + encryptor.tag


def _unseal_bytes_lib(payload: bytes, key: bytes, aad: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(key) != 32:
        raise ValueError("AES-256 需要 32 字节密钥")
    if len(payload) < NONCE_BYTES + TAG_BYTES:
        raise CryptoVerifyError("密文长度不足，缺少 nonce 或 tag")
    nonce = payload[:NONCE_BYTES]
    ciphertext = payload[NONCE_BYTES:-TAG_BYTES]
    tag = payload[-TAG_BYTES:]
    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
    decryptor.authenticate_additional_data(aad)
    try:
        return decryptor.update(ciphertext) + decryptor.finalize()
    except InvalidTag as exc:
        raise CryptoVerifyError("GCM 校验失败：密钥不对或密文被篡改") from exc


def _seal_bytes_stdlib(data: bytes, key: bytes, aad: bytes) -> bytes:
    """与 `_decrypt_bytes_stdlib` 的校验顺序逐字节对齐：GHASH(AAD) → GHASH(CT) → 长度块。"""
    aes = _AES256(key)
    table = _ghash_table(aes.encrypt_block(bytes(16)))
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = _xor_stream(data, _keystream(aes, nonce, 2, len(data)))
    state = _ghash_padded(0, aad, table)
    state = _ghash_padded(state, ciphertext, table)
    state ^= int.from_bytes(struct.pack(">QQ", len(aad) * 8, len(data) * 8), "big")
    acc = 0
    for pos in range(16):
        acc ^= table[pos][(state >> (8 * (15 - pos))) & 0xFF]
    mask = aes.encrypt_block(_counter_block(nonce, 1))
    tag = bytes(
        a ^ b
        for a, b in zip(
            struct.pack(">QQ", acc >> 64, acc & 0xFFFFFFFFFFFFFFFF), mask
        )
    )
    return nonce + ciphertext + tag


def _unseal_bytes_stdlib(payload: bytes, key: bytes, aad: bytes) -> bytes:
    if len(payload) < NONCE_BYTES + TAG_BYTES:
        raise CryptoVerifyError("密文长度不足，缺少 nonce 或 tag")
    nonce = payload[:NONCE_BYTES]
    ciphertext = payload[NONCE_BYTES:-TAG_BYTES]
    tag = payload[-TAG_BYTES:]
    aes = _AES256(key)
    table = _ghash_table(aes.encrypt_block(bytes(16)))
    state = _ghash_padded(0, aad, table)
    state = _ghash_padded(state, ciphertext, table)
    state ^= int.from_bytes(struct.pack(">QQ", len(aad) * 8, len(ciphertext) * 8), "big")
    acc = 0
    for pos in range(16):
        acc ^= table[pos][(state >> (8 * (15 - pos))) & 0xFF]
    mask = aes.encrypt_block(_counter_block(nonce, 1))
    expected = bytes(
        a ^ b
        for a, b in zip(
            struct.pack(">QQ", acc >> 64, acc & 0xFFFFFFFFFFFFFFFF), mask
        )
    )
    if not hmac.compare_digest(expected, tag):
        raise CryptoVerifyError("GCM 校验失败：密钥不对或密文被篡改")
    return _xor_stream(ciphertext, _keystream(aes, nonce, 2, len(ciphertext)))


def load_key(environ: dict[str, str] | None = None) -> bytes:
    """从环境变量读取并严格校验；没有/格式错都抛异常，由调用方拒绝上传。"""
    raw = (environ if environ is not None else os.environ).get(KEY_ENV, "")
    text = (raw or "").strip()
    if not text:
        raise CryptoKeyMissing(f"缺少环境变量 {KEY_ENV}（32 字节 base64），拒绝以明文上传备份")
    try:
        key = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CryptoKeyInvalid(f"{KEY_ENV} 不是合法 base64：{exc}") from exc
    if len(key) != 32:
        raise CryptoKeyInvalid(f"{KEY_ENV} 解码后必须是 32 字节，实际 {len(key)} 字节")
    return key


def key_configured(environ: dict[str, str] | None = None) -> bool:
    """只为界面展示：格式坏也算未配置，避免「看起来有密钥但上传必失败」。"""
    try:
        load_key(environ)
    except (CryptoKeyMissing, CryptoKeyInvalid):
        return False
    return True
