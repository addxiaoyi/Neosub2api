"""健康探针与运维指标。

- `GET /api/health`（liveness）：进程活着即 200，不查任何依赖。
- `GET /api/health/ready`（readiness）：查数据库可读 + provider readiness 汇总，
  任一子系统不健康返回 503，并在 `unhealthy` 里点名。
- `GET /api/metrics`：Prometheus 文本格式（text/plain; version=0.0.4），admin 专属。
- `GET /api/metrics/json`：保留原有自定义 JSON，路径变了、字段不变，供既有调用方读取。

鉴权与旧 `/api/metrics` 一致：默认只认 admin 会话。配置 `UNIFIED_METRICS_TOKEN`
后额外接受 `Authorization: Bearer <token>`，默认关闭，避免为了「方便抓取」把运维
数据悄悄公开。两个探针免鉴权，返回体只给子系统状态与 readiness 枚举，不回传
base_url、凭据、库路径等部署信息。
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response

from .. import __version__
from ..alerting import evaluate_alerts
from ..api_utils import log_failure_sql, log_success_sql, now
from ..billing import window_usage
from ..observability import (
    DURATION_BUCKETS_MS,
    current_context,
    http_metrics_snapshot,
    sample_quantile,
    status_class,
)
from ..security import PermissionDenied, admin_user, current_user

router = APIRouter(prefix="/api", tags=["health"])

logger = logging.getLogger(__name__)

SERVICE_NAME = "unified-2api control plane"
MIN_WINDOW_SEC = 60
MAX_WINDOW_SEC = 30 * 86400
PROMETHEUS_MEDIA_TYPE = "text/plain; version=0.0.4"

# 本地准入拒绝不算上游失败，与 alerting 的连败口径保持一致
LOCAL_REJECTION_SQL = (
    "COALESCE(error, '') LIKE '%quota_exceeded%' "
    "OR COALESCE(error, '') LIKE '%failure_rate_limited%' "
    "OR COALESCE(error, '') LIKE '%model_not_found%'"
)
CONNECTION_FAILURE_SQL = "COALESCE(error, '') LIKE '%上游不可达%'"


def _database_check(db) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        conn = db.connect()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception:
        # 不返回异常原文：sqlite 报错里可能带库路径
        return {"ok": False, "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "detail": "数据库不可读"}
    return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 2)}


def _providers_check(registry, demo=None) -> dict[str, Any]:
    """readiness 口径：至少一个「已启用且 spec 声明 ready」的 provider 才算可服务。

    needs_config 的 provider 即使启用也不计入 available：它还需要补齐外部进程/凭据，
    readiness 探针只在真的具备承接能力时放行。

    demo 传入演示运行态时，demo_ 开头的 provider 以假上游实际存活为准，不按静态 spec 报 ready。
    """
    try:
        items: list[dict[str, Any]] = []
        enabled = available = 0
        for item in registry.list_public():
            pid = str(item.get("id") or "")
            is_enabled = bool(item.get("enabled"))
            readiness = str(item.get("readiness") or "unknown")
            requires = list(item.get("requires") or [])
            if demo is not None and demo.covers(pid):
                state = demo.status()
                if not state["ok"]:
                    readiness = "needs_config"
                    requires = [state["detail"] or "演示上游不可用"]
            usable = is_enabled and readiness == "ready"
            if is_enabled:
                enabled += 1
                if usable:
                    available += 1
            items.append(
                {
                    "id": item.get("id"),
                    "enabled": is_enabled,
                    "readiness": readiness,
                    "available": usable,
                    "requires": requires,
                }
            )
    except Exception:
        return {"ok": False, "detail": "provider 注册表不可读", "items": []}
    return {
        "ok": available > 0,
        "enabled": enabled,
        "available": available,
        "needs_config": enabled - available,
        "items": items,
    }


@router.get("/health")
def liveness():
    return {"ok": True, "status": "ok", "probe": "liveness", "service": SERVICE_NAME, "version": __version__}


@router.get("/health/ready")
def readiness(request: Request):
    checks = {
        "database": _database_check(request.app.state.db),
        "providers": _providers_check(
            request.app.state.registry, getattr(request.app.state, "demo", None)
        ),
    }
    unhealthy = [name for name, check in checks.items() if not check["ok"]]
    body = {
        "ok": not unhealthy,
        "status": "ok" if not unhealthy else "error",
        "service": SERVICE_NAME,
        "version": __version__,
        "checks": checks,
        "unhealthy": unhealthy,
        "request_id": current_context().get("request_id", ""),
    }
    return JSONResponse(body, status_code=200 if not unhealthy else 503)


def metrics_access(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    """默认 admin-only；配置了 UNIFIED_METRICS_TOKEN 时额外接受 Bearer 令牌。"""
    token = str(getattr(request.app.state.settings, "metrics_token", "") or "")
    if token and authorization:
        scheme, _, supplied = authorization.partition(" ")
        if scheme.lower() == "bearer" and secrets.compare_digest(
            supplied.strip().encode(), token.encode()
        ):
            return {"username": "metrics-scraper", "role": "admin"}
    user = current_user(request)
    if user.get("role") != "admin":
        raise PermissionDenied("需要管理员权限", role=user.get("role"))
    return user


def _label_value(raw: Any) -> str:
    """Prometheus label 值转义；任何用户输入都不能破坏行结构。"""
    return (
        str(raw)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _labels(items: dict[str, Any]) -> str:
    if not items:
        return ""
    body = ",".join(f'{key}="{_label_value(value)}"' for key, value in items.items())
    return "{" + body + "}"


def _line(name: str, labels: dict[str, Any], value: int | float) -> str:
    rendered = str(int(value)) if isinstance(value, int) else f"{float(value):.3f}"
    return f"{name}{_labels(labels)} {rendered}"


def _header(lines: list[str], name: str, kind: str, help_text: str) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {kind}")


def _json_metrics(conn, window: int) -> dict[str, Any]:
    """旧版 JSON 口径：字段与改动前完全一致，只是挂到了 /api/metrics/json。"""
    since = now() - window
    total = int(conn.execute(
        "SELECT COUNT(*) AS n FROM request_logs WHERE ts >= ?", (since,)
    ).fetchone()["n"] or 0)
    failed = int(conn.execute(
        f"SELECT COUNT(*) AS n FROM request_logs WHERE ts >= ? AND ({log_failure_sql()})",
        (since,),
    ).fetchone()["n"] or 0)
    rows = conn.execute(
        f"SELECT provider, COUNT(*) AS total, "
        f"SUM(CASE WHEN {log_success_sql()} THEN 1 ELSE 0 END) AS success "
        f"FROM request_logs WHERE ts >= ? GROUP BY provider ORDER BY total DESC",
        (since,),
    ).fetchall()
    # 失败限流拒绝由 v1._log_rejection 落 status=429 + error 里带 failure_rate_limited
    limited = int(conn.execute(
        "SELECT COUNT(*) AS n FROM request_logs WHERE ts >= ? AND status = 429 "
        "AND COALESCE(error, '') LIKE '%failure_rate_limited%'",
        (since,),
    ).fetchone()["n"] or 0)
    providers = [
        {
            "provider": row["provider"] or "",
            "requests": int(row["total"] or 0),
            "success": int(row["success"] or 0),
            "failed": int(row["total"] or 0) - int(row["success"] or 0),
        }
        for row in rows
    ]
    return {
        "generated_at": now(),
        "window_seconds": window,
        "requests": {"total": total, "success": total - failed, "failed": failed},
        "providers": providers,
        "failure_rate_limited": limited,
    }


def _provider_requests(conn, since: int) -> dict[tuple[str, str], int]:
    rows = conn.execute(
        "SELECT provider, status, COUNT(*) AS n FROM request_logs "
        "WHERE ts >= ? GROUP BY provider, status",
        (since,),
    ).fetchall()
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (str(row["provider"] or ""), status_class(int(row["status"] or 0)))
        counts[key] = counts.get(key, 0) + int(row["n"] or 0)
    return counts


def _upstream_failures(conn, since: int) -> dict[str, int]:
    row = conn.execute(
        "SELECT "
        f"COALESCE(SUM(CASE WHEN {CONNECTION_FAILURE_SQL} THEN 1 ELSE 0 END), 0) AS connection, "
        "COALESCE(SUM(CASE WHEN status BETWEEN 400 AND 499 "
        f"AND NOT ({LOCAL_REJECTION_SQL}) THEN 1 ELSE 0 END), 0) AS upstream_4xx, "
        "COALESCE(SUM(CASE WHEN status >= 500 "
        f"AND NOT ({LOCAL_REJECTION_SQL}) AND NOT ({CONNECTION_FAILURE_SQL}) "
        "THEN 1 ELSE 0 END), 0) AS upstream_5xx "
        "FROM request_logs WHERE ts >= ?",
        (since,),
    ).fetchone()
    return {
        "connection": int(row["connection"] or 0),
        "upstream_4xx": int(row["upstream_4xx"] or 0),
        "upstream_5xx": int(row["upstream_5xx"] or 0),
    }


def _billing_counts(conn, since: int) -> dict[str, int]:
    """credit_status 口径与 /api/logs 的 _classify_credit 一致：billed/unpriced/
    usage_missing/estimated/not_billed；另外保留 unlinked（成功但无流水）作为兜底档。
    estimated 是第四种用量状态（网关估算），不能并进 billed，否则账面分不清真值。"""
    is_failed = "(r.status >= 400 OR COALESCE(r.error, '') <> '')"
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        f"COALESCE(SUM(CASE WHEN b.id IS NULL AND {is_failed} THEN 1 ELSE 0 END), 0) AS not_billed, "
        f"COALESCE(SUM(CASE WHEN b.id IS NULL AND NOT ({is_failed}) THEN 1 ELSE 0 END), 0) AS unlinked, "
        "COALESCE(SUM(CASE WHEN b.id IS NOT NULL AND b.note = 'usage_missing' "
        "THEN 1 ELSE 0 END), 0) AS usage_missing, "
        "COALESCE(SUM(CASE WHEN b.id IS NOT NULL AND b.usage_estimated = 1 "
        "THEN 1 ELSE 0 END), 0) AS estimated, "
        "COALESCE(SUM(CASE WHEN b.id IS NOT NULL AND b.usage_estimated = 1 "
        "THEN b.billed_micro ELSE 0 END), 0) AS estimated_micro, "
        "COALESCE(SUM(CASE WHEN b.id IS NOT NULL AND b.usage_estimated = 0 "
        "AND b.note LIKE 'unpriced:%' THEN 1 ELSE 0 END), 0) AS unpriced, "
        "COALESCE(SUM(CASE WHEN b.id IS NOT NULL AND b.usage_estimated = 0 "
        "AND b.note <> 'usage_missing' AND b.note NOT LIKE 'unpriced:%' "
        "THEN 1 ELSE 0 END), 0) AS billed, "
        "COALESCE(SUM(CASE WHEN b.id IS NOT NULL AND b.usage_estimated = 0 "
        "AND b.note <> 'usage_missing' AND b.note NOT LIKE 'unpriced:%' "
        "THEN b.billed_micro ELSE 0 END), 0) AS billed_micro "
        "FROM request_logs r LEFT JOIN billing_ledger b ON b.log_id = r.id "
        "WHERE r.ts >= ?",
        (since,),
    ).fetchone()
    return {key: int(row[key] or 0) for key in
            ("total", "not_billed", "unlinked", "usage_missing", "estimated",
             "estimated_micro", "unpriced", "billed", "billed_micro")}


def _quota_rows(conn, at: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for policy in conn.execute("SELECT * FROM quota_policies WHERE enabled = 1").fetchall():
        usage = window_usage(conn, policy, at)
        limit_micro = int(policy["limit_micro"])
        limit_requests = int(policy["limit_requests"])
        labels = {
            # 只用数字 key_id，不放 key_name / 用户名 / 密钥前缀
            "key_id": int(policy["key_id"]),
            "provider": str(policy["provider"] or "*"),
            "model": str(policy["model"] or "*"),
            "window": str(policy["window"]),
        }
        items.append({
            "labels": labels,
            "used_micro": int(usage["used_micro"]),
            "limit_micro": limit_micro,
            "remaining_micro": max(0, limit_micro - int(usage["used_micro"])) if limit_micro > 0 else 0,
            "used_requests": int(usage["used_requests"]),
            "limit_requests": limit_requests,
            "remaining_requests": (
                max(0, limit_requests - int(usage["used_requests"])) if limit_requests > 0 else 0
            ),
        })
    return items


def _alert_history(conn) -> list[Any]:
    return conn.execute(
        "SELECT kind, COALESCE(NULLIF(subject, ''), '*') AS subject, "
        "COUNT(*) AS n, MAX(triggered_at) AS last_at "
        "FROM quota_alerts GROUP BY kind, subject"
    ).fetchall()


def _render_prometheus(conn, window: int) -> str:
    at = now()
    since = at - window
    lines: list[str] = []

    provider_counts = _provider_requests(conn, since)
    _header(lines, "unified_requests_window", "gauge",
            "按 provider 与 HTTP 状态桶统计的请求数（滑动窗口）")
    for (provider, state), count in sorted(provider_counts.items()):
        lines.append(_line("unified_requests_window",
                           {"provider": provider, "status": state}, count))

    snapshot = http_metrics_snapshot()
    _header(lines, "unified_http_requests_total", "counter",
            "进程内 HTTP 请求计数（provider 来自 x-upstream-provider，控制面记为 -）")
    for method, endpoint, state, provider, count in sorted(snapshot["requests"]):
        lines.append(_line("unified_http_requests_total",
                           {"method": method, "endpoint": endpoint,
                            "status": state, "provider": provider}, count))

    _header(lines, "unified_request_duration_ms", "histogram",
            "进程内请求耗时直方图（毫秒，按路由模板不区分端点）")
    cumulative = 0
    for boundary, count in zip(DURATION_BUCKETS_MS, snapshot["duration_buckets"]):
        cumulative += count
        le = "+Inf" if boundary == float("inf") else str(int(boundary))
        lines.append(_line("unified_request_duration_ms_bucket", {"le": le}, cumulative))
    lines.append(_line("unified_request_duration_ms_sum", {}, snapshot["duration_sum_ms"]))
    lines.append(_line("unified_request_duration_ms_count", {}, snapshot["duration_count"]))

    samples = snapshot["duration_samples"]
    _header(lines, "unified_request_duration_ms_p50", "gauge",
            "最近 5000 次请求的耗时中位数（毫秒）")
    lines.append(_line("unified_request_duration_ms_p50", {},
                       sample_quantile(samples, 0.50)))
    _header(lines, "unified_request_duration_ms_p95", "gauge",
            "最近 5000 次请求的耗时 p95（毫秒）")
    lines.append(_line("unified_request_duration_ms_p95", {},
                       sample_quantile(samples, 0.95)))

    failures = _upstream_failures(conn, since)
    _header(lines, "unified_upstream_failures_window", "gauge",
            "上游失败计数（滑动窗口，已排除本地配额/限流/未知模型拒绝）")
    for kind in ("connection", "upstream_4xx", "upstream_5xx"):
        lines.append(_line("unified_upstream_failures_window", {"kind": kind}, failures[kind]))

    quota = _quota_rows(conn, at)
    _header(lines, "unified_quota_used_micro", "gauge", "当前周期已用额度（micro-USD）")
    _header(lines, "unified_quota_limit_micro", "gauge", "金额额度上限（micro-USD，0=未设）")
    _header(lines, "unified_quota_remaining_micro", "gauge", "剩余金额额度（micro-USD）")
    _header(lines, "unified_quota_used_requests", "gauge", "当前周期已用请求数")
    _header(lines, "unified_quota_limit_requests", "gauge", "请求数上限（0=未设）")
    _header(lines, "unified_quota_remaining_requests", "gauge", "剩余请求数")
    for item in quota:
        for name, key in (
            ("unified_quota_used_micro", "used_micro"),
            ("unified_quota_limit_micro", "limit_micro"),
            ("unified_quota_remaining_micro", "remaining_micro"),
            ("unified_quota_used_requests", "used_requests"),
            ("unified_quota_limit_requests", "limit_requests"),
            ("unified_quota_remaining_requests", "remaining_requests"),
        ):
            lines.append(_line(name, item["labels"], item[key]))

    billing = _billing_counts(conn, since)
    _header(lines, "unified_billing_requests_window", "gauge",
            "按 credit_status 分档的请求数（滑动窗口）")
    for status in ("billed", "unpriced", "usage_missing", "estimated", "not_billed", "unlinked"):
        lines.append(_line("unified_billing_requests_window",
                           {"credit_status": status}, billing[status]))
    _header(lines, "unified_billing_charged_micro_window", "gauge",
            "已定格计费金额（micro-USD，滑动窗口；billed 与 estimated 两档分别给值）")
    for status, key in (("billed", "billed_micro"), ("estimated", "estimated_micro")):
        lines.append(_line("unified_billing_charged_micro_window",
                           {"credit_status": status}, billing[key]))

    _header(lines, "unified_alert_history_total", "counter",
            "告警历史累计条数（按规则与对象）")
    _header(lines, "unified_alert_last_triggered_timestamp_seconds", "gauge",
            "最近一次告警触发时间（Unix 秒）")
    for row in _alert_history(conn):
        labels = {"kind": str(row["kind"]), "subject": str(row["subject"])}
        lines.append(_line("unified_alert_history_total", labels, int(row["n"])))
        lines.append(_line("unified_alert_last_triggered_timestamp_seconds", labels,
                           int(row["last_at"] or 0)))

    return "\n".join(lines) + "\n"


def _clamp_window(window_seconds: int) -> int:
    return max(MIN_WINDOW_SEC, min(int(window_seconds), MAX_WINDOW_SEC))


@router.get("/metrics")
def metrics(
    request: Request,
    window_seconds: int = 3600,
    user: dict = Depends(metrics_access),
):
    """Prometheus 文本出口；ADMIN only（或显式配置的抓取令牌）。"""
    window = _clamp_window(window_seconds)
    conn = request.app.state.db.connect()
    try:
        try:
            evaluate_alerts(conn, request.app.state.settings)
        except Exception:  # noqa: BLE001 - 告警评估失败不能带走指标抓取
            logger.exception("抓取时评估告警失败")
        body = _render_prometheus(conn, window)
    finally:
        conn.close()
    return Response(content=body, media_type=PROMETHEUS_MEDIA_TYPE)


@router.get("/metrics/json")
def metrics_json(
    request: Request,
    window_seconds: int = 3600,
    user: dict = Depends(admin_user),
):
    """旧版自定义 JSON：字段与路径迁移前一致，避免破坏既有消费方。"""
    window = _clamp_window(window_seconds)
    conn = request.app.state.db.connect()
    try:
        try:
            evaluate_alerts(conn, request.app.state.settings)
        except Exception:  # noqa: BLE001 - 告警评估失败不能带走指标读取
            logger.exception("读取 JSON 指标时评估告警失败")
        body = _json_metrics(conn, window)
    finally:
        conn.close()
    return body
