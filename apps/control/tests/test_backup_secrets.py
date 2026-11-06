"""本地归档里的上游密钥：不许明文，能还原，缺密钥时行为明确。

跑在 tmp_path 下，归档与恢复都走真实的 scripts/backup_core.py 子进程，不碰仓库 data/。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

from app.db import Database

REPO = Path(__file__).resolve().parents[3]
CORE = REPO / "scripts" / "backup_core.py"
PY = sys.executable

KEY_A = base64.b64encode(b"A" * 32).decode()
KEY_B = base64.b64encode(b"B" * 32).decode()
MARKER_CONFIG = "sk-live-provider-SECRET-MARKER-1"
MARKER_CRED = "sk-live-credential-SECRET-MARKER-2"
MARKER_UPSTREAM = "upstash-SECRET-MARKER-3"


def _seed(path: Path) -> None:
    """控制面建表 + 写入三类上游密钥（provider_config / upstream_config / 凭据行）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    Database(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            ("admin", "x", "admin", 0),
        )
        provider_config = {
            "upstream-a": {
                "api_key": MARKER_CONFIG,
                "api_key_updated_at": 1,
                "base_url": "https://upstream.example",
                "options": {"extra_keys": [MARKER_CONFIG + "-extra"]},
            }
        }
        upstream_config = {"url": "https://upstash.example", "token": MARKER_UPSTREAM}
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('provider_config', ?)",
            (json.dumps(provider_config, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('upstream_config', ?)",
            (json.dumps(upstream_config, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT INTO provider_credentials(provider, label, secret, created_at, updated_at) "
            "VALUES('upstream-a', 'main', ?, 0, 0)",
            (MARKER_CRED,),
        )
        conn.commit()
    finally:
        conn.close()


def _core(*args: str, key: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("CI", None)
    env.pop("UNIFIED_BACKUP_ENCRYPTION_KEY", None)
    if key is not None:
        env["UNIFIED_BACKUP_ENCRYPTION_KEY"] = key
    return subprocess.run(
        [PY, str(CORE), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )


def _backup(tmp_path: Path, key: str | None) -> tuple[Path, subprocess.CompletedProcess]:
    source = tmp_path / "src" / "unified.db"
    _seed(source)
    out_dir = tmp_path / "backups"
    done = _core("backup", "--db", str(source), "--out-dir", str(out_dir), key=key)
    assert done.returncode == 0, done.stderr
    archives = sorted(out_dir.glob("unified-backup-*.zip"))
    assert archives, done.stdout
    return archives[-1], done


def _members(archive: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(archive) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _manifest(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as zf:
        return json.loads(zf.read("manifest.json"))


def _archive_db(tmp_path: Path, archive: Path) -> Path:
    out = tmp_path / "extract" / "unified.db"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(_members(archive)["db/unified.db"])
    return out


def _query(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _copy_source(tmp_path: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tmp_path / "src" / "unified.db", target)


def _restore(tmp_path: Path, archive: Path, target: Path,
             key: str | None) -> subprocess.CompletedProcess:
    return _core(
        "restore", "--archive", str(archive), "--db", str(target),
        "--backups", str(tmp_path / "pre"), "--repo", str(REPO), key=key,
    )


def test_backup_with_key_has_no_plaintext_secret(tmp_path: Path) -> None:
    archive, done = _backup(tmp_path, KEY_A)
    manifest = _manifest(archive)
    assert manifest["secrets"]["policy"] == "encrypted"
    assert manifest["secrets"]["settings_fields"] == 3  # api_key + extra_keys + upstream token
    assert manifest["secrets"]["provider_credentials_rows"] == 1
    assert "secrets: policy=encrypted" in done.stdout

    for name, blob in _members(archive).items():
        for marker in (MARKER_CONFIG, MARKER_CRED, MARKER_UPSTREAM):
            assert marker.encode() not in blob, f"明文密钥出现在归档成员 {name}"

    db = _archive_db(tmp_path, archive)
    row = _query(db, "SELECT value FROM settings WHERE key='provider_config'")[0][0]
    assert MARKER_CONFIG not in row
    assert "u2a-enc:v1:" in row
    # 非密钥字段原样保留，provider 其余配置不丢
    assert "https://upstream.example" in row
    cred = _query(db, "SELECT secret FROM provider_credentials")[0][0]
    assert cred.startswith("u2a-enc:v1:")


def test_roundtrip_restores_usable_secrets(tmp_path: Path) -> None:
    archive, _ = _backup(tmp_path, KEY_A)
    target = tmp_path / "live" / "unified.db"
    _copy_source(tmp_path, target)

    done = _restore(tmp_path, archive, target, KEY_A)
    assert done.returncode == 0, done.stderr
    assert "secrets: restored fields=3 credentials=1" in done.stdout

    provider_config = json.loads(
        _query(target, "SELECT value FROM settings WHERE key='provider_config'")[0][0]
    )
    assert provider_config["upstream-a"]["api_key"] == MARKER_CONFIG
    assert provider_config["upstream-a"]["options"]["extra_keys"][0] == MARKER_CONFIG + "-extra"

    upstream_config = json.loads(
        _query(target, "SELECT value FROM settings WHERE key='upstream_config'")[0][0]
    )
    assert upstream_config["token"] == MARKER_UPSTREAM
    assert _query(target, "SELECT secret FROM provider_credentials")[0][0] == MARKER_CRED
    assert b"u2a-enc:v1:" not in target.read_bytes()


def test_backup_without_key_strips_and_warns(tmp_path: Path) -> None:
    archive, done = _backup(tmp_path, None)
    manifest = _manifest(archive)
    assert manifest["secrets"]["policy"] == "stripped"
    assert manifest["secrets"]["key_env"] == "UNIFIED_BACKUP_ENCRYPTION_KEY"
    assert "warning: 未配置 UNIFIED_BACKUP_ENCRYPTION_KEY" in done.stderr
    assert "secrets: policy=stripped" in done.stdout

    for name, blob in _members(archive).items():
        for marker in (MARKER_CONFIG, MARKER_CRED, MARKER_UPSTREAM):
            assert marker.encode() not in blob, f"未配密钥时归档成员 {name} 仍有明文密钥"

    target = tmp_path / "live" / "unified.db"
    _copy_source(tmp_path, target)
    restored = _restore(tmp_path, archive, target, None)
    assert restored.returncode == 0, restored.stderr
    assert "恢复后需在「服务商」/「凭据」页重新粘贴" in restored.stderr

    provider_config = json.loads(
        _query(target, "SELECT value FROM settings WHERE key='provider_config'")[0][0]
    )
    assert provider_config["upstream-a"]["api_key"] == ""
    assert _query(target, "SELECT secret FROM provider_credentials")[0][0] == ""


def test_restore_sealed_archive_without_key_fails_before_touching_target(tmp_path: Path) -> None:
    archive, _ = _backup(tmp_path, KEY_A)
    target = tmp_path / "live" / "unified.db"
    _copy_source(tmp_path, target)

    done = _restore(tmp_path, archive, target, None)
    assert done.returncode == 2, (done.returncode, done.stderr)
    assert "UNIFIED_BACKUP_ENCRYPTION_KEY" in done.stderr
    assert "加密封装" in done.stderr
    # 目标库必须原封不动：旧数据还在，密钥仍是原来的明文
    assert _query(target, "SELECT COUNT(*) FROM provider_credentials")[0][0] == 1
    assert _query(target, "SELECT COUNT(*) FROM settings")[0][0] >= 2
    provider_config = json.loads(
        _query(target, "SELECT value FROM settings WHERE key='provider_config'")[0][0]
    )
    assert provider_config["upstream-a"]["api_key"] == MARKER_CONFIG


def test_restore_sealed_archive_with_wrong_key_fails(tmp_path: Path) -> None:
    archive, _ = _backup(tmp_path, KEY_A)
    target = tmp_path / "live" / "unified.db"
    _copy_source(tmp_path, target)

    done = _restore(tmp_path, archive, target, KEY_B)
    assert done.returncode == 2, (done.returncode, done.stderr)
    assert "解不开" in done.stderr
    provider_config = json.loads(
        _query(target, "SELECT value FROM settings WHERE key='provider_config'")[0][0]
    )
    assert provider_config["upstream-a"]["api_key"] == MARKER_CONFIG
