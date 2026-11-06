"""价格表 API：读全开、写只追加。

改价 = 插新行（新 effective_from），没有 PATCH / DELETE：历史行是已发生流水的定价依据，
一旦原地改价，billing_ledger 的 price_snapshot 就对不上账。
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..api_utils import now
from ..billing import (
    RULE_KINDS,
    TOKEN_CLASSES,
    import_price_books,
    import_price_rules,
    resolve_price_rules,
    resolve_prices,
    to_usd,
)
from ..config import REPO_ROOT
from ..security import admin_user, current_user

router = APIRouter(prefix="/api/pricing", tags=["pricing"])

PRICES_YAML = REPO_ROOT / "config" / "prices.yaml"


def _public(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "provider": row["provider"],
        "model": row["model"],
        "token_class": row["token_class"],
        "price_micro_per_1m": int(row["price_micro_per_1m"]),
        "price_usd_per_1m": to_usd(row["price_micro_per_1m"]),
        "currency": row["currency"],
        "effective_from": int(row["effective_from"]),
        "created_by": row["created_by"],
        "note": row["note"],
    }


@router.get("/books")
def list_books(
    request: Request,
    provider: str | None = None,
    model: str | None = None,
    token_class: str | None = None,
    limit: int = 200,
    user: dict = Depends(current_user),
):
    if token_class is not None and token_class not in TOKEN_CLASSES:
        raise HTTPException(400, f"token_class 必须是 {'/'.join(TOKEN_CLASSES)}")
    limit = max(1, min(limit, 1000))
    where: list[str] = []
    params: list[Any] = []
    for field, value in (("provider", provider), ("model", model), ("token_class", token_class)):
        if value:
            where.append(f"{field} = ?")
            params.append(value)
    sql = "SELECT * FROM price_books"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY provider, model, token_class, effective_from DESC LIMIT ?"
    params.append(limit)
    conn = request.app.state.db.connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_public(row) for row in rows]


class PriceLineIn(BaseModel):
    model: str
    token_class: str
    price_micro_per_1m: int
    provider: str = "*"
    effective_from: int | None = None
    note: str = ""


@router.post("/books")
def add_book(
    body: PriceLineIn,
    request: Request,
    user: dict = Depends(admin_user),
):
    model = body.model.strip()
    provider = (body.provider or "*").strip() or "*"
    if not model:
        raise HTTPException(400, "model 不能为空")
    if body.token_class not in TOKEN_CLASSES:
        raise HTTPException(400, f"token_class 必须是 {'/'.join(TOKEN_CLASSES)}")
    if body.price_micro_per_1m < 0:
        raise HTTPException(400, "price_micro_per_1m 不能为负")
    effective_from = int(body.effective_from if body.effective_from is not None else now())

    conn = request.app.state.db.connect()
    try:
        exists = conn.execute(
            "SELECT id FROM price_books WHERE provider = ? AND model = ? AND token_class = ? "
            "AND effective_from = ?",
            (provider, model, body.token_class, effective_from),
        ).fetchone()
        if exists is not None:
            raise HTTPException(
                409,
                "同一 (provider, model, token_class, effective_from) 已存在；改价请用新的 effective_from",
            )
        cur = conn.execute(
            "INSERT INTO price_books(provider, model, token_class, price_micro_per_1m, currency, "
            "effective_from, created_by, note) VALUES(?,?,?,?,?,?,?,?)",
            (
                provider,
                model,
                body.token_class,
                body.price_micro_per_1m,
                "USD",
                effective_from,
                user.get("username") or "",
                body.note,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM price_books WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return _public(row)


@router.get("/resolve")
def resolve_preview(
    request: Request,
    provider: str,
    model: str,
    at: int | None = None,
    user: dict = Depends(current_user),
):
    """干跑解析：看某模型在 at 时刻会命中哪些价格行与计费规则（精确 vs 通配）。"""
    moment = int(at if at is not None else now())
    conn = request.app.state.db.connect()
    try:
        lines = resolve_prices(conn, provider=provider, model=model, at=moment)
        rules = resolve_price_rules(conn, provider=provider, model=model, at=moment)
    finally:
        conn.close()
    return {
        "provider": provider,
        "model": model,
        "at": moment,
        "lines": {
            name: {
                "price_id": line["price_id"],
                "provider": line["provider"],
                "model": line["model"],
                "price_micro_per_1m": line["price_micro_per_1m"],
                "price_usd_per_1m": to_usd(line["price_micro_per_1m"]),
            }
            for name, line in lines.items()
        },
        "missing": [name for name in TOKEN_CLASSES if name not in lines],
        "rules": {kind: _rule_public(rule) for kind, rule in rules.items()},
        "missing_rules": [kind for kind in RULE_KINDS if kind not in rules],
    }


def _rule_public(rule: dict[str, Any]) -> dict[str, Any]:
    unit_micro = int(rule["unit_micro"])
    return {
        "rule_id": rule.get("rule_id"),
        "provider": rule["provider"],
        "model": rule["model"],
        "kind": rule["kind"],
        "unit_micro": unit_micro,
        "unit_usd": to_usd(unit_micro),
        "threshold": int(rule["threshold"]),
        "multiplier_milli": int(rule["multiplier_milli"]),
        "multiplier": int(rule["multiplier_milli"]) / 1000,
    }


class RuleIn(BaseModel):
    kind: str
    provider: str = "*"
    model: str = "*"
    unit_micro: int = 0
    threshold: int = 0
    multiplier_milli: int = 1000
    effective_from: int | None = None
    note: str = ""


@router.get("/rules")
def list_rules(
    request: Request,
    provider: str | None = None,
    model: str | None = None,
    kind: str | None = None,
    limit: int = 200,
    user: dict = Depends(current_user),
):
    if kind is not None and kind not in RULE_KINDS:
        raise HTTPException(400, f"kind 必须是 {'/'.join(RULE_KINDS)}")
    limit = max(1, min(limit, 1000))
    where: list[str] = []
    params: list[Any] = []
    for field, value in (("provider", provider), ("model", model), ("kind", kind)):
        if value:
            where.append(f"{field} = ?")
            params.append(value)
    sql = "SELECT * FROM price_rules"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY kind, provider, model, effective_from DESC LIMIT ?"
    params.append(limit)
    conn = request.app.state.db.connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [
        {
            **_rule_public(
                {
                    "rule_id": row["id"],
                    "provider": row["provider"],
                    "model": row["model"],
                    "kind": row["kind"],
                    "unit_micro": row["unit_micro"],
                    "threshold": row["threshold"],
                    "multiplier_milli": row["multiplier_milli"],
                }
            ),
            "effective_from": int(row["effective_from"]),
            "created_by": row["created_by"],
            "note": row["note"],
        }
        for row in rows
    ]


@router.post("/rules")
def add_rule(
    body: RuleIn,
    request: Request,
    user: dict = Depends(admin_user),
):
    if body.kind not in RULE_KINDS:
        raise HTTPException(400, f"kind 必须是 {'/'.join(RULE_KINDS)}")
    provider = (body.provider or "*").strip() or "*"
    model = (body.model or "*").strip() or "*"
    if body.unit_micro < 0 or body.threshold < 0 or body.multiplier_milli < 0:
        raise HTTPException(400, "unit_micro / threshold / multiplier_milli 不能为负")
    if body.kind in ("per_call", "per_second") and body.unit_micro <= 0:
        raise HTTPException(400, f"{body.kind} 的 unit_micro 必须为正")
    if body.kind == "long_context" and (body.threshold <= 0 or body.multiplier_milli <= 0):
        raise HTTPException(400, "long_context 需要正的 threshold 与 multiplier_milli")
    effective_from = int(body.effective_from if body.effective_from is not None else now())

    conn = request.app.state.db.connect()
    try:
        exists = conn.execute(
            "SELECT id FROM price_rules WHERE provider = ? AND model = ? AND kind = ? "
            "AND effective_from = ?",
            (provider, model, body.kind, effective_from),
        ).fetchone()
        if exists is not None:
            raise HTTPException(
                409,
                "同一 (provider, model, kind, effective_from) 已存在；改规则请用新的 effective_from",
            )
        cur = conn.execute(
            "INSERT INTO price_rules(provider, model, kind, unit_micro, threshold, multiplier_milli, "
            "effective_from, created_by, note) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                provider,
                model,
                body.kind,
                body.unit_micro,
                body.threshold,
                body.multiplier_milli,
                effective_from,
                user.get("username") or "",
                body.note,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM price_rules WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return {
        **_rule_public(
            {
                "rule_id": row["id"],
                "provider": row["provider"],
                "model": row["model"],
                "kind": row["kind"],
                "unit_micro": row["unit_micro"],
                "threshold": row["threshold"],
                "multiplier_milli": row["multiplier_milli"],
            }
        ),
        "effective_from": int(row["effective_from"]),
        "created_by": row["created_by"],
        "note": row["note"],
    }


@router.post("/import")
def import_yaml(
    request: Request,
    dry_run: bool = False,
    user: dict = Depends(admin_user),
):
    path = Path(PRICES_YAML)
    if not path.is_file():
        raise HTTPException(404, f"找不到价格种子文件：{path}")
    conn = request.app.state.db.connect()
    try:
        try:
            result = import_price_books(conn, path, dry_run=dry_run)
            result["rules"] = import_price_rules(conn, path, dry_run=dry_run)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    result["path"] = str(path)
    return result
