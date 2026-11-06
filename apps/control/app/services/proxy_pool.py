"""出口代理池：fixed 固定首条，round_robin 按配置签名轮转。

控制面保存 options 后原地热更新，适配器每次请求都会用 from_options 重新读配置；
游标如果跟着实例走，每次重建都会归零，round_robin 会永远停在第一条出口，所以
游标按 (mode, entries) 共享，配置一变签名自然归零重来。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, ClassVar

MODE_FIXED = "fixed"
MODE_ROUND_ROBIN = "round_robin"
MODES = (MODE_FIXED, MODE_ROUND_ROBIN)


def _clean_entries(value: Any) -> list[str]:
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    clean: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in clean:
            clean.append(text)
    return clean


def _clean_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in MODES else MODE_FIXED


def mask_proxy(proxy: str) -> str:
    """health/日志只留主机端口：代理串里的 user:pass 回显等于把出口账号送出去。"""
    text = str(proxy or "").strip()
    at = text.rfind("@")
    if at < 0:
        return text
    head = text[:at]
    scheme = f"{head.split('://', 1)[0]}://" if "://" in head else ""
    return f"{scheme}***@{text[at + 1:]}"


@dataclass
class ProxyPool:
    """一次请求可选的出口集合；空池表示直连，所有 next() 都返回空串。"""

    entries: tuple[str, ...] = ()
    mode: str = MODE_FIXED

    _cursors: ClassVar[dict[tuple[str, tuple[str, ...]], int]] = {}
    _cursor_lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def from_options(cls, options: dict[str, Any] | None) -> "ProxyPool":
        opaque = options or {}
        mode = MODE_FIXED
        entries: list[str] = []
        raw = opaque.get("proxy_pool")
        if isinstance(raw, dict):
            mode = _clean_mode(raw.get("mode"))
            entries = _clean_entries(raw.get("entries"))
        elif isinstance(raw, (list, tuple, str)):
            entries = _clean_entries(raw)
        # 三种配置形态按优先级取第一个非空来源：混用会把 fixed 的首条出口悄悄换掉
        for fallback in ("proxies", "proxy"):
            if entries:
                break
            entries = _clean_entries(opaque.get(fallback))
        return cls(entries=tuple(entries), mode=mode)

    @property
    def enabled(self) -> bool:
        return bool(self.entries)

    @property
    def size(self) -> int:
        return len(self.entries)

    def _axis(self) -> tuple[str, tuple[str, ...]]:
        return (self.mode, self.entries)

    def next(self) -> str:
        if not self.entries:
            return ""
        if self.mode != MODE_ROUND_ROBIN:
            return self.entries[0]
        with ProxyPool._cursor_lock:
            used = ProxyPool._cursors.get(self._axis(), 0)
            ProxyPool._cursors[self._axis()] = used + 1
        return self.entries[used % len(self.entries)]

    def describe(self) -> dict[str, Any]:
        with ProxyPool._cursor_lock:
            used = ProxyPool._cursors.get(self._axis(), 0)
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "size": self.size,
            "cursor": used % self.size if self.size else 0,
            "entries": [mask_proxy(entry) for entry in self.entries],
        }
