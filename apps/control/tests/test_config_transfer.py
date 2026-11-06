"""配置打包导出/导入：脱敏、四类预检、事务回滚。

全部用 tmp_path 下的独立库，不碰 conftest 的共享库。
"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from app import config_bundle as cb
from app.config import Settings
from app.db import Database
from app.registry import ProviderRegistry

ACCOUNT_SQL = (
    "INSERT INTO accounts(file, provider, uid, nickname, enterprise_id, realm, expires_at, "
    "credits, note, disabled, meta_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _settings(tmp: Path) -> Settings:
    return Settings(
        data_dir=tmp,
        db_path=tmp / "unified.db",
        web_dir=tmp / "web",
        admin_username="admin",
        admin_password="test",
        session_ttl_sec=3600,
        cookie_secure=False,
        codebuddy_base="http://127.0.0.1:7899",
        codebuddy_api_key="",
        opencode_upstream_base="http://127.0.0.1:7899",
        grok_register_executor_url="",
        grok_register_max_concurrency=1,
        grok_register_max_workers=2,
        allow_anonymous_gateway=False,
        cors_origins=(),
        allow_private_upstreams=True,
    )


@pytest.fixture()
def env(tmp_path: Path):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    registry = ProviderRegistry(db, settings)
    conn = db.connect()
    for i in range(3):
        conn.execute(ACCOUNT_SQL, (f"acc{i}.json", "opencode", f"uid{i}", "", "", "cn", 0, None, "", 0, "{}", 1))
    conn.execute(
        "INSERT INTO provider_credentials(provider, label, base_url, secret, enabled, sort_order, note, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("opencode", "主账号", "", "sk-test-secret", 1, 0, "", 1, 1),
    )
    conn.commit()
    try:
        yield db, registry, conn
    finally:
        conn.close()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
        for name in ("providers", "accounts", "provider_credentials")
    }


def _known(registry: ProviderRegistry) -> set[str]:
    return set(registry._adapters)


def test_export_redacts_secrets_by_default(env):
    db, registry, conn = env
    plain = cb.build_bundle(conn, registry)
    assert plain["contains_secrets"] is False
    assert plain["warning"] == ""
    assert len(plain["providers"]) == len(registry._adapters)
    assert all("secret" not in item for item in plain["credentials"])
    assert all("api_key" not in item for item in plain["providers"])
    assert all("meta_json" not in item for item in plain["accounts"])
    assert plain["credentials"][0]["has_secret"] is True

    full = cb.build_bundle(conn, registry, include_secrets=True)
    assert full["contains_secrets"] is True
    assert "勿提交" in full["warning"]
    assert full["credentials"][0]["secret"] == "sk-test-secret"
    assert all("meta_json" in item for item in full["accounts"])


def test_merge_restores_deleted_accounts_and_provider_config(env):
    db, registry, conn = env
    bundle = cb.build_bundle(conn, registry)
    original_accounts = _counts(conn)["accounts"]

    conn.execute("DELETE FROM accounts WHERE file IN ('acc0.json','acc1.json')")
    conn.execute("UPDATE providers SET base_url = 'https://changed.example.com' WHERE id = 'opencode'")
    conn.commit()
    assert _counts(conn)["accounts"] == original_accounts - 2

    report = cb.apply_import(
        bundle, conn=conn, mode="merge", known_ids=_known(registry),
        clean_base=registry._clean_base, registry=registry,
    )
    assert report["totals"]["create"] == 2
    assert report["totals"]["fail"] == 0
    assert _counts(conn)["accounts"] == original_accounts
    assert conn.execute("SELECT base_url FROM providers WHERE id='opencode'").fetchone()[0] == ""


def test_dry_run_never_writes(env):
    db, registry, conn = env
    bundle = cb.build_bundle(conn, registry)
    conn.execute("DELETE FROM accounts WHERE file = 'acc0.json'")
    conn.commit()
    before = _counts(conn)

    report = cb.plan_import(bundle, conn=conn, mode="dry-run", known_ids=_known(registry))
    assert report["totals"]["create"] == 1
    assert _counts(conn) == before


def test_bad_field_rejects_whole_batch(env):
    db, registry, conn = env
    bundle = cb.build_bundle(conn, registry)
    bad = copy.deepcopy(bundle)
    del bad["accounts"][1]["uid"]           # accounts[2].uid
    bad["accounts"][0]["provider"] = "ghost"  # accounts[1].provider
    before = _counts(conn)

    with pytest.raises(cb.BundleRejected) as caught:
        cb.apply_import(bad, conn=conn, mode="overwrite", known_ids=_known(registry))
    issues = caught.value.report["issues"]
    paths = {(issue["path"], issue["column"]) for issue in issues if issue["level"] == "fail"}
    assert ("accounts[2].uid", "uid") in paths
    assert ("accounts[1].provider", "provider") in paths
    assert _counts(conn) == before


def test_transaction_rolls_back_on_midway_failure(env, monkeypatch):
    db, registry, conn = env
    bundle = cb.build_bundle(conn, registry)
    conn.execute("DELETE FROM accounts")
    conn.commit()
    before = _counts(conn)

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected failure")

    monkeypatch.setattr(cb, "_save_provider_config", boom)
    with pytest.raises(sqlite3.OperationalError):
        cb.apply_import(bundle, conn=conn, mode="merge", known_ids=_known(registry))
    assert _counts(conn) == before


def test_plain_bundle_overwrite_keeps_existing_secret(env):
    db, registry, conn = env
    bundle = cb.build_bundle(conn, registry)
    bundle["credentials"][0]["note"] = "改过备注"
    assert "secret" not in bundle["credentials"][0]

    report = cb.apply_import(bundle, conn=conn, mode="overwrite", known_ids=_known(registry))
    assert report["totals"]["overwrite"] >= 1
    row = conn.execute("SELECT secret, note FROM provider_credentials WHERE label='主账号'").fetchone()
    assert row[0] == "sk-test-secret"
    assert row[1] == "改过备注"


def test_http_export_and_plan_are_read_only(admin, db_path):
    r = admin.get("/api/config/export")
    assert r.status_code == 200
    assert r.headers["x-contains-secrets"] == "false"
    assert "attachment" in r.headers["content-disposition"]
    doc = r.json()
    assert doc["format"] == "unified-2api-config"
    assert doc["contains_secrets"] is False
    assert len(doc["providers"]) >= 14
    assert all("secret" not in item for item in doc["credentials"])

    before = {t: _counts_db(db_path)[t] for t in ("providers", "accounts", "provider_credentials")}
    plan = admin.post("/api/config/import/plan", json={"bundle": doc, "mode": "merge"})
    assert plan.status_code == 200
    body = plan.json()
    assert body["written"] == 0
    assert body["totals"]["fail"] == 0
    after = _counts_db(db_path)
    assert after == before

    secret = admin.get("/api/config/export", params={"include_secrets": True})
    assert secret.status_code == 200
    assert secret.headers["x-contains-secrets"] == "true"
    assert "勿提交" in secret.json()["warning"]


def test_http_import_rejects_bad_bundle(admin, db_path):
    doc = {
        "format": "unified-2api-config",
        "version": 1,
        "contains_secrets": False,
        "warning": "",
        "exported_at": 0,
        "providers": [],
        "accounts": [
            {"file": "bad_a.json", "provider": "ghost", "uid": "u1", "expires_at": 0,
             "disabled": False, "meta_json": "{}"},
            {"file": "bad_b.json", "provider": "opencode", "uid": "", "expires_at": 0,
             "disabled": False, "meta_json": "{}"},
        ],
        "credentials": [],
    }
    before = _counts_db(db_path)

    no_confirm = admin.post("/api/config/import", json={"bundle": doc, "mode": "merge", "confirm": False})
    assert no_confirm.status_code == 400

    r = admin.post("/api/config/import", json={"bundle": doc, "mode": "merge", "confirm": True})
    assert r.status_code == 400
    raw = json.dumps(r.json()["report"], ensure_ascii=False)
    assert "accounts[2].uid" in raw
    assert "accounts[1].provider" in raw
    assert _counts_db(db_path) == before


def _counts_db(path: str) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {
            name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in ("providers", "accounts", "provider_credentials")
        }
    finally:
        conn.close()
