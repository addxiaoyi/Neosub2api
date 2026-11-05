"""配额策略与告警（M3-2 / M3-3）：按 u2a_ 密钥设日/月额度，可再按 provider/模型收窄。

`key_id` 入参分两条通道：query 只接受数字 `api_keys.id`，完整 `u2a_` 密钥不允许
出现在 URL（会进浏览器历史、Referer、反代/访问日志），出现即 400；确实要按密钥
定位时，GET 用 `X-Api-Key` 请求头、POST 用 body，服务端按 SHA-256 精确匹配同一条
记录。出参统一带数字 `key_id`、`key_name` 与掩码后的 `key_prefix`，不需要前端再
自己做标识映射。

告警单独落 `quota_alerts` 而不是 `audit_logs`：告警需要结构化字段（阈值、周期起点、
哪类额度、当前用量）供状态接口和后续前端直接读取；audit_logs 是纯文本审计流，
塞 JSON 进去既不好查也不好按周期去重。`UNIQUE(policy_id, threshold_percent, period_start)`
从库层保证「每周期每阈值只告警一次」，周期翻转后 period_start 变化自然重新允许。

告警列表用 `limit` + `offset` 分页（保持既有 `limit` 语义，新增 `offset` 是最小
增量）；`total` 由独立 COUNT 查询给出真实总数，不再等于截断后的本次条数。

归属：`api_keys.owner_user_id` 存用户名（users 表主键是 TEXT）。admin 可查全部、
可按 key 过滤；非 admin 只能查自有 key——不带过滤条件时只返回自己名下 key 的数据，
显式查别人的 key 返回结构化 403，NULL 归属的老 key 对非 admin 不可见。
"""

import hashlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ..api_utils import now
from ..alerting import evaluate_alerts
from ..billing import QUOTA_WINDOWS, to_usd, window_usage
from ..key_prefix import KEY_PREFIX, mask_prefix
from ..security import PermissionDenied, admin_user, audit_event, current_user

router = APIRouter(prefix="/api/quota", tags=["quota"])
logger = logging.getLogger(__name__)

MAX_PERCENT = 1000
DENIED_MESSAGE = "无权查看该密钥的配额数据"


def _owner(user: dict) -> str | None:
    """admin 返回 None（不做归属限制）；其他角色返回自己的用户名。"""
    if user.get("role") == "admin":
        return None
    return user.get("username") or ""


def _assert_owned(row, user: dict):
    """非 admin 访问他人 key（含 NULL 归属）一律 403，保持 permission_denied 形状。"""
    owner = _owner(user)
    if owner is None:
        return row
    if (row["owner_user_id"] or "") != owner:
        raise PermissionDenied(DENIED_MESSAGE, role=user.get("role"))
    return row


def _key_by_token(conn, token: str):
    """完整 u2a_ 密钥按哈希精确匹配；只允许从请求头/POST body 进来。"""
    ref = token.strip()
    if not ref.startswith(KEY_PREFIX) or len(ref) <= len(KEY_PREFIX):
        raise HTTPException(400, "完整密钥必须是 u2a_ 开头的字符串")
    digest = hashlib.sha256(ref.encode()).hexdigest()
    row = conn.execute("SELECT * FROM api_keys WHERE key_hash = ?", (digest,)).fetchone()
    if row is None:
        raise HTTPException(404, "密钥不存在：u2a_ 密钥无效或已被吊销")
    return row


def _key_from_query(conn, raw: str | None):
    """query 只认数字 id；完整密钥进 URL 会落到各类访问日志，直接拒绝。"""
    if raw is None:
        return None
    ref = str(raw).strip()
    if not ref:
        raise HTTPException(400, "key_id 不能为空：请传数字 api_keys.id")
    if ref.startswith(KEY_PREFIX):
        raise HTTPException(
            400, "密钥不要放在 URL，请用 X-Api-Key 请求头"
        )
    if not ref.isdigit():
        raise HTTPException(
            400, "key_id 只接受数字 api_keys.id；完整密钥请用 X-Api-Key 请求头"
        )
    row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (int(ref),)).fetchone()
    if row is None:
        raise HTTPException(404, f"密钥不存在：{ref}")
    return row


def _key_from_body(conn, raw: int | str):
    """POST body 不经过 URL，可以带完整 u2a_ 密钥或数字 id。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise HTTPException(400, "key_id 只接受数字 id 或 u2a_ 密钥")
    ref = str(raw).strip()
    if not ref:
        raise HTTPException(400, "key_id 不能为空：请传数字 id 或 u2a_ 密钥")
    if ref.startswith(KEY_PREFIX):
        return _key_by_token(conn, ref)
    if ref.isdigit():
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (int(ref),)).fetchone()
        if row is None:
            raise HTTPException(404, f"密钥不存在：{ref}")
        return row
    raise HTTPException(400, "key_id 只接受数字 id 或 u2a_ 密钥")


def _scope_key(conn, key_id: str | None, api_key: str | None, user: dict):
    """GET 的密钥范围：query 数字 id 或 X-Api-Key，二选一；非 admin 校验归属。"""
    if api_key:
        if key_id is not None:
            raise HTTPException(400, "key_id 与 X-Api-Key 不能同时使用")
        return _assert_owned(_key_by_token(conn, api_key), user)
    key = _key_from_query(conn, key_id)
    return _assert_owned(key, user) if key is not None else None


def _mask_prefix(prefix: str) -> str:
    """只回显前缀后 4 位 + 掩码，任何场景都不输出完整密钥。"""
    if not prefix:
        return ""
    return mask_prefix(prefix)


def _thresholds(conn, policy_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT percent FROM quota_alert_thresholds WHERE policy_id = ? ORDER BY percent",
        (policy_id,),
    ).fetchall()
    return [int(row["percent"]) for row in rows]


def _policy_public(conn, row) -> dict[str, Any]:
    key = conn.execute(
        "SELECT name, prefix FROM api_keys WHERE id = ?", (row["key_id"],)
    ).fetchone()
    return {
        "id": row["id"],
        "key_id": row["key_id"],
        "key_name": key["name"] if key else "",
        "key_prefix": _mask_prefix(key["prefix"]) if key else "",
        "provider": row["provider"],
        "model": row["model"],
        "window": row["window"],
        "limit_micro": int(row["limit_micro"]),
        "limit_usd": to_usd(row["limit_micro"]),
        "limit_requests": int(row["limit_requests"]),
        "enabled": bool(row["enabled"]),
        "thresholds": _thresholds(conn, row["id"]),
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
    }


def _alert_public(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "policy_id": row["policy_id"],
        "kind": row["kind"] or "quota",
        "threshold_percent": int(row["threshold_percent"]),
        "window": row["window"],
        "period_start": int(row["period_start"]),
        "triggered_at": int(row["triggered_at"]),
        "metric": row["metric"],
        "used_micro": int(row["used_micro"]),
        "used_usd": to_usd(row["used_micro"]),
        "limit_micro": int(row["limit_micro"]),
        "used_requests": int(row["used_requests"]),
        "limit_requests": int(row["limit_requests"]),
    }


@router.get("/policies")
def list_policies(
    request: Request,
    key_id: str | None = None,
    api_key: str | None = Header(default=None, alias="X-Api-Key"),
    user: dict = Depends(current_user),
):
    conn = request.app.state.db.connect()
    try:
        key = _scope_key(conn, key_id, api_key, user)
        owner = _owner(user)
        if key is None and owner is None:
            rows = conn.execute("SELECT * FROM quota_policies ORDER BY id").fetchall()
        elif key is None:
            rows = conn.execute(
                "SELECT * FROM quota_policies "
                "WHERE key_id IN (SELECT id FROM api_keys WHERE owner_user_id = ?) ORDER BY id",
                (owner,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM quota_policies WHERE key_id = ? ORDER BY id", (int(key["id"]),)
            ).fetchall()
        return [_policy_public(conn, row) for row in rows]
    finally:
        conn.close()


class PolicyIn(BaseModel):
    key_id: int | str
    window: str
    provider: str = ""
    model: str = ""
    limit_micro: int = 0
    limit_requests: int = 0
    thresholds: list[int] = Field(default_factory=list)
    enabled: bool = True


def _clean_periods(raw: list[int]) -> list[int]:
    percents = sorted({int(value) for value in raw})
    for percent in percents:
        if not (1 <= percent <= MAX_PERCENT):
            raise HTTPException(400, f"阈值百分比需在 1-{MAX_PERCENT} 之间：{percent}")
    return percents


@router.post("/policies")
def upsert_policy(body: PolicyIn, request: Request, user: dict = Depends(admin_user)):
    window = (body.window or "").strip().lower()
    if window not in QUOTA_WINDOWS:
        raise HTTPException(400, f"window 必须是 {'/'.join(QUOTA_WINDOWS)}")
    if body.limit_micro < 0 or body.limit_requests < 0:
        raise HTTPException(400, "额度上限不能为负")
    if body.limit_micro == 0 and body.limit_requests == 0:
        raise HTTPException(400, "金额上限与请求数上限至少设置一项（0 表示不设）")
    provider = (body.provider or "").strip()
    provider = "" if provider == "*" else provider
    model = (body.model or "").strip()
    model = "" if model == "*" else model
    percents = _clean_periods(body.thresholds)

    conn = request.app.state.db.connect()
    try:
        key = _key_from_body(conn, body.key_id)
        key_id = int(key["id"])
        existing = conn.execute(
            "SELECT id FROM quota_policies WHERE key_id = ? AND provider = ? AND model = ? AND window = ?",
            (key_id, provider, model, window),
        ).fetchone()
        stamp = now()
        if existing is None:
            cur = conn.execute(
                "INSERT INTO quota_policies(key_id, provider, model, window, limit_micro, "
                "limit_requests, enabled, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (key_id, provider, model, window, body.limit_micro, body.limit_requests,
                 1 if body.enabled else 0, stamp, stamp),
            )
            policy_id = cur.lastrowid
            action = "quota.policy.create"
        else:
            policy_id = existing["id"]
            conn.execute(
                "UPDATE quota_policies SET limit_micro = ?, limit_requests = ?, enabled = ?, "
                "updated_at = ? WHERE id = ?",
                (body.limit_micro, body.limit_requests, 1 if body.enabled else 0, stamp, policy_id),
            )
            action = "quota.policy.update"
        # 阈值整体替换；已发出的告警按 (策略, 百分比, 周期) 去重，不会因重建阈值行而重复
        conn.execute("DELETE FROM quota_alert_thresholds WHERE policy_id = ?", (policy_id,))
        for percent in percents:
            conn.execute(
                "INSERT INTO quota_alert_thresholds(policy_id, percent, created_at) VALUES(?,?,?)",
                (policy_id, percent, stamp),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM quota_policies WHERE id = ?", (policy_id,)).fetchone()
        result = _policy_public(conn, row)
        audit_event(
            conn,
            user.get("username") or "",
            action,
            f"quota_policy:{policy_id}",
            f"key_id={key_id} window={window} provider={provider!r} model={model!r} "
            f"micro={body.limit_micro} requests={body.limit_requests} thresholds={percents}",
        )
        return result
    finally:
        conn.close()


@router.delete("/policies/{policy_id}")
def delete_policy(policy_id: int, request: Request, user: dict = Depends(admin_user)):
    conn = request.app.state.db.connect()
    try:
        row = conn.execute("SELECT * FROM quota_policies WHERE id = ?", (policy_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "配额策略不存在")
        conn.execute("DELETE FROM quota_alert_thresholds WHERE policy_id = ?", (policy_id,))
        conn.execute("DELETE FROM quota_policies WHERE id = ?", (policy_id,))
        conn.commit()
        # 告警是历史事实，不随策略删除而消失
        audit_event(conn, user.get("username") or "", "quota.policy.delete", f"quota_policy:{policy_id}",
                    f"key_id={row['key_id']} window={row['window']}")
    finally:
        conn.close()
    return {"ok": True, "deleted": policy_id}


def _status_item(conn, policy, at: int) -> dict[str, Any]:
    usage = window_usage(conn, policy, at)
    limit_micro = int(policy["limit_micro"])
    limit_requests = int(policy["limit_requests"])
    alerts = conn.execute(
        "SELECT * FROM quota_alerts WHERE policy_id = ? AND period_start = ? "
        "ORDER BY threshold_percent",
        (policy["id"], usage["period_start"]),
    ).fetchall()
    return {
        "policy": _policy_public(conn, policy),
        "period_start": usage["period_start"],
        "period_end": usage["period_end"],
        "reset_at": usage["reset_at"],
        "reset_after": usage["reset_after"],
        "used_micro": usage["used_micro"],
        "used_usd": to_usd(usage["used_micro"]),
        "used_requests": usage["used_requests"],
        "remaining_micro": max(0, limit_micro - usage["used_micro"]) if limit_micro else None,
        "remaining_requests": max(0, limit_requests - usage["used_requests"]) if limit_requests else None,
        "ratio_micro": round(usage["used_micro"] / limit_micro, 6) if limit_micro else None,
        "ratio_requests": round(usage["used_requests"] / limit_requests, 6) if limit_requests else None,
        "exceeded": bool(
            (limit_micro and usage["used_micro"] >= limit_micro)
            or (limit_requests and usage["used_requests"] >= limit_requests)
        ),
        "alerts": [_alert_public(row) for row in alerts],
    }


@router.get("/status")
def get_status(
    request: Request,
    key_id: str | None = None,
    at: int | None = None,
    api_key: str | None = Header(default=None, alias="X-Api-Key"),
    user: dict = Depends(current_user),
):
    """额度用量与告警状态：金额/请求数都直接聚合 billing_ledger。"""
    moment = now() if at is None else int(at)
    conn = request.app.state.db.connect()
    try:
        key = _scope_key(conn, key_id, api_key, user)
        owner = _owner(user)
        if key is None and owner is None:
            rows = conn.execute("SELECT * FROM quota_policies ORDER BY id").fetchall()
        elif key is None:
            rows = conn.execute(
                "SELECT * FROM quota_policies "
                "WHERE key_id IN (SELECT id FROM api_keys WHERE owner_user_id = ?) ORDER BY id",
                (owner,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM quota_policies WHERE key_id = ? ORDER BY id", (int(key["id"]),)
            ).fetchall()
        return {
            "at": moment,
            "items": [_status_item(conn, row, moment) for row in rows],
        }
    finally:
        conn.close()


@router.get("/alerts")
def list_alerts(
    request: Request,
    key_id: str | None = None,
    policy_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
    api_key: str | None = Header(default=None, alias="X-Api-Key"),
    user: dict = Depends(current_user),
):
    """告警历史：`limit` + `offset` 分页，`total` 为过滤后的真实总数。

    不传分页参数时 `offset=0`、沿用原 `limit` 上限，响应仍是 `{total, items}`，
    现有前端不需要改形状；`limit` 仍夹逼到 1–1000，避免一次拉全表。
    """
    take = max(1, min(limit, 1000))
    start = max(0, offset)
    conn = request.app.state.db.connect()
    try:
        # 读取时评估：v1/billing 的请求热路径按约束不改，告警落库挂在抓取/查询入口
        try:
            evaluate_alerts(conn, request.app.state.settings)
        except Exception:  # noqa: BLE001 - 评估失败不影响历史查询
            logger.exception("读取告警历史前评估告警失败")
        key = _scope_key(conn, key_id, api_key, user)
        owner = _owner(user)
        where: list[str] = []
        params: list[Any] = []
        if key is not None:
            where.append("p.key_id = ?")
            params.append(int(key["id"]))
        elif owner is not None:
            # 非 admin 不带过滤时只看自有 key 的告警；NULL 归属不在子查询里
            where.append("p.key_id IN (SELECT id FROM api_keys WHERE owner_user_id = ?)")
            params.append(owner)
        if policy_id is not None:
            where.append("a.policy_id = ?")
            params.append(policy_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        base = (
            "FROM quota_alerts a LEFT JOIN quota_policies p ON p.id = a.policy_id"
        )
        total = int(conn.execute(
            f"SELECT COUNT(*) AS n {base}{clause}", params
        ).fetchone()["n"])
        sql = (
            "SELECT a.*, p.key_id AS key_id, p.provider AS policy_provider, p.model AS model, "
            "k.name AS key_name, k.prefix AS key_prefix "
            "FROM quota_alerts a LEFT JOIN quota_policies p ON p.id = a.policy_id "
            "LEFT JOIN api_keys k ON k.id = p.key_id"
            f"{clause} ORDER BY a.triggered_at DESC, a.id DESC LIMIT ? OFFSET ?"
        )
        rows = conn.execute(sql, [*params, take, start]).fetchall()
        items = [
            {
                **_alert_public(row),
                "key_id": row["key_id"],
                "key_name": row["key_name"] or "",
                "key_prefix": _mask_prefix(row["key_prefix"] or ""),
                # 全局规则（provider 连败 / usage_missing 占比）没有策略行，对象存在 subject
                "provider": row["subject"] or row["policy_provider"] or "",
                "model": row["model"] or "",
            }
            for row in rows
        ]
    finally:
        conn.close()
    return {"total": total, "items": items}
