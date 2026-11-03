"""上游地址守卫：默认拒绝环回 / 私网 / 链路本地 / 云元数据地址（S-04）。

管理面能改 provider.base_url 和 sub2 账号的 upstream_base，网关会带着凭证去请求，
只校验 "http(s)://" 前缀就等于把 SSRF 交给管理员表单。

两类例外：
- provider 的 spec.default_base_url 本身指向 loopback（本地 sidecar / 管理端），
  这家只额外放行 loopback，其余私网、链路本地仍拦截。白名单由 spec 推导，
  新增 sidecar 只要默认地址是 loopback 就自动生效，不用改这份守卫。
- 内网部署（如宿主机私有网段）仍靠 UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1 全局放开；
  这是另一个更危险的开关，语义保持原样：跳过所有 IP 段检查。

云元数据地址是唯一的绝对红线：无论 provider 是否允许 loopback、
UNIFIED_ALLOW_PRIVATE_UPSTREAMS 是否开启，都始终拒绝。
"""

import ipaddress
import socket
from urllib.parse import urlparse

# loopback 主机名：只认 localhost，localhost.localdomain 交给解析结果判断
LOOPBACK_HOSTNAMES = {"localhost"}
# 云元数据主机名：先于解析拦截，避免依赖 DNS 也避免被 allow_private 放行
METADATA_HOSTNAMES = {"metadata.google.internal"}
# 兼容 /api/security/guard 的对外清单：需要人工注意的主机名
BLOCKED_HOSTNAMES = LOOPBACK_HOSTNAMES | METADATA_HOSTNAMES | {"localhost.localdomain"}
# 云厂商元数据：169.254.169.254（AWS/GCP/Azure）与 fd00:ec2::254（AWS IPv6）
BLOCKED_ADDRESSES = (
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
)


class UpstreamAddressError(ValueError):
    """上游地址未通过守卫。"""


def _normalize(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """把 IPv4-mapped IPv6（::ffff:127.0.0.1）折回 IPv4，避免绕过 loopback/私网判断。"""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _private_like(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _parse_host(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return _normalize(ipaddress.ip_address(host))
    except ValueError:
        return None


def is_loopback_url(url: str) -> bool:
    """判断给定 URL 的主机是否本身就是 loopback（用于从 spec 推导放行名单）。

    只做字面判断，不解析 DNS：spec 默认地址都是写死的 127.0.0.1 / [::1] / localhost。
    127.0.0.0/8 整段视为 loopback：该段由 RFC 6890 保留给环回，不可能路由出本机，
    只认 127.0.0.1 并不能多挡任何东西，反而会让绑定 127.0.0.2 的 sidecar 误伤。
    """
    host = (urlparse((url or "").strip()).hostname or "").strip().lower()
    if host in LOOPBACK_HOSTNAMES:
        return True
    addr = _parse_host(host)
    return bool(addr and addr.is_loopback)


def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UpstreamAddressError(f"无法解析上游主机 {host}：{exc}") from exc
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            out.append(_normalize(ipaddress.ip_address(info[4][0])))
        except ValueError:
            continue
    return out


def _provider_label(provider: str) -> str:
    return f"provider「{provider}」" if provider else "当前调用"


def _loopback_error(field: str, addr: str, provider: str) -> UpstreamAddressError:
    who = _provider_label(provider)
    return UpstreamAddressError(
        f"{field} 指向本机环回地址 {addr}，但{who}未开放本地地址"
        "（只有 spec.default_base_url 本身是 loopback 的 provider 才允许 loopback）。"
        "请改填该上游可访问的公网或局域网地址；如果目标确实是本地 sidecar，"
        "请使用对应的 sidecar provider 并确认其默认地址是 loopback，"
        "或显式设置 UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1 全局放开（会同时放开内网地址，风险更高）"
    )


def _private_error(
    field: str,
    addr: str,
    provider: str,
    *,
    allow_loopback: bool,
) -> UpstreamAddressError:
    who = _provider_label(provider)
    scope = "只放行 loopback 一种本地地址" if allow_loopback else "不允许任何本地或内网地址"
    return UpstreamAddressError(
        f"{field} 指向内网 / 链路本地 / 保留地址 {addr}，{who}{scope}"
        "（10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、169.254.0.0/16 等继续拦截）。"
        "请改用 loopback 或公网地址；确需连内网时显式设置 UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1"
    )


def _metadata_error(field: str, target: str) -> UpstreamAddressError:
    return UpstreamAddressError(
        f"{field} 不能指向云元数据地址 {target}：无论 provider 是否允许本地地址、"
        "UNIFIED_ALLOW_PRIVATE_UPSTREAMS 是否开启，元数据地址始终拒绝"
    )


def guard_upstream_url(
    url: str,
    *,
    allow_private: bool = False,
    allow_loopback: bool = False,
    provider: str = "",
    field: str = "base_url",
) -> str:
    """校验并规范化上游地址；空串表示"不设置"，直接放行。

    allow_private 是全局开关（原语义不变，优先于 allow_loopback）；
    allow_loopback 只应针对 spec.default_base_url 为 loopback 的 provider 打开，
    且只放行 loopback，不放行其它私网段。
    """
    cleaned = (url or "").strip().rstrip("/")
    if not cleaned:
        return cleaned
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise UpstreamAddressError(f"{field} 必须以 http:// 或 https:// 开头")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise UpstreamAddressError(f"{field} 缺少主机名")

    # 元数据主机名/字面量先拦，allow_private 也不能放行
    if host in METADATA_HOSTNAMES:
        raise _metadata_error(field, host)
    host_addr = _parse_host(host)
    if host_addr is not None and host_addr in BLOCKED_ADDRESSES:
        raise _metadata_error(field, str(host_addr))

    # 全局放开：保持原行为，跳过所有 IP 段检查（元数据已在上面拦下）
    if allow_private:
        return cleaned

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = _resolve(host, port)
    if not addresses:
        raise UpstreamAddressError(f"{field} 主机无法解析：{host}")
    for addr in addresses:
        if addr in BLOCKED_ADDRESSES:
            raise _metadata_error(field, str(addr))
        if addr.is_loopback:
            if allow_loopback:
                continue
            raise _loopback_error(field, str(addr), provider)
        if _private_like(addr):
            raise _private_error(field, str(addr), provider, allow_loopback=allow_loopback)
    return cleaned
