"""Muse 网页会话 cookie 池：多账号轮换、按上游回报续期、可选惰性保活。

寿命只认上游 cookies_exp：本地不推算、不补齐，避免把已到期或寿命未知的会话
当成可用账号继续打上游。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("unified.muse_session")

# 距到期不足一小时就该续期，留够一次请求+重试的余量
DUE_LEAD = 3600
DEFAULT_KEEPALIVE_INTERVAL = 300.0

_keepalive_task: asyncio.Task | None = None


def state_path(settings: Any = None) -> Path:
    root = os.getenv("UNIFIED_MUSE_STATE_DIR", "").strip()
    if not root:
        data_dir = getattr(settings, "data_dir", "") if settings is not None else ""
        root = str(data_dir or (Path(__file__).parent.parent / "data"))
    return Path(root) / "muse_sessions.json"


def fingerprint(cookie: str) -> str:
    legal = str(cookie or "")
    return hashlib.sha256(legal.encode("utf-8")).hexdigest()[:8]


def _now() -> int:
    return int(time.time())


def _exp_value(raw: Any) -> int | None:
    """只认正整数时间戳；0/负数/非数字都算「上游没给有效值」。"""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _usable(entry: dict[str, Any], now: int) -> bool:
    # cookies_exp <= 0 表示到期时间未知但会话本身还在，继续用它
    exp = int(entry.get("cookies_exp") or 0)
    return exp <= 0 or exp > now


def _parse_entry(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        cookie = raw.strip()
        if not cookie:
            return None
        return {"cookie": cookie, "cookies_exp": 0, "label": "", "updated_at": 0, "refresh_failures": 0}
    if isinstance(raw, dict):
        cookie = str(raw.get("cookie") or "").strip()
        if not cookie:
            return None
        return {
            "cookie": cookie,
            "cookies_exp": _exp_value(raw.get("cookies_exp")) or 0,
            "label": str(raw.get("label") or ""),
            "updated_at": 0,
            "refresh_failures": 0,
        }
    return None


def _load_state(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("Muse 会话状态文件损坏，按空池处理 path=%s", path)
        return []
    items = raw.get("entries") if isinstance(raw, dict) else None
    out: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        entry = _parse_entry(item)
        if entry is None:
            continue
        if isinstance(item, dict):
            entry["updated_at"] = int(item.get("updated_at") or 0)
            entry["refresh_failures"] = int(item.get("refresh_failures") or 0)
        out.append(entry)
    return out


def _merge_persisted(entry: dict[str, Any], persisted: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """配置条目只定「有哪些 cookie」；已落盘的到期时间是上游回报过的，绝不被配置值盖小。"""
    old = persisted.get(entry["cookie"])
    if not old:
        return entry
    persisted_exp = int(old.get("cookies_exp") or 0)
    if persisted_exp > int(entry.get("cookies_exp") or 0):
        entry["cookies_exp"] = persisted_exp
    entry["label"] = entry.get("label") or str(old.get("label") or "")
    entry["updated_at"] = int(old.get("updated_at") or 0)
    entry["refresh_failures"] = int(old.get("refresh_failures") or 0)
    return entry


class SessionPool:
    def __init__(self, entries: Iterable[dict[str, Any]], path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._entries = list(entries)
        self._cursor = 0
        self._lock = asyncio.Lock()

    @classmethod
    def from_options(cls, options: dict[str, Any] | None, settings: Any = None) -> "SessionPool":
        options = options if isinstance(options, dict) else {}
        path = state_path(settings)
        persisted = {entry["cookie"]: entry for entry in _load_state(path)}

        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        raw_pool = options.get("cookie_pool")
        for raw in raw_pool if isinstance(raw_pool, list) else []:
            entry = _parse_entry(raw)
            if entry is None or entry["cookie"] in seen:
                continue
            seen.add(entry["cookie"])
            entries.append(_merge_persisted(entry, persisted))

        if not entries:
            cookie = str(options.get("cookie") or "").strip()
            if cookie:
                entry = {"cookie": cookie, "cookies_exp": 0, "label": "", "updated_at": 0, "refresh_failures": 0}
                entries.append(_merge_persisted(entry, persisted))

        return cls(entries, path)

    @property
    def entries(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._entries]

    def pick(self, now: int | None = None) -> str:
        stamp = _now() if now is None else int(now)
        live = [entry for entry in self._entries if _usable(entry, stamp)]
        if not live:
            return ""
        chosen = live[self._cursor % len(live)]
        self._cursor = (self._cursor + 1) % len(live)
        return str(chosen["cookie"])

    def due_entries(self, lead: int = DUE_LEAD, now: int | None = None) -> list[dict[str, Any]]:
        stamp = _now() if now is None else int(now)
        return [
            entry for entry in self._entries
            if 0 < (int(entry.get("cookies_exp") or 0) - stamp) <= lead
        ]

    async def apply_expiry(self, cookie: str, upstream_exp: Any) -> bool:
        """上游回报的到期时间才写入；相同或更小的值不动，缺值只记一次失败。"""
        target = str(cookie or "")
        async with self._lock:
            entry = next((item for item in self._entries if item["cookie"] == target), None)
            if entry is None:
                return False
            exp = _exp_value(upstream_exp)
            if exp is None:
                entry["refresh_failures"] = int(entry.get("refresh_failures") or 0) + 1
                await self._save()
                return False
            old_exp = int(entry.get("cookies_exp") or 0)
            if exp <= old_exp:
                logger.info("Muse 会话续期未延长 fp=%s old=%s new=%s", fingerprint(target), old_exp, exp)
                return False
            entry["cookies_exp"] = exp
            entry["updated_at"] = _now()
            await self._save()
            return True

    def describe(self, now: int | None = None) -> dict[str, Any]:
        stamp = _now() if now is None else int(now)
        latest = max((int(entry.get("updated_at") or 0) for entry in self._entries), default=0)
        return {
            "entries": len(self._entries),
            "updated_at": latest,
            "due": len(self.due_entries(now=stamp)),
            # 健康度只给指纹，cookie 明文不外泄
            "fingerprints": [fingerprint(str(entry.get("cookie") or "")) for entry in self._entries],
        }

    def _write(self) -> None:
        if self.path is None:
            return
        payload = {
            "entries": [
                {
                    "cookie": str(entry["cookie"]),
                    "cookies_exp": int(entry.get("cookies_exp") or 0),
                    "label": str(entry.get("label") or ""),
                    "updated_at": int(entry.get("updated_at") or 0),
                    "refresh_failures": int(entry.get("refresh_failures") or 0),
                }
                for entry in self._entries
            ]
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    async def _save(self) -> None:
        if self.path is None:
            return
        try:
            await asyncio.to_thread(self._write)
        except OSError as exc:
            logger.warning("Muse 会话状态写入失败 path=%s error=%s", self.path, exc)


CookiePool = SessionPool


async def keepalive(pool: SessionPool, adapter: Any, *, now: int | None = None) -> dict[str, int]:
    due = pool.due_entries(now=now)
    stats = {"checked": len(due), "refreshed": 0, "failed": 0}
    for entry in due:
        cookie = str(entry["cookie"])
        try:
            upstream_exp = await adapter.cookie_refresh(cookie)
        except Exception as exc:  # 单条失败只记账，不能让整轮保活中断
            logger.warning("Muse 会话续期异常 fp=%s error=%s", fingerprint(cookie), exc)
            upstream_exp = 0
        if await pool.apply_expiry(cookie, upstream_exp):
            stats["refreshed"] += 1
        else:
            stats["failed"] += 1
    return stats


async def _keepalive_forever(pool: SessionPool, adapter: Any, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await keepalive(pool, adapter)
        except Exception:  # 保活是旁路，任何异常都只进日志
            logger.warning("Muse 会话保活单轮失败", exc_info=True)


def ensure_keepalive_loop(
    pool: SessionPool,
    adapter: Any,
    options: dict[str, Any] | None = None,
) -> asyncio.Task | None:
    """按配置惰性起一次保活循环；没有事件循环时静默跳过。"""
    global _keepalive_task
    opts = options if isinstance(options, dict) else {}
    if not opts.get("cookie_keepalive"):
        return _keepalive_task
    if not str(opts.get("cookie_refresh_path") or "").strip():
        return _keepalive_task
    if _keepalive_task is not None and not _keepalive_task.done():
        return _keepalive_task
    try:
        interval = max(1.0, float(opts.get("cookie_keepalive_interval") or DEFAULT_KEEPALIVE_INTERVAL))
    except (TypeError, ValueError):
        interval = DEFAULT_KEEPALIVE_INTERVAL
    try:
        _keepalive_task = asyncio.create_task(_keepalive_forever(pool, adapter, interval))
    except RuntimeError:
        # 没有运行中的事件循环（脚本/同步调用），交给下一次 health 再挑
        _keepalive_task = None
    return _keepalive_task
