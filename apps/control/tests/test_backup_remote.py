"""远端备份的端到端单测：本地假 S3（ThreadingHTTPServer）+ 真加密。

覆盖：密文上传与旁注、503 重试、403 阻塞、失败不动本地、缺密钥拒绝、
响应/日志不泄密钥、保留份数轮转；以及反向链路（restore-remote、远端删除、
探活状态）的往返与全部拒绝分支。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import sqlite3
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app import security
from app.db import Database
from app.routers import backup as backup_mod
from app.routers import backup_remote as remote_mod
from app.services import backup_crypto
from app.services.backup_remote import BackupRemoteService, remote_key
from app.services.ops_lock import acquire, release
from tests.fake_s3_server import BUCKET, FakeS3

SECRET_VALUE = "SUPERSECRET-DO-NOT-LEAK"
ACCESS_KEY = "AKIATESTKEY12345"
PREFIX = "test-prefix"
PLAINTEXT_MARKER = b"PROVIDER-SECRET-PLAINTEXT-MARKER"


@pytest.fixture()
def fake_s3():
    server = FakeS3().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def remote(tmp_path, fake_s3, monkeypatch):
    monkeypatch.setenv(
        "UNIFIED_BACKUP_ENCRYPTION_KEY", base64.b64encode(bytes(range(32))).decode()
    )
    monkeypatch.setenv("UNIFIED_BACKUP_S3_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("UNIFIED_BACKUP_S3_SECRET_ACCESS_KEY", SECRET_VALUE)
    settings = SimpleNamespace(
        db_path=str(tmp_path / "data" / "unified.db"),
        backup_remote_enabled=True,
        backup_remote_interval_sec=3600,
        backup_remote_auto_upload=False,
        backup_remote_keep=14,
    )
    db = Database(Path(settings.db_path))
    service = BackupRemoteService(settings, db)
    service.save_config(
        {
            "endpoint": fake_s3.endpoint,
            "region": "us-east-1",
            "bucket": BUCKET,
            "path_style": True,
            "prefix": PREFIX,
        }
    )
    archive_dir = tmp_path / "data" / "backups"
    archive_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(service=service, settings=settings, db=db, dir=archive_dir, fake=fake_s3)


def _make_archive(directory: Path, name: str = "unified-backup-20261105-120000.zip") -> Path:
    target = directory / name
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("provider_credentials.json", json.dumps({"secret": PLAINTEXT_MARKER.decode()}))
    target.write_bytes(buffer.getvalue())
    return target


def test_upload_encrypts_zip_and_sidecar(remote):
    archive = _make_archive(remote.dir)
    result = remote.service.upload(retry_delays=(), now=1000)
    assert result["ok"] is True, result
    assert result["attempts"] == 1
    key = remote_key(remote.service.config(), archive.name)
    assert key in remote.fake.objects
    payload, meta = remote.fake.objects[key]
    sidecar = remote.fake.objects[key + ".sha256"][0]

    digest = hashlib.sha256(payload).hexdigest()
    assert meta["sha256"] == digest
    assert sidecar.split()[0].decode() == digest
    assert len(payload) > 28  # nonce + 密文 + tag
    assert payload[:12] != archive.read_bytes()[:12]  # 文件头是 nonce，不是明文

    plain = backup_crypto.decrypt_bytes(payload, bytes(range(32)))
    assert plain == archive.read_bytes()
    assert PLAINTEXT_MARKER not in b"".join(remote.fake.bodies)

    state = remote.service.state()
    assert state["last_uploaded"] == key
    assert state["auth_blocked"] is False
    assert state["last_ok_at"] == 1000


def test_503_is_retried_then_succeeds(remote):
    _make_archive(remote.dir)
    remote.fake.fail_puts = 1
    result = remote.service.upload(retry_delays=(0, 0), now=1000)
    assert result["ok"] is True, result
    assert result["attempts"] == 2


def test_403_marks_auth_blocked_without_leaking_secret(remote, caplog):
    _make_archive(remote.dir)
    remote.fake.forced_error = (403, "AccessDenied", f"denied for {SECRET_VALUE}")
    with caplog.at_level(logging.INFO):
        result = remote.service.upload(retry_delays=(0, 0), now=1000)
    assert result["ok"] is False
    assert result["reason"] == "auth_rejected"
    assert SECRET_VALUE not in result["message"]
    state = remote.service.state()
    assert state["auth_blocked"] is True
    assert state["next_retry_at"] == 0
    assert SECRET_VALUE not in json.dumps(remote.service.public_config())
    assert SECRET_VALUE not in caplog.text


def test_clock_skew_is_translated_to_host_clock_message(remote):
    _make_archive(remote.dir)
    remote.fake.forced_error = (403, "RequestTimeTooSkewed", "the difference between the request time")
    result = remote.service.upload(retry_delays=(), now=1000)
    assert result["ok"] is False
    assert "宿主时钟偏差超过 15 分钟" in result["message"]
    assert remote.service.state()["auth_blocked"] is True


def test_upload_failure_keeps_local_archive_and_cleans_temp(remote):
    archive = _make_archive(remote.dir)
    remote.fake.forced_error = (500, "InternalError", "boom")
    result = remote.service.upload(retry_delays=(0, 0), now=1000)
    assert result["ok"] is False
    assert result["reason"] == "retryable"
    assert archive.is_file()
    assert archive.read_bytes()  # 本地归档原样保留
    leftovers = [entry.name for entry in remote.dir.iterdir() if entry.name.startswith(".backup-remote-")]
    assert leftovers == []
    state = remote.service.state()
    assert state["next_retry_at"] == 1000 + 3600


def test_missing_encryption_key_refuses_to_upload(remote, monkeypatch):
    monkeypatch.delenv("UNIFIED_BACKUP_ENCRYPTION_KEY", raising=False)
    _make_archive(remote.dir)
    result = remote.service.upload(retry_delays=(), now=1000)
    assert result["ok"] is False
    assert result["reason"] == "key_missing"
    assert "UNIFIED_BACKUP_ENCRYPTION_KEY" in result["message"]
    assert remote.fake.requests == []  # 一个外呼都不发
    state = remote.service.state()
    assert state["auth_blocked"] is True
    assert remote.service.public_config()["encryption_key_configured"] is False


def test_public_config_masks_access_key_and_hides_secret(remote):
    public = remote.service.public_config()
    assert public["configured"] is True
    assert public["access_key_id_masked"] == "AKIA****2345"
    assert public["credential_source"] == "env"
    assert SECRET_VALUE not in json.dumps(public)
    assert "secret_key" not in public
    # 加密实现必须可从状态里看出来（lib=装了 cryptography，stdlib=回落）
    assert public["crypto_backend"]["implementation"] in {"lib", "stdlib"}


def test_configured_requires_both_env_credentials(remote, monkeypatch):
    assert remote.service.configured() is True
    monkeypatch.delenv("UNIFIED_BACKUP_S3_ACCESS_KEY_ID")
    assert remote.service.configured() is False
    monkeypatch.delenv("UNIFIED_BACKUP_S3_SECRET_ACCESS_KEY", raising=False)
    assert remote.service.configured() is False
    monkeypatch.setenv("UNIFIED_BACKUP_S3_ACCESS_KEY_ID", ACCESS_KEY)
    assert remote.service.configured() is False  # 只配一半不算配好
    monkeypatch.setenv("UNIFIED_BACKUP_S3_SECRET_ACCESS_KEY", SECRET_VALUE)
    assert remote.service.configured() is True


def test_credentials_are_never_persisted(remote):
    from app.db import get_json_setting

    conn = remote.db.connect()
    try:
        stored = get_json_setting(conn, "backup_remote_config", {})
    finally:
        conn.close()
    assert "access_key" not in stored and "secret_key" not in stored
    assert ACCESS_KEY not in json.dumps(stored) and SECRET_VALUE not in json.dumps(stored)


def test_save_config_rejects_credentials_via_api(remote):
    with pytest.raises(Exception) as excinfo:
        remote.service.save_config({"access_key": ACCESS_KEY, "secret_key": SECRET_VALUE})
    assert "环境变量" in str(excinfo.value)


# ---- 双实现互通 / 格式不变 / nonce 策略 ----

HAS_LIB = backup_crypto.lib_available()
KEY = bytes(range(32))
# 2026-11-05 由纯标准库实现现场生成的样本密文；任何实现都必须解出这段原文
FIXED_PLAIN = b"unified-2api backup format vector v1\n" * 3
FIXED_PAYLOAD = base64.b64decode(
    "QEYHdxjxdJowl7ptqLWoL6EFUNcd1RNfF7ekYP6bs7CIgyyS8rjeuT43FikO2lTz4oWqsh1QRVK"
    "TM8u6IrtPkp5ew4E/22+TzzlQOnFp7LQEfqnh+pk7g8ATHlNQkTllM3Lp4VjtoJn2V82ORUDKh"
    "hyOYlg/HojctXKQVGLGPmkuGbQCBs/CIDZIrg=="
)


def _roundtrip(tmp_path: Path, plain: bytes, encrypt: str, decrypt: str) -> bytes:
    src = tmp_path / f"plain-{encrypt}.bin"
    dst = tmp_path / f"plain-{encrypt}.enc"
    src.write_bytes(plain)
    info = backup_crypto.encrypt_file_with(src, dst, KEY, encrypt)
    assert info["size_bytes"] == len(plain) + backup_crypto.NONCE_BYTES + backup_crypto.TAG_BYTES
    payload = dst.read_bytes()
    assert backup_crypto.decrypt_bytes_with(payload, KEY, decrypt) == plain
    return payload


def test_fixed_vector_still_decrypts_in_stdlib_and_lib():
    assert len(FIXED_PAYLOAD) == len(FIXED_PLAIN) + 28
    assert backup_crypto.decrypt_bytes_with(FIXED_PAYLOAD, KEY, "stdlib") == FIXED_PLAIN
    if HAS_LIB:
        assert backup_crypto.decrypt_bytes_with(FIXED_PAYLOAD, KEY, "lib") == FIXED_PLAIN


def test_same_plaintext_gets_fresh_nonce_each_time(tmp_path: Path):
    plain = b"nonce must never repeat"
    first = _roundtrip(tmp_path, plain, "stdlib", "stdlib")
    # 跨实现也必须是新 nonce（同一密文两次的 nonce 相同就等于 GCM 灾难）
    impl = "lib" if HAS_LIB else "stdlib"
    second = _roundtrip(tmp_path, plain, impl, impl)
    nonce_a, nonce_b = first[:12], second[:12]
    assert len(nonce_a) == len(nonce_b) == backup_crypto.NONCE_BYTES
    assert nonce_a != nonce_b


@pytest.mark.skipif(not HAS_LIB, reason="未安装 cryptography，无法验证双实现互通")
def test_stdlib_ciphertext_decrypts_with_lib(tmp_path: Path):
    plain = b"A" * (65536 + 7)  # 跨 CHUNK_BYTES 且末尾不足 16 字节，专门压边界处理
    _roundtrip(tmp_path, plain, "stdlib", "lib")


@pytest.mark.skipif(not HAS_LIB, reason="未安装 cryptography，无法验证双实现互通")
def test_lib_ciphertext_decrypts_with_stdlib(tmp_path: Path):
    plain = b"B" * (65536 + 7)
    _roundtrip(tmp_path, plain, "lib", "stdlib")


@pytest.mark.skipif(not HAS_LIB, reason="未安装 cryptography，无法验证篡改检测")
def test_lib_rejects_tampered_ciphertext():
    payload = bytearray(FIXED_PAYLOAD)
    payload[-1] ^= 0x01
    with pytest.raises(backup_crypto.CryptoVerifyError):
        backup_crypto.decrypt_bytes_with(bytes(payload), KEY, "lib")


def test_backend_is_labelled_and_fallback_has_reason():
    info = backup_crypto.backend_info()
    assert info["implementation"] in {"lib", "stdlib"}
    if info["implementation"] == "stdlib":
        assert info["reason"]  # 回落必须有原因，不能静默
    assert backup_crypto.backend() == info["implementation"]


def test_rotation_keeps_newest_and_removes_sidecars(remote):
    keep = 3
    remote.settings.backup_remote_keep = keep
    for index in range(6):
        name = f"unified-backup-2026010{index}-120000.zip.enc"
        remote.fake.objects[f"{PREFIX}/{name}"] = (b"old", {"sha256": "x" * 64})
        remote.fake.objects[f"{PREFIX}/{name}.sha256"] = (b"x" * 64, {})
    remote.fake.objects[f"{PREFIX}/unrelated.txt"] = (b"keep me", {})

    archive = _make_archive(remote.dir)
    result = remote.service.upload(retry_delays=(), now=1000)
    assert result["ok"] is True
    assert result["rotated"] == 4

    encrypted = sorted(key for key in remote.fake.objects if key.endswith(".zip.enc"))
    assert len(encrypted) == keep
    assert remote_key(remote.service.config(), archive.name) in encrypted
    for key in encrypted:
        assert key + ".sha256" in remote.fake.objects
    assert f"{PREFIX}/unrelated.txt" in remote.fake.objects


# ---------------------------------------------------------------- 反向链路（restore-remote）
# 走真实 backup_core 的归档格式：这里的归档由 /api/system/backup 现场生成，
# 恢复链与本地恢复完全相同，不复制第二套换库逻辑。

LATE_USER = "u-after-remote-snapshot"
LATE_NAME = "unified-backup-20261105-120000.zip"


@pytest.fixture()
def panel(tmp_path, fake_s3, monkeypatch):
    """最小控制面：挂备份路由与远端路由，鉴权用 dependency_overrides 顶掉。"""
    monkeypatch.setenv("UNIFIED_BACKUP_ENCRYPTION_KEY", base64.b64encode(bytes(range(32))).decode())
    monkeypatch.setenv("UNIFIED_BACKUP_S3_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("UNIFIED_BACKUP_S3_SECRET_ACCESS_KEY", SECRET_VALUE)
    db_path = tmp_path / "data" / "unified.db"
    db = Database(db_path)
    # 归档自检要求至少一个 admin（生产库由启动引导创建），这里补上
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            ("admin", "seed-hash", "admin", int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()
    settings = SimpleNamespace(
        db_path=str(db_path),
        backup_remote_enabled=True,
        backup_remote_interval_sec=3600,
        backup_remote_auto_upload=False,
        backup_remote_keep=14,
    )
    service = BackupRemoteService(settings, db)
    service.save_config(
        {
            "endpoint": fake_s3.endpoint,
            "region": "us-east-1",
            "bucket": BUCKET,
            "path_style": True,
            "prefix": PREFIX,
        }
    )
    app = FastAPI()
    app.state.db = db
    app.state.settings = settings
    app.state.backup_remote = service
    app.include_router(backup_mod.router)
    app.include_router(remote_mod.router)
    app.dependency_overrides[security.admin_user] = lambda: {"username": "admin", "role": "admin"}
    directory = tmp_path / "data" / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        app=app,
        service=service,
        settings=settings,
        db=db,
        db_path=str(db_path),
        dir=directory,
        fake=fake_s3,
    )


async def _api(app: FastAPI, method: str, path: str, payload: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control.test", trust_env=False) as client:
        return await client.request(method, path, json=payload)


def _live_state(db_path: str) -> dict[str, bytes | None]:
    """db / -wal / -shm 的原始字节；None = 不存在。"""
    state: dict[str, bytes | None] = {}
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{db_path}{suffix}")
        state[suffix or "db"] = path.read_bytes() if path.is_file() else None
    return state


def _temp_leftovers(directory: Path, name: str) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(f".{name}"))


def _audit_rows(db_path: str, action: str) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT target, coalesce(detail, '') FROM audit_logs WHERE action=?", (action,)
        ).fetchall()
        return [(str(target), str(detail)) for target, detail in rows]
    finally:
        conn.close()


async def _upload(panel, name: str) -> httpx.Response:
    return await _api(panel.app, "POST", "/api/system/backups/upload", {"name": name})


async def _dummy_remote(panel, name: str = LATE_NAME) -> str:
    """造一份假归档并上传；随后删掉本地副本，失败路径就能验证「什么也没留下」。"""
    _make_archive(panel.dir, name)
    uploaded = await _upload(panel, name)
    assert uploaded.status_code == 200, uploaded.text
    (panel.dir / name).unlink()
    return name


async def test_restore_remote_roundtrip_replaces_live_db(panel, caplog):
    made = await _api(panel.app, "POST", "/api/system/backup")
    assert made.status_code == 200, made.text
    name = made.json()["name"]
    uploaded = await _upload(panel, name)
    assert uploaded.status_code == 200, uploaded.text

    key = remote_key(panel.service.config(), name)
    assert key in panel.fake.objects

    # 快照之后写入的行：远端恢复后必须消失
    conn = sqlite3.connect(panel.db_path)
    try:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            (LATE_USER, "not-a-real-hash", "viewer", int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()

    with caplog.at_level(logging.INFO):
        r = await _api(
            panel.app,
            "POST",
            "/api/system/backups/restore-remote",
            {"name": name, "confirm": name},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["source"] == "remote"
    assert body["object"] == key
    assert body["restored"] == name
    assert body["selfcheck_ok"] is True
    assert body["pre_restore_backup"].startswith("unified-backup-")

    conn = sqlite3.connect(panel.db_path)
    try:
        users = [row[0] for row in conn.execute("SELECT username FROM users")]
        assert LATE_USER not in users
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    # 独立复核结构自检，不只信响应里的 selfcheck_ok
    core = backup_mod.core_module()
    with zipfile.ZipFile(panel.dir / name) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
    verdict = backup_mod._structure_check(core, Path(panel.db_path), manifest)  # noqa: SLF001
    assert verdict["ok"] is True, verdict

    # 审计：动作、对象名（target）与结果（detail）都在
    rows = _audit_rows(panel.db_path, "backup.restore_remote")
    assert len(rows) == 1
    target, detail = rows[0]
    assert target == key
    assert "result=ok" in detail

    # 下载产物留在本地，临时名与覆盖前的 .prior 都清干净
    assert (panel.dir / name).is_file()
    assert _temp_leftovers(panel.dir, name) == []

    # 响应与日志不含密钥（对称加密 key、S3 secret）
    key_b64 = base64.b64encode(bytes(range(32))).decode()
    assert key_b64 not in r.text
    assert SECRET_VALUE not in r.text
    assert key_b64 not in caplog.text
    assert SECRET_VALUE not in caplog.text


async def test_restore_remote_rejects_bad_name_and_confirm(panel):
    before = _live_state(panel.db_path)
    r = await _api(
        panel.app,
        "POST",
        "/api/system/backups/restore-remote",
        {"name": "../../unified-backup-20990101-000000.zip", "confirm": "../../unified-backup-20990101-000000.zip"},
    )
    assert r.status_code == 400, r.text
    assert "不合法" in r.json()["detail"]

    name = "unified-backup-20990101-000000.zip"
    r = await _api(
        panel.app,
        "POST",
        "/api/system/backups/restore-remote",
        {"name": name, "confirm": "yes"},
    )
    assert r.status_code == 400, r.text
    assert "逐字一致" in r.json()["detail"]

    assert panel.fake.requests == []  # 校验不过，一个外呼都不发
    assert _live_state(panel.db_path) == before
    assert _temp_leftovers(panel.dir, name) == []


async def test_restore_remote_rejects_missing_sidecar(panel):
    name = await _dummy_remote(panel)
    key = remote_key(panel.service.config(), name)
    panel.fake.objects.pop(key + ".sha256")
    before = _live_state(panel.db_path)

    r = await _api(
        panel.app, "POST", "/api/system/backups/restore-remote", {"name": name, "confirm": name}
    )
    assert r.status_code == 400, r.text
    assert "旁注" in r.json()["detail"]
    assert _live_state(panel.db_path) == before
    assert not (panel.dir / name).exists()
    assert _temp_leftovers(panel.dir, name) == []


async def test_restore_remote_rejects_checksum_mismatch(panel):
    name = await _dummy_remote(panel)
    key = remote_key(panel.service.config(), name)
    panel.fake.objects[key + ".sha256"] = (b"deadbeef" + b" " * 2 + b"tampered\n", {})
    before = _live_state(panel.db_path)

    r = await _api(
        panel.app, "POST", "/api/system/backups/restore-remote", {"name": name, "confirm": name}
    )
    assert r.status_code == 400, r.text
    assert "不一致" in r.json()["detail"]
    assert _live_state(panel.db_path) == before
    assert not (panel.dir / name).exists()
    assert _temp_leftovers(panel.dir, name) == []


async def test_restore_remote_rejects_missing_object(panel):
    name = "unified-backup-20990101-000000.zip"
    before = _live_state(panel.db_path)

    r = await _api(
        panel.app, "POST", "/api/system/backups/restore-remote", {"name": name, "confirm": name}
    )
    assert r.status_code == 404, r.text
    assert "不存在" in r.json()["detail"]
    assert _live_state(panel.db_path) == before
    assert _temp_leftovers(panel.dir, name) == []


async def test_restore_remote_requires_encryption_key(panel, monkeypatch, caplog):
    name = await _dummy_remote(panel)
    calls = len(panel.fake.requests)
    monkeypatch.delenv("UNIFIED_BACKUP_ENCRYPTION_KEY", raising=False)
    before = _live_state(panel.db_path)

    with caplog.at_level(logging.INFO):
        r = await _api(
            panel.app, "POST", "/api/system/backups/restore-remote", {"name": name, "confirm": name}
        )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "UNIFIED_BACKUP_ENCRYPTION_KEY" in detail
    assert "拒绝" in detail
    assert len(panel.fake.requests) == calls  # 缺密钥时不再发任何远端请求
    assert _live_state(panel.db_path) == before
    assert SECRET_VALUE not in r.text and SECRET_VALUE not in caplog.text


async def test_restore_remote_busy_lock_returns_409(panel):
    name = await _dummy_remote(panel)
    calls = len(panel.fake.requests)
    before = _live_state(panel.db_path)
    lock = acquire(panel.dir, "backup")
    try:
        r = await _api(
            panel.app, "POST", "/api/system/backups/restore-remote", {"name": name, "confirm": name}
        )
    finally:
        release(lock)
    assert r.status_code == 409, r.text
    assert "进行中" in r.json()["detail"]
    assert len(panel.fake.requests) == calls
    assert _live_state(panel.db_path) == before


async def test_restore_remote_bad_archive_content_cleans_up(panel):
    """下载与解密都成功但归档不可用：仍要删掉落盘文件，活库不动。"""
    name = LATE_NAME  # _dummy_remote 那份 zip 里没有 manifest
    name = await _dummy_remote(panel, name)
    before = _live_state(panel.db_path)

    r = await _api(
        panel.app, "POST", "/api/system/backups/restore-remote", {"name": name, "confirm": name}
    )
    assert r.status_code == 400, r.text
    assert "已中止恢复" in r.json()["detail"]
    assert _live_state(panel.db_path) == before
    assert not (panel.dir / name).exists()
    assert _temp_leftovers(panel.dir, name) == []


async def test_restore_remote_403_is_fatal_and_state_records_reason(panel):
    name = await _dummy_remote(panel)
    panel.fake.forced_error = (403, "AccessDenied", f"denied for {SECRET_VALUE}")

    r = await _api(
        panel.app, "POST", "/api/system/backups/restore-remote", {"name": name, "confirm": name}
    )
    assert r.status_code == 502, r.text
    assert "致命" in r.json()["detail"]
    assert SECRET_VALUE not in r.text
    state = panel.service.state()
    assert state["auth_blocked"] is True
    assert state["last_error_code"] == "auth_rejected"
    assert state["last_error"]


async def test_restore_remote_503_is_retryable(panel):
    name = await _dummy_remote(panel)
    panel.fake.forced_error = (503, "SlowDown", "please retry")

    r = await _api(
        panel.app, "POST", "/api/system/backups/restore-remote", {"name": name, "confirm": name}
    )
    assert r.status_code == 502, r.text
    assert "可重试" in r.json()["detail"]
    state = panel.service.state()
    assert state["last_error_code"] == "retryable"
    assert state["auth_blocked"] is False


async def test_restore_remote_clock_skew_is_translated(panel):
    name = await _dummy_remote(panel)
    panel.fake.forced_error = (403, "RequestTimeTooSkewed", "the difference between the request time")

    r = await _api(
        panel.app, "POST", "/api/system/backups/restore-remote", {"name": name, "confirm": name}
    )
    assert r.status_code == 502, r.text
    assert "宿主时钟偏差超过 15 分钟" in r.json()["detail"]


async def test_delete_remote_removes_archive_and_sidecar(panel):
    name = await _dummy_remote(panel)
    key = remote_key(panel.service.config(), name)
    assert key in panel.fake.objects and key + ".sha256" in panel.fake.objects

    r = await _api(panel.app, "DELETE", f"/api/system/backups/remote/{name}")
    assert r.status_code == 200, r.text
    assert r.json()["object"] == key
    assert key not in panel.fake.objects
    assert key + ".sha256" not in panel.fake.objects

    rows = _audit_rows(panel.db_path, "backup.remote.delete")
    assert len(rows) == 1
    assert rows[0][0] == key


async def test_delete_remote_rejects_names_outside_whitelist(panel):
    panel.fake.objects[f"{PREFIX}/unrelated.txt"] = (b"keep me", {})
    r = await _api(panel.app, "DELETE", "/api/system/backups/remote/unified-backup-not-a-timestamp.zip")
    assert r.status_code == 400, r.text
    assert f"{PREFIX}/unrelated.txt" in panel.fake.objects


async def test_remote_list_reports_key_and_reachability(panel):
    listing = await _api(panel.app, "GET", "/api/system/backups/remote")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["encryption_key_configured"] is True
    assert body["access_key_id_masked"] == "AKIA****2345"
    assert body["credential_source"] == "env"
    assert body["configured"] is True
    assert body["s3_reachable"] is True
    assert panel.service.state()["s3_reachable"] is True

    panel.fake.forced_error = (500, "InternalError", "boom")
    broken = await _api(panel.app, "GET", "/api/system/backups/remote")
    assert broken.status_code == 502  # 列表失败沿用既有 502，不升级成 500
    detail = broken.json()["detail"]
    assert detail["s3_reachable"] is False
    assert panel.service.state()["s3_reachable"] is False
    assert "boom" in panel.service.state()["s3_last_error"]


async def test_remote_list_marks_sidecar_per_object(panel):
    with_sidecar = await _dummy_remote(panel, LATE_NAME)
    key_ok = remote_key(panel.service.config(), with_sidecar)

    other = "unified-backup-20261105-130000.zip"
    _make_archive(panel.dir, other)
    assert (await _upload(panel, other)).status_code == 200
    key_bad = remote_key(panel.service.config(), other)
    panel.fake.objects.pop(key_bad + ".sha256")

    listing = await _api(panel.app, "GET", "/api/system/backups/remote")
    assert listing.status_code == 200, listing.text
    rows = {obj["key"]: obj for obj in listing.json()["objects"]}
    assert rows[key_ok]["has_sidecar"] is True
    assert rows[key_bad]["has_sidecar"] is False


async def test_save_config_rejects_credentials_over_http(panel):
    r = await _api(
        panel.app,
        "POST",
        "/api/system/backup-remote",
        {"access_key": ACCESS_KEY, "secret_key": SECRET_VALUE},
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "UNIFIED_BACKUP_S3_ACCESS_KEY_ID" in detail
    assert SECRET_VALUE not in r.text


async def test_restore_remote_reports_missing_env_credentials(panel, monkeypatch):
    name = await _dummy_remote(panel)
    calls = len(panel.fake.requests)
    monkeypatch.delenv("UNIFIED_BACKUP_S3_SECRET_ACCESS_KEY")
    before = _live_state(panel.db_path)

    r = await _api(
        panel.app, "POST", "/api/system/backups/restore-remote", {"name": name, "confirm": name}
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "未配置" in detail and "UNIFIED_BACKUP_S3_SECRET_ACCESS_KEY" in detail
    assert len(panel.fake.requests) == calls
    assert _live_state(panel.db_path) == before
