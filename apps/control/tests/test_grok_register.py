"""Grok 注册任务：执行器协议 v1 的链路验证（stub HTTP 服务，动态端口）。

覆盖：pending→running→done 落库、不可达、执行器报错、超时、协议返回非 JSON、
重启后 running 行回收。不依赖真实浏览器与外部网络。
"""

import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import grok_executor_stub  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.services import secret_store  # noqa: E402
from app.services.grok_register import GrokRegisterService  # noqa: E402


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


@pytest.fixture
def stub():
    server = grok_executor_stub.create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def make_service(tmp_path, monkeypatch):
    def _make(url: str, *, timeout: float = 5, interval: float = 0.05) -> GrokRegisterService:
        monkeypatch.setenv("UNIFIED_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("UNIFIED_ADMIN_PASSWORD", "test")
        monkeypatch.setenv("GROK_REGISTER_EXECUTOR_URL", url)
        monkeypatch.setenv("GROK_REGISTER_EXECUTOR_TIMEOUT_SEC", str(timeout))
        monkeypatch.setenv("GROK_REGISTER_POLL_INTERVAL_SEC", str(interval))
        monkeypatch.setenv("GROK_REGISTER_MAX_CONCURRENCY", "2")
        monkeypatch.setenv("GROK_REGISTER_MAX_WORKERS", "2")
        return GrokRegisterService(load_settings())

    return _make


async def _wait_terminal(svc: GrokRegisterService, task_id: int, seconds: float = 10.0) -> dict:
    deadline = time.time() + seconds
    await svc.run_once()
    while time.time() < deadline:
        row = svc.get(task_id)
        if row["status"] in {"succeeded", "failed", "cancelled"} and task_id not in svc._running:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务未在 {seconds}s 内进入终态：{svc.get(task_id)}")


async def test_pending_to_done_lands_accounts(stub, make_service):
    svc = make_service(stub)
    task = svc.enqueue(count=2, workers=1, max_attempts=1, payload={"stub": {"polls": 1}})

    row = await _wait_terminal(svc, int(task["id"]))

    assert row["status"] == "succeeded", row
    assert row["attempts"] == 1
    result = json.loads(row["result_json"])
    assert result["status"] == "done"
    assert result["imported_accounts"] == 2

    conn = svc._db.connect()
    try:
        accounts = conn.execute(
            "SELECT file, uid, nickname, meta_json FROM accounts WHERE provider='grok-register'"
        ).fetchall()
        creds = conn.execute(
            "SELECT secret FROM provider_credentials WHERE provider='grok-register'"
        ).fetchall()
    finally:
        conn.close()
    assert len(accounts) == 2
    assert all(row["uid"].startswith("stub-") for row in accounts)
    assert len(creds) == 2
    # 落库可能是密文，取明文再断言
    assert all(secret_store.read_secret(row["secret"])[0].startswith("stub-cookie-") for row in creds)

    steps = [s["step"] for s in svc.detail(int(task["id"]))["steps"]]
    assert steps[0] == "queued"
    assert "running" in steps
    assert "accounts" in steps
    assert steps[-1] == "succeeded"


async def test_unreachable_executor_fails_with_reason(make_service):
    svc = make_service(f"http://127.0.0.1:{_free_port()}")
    task = svc.enqueue(max_attempts=1)

    row = await _wait_terminal(svc, int(task["id"]))

    assert row["status"] == "failed"
    assert "不可达" in row["error"], row["error"]


async def test_executor_reported_failure_and_retry(stub, make_service):
    svc = make_service(stub)
    task = svc.enqueue(count=1, max_attempts=1, payload={"stub": {"mode": "fail", "polls": 2}})

    row = await _wait_terminal(svc, int(task["id"]))
    assert row["status"] == "failed"
    assert "403" in row["error"], row["error"]

    retried = svc.retry(int(task["id"]))
    assert retried["status"] == "queued"
    assert retried["attempts"] == 0

    again = await _wait_terminal(svc, int(task["id"]))
    assert again["status"] == "failed"
    assert again["attempts"] == 1


async def test_timeout_is_distinct_from_unreachable(stub, make_service):
    svc = make_service(stub, timeout=0.5, interval=0.05)
    task = svc.enqueue(max_attempts=1, payload={"stub": {"mode": "hang"}})

    row = await _wait_terminal(svc, int(task["id"]))

    assert row["status"] == "failed"
    assert "超时" in row["error"], row["error"]


@pytest.mark.parametrize("mode", ["badjson", "http500"])
async def test_bad_executor_response_fails_with_reason(stub, make_service, mode):
    svc = make_service(stub)
    task = svc.enqueue(max_attempts=1, payload={"stub": {"mode": mode}})

    row = await _wait_terminal(svc, int(task["id"]))

    assert row["status"] == "failed"
    assert row["error"], "失败原因不能为空"


async def test_orphan_running_recovered_on_restart(make_service):
    svc = make_service("")
    task = svc.enqueue(max_attempts=2)
    conn = svc._db.connect()
    try:
        conn.execute("UPDATE grok_tasks SET status='running', attempts=1 WHERE id=?", (task["id"],))
        conn.commit()
    finally:
        conn.close()

    assert svc.recover_orphans() == 1
    row = svc.get(int(task["id"]))
    assert row["status"] == "queued"
    assert "重启" in row["error"]
    steps = [s["step"] for s in svc.detail(int(task["id"]))["steps"]]
    assert "orphaned" in steps


async def test_cancel_queued(make_service):
    svc = make_service("")
    task = svc.enqueue()
    row = svc.cancel(int(task["id"]))
    assert row["status"] == "cancelled"
    assert "cancelled" in [s["step"] for s in svc.detail(int(task["id"]))["steps"]]
