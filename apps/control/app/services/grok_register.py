"""Grok 注册任务编排：队列、并发控制、状态机、失败重试与账号落库。

浏览器自动化不在主仓内实现；执行器通过 GROK_REGISTER_EXECUTOR_URL 指向外部
运行时，协议为 ``grok-register-executor/v1``（提交 / 轮询 / 取消，见
docs/providers/grok-register.md）。未配置执行器时任务以明确原因失败，不假成功。
"""

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from ..config import Settings
from ..db import Database

logger = logging.getLogger(__name__)

STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")

PROTOCOL = "grok-register-executor/v1"

# 执行器阶段 → 本端任务状态：pending/running 都在 running，done 才落 succeeded
EXECUTOR_TERMINAL_DONE = {"done", "succeeded"}
EXECUTOR_TERMINAL_FAILED = {"failed", "cancelled"}
EXECUTOR_ACTIVE = {"pending", "running"}

# 单次 HTTP 请求的超时上限；整任务总超时由 settings 控制
_REQUEST_TIMEOUT_CAP = 30.0


def _row(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _backoff_seconds(attempts: int) -> int:
    return min(5 * (2 ** max(attempts - 1, 0)), 60)


class ExecutorError(RuntimeError):
    """执行器三态失败：不可达 / 返回错误 / 超时，外加协议不合法。

    kind 只用于日志与测试断言，error 列存的是完整中文原因（前端原样展示）。
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class GrokRegisterService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._db = Database(settings.db_path)
        self._max_concurrency = max(1, settings.grok_register_max_concurrency)
        self._max_workers = max(1, settings.grok_register_max_workers)
        self._executor: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
        self._queue_loop: asyncio.Task | None = None
        self._running: dict[int, asyncio.Task] = {}
        # 执行器 job_id 映射；取消任务时用它回调执行器释放浏览器
        self._job_ids: dict[int, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        # 进程重启后 running 行没有协程在跑，先回收，避免任务停在 pending 假死
        recovered = self.recover_orphans()
        if recovered:
            logger.warning("回收了 %d 条中断的注册任务（控制面重启）", recovered)
        if self._queue_loop is None or self._queue_loop.done():
            self._queue_loop = asyncio.create_task(self._loop_run())

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
        self._loop = None

    async def _loop_run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("grok 注册队列调度失败")
            await asyncio.sleep(1)

    def recover_orphans(self) -> int:
        """把上一进程遗留的 running 行落回可用状态。

        有重试预算的重新排队（attempts 已在 claim 时+1），否则以明确原因失败；
        两种都写一条 orphaned 事件，详情抽屉能看到「重启中断」而不是无解释的卡死。
        """
        conn = self._db.connect()
        try:
            rows = conn.execute(
                "SELECT id, attempts, max_attempts FROM grok_tasks WHERE status='running'"
            ).fetchall()
        finally:
            conn.close()
        now = int(time.time())
        recovered = 0
        for row in rows:
            task_id = int(row["id"])
            exhausted = int(row["attempts"]) >= int(row["max_attempts"])
            reason = "控制面重启：任务执行被中断，执行器结果已丢失"
            conn = self._db.connect()
            try:
                if exhausted:
                    conn.execute(
                        "UPDATE grok_tasks SET status='failed', error=?, finished_at=? WHERE id=?",
                        (reason, now, task_id),
                    )
                else:
                    conn.execute(
                        "UPDATE grok_tasks SET status='queued', error=?, next_run_at=? WHERE id=?",
                        (reason, now, task_id),
                    )
                conn.commit()
            finally:
                conn.close()
            self._event(task_id, "orphaned", "failed" if exhausted else "ok", reason)
            recovered += 1
        return recovered

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
        # 本端取消后告诉执行器释放浏览器：本端不取消远端的话，远端会继续跑完整批
        self._dispatch_remote_cancel(task_id)
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
            "running": counts["running"],
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
            imported = self._land_accounts(task_id, result)
            # 落库条数写进结果，前端与排障都能看到「注册成功但没落账号」这种情况
            result = {**result, "imported_accounts": imported}
            conn = self._db.connect()
            try:
                conn.execute(
                    "UPDATE grok_tasks SET status='succeeded', result_json=?, error='', finished_at=? WHERE id=?",
                    (json.dumps(result, ensure_ascii=False, default=str), int(time.time()), task_id),
                )
                conn.commit()
            finally:
                conn.close()
            if imported:
                self._event(task_id, "accounts", detail=f"imported={imported}")
            self._event(task_id, "succeeded", detail="执行器返回结果并写入 result_json")
        except asyncio.CancelledError:
            self._mark_cancelled(task_id)
            raise
        except Exception as exc:
            current = self.get(task_id)
            if current["status"] == "cancelled":
                return
            kind = getattr(exc, "kind", "error")
            reason = str(exc)[:300]
            if int(current["attempts"]) < int(current["max_attempts"]):
                # 还有重试预算：退避后重新入队，状态机不进入 failed
                self._update(
                    task_id,
                    "queued",
                    error=reason,
                    next_run_at=int(time.time()) + _backoff_seconds(int(current["attempts"])),
                )
                self._event(task_id, "retry", "failed", f"[{kind}] {reason}")
            else:
                self._update(task_id, "failed", error=reason, finished_at=int(time.time()))
                self._event(task_id, "failed", "failed", f"[{kind}] {reason}")

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

    # ---- 执行器协议 v1 ----

    def _dispatch_remote_cancel(self, task_id: int) -> None:
        job_id = self._job_ids.get(task_id)
        if not job_id or not self._settings.grok_register_executor_url:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._cancel_remote(job_id), loop)

    async def _cancel_remote(self, job_id: str) -> None:
        url = f"{self._settings.grok_register_executor_url}/jobs/{quote(job_id, safe='')}/cancel"
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                await client.post(url)
        except Exception as exc:
            # 远端释放失败不能影响本端已落定的 cancelled 状态，只记一条告警
            logger.warning("通知执行器取消任务失败（job=%s）：%s", job_id, exc)

    async def _call_executor(self, task: dict[str, Any]) -> dict[str, Any]:
        if self._executor is not None:
            return await self._executor(task)
        url = self._settings.grok_register_executor_url
        if not url:
            raise ExecutorError(
                "unconfigured",
                "未配置注册执行器：设置 GROK_REGISTER_EXECUTOR_URL 指向浏览器注册运行时；"
                "主仓不内置浏览器自动化，任务不会假装成功",
            )
        timeout = float(self._settings.grok_register_executor_timeout_sec)
        per_request = max(5.0, min(timeout, _REQUEST_TIMEOUT_CAP))
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(per_request, connect=10.0), trust_env=False
        ) as client:
            job_id = await self._submit(client, url, task)
            self._job_ids[int(task["id"])] = job_id
            try:
                return await self._poll(client, url, job_id, task["id"], timeout)
            finally:
                self._job_ids.pop(int(task["id"]), None)

    async def _submit(self, client: httpx.AsyncClient, url: str, task: dict[str, Any]) -> str:
        body = {
            "protocol": PROTOCOL,
            "task_id": task["id"],
            "count": task["count"],
            "workers": task["workers"],
            "email_provider": task["email_provider"],
            "proxy": task["proxy"],
            "payload": json.loads(task["payload_json"] or "{}"),
        }
        try:
            response = await client.post(f"{url}/jobs", json=body)
        except httpx.RequestError as exc:
            raise ExecutorError("unreachable", f"注册执行器不可达（{url}）：{exc}") from exc
        if response.status_code >= 400:
            raise ExecutorError(
                "rejected",
                f"注册执行器拒绝提交（HTTP {response.status_code}）：{response.text[:200]}",
            )
        data = self._json_of(response, "提交")
        job_id = str(data.get("job_id") or "").strip()
        if not job_id:
            raise ExecutorError("protocol", "注册执行器响应缺少 job_id（协议 v1 要求创建后返回 job_id）")
        return job_id

    async def _poll(
        self, client: httpx.AsyncClient, url: str, job_id: str, task_id: int, timeout: float
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        interval = float(self._settings.grok_register_poll_interval_sec)
        phase = "pending"
        job_url = f"{url}/jobs/{quote(job_id, safe='')}"
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ExecutorError(
                    "timeout",
                    f"注册执行器超时（{timeout:g}s 内未完成，job={job_id} 最后阶段={phase}）",
                )
            try:
                response = await client.get(job_url)
            except httpx.RequestError as exc:
                raise ExecutorError("unreachable", f"注册执行器不可达（{url}）：{exc}") from exc
            if response.status_code == 404:
                raise ExecutorError("rejected", f"注册执行器上不存在任务：job={job_id}")
            if response.status_code >= 400:
                raise ExecutorError(
                    "rejected",
                    f"注册执行器查询失败（HTTP {response.status_code}）：{response.text[:200]}",
                )
            data = self._json_of(response, "查询")
            phase = str(data.get("status") or "").strip().lower()
            if phase in EXECUTOR_TERMINAL_DONE:
                if str(data.get("error") or "").strip():
                    raise ExecutorError("reported_failure", f"注册执行器报告失败：{data['error']}"[:300])
                return data
            if phase in EXECUTOR_TERMINAL_FAILED:
                reason = str(data.get("error") or "").strip() or "执行器未给出失败原因"
                raise ExecutorError("reported_failure", f"注册执行器报告失败：{reason}")
            if phase not in EXECUTOR_ACTIVE:
                raise ExecutorError("protocol", f"注册执行器返回未知阶段：{phase or '(空)'}")
            await asyncio.sleep(min(interval, max(left, 0.05)))

    @staticmethod
    def _json_of(response: httpx.Response, action: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ExecutorError("protocol", f"注册执行器{action}响应不是 JSON") from exc
        if not isinstance(data, dict):
            raise ExecutorError("protocol", f"注册执行器{action}响应不是 JSON 对象")
        return data

    # ---- 注册结果落库 ----

    def _land_accounts(self, task_id: int, result: dict[str, Any]) -> int:
        """把执行器返回的账号写入 accounts 表，cookie/token 镜像进统一凭据库。

        没有 uid 的条目跳过（无法去重也无法展示）；同 file 冲突时更新资料而不是插重复行。
        """
        raw = result.get("accounts")
        if not isinstance(raw, list):
            return 0
        now = int(time.time())
        saved = 0
        conn = self._db.connect()
        try:
            for item in raw[:1000]:
                if not isinstance(item, dict):
                    continue
                uid = str(item.get("uid") or "").strip()
                if not uid:
                    continue
                cookie = str(item.get("cookie") or "").strip()
                token = str(item.get("token") or "").strip()
                secret = cookie or token
                credential_id = 0
                if secret:
                    existing = conn.execute(
                        "SELECT id FROM provider_credentials WHERE provider=? AND secret=?",
                        ("grok-register", secret),
                    ).fetchone()
                    if existing is not None:
                        credential_id = int(existing["id"])
                    else:
                        cursor = conn.execute(
                            "INSERT INTO provider_credentials(provider, label, base_url, secret, enabled, "
                            "sort_order, note, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (
                                "grok-register",
                                f"任务 #{task_id} {uid}"[:64],
                                "",
                                secret,
                                1,
                                0,
                                f"注册任务 #{task_id} 产出（{item.get('nickname') or uid}）",
                                now,
                                now,
                            ),
                        )
                        credential_id = int(cursor.lastrowid)
                file = f"grok-register-{task_id}-{uid}"[:240]
                meta = {
                    "grok_task_id": task_id,
                    "credential_type": "cookie" if cookie else ("token" if token else ""),
                    "credential_id": credential_id,
                    "has_cookie": bool(cookie),
                    "has_token": bool(token),
                }
                expires_at = item.get("expires_at")
                try:
                    expires_int = int(expires_at or 0)
                except (TypeError, ValueError):
                    expires_int = 0
                credits = item.get("credits")
                conn.execute(
                    "INSERT INTO accounts(file, provider, uid, nickname, enterprise_id, realm, "
                    "expires_at, credits, note, disabled, meta_json, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(file) DO UPDATE SET nickname=excluded.nickname, "
                    "expires_at=excluded.expires_at, credits=excluded.credits, meta_json=excluded.meta_json",
                    (
                        file,
                        "grok-register",
                        uid,
                        str(item.get("nickname") or uid)[:64],
                        "",
                        str(item.get("realm") or "intl")[:16],
                        expires_int,
                        credits if isinstance(credits, (int, float)) else None,
                        f"注册任务 #{task_id} 产出"[:200],
                        0,
                        json.dumps(meta, ensure_ascii=False),
                        now,
                    ),
                )
                saved += 1
            conn.commit()
        finally:
            conn.close()
        return saved


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
