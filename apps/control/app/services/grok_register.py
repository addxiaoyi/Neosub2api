"""Grok 注册任务编排：队列、并发控制、状态机、失败重试。

浏览器自动化不在主仓内实现；执行器通过 GROK_REGISTER_EXECUTOR_URL 指向外部
运行时，未配置时任务以明确原因失败。协议与状态机见 docs/providers/grok-register.md。
"""

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

import httpx

from ..config import Settings
from ..db import Database

logger = logging.getLogger(__name__)

STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


def _row(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _backoff_seconds(attempts: int) -> int:
    return min(5 * (2 ** max(attempts - 1, 0)), 60)


class GrokRegisterService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._db = Database(settings.db_path)
        self._max_concurrency = max(1, settings.grok_register_max_concurrency)
        self._max_workers = max(1, settings.grok_register_max_workers)
        self._executor: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
        self._queue_loop: asyncio.Task | None = None
        self._running: dict[int, asyncio.Task] = {}

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._queue_loop is None or self._queue_loop.done():
            self._queue_loop = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._queue_loop is not None:
            self._queue_loop.cancel()
            try:
                await self._queue_loop
            except asyncio.CancelledError:
                pass
            self._queue_loop = None
        for task in list(self._running.values()):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("grok 注册队列调度失败")
            await asyncio.sleep(1)

    # ---- 任务 CRUD ----

    def enqueue(
        self,
        *,
        count: int = 1,
        workers: int = 1,
        email_provider: str = "",
        proxy: str = "",
        max_attempts: int = 3,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        count = max(1, min(int(count), 1000))
        workers = max(1, min(int(workers), self._max_workers))
        max_attempts = max(1, min(int(max_attempts), 5))
        conn = self._db.connect()
        try:
            cursor = conn.execute(
                "INSERT INTO grok_tasks(status, count, workers, email_provider, proxy, payload_json, "
                "attempts, max_attempts, created_at, next_run_at) VALUES('queued',?,?,?,?,?,0,?,?,?)",
                (
                    count,
                    workers,
                    email_provider.strip(),
                    proxy.strip(),
                    json.dumps(payload or {}, ensure_ascii=False),
                    max_attempts,
                    int(time.time()),
                    int(time.time()),
                ),
            )
            conn.commit()
            task_id = int(cursor.lastrowid)
        finally:
            conn.close()
        self._event(task_id, "queued", detail=f"count={count} workers={workers}")
        return self.get(task_id)

    def get(self, task_id: int) -> dict[str, Any]:
        conn = self._db.connect()
        try:
            row = conn.execute("SELECT * FROM grok_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"未知的注册任务：{task_id}")
            return _row(row)
        finally:
            conn.close()

    def detail(self, task_id: int) -> dict[str, Any]:
        """任务详情：原始行 + 结构化步骤 + 时间线。

        步骤来自 grok_task_events（状态机每次流转都落一条），不是按时间戳倒推的；
        事件里只有步骤/状态/原因，proxy 与 payload 只在任务行里，沿用 admin-only 口径。
        """
        task = self.get(task_id)
        conn = self._db.connect()
        try:
            rows = conn.execute(
                "SELECT ts, step, status, detail FROM grok_task_events "
                "WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
        return {
            "task": task,
            "status": task["status"],
            "error": task["error"],
            "steps": [_row(row) for row in rows],
            "timeline": {
                "created_at": task["created_at"],
                "started_at": task["started_at"],
                "finished_at": task["finished_at"],
                "next_run_at": task["next_run_at"],
            },
        }

    def _event(self, task_id: int, step: str, status: str = "ok", detail: str = "") -> None:
        conn = self._db.connect()
        try:
            conn.execute(
                "INSERT INTO grok_task_events(task_id, ts, step, status, detail) VALUES(?,?,?,?,?)",
                (task_id, int(time.time()), step, status, detail[:300]),
            )
            conn.commit()
        finally:
            conn.close()

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM grok_tasks ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)
            ).fetchall()
            return [_row(row) for row in rows]
        finally:
            conn.close()

    def cancel(self, task_id: int) -> dict[str, Any]:
        row = self.get(task_id)
        if row["status"] not in {"queued", "running"}:
            raise ValueError(f"任务状态为 {row['status']}，不能取消")
        conn = self._db.connect()
        try:
            cursor = conn.execute(
                "UPDATE grok_tasks SET status='cancelled', finished_at=? WHERE id=?", (int(time.time()), task_id)
            )
            conn.commit()
            changed = cursor.rowcount == 1
        finally:
            conn.close()
        task = self._running.get(task_id)
        if task is not None:
            task.cancel()
        if changed:
            self._event(task_id, "cancelled")
        return self.get(task_id)

    def retry(self, task_id: int) -> dict[str, Any]:
        row = self.get(task_id)
        if row["status"] not in {"failed", "cancelled"}:
            raise ValueError(f"任务状态为 {row['status']}，不能重试")
        conn = self._db.connect()
        try:
            conn.execute(
                "UPDATE grok_tasks SET status='queued', attempts=0, error='', result_json='', "
                "finished_at=NULL, next_run_at=? WHERE id=?",
                (int(time.time()), task_id),
            )
            conn.commit()
        finally:
            conn.close()
        self._event(task_id, "retried", detail="重置为 queued，等待重新调度")
        return self.get(task_id)

    def stats(self) -> dict[str, Any]:
        conn = self._db.connect()
        try:
            counts = {status: 0 for status in STATUSES}
            for row in conn.execute("SELECT status, COUNT(*) AS n FROM grok_tasks GROUP BY status").fetchall():
                counts[row["status"]] = int(row["n"])
        finally:
            conn.close()
        return {
            "queued": counts["queued"],
            "running": len(self._running),
            "succeeded": counts["succeeded"],
            "failed": counts["failed"],
            "cancelled": counts["cancelled"],
            "max_concurrency": self._max_concurrency,
            "max_workers": self._max_workers,
            "executor": "http" if self._settings.grok_register_executor_url else "unconfigured",
        }

    # ---- 调度 ----

    def set_executor(self, executor: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> None:
        self._executor = executor

    async def run_once(self) -> int:
        slots = self._max_concurrency - len(self._running)
        if slots <= 0:
            return 0
        conn = self._db.connect()
        try:
            rows = conn.execute(
                "SELECT id FROM grok_tasks WHERE status='queued' AND next_run_at <= ? "
                "ORDER BY id LIMIT ?",
                (int(time.time()), slots),
            ).fetchall()
        finally:
            conn.close()
        launched = 0
        for row in rows:
            if not self._claim(int(row["id"])):
                continue
            task = asyncio.create_task(self._execute(int(row["id"])))
            self._running[int(row["id"])] = task
            task.add_done_callback(lambda finished, tid=int(row["id"]): self._running.pop(tid, None))
            launched += 1
        return launched

    def _claim(self, task_id: int) -> bool:
        conn = self._db.connect()
        try:
            cursor = conn.execute(
                "UPDATE grok_tasks SET status='running', attempts=attempts+1, started_at=?, error='' "
                "WHERE id=? AND status='queued'",
                (int(time.time()), task_id),
            )
            conn.commit()
            claimed = cursor.rowcount == 1
        finally:
            conn.close()
        if claimed:
            self._event(task_id, "running", detail="执行器接手")
        return claimed

    async def _execute(self, task_id: int) -> None:
        task = self.get(task_id)
        try:
            result = await self._call_executor(task)
            if self.get(task_id)["status"] == "cancelled":
                return
            conn = self._db.connect()
            try:
                conn.execute(
                    "UPDATE grok_tasks SET status='succeeded', result_json=?, error='', finished_at=? WHERE id=?",
                    (json.dumps(result, ensure_ascii=False, default=str), int(time.time()), task_id),
                )
                conn.commit()
            finally:
                conn.close()
            self._event(task_id, "succeeded", detail="执行器返回结果并写入 result_json")
        except asyncio.CancelledError:
            self._mark_cancelled(task_id)
            raise
        except Exception as exc:
            current = self.get(task_id)
            if current["status"] == "cancelled":
                return
            if int(current["attempts"]) < int(current["max_attempts"]):
                # 还有重试预算：退避后重新入队，状态机不进入 failed
                self._update(
                    task_id,
                    "queued",
                    error=str(exc)[:300],
                    next_run_at=int(time.time()) + _backoff_seconds(int(current["attempts"])),
                )
                self._event(task_id, "retry", "failed", str(exc)[:300])
            else:
                self._update(task_id, "failed", error=str(exc)[:300], finished_at=int(time.time()))
                self._event(task_id, "failed", "failed", str(exc)[:300])

    def _mark_cancelled(self, task_id: int) -> None:
        conn = self._db.connect()
        try:
            cursor = conn.execute(
                "UPDATE grok_tasks SET status='cancelled', finished_at=? WHERE id=? AND status='running'",
                (int(time.time()), task_id),
            )
            conn.commit()
            changed = cursor.rowcount == 1
        finally:
            conn.close()
        if changed:
            self._event(task_id, "cancelled")

    def _update(self, task_id: int, status: str, **fields: Any) -> None:
        allowed = {"error", "next_run_at", "finished_at"}
        sets = ["status = ?"]
        values: list[Any] = [status]
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            values.append(value)
        conn = self._db.connect()
        try:
            conn.execute(f"UPDATE grok_tasks SET {', '.join(sets)} WHERE id = ?", (*values, task_id))
            conn.commit()
        finally:
            conn.close()

    async def _call_executor(self, task: dict[str, Any]) -> dict[str, Any]:
        if self._executor is not None:
            return await self._executor(task)
        url = self._settings.grok_register_executor_url
        if not url:
            raise RuntimeError(
                "未配置注册执行器：设置 GROK_REGISTER_EXECUTOR_URL 指向浏览器注册运行时；"
                "主仓不内置浏览器自动化，任务不会假装成功"
            )
        body = {
            "task_id": task["id"],
            "count": task["count"],
            "workers": task["workers"],
            "email_provider": task["email_provider"],
            "proxy": task["proxy"],
            "payload": json.loads(task["payload_json"] or "{}"),
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=15.0), trust_env=False) as client:
            try:
                response = await client.post(url, json=body)
            except httpx.RequestError as exc:
                raise RuntimeError(f"注册执行器不可达（{url}）：{exc}") from exc
            if response.status_code >= 400:
                raise RuntimeError(f"注册执行器返回 {response.status_code}：{response.text[:200]}")
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError("注册执行器返回的不是 JSON") from exc
            return data if isinstance(data, dict) else {"result": data}


def queue_stats(db_path) -> dict[str, Any]:
    """给 provider 健康检查用的只读统计（不启动调度器）。"""
    db = Database(db_path)
    conn = db.connect()
    try:
        counts = {status: 0 for status in STATUSES}
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM grok_tasks GROUP BY status").fetchall():
            counts[row["status"]] = int(row["n"])
    finally:
        conn.close()
    return {"queued": counts["queued"], "running": counts["running"], "failed": counts["failed"]}
