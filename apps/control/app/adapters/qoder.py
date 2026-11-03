"""Qoder 适配器（clean-room 重写）。

协议契约见 docs/providers/qoder.md：COSY 签名头 + 信封 SSE，对外提供 OpenAI /
Anthropic / Responses 三套协议。加密原语（自定义 base64 / AES-128-CBC / RSA
PKCS#1 v1.5）全部用标准库自写，不引入三方依赖，也不复制上游实现。

未在真机验证的部分（请求骨架的业务字段子集）在 health 与文档里明确标注。
"""

import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
from typing import Any, AsyncIterator

from ..config import Settings
from .base import ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient

# ---------- 区域矩阵 ----------

REGIONS: dict[str, dict[str, Any]] = {
    "cn": {
        "website": "https://qoder.com.cn",
        "openapi": "https://openapi.qoder.com.cn",
        "gateway": "https://gateway.qoder.com.cn",
        "fallbacks": [],
        "client_id": "1c5e33e1-364d-4ce6-b02c-acaa81274a5c",
        "user_agent": "QoderWork/1.1.64",
        "workbuddy": "https://copilot.tencent.com",
    },
    "global": {
        "website": "https://qoder.com",
        "openapi": "https://openapi.qoder.sh",
        "gateway": "https://api1.qoder.sh",
        "fallbacks": ["https://api2.qoder.sh", "https://api3.qoder.sh"],
        "client_id": "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb",
        "user_agent": "Qoder/1.1.64",
        "workbuddy": "https://www.workbuddy.ai",
    },
}

PRODUCTS = ("qoder", "workbuddy", "trae")

EP_CHAT = "/algo/api/v2/service/pro/sse/agent_chat_generation?Encode=1"
EP_MODELS = "/algo/api/v2/model/list?Encode=1"
WB_CHAT = "/v2/chat/completions"

COSY_VERSION = "1.1.64"
DEFAULT_USER_TYPE = "personal_professional_trial"
COSY_CLIENT_IP = "169.254.198.161"
MAX_TOKENS_LIMIT = 131_072

CUSTOM_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
PAD_CHAR = "$"

SERVER_PUB_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""

# 会话失效 / 鉴权被拒 / 硬性额度：一律按 429 交给调用方退避，不做无意义重试
SESSION_DEAD_CODES = {12153}
SESSION_DEAD_WORDS = ("SessionDead", "AuthRejected", "HardCredit")

# 允许换 host 的错误类别
FALLBACK_STATUS = {429, 500, 502, 503, 504}
FALLBACK_WORDS = ("model_gate", "model_rate", "model_blocked", "server", "timeout", "not_found")


# ---------- 自定义 base64 ----------


def qoder_encode(plain: bytes) -> str:
    """标准 base64 → 三段轮转 → 自定义字母表（`=` 换成 `$`）。"""
    std = base64.b64encode(plain).decode()
    n = len(std)
    cut = n // 3
    rotated = std[n - cut:] + std[cut:n - cut] + std[:cut]
    table = {ch: CUSTOM_ALPHABET[i] for i, ch in enumerate(STD_ALPHABET)}
    return "".join(PAD_CHAR if ch == "=" else table.get(ch, ch) for ch in rotated)


def qoder_decode(encoded: str) -> bytes:
    """`qoder_encode` 的逆运算（测试与排障用）。"""
    table = {ch: STD_ALPHABET[i] for i, ch in enumerate(CUSTOM_ALPHABET)}
    std = "".join("=" if ch == PAD_CHAR else table.get(ch, ch) for ch in encoded)
    n = len(std)
    cut = n // 3
    head, rest = std[:cut], std[cut:]
    middle, tail = rest[: n - 2 * cut], rest[n - 2 * cut:]
    return base64.b64decode(tail + middle + head)


def sorted_compact(values: dict[str, Any]) -> bytes:
    """键按字节序排序的紧凑 JSON；null 归一成空串（服务端按此字节做签名）。"""
    out = []
    for key in sorted(values, key=lambda k: k.encode()):
        value = values[key]
        text = '""' if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        out.append(json.dumps(key, ensure_ascii=False) + ":" + text)
    return ("{" + ",".join(out) + "}").encode()


def strip_algo_prefix(path: str) -> str:
    # 与官方客户端逐字对齐：只要以 /algo 开头就剥 5 个字符
    return path[5:] if path.startswith("/algo") else path


def cosy_signature(payload_b64: str, cosy_key: str, date: str, body: str, path: str) -> str:
    raw = "\n".join([payload_b64, cosy_key, date, body, path])
    return hashlib.md5(raw.encode()).hexdigest()


# ---------- 纯 Python AES-128-CBC ----------

_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _gf_mul(a: int, b: int) -> int:
    product = 0
    for _ in range(8):
        if b & 1:
            product ^= a
        a = ((a << 1) & 0xFF) ^ (0x1B if a & 0x80 else 0)
        b >>= 1
    return product


def _gf_inv(a: int) -> int:
    """GF(2^8) 乘法逆元：a^254。"""
    result, base, exp = 1, a, 254
    while exp:
        if exp & 1:
            result = _gf_mul(result, base)
        base = _gf_mul(base, base)
        exp >>= 1
    return result


def _build_sbox() -> tuple[int, ...]:
    box = []
    for i in range(256):
        inv = 0 if i == 0 else _gf_inv(i)
        rotated = inv
        acc = inv
        for _ in range(4):
            rotated = ((rotated << 1) | (rotated >> 7)) & 0xFF
            acc ^= rotated
        box.append(acc ^ 0x63)
    return tuple(box)


SBOX = _build_sbox()


def _round_keys(key: bytes) -> list[list[int]]:
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    return [sum(words[r * 4:r * 4 + 4], []) for r in range(11)]


def _encrypt_block(block: bytes, keys: list[list[int]]) -> bytes:
    state = list(block)

    def add_round(round_key: list[int]) -> None:
        for i in range(16):
            state[i] ^= round_key[i]

    add_round(keys[0])
    for rnd in range(1, 11):
        for i in range(16):
            state[i] = SBOX[state[i]]
        shifted = [0] * 16
        for col in range(4):
            for row in range(4):
                shifted[col * 4 + row] = state[((col + row) % 4) * 4 + row]
        state = shifted
        if rnd < 10:
            for col in range(4):
                a0, a1, a2, a3 = state[col * 4:col * 4 + 4]
                state[col * 4] = _gf_mul(a0, 2) ^ _gf_mul(a1, 3) ^ a2 ^ a3
                state[col * 4 + 1] = a0 ^ _gf_mul(a1, 2) ^ _gf_mul(a2, 3) ^ a3
                state[col * 4 + 2] = a0 ^ a1 ^ _gf_mul(a2, 2) ^ _gf_mul(a3, 3)
                state[col * 4 + 3] = _gf_mul(a0, 3) ^ a1 ^ a2 ^ _gf_mul(a3, 2)
        add_round(keys[rnd])
    return bytes(state)


def aes_cbc_encrypt(plain: bytes, key: bytes) -> bytes:
    """AES-128-CBC + PKCS7（Qoder 的 info 体 key == iv == temp_key）。"""
    if len(key) != 16:
        raise ValueError("AES-128 需要 16 字节密钥")
    padding = 16 - len(plain) % 16
    data = plain + bytes([padding]) * padding
    keys = _round_keys(key)
    prev = key
    out = bytearray()
    for offset in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[offset:offset + 16], prev))
        prev = _encrypt_block(block, keys)
        out.extend(prev)
    return bytes(out)


# ---------- RSA PKCS#1 v1.5 ----------


def _der_length(data: bytes, index: int) -> tuple[int, int]:
    first = data[index]
    index += 1
    if first & 0x80:
        count = first & 0x7F
        value = int.from_bytes(data[index:index + count], "big")
        return value, index + count
    return first, index


def parse_rsa_public_key(pem: str) -> tuple[int, int]:
    """从 PEM 公钥里取 (n, e)；只支持 RSA 的 SPKI/PKCS#1 两种包装。"""
    lines = [line for line in pem.strip().splitlines() if "-----" not in line]
    der = base64.b64decode("".join(lines))

    def read_integer(pos: int) -> tuple[int, int]:
        if der[pos] != 0x02:
            raise ValueError("期望 INTEGER")
        size, pos = _der_length(der, pos + 1)
        return int.from_bytes(der[pos:pos + size], "big"), pos + size

    def read_sequence(pos: int) -> tuple[int, int]:
        if der[pos] != 0x30:
            raise ValueError("期望 SEQUENCE")
        size, body = _der_length(der, pos + 1)
        return body, body + size

    pos, _ = read_sequence(0)
    if der[pos] == 0x30:
        # SPKI：AlgorithmIdentifier 之后是 BIT STRING 包裹的 RSAPublicKey
        _, pos = read_sequence(pos)
        if der[pos] != 0x03:
            raise ValueError("期望 BIT STRING")
        size, pos = _der_length(der, pos + 1)
        pos += 1  # unused bits
        pos, _ = read_sequence(pos)
    modulus, pos = read_integer(pos)
    exponent, _ = read_integer(pos)
    return modulus, exponent


def rsa_pkcs1v15_encrypt(plain: bytes, pem: str) -> bytes:
    modulus, exponent = parse_rsa_public_key(pem)
    size = (modulus.bit_length() + 7) // 8
    padding_len = size - len(plain) - 3
    if padding_len < 8:
        raise ValueError("明文过长，RSA PKCS#1 v1.5 无法填充")
    filler = bytearray()
    while len(filler) < padding_len:
        filler.extend(os.urandom(padding_len - len(filler)).replace(b"\x00", b"\x01"))
    block = b"\x00\x02" + bytes(filler[:padding_len]) + b"\x00" + plain
    return pow(int.from_bytes(block, "big"), exponent, modulus).to_bytes(size, "big")


# ---------- 设备指纹 ----------


def derive_id(uid: str, salt: str) -> str:
    return hashlib.md5(f"{salt}:{uid}".encode()).hexdigest()


def machine_identity(uid: str) -> dict[str, str]:
    digest = hashlib.sha512(f"machinetoken:{uid}".encode()).digest()
    token = base64.urlsafe_b64encode(digest).decode().rstrip("=")[:43]
    return {
        "machine_id": derive_id(uid, "machine"),
        "machine_token": token,
        "machine_type": derive_id(uid, "machinetype")[:18],
        "session_id": derive_id(uid, "session"),
    }


class _Reply:
    def __init__(
        self,
        status_code: int,
        *,
        body: bytes | None = None,
        stream: AsyncIterator[bytes] | None = None,
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = {"content-type": content_type}
        self._stream = stream

    async def iter_bytes(self):
        if self._stream is None:
            return
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        self._stream = None


async def _sse(raw: AsyncIterator[bytes]) -> AsyncIterator[tuple[str, str]]:
    buffer = ""
    event = ""
    data: list[str] = []
    async for chunk in raw:
        buffer += chunk.decode("utf-8", "ignore")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                if event or data:
                    yield event, "\n".join(data)
                event, data = "", []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
    if event or data:
        yield event, "\n".join(data)


def _unwrap(line: str) -> dict[str, Any] | None:
    """剥信封：`{"body":"<内层 JSON 或 [DONE]>","statusCodeValue":200}`。"""
    try:
        outer = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(outer, dict):
        return None
    inner = outer.get("body")
    if isinstance(inner, dict):
        return inner
    if isinstance(inner, str):
        text = inner.strip()
        if not text or text == "[DONE]":
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return outer


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in ("text", "input_text", "output_text"):
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


class QoderAdapter(ProviderAdapter):
    spec = ProviderSpec(
        id="qoder",
        name="Qoder（COSY）",
        vendor="Qoder（协议参考）",
        license="本仓自研（MIT）",
        runtime="主仓内置（直连 COSY 网关）",
        status="implemented",
        description="COSY 签名网关：OpenAI / Anthropic / Responses 三套协议转换，多产品线 × 多地域路由与 fallback。",
        default_base_url=REGIONS["cn"]["gateway"],
        capabilities=stub_capabilities(chat=True, messages=True, models=True, responses=True),
        protocols=("openai.chat.completions", "anthropic.messages", "openai.responses"),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        self._sessions: dict[str, dict[str, Any]] = {}
        self._cooldown: dict[str, float] = {}
        super().__init__(base_url)

    # ---------- 路由 ----------

    def _region(self, model: str) -> tuple[str, str]:
        """模型前缀 `cn:` / `global:` 定区域；没前缀按 options.region（默认 cn）。"""
        for code in ("global", "cn"):
            if model.startswith(code + ":"):
                return code, model[len(code) + 1:]
        configured = str(self.options.get("region") or "cn").strip().lower()
        return (configured if configured in REGIONS else "cn"), model

    def _product(self, model: str) -> tuple[str, str]:
        for name in PRODUCTS:
            if model.startswith(name + "/"):
                return name, model[len(name) + 1:]
        configured = str(self.options.get("product") or "qoder").strip().lower()
        return (configured if configured in PRODUCTS else "qoder"), model

    def _hosts(self, region: str) -> list[str]:
        info = REGIONS[region]
        override = str(self.options.get("gateway_base") or "").rstrip("/")
        primary = override or str(info["gateway"])
        if override:
            return [primary]
        return [primary, *info["fallbacks"]]

    def _proxy(self, region: str) -> str:
        """地域代理可选：先看按地域的映射，再看全局 proxy；都没有就直连。"""
        proxies = self.options.get("region_proxies")
        if isinstance(proxies, dict) and proxies.get(region):
            return str(proxies[region])
        return str(self.options.get("proxy") or "")

    def _direct_note(self, region: str) -> str:
        if self._proxy(region):
            return ""
        return f"（未配置 {region} 地域代理，已直连；若为地域限制导致，请配置 options.proxy 或 region_proxies.{region}）"

    # ---------- 身份 ----------

    def _token(self) -> str:
        raw = self.api_key.strip()
        return raw[7:].strip() if raw.lower().startswith("bearer ") else raw

    def _uid(self) -> str:
        configured = str(self.options.get("uid") or "").strip()
        if configured:
            return configured
        token = self._token()
        return hashlib.sha256(token.encode()).hexdigest()[:16] if token else ""

    def _session(self) -> dict[str, Any]:
        token = self._token()
        if not token:
            raise ProviderError(401, "Qoder 未配置 access token（服务商页 api_key）")
        uid = self._uid()
        cached = self._sessions.get(uid)
        if cached and cached["access"] == token:
            return cached
        temp_key = uuid.uuid4().hex[:16].encode()
        cosy_key = base64.b64encode(rsa_pkcs1v15_encrypt(temp_key, SERVER_PUB_PEM)).decode()
        identity = {
            "name": str(self.options.get("nickname") or ""),
            "aid": uid,
            "uid": uid,
            "yx_uid": "",
            "organization_id": str(self.options.get("organization_id") or ""),
            "organization_name": str(self.options.get("organization_name") or ""),
            "user_type": str(self.options.get("user_type") or DEFAULT_USER_TYPE),
            "security_oauth_token": token,
            "refresh_token": str(self.options.get("refresh_token") or ""),
        }
        info = base64.b64encode(aes_cbc_encrypt(sorted_compact(identity), temp_key)).decode()
        session = {"access": token, "uid": uid, "cosy_key": cosy_key, "info": info, **machine_identity(uid)}
        self._sessions[uid] = session
        return session

    def _headers(self, region: str, body: str, path: str, *, model_key: str = "", sse: bool = True) -> dict[str, str]:
        session = self._session()
        payload = base64.b64encode(
            sorted_compact(
                {
                    "cosyVersion": COSY_VERSION,
                    "ideVersion": "",
                    "info": session["info"],
                    "requestId": str(uuid.uuid4()),
                    "version": "v1",
                }
            )
        ).decode()
        date = str(int(time.time()))
        sig = cosy_signature(payload, session["cosy_key"], date, body, strip_algo_prefix(path))
        headers = {
            "cosy-data-policy": "AGREE",
            "content-type": "application/json",
            "cosy-machinetype": session["machine_type"],
            "cosy-clienttype": "5",
            "cosy-date": date,
            "cosy-user": session["uid"],
            "cosy-key": session["cosy_key"],
            "accept": "text/event-stream" if sse else "application/json",
            "cosy-clientip": COSY_CLIENT_IP,
            "authorization": f"Bearer COSY.{payload}.{sig}",
            "accept-encoding": "identity",
            "cosy-version": COSY_VERSION,
            "cosy-machineid": session["machine_id"],
            "cosy-machinetoken": session["machine_token"],
            "login-version": "v2",
            "user-agent": str(self.options.get("user_agent") or REGIONS[region]["user_agent"]),
        }
        if sse:
            headers["cache-control"] = "no-cache"
        if model_key:
            headers["x-model-key"] = model_key
            headers["x-model-source"] = "system"
        return headers

    # ---------- 错误分类 ----------

    def _classify(self, status: int | None, text: str) -> ProviderError:
        code = None
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            obj = None
        if isinstance(obj, dict):
            for key in ("code", "error_code", "statusCodeValue"):
                if isinstance(obj.get(key), int):
                    code = obj[key]
                    break
        joined = f"{code or ''} {text}"
        if status == 401 or code in SESSION_DEAD_CODES or any(word in joined for word in SESSION_DEAD_WORDS):
            err = ProviderError(429, f"Qoder 会话已失效或鉴权被拒（需重新登录刷新 token）：{text[:160]}")
            err.session_dead = True
        elif status == 429:
            retry_after = self._retry_after(text)
            note = f"，Retry-After={retry_after}s" if retry_after else ""
            err = ProviderError(429, f"Qoder 触发限流{note}：{text[:160]}")
            err.retry_after = retry_after
        elif status == 404:
            err = ProviderError(404, f"Qoder 上游端点不存在：{text[:160]}")
        elif status is not None and status >= 500:
            err = ProviderError(502, f"Qoder 上游服务异常（{status}）：{text[:160]}（可换 fallback host）")
        elif status == 400:
            err = ProviderError(400, f"Qoder 请求被拒：{text[:160]}")
        else:
            err = ProviderError(status or 400, f"Qoder 上游错误：{text[:160]}")
        return err.with_vendor_detail(text)

    @staticmethod
    def _retry_after(text: str) -> int:
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return 0
        if not isinstance(obj, dict):
            return 0
        for key in ("retry_after", "retryAfter", "retry_after_seconds"):
            value = obj.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        return 0

    def _cooling(self, key: str) -> int:
        until = self._cooldown.get(key, 0.0)
        remain = int(until - time.time())
        return max(0, remain)

    # ---------- 上游 ----------

    def _chat_body(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        for item in payload.get("messages") or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            if role == "developer":
                role = "system"
            message: dict[str, Any] = {"role": role, "content": _content_text(item.get("content"))}
            if isinstance(item.get("tool_calls"), list):
                message["tool_calls"] = item["tool_calls"]
            if item.get("tool_call_id"):
                message["tool_call_id"] = item["tool_call_id"]
            messages.append(message)
        if not messages:
            raise ProviderError(400, "Qoder 请求缺少 messages")

        max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
        parameters: dict[str, Any] = {
            "max_tokens": int(max(1, min(int(max_tokens or 8192), MAX_TOKENS_LIMIT))),
            "temperature": payload.get("temperature", 0.7),
        }
        if payload.get("top_p") is not None:
            parameters["top_p"] = payload["top_p"]
        request_id = str(uuid.uuid4())
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        prompt = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return {
            "agent_id": "agent_common",
            "chat_task": "FREE_INPUT",
            "session_type": "qodercli",
            "session_id": str(uuid.uuid4()),
            "request_id": request_id,
            "chat_record_id": request_id,
            "request_set_id": str(uuid.uuid4()),
            "stream": True,
            "aliyun_user_type": str(self.options.get("user_type") or DEFAULT_USER_TYPE),
            "model_config": {
                "key": model,
                "display_name": model,
                "model": "",
                "format": "openai",
                "is_vl": False,
                "is_reasoning": False,
                "api_key": "",
                "url": "",
                "source": "system",
                "max_input_tokens": 0,
            },
            "messages": messages,
            "system": system,
            "chat_context": {
                "text": {"text": prompt},
                "extra": {"originalContent": {"text": prompt}, "modelConfig": {"key": model}},
            },
            "parameters": parameters,
            "tools": payload.get("tools") or [],
            "business": {
                "id": str(uuid.uuid4()),
                "begin_at": int(time.time() * 1000),
                "name": (prompt[:30] or "chat"),
                "product": "cli",
                "type": "agent",
                "stage": "start",
            },
        }

    async def _workbuddy(self, payload: dict[str, Any], region: str, model: str):
        host = str(self.options.get("workbuddy_base") or REGIONS[region]["workbuddy"])
        stream = bool(payload.get("stream"))
        body = dict(payload)
        body["model"] = model
        client = UnifiedApiClient(
            host,
            api_key=self._token(),
            timeout=float(self.options.get("timeout") or 300.0),
            proxy=self._proxy(region),
        )
        reply = await client.forward(WB_CHAT, body, stream=stream, accept="text/event-stream" if stream else "application/json")
        if reply.status_code >= 400:
            text = reply.body.decode("utf-8", "ignore") if reply.body else ""
            if reply.response is not None:
                raw = await reply.response.aread()
                await reply.aclose()
                text = raw.decode("utf-8", "ignore")
            err = self._classify(reply.status_code, text)
            err.message = f"{err.message} {self._direct_note(region)}".strip()
            raise err
        return _Reply(reply.status_code, body=reply.body, stream=reply.iter_bytes(), content_type=reply.headers.get("content-type", "application/json"))

    async def _open_upstream(self, model: str, region: str, payload: dict[str, Any]):
        """返回 (内层 SSE 生成器, host) 或抛 ProviderError；按 fallback 链换 host。"""
        body = self._chat_body(payload, model)
        encoded = qoder_encode(json.dumps(body, ensure_ascii=False).encode())
        last: ProviderError | None = None
        for host in self._hosts(region):
            headers = self._headers(region, encoded, EP_CHAT, model_key=model, sse=True)
            client = UnifiedApiClient(
                host,
                timeout=float(self.options.get("timeout") or 300.0),
                proxy=self._proxy(region),
                extra_headers=headers,
            )
            try:
                reply = await client.forward(EP_CHAT, raw=encoded.encode(), stream=True)
            except ProviderError as exc:
                last = ProviderError(
                    502,
                    f"{exc.message}{self._direct_note(region)}",
                    upstream_code=exc.upstream_code,
                    upstream_message=exc.upstream_message,
                )
                continue
            if reply.status_code >= 400:
                raw = await reply.response.aread()
                await reply.aclose()
                text = raw.decode("utf-8", "ignore")
                err = self._classify(reply.status_code, text)
                last = err
                if reply.status_code in FALLBACK_STATUS and not getattr(err, "session_dead", False):
                    if any(word in text for word in FALLBACK_WORDS) or reply.status_code >= 500:
                        continue
                    continue
                raise err
            return reply, host
        if last is not None and last.status_code == 429:
            # 429 冷却：按 Retry-After（缺省 30s）记冷却窗口，避免继续锤上游
            wait = int(getattr(last, "retry_after", 0) or 30)
            self._cooldown[f"{region}:{self._uid()}"] = time.time() + wait
        raise last or ProviderError(502, "Qoder 上游全部 host 不可用")

    async def _chunks(self, reply) -> AsyncIterator[dict[str, Any]]:
        try:
            async for _, data in _sse(reply.iter_bytes()):
                if not data:
                    continue
                obj = _unwrap(data)
                if obj:
                    yield obj
        finally:
            await reply.aclose()

    async def _collect(self, reply, model: str) -> dict[str, Any]:
        content: list[str] = []
        reasoning: list[str] = []
        slots: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] | None = None
        finish = "stop"
        async for obj in self._chunks(reply):
            if obj.get("usage"):
                usage = obj["usage"]
            for choice in obj.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason"):
                    finish = str(choice["finish_reason"])
                delta = choice.get("delta") or {}
                if not isinstance(delta, dict):
                    continue
                if isinstance(delta.get("content"), str):
                    content.append(delta["content"])
                if isinstance(delta.get("reasoning_content"), str):
                    reasoning.append(delta["reasoning_content"])
                for call in delta.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    idx = int(call.get("index") or 0)
                    slot = slots.setdefault(idx, {"index": idx, "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    fn = call.get("function") or {}
                    if call.get("id"):
                        slot["id"] = call["id"]
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if slots:
            message["tool_calls"] = [slots[i] for i in sorted(slots)]
        result: dict[str, Any] = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        }
        if usage:
            result["usage"] = usage
        return result

    # ---------- 对外：OpenAI chat ----------

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        region, model = self._region(str(payload.get("model") or ""))
        product, model = self._product(model)
        if not model:
            raise ProviderError(400, "Qoder 请求缺少 model")
        if product == "trae":
            raise ProviderError(501, "Qoder 的 trae 产品线不走 COSY，请改用 provider `trae`（Solo/Work 通道）")
        cool = self._cooling(f"{region}:{self._uid()}")
        if cool:
            err = ProviderError(429, f"Qoder 限流冷却中，{cool}s 后重试")
            err.retry_after = cool
            raise err
        if product == "workbuddy":
            return await self._workbuddy(payload, region, model)

        reply, host = await self._open_upstream(model, region, payload)
        if stream:
            return _Reply(200, stream=self._render_chat(reply, model), content_type="text/event-stream")
        data = await self._collect(reply, model)
        data["model"] = model
        return _Reply(200, body=json.dumps(data, ensure_ascii=False).encode())

    async def _render_chat(self, reply, model: str) -> AsyncIterator[bytes]:
        cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
        try:
            async for obj in self._chunks(reply):
                obj.setdefault("id", cmpl_id)
                obj.setdefault("model", model)
                obj.setdefault("created", int(time.time()))
                yield b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"
        finally:
            await reply.aclose()
        yield b"data: [DONE]\n\n"

    # ---------- 对外：Anthropic messages ----------

    @staticmethod
    def _anthropic_to_openai(payload: dict[str, Any]) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        system = payload.get("system")
        if system:
            messages.append({"role": "system", "content": _content_text(system)})
        for item in payload.get("messages") or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            messages.append({"role": role, "content": _content_text(item.get("content"))})
        out: dict[str, Any] = {
            "model": payload.get("model"),
            "messages": messages,
            "stream": bool(payload.get("stream")),
        }
        if payload.get("max_tokens") is not None:
            out["max_tokens"] = payload["max_tokens"]
        if payload.get("temperature") is not None:
            out["temperature"] = payload["temperature"]
        if payload.get("stop_sequences"):
            out["stop"] = payload["stop_sequences"]
        return out

    async def messages(self, payload: dict[str, Any], *, stream: bool):
        openai_payload = self._anthropic_to_openai({**payload, "stream": stream})
        region, model = self._region(str(openai_payload.get("model") or ""))
        product, model = self._product(model)
        if product != "qoder":
            raise ProviderError(501, f"Qoder 的 {product} 产品线不支持 Anthropic 协议")
        reply, _ = await self._open_upstream(model, region, openai_payload)
        if stream:
            return _Reply(200, stream=self._render_anthropic(reply, model), content_type="text/event-stream")
        data = await self._collect(reply, model)
        text = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage") or {}
        body = {
            "id": f"msg_{uuid.uuid4().hex[:20]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}] if text else [],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            },
        }
        return _Reply(200, body=json.dumps(body, ensure_ascii=False).encode())

    async def _render_anthropic(self, reply, model: str) -> AsyncIterator[bytes]:
        msg_id = f"msg_{uuid.uuid4().hex[:20]}"

        def event(name: str, data: dict[str, Any]) -> bytes:
            return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

        yield event("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id, "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })
        yield event("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        usage: dict[str, Any] = {}
        try:
            async for obj in self._chunks(reply):
                if obj.get("usage"):
                    usage = obj["usage"]
                for choice in obj.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") or {}
                    text = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(text, str) and text:
                        yield event("content_block_delta", {
                            "type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": text},
                        })
        finally:
            await reply.aclose()
        yield event("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            },
        })
        yield event("message_stop", {"type": "message_stop"})

    # ---------- 对外：OpenAI Responses ----------

    @staticmethod
    def _responses_to_openai(payload: dict[str, Any]) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        instructions = payload.get("instructions")
        if instructions:
            messages.append({"role": "system", "content": _content_text(instructions)})
        source = payload.get("input")
        if isinstance(source, str):
            messages.append({"role": "user", "content": source})
        elif isinstance(source, list):
            for item in source:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    role = str(item.get("role") or "user")
                    messages.append({"role": role, "content": _content_text(item.get("content"))})
        out: dict[str, Any] = {
            "model": payload.get("model"),
            "messages": messages,
            "stream": bool(payload.get("stream")),
        }
        limit = payload.get("max_output_tokens")
        if isinstance(limit, int) and limit > 0:
            out["max_tokens"] = limit
        if payload.get("temperature") is not None:
            out["temperature"] = payload["temperature"]
        return out

    async def responses(self, payload: dict[str, Any], *, stream: bool):
        openai_payload = self._responses_to_openai({**payload, "stream": stream})
        if not openai_payload["messages"]:
            raise ProviderError(400, "Responses 请求缺少 input")
        region, model = self._region(str(openai_payload.get("model") or ""))
        product, model = self._product(model)
        if product != "qoder":
            raise ProviderError(501, f"Qoder 的 {product} 产品线不支持 Responses 协议")
        reply, _ = await self._open_upstream(model, region, openai_payload)
        if stream:
            return _Reply(200, stream=self._render_responses(reply, model), content_type="text/event-stream")
        data = await self._collect(reply, model)
        text = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage") or {}
        body = {
            "id": f"resp_{uuid.uuid4().hex[:20]}",
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": model,
            "output": [{
                "id": f"msg_{uuid.uuid4().hex[:20]}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }],
            "usage": {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
        }
        return _Reply(200, body=json.dumps(body, ensure_ascii=False).encode())

    async def _render_responses(self, reply, model: str) -> AsyncIterator[bytes]:
        resp_id = f"resp_{uuid.uuid4().hex[:20]}"
        item_id = f"msg_{uuid.uuid4().hex[:20]}"

        def event(name: str, data: dict[str, Any]) -> bytes:
            return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

        yield event("response.created", {
            "type": "response.created",
            "response": {
                "id": resp_id, "object": "response", "status": "in_progress",
                "model": model, "output": [],
            },
        })
        yield event("response.output_item.added", {
            "type": "response.output_item.added", "output_index": 0,
            "item": {"id": item_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []},
        })
        usage: dict[str, Any] = {}
        full: list[str] = []
        try:
            async for obj in self._chunks(reply):
                if obj.get("usage"):
                    usage = obj["usage"]
                for choice in obj.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") or {}
                    text = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(text, str) and text:
                        full.append(text)
                        yield event("response.output_text.delta", {
                            "type": "response.output_text.delta", "item_id": item_id,
                            "output_index": 0, "content_index": 0, "delta": text,
                        })
        finally:
            await reply.aclose()
        yield event("response.output_text.done", {
            "type": "response.output_text.done", "item_id": item_id,
            "output_index": 0, "content_index": 0, "text": "".join(full),
        })
        yield event("response.completed", {
            "type": "response.completed",
            "response": {
                "id": resp_id, "object": "response", "status": "completed", "model": model,
                "output": [{
                    "id": item_id, "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "".join(full), "annotations": []}],
                }],
                "usage": {
                    "input_tokens": int(usage.get("prompt_tokens") or 0),
                    "output_tokens": int(usage.get("completion_tokens") or 0),
                    "total_tokens": int(usage.get("total_tokens") or 0),
                },
            },
        })

    # ---------- 清单与健康 ----------

    async def fetch_models(self) -> list[dict[str, Any]]:
        token = self._token()
        if not token:
            raise ProviderError(401, "Qoder 未配置 access token（服务商页 api_key 或 options.extra_keys）")
        region = str(self.options.get("region") or "cn")
        models: list[dict[str, Any]] = []
        last_error: ProviderError | None = None
        reached = False
        for host in self._hosts(region):
            encoded = qoder_encode(b"{}")
            headers = self._headers(region, encoded, EP_MODELS, sse=False)
            client = UnifiedApiClient(
                host, timeout=30.0, proxy=self._proxy(region), extra_headers=headers,
            )
            try:
                reply = await client.forward(EP_MODELS, raw=encoded.encode(), stream=False, method="GET")
            except ProviderError as exc:
                last_error = exc
                continue
            if reply.status_code >= 400:
                last_error = ProviderError(reply.status_code, f"Qoder 模型目录返回 {reply.status_code}")
                continue
            reached = True
            try:
                outer = json.loads(reply.body or b"{}")
            except json.JSONDecodeError as exc:
                last_error = ProviderError(502, f"Qoder 模型目录返回非 JSON：{exc}")
                continue
            entries = outer.get("body") if isinstance(outer, dict) else None
            if isinstance(entries, str):
                try:
                    entries = json.loads(entries)
                except json.JSONDecodeError:
                    entries = None
            rows = entries if isinstance(entries, list) else (outer.get("data") if isinstance(outer, dict) else None)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, str):
                    models.append({"id": row})
                elif isinstance(row, dict):
                    key = row.get("key") or row.get("model") or row.get("name")
                    if key:
                        models.append({"id": str(key)})
            if models:
                break
        return models

    async def health(self) -> dict[str, Any]:
        if not self._token():
            return {"connected": False, "error": "未配置 access token"}
        region = str(self.options.get("region") or "cn")
        proxy = self._proxy(region)
        try:
            models = await self.list_models()
        except ProviderError as exc:
            return {"connected": False, "error": exc.message, "region": region}
        return {
            "connected": bool(models),
            "region": region,
            "product": str(self.options.get("product") or "qoder"),
            "proxy": proxy or "direct",
            "models": len(models),
            "note": "" if proxy else f"{region} 未配置地域代理，直连",
        }


def build(settings: Settings, base_url: str = "") -> QoderAdapter:
    return QoderAdapter(settings, base_url)
