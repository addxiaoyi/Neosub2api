"""服务商管理台聚合层：只读本地证据，不在这里外呼上游。

口径：
- 配置项一律按白名单出字段，密钥/代理/上游地址只给「是否配置」与数量，不给原文；
- 用量与日志复用 /api/stats、/api/logs 的归属规则：admin 看全站，其余角色只看自己名下 key；
- 轮转、会话池、代理池、领取状态都取进程内/落盘快照，页面打开时不触发网络请求。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..api_utils import mask_secret, mask_text, now, redact_endpoints
from ..billing import WINDOW_TZ_OFFSET, to_usd
from . import muse_session
from .proxy_pool import ProxyPool

CONSOLE_PROVIDERS = (
    "codebuddy",
    "muse",
    "gptgrok",
    "zcode",
    "trae",
    "qoder",
    "qoder_sidecar",
    "codar",
    "codar_sidecar",
)

WINDOW_SECONDS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
_TZ = dt.timezone(dt.timedelta(seconds=WINDOW_TZ_OFFSET))

# 每家管理台能做的事：probe 是通用探活，其余按适配器实际实现挂载
ACTIONS = {
    "codebuddy": ("probe", "run_task"),
    "muse": ("probe", "keepalive"),
    "gptgrok": ("probe", "cancel_task"),
    "zcode": ("probe", "claim"),
    "trae": ("probe",),
    "qoder": ("probe",),
    "qoder_sidecar": ("probe",),
    "codar": ("probe",),
    "codar_sidecar": ("probe",),
}

CODEBUDDY_TASKS = ("checkin_all", "refresh_credits")


def clean(value: Any, limit: int = 240) -> str:
    """对外文案统一脱敏：凭据形态与 URL/内网地址一起处理。"""
    text = redact_endpoints(mask_text(str(value or "")))
    return text[:limit]


def sanitize(value: Any, depth: int = 0) -> Any:
    """把探活结果压成可展示结构：字符串脱敏，列表/字典限深限量。"""
    if depth > 4:
        return None
    if isinstance(value, str):
        return clean(value, 300)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 40:
                break
            out[str(key)[:64]] = sanitize(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize(item, depth + 1) for item in list(value)[:30]]
    return clean(value, 300)


def owner_scope(user: dict) -> str | None:
    if user.get("role") == "admin":
        return None
    return str(user.get("username") or "")


def _owned(owner: str | None) -> tuple[str, list[Any]]:
    if owner is None:
        return "", []
    return "key_id IN (SELECT id FROM api_keys WHERE owner_user_id = ?)", [owner]


def _models(options: dict[str, Any]) -> tuple[list[str], int]:
    raw = options.get("models")
    items = [str(item) for item in raw if isinstance(item, (str, int, float))] if isinstance(raw, list) else []
    return items[:40], len(items)


def _proxy(options: dict[str, Any]) -> dict[str, Any]:
    pool = ProxyPool.from_options(options)
    # entries 里是真实出口地址，管理台只报规模与模式
    return {"enabled": pool.enabled, "mode": pool.mode, "size": pool.size}


def provider_config(pid: str, options: dict[str, Any]) -> dict[str, Any]:
    models, model_count = _models(options)
    config: dict[str, Any] = {"models": models, "model_count": model_count, "proxy": _proxy(options)}
    if pid == "muse":
        config["session"] = {
            "cookie_pool": len(options.get("cookie_pool") or []) if isinstance(options.get("cookie_pool"), list) else 0,
            "keepalive": bool(options.get("cookie_keepalive")),
            "keepalive_interval": options.get("cookie_keepalive_interval"),
        }
    if pid == "gptgrok":
        config["media"] = {
            "base_configured": bool(str(options.get("media_base") or "").strip()),
            "key_configured": bool(str(options.get("media_api_key") or "").strip()),
        }
        config["channel"] = str(options.get("health_channel") or options.get("channel") or "chatgpt")
    if pid == "zcode":
        config["claim"] = {
            "enabled": bool(options.get("weekend_claim")),
            "path_configured": bool(str(options.get("claim_path") or "").strip()),
            "window": options.get("claim_window"),
            "hours": options.get("claim_hours"),
        }
    if pid == "trae":
        config["channel"] = str(options.get("channel") or "solo")
        config["work_mode"] = str(options.get("work_mode") or "stream")
        config["extra_keys"] = len(options.get("extra_keys") or []) if isinstance(options.get("extra_keys"), list) else 0
    if pid == "qoder":
        config["region"] = str(options.get("region") or "cn")
        config["product"] = str(options.get("product") or "qoder")
    if pid in ("codar", "codar_sidecar"):
        config["schedule_policy"] = str(options.get("schedule_policy") or options.get("schedule") or "")
        config["refresh"] = bool(options.get("refresh", True))
        config["allow_external_refresh"] = bool(options.get("allow_external_refresh", False))
        config["restricted_codes"] = options.get("restricted_codes") if isinstance(options.get("restricted_codes"), list) else []
    return config


def credentials(registry: Any, pid: str) -> list[dict[str, Any]]:
    items = []
    for row in registry.credentials(pid):
        items.append(
            {
                "id": int(row.get("id") or 0),
                "label": str(row.get("label") or "")[:64],
                "secret_masked": mask_secret(str(row.get("secret") or "")),
                "enabled": bool(row.get("enabled")),
                "sort_order": int(row.get("sort_order") or 0),
                "has_base_url": bool(str(row.get("base_url") or "").strip()),
            }
        )
    return items


def account_stats(conn: Any, pid: str, owner: str | None) -> dict[str, Any] | None:
    where = "provider = ?"
    params: list[Any] = [pid]
    if owner is not None:
        where += " AND owner_user_id = ?"
        params.append(owner)
    rows = conn.execute(
        "SELECT disabled, expires_at, credits FROM accounts WHERE " + where,
        params,
    ).fetchall()
    if not rows:
        return None
    stamp = now()
    expired = sum(1 for row in rows if int(row["expires_at"] or 0) and int(row["expires_at"]) < stamp)
    return {
        "total": len(rows),
        "disabled": sum(1 for row in rows if int(row["disabled"] or 0)),
        "expired": expired,
        "credits": round(sum(float(row["credits"] or 0) for row in rows), 2),
    }


def usage(conn: Any, pid: str, window: str, owner: str | None) -> dict[str, Any]:
    span = WINDOW_SECONDS.get(window, WINDOW_SECONDS["7d"])
    start = now() - span
    days = max(1, min(30, span // 86400))
    scope, params = _owned(owner)
    scope_and = f" AND {scope}" if scope else ""
    rows = conn.execute(
        "SELECT ts, status, error, model, prompt_tokens, completion_tokens, latency_ms "
        "FROM request_logs WHERE provider = ? AND ts >= ?" + scope_and,
        (pid, start, *params),
    ).fetchall()
    total = len(rows)
    success = sum(1 for row in rows if int(row["status"]) < 400 and not (row["error"] or ""))
    failed = total - success
    tokens = sum(int(row["prompt_tokens"] or 0) + int(row["completion_tokens"] or 0) for row in rows)
    latency_avg = round(sum(int(row["latency_ms"] or 0) for row in rows) / total, 1) if total else 0
    model_counts: dict[str, int] = {}
    buckets: dict[str, dict[str, Any]] = {}
    for offset in range(days - 1, -1, -1):
        day = (dt.datetime.fromtimestamp(now(), _TZ) - dt.timedelta(days=offset)).date().isoformat()
        buckets[day] = {"day": day, "requests": 0, "failed": 0, "tokens": 0}
    for row in rows:
        label = dt.datetime.fromtimestamp(int(row["ts"]), _TZ).date().isoformat()
        bucket = buckets.get(label)
        if bucket is not None:
            bucket["requests"] += 1
            bucket["tokens"] += int(row["prompt_tokens"] or 0) + int(row["completion_tokens"] or 0)
            if int(row["status"]) >= 400 or (row["error"] or ""):
                bucket["failed"] += 1
        model = str(row["model"] or "")
        if model:
            model_counts[model] = model_counts.get(model, 0) + 1
    top_models = [
        {"model": model, "requests": count}
        for model, count in sorted(model_counts.items(), key=lambda item: item[1], reverse=True)[:5]
    ]
    credit_row = conn.execute(
        "SELECT COALESCE(SUM(billed_micro), 0) AS micro FROM billing_ledger "
        "WHERE provider = ? AND ts >= ?" + scope_and,
        (pid, start, *params),
    ).fetchone()
    return {
        "window": window if window in WINDOW_SECONDS else "7d",
        "requests": total,
        "success": success,
        "failed": failed,
        "tokens": tokens,
        "credit_usd": round(to_usd(int(credit_row["micro"] or 0)), 6),
        "latency_avg_ms": latency_avg,
        "top_models": top_models,
        "daily": list(buckets.values()),
    }


def recent_logs(conn: Any, pid: str, owner: str | None, limit: int = 20) -> list[dict[str, Any]]:
    scope, params = _owned(owner)
    scope_and = f" AND {scope}" if scope else ""
    rows = conn.execute(
        "SELECT id, ts, model, status, latency_ms, stream, prompt_tokens, completion_tokens, error "
        "FROM request_logs WHERE provider = ?" + scope_and + " ORDER BY id DESC LIMIT ?",
        (pid, *params, max(1, min(50, limit))),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "ts": int(row["ts"]),
            "model": str(row["model"] or ""),
            "status": int(row["status"]),
            "latency_ms": int(row["latency_ms"] or 0),
            "stream": bool(row["stream"]),
            "tokens": int(row["prompt_tokens"] or 0) + int(row["completion_tokens"] or 0),
            "error": clean(row["error"], 160),
        }
        for row in rows
    ]


def rotation_groups(adapter: Any) -> tuple[Any, list[str], str]:
    pool = None
    for name in ("_pool", "_keys_pool", "_scheduler"):
        candidate = getattr(adapter, name, None)
        if candidate is not None:
            pool = candidate
            break
    keys: list[str] = []
    getter = getattr(adapter, "_keys", None)
    if callable(getter):
        keys = [str(key) for key in getter() if str(key or "")]
    elif hasattr(adapter, "_configured_tokens"):
        keys = [str(key) for key in adapter._configured_tokens() if str(key or "")]
    group = ""
    group_getter = getattr(adapter, "_group", None)
    if callable(group_getter):
        try:
            group = str(group_getter() or "")
        except Exception:
            group = ""
    return pool, keys, group


async def rotation_snapshot(adapter: Any) -> dict[str, Any] | None:
    pool, keys, group = rotation_groups(adapter)
    if pool is None or not keys:
        return None
    try:
        if group:
            return await pool.snapshot(keys, group=group)
        return await pool.snapshot(keys)
    except Exception:
        return None


def muse_sessions(pid: str, adapter: Any, settings: Any) -> dict[str, Any] | None:
    if pid != "muse":
        return None
    pool = muse_session.SessionPool.from_options(getattr(adapter, "options", {}) or {}, settings)
    return pool.describe()


def task_queue_stats(store: Any) -> dict[str, Any]:
    counts = getattr(store, "_tasks", {}) or {}
    stats = {"total": len(counts), "queued": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}
    for rec in counts.values():
        status = str(rec.get("status") or "")
        if status in stats:
            stats[status] += 1
    return stats
