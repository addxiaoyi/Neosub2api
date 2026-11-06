"""本地媒体任务队列与可编辑文件任务归属台账。

上游媒体子工程的队列（/api/tasks）是单进程内存实现，网关不能直接复用；这里做一层
本地 FIFO：单 worker、queued/running/completed/failed/cancelled 五态，取消只对未终态生效。
任务与归属只存 JSON 元数据，二进制产物留在上游，网关不落盘。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger("unified.media_tasks")

RETENTION_SECONDS = 7 * 24 * 3600
MAX_RECORDS = 200
TERMINAL = frozenset({"completed", "failed", "cancelled"})
TASK_KINDS = ("editable_file", "video")
POLL_INTERVAL = 2.0
POLL_LIMIT = 120.0

Runner = Callable[[str, dict[str, Any], Callable[[], bool]], Awaitable[dict[str, Any]]]


def state_path(settings: Any) -> Path:
    root = os.getenv("UNIFIED_MEDIA_STATE_DIR") or str(getattr(settings, "data_dir", "."))
    return Path(root) / "gptgrok_media_state.json"


def _now() -> int:
    return int(time.time())


class MediaStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._editable: dict[str, dict[str, Any]] = {}
        self._runners: dict[str, Runner] = {}
        self._default_runner: Runner | None = None
        self._worker: asyncio.Task | None = None
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("媒体任务状态文件损坏，按空台账处理 path=%s", self.path)
            return
        tasks = raw.get("tasks") if isinstance(raw, dict) else None
        editable = raw.get("editable") if isinstance(raw, dict) else None
        for item in tasks if isinstance(tasks, list) else []:
            tid = str(item.get("id") or "")
            if not tid:
                continue
            rec = dict(item)
            # 进程重启后没有 worker 接着跑，running 只能判失败，不能假装还在跑
            if rec.get("status") == "running":
                rec["status"] = "failed"
                rec["error"] = "interrupted"
                rec["updated_at"] = _now()
            self._tasks[tid] = rec
        for item in editable if isinstance(editable, list) else []:
            tid = str(item.get("id") or "")
            if tid:
                self._editable[tid] = dict(item)
        self._prune()

    def _prune(self) -> None:
        cutoff = _now() - RETENTION_SECONDS
        self._tasks = {
            tid: rec for tid, rec in self._tasks.items()
            if int(rec.get("updated_at") or 0) >= cutoff
        }
        self._editable = {
            tid: rec for tid, rec in self._editable.items()
            if int(rec.get("updated_at") or 0) >= cutoff
        }
        for bucket in (self._tasks, self._editable):
            if len(bucket) > MAX_RECORDS:
                keep = sorted(
                    bucket.items(), key=lambda kv: int(kv[1].get("updated_at") or 0), reverse=True
                )[:MAX_RECORDS]
                bucket.clear()
                bucket.update(keep)

    def _write(self) -> None:
        payload = {"version": 1, "tasks": list(self._tasks.values()),
                   "editable": list(self._editable.values())}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    async def _save(self) -> None:
        self._prune()
        try:
            await asyncio.to_thread(self._write)
        except OSError as exc:
            # 状态落盘失败不能拖垮请求，但必须留证据
            logger.warning("媒体任务状态写入失败 path=%s error=%s", self.path, exc)

    # ---------- 2-A5 队列 ----------

    def _position(self, tid: str) -> int:
        rec = self._tasks.get(tid)
        if not rec or rec.get("status") != "queued":
            return 0
        older = [
            other for other in self._tasks.values()
            if other.get("status") == "queued"
            and (int(other.get("created_at") or 0), other["id"]) < (int(rec.get("created_at") or 0), tid)
        ]
        return len(older) + 1

    def _task_view(self, rec: dict[str, Any]) -> dict[str, Any]:
        view: dict[str, Any] = {
            "id": rec["id"],
            "object": "task",
            "kind": rec.get("kind"),
            "status": rec.get("status"),
            "queue_position": self._position(rec["id"]),
            "created_at": int(rec.get("created_at") or 0),
            "updated_at": int(rec.get("updated_at") or 0),
        }
        if rec.get("result") is not None:
            view["result"] = rec["result"]
        if rec.get("error"):
            view["error"] = rec["error"]
        return view

    async def submit(
        self, *, owner: str, kind: str, payload: dict[str, Any], runner: Runner
    ) -> dict[str, Any]:
        async with self._lock:
            now = _now()
            tid = f"task_{secrets.token_hex(8)}"
            rec = {
                "id": tid,
                "owner": owner,
                "kind": kind,
                "payload": payload,
                "status": "queued",
                "result": None,
                "error": "",
                "created_at": now,
                "updated_at": now,
            }
            self._tasks[tid] = rec
            self._runners[tid] = runner
            self._default_runner = runner
            await self._save()
            view = self._task_view(rec)
        self._ensure_worker()
        return view

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        try:
            self._worker = asyncio.create_task(self._drain())
        except RuntimeError:
            # 没有运行中的事件循环（只可能出现在直调测试），交给下次提交再启
            self._worker = None

    def _owned(self, owner: str, tid: str) -> dict[str, Any] | None:
        rec = self._tasks.get(tid)
        if not rec or rec.get("owner") != owner:
            return None
        return rec

    async def list_tasks(self, owner: str) -> list[dict[str, Any]]:
        async with self._lock:
            items = [rec for rec in self._tasks.values() if rec.get("owner") == owner]
        items.sort(key=lambda rec: int(rec.get("created_at") or 0), reverse=True)
        return [self._task_view(rec) for rec in items]

    async def get_task(self, owner: str, tid: str) -> dict[str, Any] | None:
        async with self._lock:
            rec = self._owned(owner, tid)
            return self._task_view(rec) if rec else None

    async def cancel(self, owner: str, tid: str) -> dict[str, Any] | None:
        async with self._lock:
            rec = self._owned(owner, tid)
            if not rec or rec.get("status") in TERMINAL:
                return None
            rec["status"] = "cancelled"
            rec["updated_at"] = _now()
            await self._save()
            return {"cancelled": True, "id": tid}

    def _is_cancelled(self, tid: str) -> bool:
        rec = self._tasks.get(tid)
        return bool(rec) and rec.get("status") == "cancelled"

    async def _finish(self, tid: str, *, result: Any = None, error: str = "") -> None:
        async with self._lock:
            rec = self._tasks.get(tid)
            if not rec:
                return
            if rec.get("status") == "cancelled":
                # 取消后不再写结果，保持 cancelled 为终态
                rec["updated_at"] = _now()
                await self._save()
                return
            status = str((result or {}).get("status") or "") if isinstance(result, dict) else ""
            if error:
                rec["status"] = "failed"
                rec["error"] = error
            elif status in ("failed", "cancelled"):
                rec["status"] = status
                rec["error"] = rec.get("error") or (result or {}).get("error") or ""
            else:
                rec["status"] = "completed"
                rec["result"] = result
            rec["updated_at"] = _now()
            await self._save()

    async def _drain(self) -> None:
        while True:
            async with self._lock:
                queued = [rec for rec in self._tasks.values() if rec.get("status") == "queued"]
                queued.sort(key=lambda rec: (int(rec.get("created_at") or 0), rec["id"]))
                if not queued:
                    self._worker = None
                    return
                rec = queued[0]
                rec["status"] = "running"
                rec["updated_at"] = _now()
                await self._save()
                runner = self._runners.pop(rec["id"], None) or self._default_runner
            if runner is None:
                await self._finish(rec["id"], error="runner_unavailable")
                continue
            try:
                result = await runner(rec["kind"], rec.get("payload") or {}, lambda tid=rec["id"]: self._is_cancelled(tid))
            except Exception as exc:  # 单个任务失败不拖垮队列
                logger.warning("媒体任务执行失败 id=%s error=%s", rec["id"], exc, exc_info=True)
                await self._finish(rec["id"], error=str(exc) or exc.__class__.__name__)
            else:
                await self._finish(rec["id"], result=result)

    # ---------- 2-A3 归属台账 ----------

    def _editable_view(self, rec: dict[str, Any]) -> dict[str, Any]:
        view = dict(rec.get("view") or {})
        view["id"] = rec["id"]
        view["updated_at"] = int(rec.get("updated_at") or 0)
        return view

    async def own_editable(self, owner: str, task_id: str, view: dict[str, Any]) -> None:
        async with self._lock:
            self._editable[task_id] = {
                "id": task_id,
                "owner": owner,
                "updated_at": _now(),
                "view": view,
            }
            await self._save()

    async def editable_owner(self, task_id: str) -> str:
        async with self._lock:
            rec = self._editable.get(task_id)
            return str(rec.get("owner") or "") if rec else ""

    async def editable_view(self, owner: str, task_id: str) -> dict[str, Any] | None:
        async with self._lock:
            rec = self._editable.get(task_id)
            if not rec or rec.get("owner") != owner:
                return None
            return self._editable_view(rec)

    async def refresh_editable(self, owner: str, task_id: str, view: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._editable[task_id] = {
                "id": task_id,
                "owner": owner,
                "updated_at": _now(),
                "view": view,
            }
            await self._save()
            return self._editable_view(self._editable[task_id])

    async def owned_editable_ids(self, owner: str) -> set[str]:
        async with self._lock:
            return {
                tid for tid, rec in self._editable.items() if rec.get("owner") == owner
            }

    async def owned_editable_views(self, owner: str) -> list[dict[str, Any]]:
        async with self._lock:
            items = [rec for rec in self._editable.values() if rec.get("owner") == owner]
        items.sort(key=lambda rec: int(rec.get("updated_at") or 0), reverse=True)
        return [self._editable_view(rec) for rec in items[:MAX_RECORDS]]
