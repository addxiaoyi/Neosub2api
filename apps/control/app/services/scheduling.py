"""账号调度策略：粘性 / 优先级 / 权重 / 故障转移四档。

四档只在「第一个号怎么选」上不同，选完之后一律按配置顺序向后轮转，避免
同一请求里反复撞同一个号：

- sticky：会话粘滞。同一 session_key 上次用的号仍健康就继续用；首次见到该
  会话时按顺序选一个并绑定。没有 session_key 时退化为 failover（不假装粘滞）。
- priority：按配置顺序取第一个健康号，适合主号优先、备号兜底的部署。
- weighted：在健康号之间按权重随机，权重来自 options.token_weights / key_weights。
- failover：均权轮转游标，保证失败前不重复命中同一个号。

所有号都在冷却时显式降级：忽略冷却、按最早恢复顺序尝试，并在日志与快照里
标记 degraded，方便排查「为什么这次用了冷却中的号」。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .rotation import (
    CredentialPool,
    Failure,
    STICKY_TTL,
    fingerprint,
    sticky_key_from_payload,
)

logger = logging.getLogger("unified.scheduling")

POLICIES = ("sticky", "priority", "weighted", "failover")
DEFAULT_POLICY = "sticky"
DEFAULT_LIMIT = 3


@dataclass(frozen=True)
class Plan:
    """一次调度的执行顺序。indices 是按顺序要尝试的下标。"""

    indices: list[int] = field(default_factory=list)
    rule: str = "none"
    degraded: bool = False
    note: str = ""

    def describe(self, credentials: Sequence[str]) -> str:
        ids = [fingerprint(credentials[index]) for index in self.indices]
        state = "降级" if self.degraded else "正常"
        return f"规则={self.rule} 状态={state} 顺序={ids} 说明={self.note or '-'}"


class Scheduler:
    """叠加在 CredentialPool 之上的策略层：策略决定顺序，池负责健康与冷却。"""

    def __init__(
        self,
        *,
        sticky_ttl: float = STICKY_TTL,
        rng: random.Random | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._pool = CredentialPool(sticky_ttl=sticky_ttl, rng=rng, clock=clock)
        self._policy = DEFAULT_POLICY

    # ---------- 策略 ----------

    def resolve_policy(self, policy: str) -> str:
        raw = str(policy or "").strip().lower()
        if raw in POLICIES:
            return raw
        if raw:
            logger.warning("未知调度策略 %r，回落到 %s", policy, DEFAULT_POLICY)
        return DEFAULT_POLICY

    async def plan(
        self,
        credentials: Sequence[str],
        *,
        policy: str = "",
        session_key: str = "",
        limit: int = DEFAULT_LIMIT,
        weights: Any = None,
    ) -> Plan:
        creds: list[str] = []
        for credential in credentials:
            if credential and credential not in creds:
                creds.append(credential)
        if not creds:
            return Plan([], "none", False, "账号池为空")
        resolved = self.resolve_policy(policy)
        self._policy = resolved
        # 每次请求都重设权重：options 改回均权时不能沿用上一轮的旧权重
        self._pool.set_weights(creds, weights)
        usable = await self._pool.available(creds)
        degraded = not usable
        note = ""
        if degraded:
            usable = creds
            note = "全部账号冷却，忽略冷却按最早恢复顺序降级"
        if resolved == "weighted" and not self._pool.has_weights():
            resolved = "failover"
            note = note or "未配置有效权重，weighted 退化为故障转移"
        if resolved == "sticky" and not session_key:
            resolved = "failover"
            note = note or "请求未带会话标识，粘滞退化为故障转移"
        first = await self._first(creds, usable, resolved, session_key)
        start = creds.index(first)
        # 从选中号开始向后轮转，跳过已选中的号与非健康号
        ordered: list[int] = []
        for offset in range(len(creds)):
            index = (start + offset) % len(creds)
            if index != start and creds[index] in usable:
                ordered.append(index)
        indices = [start, *ordered][: max(1, int(limit or DEFAULT_LIMIT))]
        if session_key and not degraded:
            await self._pool.bind_sticky(session_key, first)
        logger.info(
            "调度决定 policy=%s first=%s 顺序=%s 降级=%s 说明=%s",
            resolved, fingerprint(first), [fingerprint(creds[i]) for i in indices], degraded, note or "-",
        )
        return Plan(indices, resolved, degraded, note)

    async def _first(
        self, creds: Sequence[str], usable: Sequence[str], policy: str, session_key: str
    ) -> str:
        if policy == "priority":
            chosen = usable[0]
            await self._pool.touch(chosen)
            return chosen
        pick = await self._pool.acquire(creds, sticky_key=session_key, allow_degraded=True)
        if pick is None:
            return usable[0]
        return pick.key

    # ---------- 健康登记 ----------

    async def note_failure(self, credential: str, status: int, *, cooldown_sec: float | None = None) -> Failure:
        return await self._pool.report_failure(credential, status, cooldown_sec=cooldown_sec)

    async def note_success(self, credential: str) -> None:
        await self._pool.report_success(credential)

    async def cool(self, credential: str, seconds: float) -> float:
        return await self._pool.cool(credential, seconds)

    async def snapshot(self, credentials: Sequence[str]) -> dict[str, Any]:
        state = await self._pool.snapshot(credentials)
        state["policy"] = self._policy
        return state


def session_key(payload: dict[str, Any] | None) -> str:
    """会话标识复用 rotation 的口径；取不到就返回空串（调度退化为 failover）。"""
    return sticky_key_from_payload(payload)
