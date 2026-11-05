"""provider 定时健康检测：调度口径、三档落库、脱敏与边界。

分两层验证：
- 服务层直连临时 SQLite，用假适配器覆盖三档结果与脱敏，不依赖网络；
- 端点层打会话级控制面，校验权限、快照形状，以及「不写用量表、不改启用状态」。
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

CONTROL = Path(__file__).resolve().parents[1]
if str(CONTROL) not in sys.path:
    sys.path.insert(0, str(CONTROL))

from app.adapters.base import ProviderError
from app.config import clamp_health_interval, load_settings
from app.db import Database
from app.services.provider_health import ProviderHealthService

LEAK = "sk-LEAK-abcdef123456"
VIEWER_PASSWORD = "health-viewer-pass"


class _OkAdapter:
    """覆盖 health()：按适配器自己报的 connected 判定。"""

    spec = SimpleNamespace(name="Okay", id="okay")

    def __init__(self) -> None:
        self.api_key = ""
        self.api_keys: list[str] = []
        self.options: dict[str, object] = {}

    async def health(self) -> dict:
        return {"connected": True, "note": "上游已确认"}


class _ModelsAdapter:
    """只覆盖 fetch_models()：拉得到目录就算连通。"""

    spec = SimpleNamespace(name="Models", id="models")

    def __init__(self) -> None:
        self.api_key = ""
        self.api_keys: list[str] = []
        self.options: dict[str, object] = {}

    async def fetch_models(self) -> list[dict]:
        return [{"id": "m1"}, {"id": "m2"}]


class _BareAdapter:
    """两个方法都没覆盖：结果必须是 unsupported，不能伪造失败。"""

    spec = SimpleNamespace(name="Bare", id="bare")

    def __init__(self) -> None:
        self.api_key = ""
        self.api_keys: list[str] = []
        self.options: dict[str, object] = {}


class _BadAdapter(_BareAdapter):
    spec = SimpleNamespace(name="Bad", id="bad")

    def __init__(self, message: str, secret: str = "") -> None:
        super().__init__()
        self.api_key = secret
        self._message = message

    async def health(self) -> dict:
        raise ProviderError(401, self._message)


class _Registry:
    def __init__(self, adapters: dict[str, object]) -> None:
        self._adapters = dict(adapters)

    def list_public(self) -> list[dict]:
        return [{"id": pid} for pid in self._adapters]

    def get(self, pid: str) -> object:
        return self._adapters[pid]


@pytest.fixture()
def health_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "health.db")


def _service(db: Database, adapters: dict[str, object], *, enabled: bool = True, interval: object = 300):
    settings = SimpleNamespace(health_check_enabled=enabled, health_check_interval_sec=interval)
    return ProviderHealthService(settings, db, _Registry(adapters))


def _rows(db: Database) -> list[sqlite3.Row]:
    conn = db.connect()
    try:
        return conn.execute("SELECT * FROM provider_health_checks ORDER BY id").fetchall()
    finally:
        conn.close()


def _count(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


# ---- 调度开关与间隔钳制 ----


async def test_tick_does_nothing_when_disabled(health_db: Database) -> None:
    svc = _service(health_db, {"okay": _OkAdapter()}, enabled=False)
    await svc.tick()
    assert _rows(health_db) == []


async def test_tick_runs_when_enabled(health_db: Database) -> None:
    svc = _service(health_db, {"okay": _OkAdapter()}, enabled=True)
    await svc.tick()
    assert [row["status"] for row in _rows(health_db)] == ["ok"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(5, 60), ("30", 60), (0, 60), (999999, 86400), ("abc", 300), (300, 300)],
)
def test_interval_is_clamped(raw: object, expected: int) -> None:
    assert clamp_health_interval(raw) == expected
    svc = _service(SimpleNamespace(), {}, interval=raw)
    assert svc.interval_sec == expected


def test_env_interval_is_clamped_on_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UNIFIED_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("UNIFIED_ADMIN_PASSWORD", "clamp-test")
    monkeypatch.setenv("UNIFIED_HEALTH_CHECK_INTERVAL_SEC", "5")
    monkeypatch.setenv("UNIFIED_HEALTH_CHECK_ENABLED", "false")
    monkeypatch.setenv("GROK_REGISTER_EXECUTOR_URL", "")
    settings = load_settings()
    assert settings.health_check_interval_sec == 60
    assert settings.health_check_enabled is False

    monkeypatch.setenv("UNIFIED_HEALTH_CHECK_INTERVAL_SEC", "999999")
    assert load_settings().health_check_interval_sec == 86400


# ---- 三档结果 ----


async def test_run_once_stores_three_tiers(health_db: Database) -> None:
    svc = _service(
        health_db,
        {"okay": _OkAdapter(), "models": _ModelsAdapter(), "bare": _BareAdapter(), "bad": _BadAdapter("上游 500")},
    )
    await svc.run_once()
    statuses = {row["provider"]: row["status"] for row in _rows(health_db)}
    assert statuses == {"okay": "ok", "models": "ok", "bare": "unsupported", "bad": "failed"}


async def test_error_summary_is_redacted(health_db: Database) -> None:
    svc = _service(health_db, {"bad": _BadAdapter(f"invalid api key {LEAK}", secret=LEAK)})
    await svc.run_once("bad")
    row = _rows(health_db)[0]
    assert LEAK not in row["error"]
    assert "***" in row["error"]


async def test_detection_touches_only_history_table(health_db: Database) -> None:
    db_path = str(health_db.path)
    before = {name: _count(db_path, name) for name in ("request_logs", "billing_ledger", "providers")}
    svc = _service(health_db, {"okay": _OkAdapter()})
    await svc.run_once()
    after = {name: _count(db_path, name) for name in before}
    assert after == before


async def test_snapshot_reports_unchecked_provider(health_db: Database) -> None:
    svc = _service(health_db, {"okay": _OkAdapter(), "bare": _BareAdapter()})
    snap = svc.snapshot()
    assert snap["enabled"] is True
    assert {item["provider"]: item["status"] for item in snap["providers"]} == {
        "okay": None,
        "bare": None,
    }
    assert snap["history"] == []


# ---- 端点层（会话级控制面） ----


@pytest.fixture(scope="module")
def viewer_client(admin: httpx.Client, base_url: str):
    name = f"health-viewer-{uuid.uuid4().hex[:6]}"
    created = admin.post(
        "/api/users",
        json={"username": name, "password": VIEWER_PASSWORD, "role": "viewer"},
    )
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": VIEWER_PASSWORD})
    assert login.status_code == 200, login.text
    yield client
    client.close()


def test_health_get_is_readable_and_defaults_to_off(admin: httpx.Client, viewer_client: httpx.Client) -> None:
    for client in (admin, viewer_client):
        r = client.get("/api/providers/health")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is False, "默认必须是关闭的"
        ids = {item["id"] for item in admin.get("/api/providers").json()}
        assert {item["provider"] for item in body["providers"]} == ids
        assert all(item["status"] is None for item in body["providers"])
        assert body["history"] == []


def test_health_run_is_admin_only(viewer_client: httpx.Client) -> None:
    r = viewer_client.post("/api/providers/health/run")
    assert r.status_code == 403, r.text


def test_health_run_persists_and_changes_nothing_else(admin: httpx.Client, db_path: str) -> None:
    enabled_before = {item["id"]: item["enabled"] for item in admin.get("/api/providers").json()}
    logs_before = _count(db_path, "request_logs")
    ledger_before = _count(db_path, "billing_ledger")

    r = admin.post("/api/providers/health/run")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert {item["status"] for item in body["providers"]} <= {"ok", "failed", "unsupported"}
    assert len(body["history"]) == min(20, len(body["providers"]))
    assert all(item["checked_at"] for item in body["providers"])

    enabled_after = {item["id"]: item["enabled"] for item in admin.get("/api/providers").json()}
    assert enabled_after == enabled_before, "检测不允许改变任何 provider 的启用状态"
    assert _count(db_path, "request_logs") == logs_before
    assert _count(db_path, "billing_ledger") == ledger_before

    conn = sqlite3.connect(db_path)
    try:
        stored = conn.execute(
            "SELECT provider, status FROM provider_health_checks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert {pid: status for pid, status in stored} == {
        item["provider"]: item["status"] for item in body["providers"]
    }

    filtered = admin.get("/api/providers/health", params={"provider": "codebuddy"})
    assert filtered.status_code == 200
    assert [item["provider"] for item in filtered.json()["providers"]] == ["codebuddy"]

    unknown = admin.get("/api/providers/health", params={"provider": "nope"})
    assert unknown.status_code == 404

