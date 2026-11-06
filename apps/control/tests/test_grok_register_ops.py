"""A 组控制面补齐：失败退避 0/2/4/8、凭据导出（字段/范围/脱敏边界）、任务状态流转。

不依赖真实浏览器与外部网络：执行器用内存假函数，导出直接读临时库；路由层用
TestClient 覆盖真实鉴权依赖，不新起进程。
"""

import asyncio
import csv
import io
import json
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.services.grok_register as grok_service_module
from app.config import load_settings
from app.routers import grok as grok_router
from app.security import current_user
from app.services import secret_store
from app.services.grok_register import GrokRegisterService, _backoff_seconds


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFIED_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("UNIFIED_ADMIN_PASSWORD", "test")
    monkeypatch.setenv("GROK_REGISTER_EXECUTOR_URL", "")
    monkeypatch.setenv("GROK_REGISTER_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("GROK_REGISTER_MAX_WORKERS", "2")
    return GrokRegisterService(load_settings())


def _new_task(svc: GrokRegisterService, **kwargs: Any) -> int:
    task = svc.enqueue(count=kwargs.pop("count", 1), **kwargs)
    return int(task["id"])


def _land(svc: GrokRegisterService, task_id: int, accounts: list[dict[str, Any]]) -> int:
    return svc._land_accounts(task_id, {"accounts": accounts})


def _force_running(svc: GrokRegisterService, task_id: int, attempts: int) -> None:
    conn = svc._db.connect()
    try:
        conn.execute(
            "UPDATE grok_tasks SET status='running', attempts=? WHERE id=?", (attempts, task_id)
        )
        conn.commit()
    finally:
        conn.close()


# ---- A-2 退避序列 ----

def test_backoff_sequence_values():
    assert [_backoff_seconds(n) for n in range(1, 6)] == [0, 2, 4, 8, 8]
    # attempts 为 0 只会出现在手工重置后，按第一档 0 秒处理
    assert _backoff_seconds(0) == 0


async def test_retry_schedules_0_2_4_8_seconds(svc, monkeypatch):
    now = 1_700_000_000

    class FrozenClock:
        @staticmethod
        def time() -> int:
            return now

    # 类体里再引用 time 会被上面的同名静态方法遮住，故在类定义后补挂
    FrozenClock.monotonic = staticmethod(time.monotonic)
    FrozenClock.strftime = staticmethod(time.strftime)

    monkeypatch.setattr(grok_service_module, "time", FrozenClock())

    async def fail(_task: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    svc.set_executor(fail)
    task_id = _new_task(svc, max_attempts=5)

    for attempt, expected in enumerate([0, 2, 4, 8], start=1):
        _force_running(svc, task_id, attempt)
        await svc._execute(task_id)
        row = svc.get(task_id)
        assert row["status"] == "queued", row
        assert row["next_run_at"] - now == expected, (attempt, row["next_run_at"])
        steps = [s["step"] for s in svc.detail(task_id)["steps"]]
        assert steps.count("retry") == attempt

    # 第 5 次失败已无重试预算，落 failed 并把结束时刻写实
    _force_running(svc, task_id, 5)
    await svc._execute(task_id)
    row = svc.get(task_id)
    assert row["status"] == "failed"
    assert row["finished_at"] == now
    assert "boom" in row["error"]


# ---- A-1 凭据导出 ----

def test_export_json_fields_match_accounts(svc):
    task_id = _new_task(svc, count=2, payload={"mailbox": "secret-mail"}, proxy="http://u:p@1.2.3.4:8080")
    assert _land(
        svc,
        task_id,
        [
            {
                "uid": "u-1",
                "nickname": "Alice",
                "cookie": "sso=abc; sso-rw=def",
                "realm": "intl",
                "credits": 12.5,
                "expires_at": 1999999999,
            },
            {"uid": "u-2", "nickname": "Bob", "token": "tok-xyz"},
        ],
    ) == 2

    body, media_type, filename = svc.export_credentials(task_id, "json")
    assert media_type.startswith("application/json")
    assert filename.endswith(".json")
    text = body.decode("utf-8")
    payload = json.loads(text)
    assert payload["task_id"] == task_id
    rows = payload["accounts"]

    conn = svc._db.connect()
    try:
        accounts = conn.execute(
            "SELECT file, uid, nickname, expires_at, credits FROM accounts "
            "WHERE provider='grok-register' ORDER BY file"
        ).fetchall()
        secrets = [
            secret_store.read_secret(row["secret"])[0]
            for row in conn.execute(
                "SELECT secret FROM provider_credentials WHERE provider='grok-register'"
            ).fetchall()
        ]
    finally:
        conn.close()

    # 与 accounts 表逐行一致（file/uid/nickname/expires_at/credits）
    assert [(r["file"], r["uid"], r["nickname"], r["expires_at"], r["credits"]) for r in rows] == [
        (a["file"], a["uid"], a["nickname"], a["expires_at"], a["credits"]) for a in accounts
    ]
    # 凭据原样输出，不做掩码；否则导出件没法直接登录
    assert sorted(row["credential"] for row in rows) == sorted(secrets)
    assert rows[0]["credential_type"] == "cookie"
    assert rows[1]["credential_type"] == "token"

    # 导出范围只限账号与凭据：任务行里的代理/附加参数不能跟着漏出去
    for leaked in ("payload_json", "result_json", "secret-mail", "1.2.3.4"):
        assert leaked not in text


def test_export_csv_header_bom_and_formula_guard(svc):
    task_id = _new_task(svc)
    _land(
        svc,
        task_id,
        [
            {"uid": "u-1", "nickname": "=SUM(A1:A2)", "cookie": "sso=abc"},
            {"uid": "u-2", "nickname": "Bob", "token": "tok-xyz"},
        ],
    )

    body, media_type, filename = svc.export_credentials(task_id, "csv")
    assert media_type.startswith("text/csv")
    assert filename.endswith(".csv")
    raw = body.decode("utf-8")
    assert raw.startswith("\ufeff")

    reader = csv.DictReader(io.StringIO(raw.lstrip("\ufeff")))
    assert reader.fieldnames == [
        "task_id",
        "uid",
        "nickname",
        "realm",
        "expires_at",
        "credits",
        "credential_type",
        "credential",
        "file",
        "created_at",
    ]
    records = list(reader)
    assert len(records) == 2
    assert records[0]["nickname"] == "'=SUM(A1:A2)"
    assert records[0]["credential"] == "sso=abc"
    assert records[0]["credential_type"] == "cookie"
    assert records[1]["credential"] == "tok-xyz"


def test_export_only_covers_own_task_and_provider(svc):
    first = _new_task(svc)
    second = _new_task(svc)
    _land(svc, first, [{"uid": "a-1", "cookie": "cookie-a"}])
    _land(svc, second, [{"uid": "b-1", "cookie": "cookie-b"}])

    conn = svc._db.connect()
    try:
        conn.execute(
            "INSERT INTO provider_credentials(provider, label, base_url, secret, enabled, "
            "sort_order, note, created_at, updated_at) VALUES('codebuddy','other','',?,1,0,'',0,0)",
            ("other-provider-secret",),
        )
        conn.commit()
    finally:
        conn.close()

    text = svc.export_credentials(first, "json")[0].decode("utf-8")
    assert "a-1" in text and "cookie-a" in text
    assert "b-1" not in text and "cookie-b" not in text
    assert "other-provider-secret" not in text


def test_export_rejects_bad_format_and_unknown_task(svc):
    task_id = _new_task(svc)
    with pytest.raises(ValueError):
        svc.export_credentials(task_id, "xml")
    with pytest.raises(KeyError):
        svc.export_credentials(task_id + 1000, "json")


# ---- 任务状态流转 ----

async def test_state_transitions_queued_running_succeeded(svc):
    task_id = _new_task(svc)

    async def ok(_task: dict[str, Any]) -> dict[str, Any]:
        return {"status": "done", "accounts": [{"uid": "u-1", "cookie": "c-1"}]}

    svc.set_executor(ok)
    assert svc.get(task_id)["status"] == "queued"
    assert await svc.run_once() == 1

    for _ in range(200):
        if svc.get(task_id)["status"] == "succeeded" and task_id not in svc._running:
            break
        await asyncio.sleep(0.01)
    row = svc.get(task_id)
    assert row["status"] == "succeeded", row
    assert row["attempts"] == 1
    assert json.loads(row["result_json"])["imported_accounts"] == 1
    assert [s["step"] for s in svc.detail(task_id)["steps"]] == [
        "queued",
        "running",
        "accounts",
        "succeeded",
    ]


def test_cancel_then_retry_transitions(svc):
    task_id = _new_task(svc)
    assert svc.cancel(task_id)["status"] == "cancelled"
    # 终态不能重复取消
    with pytest.raises(ValueError):
        svc.cancel(task_id)

    again = svc.retry(task_id)
    assert again["status"] == "queued"
    assert again["attempts"] == 0
    # 排队中的任务不能重试
    with pytest.raises(ValueError):
        svc.retry(task_id)


# ---- 路由层 ----

def _client(svc: GrokRegisterService, role: str) -> TestClient:
    app = FastAPI()
    app.include_router(grok_router.router)
    app.state.grok_service = svc
    app.dependency_overrides[current_user] = lambda: {"username": role, "role": role}
    return TestClient(app)


def test_export_endpoint_json_and_csv(svc):
    task_id = _new_task(svc)
    _land(svc, task_id, [{"uid": "u-1", "cookie": "sso=abc"}])

    with _client(svc, "admin") as client:
        r = client.get(f"/api/grok/register/tasks/{task_id}/credentials", params={"fmt": "json"})
        assert r.status_code == 200
        assert f'filename="grok-credentials-task-{task_id}-' in r.headers["content-disposition"]
        assert r.headers["content-disposition"].endswith('.json"')
        assert r.headers["cache-control"] == "no-store"
        assert r.json()["accounts"][0]["uid"] == "u-1"

        r = client.get(f"/api/grok/register/tasks/{task_id}/credentials", params={"fmt": "csv"})
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert r.content.decode("utf-8").startswith("\ufeff")


def test_export_endpoint_status_codes(svc):
    task_id = _new_task(svc)
    with _client(svc, "admin") as client:
        assert (
            client.get(f"/api/grok/register/tasks/{task_id}/credentials", params={"fmt": "xml"}).status_code
            == 400
        )
        assert client.get("/api/grok/register/tasks/999999/credentials").status_code == 404


def test_export_endpoint_requires_admin(svc):
    task_id = _new_task(svc)
    with _client(svc, "viewer") as client:
        r = client.get(f"/api/grok/register/tasks/{task_id}/credentials")
        assert r.status_code == 403
