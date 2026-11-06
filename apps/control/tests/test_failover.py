"""跨 provider 故障转移的端到端测试。

本模块另起一个独立控制面（`UNIFIED_FAILOVER_ENABLED=1`），避免改主会话实例的默认契约；
三个可编排的本地上游分别挂在 codebuddy / trae_sidecar / deepseek_sidecar 上，按用例
返回 500/400/429/正常/流式/半路断流。解析、计费、日志都走真实网关，不用 mock。
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
    failover_max_attempts,
    retryable_status,
)
from tests.conftest import (
    CONTROL,
    PY,
    STARTUP_TIMEOUT,
    _port_free,
    _startup_down,
    _wait_up,
)

pytestmark = pytest.mark.gateway

# 价位表里已有价格、但三家测试上游的 /v1/models 都不会列出：走兜底候选 + 可计费
MODEL_500 = "hy3"
MODEL_400 = "glm-5.2"
MODEL_STREAM = "kimi-k3"
MODEL_BREAK = "gpt-4o"
MODEL_ALL_FAIL = "deepseek-v4.1-flash"
MODEL_CAP = "minimax-m3"
MODEL_429 = "kimi-k2.7"

FAKE_KEY = "fake-upstream-key"


def _sse_chunk(model: str, text: str, *, finish: str | None, usage: dict | None = None) -> bytes:
    body: dict = {
        "id": "chatcmpl-failover",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "delta": {"content": text} if text else {}, "finish_reason": finish}
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return b"data: " + json.dumps(body).encode() + b"\n\n"


def _completion(model: str, label: str) -> dict:
    return {
        "id": "chatcmpl-failover",
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


class Upstream:
    """可编排假上游：mode 决定 chat 的返回，chat_hits 用于断言"没被访问"。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.mode = "ok"
        self.chat_hits = 0
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
                if self.path.split("?")[0].rstrip("/") == "/v1/models":
                    self._json(200, {"object": "list", "data": []})
                    return
                self._json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    payload = {}
                model = str(payload.get("model") or "")
                with outer._lock:
                    outer.chat_hits += 1
                    mode = outer.mode
                if mode == "stream":
                    self._stream(model, broken=False)
                    return
                if mode == "stream-break":
                    self._stream(model, broken=True)
                    return
                code = {
                    "err-400": 400, "err-429": 429, "err-500": 500,
                    "err-502": 502, "err-503": 503,
                }.get(mode)
                if code is not None:
                    self._json(code, {"error": {
                        "message": f"{mode} {outer.label}",
                        "type": "invalid_request_error" if code < 500 else "server_error",
                    }})
                    return
                self._json(200, _completion(model, outer.label))

            def _stream(self, model: str, *, broken: bool) -> None:
                frames = [
                    _sse_chunk(model, f"from-{outer.label}", finish=None),
                    _sse_chunk(
                        model, "", finish="stop",
                        usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                    ),
                    b"data: [DONE]\n\n",
                ]
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                if broken:
                    # 声明了一个写不完的长度，随后断开：模拟首帧后上游死掉
                    self.send_header("content-length", "4096")
                else:
                    self.send_header("connection", "close")
                self.end_headers()
                self.wfile.write(frames[0])
                self.wfile.flush()
                if not broken:
                    for frame in frames[1:]:
                        self.wfile.write(frame)
                    self.wfile.flush()
                self.close_connection = True

        self._handler = Handler

    def handler(self) -> type[BaseHTTPRequestHandler]:
        return self._handler

    def reset(self, mode: str) -> None:
        with self._lock:
            self.mode = mode
            self.chat_hits = 0


def _start_upstream(label: str) -> tuple[ThreadingHTTPServer, Upstream]:
    up = Upstream(label)
    server = ThreadingHTTPServer(("127.0.0.1", 0), up.handler())
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, up


def _pick_port() -> int:
    # 主套件占 8076/8077，这里从 8100 起找空闲口，不碰别的会话的 8070-8079
    for port in range(8100, 8140):
        if _port_free(port):
            return port
    pytest.fail("8100-8139 没有空闲端口，无法起故障转移测试控制面")


@pytest.fixture(scope="module")
def failover_env(tmp_path_factory) -> types.SimpleNamespace:
    port = _pick_port()
    tmp = tmp_path_factory.mktemp("failover")
    data_dir = tmp / "data"
    data_dir.mkdir()

    servers = {label: _start_upstream(label) for label in ("codebuddy", "trae_sidecar", "deepseek_sidecar")}
    bases = {label: f"http://127.0.0.1:{server.server_address[1]}" for label, (server, _) in servers.items()}

    env = os.environ.copy()
    env.update({
        "UNIFIED_DATA_DIR": str(data_dir),
        "UNIFIED_DB_PATH": str(data_dir / "unified.db"),
        "UNIFIED_ADMIN_USER": "admin",
        "UNIFIED_ADMIN_PASSWORD": "test",
        "UNIFIED_WEB_DIR": str(tmp / "no-web"),
        "UNIFIED_ALLOW_ANONYMOUS_GATEWAY": "false",
        "UNIFIED_ALLOW_PRIVATE_UPSTREAMS": "1",
        "GROK_REGISTER_EXECUTOR_URL": "",
        "UNIFIED_FAILOVER_ENABLED": "1",
        "UNIFIED_FAILOVER_MAX_ATTEMPTS": "2",
        # 未知模型落到兜底候选，才能让三家都成为同一请求的候选
        "UNIFIED_MODEL_FALLBACK_FIRST": "1",
    })

    log_path = tmp / "control.stderr.log"
    log = log_path.open("wb")
    proc = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(CONTROL),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=log,
    )

    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10.0)
    try:
        if not _wait_up(f"http://127.0.0.1:{port}/healthz", STARTUP_TIMEOUT, proc):
            _startup_down("故障转移控制面", proc, port, STARTUP_TIMEOUT, log_path)
        login = client.post("/api/login", json={"username": "admin", "password": "test"})
        assert login.status_code == 200, f"{login.status_code} {login.text}"

        patches = {
            "codebuddy": {"base_url": bases["codebuddy"]},
            "trae_sidecar": {"enabled": True, "api_key": FAKE_KEY, "base_url": bases["trae_sidecar"]},
            "deepseek_sidecar": {"enabled": True, "api_key": FAKE_KEY, "base_url": bases["deepseek_sidecar"]},
        }
        for pid, patch in patches.items():
            r = client.patch(f"/api/providers/{pid}", json=patch)
            assert r.status_code == 200, f"{pid}: {r.status_code} {r.text}"

        imported = client.post("/api/pricing/import")
        assert imported.status_code == 200, f"{imported.status_code} {imported.text}"

        created = client.post("/api/keys", json={"name": "failover-suite"})
        assert created.status_code == 200, f"{created.status_code} {created.text}"

        yield types.SimpleNamespace(
            client=client,
            key=created.json()["key"],
            db_path=str(data_dir / "unified.db"),
            upstreams={label: up for label, (_, up) in servers.items()},
        )
    finally:
        client.close()
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            proc.wait(timeout=2)
        for server, _ in servers.values():
            server.shutdown()
            server.server_close()
        log.close()


def _headers(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


def _chat(env: types.SimpleNamespace, model: str, *, stream: bool = False) -> httpx.Response:
    return env.client.post(
        "/v1/chat/completions",
        headers=_headers(env.key),
        json={
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
        },
    )


def _logs(env: types.SimpleNamespace, model: str, total: int, timeout: float = 5.0) -> list[dict]:
    deadline = time.time() + timeout
    last = 0
    while time.time() < deadline:
        body = env.client.get("/api/logs", params={"model": model, "size": 50}).json()
        last = body["total"]
        if last >= total:
            return body["items"]
        time.sleep(0.1)
    raise AssertionError(f"{model} 的 request_logs 只有 {last} 行，期望 {total}")


def _max_ledger_id(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM billing_ledger").fetchone()[0])
    finally:
        conn.close()


def _ledger_rows(db_path: str, after_id: int) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM billing_ledger WHERE id > ? ORDER BY id", (after_id,)
        ).fetchall()
    finally:
        conn.close()


def _wait_ledger(db_path: str, after_id: int, expect: int, timeout: float = 5.0) -> list[sqlite3.Row]:
    deadline = time.time() + timeout
    rows: list[sqlite3.Row] = []
    while time.time() < deadline:
        rows = _ledger_rows(db_path, after_id)
        if len(rows) >= expect:
            return rows
        time.sleep(0.1)
    raise AssertionError(f"billing_ledger 只有 {len(rows)} 笔新增，期望 {expect}")


def _assert_no_internal_leak(text: str) -> None:
    for needle in ("127.0.0.1", "http://", "https://", "u2a_", "sk-"):
        assert needle not in text, f"错误体泄露了内部信息：{needle} / {text}"


def test_first_target_500_falls_over_and_bills_once(failover_env: types.SimpleNamespace) -> None:
    env = failover_env
    env.upstreams["codebuddy"].reset("err-500")
    env.upstreams["trae_sidecar"].reset("ok")
    env.upstreams["deepseek_sidecar"].reset("ok")
    before = _max_ledger_id(env.db_path)

    r = _chat(env, MODEL_500)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    assert r.headers.get("x-upstream-provider") == "trae_sidecar"
    assert "from-trae_sidecar" in r.json()["choices"][0]["message"]["content"]

    items = _logs(env, MODEL_500, 2)
    # 上游 500 对外映射为 502，日志跟随对外状态，保证日志与响应同一口径
    failed = [row for row in items if row["status"] == 502]
    ok = [row for row in items if row["status"] == 200]
    assert len(failed) == 1 and failed[0]["provider"] == "codebuddy"
    assert failed[0]["credit_status"] == "not_billed"
    assert failed[0]["error"].startswith("[尝试1/2]"), failed[0]["error"]
    assert len(ok) == 1 and ok[0]["provider"] == "trae_sidecar"
    assert ok[0]["credit_status"] == "billed"

    ledger = _wait_ledger(env.db_path, before, 1)
    assert len(ledger) == 1, f"一次请求只允许一笔成功账单：{[dict(r) for r in ledger]}"
    assert ledger[0]["provider"] == "trae_sidecar"
    assert ledger[0]["note"] == ""
    time.sleep(0.3)
    assert len(_ledger_rows(env.db_path, before)) == 1


def test_client_400_is_not_retried(failover_env: types.SimpleNamespace) -> None:
    env = failover_env
    env.upstreams["codebuddy"].reset("err-400")
    env.upstreams["trae_sidecar"].reset("ok")
    env.upstreams["deepseek_sidecar"].reset("ok")
    before = _max_ledger_id(env.db_path)

    r = _chat(env, MODEL_400)
    assert r.status_code == 400, f"{r.status_code} {r.text}"
    assert r.json()["error"]["code"] == "upstream_bad_request"
    assert r.json()["error"]["provider"] == "codebuddy"
    _assert_no_internal_leak(r.text)

    # 400 是请求本身的问题，换家毫无意义：第二个目标一次都不该被调用
    assert env.upstreams["trae_sidecar"].chat_hits == 0
    items = _logs(env, MODEL_400, 1)
    assert len(items) == 1 and items[0]["provider"] == "codebuddy"
    assert items[0]["status"] == 400 and items[0]["credit_status"] == "not_billed"
    time.sleep(0.2)
    assert _ledger_rows(env.db_path, before) == []


def test_429_falls_over(failover_env: types.SimpleNamespace) -> None:
    env = failover_env
    env.upstreams["codebuddy"].reset("err-429")
    env.upstreams["trae_sidecar"].reset("ok")
    before = _max_ledger_id(env.db_path)

    r = _chat(env, MODEL_429)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    assert r.headers.get("x-upstream-provider") == "trae_sidecar"

    items = _logs(env, MODEL_429, 2)
    assert {row["status"] for row in items} == {429, 200}
    assert _wait_ledger(env.db_path, before, 1) != []


def test_stream_failure_before_first_byte_falls_over(failover_env: types.SimpleNamespace) -> None:
    env = failover_env
    env.upstreams["codebuddy"].reset("err-500")
    env.upstreams["trae_sidecar"].reset("stream")
    before = _max_ledger_id(env.db_path)

    r = _chat(env, MODEL_STREAM, stream=True)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    assert r.headers.get("x-upstream-provider") == "trae_sidecar"
    body = r.text
    assert "from-trae_sidecar" in body, body
    assert "data: [DONE]" in body

    items = _logs(env, MODEL_STREAM, 2)
    assert {row["status"] for row in items} == {502, 200}
    ok = next(row for row in items if row["status"] == 200)
    assert ok["stream"] is True and ok["credit_status"] == "billed"
    ledger = _wait_ledger(env.db_path, before, 1)
    assert len(ledger) == 1 and ledger[0]["provider"] == "trae_sidecar"


def test_stream_break_after_first_byte_does_not_fall_over(failover_env: types.SimpleNamespace) -> None:
    env = failover_env
    env.upstreams["codebuddy"].reset("stream-break")
    env.upstreams["trae_sidecar"].reset("stream")
    before = _max_ledger_id(env.db_path)

    r = _chat(env, MODEL_BREAK, stream=True)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.text
    assert "from-codebuddy" in body, body
    # 首帧已经发给客户端，不能再拼第二家的内容，只能补一个错误帧
    assert "from-trae_sidecar" not in body
    assert '"error"' in body, body

    assert env.upstreams["trae_sidecar"].chat_hits == 0
    items = _logs(env, MODEL_BREAK, 1)
    assert len(items) == 1
    row = items[0]
    assert row["provider"] == "codebuddy" and row["status"] == 200
    assert row["stream"] is True and row["error"]
    assert row["credit_status"] == "not_billed"
    time.sleep(0.2)
    assert _ledger_rows(env.db_path, before) == []


def test_all_targets_fail_returns_last_upstream_error(failover_env: types.SimpleNamespace) -> None:
    env = failover_env
    env.upstreams["codebuddy"].reset("err-502")
    env.upstreams["trae_sidecar"].reset("err-503")
    env.upstreams["deepseek_sidecar"].reset("ok")
    before = _max_ledger_id(env.db_path)

    r = _chat(env, MODEL_ALL_FAIL)
    assert r.status_code == 503, f"{r.status_code} {r.text}"
    error = r.json()["error"]
    assert error["code"] == "provider_unavailable"
    assert error["provider"] == "trae_sidecar"
    assert error["upstream_status"] == 503
    _assert_no_internal_leak(r.text)

    items = _logs(env, MODEL_ALL_FAIL, 2)
    assert {row["status"] for row in items} == {502, 503}
    assert all(row["credit_status"] == "not_billed" for row in items)
    time.sleep(0.2)
    assert _ledger_rows(env.db_path, before) == []


def test_attempt_cap_stops_third_provider(failover_env: types.SimpleNamespace) -> None:
    env = failover_env
    env.upstreams["codebuddy"].reset("err-500")
    env.upstreams["trae_sidecar"].reset("err-500")
    env.upstreams["deepseek_sidecar"].reset("ok")
    before = _max_ledger_id(env.db_path)

    r = _chat(env, MODEL_CAP)
    assert r.status_code == 502, f"{r.status_code} {r.text}"
    assert r.json()["error"]["provider"] == "trae_sidecar"

    # 上限 2：第三家必须一次都没被访问，避免一次请求打穿整个池子
    assert env.upstreams["deepseek_sidecar"].chat_hits == 0
    items = _logs(env, MODEL_CAP, 2)
    assert len(items) == 2
    assert all(row["credit_status"] == "not_billed" for row in items)
    assert any(row["error"].startswith("[尝试2/2]") for row in items)
    time.sleep(0.2)
    assert _ledger_rows(env.db_path, before) == []


def test_explicit_provider_is_pinned_and_not_retried(failover_env: types.SimpleNamespace) -> None:
    env = failover_env
    env.upstreams["codebuddy"].reset("err-500")
    env.upstreams["trae_sidecar"].reset("ok")
    before = _max_ledger_id(env.db_path)

    r = env.client.post(
        "/v1/chat/completions",
        headers=_headers(env.key) | {"x-provider": "codebuddy"},
        json={"model": "explicit-model-x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 502, f"{r.status_code} {r.text}"
    assert r.json()["error"]["provider"] == "codebuddy"
    assert env.upstreams["trae_sidecar"].chat_hits == 0
    items = _logs(env, "explicit-model-x", 1)
    assert len(items) == 1 and items[0]["provider"] == "codebuddy"
    time.sleep(0.2)
    assert _ledger_rows(env.db_path, before) == []


def test_switch_off_keeps_ambiguous_400(admin: httpx.Client, anon: httpx.Client) -> None:
    """默认（开关关闭）的主实例：目录多命中仍是 400，不会被静默改成自动路由。"""
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "opencode")
    added_key = not before.get("has_api_key")
    key = admin.post("/api/keys", json={"name": "failover-off-suite"}).json()["key"]
    try:
        admin.patch("/api/providers/opencode", json={"enabled": True, "api_key": "fake-key"})
        r = anon.post(
            "/v1/chat/completions",
            headers=_headers(key),
            json={"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 400, f"{r.status_code} {r.text}"
        assert r.json()["error"]["code"] == "ambiguous_model"
    finally:
        restore = {"enabled": bool(before.get("enabled"))}
        if added_key:
            restore["api_key"] = ""
        admin.patch("/api/providers/opencode", json=restore)


def test_retryable_status_matrix() -> None:
    for status in (408, 429, 500, 502, 503, 504, 505):
        assert retryable_status(status), status
    for status in (400, 401, 403, 404, 409, 422, 501):
        assert not retryable_status(status), status


def test_failover_env_switch_and_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNIFIED_FAILOVER_ENABLED", raising=False)
    monkeypatch.delenv("UNIFIED_FAILOVER_MAX_ATTEMPTS", raising=False)
    assert failover_enabled() is False
    assert failover_max_attempts() == DEFAULT_FAILOVER_ATTEMPTS

    monkeypatch.setenv("UNIFIED_FAILOVER_ENABLED", "on")
    monkeypatch.setenv("UNIFIED_FAILOVER_MAX_ATTEMPTS", "99")
    assert failover_enabled() is True
    assert failover_max_attempts() == MAX_FAILOVER_ATTEMPTS

    monkeypatch.setenv("UNIFIED_FAILOVER_MAX_ATTEMPTS", "0")
    assert failover_max_attempts() == 1
    monkeypatch.setenv("UNIFIED_FAILOVER_MAX_ATTEMPTS", "abc")
    assert failover_max_attempts() == DEFAULT_FAILOVER_ATTEMPTS
