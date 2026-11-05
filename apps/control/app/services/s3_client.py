"""S3 兼容对象存储的最小客户端：httpx + 标准库，自己实现 SigV4。

只实现远端备份需要的动作（put/get/head/list/delete），不引厂商 SDK。
`x-amz-content-sha256` 永远是真算出的摘要，不使用 UNSIGNED-PAYLOAD。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping
from urllib.parse import quote, urlsplit

import httpx
import xml.etree.ElementTree as ET

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SAFE_URI = "-_.~"
_SIGNED_ALWAYS = ("host", "x-amz-content-sha256", "x-amz-date")
_SPACE_RE = re.compile(r"\s+")

# 这些错误重试没有意义：要么凭据/权限错，要么桶不存在，要么时钟偏差
_FATAL_CODES = frozenset(
    {
        "AccessDenied",
        "AuthorizationHeaderMalformed",
        "ExpiredToken",
        "InvalidAccessKeyId",
        "InvalidArgument",
        "InvalidBucketName",
        "InvalidRequest",
        "InvalidToken",
        "NoSuchBucket",
        "RequestTimeTooSkewed",
        "SignatureDoesNotMatch",
        "XAmzContentSHA256Mismatch",
    }
)


class S3Error(Exception):
    """对象存储返回的错误；`fatal` 决定是否停止重试。"""

    def __init__(self, status: int, code: str, message: str, *, fatal: bool) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.fatal = fatal
        super().__init__(f"{status} {code}: {message}")

    def __str__(self) -> str:
        return f"{self.status} {self.code}: {self.message}"


@dataclass(frozen=True)
class S3Config:
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    path_style: bool = True
    session_token: str = ""


@dataclass(frozen=True)
class S3Object:
    key: str
    size: int = 0
    last_modified: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


def _trim(value: str) -> str:
    return _SPACE_RE.sub(" ", value.strip())


def _encode_segment(segment: str) -> str:
    return quote(segment, safe=_SAFE_URI)


def canonical_uri(path: str) -> str:
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    return "/".join(_encode_segment(part) for part in path.split("/"))


def _query_pairs(query: Iterable[tuple[str, object]] | Mapping[str, object] | None) -> list[tuple[str, str]]:
    if not query:
        return []
    items = query.items() if isinstance(query, Mapping) else query
    return [(_encode_segment(str(k)), _encode_segment(str(v))) for k, v in items if v is not None]


def canonical_query(query: Iterable[tuple[str, object]] | Mapping[str, object] | None) -> str:
    pairs = sorted(_query_pairs(query), key=lambda item: (item[0], item[1]))
    return "&".join(f"{key}={value}" for key, value in pairs)


def _signed_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    merged = {name.lower(): _trim(str(value)) for name, value in headers.items()}
    names = sorted(merged)
    block = "\n".join(f"{name}:{merged[name]}" for name in names)
    return block, ";".join(names)


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    # SigV4 的密钥派生第一环是把字面量 "AWS4" 拼在密钥前面，漏了会得到完全不同的签名
    key = _hmac(b"AWS4" + secret_key.encode(), date.encode())
    key = _hmac(key, region.encode())
    key = _hmac(key, service.encode())
    return _hmac(key, b"aws4_request")


def sign_headers(
    *,
    method: str,
    host: str,
    path: str,
    query: Iterable[tuple[str, object]] | Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    amz_date: str,
    service: str = "s3",
) -> dict[str, str]:
    """返回带 Authorization 的完整请求头（键统一小写）。

    传入的每一个头都会参与签名——测试里就是这么对官方向量逐字比对的。
    """
    supplied = dict(headers or {})
    supplied["host"] = host
    supplied["x-amz-date"] = amz_date
    supplied["x-amz-content-sha256"] = payload_hash
    block, signed = _signed_headers(supplied)
    canonical = "\n".join(
        [
            method.upper(),
            canonical_uri(path),
            canonical_query(query),
            block,
            "",
            signed,
            payload_hash,
        ]
    )
    date = amz_date[:8]
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    signature = hmac.new(
        signing_key(secret_key, date, region, service), to_sign.encode(), hashlib.sha256
    ).hexdigest()
    result = {name.lower(): value for name, value in supplied.items()}
    result["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope},"
        f"SignedHeaders={signed},Signature={signature}"
    )
    return result


def _xml_error(body: bytes) -> tuple[str, str]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "", ""
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    code = root.findtext("Code") or ""
    message = root.findtext("Message") or ""
    return code.strip(), message.strip()


def _classify(status: int, code: str, message: str) -> S3Error:
    if code == "RequestTimeTooSkewed":
        return S3Error(
            status,
            code,
            "宿主时钟偏差超过 15 分钟：请同步系统时间后重试（RequestTimeTooSkewed）",
            fatal=True,
        )
    fatal = code in _FATAL_CODES
    if not code:
        fatal = status in {400, 401, 403, 404, 405, 501}
    else:
        fatal = fatal or status in {400, 401, 403}
    if status == 429 or status >= 500:
        fatal = False
    return S3Error(status, code or f"HTTP{status}", message or f"HTTP {status}", fatal=fatal)


class S3Client:
    def __init__(self, config: S3Config, *, timeout: float = 30.0, transport: httpx.BaseTransport | None = None) -> None:
        parts = urlsplit(config.endpoint.strip())
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("endpoint 必须是 http(s)://host[:port]")
        self._config = config
        self._scheme = parts.scheme
        self._endpoint_host = parts.netloc
        self._base_path = parts.path.rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "S3Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _host(self) -> str:
        if self._config.path_style:
            return self._endpoint_host
        return f"{self._config.bucket}.{self._endpoint_host}"

    def _path(self, key: str | None) -> str:
        prefix = self._base_path
        if self._config.path_style:
            prefix = f"{prefix}/{self._config.bucket}"
        if key:
            prefix = f"{prefix}/{key}"
        return prefix or "/"

    def _url(self, path: str, query: Iterable[tuple[str, object]] | Mapping[str, object] | None) -> str:
        encoded_path = canonical_uri(path)
        qs = canonical_query(query)
        return f"{self._scheme}://{self._host()}{encoded_path}" + (f"?{qs}" if qs else "")

    def _send(
        self,
        method: str,
        *,
        key: str | None = None,
        query: Iterable[tuple[str, object]] | Mapping[str, object] | None = None,
        body: bytes = b"",
        metadata: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        headers: dict[str, str] = {}
        if self._config.session_token:
            # 临时凭据必须参与签名，否则 STS 会话直接被拒
            headers["x-amz-security-token"] = self._config.session_token
        for name, value in (metadata or {}).items():
            headers[f"x-amz-meta-{name}".lower()] = value
        amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self._path(key)
        signed = sign_headers(
            method=method,
            host=self._host(),
            path=path,
            query=query,
            headers=headers,
            payload_hash=payload_hash,
            access_key=self._config.access_key,
            secret_key=self._config.secret_key,
            region=self._config.region,
            amz_date=amz_date,
        )
        try:
            response = self._client.request(method, self._url(path, query), headers=signed, content=body)
        except httpx.HTTPError as exc:
            # 网络/超时归为可重试，交给上层按 30s/2m/10m 退避
            raise S3Error(0, "NetworkError", f"{type(exc).__name__}: {exc}", fatal=False) from exc
        if response.status_code < 300:
            return response
        code, message = _xml_error(response.content)
        raise _classify(response.status_code, code, message)

    def put_object(self, key: str, body: bytes, metadata: Mapping[str, str] | None = None) -> dict[str, str]:
        response = self._send("PUT", key=key, body=body, metadata=metadata)
        etag = response.headers.get("etag", "").strip('"')
        return {"etag": etag}

    def get_object(self, key: str) -> bytes:
        return self._send("GET", key=key).content

    def head_object(self, key: str) -> S3Object:
        response = self._send("HEAD", key=key)
        size = int(response.headers.get("content-length", "0") or 0)
        meta = {
            name.lower()[len("x-amz-meta-") :]: value
            for name, value in response.headers.items()
            if name.lower().startswith("x-amz-meta-")
        }
        return S3Object(key=key, size=size, last_modified=response.headers.get("last-modified", ""), metadata=meta)

    def delete_object(self, key: str) -> None:
        self._send("DELETE", key=key)

    def list_objects_v2(self, prefix: str = "", *, max_keys: int = 1000) -> list[S3Object]:
        found: list[S3Object] = []
        token: str | None = None
        while True:
            query: list[tuple[str, object]] = [("list-type", "2"), ("prefix", prefix), ("max-keys", max_keys)]
            if token:
                query.append(("continuation-token", token))
            root = ET.fromstring(self._send("GET", query=query).content)
            # 各家实现都会带默认命名空间，统一去掉前缀再按本地名找节点
            for node in root.iter():
                node.tag = node.tag.rsplit("}", 1)[-1]
            for item in root.findall("Contents"):
                found.append(
                    S3Object(
                        key=(item.findtext("Key") or "").strip(),
                        size=int(item.findtext("Size") or 0),
                        last_modified=(item.findtext("LastModified") or "").strip(),
                    )
                )
            truncated = (root.findtext("IsTruncated") or "").lower() == "true"
            token = root.findtext("NextContinuationToken")
            if not truncated or not token:
                break
        return found
