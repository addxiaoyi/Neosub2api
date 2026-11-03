"""多密钥轮换与冷却。

每个适配器实例各持一份状态（游标 + 锁），不在模块级保存任何可变数据，
避免同进程多个实例互相打乱轮换顺序。冷却语义对齐 sub2api：失败密钥进入
冷却期，期间跳过；到期自动重新参与。
"""

import asyncio
import time

# 401 / 403 / 429 视为「这把 key 本身不可用」，值得冷却；5xx 与网络错误是上游问题，只换 key 不退避冷却
COOLDOWN_STATUSES = frozenset({401, 403, 429})
DEFAULT_COOLDOWN_SEC = 30.0


class KeyPool:
    """按实例维护的密钥轮换状态。所有读写都在同一把锁内完成。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cursor = 0
        self._cooling: dict[str, float] = {}

    def _expire(self, now: float) -> None:
        # 冷却期结束的 key 自动回到轮换池
        for key in [k for k, until in self._cooling.items() if until <= now]:
            del self._cooling[key]

    async def available(self, keys: list[str]) -> list[str]:
        async with self._lock:
            self._expire(time.monotonic())
            return [key for key in keys if key not in self._cooling]

    async def peek(self, keys: list[str]) -> str | None:
        """只看不取：供健康探测使用，不推进游标。"""
        async with self._lock:
            self._expire(time.monotonic())
            for key in keys:
                if key not in self._cooling:
                    return key
            return None

    async def pick(self, keys: list[str]) -> str | None:
        """按游标取一把可用 key 并推进游标；全部冷却时返回 None。"""
        async with self._lock:
            self._expire(time.monotonic())
            usable = [key for key in keys if key not in self._cooling]
            if not usable:
                return None
            index = self._cursor % len(usable)
            self._cursor = (index + 1) % len(usable)
            return usable[index]

    async def cool(self, key: str, seconds: float | None = None) -> None:
        async with self._lock:
            span = DEFAULT_COOLDOWN_SEC if seconds is None else seconds
            self._cooling[key] = time.monotonic() + max(span, 0.0)
