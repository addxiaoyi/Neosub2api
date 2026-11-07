"""ZCode 周末额度自动领取：状态落盘、同周去重、严格成功判定。

未确认上游端点前不臆造默认路径（options.claim_path 为空即整体 no-op）；关闭
weekend_claim 时本服务不读状态、不发请求、不建目录，与未接入前逐字节一致。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings

logger = logging.getLogger("unified.zcode")

# 上游额度按北京时间结算；进程所在时区不应影响「周末」与「时段」判定
CST = datetime.timezone(datetime.timedelta(hours=8))

DEFAULTS: dict[str, Any] = {
    # datetime.weekday() 口径：0=周一 … 5=周六、6=周日
    "weekend_days": (5, 6),
    "window": "00:00-23:59",
    "claim_window": "sat,sun",
    "claim_hours": "00:00-23:59",
}

STATE_FILE = "zcode_claims.json"
CLAIM_TIMEOUT = 20.0

_DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _clock(now: datetime.datetime | None) -> datetime.datetime:
    if now is None:
        return datetime.datetime.now(CST)
    # 注入的 naive 时间按北京时间解释，测试不必关心本机时区
    return now.replace(tzinfo=CST) if now.tzinfo is None else now.astimezone(CST)


def _stamp(now: datetime.datetime | None) -> str:
    iso = _clock(now).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _fingerprint(token: str) -> str:
    """只落指纹：状态文件即使泄露也换不回账号 token。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _claim_path(options: dict[str, Any]) -> str:
    raw = str(options.get("claim_path") or "").strip()
    if not raw:
        return ""
    return raw if raw.startswith("/") else f"/{raw}"


def _days(raw: Any) -> tuple[int, ...]:
    text = str(raw or "").strip().lower()
    if not text:
        return tuple(DEFAULTS["weekend_days"])
    parsed: set[int] = set()
    for part in text.replace("，", ",").split(","):
        token = part.strip()
        if not token:
            continue
        if token.isdigit():
            value = int(token)
        elif token[:3] in _DAY_NAMES:
            value = _DAY_NAMES[token[:3]]
        else:
            return tuple(DEFAULTS["weekend_days"])
        if not 0 <= value <= 6:
            return tuple(DEFAULTS["weekend_days"])
        parsed.add(value)
    return tuple(sorted(parsed)) if parsed else tuple(DEFAULTS["weekend_days"])


def _hours(raw: Any) -> tuple[datetime.time, datetime.time]:
    text = str(raw or DEFAULTS["window"]).strip()
    try:
        start_raw, end_raw = (part.strip() for part in text.split("-", 1))
        start = datetime.time.fromisoformat(start_raw)
        end = datetime.time.fromisoformat(end_raw)
    except (TypeError, ValueError):
        start, end = datetime.time(0, 0), datetime.time(23, 59)
    return start, end


def _in_window(moment: datetime.time, start: datetime.time, end: datetime.time) -> bool:
    # 允许跨零点窗口（如 22:00-02:00）
    if start <= end:
        return start <= moment <= end
    return moment >= start or moment <= end


def _granted(data: dict[str, Any]) -> bool:
    """只有显式领取信号才算成功；2xx 本身不代表额度到账。"""
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    for holder in (data, inner):
        if holder.get("claimed") is True:
            return True
        for field in ("amount", "quota", "credits"):
            value = holder.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if value > 0:
                return True
    return False


def _state_dir(settings: Settings | None) -> Path:
    env = os.getenv("UNIFIED_ZCODE_STATE_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    data_dir = getattr(settings, "data_dir", None) if settings is not None else None
    if data_dir:
        return Path(data_dir)
    return Path(__file__).resolve().parents[1] / "data"


class ZcodeClaimService:
    """并发度固定为 1：领取会动真实账号额度，串行 + 周内去重比吞吐重要。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        adapter: Any = None,
        state_dir: str | Path | None = None,
    ) -> None:
        self._adapter = adapter
        self._state_dir = Path(state_dir) if state_dir else _state_dir(settings)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._missing_path_warned = False

    @property
    def state_path(self) -> Path:
        return self._state_dir / STATE_FILE

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"claimed": {}}
        claimed = data.get("claimed") if isinstance(data, dict) else None
        return {"claimed": claimed if isinstance(claimed, dict) else {}}

    def _write_state(self, state: dict[str, Any]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        target = self.state_path
        tmp = target.with_name(f"{target.name}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)

    def describe(self, now: datetime.datetime | None = None, *, adapter: Any = None) -> dict[str, Any]:
        """管理台只读快照：开关、端点是否配置、本轮是否该领、已领周次与指纹。"""
        target = adapter if adapter is not None else self._adapter
        options = (getattr(target, "options", None) or {}) if target is not None else {}
        state = self._read_state()
        claimed = state["claimed"]
        stamps = [str(item) for item in claimed.values() if isinstance(item, str)]
        return {
            "enabled": _as_bool(options.get("weekend_claim"), False),
            "path_configured": bool(_claim_path(options)),
            "due": self.due(now, adapter=target),
            "claimed_count": len(claimed),
            "last_week": max(stamps) if stamps else "",
            "entries": [
                {"fingerprint": fingerprint, "week": str(week)[:32]}
                for fingerprint, week in list(claimed.items())[:20]
            ],
        }

    def due(self, now: datetime.datetime | None = None, *, adapter: Any = None) -> bool:
        target = adapter if adapter is not None else self._adapter
        if target is None:
            return False
        options = getattr(target, "options", None) or {}
        if not _as_bool(options.get("weekend_claim"), False):
            return False
        if not _claim_path(options):
            return False
        moment = _clock(now)
        if moment.weekday() not in _days(options.get("claim_window", DEFAULTS["claim_window"])):
            return False
        start, end = _hours(options.get("claim_hours", DEFAULTS["claim_hours"]))
        return _in_window(moment.time(), start, end)

    def _path_blind(self, options: dict[str, Any]) -> bool:
        """开了开关却没配端点：只说明一次，避免每次健康巡检刷日志。"""
        if not _as_bool(options.get("weekend_claim")) or _claim_path(options):
            self._missing_path_warned = False
            return False
        if not self._missing_path_warned:
            logger.warning("ZCode 周末领取已开启但未配置 claim_path：上游端点未确认，不发送任何请求")
            self._missing_path_warned = True
        return True

    async def maybe_start(self, adapter: Any = None) -> None:
        target = adapter if adapter is not None else self._adapter
        if target is None:
            return
        if self._path_blind(getattr(target, "options", None) or {}):
            return
        if not self.due(adapter=target):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(target))
        self._task.add_done_callback(self._clear_task)

    def _clear_task(self, done: asyncio.Task) -> None:
        if self._task is done:
            self._task = None

    async def run(self, adapter: Any = None, *, now: datetime.datetime | None = None) -> dict[str, Any]:
        try:
            return await self._run(adapter, now=now)
        finally:
            self._task = None

    async def _run(self, adapter: Any, *, now: datetime.datetime | None) -> dict[str, Any]:
        target = adapter if adapter is not None else self._adapter
        summary: dict[str, Any] = {
            "due": self.due(now, adapter=target),
            "checked": 0,
            "claimed": 0,
            "skipped": 0,
            "failed": 0,
        }
        if target is None:
            return summary

        options = getattr(target, "options", None) or {}
        if self._path_blind(options) or not summary["due"]:
            return summary
        path = _claim_path(options)
        keys = [str(key) for key in target._keys() if str(key or "")]
        if not keys:
            return summary

        base = str(getattr(target, "base_url", "") or "").rstrip("/")
        stamp = _stamp(now)
        async with self._lock:
            state = self._read_state()
            claimed = state["claimed"]
            async with httpx.AsyncClient(timeout=CLAIM_TIMEOUT, trust_env=False) as client:
                for token in keys:
                    fingerprint = _fingerprint(token)
                    summary["checked"] += 1
                    if claimed.get(fingerprint) == stamp:
                        summary["skipped"] += 1
                        continue
                    try:
                        granted = await self._claim_one(client, target, base, path, token)
                    except Exception as exc:
                        summary["failed"] += 1
                        logger.warning("ZCode 周末额度领取失败（uid=%s）：%s", fingerprint, exc)
                        continue
                    if not granted:
                        # 2xx 但没有领取信号：不记状态，下一轮继续尝试，绝不虚增
                        continue
                    claimed[fingerprint] = stamp
                    summary["claimed"] += 1
            if summary["claimed"]:
                self._write_state(state)
        return summary

    async def _claim_one(
        self, client: httpx.AsyncClient, adapter: Any, base: str, path: str, token: str
    ) -> bool:
        headers = adapter._headers(token, stream=False)
        response = await client.post(f"{base}{path}", json={}, headers=headers)
        if not 200 <= response.status_code < 300:
            raise ValueError(f"HTTP {response.status_code}：{response.text[:160]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ValueError("响应不是 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("响应不是 JSON 对象")
        return _granted(data)
