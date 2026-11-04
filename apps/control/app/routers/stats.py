"""统计：/api/stats/summary 与 /api/stats/daily。

归属口径：admin 看全站；非 admin 只看自己名下 key 的聚合。request_logs 与
billing_ledger 都带 key_id，api_keys.owner_user_id 提供归属，所以按 key 归属
收窄是可实现的，全站聚合不需要对普通用户开放。key_id 为空的匿名日志无法归属，
对非 admin 一律不计入——这是最保守的可实现口径：宁可少算，也不把别人的算进来。
响应里只有聚合数字与模型名，不含任何密钥标识。
"""

import datetime as dt

from fastapi import APIRouter, Depends, Request

from ..api_utils import log_success_sql, now
from ..billing import to_usd
from ..security import current_user

router = APIRouter(prefix="/api/stats", tags=["stats"])


def _owner(user: dict) -> str | None:
    """admin 返回 None（不收窄）；其他角色返回自己的用户名。"""
    if user.get("role") == "admin":
        return None
    return user.get("username") or ""


def _owned(owner: str | None) -> tuple[str, list]:
    """返回可直接拼进 SQL 的归属条件；空串表示全站。"""
    if owner is None:
        return "", []
    return "key_id IN (SELECT id FROM api_keys WHERE owner_user_id = ?)", [owner]


def _day_start(offset_days: int = 0) -> int:
    day = dt.date.today() - dt.timedelta(days=offset_days)
    return int(dt.datetime.combine(day, dt.time.min).timestamp())


def _zero_summary() -> dict:
    return {
        "today_requests": 0,
        "today_tokens": 0,
        "today_credit": 0,
        "week_credit": 0,
        "total_credit": 0,
        "week_requests": 0,
        "week_tokens": 0,
        "total_requests": 0,
        "total_tokens": 0,
        "active_keys": 0,
        "top_model": None,
        "usage_health": {"ok": True, "detail": "M1 用量库已就绪，尚无请求", "logs_today": 0},
        "failures": {"today_4xx": 0, "today_5xx": 0, "week_4xx": 0, "week_5xx": 0},
    }


@router.get("/summary")
def summary(request: Request, realm: str | None = None, user: dict = Depends(current_user)):
    owner = _owner(user)
    scope, scope_params = _owned(owner)
    scope_and = f" AND {scope}" if scope else ""
    scope_where = f" WHERE {scope}" if scope else ""
    conn = request.app.state.db.connect()
    try:
        result = _zero_summary()
        today = _day_start()
        week = _day_start(6)
        rows = conn.execute(
            "SELECT ts, status, error, prompt_tokens, completion_tokens, model FROM request_logs "
            "WHERE ts >= ?" + scope_and,
            (week, *scope_params),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS n, SUM(prompt_tokens + completion_tokens) AS tokens "
            "FROM request_logs" + scope_where,
            scope_params,
        ).fetchone()
        for row in rows:
            tokens = int(row["prompt_tokens"] or 0) + int(row["completion_tokens"] or 0)
            # 成功要同时看 error 列：流式中途断流 HTTP 状态还是 200
            is_ok = row["status"] < 400 and not (row["error"] or "").strip()
            is_client_error = 400 <= row["status"] < 500
            if is_ok:
                result["week_requests"] += 1
                result["week_tokens"] += tokens
            if row["ts"] >= today:
                if is_ok:
                    result["today_requests"] += 1
                    result["today_tokens"] += tokens
                elif is_client_error:
                    result["failures"]["today_4xx"] += 1
                else:
                    result["failures"]["today_5xx"] += 1
            if not is_ok:
                if is_client_error:
                    result["failures"]["week_4xx"] += 1
                else:
                    result["failures"]["week_5xx"] += 1
        result["total_requests"] = int(total["n"] or 0)
        result["total_tokens"] = int(total["tokens"] or 0)
        credit = conn.execute(
            "SELECT COALESCE(SUM(billed_micro), 0) AS total_micro, "
            "COALESCE(SUM(CASE WHEN ts >= ? THEN billed_micro ELSE 0 END), 0) AS week_micro, "
            "COALESCE(SUM(CASE WHEN ts >= ? THEN billed_micro ELSE 0 END), 0) AS today_micro "
            "FROM billing_ledger" + scope_where,
            (week, today, *scope_params),
        ).fetchone()
        result["total_credit"] = to_usd(credit["total_micro"])
        result["week_credit"] = to_usd(credit["week_micro"])
        result["today_credit"] = to_usd(credit["today_micro"])
        active_sql = "SELECT COUNT(*) AS n FROM api_keys WHERE enabled = 1"
        active_params: list = []
        if owner is not None:
            active_sql += " AND owner_user_id = ?"
            active_params.append(owner)
        result["active_keys"] = int(conn.execute(active_sql, active_params).fetchone()["n"] or 0)
        top = conn.execute(
            f"SELECT model, COUNT(*) AS n FROM request_logs "
            f"WHERE ts >= ? AND model IS NOT NULL AND {log_success_sql()}{scope_and} "
            f"GROUP BY model ORDER BY n DESC LIMIT 1",
            (week, *scope_params),
        ).fetchone()
        result["top_model"] = top["model"] if top else None
        result["usage_health"]["logs_today"] = int(
            conn.execute(
                f"SELECT COUNT(*) AS n FROM request_logs WHERE ts >= ? AND {log_success_sql()}" + scope_and,
                (today, *scope_params),
            ).fetchone()["n"]
            or 0
        )
        return result
    finally:
        conn.close()


@router.get("/daily")
def daily(request: Request, days: int = 30, realm: str | None = None, user: dict = Depends(current_user)):
    days = max(1, min(days, 90))
    owner = _owner(user)
    scope, scope_params = _owned(owner)
    scope_and = f" AND {scope}" if scope else ""
    conn = request.app.state.db.connect()
    try:
        buckets: dict[str, dict] = {}
        start = _day_start(days - 1)
        for offset in range(days - 1, -1, -1):
            label = (dt.date.today() - dt.timedelta(days=offset)).isoformat()
            buckets[label] = {
                "day": label,
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "credit": 0,
                "failed": 0,
            }
        rows = conn.execute(
            "SELECT * FROM request_logs WHERE ts >= ?" + scope_and, (start, *scope_params)
        ).fetchall()
        for row in rows:
            label = dt.datetime.fromtimestamp(row["ts"]).date().isoformat()
            bucket = buckets.get(label)
            if bucket is None:
                continue
            if row["status"] < 400 and not (row["error"] or "").strip():
                bucket["requests"] += 1
                bucket["prompt_tokens"] += int(row["prompt_tokens"] or 0)
                bucket["completion_tokens"] += int(row["completion_tokens"] or 0)
            else:
                bucket["failed"] += 1
        # 金额按日归集：口径与 request_logs 的 success 分桶无关，失败请求通常本来就没有金额
        micro_by_day: dict[str, int] = {label: 0 for label in buckets}
        for row in conn.execute(
            "SELECT ts, billed_micro FROM billing_ledger WHERE ts >= ?" + scope_and,
            (start, *scope_params),
        ):
            label = dt.datetime.fromtimestamp(row["ts"]).date().isoformat()
            if label in micro_by_day:
                micro_by_day[label] += int(row["billed_micro"] or 0)
        for label, bucket in buckets.items():
            bucket["credit"] = to_usd(micro_by_day[label])
        return list(buckets.values())
    finally:
        conn.close()
