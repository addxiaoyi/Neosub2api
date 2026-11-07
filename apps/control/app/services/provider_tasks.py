"""管理台任务运行台账（taskrun / tasklog）。

只落盘「谁在什么时候跑了什么、结果计数多少、错误脱敏摘要」，
不保存上游原始响应，避免把账号资料或地址带进日志。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..api_utils import mask_text, redact_endpoints

MAX_RUNS = 150
_LOCK = threading.Lock()


def _clean(value: Any, limit: int = 240) -> str:
    return redact_endpoints(mask_text(str(value or "")))[:limit]


def _sanitize(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return None
    if isinstance(value, str):
        return _clean(value, 200)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key)[:48]: _sanitize(item, depth + 1) for key, item in list(value.items())[:25]}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth + 1) for item in list(value)[:20]]
    return _clean(value, 200)


def _path(data_dir: str) -> Path:
    return Path(data_dir) / "provider_task_runs.json"


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _save(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def record(
    data_dir: str,
    provider: str,
    task: str,
    *,
    status: str,
    actor: str,
    stats: dict[str, Any] | None = None,
    message: str = "",
    started_at: int | None = None,
    finished_at: int | None = None,
) -> dict[str, Any]:
    run = {
        "id": uuid.uuid4().hex[:12],
        "provider": provider,
        "task": task,
        "status": status,
        "actor": str(actor or "")[:64],
        "stats": _sanitize(stats or {}),
        "message": _clean(message, 300),
        "started_at": int(started_at or time.time()),
        "finished_at": int(finished_at or time.time()),
    }
    path = _path(data_dir)
    with _LOCK:
        items = [item for item in _load(path) if item.get("id") != run["id"]]
        items.insert(0, run)
        _save(path, items[:MAX_RUNS])
    return run


def list_runs(data_dir: str, provider: str, limit: int = 20) -> list[dict[str, Any]]:
    path = _path(data_dir)
    with _LOCK:
        items = _load(path)
    matched = [item for item in items if item.get("provider") == provider]
    return matched[: max(1, min(int(limit), 50))]


def task_types(provider: str) -> tuple[str, ...]:
    if provider == "codebuddy":
        return ("checkin_all", "refresh_credits")
    return ()
