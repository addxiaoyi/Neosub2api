"""多密钥轮换、冷却与租约。

每个适配器实例各持一份状态（游标 + 锁 + 租约），不在模块级保存任何可变数据，
避免同进程多个实例互相打乱轮换顺序。冷却语义对齐 sub2api：失败密钥进入
冷却期，期间跳过；到期自动重新参与。

租约表示「这把 key 正被某个在途请求占用」：并发请求不会领到同一把 key，
请求结束必须 release（成功）或 feedback（失败，可顺带冷却）。租约带兜底
时长，调用方异常没归还时到期自动释放，避免一把 key 被永久占住。
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Callable

# 401 / 403 / 429 视为「这把 key 本身不可用」，值得冷却；5xx 与网络错误是上游问题，只换 key 不退避冷却
COOLDOWN_STATUSES = frozenset({401, 403, 429})
DEFAULT_COOLDOWN_SEC = 30.0
# 租约兜底时长：请求异常退出且没归还时，超过这个时间自动回收（评审定 60 秒）
DEFAULT_LEASE_TTL = 60.0
# 池子占满时最多等多久再报错；流式长请求通常远小于这个值
DEFAULT_LEASE_WAIT = 30.0


@dataclass(frozen=True, slots=True)
class KeyLease:
    """一次在途请求对某把 key 的占用凭证。"""

    key: str
    id: int


class KeyPool:
    """按实例维护的密钥轮换、冷却与租约状态。所有读写都在同一把锁内完成。"""

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        lease_ttl: float = DEFAULT_LEASE_TTL,
    ) -> None:
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)
        self._cursor = 0
        self._cooling: dict[str, float] = {}
        self._leases: dict[str, tuple[int, float]] = {}
        self._failures: dict[str, int] = {}
        self._lease_seq = 0
        self._clock = clock or time.monotonic
        self._lease_ttl = max(lease_ttl, 0.0)

    def _expire(self, now: float) -> None:
        # 冷却期结束的 key 自动回到轮换池；超时的租约一并回收
        for key in [k for k, until in self._cooling.items() if until <= now]:
            del self._cooling[key]
        for key in [k for k, (_, until) in self._leases.items() if until <= now]:
            del self._leases[key]

    def _usable(self, keys: list[str]) -> list[str]:
        return [key for key in keys if key not in self._cooling and key not in self._leases]

    async def available(self, keys: list[str]) -> list[str]:
        async with self._cond:
            self._expire(self._clock())
            return self._usable(keys)

    async def uncooled(self, keys: list[str]) -> list[str]:
        """只看冷却、不看租约：用于判断池里是否还有别的活钥匙可换。"""
        async with self._cond:
            self._expire(self._clock())
            return [key for key in keys if key not in self._cooling]

    async def peek(self, keys: list[str]) -> str | None:
        """只看不取：供健康探测使用，不推进游标、不占租约，也不看租约占用。"""
        async with self._cond:
            self._expire(self._clock())
            for key in keys:
                if key not in self._cooling:
                    return key
            return None

    async def pick(self, keys: list[str]) -> str | None:
        """按游标取一把可用 key 并推进游标；全部冷却/占用时返回 None。"""
        async with self._cond:
            self._expire(self._clock())
            usable = self._usable(keys)
            if not usable:
                return None
            index = self._cursor % len(usable)
            self._cursor = (index + 1) % len(usable)
            return usable[index]

    async def reserve(self, keys: list[str], timeout: float | None = DEFAULT_LEASE_WAIT) -> KeyLease | None:
        """领一份租约：优先按游标轮换；池子占满时等待归还，超时返回 None。"""
        deadline = None if timeout is None else self._clock() + max(timeout, 0.0)
        async with self._cond:
            while True:
                now = self._clock()
                self._expire(now)
                usable = self._usable(keys)
                if usable:
                    index = self._cursor % len(usable)
                    self._cursor = (index + 1) % len(usable)
                    key = usable[index]
                    self._lease_seq += 1
                    lease = KeyLease(key, self._lease_seq)
                    self._leases[key] = (lease.id, now + self._lease_ttl)
                    return lease
                if deadline is None:
                    return None
                remaining = deadline - now
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None

    async def release(self, lease: KeyLease) -> None:
        """成功归还：只清掉自己那份租约，租约 id 不匹配时不动别人的占用。"""
        async with self._cond:
            current = self._leases.get(lease.key)
            if current is not None and current[0] == lease.id:
                del self._leases[lease.key]
                self._cond.notify_all()

    async def feedback(self, lease: KeyLease, *, ok: bool, cooldown_seconds: float | None = None) -> None:
        """失败归还：ok=False 时按需冷却该 key 并累计失败次数，然后释放租约。"""
        async with self._cond:
            now = self._clock()
            if ok:
                self._failures[lease.key] = 0
            else:
                self._failures[lease.key] = self._failures.get(lease.key, 0) + 1
                span = DEFAULT_COOLDOWN_SEC if cooldown_seconds is None else cooldown_seconds
                self._cooling[lease.key] = now + max(span, 0.0)
            current = self._leases.get(lease.key)
            if current is not None and current[0] == lease.id:
                del self._leases[lease.key]
            self._cond.notify_all()

    async def failure_count(self, key: str) -> int:
        return self._failures.get(key, 0)

    async def cool(self, key: str, seconds: float | None = None) -> None:
        async with self._cond:
            span = DEFAULT_COOLDOWN_SEC if seconds is None else seconds
            self._cooling[key] = self._clock() + max(span, 0.0)
            self._cond.notify_all()
