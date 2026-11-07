"""账号池调度：分档冷却、熔断、粘性、权重与轮转组持久化。

选择顺序（每一步都可解释，命中原因进日志与 health 的 rotation 快照）：

1. 健康：冷却中 / 熔断打开的账号直接跳过，冷却剩余秒数可查；
2. 粘性：同一 sticky_key 上次绑定的账号仍健康 → 优先复用（跨请求会话不换号）；
3. 权重：配置了非均权（options.key_weights）→ 在健康账号里按权重随机；
4. 轮转：默认均权轮转游标，保证不配权重时的行为可复现；
5. 全冷却：strict 模式显式拒绝；degraded 模式按「最早恢复」降级试一次。

失败冷却按状态码分档，同账号连续失败按倍数升级；连续失败达到阈值即熔断，
熔断窗口内该账号不再参与选择。429 额外按「账号+模型」记一条 60s 冷却：只挡
这一个模型，账号本身不判死，其它模型照常可用。落库只存凭据指纹，不复制明文密钥。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger("unified.rotation")

DEFAULT_COOLDOWN = 30.0
# 分档冷却：鉴权失败要人工换号，比限流更久；402 余额受限同理
COOLDOWN_BY_STATUS = {
    401: 600.0,
    403: 600.0,
    402: 900.0,
    408: 60.0,
    429: 120.0,
    500: 60.0,
    502: 60.0,
    503: 60.0,
    504: 60.0,
}
TIER_FACTOR = 2.0
MAX_COOLDOWN = 3600.0
# 模型级限流：上游按模型报 429，只冷这一条模型，账号本身继续服务其它模型
MODEL_RATE_LIMIT_COOLDOWN = 60.0
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN = 3600.0
STICKY_TTL = 1800.0
GROUP_MAX_LEN = 64

STICKY_FIELDS = ("session_id", "conversation_id", "prompt_cache_key")
STICKY_META_FIELDS = ("session_id", "conversation_id", "user_id")


def fingerprint(secret: str) -> str:
    """凭据指纹：日志与落库都只认它，绝不写明文密钥。"""
    if not secret:
        return "cred-empty"
    return "cred-" + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def sticky_key_from_payload(payload: dict[str, Any] | None) -> str:
    """从请求体取会话标识。

    网关适配器拿不到 HTTP 头（v1 只传 payload），所以按客户端常见字段取值；
    取不到就返回空串，调度退回轮转，不伪造粘性。
    """
    if not isinstance(payload, dict):
        return ""
    for field in STICKY_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    meta = payload.get("metadata")
    if isinstance(meta, dict):
        for field in STICKY_META_FIELDS:
            value = meta.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    user = payload.get("user")
    return user.strip()[:200] if isinstance(user, str) and user.strip() else ""


@dataclass(frozen=True)
class Pick:
    """一次选号结果。key 是凭据本体，对外只能通过 cred_id 暴露。"""

    key: str
    index: int
    rule: str
    note: str = ""

    @property
    def cred_id(self) -> str:
        return fingerprint(self.key)


@dataclass(frozen=True)
class Failure:
    """失败登记结果，供调用方写日志/决定是否还有别的号可用。"""

    cred_id: str
    status: int
    cooldown_sec: float
    consecutive: int
    breaker_open: bool
    model: str = ""

    def describe(self) -> str:
        state = "熔断" if self.breaker_open else "冷却"
        scope = f" 模型={self.model}" if self.model else ""
        return f"{self.cred_id} {state} {self.cooldown_sec:.0f}s（连续失败 {self.consecutive} 次）{scope}"


class CredentialPool:
    """单个适配器进程内的账号池状态。

    与旧 KeyPool 的差异：多了权重、粘性、熔断与可查询快照；available / pick / cool
    三个方法的签名保持兼容，老调用点不用改。
    """

    def __init__(
        self,
        *,
        sticky_ttl: float = STICKY_TTL,
        breaker_threshold: int = BREAKER_THRESHOLD,
        rng: random.Random | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._sticky_ttl = sticky_ttl
        self._breaker_threshold = max(1, breaker_threshold)
        self._rng = rng or random.Random()
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cursor = 0
        self._cooling: dict[str, float] = {}
        self._model_cooling: dict[tuple[str, str], float] = {}
        self._failures: dict[str, int] = {}
        self._last_used: dict[str, float] = {}
        self._sticky: dict[str, tuple[str, float]] = {}
        self._weights: dict[str, float] = {}

    # ---------- 配置 ----------

    def set_weights(self, keys: Sequence[str], spec: Any) -> None:
        """权重两种写法：与 keys 对齐的数组，或 {序号|指纹: 权重}。

        没配或全为 1 时清空，调度回到均权轮转，避免默认行为变成随机。
        """
        weights: dict[str, float] = {}
        if isinstance(spec, dict):
            for raw_index, raw_weight in spec.items():
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(keys):
                    weights[fingerprint(keys[index])] = max(0.0, float(raw_weight))
        elif isinstance(spec, (list, tuple)):
            for index, raw_weight in enumerate(spec):
                if index >= len(keys):
                    break
                try:
                    weights[fingerprint(keys[index])] = max(0.0, float(raw_weight))
                except (TypeError, ValueError):
                    continue
        if not any(weight != 1.0 for weight in weights.values()):
            weights = {}
        self._weights = weights

    def has_weights(self) -> bool:
        """是否配置了非均权；未配置时调度必须回到可复现的轮转。"""
        return bool(self._weights)

    # ---------- 查询 ----------

    def _now(self) -> float:
        return float(self._clock())

    def _expire(self, now: float) -> None:
        for cred_id, until in list(self._cooling.items()):
            if until <= now:
                self._cooling.pop(cred_id, None)
        for (cred_id, model), until in list(self._model_cooling.items()):
            if until <= now:
                self._model_cooling.pop((cred_id, model), None)

    def _model_until(self, cred_id: str, model: str) -> float:
        return self._model_cooling.get((cred_id, model), 0.0) if model else 0.0

    def _recovery(self, cred_id: str, model: str) -> float:
        """账号最早可再用的时刻：账号冷却与模型冷却取较晚者。"""
        return max(self._cooling.get(cred_id, 0.0), self._model_until(cred_id, model))

    def _available(self, keys: Sequence[str], now: float, model: str = "") -> list[str]:
        self._expire(now)
        result: list[str] = []
        for key in keys:
            cred_id = fingerprint(key)
            if self._cooling.get(cred_id, 0.0) > now:
                continue
            if model and self._model_until(cred_id, model) > now:
                continue
            result.append(key)
        return result

    async def available(self, keys: Sequence[str], *, model: str = "") -> list[str]:
        async with self._lock:
            return self._available(keys, self._now(), model)

    async def remaining(self, key: str, *, model: str = "") -> float:
        async with self._lock:
            now = self._now()
            self._expire(now)
            return max(0.0, self._recovery(fingerprint(key), model) - now)

    def _weighted(self, keys: Sequence[str]) -> str:
        weights = [max(self._weights.get(fingerprint(key), 1.0), 0.0001) for key in keys]
        return self._rng.choices(list(keys), weights=weights, k=1)[0]

    def _sticky_bound(self, sticky_key: str, available: Sequence[str], now: float) -> str:
        binding = self._sticky.get(sticky_key)
        if not binding:
            return ""
        cred_id, bound_at = binding
        if now - bound_at > self._sticky_ttl:
            self._sticky.pop(sticky_key, None)
            return ""
        for key in available:
            if fingerprint(key) == cred_id:
                return key
        return ""

    async def acquire(
        self,
        keys: Sequence[str],
        *,
        sticky_key: str = "",
        allow_degraded: bool = False,
        model: str = "",
    ) -> Pick | None:
        async with self._lock:
            now = self._now()
            available = self._available(keys, now, model)
            if not available:
                if not allow_degraded:
                    return None
                # 降级：所有号都在冷却，挑最早恢复的那个试一次，并明确标记 degraded
                soonest = min(keys, key=lambda key: self._recovery(fingerprint(key), model))
                index = list(keys).index(soonest)
                self._touch(soonest, now)
                wait = self._recovery(fingerprint(soonest), model) - now
                logger.warning(
                    "账号池全冷却，降级选号 provider_pool=%d cred=%s 模型=%s 剩余=%.0fs",
                    len(keys), fingerprint(soonest), model or "-", wait,
                )
                return Pick(soonest, index, "degraded", note="全部账号冷却中，按最早恢复降级")
            if sticky_key:
                bound = self._sticky_bound(sticky_key, available, now)
                if bound:
                    index = list(keys).index(bound)
                    self._touch(bound, now)
                    return Pick(bound, index, "sticky", note=f"会话 {sticky_key[:24]} 沿用上次账号")
            if self._weights and len(available) > 1:
                chosen = self._weighted(available)
                index = list(keys).index(chosen)
                self._touch(chosen, now)
                return Pick(chosen, index, "weighted", note=f"权重 {self._weights.get(fingerprint(chosen), 1.0):g}")
            chosen = available[self._cursor % len(available)]
            self._cursor += 1
            index = list(keys).index(chosen)
            self._touch(chosen, now)
            return Pick(chosen, index, "round_robin", note=f"游标 {self._cursor - 1}")

    def _touch(self, key: str, now: float) -> None:
        self._last_used[fingerprint(key)] = now

    async def pick(self, keys: Sequence[str], *, sticky_key: str = "", model: str = "") -> str | None:
        """兼容旧 KeyPool 调用点：只返回凭据本体，不返回规则。"""
        result = await self.acquire(keys, sticky_key=sticky_key, model=model)
        return result.key if result else None

    async def touch(self, key: str) -> None:
        async with self._lock:
            self._touch(key, self._now())

    # ---------- 健康登记 ----------

    async def report_failure(
        self, key: str, status: int, *, cooldown_sec: float | None = None, model: str = ""
    ) -> Failure:
        async with self._lock:
            now = self._now()
            self._expire(now)
            cred_id = fingerprint(key)
            # 429 是上游按模型报的限流：只冷这一条模型，账号与粘性都不动
            if model and status == 429:
                span = float(cooldown_sec if cooldown_sec is not None else MODEL_RATE_LIMIT_COOLDOWN)
                span = min(max(span, 1.0), MAX_COOLDOWN)
                self._model_cooling[(cred_id, model)] = now + span
                self._last_used[cred_id] = now
                failure = Failure(cred_id, status, span, 0, False, model)
                logger.info("模型限流 %s", failure.describe())
                return failure
            consecutive = self._failures.get(cred_id, 0) + 1
            self._failures[cred_id] = consecutive
            base = max(COOLDOWN_BY_STATUS.get(status, 0.0), cooldown_sec if cooldown_sec is not None else DEFAULT_COOLDOWN)
            span = min(base * (TIER_FACTOR ** (consecutive - 1)), MAX_COOLDOWN)
            breaker = consecutive >= self._breaker_threshold
            if breaker:
                span = max(span, BREAKER_COOLDOWN)
            self._cooling[cred_id] = now + span
            # 绑在失败账号上的粘性立即解绑，下一次请求重新选号而不是继续撞坏号
            for session, (bound, _) in list(self._sticky.items()):
                if bound == cred_id:
                    self._sticky.pop(session, None)
            result = Failure(cred_id, status, span, consecutive, breaker)
            log = logger.warning if breaker else logger.info
            log("账号失败 %s 状态=%s", result.describe(), status)
            return result

    async def report_network_error(self, key: str) -> None:
        """网络层失败只解绑粘性、不进冷却：连不上多半是出口问题，单次失败不判死账号。"""
        async with self._lock:
            cred_id = fingerprint(key)
            for session, (bound, _) in list(self._sticky.items()):
                if bound == cred_id:
                    self._sticky.pop(session, None)

    async def report_success(self, key: str, *, model: str = "") -> None:
        async with self._lock:
            now = self._now()
            cred_id = fingerprint(key)
            if model:
                self._model_cooling.pop((cred_id, model), None)
            failures = self._failures.pop(cred_id, 0)
            self._cooling.pop(cred_id, None)
            self._last_used[cred_id] = now
            if failures:
                logger.info("账号恢复 %s（此前连续失败 %d 次）", cred_id, failures)

    async def bind_sticky(self, sticky_key: str, key: str) -> None:
        if not sticky_key:
            return
        async with self._lock:
            self._sticky[sticky_key] = (fingerprint(key), self._now())

    async def cool(self, key: str, seconds: float | None = None) -> float:
        """兼容旧 KeyPool：按给定秒数冷却，不做分档与熔断升级。"""
        span = float(seconds if seconds is not None else DEFAULT_COOLDOWN)
        async with self._lock:
            self._cooling[fingerprint(key)] = self._now() + span
        return span

    # ---------- 可观测性 ----------

    async def snapshot(
        self,
        keys: Sequence[str],
        *,
        group: str = "",
        group_active: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        async with self._lock:
            now = self._now()
            self._expire(now)
            wanted = [fingerprint(key) for key in keys]
            cooled: dict[str, dict[str, float]] = {}
            for (cred_id, name), until in self._model_cooling.items():
                if cred_id not in wanted:
                    continue
                wait = until - now
                if wait > 0:
                    cooled.setdefault(cred_id, {})[name] = round(wait, 1)
            model_cooling = (
                sum(1 for cred_id in wanted if cooled.get(cred_id, {}).get(model, 0.0) > 0)
                if model
                else sum(len(entries) for entries in cooled.values())
            )
            entries: list[dict[str, Any]] = []
            cooling = 0
            for key in keys:
                cred_id = fingerprint(key)
                until = self._cooling.get(cred_id, 0.0)
                remaining = max(0.0, until - now)
                if model:
                    remaining = max(remaining, cooled.get(cred_id, {}).get(model, 0.0))
                if remaining > 0:
                    cooling += 1
                last = self._last_used.get(cred_id)
                failures = self._failures.get(cred_id, 0)
                if remaining > 0:
                    state = "breaker" if failures >= self._breaker_threshold else "cooling"
                elif failures:
                    state = "degraded"
                else:
                    state = "ready"
                entries.append({
                    "id": cred_id,
                    "state": state,
                    "cooldown_remaining": round(remaining, 1),
                    "consecutive_failures": failures,
                    "last_used_sec_ago": round(now - last, 1) if last else None,
                    "weight": self._weights.get(cred_id, 1.0),
                })
            result: dict[str, Any] = {
                "rule": "sticky>weighted>round_robin" if self._weights else "sticky>round_robin",
                "available": len(keys) - cooling,
                "cooling": cooling,
                "model_cooling": model_cooling,
                "model_cooldowns": cooled,
                "credentials": entries,
            }
            if group:
                result["group"] = group
                result["group_active"] = group_active
            return result


class RotationStore:
    """轮转组状态落库：只存凭据指纹与规则名，不复制明文密钥。

    适配器可能在 Database 尚未建表时被单测直接构造，所以这里按 db.py 的同一段
    DDL 做幂等建表；读写失败一律降级为内存态并留 WARNING，不能让调度把请求打死。
    """

    def __init__(self, db_path: Path | str | None) -> None:
        self._db_path = Path(db_path) if db_path else None
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection | None:
        if self._db_path is None:
            return None
        if self._conn is not None:
            return self._conn
        from ..db import ROTATION_STATE_DDL

        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("轮转组状态目录不可写 %s：%s", self._db_path.parent, exc)
            return None
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.executescript(ROTATION_STATE_DDL)
        self._conn = conn
        return conn

    def load(self, provider: str, group: str) -> dict[str, Any]:
        try:
            conn = self._connect()
            if conn is None:
                return {}
            row = conn.execute(
                "SELECT state_json FROM rotation_state WHERE provider = ? AND group_name = ?",
                (provider, _clip(group)),
            ).fetchone()
            if row is None:
                return {}
            data = json.loads(row["state_json"] or "{}")
            return data if isinstance(data, dict) else {}
        except (sqlite3.Error, OSError, json.JSONDecodeError) as exc:
            logger.warning("轮转组状态读取失败 provider=%s group=%s：%s", provider, group, exc)
            return {}

    def save(self, provider: str, group: str, *, active: str, rule: str) -> None:
        payload = json.dumps({"active": active, "rule": rule}, separators=(",", ":"))
        try:
            conn = self._connect()
            if conn is None:
                return
            with conn:
                conn.execute(
                    "INSERT INTO rotation_state(provider, group_name, state_json, updated_at) "
                    "VALUES(?, ?, ?, ?) ON CONFLICT(provider, group_name) DO UPDATE SET "
                    "state_json = excluded.state_json, updated_at = excluded.updated_at",
                    (provider, _clip(group), payload, int(time.time())),
                )
        except (sqlite3.Error, OSError) as exc:
            logger.warning("轮转组状态写入失败 provider=%s group=%s：%s", provider, group, exc)

    def clear(self, provider: str, group: str) -> None:
        """解绑持久化成员：网络层失败后必须重新选号，不能继续命中同一把坏 key。"""
        try:
            conn = self._connect()
            if conn is None:
                return
            with conn:
                conn.execute(
                    "DELETE FROM rotation_state WHERE provider = ? AND group_name = ?",
                    (provider, _clip(group)),
                )
        except (sqlite3.Error, OSError) as exc:
            logger.warning("轮转组状态清除失败 provider=%s group=%s：%s", provider, group, exc)

    def close(self) -> None:
        """显式释放 sqlite 连接：Windows 上临时目录清理要求句柄先归还。"""
        if self._conn is None:
            return
        try:
            self._conn.close()
        except sqlite3.Error as exc:
            logger.warning("轮转组状态连接关闭失败 %s：%s", self._db_path, exc)
        finally:
            self._conn = None


def _clip(group: str) -> str:
    return (group or "default").strip()[:GROUP_MAX_LEN] or "default"


async def pick_in_group(
    pool: CredentialPool,
    store: RotationStore,
    *,
    provider: str,
    group: str,
    keys: Sequence[str],
    sticky_key: str = "",
    model: str = "",
) -> Pick | None:
    """轮转组选号：组内粘滞成员优先；成员失败/冷却后换组内下一个并落库。

    组状态只记 active 指纹，所以进程重启后同一组仍命中同一个号（冷却态除外）。
    传 model 时按「账号+模型」粒度排除冷却，这是 qoder 上游按模型限流的口径。
    """
    if not keys:
        return None
    group = _clip(group)
    state = store.load(provider, group)
    active = str(state.get("active") or "")
    available = await pool.available(keys, model=model)
    if active:
        for index, key in enumerate(keys):
            if fingerprint(key) == active and key in available:
                await pool.touch(key)
                logger.info(
                    "轮转组命中 provider=%s group=%s cred=%s 规则=group_sticky",
                    provider, group, active,
                )
                return Pick(key, index, "group_sticky", note=f"group={group} 沿用持久化成员")
    pick = await pool.acquire(keys, sticky_key=sticky_key, model=model)
    if pick is None:
        return None
    rule = "group_rotate" if active else "group_init"
    store.save(provider, group, active=pick.cred_id, rule=rule)
    logger.info(
        "轮转组换号 provider=%s group=%s cred=%s 规则=%s 原因=%s",
        provider, group, pick.cred_id, rule, pick.note or "-",
    )
    return Pick(pick.key, pick.index, rule, note=pick.note)
