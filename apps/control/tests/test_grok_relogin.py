"""2-C4：执行器 job_type 透传、relogin 前置校验与详情回显（stub 执行器，动态端口）。"""

import asyncio
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import load_settings
from app.routers import grok as grok_router
from app.security import current_user
from app.services.grok_register import GrokRegisterService


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, _Handler)
        self.submits: list[dict] = []
        self.lock = Lock()


class _Handler(BaseHTTPRequestHandler):
    server: _StubServer

    def log_message(self, fmt, *args):
        return

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_POST(self):  # noqa: N802
        if self.path == "/jobs":
            with self.server.lock:
                self.server.submits.append(self._body())
            self._send(201, {"job_id": "job-1", "status": "pending"})
            return
        self._send(404, {"error": "not found"})

    def do_GET(self):  # noqa: N802
        if self.path == "/jobs/job-1":
            self._send(200, {"job_id": "job-1", "status": "done", "accounts": []})
            return
        self._send(404, {"error": "not found"})


@pytest.fixture
def stub():
    server = _StubServer(("127.0.0.1", 0))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def svc(tmp_path, monkeypatch, stub):
    monkeypatch.setenv("UNIFIED_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("UNIFIED_ADMIN_PASSWORD", "test")
    monkeypatch.setenv("GROK_REGISTER_EXECUTOR_URL", f"http://127.0.0.1:{stub.server_address[1]}")
    monkeypatch.setenv("GROK_REGISTER_EXECUTOR_TIMEOUT_SEC", "5")
    monkeypatch.setenv("GROK_REGISTER_POLL_INTERVAL_SEC", "0.05")
    monkeypatch.setenv("GROK_REGISTER_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("GROK_REGISTER_MAX_WORKERS", "2")
    return GrokRegisterService(load_settings())


def _client(svc: GrokRegisterService) -> TestClient:
    app = FastAPI()
    app.include_router(grok_router.router)
    app.state.grok_service = svc
    app.dependency_overrides[current_user] = lambda: {"username": "admin", "role": "admin"}
    return TestClient(app)


async def _wait_terminal(svc: GrokRegisterService, task_id: int, seconds: float = 10.0) -> dict:
    deadline = time.time() + seconds
    await svc.run_once()
    while time.time() < deadline:
        row = svc.get(task_id)
        if row["status"] in {"succeeded", "failed", "cancelled"} and task_id not in svc._running:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务未在 {seconds}s 内进入终态：{svc.get(task_id)}")


async def test_relogin_submit_body_includes_job_type_and_proxy(svc, stub):
    task = svc.enqueue(
        job_type="relogin",
        proxy="http://10.0.0.1:8888",
        max_attempts=1,
        payload={"sso": "sso-abc"},
    )

    row = await _wait_terminal(svc, int(task["id"]))

    assert row["status"] == "succeeded", row
    assert row["job_type"] == "relogin"
    body = stub.submits[-1]
    assert body["job_type"] == "relogin"
    assert body["proxy"] == "http://10.0.0.1:8888"
    assert body["payload"] == {"sso": "sso-abc"}


async def test_default_job_type_is_register(svc, stub):
    task = svc.enqueue(max_attempts=1)

    row = await _wait_terminal(svc, int(task["id"]))

    assert row["job_type"] == "register"
    assert stub.submits[-1]["job_type"] == "register"


def test_unknown_job_type_rejected_by_router(svc):
    with _client(svc) as client:
        r = client.post("/api/grok/register/tasks", json={"job_type": "sso"})
    assert r.status_code == 400
    assert "任务类型" in r.json()["detail"]


def test_relogin_requires_existing_session(svc):
    with _client(svc) as client:
        assert (
            client.post("/api/grok/register/tasks", json={"job_type": "relogin"}).status_code == 400
        )
        blank = client.post(
            "/api/grok/register/tasks", json={"job_type": "relogin", "payload": {"sso": "   "}}
        )
        assert blank.status_code == 400
        assert "sso" in blank.json()["detail"] or "cookies" in blank.json()["detail"]


def test_relogin_with_session_queues_and_detail_exposes_job_type(svc):
    with _client(svc) as client:
        r = client.post(
            "/api/grok/register/tasks",
            json={
                "job_type": "relogin",
                "payload": {"cookies": [{"name": "sso", "value": "v"}]},
                "proxy": "http://10.0.0.2:9000",
            },
        )
        assert r.status_code == 200
        created = r.json()
        assert created["job_type"] == "relogin"
        assert created["proxy"] == "http://10.0.0.2:9000"

        detail = client.get(f"/api/grok/register/tasks/{created['id']}")
        assert detail.status_code == 200
        assert detail.json()["job_type"] == "relogin"


def test_service_rejects_unknown_job_type(svc):
    with pytest.raises(ValueError) as exc:
        svc.enqueue(job_type="nope")
    assert "任务类型" in str(exc.value)
