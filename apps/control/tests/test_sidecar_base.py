"""sidecar base 环境变量链路：四家 sidecar 的默认地址统一从 env → Settings → registry 走。

设了环境变量时 /api/providers 的 base_url 随之改变；不设时用代码默认值。
"""

from __future__ import annotations

import pytest

from app.config import load_settings
from app.db import Database
from app.registry import ProviderRegistry

pytestmark = pytest.mark.providers

_ENVS = {
    "trae_sidecar": ("TRAE_SIDECAR_BASE", "http://127.0.0.1:7871"),
    "deepseek_sidecar": ("DEEPSEEK_SIDECAR_BASE", "http://127.0.0.1:5001"),
    "qoder_sidecar": ("QODER_SIDECAR_BASE", "http://127.0.0.1:7873"),
    "codar_sidecar": ("CODAR_SIDECAR_BASE", "http://127.0.0.1:8801"),
}


def _base_urls(monkeypatch: pytest.MonkeyPatch, tmp_path, overrides: dict[str, str]) -> dict[str, str]:
    for var, _ in _ENVS.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("UNIFIED_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UNIFIED_DB_PATH", str(tmp_path / "unified.db"))
    monkeypatch.setenv("UNIFIED_ADMIN_PASSWORD", "test")
    for var, value in overrides.items():
        monkeypatch.setenv(var, value)
    settings = load_settings()
    registry = ProviderRegistry(Database(settings.db_path), settings)
    return {item["id"]: item["base_url"] for item in registry.list_public()}


def test_sidecar_base_uses_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    bases = _base_urls(monkeypatch, tmp_path, {})
    for pid, (_, default) in _ENVS.items():
        assert bases[pid] == default, f"{pid} 默认值应为 {default}，实际 {bases[pid]}"


def test_sidecar_base_follows_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    overrides = {var: f"http://127.0.0.1:9{700 + i}" for i, (var, _) in enumerate(_ENVS.values())}
    bases = _base_urls(monkeypatch, tmp_path, overrides)
    for pid, (var, _) in _ENVS.items():
        assert bases[pid] == overrides[var], f"{pid} 应跟随 {var}，实际 {bases[pid]}"
