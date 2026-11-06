"""首次启动向导的状态持久化。

只存一个 JSON 于 settings 表，不参与路由/鉴权/计费。
完成后写 completed=True；重建库（settings 清空）自然回到未完成态。
"""

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..api_utils import now
from ..db import get_setting, set_setting
from ..security import admin_user, audit_event, current_user

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])

STATE_KEY = "onboarding_state"
STEP_IDS = ("account", "provider", "credential", "selfcheck", "smoke", "key")

DEFAULT: dict[str, Any] = {"completed": False, "current_step": 0, "skipped_steps": [], "updated_at": None}


def _load(conn) -> dict[str, Any]:
    raw = get_setting(conn, STATE_KEY)
    if not raw:
        return dict(DEFAULT)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return dict(DEFAULT)
    if not isinstance(data, dict):
        return dict(DEFAULT)
    steps = [s for s in data.get("skipped_steps") or [] if s in STEP_IDS]
    step = data.get("current_step")
    step = step if isinstance(step, int) and 0 <= step < len(STEP_IDS) else 0
    return {
        "completed": bool(data.get("completed")),
        "current_step": step,
        "skipped_steps": steps,
        "updated_at": data.get("updated_at"),
    }


class OnboardingPatch(BaseModel):
    completed: bool | None = None
    current_step: int | None = None
    skipped_steps: list[str] | None = None


@router.get("")
def read_onboarding(request: Request, user: dict = Depends(current_user)):
    conn = request.app.state.db.connect()
    try:
        return _load(conn)
    finally:
        conn.close()


@router.post("")
def save_onboarding(
    body: OnboardingPatch,
    request: Request,
    user: dict = Depends(admin_user),
):
    conn = request.app.state.db.connect()
    try:
        state = _load(conn)
        if body.completed is not None:
            state["completed"] = body.completed
            # 完成即清空跳过项：待办条随 completed 一起消失
            if body.completed:
                state["skipped_steps"] = []
        if body.current_step is not None:
            state["current_step"] = max(0, min(len(STEP_IDS) - 1, body.current_step))
        if body.skipped_steps is not None:
            # 去重保序，非法 step 直接丢弃（前端键名漂移时不落脏数据）
            state["skipped_steps"] = list(dict.fromkeys(s for s in body.skipped_steps if s in STEP_IDS))
        state["updated_at"] = now()
        set_setting(conn, STATE_KEY, json.dumps(state, ensure_ascii=False))
        audit_event(
            conn,
            user["username"],
            "onboarding.save",
            "completed" if state["completed"] else f"step={state['current_step']}",
            f"skipped={len(state['skipped_steps'])}",
            request.client.host if request.client else "",
        )
        return state
    finally:
        conn.close()


@router.post("/reset")
def reset_onboarding(request: Request, user: dict = Depends(admin_user)):
    """显式重跑：删掉标志，下次进首页重新进入向导。"""
    conn = request.app.state.db.connect()
    try:
        conn.execute("DELETE FROM settings WHERE key = ?", (STATE_KEY,))
        conn.commit()
        audit_event(
            conn,
            user["username"],
            "onboarding.reset",
            STATE_KEY,
            "",
            request.client.host if request.client else "",
        )
    finally:
        conn.close()
    return dict(DEFAULT)
