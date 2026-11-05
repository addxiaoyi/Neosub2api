"""provider 定时健康检测：周期探测 + 留痕。

沿用仓库既有后台任务机制（GrokRegisterService 同款：lifespan 里 start/stop 的
asyncio 周期任务），不引入第二套调度器。

边界（有意为之）：
- 只读探测，绝不改 providers 表的 enabled 状态；
- 不参与 /v1 路由、不触发故障转移、不写 request_logs / billing_ledger；
- 与 alerting 的「provider 连败 N 次」链路保持独立：那条链路看的是真实请求失败，
  这里是主动探测，混在一起会把「没配好」误报成「线上挂了」。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any

from ..config import HEALTH_INTERVAL_DEFAULT, Settings, clamp_health_interval
from ..db import Database
from ..registry import ProviderRegistry
from .provider_probe import STATUS_FAILED, ProbeOutcome, probe

logger = logging.getLogger("unified.health")

# 每个 provider 的保留条数；300s 一轮约等于 1.7 天，够定位最近一次波动
KEEP_PER_PROVIDER = 500
DEFAULT_HISTORY = 20
MAX_HISTORY = 200


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "provider": row["provider"],
        "status": row["status"],
        "started_at": int(row["started_at"]),
        "finished_at": int(row["finished_at"]),
        "latency_ms": int(row["latency_ms"]),
        "error": row["error"],
    }


class ProviderHealthService:
    def __init__(self, settings: Settings, db: Database, registry: ProviderRegistry) -> None:
        self._settings = settings
        self._db = db
        self._registry = registry
        self._loop: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._loop is None or self._loop.done():
            self._loop = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        if self._loop is None:
            return
        self._loop.cancel()
        try:
            await self._loop
        except asyncio.CancelledError:
            pass
        self._loop = None

    async def _run_forever(self) -> None:
        while True:
            await asyncio.sleep(self.interval_sec)
            try:
                await self.tick()
            except Exception:  # 单轮失败不能让调度线程退出
                logger.exception("provider 健康检测调度失败")

    # ---- 调度口径 ----

    @property
    def interval_sec(self) -> int:
        return clamp_health_interval(getattr(self._settings, "health_check_interval_sec", HEALTH_INTERVAL_DEFAULT))

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings, "health_check_enabled", False))

    async def tick(self) -> None:
        """一次计划内的检测：总开关关闭时什么都不做（手动端点不走这里）。"""
        if not self.enabled:
            return
        result = await self.run_once()
        logger.info("健康检测完成：%s 个 provider", len(result["providers"]))

    # ---- 执行 ----

    async def run_once(self, provider: str | None = None) -> dict[str, Any]:
        """跑一轮探测并落库，返回与 GET 端点一致的快照。

        各 provider 互不依赖，并发探测；否则一轮耗时是各家超时的总和，
        手动按钮和测试都会被单个慢 sidecar 拖住。
        """
        async with self._lock:
            targets = [provider] if provider else [item["id"] for item in self._registry.list_public()]
            await asyncio.gather(*(self._probe_and_store(pid) for pid in targets))
            self._prune(targets)
        return self.snapshot(provider=provider)

    async def _probe_and_store(self, pid: str) -> None:
        started = int(time.time())
        try:
            adapter = self._registry.get(pid)
        except Exception as exc:  # 适配器构建失败也算一次如实记录
            self._store(pid, started, int(time.time()), ProbeOutcome(
                status=STATUS_FAILED, latency_ms=0, message=f"适配器不可用：{exc}",
            ))
            return
        outcome = await probe(adapter)
        self._store(pid, started, int(time.time()), outcome)

    def _store(self, provider: str, started: int, finished: int, outcome: ProbeOutcome) -> None:
        conn = self._db.connect()
        try:
            conn.execute(
                "INSERT INTO provider_health_checks(provider, started_at, finished_at, status, latency_ms, error) "
                "VALUES(?,?,?,?,?,?)",
                (provider, started, finished, outcome.status, outcome.latency_ms, outcome.message),
            )
            conn.commit()
        finally:
            conn.close()

    def _prune(self, providers: list[str]) -> None:
        conn = self._db.connect()
        try:
            for pid in providers:
                conn.execute(
                    "DELETE FROM provider_health_checks WHERE provider = ? AND id < ("
                    "  SELECT MIN(id) FROM ("
                    "    SELECT id FROM provider_health_checks WHERE provider = ? ORDER BY id DESC LIMIT ?"
                    "  )"
                    ")",
                    (pid, pid, KEEP_PER_PROVIDER),
                )
            conn.commit()
        finally:
            conn.close()

    # ---- 读取 ----

    def snapshot(self, provider: str | None = None, limit: int = DEFAULT_HISTORY) -> dict[str, Any]:
        limit = max(1, min(MAX_HISTORY, int(limit)))
        conn = self._db.connect()
        try:
            latest = {
                row["provider"]: _row(row)
                for row in conn.execute(
                    "SELECT h.* FROM provider_health_checks h JOIN ("
                    "  SELECT provider, MAX(id) AS id FROM provider_health_checks GROUP BY provider"
                    ") m ON h.id = m.id"
                ).fetchall()
            }
            if provider:
                rows = conn.execute(
                    "SELECT * FROM provider_health_checks WHERE provider = ? ORDER BY id DESC LIMIT ?",
                    (provider, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM provider_health_checks ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        finally:
            conn.close()

        registered = [item["id"] for item in self._registry.list_public()]
        if provider:
            registered = [pid for pid in registered if pid == provider] or [provider]
        providers = []
        for pid in registered:
            last = latest.get(pid)
            providers.append(
                {
                    "provider": pid,
                    "status": last["status"] if last else None,
                    "checked_at": last["finished_at"] if last else None,
                    "latency_ms": last["latency_ms"] if last else None,
                    "error": last["error"] if last else "",
                }
            )
        return {
            "ok": True,
            "enabled": self.enabled,
            "interval_sec": self.interval_sec,
            "providers": providers,
            "history": [_row(row) for row in rows],
        }
