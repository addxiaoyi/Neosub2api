"""可观测性回归：request id 贯穿、JSON 日志脱敏、探针免鉴权、readiness 503。

JSON 模式是启动期开关，只能在独立控制面实例上验证，因此这里起一个临时 uvicorn。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from app.observability import JsonFormatter

REPO = Path(__file__).resolve().parents[3]
CONTROL = REPO / "apps" / "control"
# 起子进程就用当前解释器，不绑定 Windows 内嵌运行时的目录布局
PY = sys.executable


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_up(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.0, trust_env=False).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


# ---------- formatter 单元：json 模式下 u2a_ 密钥仍需脱敏 ----------


def test_json_formatter_redacts_u2a_key() -> None:
    secret = "u2a_supersecretvalue42"
    record = logging.LogRecord(
        "unified.access", logging.INFO, __file__, 1,
        "GET /v1/models?key=%s -> 200", (secret,), None,
    )
    payload = json.loads(JsonFormatter().format(record))
    dumped = json.dumps(payload, ensure_ascii=False)
    assert secret not in dumped
    assert "u2a_…" in dumped  # 掩码口径与 api_utils.mask_secret 一致
    assert payload["level"] == "INFO"
    assert "\n" not in JsonFormatter().format(record)


def test_json_formatter_reads_uvicorn_access_fields() -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/api/health?key=u2a_abcdefghijk", "1.1", 200),
        None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["status"] == 200
    assert payload["path"] == "/api/health"  # 路径不带查询串
    assert "u2a_abcdefghijk" not in json.dumps(payload)


# ---------- 独立端点：request id 与 json 日志 ----------


@pytest.fixture(scope="module")
def json_gateway(tmp_path_factory):
    if not Path(PY).exists():
        pytest.skip(f"运行时 python 缺失：{PY}")
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("json-log")
    env = os.environ.copy()
    env.update(
        {
            "UNIFIED_DATA_DIR": str(data_dir),
            "UNIFIED_DB_PATH": str(data_dir / "unified.db"),
            "UNIFIED_ADMIN_USER": "admin",
            "UNIFIED_ADMIN_PASSWORD": "test",
            "UNIFIED_WEB_DIR": str(data_dir / "no-web"),
            "UNIFIED_ALLOW_PRIVATE_UPSTREAMS": "1",
            "GROK_REGISTER_EXECUTOR_URL": "",
            "UNIFIED_LOG_FORMAT": "json",
        }
    )
    proc = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(CONTROL),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        if not _wait_up(f"{base}/api/health"):
            pytest.skip("JSON 日志实例启动失败")
        yield base, proc
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.communicate(timeout=10)
        except Exception:
            proc.kill()


def test_request_id_is_echoed_on_json_gateway(json_gateway) -> None:
    base, _ = json_gateway
    with httpx.Client(base_url=base, trust_env=False, timeout=10.0) as client:
        r = client.get("/api/health", headers={"X-Request-Id": "trace-abc-123"})
        assert r.status_code == 200
        assert r.headers.get("x-request-id") == "trace-abc-123"


def test_json_logs_carry_request_id_and_redact_keys(json_gateway) -> None:
    base, proc = json_gateway
    secret = "u2a_supersecretvalue42"
    with httpx.Client(base_url=base, trust_env=False, timeout=10.0) as client:
        r = client.get(f"/api/health?probe={secret}", headers={"X-Request-Id": "trace-log-1"})
        assert r.status_code == 200
    proc.terminate()
    output = proc.communicate(timeout=10)[0]

    records = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{"):
            records.append(json.loads(line))  # 单行 JSON：能逐行解析即证明没被换行打断

    access = [item for item in records if item.get("logger") == "unified.access"]
    assert access, output
    # 第一条是启动探活（无调用方 id），取最后一条才是带 X-Request-Id 的业务请求
    entry = next(item for item in reversed(access) if item.get("path") == "/api/health")
    assert entry["request_id"] == "trace-log-1"
    assert entry["status"] == 200
    assert isinstance(entry["duration_ms"], (int, float))
    assert secret not in output


# ---------- 共享控制面：探针免鉴权 / readiness 503 / metrics ----------


def test_request_id_header_sanitized(base_url) -> None:
    with httpx.Client(base_url=base_url, trust_env=False, timeout=10.0) as client:
        generated = client.get("/api/health").headers.get("x-request-id")
        assert generated and len(generated) >= 8
        bad = client.get("/api/health", headers={"X-Request-Id": 'x" y'})
        # 非法字符不能原样回写（防止响应头注入），应换成新的 uuid hex
        assert bad.headers.get("x-request-id") != 'x" y'
        assert len(bad.headers.get("x-request-id", "")) == 32


def test_probes_work_without_login(base_url) -> None:
    # 用全新的 client：会话级 anon 在其他用例登录后可能带 cookie，不能证明免鉴权
    with httpx.Client(base_url=base_url, trust_env=False, timeout=10.0) as client:
        live = client.get("/api/health")
        assert live.status_code == 200
        assert live.json()["ok"] is True
        assert "db" not in live.json()

        ready = client.get("/api/health/ready")
        assert ready.status_code in (200, 503)
        body = ready.json()
        assert body["checks"]["database"]["ok"] is True
        text = ready.text
        for leaked in ("base_url", "unified.db", "u2a_"):
            assert leaked not in text


def test_readiness_503_names_unhealthy_subsystem(admin, base_url) -> None:
    original = {item["id"]: bool(item["enabled"]) for item in admin.get("/api/providers").json()}
    try:
        for pid, enabled in original.items():
            if enabled:
                admin.patch(f"/api/providers/{pid}", json={"enabled": False}).raise_for_status()
        down = admin.get("/api/health/ready")
        assert down.status_code == 503
        assert down.json()["unhealthy"] == ["providers"]
        assert down.json()["checks"]["providers"]["enabled"] == 0

        # opencode 的 spec.readiness 是 ready；启用后至少一家可服务，探针应恢复 200
        admin.patch("/api/providers/opencode", json={"enabled": True}).raise_for_status()
        up = admin.get("/api/health/ready")
        assert up.status_code == 200
        assert up.json()["unhealthy"] == []
    finally:
        for pid, enabled in original.items():
            admin.patch(f"/api/providers/{pid}", json={"enabled": enabled}).raise_for_status()


def test_metrics_requires_admin_and_reads_existing_tables(admin, base_url) -> None:
    with httpx.Client(base_url=base_url, trust_env=False, timeout=10.0) as client:
        assert client.get("/api/metrics").status_code == 401

    text = admin.get("/api/metrics")
    assert text.status_code == 200
    assert text.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in text.headers["content-type"]

    body = admin.get("/api/metrics/json").json()
    assert {"generated_at", "window_seconds", "requests", "providers", "failure_rate_limited"} <= set(body)
    assert {"total", "success", "failed"} <= set(body["requests"])
    assert isinstance(body["providers"], list)
    assert body["window_seconds"] == 3600
    narrowed = admin.get("/api/metrics/json", params={"window_seconds": 10})
    assert narrowed.json()["window_seconds"] == 60  # 下限收口，避免全表扫描
