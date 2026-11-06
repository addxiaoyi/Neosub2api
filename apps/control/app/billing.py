"""计费旁路：M2 直通用量，M3-1 追加定价换算（只算钱，不拦人）。

M2 的 record_usage 把上游 usage 原样落 usage_records.usage_json；
M3-1 在同一事务里追加 billing_ledger，金额定格为整数 micro-USD。
网关响应路径不受影响：任何计费异常都不能打挂请求。
"""

import datetime as dt
import json
import logging
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

from .usage_estimate import ESTIMATE_VERSION

MICRO_PER_USD = 1_000_000
# token 分类与 usage 字段的对应关系见 split_usage
TOKEN_CLASSES = ("input", "output", "cache_read", "cache_write", "reasoning")
# 与 token 价正交的计费规则：按次、按秒、长上下文倍率
RULE_KINDS = ("per_call", "per_second", "long_context")
# 四舍五入到 1 micro 的补偿项：cost = (tokens * price + _ROUND_HALF) // MICRO_PER_USD
_ROUND_HALF = MICRO_PER_USD // 2

# 配额窗口按固定 UTC+8 解释日/月界（Asia/Shanghai 无夏令时）。
# 不引入 zoneinfo：Windows 上缺 tzdata 时 IANA 时区会直接查不到。
WINDOW_TZ_OFFSET = 8 * 3600
QUOTA_WINDOWS = ("day", "month")
_WINDOW_LABELS = {"day": "日额度", "month": "月额度"}

logger = logging.getLogger("unified.billing")


def record_usage(
    conn: sqlite3.Connection,
    *,
    key_id: int | None,
    key_name: str,
    provider: str,
    model: str,
    status: int,
    stream: bool,
    usage: dict[str, Any] | None,
    latency_ms: int,
    ts: int,
) -> None:
    # 匿名/失效密钥没有归属，显式写 NULL 而不是 0；聚合侧统一用 COALESCE 分桶
    normalized = int(key_id) if key_id is not None else None
    conn.execute(
        "INSERT INTO usage_records(ts, key_id, key_name, provider, model, status, stream, usage_json, latency_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            ts,
            normalized,
            key_name,
            provider,
            model,
            status,
            1 if stream else 0,
            json.dumps(usage, ensure_ascii=False) if usage else "",
            latency_ms,
        ),
    )


def usage_by_key(conn: sqlite3.Connection, *, since: int = 0) -> list[dict[str, Any]]:
    """按 key 归因用量。key_id 为 NULL 的记录单独归到「未归属」，不参与整数主键运算。"""
    rows = conn.execute(
        "SELECT CASE WHEN key_id IS NULL THEN NULL ELSE key_id END AS key_id, "
        "COALESCE(NULLIF(key_name, ''), CASE WHEN key_id IS NULL THEN '未归属' ELSE '已删除密钥' END) AS key_name, "
        "COUNT(*) AS requests "
        "FROM usage_records WHERE ts >= ? "
        "GROUP BY key_id, key_name ORDER BY requests DESC",
        (since,),
    ).fetchall()
    return [dict(row) for row in rows]


def _sse_objects(chunk: bytes) -> Iterator[dict[str, Any]]:
    """逐行解析 SSE 的 data: 负载，解析不成 JSON 的行直接丢掉。"""
    for line in chunk.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == b"[DONE]":
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict):
            yield data


def usage_in_chunk(chunk: bytes) -> dict[str, Any] | None:
    """一个 SSE 分片里的 usage：Chat 放顶层，Anthropic 放 message.usage，Responses 挂 response.usage。

    必须先解析 JSON 再判断，不能靠正文里出现 "usage" 字样猜。
    """
    found: dict[str, Any] | None = None
    for data in _sse_objects(chunk):
        usage = data.get("usage")
        if not isinstance(usage, dict) or not usage:
            message = data.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
        if not (isinstance(usage, dict) and usage):
            # Responses 协议的 usage 在 response 对象下（response.completed 等事件）
            response = data.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
        if isinstance(usage, dict) and usage:
            # Anthropic 把 input_tokens 放 message_start、output_tokens 放 message_delta，合并才完整
            found = {**(found or {}), **usage}
    return found


def extract_usage(payload: bytes) -> dict[str, Any] | None:
    """非流式响应体里取原始 usage 对象（OpenAI 与 Anthropic 都叫 usage）。"""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    usage = data.get("usage") if isinstance(data, dict) else None
    return usage if isinstance(usage, dict) else None


def usage_from_stream(chunks: list[bytes]) -> dict[str, Any] | None:
    """流式响应里捞 usage：OpenAI 在末个 chunk（需 include_usage），Anthropic 在 message_start/message_delta。"""
    found: dict[str, Any] | None = None
    for chunk in chunks:
        part = usage_in_chunk(chunk)
        if part:
            found = {**(found or {}), **part}
    return found


def totals(usage: dict[str, Any] | None) -> tuple[int, int]:
    """从原始 usage 折算 prompt/completion 计数，供 request_logs 汇总列使用。"""
    if not usage:
        return 0, 0
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return prompt, completion


def to_usd(micro: int | None) -> float:
    """micro-USD → 美元。只在展示层用；落库一律整数。"""
    return round(int(micro or 0) / MICRO_PER_USD, 6)


def _int(value: Any) -> int:
    """上游 usage 字段可能是 None / 字符串 / 浮点，统一成非负整数。"""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def split_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    """把上游 usage 拆成五类 token，供按类计价。

    OpenAI（chat/legacy responses）：prompt_tokens 含缓存命中，扣出来按 cache_read 单独计价；
    completion_tokens 含 reasoning，扣出来按 reasoning 计价（缺价格行时回落 output 价）。
    Anthropic：input_tokens 本身不含缓存，cache_read / cache_write 有独立字段。
    细节字段按「存在即判定」，不靠 provider 名猜，避免 responses 的 input_tokens 走错分支。
    """
    empty = {name: 0 for name in TOKEN_CLASSES}
    if not usage:
        return empty

    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")
    completion = usage.get("completion_tokens")
    if completion is None:
        completion = usage.get("output_tokens")
    prompt = _int(prompt)
    completion = _int(completion)

    in_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    out_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    cached = _int(in_details.get("cached_tokens")) if isinstance(in_details, dict) else 0
    reasoning = _int(out_details.get("reasoning_tokens")) if isinstance(out_details, dict) else 0

    is_anthropic = "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage
    cache_read = _int(usage.get("cache_read_input_tokens")) if is_anthropic else cached
    cache_write = _int(usage.get("cache_creation_input_tokens")) if is_anthropic else 0
    # Anthropic 的 input_tokens 不含缓存；OpenAI 的 prompt_tokens 含 cached，必须扣掉才不重复计费
    input_tokens = prompt if is_anthropic else max(prompt - cached, 0)

    return {
        "input": input_tokens,
        "output": max(completion - reasoning, 0),
        "cache_read": cache_read,
        "cache_write": cache_write,
        "reasoning": reasoning,
    }


def resolve_prices(
    conn: sqlite3.Connection,
    *,
    provider: str,
    model: str,
    at: int,
    token_classes: tuple[str, ...] = TOKEN_CLASSES,
) -> dict[str, dict[str, Any]]:
    """解析某模型在 at 时刻生效的价格行：精确 provider/model 优先于 '*'，同精度取最新。

    at 传请求准入时刻（不是落库时刻），配合 append-only 让在途请求不受改价影响。
    """
    lines: dict[str, dict[str, Any]] = {}
    for token_class in token_classes:
        row = conn.execute(
            "SELECT id, provider, model, price_micro_per_1m FROM price_books "
            "WHERE token_class = ? AND effective_from <= ? "
            "AND provider IN (?, '*') AND model IN (?, '*') "
            "ORDER BY (provider <> '*') DESC, (model <> '*') DESC, effective_from DESC "
            "LIMIT 1",
            (token_class, at, provider, model),
        ).fetchone()
        if row is None:
            continue
        lines[token_class] = {
            "price_id": row["id"],
            "provider": row["provider"],
            "model": row["model"],
            "price_micro_per_1m": int(row["price_micro_per_1m"]),
        }
    return lines


def resolve_price_rules(
    conn: sqlite3.Connection,
    *,
    provider: str,
    model: str,
    at: int,
) -> dict[str, dict[str, Any]]:
    """解析某模型在 at 时刻生效的计费规则，按 kind 各取一行。

    优先级与 resolve_prices 一致：精确 provider/model 优先于 '*'，同精度取最新。
    """
    rules: dict[str, dict[str, Any]] = {}
    for kind in RULE_KINDS:
        row = conn.execute(
            "SELECT id, provider, model, kind, unit_micro, threshold, multiplier_milli "
            "FROM price_rules WHERE kind = ? AND effective_from <= ? "
            "AND provider IN (?, '*') AND model IN (?, '*') "
            "ORDER BY (provider <> '*') DESC, (model <> '*') DESC, effective_from DESC "
            "LIMIT 1",
            (kind, at, provider, model),
        ).fetchone()
        if row is None:
            continue
        rules[kind] = {
            "rule_id": row["id"],
            "provider": row["provider"],
            "model": row["model"],
            "kind": row["kind"],
            "unit_micro": int(row["unit_micro"]),
            "threshold": int(row["threshold"]),
            "multiplier_milli": int(row["multiplier_milli"]),
        }
    return rules


def compute_rule_charge_micro(
    rules: dict[str, dict[str, Any]],
    *,
    calls: int = 1,
    seconds: int = 0,
) -> tuple[int, list[str]]:
    """按次与按秒的固定费用；返回 (金额, 命中的规则说明)。

    per_call 每个 settle 记一次；per_second 必须有秒数才计费，缺秒数不猜时长。
    """
    total = 0
    applied: list[str] = []
    per_call = rules.get("per_call")
    if per_call is not None and calls > 0:
        total += calls * int(per_call["unit_micro"])
        applied.append("per_call")
    per_second = rules.get("per_second")
    if per_second is not None and seconds > 0:
        total += seconds * int(per_second["unit_micro"])
        applied.append(f"per_second:{seconds}s")
    return total, applied


def apply_long_context_micro(
    rules: dict[str, dict[str, Any]],
    *,
    input_tokens: int,
    base_micro: int,
) -> tuple[int, str]:
    """输入 token 达到阈值时把 token 金额乘上倍率；倍率为 0 视为无效规则。"""
    rule = rules.get("long_context")
    if rule is None or base_micro <= 0 or input_tokens < int(rule["threshold"]):
        return base_micro, ""
    multiplier = int(rule["multiplier_milli"])
    if multiplier <= 0:
        return base_micro, ""
    return (base_micro * multiplier + 500) // 1000, f"long_context:{multiplier}"


def compute_cost_micro(
    tokens: dict[str, int], prices: dict[str, dict[str, Any]]
) -> tuple[int, list[str]]:
    """按类换算金额，返回 (总额 micro-USD, 缺价的 token 类)。

    每类单独算再相加，整数四位舍五入，单请求最大误差 ≤ 2.5 micro，且两方言行为一致。
    """
    total = 0
    unpriced: list[str] = []
    for name in TOKEN_CLASSES:
        count = int(tokens.get(name) or 0)
        if count <= 0:
            continue
        line = prices.get(name)
        if line is None and name == "reasoning":
            line = prices.get("output")  # 推理没有单独报价时按输出价计
        if line is None:
            unpriced.append(name)
            continue
        total += (count * int(line["price_micro_per_1m"]) + _ROUND_HALF) // MICRO_PER_USD
    return total, unpriced


def record_price_snapshot(
    conn: sqlite3.Connection,
    *,
    at: int,
    provider: str,
    model: str,
    prices: dict[str, dict[str, Any]],
    rules: dict[str, dict[str, Any]] | None = None,
) -> int | None:
    """冻结本次解析结果；token 价与规则都没有时不写空快照。

    规则放在保留键 "rules" 下：token_class 只有固定五种，不会与之冲突。
    """
    if not prices and not rules:
        return None
    payload: dict[str, Any] = {
        name: {
            "price_id": line.get("price_id"),
            "provider": line.get("provider"),
            "model": line.get("model"),
            "price_micro_per_1m": int(line.get("price_micro_per_1m") or 0),
        }
        for name, line in prices.items()
    }
    if rules:
        payload["rules"] = {
            kind: {
                "rule_id": rule.get("rule_id"),
                "provider": rule.get("provider"),
                "model": rule.get("model"),
                "unit_micro": int(rule.get("unit_micro") or 0),
                "threshold": int(rule.get("threshold") or 0),
                "multiplier_milli": int(rule.get("multiplier_milli") or 0),
            }
            for kind, rule in rules.items()
        }
    cur = conn.execute(
        "INSERT INTO price_book_snapshots(resolved_at, provider, model, lines_json) VALUES(?,?,?,?)",
        (at, provider, model, json.dumps(payload, ensure_ascii=False)),
    )
    return cur.lastrowid


def settle(
    conn: sqlite3.Connection,
    *,
    ts: int,
    key_id: int | None,
    key_name: str,
    provider: str,
    model: str,
    status: int,
    usage: dict[str, Any] | None,
    pricing: dict[str, Any] | None = None,
    billing_account_id: int | None = None,
    failed: bool = False,
    log_id: int | None = None,
    estimated_usage: dict[str, Any] | None = None,
    estimate_version: str = ESTIMATE_VERSION,
    seconds: int | None = None,
) -> dict[str, Any]:
    """把一次请求的用量折成金额写流水；不 commit，由调用方与 request_logs 同一事务提交。

    pricing 是准入时 resolve_prices 的结果（{"at", "provider", "model", "lines"}）；
    缺失时按"未定价"处理（金额 0、estimated=1），不是错误。

    failed 由调用方按「request_logs.error 是否非空」传入：断流时状态码仍是 200，
    只看 status 分不出来，必须以错误标记为准。上游正常结束但没有 usage 不算失败。

    log_id 是本次请求在 request_logs 的行 id：写进 log_id 列，
    /api/logs 靠它精确回填逐条金额（不做 key+model+时间窗弱关联，防串账）。

    上游真值永远优先：只要 usage 非空（含全 0），estimated_usage 一律不参与。
    只有 usage 为空、且调用方在开关打开时显式给了 estimated_usage，才落估算行：
    note=usage_estimated:<口径版本>，usage_estimated=1，与 usage_missing/usage_zero 可区分。

    seconds 是视频等按时长计费的请求时长：只有传了秒数且命中 per_second 规则才计费。
    按次与按时长的金额都不可分摊，必须和 token 金额一起在一次 settle 里定格。
    """
    zeros = {name: 0 for name in TOKEN_CLASSES}
    if failed or (not usage and status >= 400):
        # 失败请求没有可计量用量，留痕交给 request_logs，不在这里造零金额行
        return {"written": False, "billed_micro": 0, "estimated": 0, "usage_estimated": 0,
                "note": "", "tokens": zeros}

    lines = (pricing or {}).get("lines") or {}
    usage_estimated = 0
    if usage:
        tokens = split_usage(usage)
        billed_micro, unpriced = compute_cost_micro(tokens, lines)
        if any(tokens.values()):
            note = f"unpriced:{','.join(unpriced)}" if unpriced else ""
        else:
            # 上游回了全零 usage（muse 等无计量通道）：0 元但必须能和真实免费请求区分
            note = f"usage_zero:{provider}" if provider else "usage_zero"
    elif estimated_usage:
        # 网关估算：第四种状态，既不是 billed 也不是 usage_missing
        tokens = split_usage(estimated_usage)
        billed_micro, unpriced = compute_cost_micro(tokens, lines)
        usage_estimated = 1
        note = f"usage_estimated:{estimate_version}"
        if unpriced:
            note += f";unpriced:{','.join(unpriced)}"
    else:
        tokens = zeros
        billed_micro, unpriced = 0, []
        note = "usage_missing"
    at = int((pricing or {}).get("at") or ts)
    rules = resolve_price_rules(conn, provider=provider, model=model, at=at)
    billed_micro, long_note = apply_long_context_micro(
        rules, input_tokens=tokens["input"], base_micro=billed_micro
    )
    rule_micro, rule_notes = compute_rule_charge_micro(rules, seconds=max(int(seconds or 0), 0))
    billed_micro += rule_micro
    if note == "usage_missing" and rule_notes:
        # 按次/按秒价已定格：这次请求有金额，只是上游没给 token 用量
        note = ""
    note = ";".join(part for part in (note, *rule_notes, long_note) if part)
    estimated = 1 if unpriced else 0
    snapshot_id = record_price_snapshot(
        conn,
        at=at,
        provider=provider,
        model=model,
        prices=lines,
        rules=rules,
    )
    conn.execute(
        "INSERT INTO billing_ledger(ts, key_id, key_name, billing_account_id, provider, model, status, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, "
        "billed_micro, price_snapshot_id, estimated, note, log_id, usage_estimated) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ts,
            key_id,
            key_name,
            billing_account_id,
            provider,
            model,
            status,
            tokens["input"],
            tokens["output"],
            tokens["cache_read"],
            tokens["cache_write"],
            tokens["reasoning"],
            billed_micro,
            snapshot_id,
            estimated,
            note,
            int(log_id) if log_id is not None else None,
            usage_estimated,
        ),
    )
    # 用量刚发生变化，顺带评估阈值；告警失败不能带走已写的流水
    try:
        evaluate_alerts(conn, key_id=key_id, provider=provider, model=model, at=ts)
    except Exception:  # noqa: BLE001
        logger.exception("配额告警评估失败 key_id=%s provider=%s model=%s", key_id, provider, model)
    return {
        "written": True,
        "billed_micro": billed_micro,
        "estimated": estimated,
        "usage_estimated": usage_estimated,
        "note": note,
        "snapshot_id": snapshot_id,
        "tokens": tokens,
    }


def period_bounds(window: str, at: int) -> tuple[int, int]:
    """返回 (周期起点, 周期终点) epoch 秒；日/月界按 UTC+8 计算。

    惰性周期：不跑清零任务，按当前时刻算出周期再聚合 billing_ledger。
    """
    if window not in QUOTA_WINDOWS:
        raise ValueError(f"未知配额周期：{window!r}")
    tz = dt.timezone(dt.timedelta(seconds=WINDOW_TZ_OFFSET))
    local = dt.datetime.fromtimestamp(int(at), tz=tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "month":
        start = start.replace(day=1)
        end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    else:
        end = start + dt.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def _policy_scope(policy: sqlite3.Row, start: int, end: int) -> tuple[str, list[Any]]:
    """策略的计量范围：key 必选，provider / model 为空表示不限。"""
    where = "key_id = ? AND ts >= ? AND ts < ?"
    params: list[Any] = [policy["key_id"], start, end]
    if policy["provider"]:
        where += " AND provider = ?"
        params.append(policy["provider"])
    if policy["model"]:
        where += " AND model = ?"
        params.append(policy["model"])
    return where, params


def matching_policies(
    conn: sqlite3.Connection, *, key_id: int, provider: str, model: str
) -> list[sqlite3.Row]:
    """命中当前请求的策略：key 维度必配，provider/model 可收窄。"""
    return conn.execute(
        "SELECT * FROM quota_policies WHERE key_id = ? AND enabled = 1 "
        "AND (provider = '' OR provider = ?) AND (model = '' OR model = ?) ORDER BY id",
        (key_id, provider, model),
    ).fetchall()


def check_failure_rate(
    conn: sqlite3.Connection, *, key_id: int | None, provider: str = "", window_sec: int, max_failures: int, at: int
) -> dict[str, Any] | None:
    """失败请求滑动窗口限流：窗内失败条数达到上限就返回拒绝详情，否则 None。

    计数直接聚合 request_logs（status>=400 或 error 非空），不另起累加器，
    这样日志与限流只有一份真相。窗口是滑动的：最早一条失败记录过期即释放名额，
    因此 reset_after 取「最早一条失败 + 窗长 - 当前时刻」，而不是整段窗口重开。
    429 是本层自己的拒绝（quota / 失败限流），不计入失败窗口：否则客户端被拒后
    持续重试会不断续上窗口，永远退不出来。

    计数按 (key, provider) 隔离：一家上游故障不能把同一把 key 下的其他 provider 连坐。
    provider 传空串时保持旧口径（只按 key 聚合），供显式运维查询使用。
    """
    if key_id is None or max_failures <= 0 or window_sec <= 0:
        return None
    since = int(at) - int(window_sec)
    where = (
        "key_id = ? AND ts >= ? AND status <> 429 "
        "AND (status >= 400 OR COALESCE(error, '') <> '')"
    )
    params: list[Any] = [int(key_id), since]
    if provider:
        where += " AND provider = ?"
        params.append(provider)
    row = conn.execute(
        f"SELECT COUNT(*) AS n, MIN(ts) AS first_ts FROM request_logs WHERE {where}",
        params,
    ).fetchone()
    used = int(row["n"] or 0)
    if used < max_failures:
        return None
    first_ts = int(row["first_ts"] or at)
    reset_after = max(1, first_ts + int(window_sec) - int(at))
    detail = {
        "type": "failure_rate_limited",
        "layer": "key",
        "metric": "failures",
        "used": used,
        "limit": int(max_failures),
        "unit": "requests",
        "window": "sliding",
        "window_sec": int(window_sec),
        "reset_at": int(at) + reset_after,
        "reset_after": reset_after,
        "message": (
            f"{int(window_sec)} 秒内失败请求已达 {used} 次，上限 {int(max_failures)} 次；"
            f"请在 {reset_after} 秒后重试"
        ),
    }
    if provider:
        detail["provider"] = provider  # 计数按 (key, provider) 隔离，拒因要能指认是哪家
    return detail


def window_usage(conn: sqlite3.Connection, policy: sqlite3.Row, at: int) -> dict[str, Any]:
    """从 billing_ledger 聚合当前周期用量，金额与请求数同一口径，不另起累加器。"""
    start, end = period_bounds(policy["window"], at)
    where, params = _policy_scope(policy, start, end)
    row = conn.execute(
        f"SELECT COALESCE(SUM(billed_micro), 0) AS micro, COUNT(*) AS requests, "
        f"COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens "
        f"+ cache_write_tokens + reasoning_tokens), 0) AS tokens "
        f"FROM billing_ledger WHERE {where}",
        params,
    ).fetchone()
    return {
        "used_micro": int(row["micro"] or 0),
        "used_requests": int(row["requests"] or 0),
        "used_tokens": int(row["tokens"] or 0),
        "period_start": start,
        "period_end": end,
        "reset_at": end,
        "reset_after": max(0, end - int(at)),
    }


def check_quota(
    conn: sqlite3.Connection, *, key_id: int | None, provider: str, model: str, at: int
) -> dict[str, Any] | None:
    """准入校验：任一命中策略已达上限就返回拒绝详情，否则 None。

    只判断「已用 >= 上限」是否成立，不做预估预扣。
    """
    if key_id is None:
        return None
    for policy in matching_policies(conn, key_id=key_id, provider=provider, model=model):
        usage = window_usage(conn, policy, at)
        if policy["limit_micro"] > 0 and usage["used_micro"] >= policy["limit_micro"]:
            metric = "micro"
        elif policy["limit_requests"] > 0 and usage["used_requests"] >= policy["limit_requests"]:
            metric = "requests"
        elif policy["limit_tokens"] > 0 and usage["used_tokens"] >= policy["limit_tokens"]:
            metric = "tokens"
        else:
            continue
        if metric == "micro":
            used, limit = usage["used_micro"], int(policy["limit_micro"])
            unit, label = "micro-USD", "金额"
        elif metric == "requests":
            used, limit = usage["used_requests"], int(policy["limit_requests"])
            unit, label = "次请求", "请求数"
        else:
            used, limit = usage["used_tokens"], int(policy["limit_tokens"])
            unit, label = "token", "token"
        reset_local = dt.datetime.fromtimestamp(
            usage["reset_at"], tz=dt.timezone(dt.timedelta(seconds=WINDOW_TZ_OFFSET))
        )
        return {
            "type": "quota_exceeded",
            "layer": "key",
            "policy_id": policy["id"],
            "key_id": policy["key_id"],
            "window": policy["window"],
            "metric": metric,
            "used": used,
            "limit": limit,
            "unit": "micro_usd" if metric == "micro" else metric,
            "period_start": usage["period_start"],
            "reset_at": usage["reset_at"],
            "reset_after": usage["reset_after"],
            "message": (
                f"{_WINDOW_LABELS[policy['window']]}（{label}）已用 {used} {unit}，上限 {limit} {unit}；"
                f"周期重置于 {reset_local:%Y-%m-%d %H:%M:%S}（UTC+8）"
            ),
        }
    return None


_QUOTA_METRIC_FIELDS = {
    "micro": ("used_micro", "limit_micro"),
    "requests": ("used_requests", "limit_requests"),
    "tokens": ("used_tokens", "limit_tokens"),
}


def _metric_ratio(metric: str, usage: dict[str, Any], policy: sqlite3.Row) -> float:
    """占用比例：多类同时跨线时用它挑最该报的那一类。"""
    used_key, limit_key = _QUOTA_METRIC_FIELDS[metric]
    limit = int(policy[limit_key] or 0)
    return (int(usage[used_key]) / limit) if limit else 0.0


def _alert_for_threshold(
    conn: sqlite3.Connection,
    *,
    policy: sqlite3.Row,
    usage: dict[str, Any],
    percent: int,
    at: int,
) -> dict[str, Any] | None:
    """单条阈值：本周期首次跨线才落一行，已告警或未命中返回 None。"""
    micro_hit = policy["limit_micro"] > 0 and (
        usage["used_micro"] * 100 >= int(policy["limit_micro"]) * percent
    )
    requests_hit = policy["limit_requests"] > 0 and (
        usage["used_requests"] * 100 >= int(policy["limit_requests"]) * percent
    )
    tokens_hit = policy["limit_tokens"] > 0 and (
        usage["used_tokens"] * 100 >= int(policy["limit_tokens"]) * percent
    )
    hits = [
        name
        for name, hit in (("micro", micro_hit), ("requests", requests_hit), ("tokens", tokens_hit))
        if hit
    ]
    if not hits:
        return None
    if len(hits) > 1:
        # 多类都跨了只报占用比例最高的那一类，避免同一阈值刷多条
        metric = max(hits, key=lambda name: _metric_ratio(name, usage, policy))
    else:
        metric = hits[0]
    existing = conn.execute(
        "SELECT id FROM quota_alerts WHERE policy_id = ? AND threshold_percent = ? "
        "AND period_start = ?",
        (policy["id"], percent, usage["period_start"]),
    ).fetchone()
    if existing is not None:
        return None
    conn.execute(
        "INSERT INTO quota_alerts(policy_id, threshold_percent, window, period_start, "
        "triggered_at, metric, used_micro, limit_micro, used_requests, limit_requests, "
        "used_tokens, limit_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            policy["id"],
            percent,
            policy["window"],
            usage["period_start"],
            int(at),
            metric,
            usage["used_micro"],
            int(policy["limit_micro"]),
            usage["used_requests"],
            int(policy["limit_requests"]),
            usage["used_tokens"],
            int(policy["limit_tokens"]),
        ),
    )
    return {
        "policy_id": policy["id"],
        "key_id": policy["key_id"],
        "threshold_percent": percent,
        "window": policy["window"],
        "period_start": usage["period_start"],
        "triggered_at": int(at),
        "metric": metric,
        "used_micro": usage["used_micro"],
        "limit_micro": int(policy["limit_micro"]),
        "used_requests": usage["used_requests"],
        "limit_requests": int(policy["limit_requests"]),
        "used_tokens": usage["used_tokens"],
        "limit_tokens": int(policy["limit_tokens"]),
    }


def evaluate_alerts(
    conn: sqlite3.Connection, *, key_id: int | None, provider: str, model: str, at: int
) -> list[dict[str, Any]]:
    """跨阈值记一条告警；同策略、同阈值、同周期只记一次，周期翻转后重新允许。

    在 settle 写完整流水后调用，所以聚合结果天然包含本次请求的金额与次数。

    逐条隔离：单条阈值（或单条策略的周期聚合）抛异常只记日志并跳过本轮，
    同请求内的后续阈值与后续策略照常评估，失败的那条交给下一次请求重评。
    """
    if key_id is None:
        return []
    created: list[dict[str, Any]] = []
    for policy in matching_policies(conn, key_id=key_id, provider=provider, model=model):
        try:
            usage = window_usage(conn, policy, at)
            thresholds = conn.execute(
                "SELECT percent FROM quota_alert_thresholds WHERE policy_id = ? ORDER BY percent",
                (policy["id"],),
            ).fetchall()
        except Exception:  # noqa: BLE001 - 单条策略聚合失败不能带走其余策略
            logger.exception(
                "配额告警周期聚合失败 policy_id=%s key_id=%s provider=%s model=%s",
                policy["id"], key_id, provider, model,
            )
            continue
        for threshold in thresholds:
            percent = int(threshold["percent"])
            try:
                alert = _alert_for_threshold(
                    conn, policy=policy, usage=usage, percent=percent, at=at
                )
            except Exception:  # noqa: BLE001 - 单条阈值失败不能带走同策略后续阈值
                logger.exception(
                    "配额告警阈值评估失败 policy_id=%s threshold=%s key_id=%s provider=%s "
                    "model=%s window=%s",
                    policy["id"], percent, policy["key_id"], provider, model, policy["window"],
                )
                continue
            if alert is not None:
                created.append(alert)
    return created


def _load_seed_yaml(path: Path) -> dict[str, Any]:
    """读 prices.yaml 并做通用校验；两个导入函数共用同一套报错口径。"""
    try:
        import yaml
    except ImportError as exc:  # 运行时缺依赖时给出可执行的修复指令
        raise RuntimeError("导入价格表需要 PyYAML：pip install PyYAML") from exc

    try:
        raw_text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"读取 prices.yaml 失败：{exc}") from exc
    # 空文件喂给 safe_load 只会得到 None，必须单独拦下：否则界面拿到 0 条却以为导入成功
    if not raw_text.strip():
        raise ValueError("prices.yaml 内容为空，没有可导入的价格条目")
    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"第 {mark.line + 1} 行第 {mark.column + 1} 列：" if mark else ""
        raise ValueError(f"prices.yaml 解析失败（{where}{getattr(exc, 'problem', None) or exc}）") from exc
    if not isinstance(data, dict):
        raise ValueError("prices.yaml 顶层必须是映射")
    return data


def import_price_books(conn: sqlite3.Connection, path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """把 config/prices.yaml 的种子价导入 price_books；重复版本跳过，不覆盖旧行。

    unpriced 段只回传给调用方（前端/接口标注用），不落表：没有公开价就不编价。
    """
    data = _load_seed_yaml(path)
    rows = data.get("prices")
    if rows is None:
        raise ValueError("prices.yaml 缺少 prices 段")
    if not isinstance(rows, list):
        raise ValueError("prices 必须是列表")
    if not rows:
        raise ValueError("prices.yaml 的 prices 段为空，没有可导入的价格条目")
    if not isinstance(data.get("unpriced") or [], list):
        raise ValueError("prices.yaml 的 unpriced 段必须是列表")

    inserted = 0
    skipped = 0
    for index, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"prices 第 {index} 条不是映射")
        token_class = str(item.get("token_class") or "")
        if token_class not in TOKEN_CLASSES:
            raise ValueError(f"prices 第 {index} 条 token_class 非法：{token_class!r}")
        micro = item.get("price_micro_per_1m")
        if micro is None:
            raw = item.get("price_per_mtok")
            if raw is None:
                raise ValueError(f"prices 第 {index} 条缺 price_micro_per_1m / price_per_mtok")
            micro = int((Decimal(str(raw)) * MICRO_PER_USD).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        micro = int(micro)
        if micro < 0:
            raise ValueError(f"prices 第 {index} 条价格为负：{micro}")
        provider = str(item.get("provider") or "*").strip() or "*"
        model = str(item.get("model") or "").strip()
        if not model:
            raise ValueError(f"prices 第 {index} 条缺 model")
        effective_from = int(item.get("effective_from") or data.get("default_effective_from") or 0)

        exists = conn.execute(
            "SELECT id FROM price_books WHERE provider = ? AND model = ? AND token_class = ? "
            "AND effective_from = ?",
            (provider, model, token_class, effective_from),
        ).fetchone()
        if exists is not None:
            skipped += 1
            continue
        inserted += 1
        if dry_run:
            continue
        conn.execute(
            "INSERT INTO price_books(provider, model, token_class, price_micro_per_1m, currency, "
            "effective_from, created_by, note) VALUES(?,?,?,?,?,?,?,?)",
            (
                provider,
                model,
                token_class,
                micro,
                str(data.get("currency") or "USD"),
                effective_from,
                "prices.yaml",
                str(item.get("note") or ""),
            ),
        )

    unpriced = [
        str(item.get("model") or "")
        for item in (data.get("unpriced") or [])
        if isinstance(item, dict) and item.get("model")
    ]
    return {
        "inserted": inserted,
        "skipped": skipped,
        "total": len(rows),
        "dry_run": dry_run,
        "currency": str(data.get("currency") or "USD"),
        "unpriced": unpriced,
    }


def import_price_rules(conn: sqlite3.Connection, path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """把 prices.yaml 的 rules 段导入 price_rules；重复版本跳过。

    rules 段可以整段缺失（老文件）：缺失按 0 条处理，不报错。
    rules_pending 段与 unpriced 同理，只在接口层回传，不落表。
    """
    data = _load_seed_yaml(path)
    rows = data.get("rules") or []
    if not isinstance(rows, list):
        raise ValueError("rules 必须是列表")
    if not isinstance(data.get("rules_pending") or [], list):
        raise ValueError("prices.yaml 的 rules_pending 段必须是列表")

    inserted = 0
    skipped = 0
    for index, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"rules 第 {index} 条不是映射")
        kind = str(item.get("kind") or "")
        if kind not in RULE_KINDS:
            raise ValueError(f"rules 第 {index} 条 kind 非法：{kind!r}")
        provider = str(item.get("provider") or "*").strip() or "*"
        model = str(item.get("model") or "").strip() or "*"
        unit_micro = int(item.get("unit_micro") or 0)
        threshold = int(item.get("threshold") or 0)
        multiplier_milli = int(item.get("multiplier_milli") or 1000)
        if unit_micro < 0 or threshold < 0 or multiplier_milli < 0:
            raise ValueError(f"rules 第 {index} 条出现负数")
        if kind == "long_context" and (threshold <= 0 or multiplier_milli <= 0):
            raise ValueError(f"rules 第 {index} 条 long_context 需要正的 threshold 与 multiplier_milli")
        effective_from = int(item.get("effective_from") or data.get("default_effective_from") or 0)
        exists = conn.execute(
            "SELECT id FROM price_rules WHERE provider = ? AND model = ? AND kind = ? "
            "AND effective_from = ?",
            (provider, model, kind, effective_from),
        ).fetchone()
        if exists is not None:
            skipped += 1
            continue
        inserted += 1
        if dry_run:
            continue
        conn.execute(
            "INSERT INTO price_rules(provider, model, kind, unit_micro, threshold, multiplier_milli, "
            "effective_from, created_by, note) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                provider,
                model,
                kind,
                unit_micro,
                threshold,
                multiplier_milli,
                effective_from,
                "prices.yaml",
                str(item.get("note") or ""),
            ),
        )

    pending = [
        str(item.get("model") or "")
        for item in (data.get("rules_pending") or [])
        if isinstance(item, dict) and item.get("model")
    ]
    return {
        "inserted": inserted,
        "skipped": skipped,
        "total": len(rows),
        "dry_run": dry_run,
        "pending": pending,
    }
