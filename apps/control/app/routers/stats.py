import datetime as dt

from fastapi import APIRouter, Depends, Request

from ..api_utils import log_success_sql, now
from ..billing import to_usd
from ..security import current_user

router = APIRouter(prefix="/api/stats", tags=["stats"])


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
    conn = request.app.state.db.connect()
    try:
        result = _zero_summary()
        today = _day_start()
        week = _day_start(6)
        rows = conn.execute(
            "SELECT ts, status, error, prompt_tokens, completion_tokens, model FROM request_logs WHERE ts >= ?",
            (week,),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS n, SUM(prompt_tokens + completion_tokens) AS tokens "
            "FROM request_logs"
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
            "FROM billing_ledger",
            (week, today),
        ).fetchone()
        result["total_credit"] = to_usd(credit["total_micro"])
        result["week_credit"] = to_usd(credit["week_micro"])
        result["today_credit"] = to_usd(credit["today_micro"])
        result["active_keys"] = int(
            conn.execute("SELECT COUNT(*) AS n FROM api_keys WHERE enabled = 1").fetchone()["n"] or 0
        )
        top = conn.execute(
            f"SELECT model, COUNT(*) AS n FROM request_logs "
            f"WHERE ts >= ? AND model IS NOT NULL AND {log_success_sql()} "
            f"GROUP BY model ORDER BY n DESC LIMIT 1",
            (week,),
        ).fetchone()
        result["top_model"] = top["model"] if top else None
        result["usage_health"]["logs_today"] = int(
            conn.execute(
                f"SELECT COUNT(*) AS n FROM request_logs WHERE ts >= ? AND {log_success_sql()}",
                (today,),
            ).fetchone()["n"]
            or 0
        )
        return result
    finally:
        conn.close()


@router.get("/daily")
def daily(request: Request, days: int = 30, realm: str | None = None, user: dict = Depends(current_user)):
    days = max(1, min(days, 90))
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
        rows = conn.execute("SELECT * FROM request_logs WHERE ts >= ?", (start,)).fetchall()
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
        for row in conn.execute("SELECT ts, billed_micro FROM billing_ledger WHERE ts >= ?", (start,)):
            label = dt.datetime.fromtimestamp(row["ts"]).date().isoformat()
            if label in micro_by_day:
                micro_by_day[label] += int(row["billed_micro"] or 0)
        for label, bucket in buckets.items():
            bucket["credit"] = to_usd(micro_by_day[label])
        return list(buckets.values())
    finally:
        conn.close()
