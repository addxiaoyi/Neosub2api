"""价格表 API：读全开、写只追加。

改价 = 插新行（新 effective_from），没有 PATCH / DELETE：历史行是已发生流水的定价依据，
一旦原地改价，billing_ledger 的 price_snapshot 就对不上账。
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..api_utils import now
from ..billing import TOKEN_CLASSES, import_price_books, resolve_prices, to_usd
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
    """干跑解析：看某模型在 at 时刻会命中哪些价格行（精确 vs 通配）。"""
    moment = int(at if at is not None else now())
    conn = request.app.state.db.connect()
    try:
        lines = resolve_prices(conn, provider=provider, model=model, at=moment)
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
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    result["path"] = str(path)
    return result
