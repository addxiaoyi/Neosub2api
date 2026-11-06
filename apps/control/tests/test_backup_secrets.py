"""本地归档里的上游密钥：不许明文，恢复后保持封装态，缺密钥时行为明确。

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
from app.services import backup_crypto

REPO = Path(__file__).resolve().parents[3]
CORE = REPO / "scripts" / "backup_core.py"
PY = sys.executable

KEY_A = base64.b64encode(b"A" * 32).decode()
KEY_B = base64.b64encode(b"B" * 32).decode()
MARKER_CONFIG = "sk-live-provider-SECRET-MARKER-1"
MARKER_CRED = "sk-live-credential-SECRET-MARKER-2"
MARKER_UPSTREAM = "upstash-SECRET-MARKER-3"


def _seal(text: str, key_b64: str) -> str:
    """模拟另一路「落库加密」已经写入的封装态，备份侧只能识别、不能重封。"""
    return backup_crypto.seal_text(text, base64.b64decode(key_b64))


def _seed(path: Path, *, preseed: tuple[str, ...] = ()) -> None:
    """控制面建表 + 写入三类上游密钥（provider_config / upstream_config / 凭据行）。

    `preseed` 点名哪些位置先以 `u2a-enc:v1:` 落库，用来覆盖「库里已整体封存」的场景。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    Database(path)
    config_key = _seal(MARKER_CONFIG, KEY_A) if "provider_config" in preseed else MARKER_CONFIG
    config_extra = (
        _seal(MARKER_CONFIG + "-extra", KEY_A) if "provider_config" in preseed
        else MARKER_CONFIG + "-extra"
    )
    upstream_key = _seal(MARKER_UPSTREAM, KEY_A) if "upstream_config" in preseed else MARKER_UPSTREAM
    cred_key = _seal(MARKER_CRED, KEY_A) if "credential" in preseed else MARKER_CRED
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            ("admin", "x", "admin", 0),
        )
        provider_config = {
            "upstream-a": {
                "api_key": config_key,
                "api_key_updated_at": 1,
                "base_url": "https://upstream.example",
                "options": {"extra_keys": [config_extra]},
            }
        }
        upstream_config = {"url": "https://upstash.example", "token": upstream_key}
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
            (cred_key,),
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


def _backup(tmp_path: Path, key: str | None,
            *, preseed: tuple[str, ...] = ()) -> tuple[Path, subprocess.CompletedProcess]:
    source = tmp_path / "src" / "unified.db"
    _seed(source, preseed=preseed)
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
    secrets = manifest["secrets"]
    assert secrets["policy"] == "encrypted"
    assert secrets["already_sealed"] == {"settings_fields": 0, "provider_credentials_rows": 0}
    assert secrets["sealed_now"] == {"settings_fields": 3, "provider_credentials_rows": 1}
    assert secrets["not_sealed"] == {"settings_fields": 0, "provider_credentials_rows": 0}
    # 旧字段仍表示「本次封装」
    assert secrets["settings_fields"] == 3  # api_key + extra_keys + upstream token
    assert secrets["provider_credentials_rows"] == 1
    assert "secrets: policy=encrypted" in done.stdout
    assert "sealed_now=fields=3/credentials=1" in done.stdout

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


def test_manifest_counts_already_sealed_rows(tmp_path: Path) -> None:
    """库里已封存的行要算「受保护」；policy 不能再误报成 none。"""
    archive, done = _backup(tmp_path, KEY_A, preseed=("provider_config", "credential"))
    secrets = _manifest(archive)["secrets"]
    assert secrets["policy"] == "encrypted"
    assert secrets["already_sealed"] == {"settings_fields": 2, "provider_credentials_rows": 1}
    assert secrets["sealed_now"] == {"settings_fields": 1, "provider_credentials_rows": 0}
    assert secrets["not_sealed"] == {"settings_fields": 0, "provider_credentials_rows": 0}
    assert secrets["settings_fields"] == 1
    assert secrets["provider_credentials_rows"] == 0
    assert "already_sealed=fields=2/credentials=1" in done.stdout

    db = _archive_db(tmp_path, archive)
    # 已封存字段不能被再封一层，也不能被解封
    api_key = json.loads(
        _query(db, "SELECT value FROM settings WHERE key='provider_config'")[0][0]
    )["upstream-a"]["api_key"]
    assert api_key.startswith(backup_crypto.SEAL_PREFIX)
    assert backup_crypto.unseal_text(api_key, base64.b64decode(KEY_A)) == MARKER_CONFIG


def test_restore_keeps_sealed_fields_for_app_read(tmp_path: Path) -> None:
    archive, _ = _backup(tmp_path, KEY_A)
    target = tmp_path / "live" / "unified.db"
    _copy_source(tmp_path, target)

    done = _restore(tmp_path, archive, target, KEY_A)
    assert done.returncode == 0, done.stderr
    assert "secrets: sealed fields=3 credentials=1" in done.stdout
    assert "已保留封装态" in done.stdout

    # 恢复阶段不解封：落库的仍是 u2a-enc:v1:，解封留给应用读取路径
    key = base64.b64decode(KEY_A)
    provider_config = json.loads(
        _query(target, "SELECT value FROM settings WHERE key='provider_config'")[0][0]
    )
    api_key = provider_config["upstream-a"]["api_key"]
    assert api_key.startswith(backup_crypto.SEAL_PREFIX)
    assert backup_crypto.unseal_text(api_key, key) == MARKER_CONFIG
    extra = provider_config["upstream-a"]["options"]["extra_keys"][0]
    assert extra.startswith(backup_crypto.SEAL_PREFIX)
    assert backup_crypto.unseal_text(extra, key) == MARKER_CONFIG + "-extra"

    upstream_token = json.loads(
        _query(target, "SELECT value FROM settings WHERE key='upstream_config'")[0][0]
    )["token"]
    assert upstream_token.startswith(backup_crypto.SEAL_PREFIX)
    assert backup_crypto.unseal_text(upstream_token, key) == MARKER_UPSTREAM

    cred = _query(target, "SELECT secret FROM provider_credentials")[0][0]
    assert cred.startswith(backup_crypto.SEAL_PREFIX)
    assert backup_crypto.unseal_text(cred, key) == MARKER_CRED

    blob = target.read_bytes()
    for marker in (MARKER_CONFIG, MARKER_CRED, MARKER_UPSTREAM):
        assert marker.encode() not in blob, "恢复后库里出现了明文密钥"


def test_backup_without_key_strips_and_warns(tmp_path: Path) -> None:
    archive, done = _backup(tmp_path, None)
    manifest = _manifest(archive)
    secrets = manifest["secrets"]
    assert secrets["policy"] == "stripped"
    assert secrets["key_env"] == "UNIFIED_BACKUP_ENCRYPTION_KEY"
    assert secrets["already_sealed"] == {"settings_fields": 0, "provider_credentials_rows": 0}
    assert secrets["sealed_now"] == {"settings_fields": 0, "provider_credentials_rows": 0}
    assert secrets["not_sealed"] == {"settings_fields": 3, "provider_credentials_rows": 1}
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


def test_restore_already_sealed_archive_without_key_is_not_silently_downgraded(tmp_path: Path) -> None:
    """库已整体封存、备份时又没配密钥：恢复照样拒绝，不能把密文变成明文或空值。"""
    archive, _ = _backup(
        tmp_path, None, preseed=("provider_config", "upstream_config", "credential"),
    )
    secrets = _manifest(archive)["secrets"]
    assert secrets["policy"] == "encrypted"
    assert secrets["already_sealed"] == {"settings_fields": 3, "provider_credentials_rows": 1}
    assert secrets["not_sealed"] == {"settings_fields": 0, "provider_credentials_rows": 0}

    target = tmp_path / "live" / "unified.db"
    _copy_source(tmp_path, target)
    before = target.read_bytes()

    done = _restore(tmp_path, archive, target, None)
    assert done.returncode == 2, (done.returncode, done.stderr)
    assert "加密封装" in done.stderr
    assert target.read_bytes() == before, "恢复失败却改动了目标库"


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
