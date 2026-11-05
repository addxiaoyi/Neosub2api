"""提示词库：可复用提示词模板的读写。

上游 gptGrok2api 的提示词是打包在 services/default_prompt_library.json 里的只读清单
（GET /api/prompts，只有来源开关，没有条目级写接口）。本仓没有那份内容资产，
也没有任何提示词存储，所以这里只落「本仓自管」的最小形态：
管理员维护模板，登录用户只读、复制。

表由 db.py 的 SCHEMA 统一建（与其它表同一份契约），这里只假设表已存在。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..api_utils import now
from ..security import admin_user, audit_event, current_user

router = APIRouter(prefix="/api/prompts", tags=["prompts"])

MAX_PAGE_SIZE = 100
MODES = ("", "generate", "edit")


def _row(row, with_body: bool = False) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "category": row["category"],
        "sub_category": row["sub_category"],
        "mode": row["mode"],
        "image_size": row["image_size"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if with_body:
        item["prompt"] = row["prompt"]
    else:
        body = row["prompt"]
        item["excerpt"] = body if len(body) <= 120 else body[:120] + "…"
    return item


def _clean(value: Any, field: str, limit: int, required: bool) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise HTTPException(400, f"{field}不能为空")
    if len(text) > limit:
        raise HTTPException(400, f"{field}长度不能超过 {limit} 个字符")
    return text


def _apply(payload: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    source = {**current, **{k: v for k, v in payload.items() if v is not None}}
    merged = {
        "title": _clean(source.get("title"), "标题", 120, True),
        "description": _clean(source.get("description"), "简介", 500, False),
        "prompt": _clean(source.get("prompt"), "提示词", 8000, True),
        "category": _clean(source.get("category"), "分类", 40, False),
        "sub_category": _clean(source.get("sub_category"), "子分类", 40, False),
        "mode": _clean(source.get("mode"), "模式", 16, False),
        "image_size": _clean(source.get("image_size"), "图片比例", 24, False),
    }
    if merged["mode"] not in MODES:
        raise HTTPException(400, "模式只能是 generate 或 edit")
    return merged


class PromptCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=8000)
    description: str = ""
    category: str = ""
    sub_category: str = ""
    mode: str = ""
    image_size: str = ""


class PromptPatch(BaseModel):
    title: str | None = None
    prompt: str | None = None
    description: str | None = None
    category: str | None = None
    sub_category: str | None = None
    mode: str | None = None
    image_size: str | None = None


@router.get("")
def list_prompts(
    request: Request,
    page: int = 1,
    size: int = 20,
    q: str | None = None,
    category: str | None = None,
    user: dict = Depends(current_user),
):
    page = max(1, int(page))
    size = max(1, min(MAX_PAGE_SIZE, int(size)))
    clauses: list[str] = []
    params: list[Any] = []
    keyword = (q or "").strip()
    if keyword:
        like = f"%{keyword}%"
        clauses.append("(title LIKE ? OR description LIKE ? OR prompt LIKE ?)")
        params.extend([like, like, like])
    group = (category or "").strip()
    if group:
        clauses.append("category = ?")
        params.append(group)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    conn = request.app.state.db.connect()
    try:
        total = int(conn.execute(f"SELECT COUNT(*) AS n FROM prompt_templates{where}", params).fetchone()["n"])
        rows = conn.execute(
            f"SELECT * FROM prompt_templates{where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, size, (page - 1) * size],
        ).fetchall()
    finally:
        conn.close()
    return {
        "total": total,
        "page": page,
        "size": size,
        "items": [_row(row) for row in rows],
    }


@router.get("/categories")
def list_categories(request: Request, user: dict = Depends(current_user)):
    conn = request.app.state.db.connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT category FROM prompt_templates WHERE category <> '' ORDER BY category"
        ).fetchall()
    finally:
        conn.close()
    return {"items": [row["category"] for row in rows]}


@router.get("/{prompt_id}")
def get_prompt(prompt_id: int, request: Request, user: dict = Depends(current_user)):
    conn = request.app.state.db.connect()
    try:
        row = conn.execute("SELECT * FROM prompt_templates WHERE id = ?", (prompt_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "提示词不存在")
    return _row(row, with_body=True)


def _audit(request: Request, conn, user: dict, action: str, prompt_id: int, detail: str) -> None:
    ip = request.client.host if request.client else ""
    audit_event(conn, user["username"], action, f"prompt:{prompt_id}", detail, ip)


@router.post("")
def create_prompt(body: PromptCreate, request: Request, user: dict = Depends(admin_user)):
    values = _apply(body.model_dump(), {})
    stamp = now()
    conn = request.app.state.db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO prompt_templates(title, description, prompt, category, sub_category, "
            "mode, image_size, created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                values["title"], values["description"], values["prompt"], values["category"],
                values["sub_category"], values["mode"], values["image_size"],
                user["username"], stamp, stamp,
            ),
        )
        prompt_id = int(cur.lastrowid)
        _audit(request, conn, user, "prompt.create", prompt_id, values["title"])
        conn.commit()
        row = conn.execute("SELECT * FROM prompt_templates WHERE id = ?", (prompt_id,)).fetchone()
    finally:
        conn.close()
    return _row(row, with_body=True)


@router.patch("/{prompt_id}")
def update_prompt(prompt_id: int, body: PromptPatch, request: Request, user: dict = Depends(admin_user)):
    patch = body.model_dump(exclude_unset=True)
    conn = request.app.state.db.connect()
    try:
        row = conn.execute("SELECT * FROM prompt_templates WHERE id = ?", (prompt_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "提示词不存在")
        values = _apply(patch, dict(row))
        conn.execute(
            "UPDATE prompt_templates SET title=?, description=?, prompt=?, category=?, "
            "sub_category=?, mode=?, image_size=?, updated_at=? WHERE id=?",
            (
                values["title"], values["description"], values["prompt"], values["category"],
                values["sub_category"], values["mode"], values["image_size"], now(), prompt_id,
            ),
        )
        _audit(request, conn, user, "prompt.update", prompt_id, values["title"])
        conn.commit()
        row = conn.execute("SELECT * FROM prompt_templates WHERE id = ?", (prompt_id,)).fetchone()
    finally:
        conn.close()
    return _row(row, with_body=True)


@router.delete("/{prompt_id}")
def delete_prompt(prompt_id: int, request: Request, user: dict = Depends(admin_user)):
    conn = request.app.state.db.connect()
    try:
        row = conn.execute("SELECT title FROM prompt_templates WHERE id = ?", (prompt_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "提示词不存在")
        conn.execute("DELETE FROM prompt_templates WHERE id = ?", (prompt_id,))
        _audit(request, conn, user, "prompt.delete", prompt_id, row["title"])
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}
