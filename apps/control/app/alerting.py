"""运维告警规则：provider 连败 / 配额剩余 / usage_missing 占比。

全部复用 `quota_alerts` 表与 `UNIQUE(policy_id, threshold_percent, period_start)` 的
去重语义，不另建告警存储。两条全局规则（连败、usage_missing 占比）没有策略 id，
用 `kind` + `subject` 标记，并把 policy_id 取成稳定的负数（CRC32）——这样既不会和
真实策略 id 冲突，也能沿用既有唯一约束做「同一对象同一周期只告警一次」。

评估入口在 /api/metrics 与 /api/quota/alerts 读取时触发，而不是塞进 v1/billing 的
请求热路径（那两处按约束不改）。因此连败规则是「抓取时刻仍处于连败」才落库，
历史一旦落库不会消失；抓取间隔内瞬间恢复的连败可能漏报，这一点在交付报告中写明。
"""

from __future__ import annotations

import logging
import sqlite3
import zlib
from typing import Any

from .api_utils import now
from .billing import window_usage

logger = logging.getLogger(__name__)

PROVIDER_LOOKBACK_SEC = 86400
USAGE_MISSING_WINDOW_SEC = 600
USAGE_MISSING_BUCKET_SEC = 3600
# 本地准入拒绝（配额/失败限流/未知模型）不是 provider 故障，不计入连败
LOCAL_REJECTION_MARKS = ("quota_exceeded", "failure_rate_limited", "model_not_found")


def _global_policy_id(kind: str, subject: str) -> int:
    """全局规则的合成 policy_id：负数、确定、不会撞真实策略 id。"""
    digest = zlib.crc32(f"{kind}:{subject}".encode())
    return -(digest % 2_000_000_000 + 1)


def _is_provider_failure(row: Any) -> bool:
    error = str(row["error"] or "")
    if any(mark in error for mark in LOCAL_REJECTION_MARKS):
        return False
    return int(row["status"] or 0) >= 400 or bool(error)


def _insert_alert(
    conn: sqlite3.Connection,
    *,
    policy_id: int,
    threshold_percent: int,
    window: str,
    period_start: int,
    at: int,
    metric: str,
    used_micro: int = 0,
    limit_micro: int = 0,
    used_requests: int = 0,
    limit_requests: int = 0,
    kind: str = "quota",
    subject: str = "",
) -> int:
    exists = conn.execute(
        "SELECT 1 FROM quota_alerts WHERE policy_id = ? AND threshold_percent = ? "
        "AND period_start = ? AND kind = ?",
        (policy_id, threshold_percent, period_start, kind),
    ).fetchone()
    if exists is not None:
        return 0
    cur = conn.execute(
        "INSERT OR IGNORE INTO quota_alerts(policy_id, threshold_percent, window, period_start, "
        "triggered_at, metric, used_micro, limit_micro, used_requests, limit_requests, kind, subject) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            policy_id, threshold_percent, window, period_start, int(at), metric,
            int(used_micro), int(limit_micro), int(used_requests), int(limit_requests),
            kind, subject,
        ),
    )
    return 1 if cur.rowcount else 0


def _provider_failure_alerts(conn: sqlite3.Connection, streak: int, at: int) -> int:
    if streak <= 0:
        return 0
    providers = conn.execute(
        "SELECT DISTINCT provider FROM request_logs "
        "WHERE ts >= ? AND provider IS NOT NULL AND provider <> ''",
        (at - PROVIDER_LOOKBACK_SEC,),
    ).fetchall()
    created = 0
    for row in providers:
        provider = str(row["provider"])
        latest = conn.execute(
            "SELECT ts, status, error FROM request_logs WHERE provider = ? ORDER BY id DESC LIMIT ?",
            (provider, streak),
        ).fetchall()
        if len(latest) < streak or not all(_is_provider_failure(item) for item in latest):
            continue
        start_ts = min(int(item["ts"]) for item in latest)
        created += _insert_alert(
            conn,
            policy_id=_global_policy_id("provider_failure", provider),
            threshold_percent=streak,
            window="streak",
            period_start=start_ts,
            at=at,
            metric="requests",
            used_requests=streak,
            limit_requests=streak,
            kind="provider_failure",
            subject=provider,
        )
    return created


def _quota_remaining_alerts(conn: sqlite3.Connection, remaining_percent: int, at: int) -> int:
    # 剩余 <= 20% 等价于已用 >= 80%，沿用既有「已用百分比」阈值口径。
    if not 1 <= remaining_percent <= 99:
        return 0
    threshold = 100 - remaining_percent
    created = 0
    for policy in conn.execute("SELECT * FROM quota_policies WHERE enabled = 1").fetchall():
        # 已有显式阈值的策略按用户配置走，不再叠加全局「剩余」阈值，避免多报
        configured = conn.execute(
            "SELECT COUNT(*) AS n FROM quota_alert_thresholds WHERE policy_id = ?",
            (int(policy["id"]),),
        ).fetchone()
        if int(configured["n"] or 0) > 0:
            continue
        usage = window_usage(conn, policy, at)
        limit_micro = int(policy["limit_micro"])
        limit_requests = int(policy["limit_requests"])
        micro_hit = limit_micro > 0 and usage["used_micro"] * 100 >= limit_micro * threshold
        requests_hit = limit_requests > 0 and usage["used_requests"] * 100 >= limit_requests * threshold
        if not (micro_hit or requests_hit):
            continue
        if micro_hit and requests_hit:
            metric = (
                "micro"
                if usage["used_micro"] * limit_requests >= usage["used_requests"] * limit_micro
                else "requests"
            )
        else:
            metric = "micro" if micro_hit else "requests"
        created += _insert_alert(
            conn,
            policy_id=int(policy["id"]),
            threshold_percent=threshold,
            window=str(policy["window"]),
            period_start=int(usage["period_start"]),
            at=at,
            metric=metric,
            used_micro=int(usage["used_micro"]),
            limit_micro=limit_micro,
            used_requests=int(usage["used_requests"]),
            limit_requests=limit_requests,
            kind="quota",
        )
    return created


def _usage_missing_alert(
    conn: sqlite3.Connection, ratio_percent: int, min_requests: int, at: int
) -> int:
    if ratio_percent <= 0:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "COALESCE(SUM(CASE WHEN b.note = 'usage_missing' THEN 1 ELSE 0 END), 0) AS missing "
        "FROM request_logs r LEFT JOIN billing_ledger b ON b.log_id = r.id "
        "WHERE r.ts >= ?",
        (at - USAGE_MISSING_WINDOW_SEC,),
    ).fetchone()
    total = int(row["n"] or 0)
    missing = int(row["missing"] or 0)
    if total < max(1, int(min_requests)) or missing * 100 < total * ratio_percent:
        return 0
    bucket_start = (int(at) // USAGE_MISSING_BUCKET_SEC) * USAGE_MISSING_BUCKET_SEC
    return _insert_alert(
        conn,
        policy_id=_global_policy_id("usage_missing_ratio", ""),
        threshold_percent=ratio_percent,
        window="hour",
        period_start=bucket_start,
        at=at,
        metric="requests",
        used_requests=missing,
        limit_requests=total,
        kind="usage_missing_ratio",
    )


def evaluate_alerts(conn: sqlite3.Connection, settings: Any, at: int | None = None) -> int:
    """按配置评估三条规则；单条规则失败只记日志，不影响其余规则。"""
    stamp = int(at) if at is not None else now()
    rules = (
        ("provider_failure", lambda: _provider_failure_alerts(
            conn, int(getattr(settings, "alert_provider_failure_streak", 0)), stamp
        )),
        ("quota_remaining", lambda: _quota_remaining_alerts(
            conn, int(getattr(settings, "alert_quota_remaining_percent", 0)), stamp
        )),
        ("usage_missing", lambda: _usage_missing_alert(
            conn,
            int(getattr(settings, "alert_usage_missing_ratio_percent", 0)),
            int(getattr(settings, "alert_usage_missing_min_requests", 0)),
            stamp,
        )),
    )
    created = 0
    for name, rule in rules:
        try:
            created += rule()
        except Exception:  # noqa: BLE001 - 单条规则失败不能影响其余规则与抓取
            logger.exception("告警规则评估失败 rule=%s", name)
    if created:
        conn.commit()
    return created
