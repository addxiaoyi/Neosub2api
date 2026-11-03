"""存量库里的历史默认地址：只告警、不改写，且不误报用户手填的地址。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

CONTROL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROL))

from app.config import load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.registry import ProviderRegistry  # noqa: E402


def _boot(monkeypatch, tmp_path: Path, *, base_url: str = "") -> tuple[object, ProviderRegistry]:
    monkeypatch.setenv("UNIFIED_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UNIFIED_DB_PATH", str(tmp_path / "unified.db"))
    monkeypatch.delenv("DEEPSEEK_SIDECAR_BASE", raising=False)
    settings = load_settings()
    registry = ProviderRegistry(Database(settings.db_path), settings)
    if base_url:
        conn = Database(settings.db_path).connect()
        try:
            conn.execute("UPDATE providers SET base_url = ? WHERE id = 'deepseek_sidecar'", (base_url,))
            conn.commit()
        finally:
            conn.close()
    return settings, registry


def _add_cred(
    settings,
    base_url: str,
    *,
    label: str = "主账号",
    enabled: int = 1,
    provider: str = "deepseek_sidecar",
) -> int:
    conn = Database(settings.db_path).connect()
    try:
        cur = conn.execute(
            "INSERT INTO provider_credentials(provider, label, base_url, secret, enabled, sort_order, "
            "note, created_at, updated_at) VALUES(?, ?, ?, '', ?, 0, '', 1, 1)",
            (provider, label, base_url, enabled),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _item(registry: ProviderRegistry) -> dict:
    return next(row for row in registry.list_public() if row["id"] == "deepseek_sidecar")


def test_legacy_7872_warns_but_keeps_value(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    settings, _ = _boot(monkeypatch, tmp_path)
    conn = Database(settings.db_path).connect()
    try:
        conn.execute("UPDATE providers SET base_url = ? WHERE id = 'deepseek_sidecar'",
                     ("http://127.0.0.1:7872",))
        conn.commit()
    finally:
        conn.close()

    restarted = ProviderRegistry(Database(settings.db_path), settings)
    item = _item(restarted)
    assert item["base_url"] == "http://127.0.0.1:7872", "只告警，绝不自动改写用户配置"
    assert "7872" in item["base_url_warning"]
    assert "5001" in item["base_url_warning"]
    assert item["base_url_source"] == "db"

    warned = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert any("7872" in message and "deepseek_sidecar" in message for message in warned)


def test_custom_base_is_not_flagged(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    settings, _ = _boot(monkeypatch, tmp_path, base_url="http://127.0.0.1:9000")
    restarted = ProviderRegistry(Database(settings.db_path), settings)
    item = _item(restarted)
    assert item["base_url"] == "http://127.0.0.1:9000"
    assert item["base_url_warning"] == ""
    assert not any("7872" in record.getMessage() for record in caplog.records)


def test_credential_legacy_7872_is_flagged(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    settings, _ = _boot(monkeypatch, tmp_path)
    cred_id = _add_cred(settings, "http://127.0.0.1:7872", label="主账号")

    restarted = ProviderRegistry(Database(settings.db_path), settings)
    item = _item(restarted)
    # provider 级字段仍是字符串且为空：凭据残留不混进 provider 级提示
    assert item["base_url_warning"] == ""
    warnings = item["credential_base_warnings"]
    assert len(warnings) == 1
    entry = warnings[0]
    assert entry["provider"] == "deepseek_sidecar"
    assert entry["id"] == cred_id
    assert entry["label"] == "主账号"
    assert entry["base_url"] == "http://127.0.0.1:7872"
    assert entry["suggested_base_url"] == "http://127.0.0.1:5001"
    assert f"#{cred_id}" in entry["message"]
    assert "主账号" in entry["message"]
    assert "7872" in entry["message"] and "5001" in entry["message"]
    # 只告警，凭据行原值不动
    assert item["credentials"][0]["base_url"] == "http://127.0.0.1:7872"

    logged = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert any(f"#{cred_id}" in message and "主账号" in message for message in logged)


def test_credential_custom_base_is_not_flagged(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    settings, _ = _boot(monkeypatch, tmp_path)
    _add_cred(settings, "http://127.0.0.1:9000", label="自建地址")

    restarted = ProviderRegistry(Database(settings.db_path), settings)
    item = _item(restarted)
    assert item["base_url"] == "http://127.0.0.1:9000"
    assert item["base_url_warning"] == ""
    assert item["credential_base_warnings"] == []
    assert item["credentials"][0]["base_url"] == "http://127.0.0.1:9000"
    assert not any("7872" in record.getMessage() for record in caplog.records)


def test_disabled_credential_is_silent_until_enabled(monkeypatch, tmp_path, caplog):
    """停用的凭据不参与解析也不告警；重新启用后接口字段立刻可见（启动日志要等下次重启）。"""
    caplog.set_level(logging.WARNING)
    settings, _ = _boot(monkeypatch, tmp_path)
    cred_id = _add_cred(settings, "http://127.0.0.1:7872", label="备用号", enabled=0)

    restarted = ProviderRegistry(Database(settings.db_path), settings)
    item = _item(restarted)
    assert item["credential_base_warnings"] == []
    assert not any("7872" in record.getMessage() for record in caplog.records)

    conn = Database(settings.db_path).connect()
    try:
        conn.execute("UPDATE provider_credentials SET enabled = 1 WHERE id = ?", (cred_id,))
        conn.commit()
    finally:
        conn.close()

    item = _item(restarted)
    assert len(item["credential_base_warnings"]) == 1
    assert item["credential_base_warnings"][0]["id"] == cred_id
