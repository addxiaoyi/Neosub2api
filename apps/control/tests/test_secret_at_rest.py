"""凭据落库加密与会话 token 哈希的回归用例。

这些用例都直接操作临时库，不依赖会话级控制面，所以不会污染 928 用例的既有计数口径。
"""

from __future__ import annotations

import base64
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import load_settings
from app.db import Database, get_json_setting, set_json_setting
from app.registry import ProviderRegistry
from app.routers import security as security_router
from app.security import admin_user, hash_token, migrate_session_tokens
from app.services import secret_store
from app.services.backup_crypto import is_sealed


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "unified.db")


@pytest.fixture
def crypto_env(monkeypatch):
    monkeypatch.setenv(
        "UNIFIED_BACKUP_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode()
    )
    secret_store.reset_warnings()
    yield
    secret_store.reset_warnings()


@pytest.fixture
def plain_env(monkeypatch):
    monkeypatch.delenv("UNIFIED_BACKUP_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("UNIFIED_SECRET_AT_REST_REQUIRED", raising=False)
    secret_store.reset_warnings()
    yield
    secret_store.reset_warnings()


def _insert_credential(db, secret: str, provider: str = "codebuddy") -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO provider_credentials(provider, label, base_url, secret, enabled, sort_order, "
            "note, created_at, updated_at) VALUES(?,?,?,?,1,0,'',0,0)",
            (provider, "", "", secret),
        )
        conn.commit()
    finally:
        conn.close()


def _read_credential(db) -> str:
    conn = db.connect()
    try:
        return str(conn.execute("SELECT secret FROM provider_credentials").fetchone()["secret"])
    finally:
        conn.close()


def test_seal_roundtrip(crypto_env):
    assert secret_store.seal("") == ""
    assert secret_store.seal(None) == ""
    plain = "sk-测试-密钥-🚀-" + "x" * 2000
    sealed = secret_store.seal(plain)
    assert is_sealed(sealed) and plain not in sealed
    assert secret_store.unseal(sealed) == plain
    # 幂等：重复封装不会叠套
    assert secret_store.seal(sealed) == sealed
    # 明文直通，旧数据无感
    assert secret_store.unseal("legacy-plain") == "legacy-plain"


def test_seal_missing_key_keeps_plaintext(plain_env):
    assert secret_store.seal("sk-live") == "sk-live"


def test_unseal_sealed_without_key(plain_env, crypto_env, monkeypatch):
    sealed = secret_store.seal("sk-live")
    monkeypatch.delenv("UNIFIED_BACKUP_ENCRYPTION_KEY", raising=False)
    secret_store.reset_warnings()
    plain, err = secret_store.read_secret(sealed)
    assert plain == "" and err == "key_missing"
    with pytest.raises(secret_store.SecretUnavailable):
        secret_store.unseal(sealed)


def test_wrong_key_reports_decrypt_failed(crypto_env, monkeypatch):
    sealed = secret_store.seal("sk-live")
    monkeypatch.setenv(
        "UNIFIED_BACKUP_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode()
    )
    plain, err = secret_store.read_secret(sealed)
    assert plain == "" and err in {"decrypt_failed", "decode_failed"}


def test_migrate_seals_rows_and_is_idempotent(crypto_env, db):
    _insert_credential(db, "sk-live-1")
    conn = db.connect()
    set_json_setting(conn, "provider_config", {"codebuddy": {"api_key": "sk-cfg-1", "options": {}}})
    conn.close()

    counts = secret_store.migrate(db)
    assert counts["credentials"] == 1 and counts["api_keys"] == 1
    assert is_sealed(_read_credential(db))
    stored = get_json_setting(db.connect(), "provider_config", {})
    assert is_sealed(stored["codebuddy"]["api_key"])
    # options 不在本期范围，必须原样保留
    assert stored["codebuddy"]["options"] == {}

    assert secret_store.migrate(db) == {"credentials": 0, "api_keys": 0, "undecryptable": 0}
    assert secret_store.read_secret(_read_credential(db))[0] == "sk-live-1"


def test_registry_reads_sealed_as_plaintext(crypto_env, db):
    _insert_credential(db, "sk-live-1")
    secret_store.migrate(db)
    registry = ProviderRegistry(db, load_settings())
    rows = registry.credentials()
    assert [row["secret"] for row in rows] == ["sk-live-1"]
    assert registry.credential(int(rows[0]["id"]))["secret"] == "sk-live-1"
    # 对外视图不能出现密文
    view = registry.resolved("codebuddy")
    masked = [row["secret_masked"] for row in view.public_credentials()]
    assert all("u2a-enc" not in text for text in masked)


def test_update_seals_api_key_and_keeps_others_sealed(crypto_env, db):
    registry = ProviderRegistry(db, load_settings())
    conn = db.connect()
    set_json_setting(
        conn,
        "provider_config",
        {"opencode": {"api_key": secret_store.seal("sk-other")}},
    )
    conn.close()

    registry.update("codebuddy", api_key="sk-new")
    stored = get_json_setting(db.connect(), "provider_config", {})
    assert is_sealed(stored["codebuddy"]["api_key"])
    assert secret_store.read_secret(stored["codebuddy"]["api_key"])[0] == "sk-new"
    # 整表回写不能把别家的密文洗成明文
    assert is_sealed(stored["opencode"]["api_key"])
    assert secret_store.read_secret(stored["opencode"]["api_key"])[0] == "sk-other"


def test_bootstrap_required_without_key_refuses(plain_env, monkeypatch, db):
    monkeypatch.setenv("UNIFIED_SECRET_AT_REST_REQUIRED", "1")
    with pytest.raises(RuntimeError):
        secret_store.bootstrap(db)


def test_status_counts(plain_env, db):
    _insert_credential(db, "sk-plain")
    summary = secret_store.status(db)
    assert summary["mode"] == "plaintext"
    assert summary["credentials_plaintext"] == 1
    assert summary["sessions_plaintext"] == 0


def test_status_endpoint_exposes_counts_without_secrets(plain_env, db):
    _insert_credential(db, "sk-plain")

    app = FastAPI()
    app.state.db = db
    app.include_router(security_router.router)
    app.dependency_overrides[admin_user] = lambda: {"username": "admin", "role": "admin"}
    body = TestClient(app).get("/api/security/secret-at-rest").json()

    assert body["mode"] == "plaintext"
    assert body["credentials_plaintext"] == 1
    assert "sk-plain" not in json.dumps(body)


def test_session_tokens_are_hashed(db):
    conn = db.connect()
    raw = "raw-cookie-token-xyz"
    conn.execute(
        "INSERT INTO sessions(token, username, created_at, expires_at) VALUES(?,?,?,?)",
        (raw, "admin", 0, 10 ** 9),
    )
    conn.commit()

    assert migrate_session_tokens(conn) == 1
    stored = str(conn.execute("SELECT token FROM sessions").fetchone()["token"])
    assert stored == hash_token(raw) and stored.startswith("s256:")
    # 再跑一次必须幂等
    assert migrate_session_tokens(conn) == 0
    assert conn.execute("SELECT token FROM sessions").fetchone()["token"] == hash_token(raw)
    conn.close()
