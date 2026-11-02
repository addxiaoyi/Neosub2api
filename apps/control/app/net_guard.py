"""上游地址守卫：拒绝环回 / 私网 / 链路本地 / 云元数据地址（S-04）。

管理面能改 provider.base_url 和 sub2 账号的 upstream_base，网关会带着凭证去请求，
只校验 "http(s)://" 前缀就等于把 SSRF 交给管理员表单。默认拒绝内网目标，
内网部署（如宿主机 sidecar）靠 UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1 显式放开。
"""

import ipaddress
import socket
from urllib.parse import urlparse

BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}
# 云厂商元数据：169.254.169.254（AWS/GCP/Azure）与 fd00:ec2::254（AWS IPv6）
BLOCKED_ADDRESSES = (
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
)


class UpstreamAddressError(ValueError):
    """上游地址未通过守卫。"""


def _blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        addr in BLOCKED_ADDRESSES
        or addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UpstreamAddressError(f"无法解析上游主机 {host}：{exc}") from exc
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return out


def guard_upstream_url(url: str, *, allow_private: bool = False, field: str = "base_url") -> str:
    """校验并规范化上游地址；空串表示"不设置"，直接放行。"""
    cleaned = (url or "").strip().rstrip("/")
    if not cleaned:
        return cleaned
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise UpstreamAddressError(f"{field} 必须以 http:// 或 https:// 开头")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise UpstreamAddressError(f"{field} 缺少主机名")
    if allow_private:
        return cleaned
    if host in BLOCKED_HOSTNAMES:
        raise UpstreamAddressError(f"{field} 不能指向本机或元数据地址：{host}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = _resolve(host, port)
    if not addresses:
        raise UpstreamAddressError(f"{field} 主机无法解析：{host}")
    for addr in addresses:
        if _blocked(addr):
            raise UpstreamAddressError(f"{field} 不能指向内网 / 环回 / 链路本地地址：{addr}")
    return cleaned
