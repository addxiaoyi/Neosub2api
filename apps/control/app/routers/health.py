"""健康探针与运维指标。

- `GET /api/health`（liveness）：进程活着即 200，不查任何依赖。
- `GET /api/health/ready`（readiness）：查数据库可读 + provider readiness 汇总，
  任一子系统不健康返回 503，并在 `unhealthy` 里点名。
- `GET /api/metrics`：admin 专属。理由：它暴露按 provider 的失败分布与限流触发次数，
  属于运维态数据，与既有 /api/stats 同一权限面；免鉴权只留给编排器探针。

两个探针都免鉴权（探针不带会话），返回体只给子系统状态与 readiness 枚举，
不回传 base_url、凭据、库路径等部署信息。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..api_utils import log_failure_sql, log_success_sql, now
from ..observability import current_context
from ..security import admin_user

router = APIRouter(prefix="/api", tags=["health"])

SERVICE_NAME = "unified-2api control plane"
MIN_WINDOW_SEC = 60
MAX_WINDOW_SEC = 30 * 86400


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


@router.get("/metrics")
def metrics(request: Request, window_seconds: int = 3600, user: dict = Depends(admin_user)):
    """从既有 request_logs 聚合，不新建累加器；口径与 /api/logs、/api/stats 一致。"""
    window = max(MIN_WINDOW_SEC, min(int(window_seconds), MAX_WINDOW_SEC))
    since = now() - window
    conn = request.app.state.db.connect()
    try:
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
    finally:
        conn.close()
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
