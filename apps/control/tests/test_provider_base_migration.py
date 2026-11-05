"""providers 启动迁移：只迁仍是出厂默认值的 base_url，用户改过的 URL 一字不动。"""

from __future__ import annotations

import sys
from pathlib import Path

CONTROL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROL))

from app.config import load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.registry import ProviderRegistry  # noqa: E402

# old→new 映射：与 db.py 里四条 UPDATE 的全等 WHERE 一一对应
OLD_TO_NEW = {
    "codebuddy": ("http://127.0.0.1:7863", "http://127.0.0.1:7864"),
    "opencode": ("http://127.0.0.1:7869", "https://opencode.ai/zen"),
    "sub2api": ("http://127.0.0.1:7870", ""),
    "grok-register": ("http://127.0.0.1:7866", ""),
}


def _boot(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("UNIFIED_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UNIFIED_DB_PATH", str(tmp_path / "unified.db"))
    settings = load_settings()
    # providers 行由 registry 首次启动时播种，播种后再改值模拟存量库
    ProviderRegistry(Database(settings.db_path), settings)
    return settings


def _set_base(settings, provider: str, base_url: str) -> None:
    conn = Database(settings.db_path).connect()
    try:
        conn.execute("UPDATE providers SET base_url = ? WHERE id = ?", (base_url, provider))
        conn.commit()
    finally:
        conn.close()


def _get_base(settings, provider: str) -> str:
    conn = Database(settings.db_path).connect()
    try:
        row = conn.execute("SELECT base_url FROM providers WHERE id = ?", (provider,)).fetchone()
        return row["base_url"]
    finally:
        conn.close()


def test_custom_base_url_survives_two_migrations(monkeypatch, tmp_path):
    """自定义域名+端口+path：连续跑两次迁移也一字未变。"""
    settings = _boot(monkeypatch, tmp_path)
    custom = "https://gw.example.com:9999/custom/v1"
    _set_base(settings, "codebuddy", custom)

    Database(settings.db_path)
    Database(settings.db_path)

    assert _get_base(settings, "codebuddy") == custom


def test_same_port_different_host_not_migrated(monkeypatch, tmp_path):
    """同 7863 端口但不同 host：端口子串匹配的旧写法会误伤，全等匹配不会。"""
    settings = _boot(monkeypatch, tmp_path)
    custom = "https://gw.example.com:7863/v1"
    _set_base(settings, "codebuddy", custom)

    Database(settings.db_path)

    assert _get_base(settings, "codebuddy") == custom


def test_trailing_slash_variant_not_migrated(monkeypatch, tmp_path):
    """带尾斜杠的自定义 URL：SQLite 的 = 是严格字节比较，差一个字符就不该命中。"""
    settings = _boot(monkeypatch, tmp_path)
    custom = "https://gw.example.com:7863/v1/"
    _set_base(settings, "codebuddy", custom)

    Database(settings.db_path)
    Database(settings.db_path)

    assert _get_base(settings, "codebuddy") == custom


def test_factory_default_migrated_to_new_default(monkeypatch, tmp_path):
    """真正的出厂旧默认值要迁成新默认——证明全等匹配没把功能改死。"""
    settings = _boot(monkeypatch, tmp_path)
    for provider, (old, _new) in OLD_TO_NEW.items():
        _set_base(settings, provider, old)

    Database(settings.db_path)

    for provider, (_old, new) in OLD_TO_NEW.items():
        assert _get_base(settings, provider) == new, provider


def test_new_default_idempotent(monkeypatch, tmp_path):
    """已是新默认值的行：连续迁移两次不重复变更。"""
    settings = _boot(monkeypatch, tmp_path)
    for provider, (_old, new) in OLD_TO_NEW.items():
        _set_base(settings, provider, new)

    Database(settings.db_path)
    Database(settings.db_path)

    for provider, (_old, new) in OLD_TO_NEW.items():
        assert _get_base(settings, provider) == new, provider
