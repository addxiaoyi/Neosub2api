"""四家 sidecar 的 HTTP 存活探测（只判链路，不碰协议业务）。

只发一次 `GET /v1/models`（四家都声明了该端点）：超时、拒绝连接、鉴权拒绝、
端口被别的进程占用，各自给出不同结论，供 /api/providers 的就绪状态与前端提示使用。
结果缓存数秒，页面刷新不会每次都打满四个 sidecar。
"""

from __future__ import annotations

import asyncio
import os
import ssl
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

PROBE_PATH = "/v1/models"
PROBE_TIMEOUT_SEC = float(os.getenv("UNIFIED_SIDECAR_PROBE_TIMEOUT", "1.5"))
CACHE_TTL_SEC = 5.0

DEFAULT_PORTS = {
    "trae_sidecar": 7871,
    "deepseek_sidecar": 5001,
    "qoder_sidecar": 7873,
    "codar_sidecar": 8801,
}

# 进程内缓存：pid -> (状态, 单调时钟时刻)
_cache: dict[str, tuple["SidecarStatus", float]] = {}


@dataclass(frozen=True)
class SidecarStatus:
    pid: str
    state: str  # alive | auth | timeout | refused | http_error
    message: str
    port: int
    base_url: str
    path: str
    http_status: int | None = None
    latency_ms: int | None = None
    checked_at: float = 0.0

    def to_public(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "port": self.port,
            "base_url": self.base_url,
            "path": self.path,
            "http_status": self.http_status,
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at,
            "message": self.message,
        }


def port_of(pid: str, base_url: str) -> int:
    try:
        return urlsplit(base_url).port or DEFAULT_PORTS.get(pid, 0)
    except ValueError:
        return DEFAULT_PORTS.get(pid, 0)


def _status(
    pid: str,
    state: str,
    message: str,
    *,
    base_url: str,
    path: str,
    port: int,
    http_status: int | None = None,
    latency_ms: int | None = None,
) -> SidecarStatus:
    return SidecarStatus(
        pid=pid,
        state=state,
        message=message,
        port=port,
        base_url=base_url,
        path=path,
        http_status=http_status,
        latency_ms=latency_ms,
        checked_at=int(time.time()),
    )


_tls_ctx: ssl.SSLContext | None = None


def _tls_context() -> ssl.SSLContext:
    """复用一个 SSLContext：httpx 每次建 client 都重新加载证书链，在本机要 1s+，
    会把 1.5s 的探测超时全吃掉。sidecar 通常是 http，https 时用系统信任库即可。"""
    global _tls_ctx
    if _tls_ctx is None:
        _tls_ctx = ssl.create_default_context()
    return _tls_ctx


async def _fetch(url: str, headers: dict[str, str], timeout: float) -> httpx.Response:
    """单独抽出来，测试可在这里注入 ConnectError/TimeoutException，不依赖系统端口行为。"""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=timeout),
        trust_env=False,
        verify=_tls_context(),
    ) as client:
        return await client.get(url, headers=headers)


async def _dial(host: str, port: int, timeout: float) -> None:
    """裸 TCP 预检。拒绝连接与超时在这里就能分开，也避开 httpx 在 Windows 上
    对同一地址多次重试、把短超时兜成数秒的问题。单独抽出来便于测试注入 refused。"""
    _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass


async def probe_sidecar(
    pid: str,
    base_url: str,
    path: str | None = None,
    *,
    api_key: str = "",
    timeout: float | None = None,
) -> SidecarStatus:
    """一次短超时探测。任何返回值都带人话，调用方不需要再拼措辞。"""
    path = path or PROBE_PATH
    timeout = PROBE_TIMEOUT_SEC if timeout is None else timeout
    port = port_of(pid, base_url)
    url = base_url.rstrip("/") + path
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
    host = urlsplit(base_url).hostname or ""
    started = time.perf_counter()

    if host and port:
        try:
            await _dial(host, port, timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return _status(
                pid, "timeout",
                f"侧车无响应（端口 {port}）：sidecar 探测 {timeout:g}s 超时，进程可能卡死或仍在启动",
                base_url=base_url, path=path, port=port,
            )
        except ConnectionRefusedError:
            return _status(
                pid, "refused",
                f"侧车未运行（端口 {port}）：sidecar 连接被拒绝，请先启动对应进程",
                base_url=base_url, path=path, port=port,
            )
        except OSError as exc:
            return _status(
                pid, "http_error",
                f"端口 {port} 的 sidecar 探测失败：{type(exc).__name__}",
                base_url=base_url, path=path, port=port,
            )

    try:
        resp = await asyncio.wait_for(_fetch(url, headers, timeout), timeout)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return _status(
            pid, "timeout",
            f"侧车无响应（端口 {port}）：sidecar 探测 {timeout:g}s 超时，进程可能卡死或仍在启动",
            base_url=base_url, path=path, port=port,
        )
    except httpx.ConnectError:
        return _status(
            pid, "refused",
            f"侧车未运行（端口 {port}）：sidecar 连接被拒绝，请先启动对应进程",
            base_url=base_url, path=path, port=port,
        )
    except httpx.RequestError as exc:
        return _status(
            pid, "http_error",
            f"端口 {port} 的 sidecar 探测失败：{type(exc).__name__}",
            base_url=base_url, path=path, port=port,
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    code = resp.status_code
    if code < 400:
        return _status(
            pid, "alive",
            f"sidecar 在线（端口 {port}，{latency_ms}ms）",
            base_url=base_url, path=path, port=port, http_status=code, latency_ms=latency_ms,
        )
    if code in (401, 403):
        return _status(
            pid, "auth",
            f"sidecar 在线（端口 {port}），但 {path} 返回 {code}：api_key 未配置或不匹配",
            base_url=base_url, path=path, port=port, http_status=code, latency_ms=latency_ms,
        )
    return _status(
        pid, "http_error",
        f"端口 {port} 有服务在监听，但 {path} 返回 {code}：可能不是目标 sidecar（端口被占用）",
        base_url=base_url, path=path, port=port, http_status=code, latency_ms=latency_ms,
    )


def view(pid: str) -> SidecarStatus | None:
    entry = _cache.get(pid)
    return entry[0] if entry else None


def remember(status: SidecarStatus) -> None:
    """写入缓存（测试与探测结果共用同一条路径）。"""
    _cache[status.pid] = (status, time.monotonic())


def decorate_public(item: dict[str, Any]) -> None:
    """把缓存里的探测结果贴到 /api/providers 的单条记录上。

    只改 readiness / requires 与新增 sidecar_status，不动能力位与其它字段；
    没探测过就保持 spec 原值，避免把「还没测」当成「不可用」。
    """
    if item.get("status") != "sidecar":
        return
    status = view(str(item.get("id") or ""))
    if status is None:
        return
    item["sidecar_status"] = status.to_public()
    if status.state == "alive":
        item["readiness"] = "ready"
        item["requires"] = []
    else:
        item["readiness"] = "needs_config"
        item["requires"] = [status.message]


async def refresh(registry: Any, *, force: bool = False) -> dict[str, SidecarStatus]:
    """并发探测四家 sidecar，带 TTL 缓存。registry 只有 .get(pid) 与 .list_public() 两个依赖。"""
    items = registry.list_public()
    pids = [str(item["id"]) for item in items if item.get("status") == "sidecar"]
    now = time.monotonic()
    todo = [
        pid for pid in pids
        if force or pid not in _cache or now - _cache[pid][1] >= CACHE_TTL_SEC
    ]
    if not todo:
        return {pid: _cache[pid][0] for pid in pids if pid in _cache}

    async def one(pid: str) -> tuple[str, SidecarStatus]:
        adapter = registry.get(pid)
        path = str(getattr(adapter, "options", {}).get("health_path") or PROBE_PATH)
        status = await probe_sidecar(
            pid, adapter.base_url, path, api_key=getattr(adapter, "api_key", "")
        )
        return pid, status

    results = await asyncio.gather(*(one(pid) for pid in todo))
    for _, status in results:
        remember(status)
    return {pid: entry[0] for pid, entry in _cache.items()}


def clear_cache() -> None:
    """测试与配置变更后重置缓存。"""
    _cache.clear()
