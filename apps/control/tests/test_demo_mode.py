"""演示模式（UNIFIED_DEMO_MODE）验收：开启有数据、关闭无痕迹、非回环地址拒绝启动。"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from app import demo
from app.billing import period_bounds

CONTROL_DIR = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
BASE_PORT = int(os.getenv("UNIFIED_TEST_CTRL_PORT", "8060"))
DEMO_PORT = BASE_PORT + 20
OFF_PORT = DEMO_PORT + 1
CLEAR_PORT = DEMO_PORT + 2
ADMIN_PASS = "demo-test-pass"


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _env(data_dir: Path, *, demo_on: bool, port: int) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "UNIFIED_DATA_DIR": str(data_dir),
            "UNIFIED_DB_PATH": str(data_dir / "unified.db"),
            "UNIFIED_HOST": "127.0.0.1",
            "UNIFIED_PORT": str(port),
            "UNIFIED_ADMIN_PASSWORD": ADMIN_PASS,
            "UNIFIED_WEB_DIR": str(data_dir / "web"),
        }
    )
    if demo_on:
        env["UNIFIED_DEMO_MODE"] = "1"
    else:
        env.pop("UNIFIED_DEMO_MODE", None)
    return env


def _tail(proc: subprocess.Popen) -> str:
    stream = getattr(proc, "stdout", None)
    if stream is None:
        return ""
    try:
        return (stream.read() or "")[-4000:]
    except Exception:  # pragma: no cover - 只用于失败诊断
        return ""


@contextmanager
def _control(data_dir: Path, *, demo_on: bool, port: int):
    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(CONTROL_DIR),
        env=_env(data_dir, demo_on=demo_on, port=port),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"控制面提前退出（demo={demo_on}）：\n{_tail(proc)}")
            try:
                if httpx.get(f"{base}/healthz", timeout=2, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise AssertionError(f"控制面 60s 未就绪（demo={demo_on}）：\n{_tail(proc)}")
        yield base, data_dir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _demo_token(data_dir: Path) -> str:
    text = (data_dir / "demo-credentials.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        if "= u2a_" in line:
            return line.split("= ", 1)[1].strip()
    raise AssertionError("演示密钥文件里没有可用 token")


def _db_counts(data_dir: Path) -> dict[str, int]:
    conn = sqlite3.connect(data_dir / "unified.db")
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "request_logs",
                "usage_records",
                "billing_ledger",
                "price_books",
                "quota_policies",
                "quota_alerts",
                "accounts",
                "provider_credentials",
                "sub2_groups",
                "sub2_accounts",
                "sub2_ledger",
            )
        }
    finally:
        conn.close()


@pytest.fixture(scope="module")
def demo_site(tmp_path_factory):
    if not _port_free(DEMO_PORT):
        pytest.skip(f"端口 {DEMO_PORT} 被占用，跳过演示模式用例")
    data_dir = tmp_path_factory.mktemp("demo-on")
    with _control(data_dir, demo_on=True, port=DEMO_PORT) as ctx:
        yield ctx


@pytest.fixture(scope="module")
def plain_site(tmp_path_factory):
    if not _port_free(OFF_PORT):
        pytest.skip(f"端口 {OFF_PORT} 被占用，跳过关闭模式用例")
    data_dir = tmp_path_factory.mktemp("demo-off")
    with _control(data_dir, demo_on=False, port=OFF_PORT) as ctx:
        yield ctx


def test_loopback_host_predicate():
    assert demo.is_loopback_host("127.0.0.1")
    assert demo.is_loopback_host("127.0.0.5")
    assert demo.is_loopback_host("localhost")
    assert demo.is_loopback_host("::1")
    for rejected in ("0.0.0.0", "192.168.1.10", "example.com", ""):
        assert not demo.is_loopback_host(rejected)


def test_ensure_loopback_bind_rejects_public_host():
    with pytest.raises(demo.DemoModeError) as exc:
        demo.ensure_loopback_bind("0.0.0.0")
    assert "回环" in str(exc.value) and "UNIFIED_DEMO_MODE" in str(exc.value)


def test_ledger_day_buckets_hold_at_local_morning():
    """本地 07:30（UTC 为前一日 23:30）时，日桶仍须按 UTC+8 切出 7 天。

    种子行基于本地零点推算，day-0 行会被 min(ts_row, ts) 压到当前时刻；
    本地凌晨的那个时刻在 UTC 上属于前一日，只有按本地日界分桶才能稳定得到 7 个。
    """
    ts = int(dt.datetime(2026, 11, 5, 7, 30, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())
    day_start = period_bounds("day", ts)[0]
    rows = []
    for day, hour, minute, *_rest in demo.DEMO_SUB2_LEDGER:
        row_ts = day_start - day * 86400 + hour * 3600 + minute * 60
        if day == 0:
            row_ts = min(row_ts, ts)
        rows.append(row_ts)
    assert len({period_bounds("day", row_ts)[0] for row_ts in rows}) == 7
    # 旧断言的 UTC 日桶在这个时刻会把 day-0 与 day-1 撞进同一桶，只剩 6 个
    assert len({row_ts // 86400 for row_ts in rows}) == 6


def test_readiness_reflects_demo_provider(demo_site):
    base, _ = demo_site
    resp = httpx.get(f"{base}/api/health/ready", timeout=10, trust_env=False)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    items = payload["checks"]["providers"]["items"]
    demo_item = next(item for item in items if item["id"] == "demo_local")
    assert demo_item["enabled"] is True
    assert demo_item["readiness"] == "ready"
    assert demo_item["available"] is True
    assert payload["checks"]["providers"]["available"] >= 1


def test_models_and_chat_go_through_gateway(demo_site):
    base, data_dir = demo_site
    headers = {"Authorization": f"Bearer {_demo_token(data_dir)}"}
    models = httpx.get(f"{base}/v1/models", headers=headers, timeout=30, trust_env=False)
    assert models.status_code == 200
    ids = {item["id"] for item in models.json()["data"]}
    assert {"fake-chat-1", "fake-chat-2"} <= ids

    reply = httpx.post(
        f"{base}/v1/chat/completions",
        headers=headers,
        json={"model": "fake-chat-1", "messages": [{"role": "user", "content": "演示验收"}]},
        timeout=30,
        trust_env=False,
    )
    assert reply.status_code == 200
    payload = reply.json()
    assert payload["choices"][0]["message"]["content"].startswith("[fake]")
    assert payload["usage"]["total_tokens"] > 0


def test_seeded_rows_are_visible_and_marked(demo_site):
    base, data_dir = demo_site
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
        login = client.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        assert login.status_code == 200
        logs = client.get("/api/logs", params={"limit": 50}).json()
        assert logs["total"] >= 6
        assert {item["provider"] for item in logs["items"]} == {"demo_local"}
        assert any(str(item["key_name"]).startswith("[demo] ") for item in logs["items"])

        keys = client.get("/api/keys").json()
        assert isinstance(keys, list)
        names = [item["name"] for item in keys]
        assert all(name.startswith("[demo] ") for name in names)
        assert len(names) == 2

        stats = client.get("/api/stats/summary").json()
        assert stats["total_requests"] >= 6
        assert stats["today_credit"] > 0

        accounts = client.get("/api/accounts").json()
        assert accounts["total"] == 2
        assert all(item["provider"] == "demo_local" for item in accounts["accounts"])

        # /quota 页三块：分组卡片、账号池、消费流水
        groups = client.get("/api/sub2api/groups").json()
        assert {group["id"] for group in groups} == {"demo_team", "demo_shared"}
        team = next(group for group in groups if group["id"] == "demo_team")
        assert team["quota_used"] > 0
        assert team["quota_used"] < team["quota_limit"]  # 限额组的预算进度有真实比例
        assert team["accounts"] >= 3 and team["active_accounts"] >= 1
        shared = next(group for group in groups if group["id"] == "demo_shared")
        assert shared["quota_limit"] == 0  # 共享池不限额

        pool = client.get("/api/sub2api/accounts").json()
        assert len(pool) >= 5
        assert all(item["has_api_key"] for item in pool)
        assert any(item["cooldown_until"] > time.time() for item in pool)  # 冷却中徽标
        assert any(item["enabled"] is False for item in pool)  # 已停用徽标
        assert any(
            item["quota_limit"] > 0 and item["quota_used"] >= item["quota_limit"] for item in pool
        )  # 额度用尽徽标

        ledger = client.get("/api/sub2api/ledger").json()
        assert len(ledger) >= 20
        assert {row["group_id"] for row in ledger} == {"demo_team", "demo_shared"}
        assert any(row["status"] >= 400 for row in ledger)
        now_ts = int(time.time())
        day_start, _ = period_bounds("day", now_ts)
        assert max(row["ts"] for row in ledger) <= now_ts
        assert any(day_start <= row["ts"] <= now_ts for row in ledger)  # 「今日」按当前时间生成
        # 跨 7 天分布按 UTC+8 日桶数（与种子同一口径）；用 ts // 86400 是 UTC 日桶，
        # 本地凌晨会把被 min(ts_row, ts) 压到今天的 day-0 行算进前一桶，只剩 6 个
        assert len({period_bounds("day", row["ts"])[0] for row in ledger}) >= 7

        # 今日用量 / 预算进度 / 告警阈值：三个 quota 端点都得有真数据
        status = client.get("/api/quota/status").json()
        assert len(status["items"]) == 3
        alert_item = next(
            item for item in status["items"] if item["policy"]["key_name"].endswith("（已停用）")
        )
        assert alert_item["used_requests"] == demo.DEMO_ALERT_LIMIT_REQUESTS
        assert alert_item["exceeded"] is True
        assert [row["threshold_percent"] for row in alert_item["alerts"]] == [80, 100]

        alerts = client.get("/api/quota/alerts").json()
        assert alerts["total"] >= 2
        assert {row["metric"] for row in alerts["items"]} == {"requests"}
        assert all(row["key_prefix"].startswith("u2a_") for row in alerts["items"])  # 只回掩码前缀

    counts = _db_counts(data_dir)
    assert counts["quota_policies"] == 3  # 密钥 A 两条 + 告警样例一条
    assert counts["quota_alerts"] >= 2
    assert counts["sub2_groups"] == 2
    assert counts["sub2_accounts"] == 5
    assert counts["sub2_ledger"] >= 20
    assert counts["price_books"] == 4


def test_plain_mode_has_no_demo_traces(plain_site):
    base, data_dir = plain_site
    ready = httpx.get(f"{base}/api/health/ready", timeout=10, trust_env=False)
    assert ready.status_code == 503
    items = ready.json()["checks"]["providers"]["items"]
    assert all(item["id"] != "demo_local" for item in items)

    assert httpx.get(f"{base}/v1/models", timeout=10, trust_env=False).status_code == 401

    with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
        assert client.post("/api/login", json={"username": "admin", "password": ADMIN_PASS}).status_code == 200
        logs = client.get("/api/logs", params={"limit": 50}).json()
        assert logs["total"] == 0
        assert client.get("/api/keys").json() == []
        assert client.get("/api/stats/summary").json()["total_requests"] == 0

    assert not (data_dir / "demo-credentials.txt").exists()
    counts = _db_counts(data_dir)
    assert all(value == 0 for value in counts.values()), counts
    conn = sqlite3.connect(data_dir / "unified.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM providers WHERE id LIKE 'demo\\_%' ESCAPE '\\'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM api_keys WHERE name LIKE '[demo] %'").fetchone()[0] == 0
    finally:
        conn.close()


def test_clear_removes_only_demo_rows(tmp_path_factory):
    if not _port_free(CLEAR_PORT):
        pytest.skip(f"端口 {CLEAR_PORT} 被占用，跳过清除用例")
    data_dir = tmp_path_factory.mktemp("demo-clear")
    with _control(data_dir, demo_on=True, port=CLEAR_PORT):
        pass

    before = _db_counts(data_dir)
    assert before["request_logs"] > 0

    clear_code = (
        "import sys, json;"
        f"sys.path.insert(0, r'{CONTROL_DIR}');"
        "from app.config import load_settings;"
        "from app.db import Database;"
        "from app.demo import clear_demo_data;"
        "settings = load_settings();"
        "print('CLEARED=' + json.dumps(clear_demo_data(Database(settings.db_path), settings)))"
    )
    result = subprocess.run(
        [PYTHON, "-c", clear_code],
        cwd=str(CONTROL_DIR),
        env=_env(data_dir, demo_on=False, port=CLEAR_PORT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "CLEARED=" in result.stdout
    cleared = json.loads(result.stdout.split("CLEARED=", 1)[1].strip())
    assert cleared["request_logs"] == before["request_logs"]
    assert cleared["providers"] == 1
    assert cleared["providers_restored"] == 1  # codebuddy 的默认启用状态被恢复
    assert not (data_dir / "demo-credentials.txt").exists()

    after = _db_counts(data_dir)
    assert all(value == 0 for value in after.values()), after
    conn = sqlite3.connect(data_dir / "unified.db")
    try:
        row = conn.execute("SELECT enabled FROM providers WHERE id = 'codebuddy'").fetchone()
        assert row is not None and row[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM settings WHERE key = 'demo_seed'").fetchone()[0] == 0
    finally:
        conn.close()


def test_non_loopback_bind_aborts_startup(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("demo-public")
    env = _env(data_dir, demo_on=True, port=0)
    env["UNIFIED_HOST"] = "0.0.0.0"
    result = subprocess.run(
        [PYTHON, "-c", f"import sys; sys.path.insert(0, r'{CONTROL_DIR}'); import app.main"],
        cwd=str(CONTROL_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "UNIFIED_DEMO_MODE" in output
    assert "回环" in output
    assert not (data_dir / "demo-credentials.txt").exists()
