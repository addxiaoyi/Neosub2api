"""配置与账号的导出/导入端点（admin）。

导出以 JSON 附件下载；导入分两步：先 plan（dry-run 报告），
再由用户确认执行 merge/overwrite。预检有 fail 时整批拒绝，库零写入。
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ..api_utils import db_of
from ..config_bundle import BundleWriteFailed, apply_import, build_bundle, plan_import
from ..security import admin_user, audit_event

router = APIRouter(prefix="/api/config", tags=["config-transfer"])


def _registry(request: Request) -> Any:
    return request.app.state.registry


def _known_ids(request: Request) -> set[str]:
    ids = {adapter.spec.id for adapter in _registry(request)._adapters.values()}
    conn = db_of(request)
    try:
        ids |= {str(row["id"]) for row in conn.execute("SELECT id FROM providers").fetchall()}
    finally:
        conn.close()
    return ids


@router.get("/export")
def export_config(request: Request, include_secrets: bool = False, user=Depends(admin_user)):
    conn = db_of(request)
    try:
        bundle = build_bundle(conn, _registry(request), include_secrets=include_secrets)
    finally:
        conn.close()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    body = json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="unified-config-{stamp}.json"',
            "X-Contains-Secrets": "true" if include_secrets else "false",
        },
    )


class ImportIn(BaseModel):
    bundle: dict[str, Any]
    mode: str = Field(default="dry-run", pattern="^(merge|overwrite|dry-run)$")
    confirm: bool = False


@router.post("/import/plan")
def plan_config_import(body: ImportIn, request: Request, user=Depends(admin_user)):
    if body.mode not in ("merge", "overwrite"):
        raise HTTPException(status_code=400, detail="mode 只能是 merge 或 overwrite")
    conn = db_of(request)
    try:
        report = plan_import(
            body.bundle,
            conn=conn,
            mode=body.mode,
            known_ids=_known_ids(request),
            clean_base=_registry(request)._clean_base,
        )
    finally:
        conn.close()
    report["written"] = 0
    report["ok"] = not any(issue["level"] == "fail" for issue in report["issues"])
    return report


@router.post("/import")
def run_config_import(body: ImportIn, request: Request, user=Depends(admin_user)):
    if body.mode == "dry-run":
        raise HTTPException(status_code=400, detail="dry-run 请改用 /api/config/import/plan")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="导入会写库，必须显式确认（confirm=true）")

    conn = db_of(request)
    started = time.perf_counter()
    try:
        report = apply_import(
            body.bundle,
            conn=conn,
            mode=body.mode,
            known_ids=_known_ids(request),
            clean_base=_registry(request)._clean_base,
            registry=_registry(request),
        )
    except BundleWriteFailed as exc:
        raise HTTPException(
            status_code=500, detail=f"写入失败，事务已回滚：{exc.path} — {exc.error}"
        ) from exc
    except Exception as exc:
        report = getattr(exc, "report", None)
        if report is not None:
            return JSONResponse(
                status_code=400,
                content={"detail": "预检未通过，整批拒绝，库未改动", "report": report},
            )
        raise
    finally:
        conn.close()

    duration_ms = int((time.perf_counter() - started) * 1000)
    counts = report["totals"]
    audit_conn = db_of(request)
    try:
        audit_event(
            audit_conn,
            user["username"],
            "config.import",
            f"mode={body.mode}",
            f"create={counts['create']} overwrite={counts['overwrite']} "
            f"skip={counts['skip']} fail={counts['fail']} duration={duration_ms}ms",
            request.client.host if request.client else "",
        )
    finally:
        audit_conn.close()
    return report
