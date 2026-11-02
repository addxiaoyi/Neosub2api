import datetime as dt

from fastapi import APIRouter, Depends, Request

from ..api_utils import now
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
            "SELECT ts, status, prompt_tokens, completion_tokens, model FROM request_logs WHERE ts >= ?",
            (week,),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS n, SUM(prompt_tokens + completion_tokens) AS tokens "
            "FROM request_logs"
        ).fetchone()
        for row in rows:
            tokens = int(row["prompt_tokens"] or 0) + int(row["completion_tokens"] or 0)
            is_ok = row["status"] < 400
            if is_ok:
                result["week_requests"] += 1
                result["week_tokens"] += tokens
            if row["ts"] >= today:
                if is_ok:
                    result["today_requests"] += 1
                    result["today_tokens"] += tokens
                elif row["status"] < 500:
                    result["failures"]["today_4xx"] += 1
                else:
                    result["failures"]["today_5xx"] += 1
            if not is_ok:
                if row["status"] < 500:
                    result["failures"]["week_4xx"] += 1
                else:
                    result["failures"]["week_5xx"] += 1
        result["total_requests"] = int(total["n"] or 0)
        result["total_tokens"] = int(total["tokens"] or 0)
        result["active_keys"] = int(
            conn.execute("SELECT COUNT(*) AS n FROM api_keys WHERE enabled = 1").fetchone()["n"] or 0
        )
        top = conn.execute(
            "SELECT model, COUNT(*) AS n FROM request_logs "
            "WHERE ts >= ? AND model IS NOT NULL AND status < 400 "
            "GROUP BY model ORDER BY n DESC LIMIT 1",
            (week,),
        ).fetchone()
        result["top_model"] = top["model"] if top else None
        result["usage_health"]["logs_today"] = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM request_logs WHERE ts >= ? AND status < 400",
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
            if row["status"] < 400:
                bucket["requests"] += 1
                bucket["prompt_tokens"] += int(row["prompt_tokens"] or 0)
                bucket["completion_tokens"] += int(row["completion_tokens"] or 0)
            else:
                bucket["failed"] += 1
        return list(buckets.values())
    finally:
        conn.close()
