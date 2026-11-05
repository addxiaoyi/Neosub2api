"""跨 provider 故障转移「可持久化配置」的测试。

独立控制面（8180）+ 假上游（8181），控制面刻意不带任何 `UNIFIED_FAILOVER_*` 环境变量，
覆盖：默认 OFF、管理员保存后立即生效（无需重启）、尝试次数上限实时生效、
viewer 403 / admin 200、写操作审计落库；另附 env 覆盖配置与钳制的单元级断言。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from app.model_resolve import (
    DEFAULT_FAILOVER_ATTEMPTS,
    MAX_FAILOVER_ATTEMPTS,
    failover_enabled,
    failover_env_locked,
    failover_max_attempts,
    failover_sources,
    load_stored_failover,
)
from tests.conftest import CONTROL, PY, STARTUP_TIMEOUT, _port_free, _startup_down, _wait_up

pytestmark = pytest.mark.gateway

CTRL_PORT = 8180
FAKE_PORT = 8181
MODEL = "shared-model"
FAIL_KEY = "fail-key"
OK_KEY = "ok-key"


def _completion(model: str, label: str) -> dict:
    return {
        "id": "chatcmpl-failover-config",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": f"from-{label}"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


class FakeUpstream:
    """同一个上游按 Bearer 前缀分流：fail-* 一律 500，其余 200。"""

    def __init__(self) -> None:
        self.hits = {"fail": 0, "ok": 0}
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def _json(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.send_header("connection", "close")
                self.end_headers()
                self.wfile.write(raw)
                self.close_connection = True

            def do_GET(self) -> None:
                if self.path.split("?")[0].rstrip("/").endswith("/v1/models"):
                    self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
                    return
                self._json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    payload = {}
                auth = str(self.headers.get("authorization") or "")
                bucket = "fail" if "fail" in auth.lower() else "ok"
                with outer._lock:
                    outer.hits[bucket] += 1
                if bucket == "fail":
                    self._json(500, {"error": {"message": "boom fail-key", "type": "server_error"}})
                    return
                self._json(200, _completion(str(payload.get("model") or MODEL), "opencode"))

        self._handler = Handler

    def reset(self) -> None:
        with self._lock:
            self.hits = {"fail": 0, "ok": 0}


@pytest.fixture(scope="module")
def failover_cfg(tmp_path_factory) -> types.SimpleNamespace:
    for port in (CTRL_PORT, FAKE_PORT):
        if not _port_free(port):
            pytest.fail(f"端口 {port} 已被占用，无法起故障转移配置测试控制面")

    tmp = tmp_path_factory.mktemp("failover-config")
    data_dir = tmp / "data"
    data_dir.mkdir()

    fake = FakeUpstream()
    server = ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), fake._handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{FAKE_PORT}"

    env = os.environ.copy()
    # 刻意不带 failover 环境变量：默认值必须是 OFF / 2
    for name in ("UNIFIED_FAILOVER_ENABLED", "UNIFIED_FAILOVER_MAX_ATTEMPTS"):
        env.pop(name, None)
    env.update(
        {
            "UNIFIED_DATA_DIR": str(data_dir),
            "UNIFIED_DB_PATH": str(data_dir / "unified.db"),
            "UNIFIED_ADMIN_USER": "admin",
            "UNIFIED_ADMIN_PASSWORD": "test",
            "UNIFIED_WEB_DIR": str(tmp / "no-web"),
            "UNIFIED_ALLOW_ANONYMOUS_GATEWAY": "false",
            "UNIFIED_ALLOW_PRIVATE_UPSTREAMS": "1",
            "GROK_REGISTER_EXECUTOR_URL": "",
            "CODEBUDDY_BASE": base,
            "OPENCODE_UPSTREAM_BASE": base,
        }
    )

    log_path = tmp / "control.stderr.log"
    log = log_path.open("wb")
    proc = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(CTRL_PORT), "--log-level", "warning"],
        cwd=str(CONTROL),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=log,
    )

    client = httpx.Client(base_url=f"http://127.0.0.1:{CTRL_PORT}", trust_env=False, timeout=10.0)
    try:
        if not _wait_up(f"http://127.0.0.1:{CTRL_PORT}/healthz", STARTUP_TIMEOUT, proc):
            _startup_down("故障转移配置控制面", proc, CTRL_PORT, STARTUP_TIMEOUT, log_path)
        login = client.post("/api/login", json={"username": "admin", "password": "test"})
        assert login.status_code == 200, f"{login.status_code} {login.text}"

        for pid, key in (("codebuddy", FAIL_KEY), ("opencode", OK_KEY)):
            r = client.patch(f"/api/providers/{pid}", json={"enabled": True, "api_key": key, "base_url": base})
            assert r.status_code == 200, f"{pid}: {r.status_code} {r.text}"

        imported = client.post("/api/pricing/import")
        assert imported.status_code == 200, f"{imported.status_code} {imported.text}"

        created = client.post("/api/keys", json={"name": "failover-config-suite"})
        assert created.status_code == 200, f"{created.status_code} {created.text}"

        yield types.SimpleNamespace(
            client=client,
            key=created.json()["key"],
            db_path=str(data_dir / "unified.db"),
            fake=fake,
        )
    finally:
        client.close()
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            proc.wait(timeout=2)
        server.shutdown()
        server.server_close()
        log.close()


@pytest.fixture(autouse=True)
def _reset_upstream(failover_cfg: types.SimpleNamespace):
    failover_cfg.fake.reset()
    yield


@pytest.fixture(autouse=True)
def _clear_module_cache():
    """单元级断言会改 model_resolve 的进程内副本，每个用例结束还原成空配置。"""
    yield
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        load_stored_failover(conn)
    finally:
        conn.close()


def _chat(env: types.SimpleNamespace, model: str = MODEL) -> httpx.Response:
    return env.client.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {env.key}"},
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


def _set(env: types.SimpleNamespace, **values: object) -> dict:
    r = env.client.post("/api/settings/failover", json=values)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()


def _stats(env: types.SimpleNamespace, expect: int, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    stats: dict = {}
    while time.time() < deadline:
        stats = env.client.get("/api/settings/failover").json()["stats"]
        if stats["transferred_rows"] >= expect:
            return stats
        time.sleep(0.1)
    raise AssertionError(f"transferred_rows 只有 {stats.get('transferred_rows')}，期望 >= {expect}")


def test_default_off_and_ambiguous_400(failover_cfg: types.SimpleNamespace) -> None:
    env = failover_cfg
    state = env.client.get("/api/settings/failover")
    assert state.status_code == 200, f"{state.status_code} {state.text}"
    body = state.json()
    assert body["enabled"] is False
    assert body["max_attempts"] == DEFAULT_FAILOVER_ATTEMPTS
    assert body["source"] == {"enabled": "default", "max_attempts": "default"}
    assert body["env"]["enabled_locked"] is False
    assert body["env"]["max_attempts_locked"] is False

    r = _chat(env)
    assert r.status_code == 400, f"{r.status_code} {r.text}"
    assert r.json()["error"]["code"] == "ambiguous_model"
    assert env.fake.hits == {"fail": 0, "ok": 0}


def test_enable_applies_without_restart(failover_cfg: types.SimpleNamespace) -> None:
    env = failover_cfg
    _set(env, enabled=True)
    assert env.client.get("/api/settings/failover").json()["source"]["enabled"] == "db"

    r = _chat(env)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    assert r.headers.get("x-upstream-provider") == "opencode"
    assert "from-opencode" in r.json()["choices"][0]["message"]["content"]
    # 首候选 codebuddy 失败一次，换到 opencode 成功一次
    assert env.fake.hits == {"fail": 1, "ok": 1}

    stats = _stats(env, 1)
    assert stats["transferred_rows"] >= 1
    assert stats["last_attempt_at"] is not None
    assert "request_logs" in stats["source"]


def test_attempt_cap_change_applies_immediately(failover_cfg: types.SimpleNamespace) -> None:
    env = failover_cfg
    _set(env, enabled=True, max_attempts=1)
    env.fake.reset()

    r = _chat(env)
    assert r.status_code == 502, f"{r.status_code} {r.text}"
    assert r.json()["error"]["provider"] == "codebuddy"
    assert env.fake.hits == {"fail": 1, "ok": 0}, "上限 1 时第二家一次都不该被访问"

    _set(env, max_attempts=2)
    env.fake.reset()
    r = _chat(env)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    assert r.headers.get("x-upstream-provider") == "opencode"
    assert env.fake.hits == {"fail": 1, "ok": 1}


def test_viewer_forbidden_admin_allowed(failover_cfg: types.SimpleNamespace) -> None:
    env = failover_cfg
    username, password = "viewer-failover", "viewer-pass"
    created = env.client.post("/api/users", json={"username": username, "password": password, "role": "viewer"})
    assert created.status_code in (200, 409), f"{created.status_code} {created.text}"

    viewer = httpx.Client(base_url=f"http://127.0.0.1:{CTRL_PORT}", trust_env=False, timeout=10.0)
    try:
        login = viewer.post("/api/login", json={"username": username, "password": password})
        assert login.status_code == 200, f"{login.status_code} {login.text}"
        assert viewer.get("/api/settings/failover").status_code == 403
        assert viewer.post("/api/settings/failover", json={"enabled": True}).status_code == 403
    finally:
        viewer.close()

    assert env.client.get("/api/settings/failover").status_code == 200
    assert _set(env, max_attempts=2)["saved"] == ["max_attempts"]


def test_write_is_audited(failover_cfg: types.SimpleNamespace) -> None:
    env = failover_cfg
    _set(env, enabled=True, max_attempts=3)

    conn = sqlite3.connect(env.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT action, target, detail FROM audit_logs WHERE action = 'settings.failover' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "管理员写故障转移配置必须落审计"
    assert row["target"] == "enabled,max_attempts"
    detail = json.loads(row["detail"])
    assert detail == {"enabled": True, "max_attempts": 3}


def test_invalid_values_rejected(failover_cfg: types.SimpleNamespace) -> None:
    env = failover_cfg
    for payload, reason in (
        ({"max_attempts": 99}, "out_of_range"),
        ({"max_attempts": 0}, "out_of_range"),
        ({"max_attempts": "2"}, "not_an_integer"),
        ({"enabled": "true"}, "invalid_boolean"),
        ({"nope": 1}, "unknown_key"),
        ({}, "empty_payload"),
    ):
        r = env.client.post("/api/settings/failover", json=payload)
        assert r.status_code == 400, f"{payload}: {r.status_code} {r.text}"
        assert r.json()["error"]["errors"][0]["reason"] == reason, r.text


def _unit_conn(tmp_path: Path, payload: dict | None) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "unit-settings.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if payload is not None:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('failover_config', ?)",
            (json.dumps(payload),),
        )
    conn.commit()
    return conn


def test_env_overrides_stored_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNIFIED_FAILOVER_ENABLED", raising=False)
    monkeypatch.delenv("UNIFIED_FAILOVER_MAX_ATTEMPTS", raising=False)
    conn = _unit_conn(tmp_path, {"enabled": True, "max_attempts": 5})
    try:
        load_stored_failover(conn)
        assert failover_enabled() is True
        assert failover_max_attempts() == 5
        assert failover_sources() == {"enabled": "db", "max_attempts": "db"}

        monkeypatch.setenv("UNIFIED_FAILOVER_ENABLED", "0")
        monkeypatch.setenv("UNIFIED_FAILOVER_MAX_ATTEMPTS", "1")
        assert failover_enabled() is False, "env 即使写成关闭也必须压过界面配置"
        assert failover_max_attempts() == 1
        assert failover_env_locked() == {"enabled": True, "max_attempts": True}
        assert failover_sources() == {"enabled": "env", "max_attempts": "env"}
    finally:
        conn.close()


def test_stored_attempts_are_clamped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNIFIED_FAILOVER_ENABLED", raising=False)
    monkeypatch.delenv("UNIFIED_FAILOVER_MAX_ATTEMPTS", raising=False)

    conn = _unit_conn(tmp_path, {"enabled": True, "max_attempts": 99})
    try:
        load_stored_failover(conn)
        assert failover_max_attempts() == MAX_FAILOVER_ATTEMPTS
    finally:
        conn.close()

    conn = _unit_conn(tmp_path, {"enabled": True, "max_attempts": 0})
    try:
        load_stored_failover(conn)
        assert failover_max_attempts() == 1
    finally:
        conn.close()

    conn = _unit_conn(tmp_path, {"enabled": True})
    try:
        load_stored_failover(conn)
        assert failover_max_attempts() == DEFAULT_FAILOVER_ATTEMPTS
    finally:
        conn.close()

    monkeypatch.setenv("UNIFIED_FAILOVER_MAX_ATTEMPTS", "abc")
    assert failover_max_attempts() == DEFAULT_FAILOVER_ATTEMPTS
