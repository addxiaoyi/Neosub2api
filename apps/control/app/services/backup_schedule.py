"""远端备份的定时调度：与 provider_health 同构的 asyncio 周期任务。

60 秒一个 tick，到点条件 = 开关打开且 (距上次尝试满 interval，或存在已到期的重试)。
抢不到 ops 锁就跳过本轮，不制造 409；缺密钥时每轮都写清楚原因并置不可用。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..config import clamp_backup_remote_interval
from .backup_remote import BackupRemoteService

logger = logging.getLogger("unified.backup.schedule")

TICK_SEC = 60


class BackupScheduleService:
    def __init__(self, settings: Any, db: Any, remote: BackupRemoteService | None = None) -> None:
        self._settings = settings
        self._db = db
        self._remote = remote or BackupRemoteService(settings, db)
        self._loop: asyncio.Task | None = None

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
            await asyncio.sleep(TICK_SEC)
            try:
                await self.tick()
            except Exception:  # 单轮失败不能让调度任务退出
                logger.exception("远端备份调度失败")

    # ---- 调度口径 ----

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings, "backup_remote_enabled", False))

    @property
    def interval_sec(self) -> int:
        raw = getattr(self._settings, "backup_remote_interval_sec", 86400)
        return clamp_backup_remote_interval(raw)

    async def tick(self, now: int | None = None) -> dict[str, Any]:
        """一次计划内的上传；关闭时什么都不做（手动端点不走这里）。"""
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "disabled"}
        now = int(time.time()) if now is None else int(now)

        # 缺密钥/密钥格式错是环境问题：每轮都复检，修好后自动解除阻塞
        problem = self._remote.note_key_problem(now)
        if problem is not None:
            logger.warning("定时远端备份缺密钥：%s", problem.get("message"))
            return {**problem, "skipped": True}

        state = self._remote.state()
        code = str(state.get("last_error_code") or "")
        just_unblocked = False
        if state.get("auth_blocked"):
            if code in {"key_missing", "key_invalid"}:
                self._remote.clear_block()
                state = self._remote.state()
                just_unblocked = True
            else:
                # 认证被拒是明确的停重试：等管理员改连接参数（save_config 会清这个位）
                return {"ok": False, "skipped": True, "reason": "auth_blocked", "message": state.get("last_error", "")}

        last = int(state.get("last_attempt_at") or 0)
        retry_at = int(state.get("next_retry_at") or 0)
        due = just_unblocked or (last == 0 or now - last >= self.interval_sec) or (retry_at > 0 and now >= retry_at)
        if not due:
            return {"ok": False, "skipped": True, "reason": "not_due"}

        create_before = bool(getattr(self._settings, "backup_remote_auto_upload", False))
        result = await asyncio.to_thread(self._remote.upload, create_before=create_before, now=now)
        if result.get("ok"):
            logger.info("定时远端备份完成：%s", result.get("object"))
        elif result.get("skipped"):
            logger.info("定时远端备份跳过：%s", result.get("message"))
        else:
            logger.warning("定时远端备份失败：%s", result.get("message"))
        return result
