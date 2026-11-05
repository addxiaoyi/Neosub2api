"""定时远端备份的调度口径：默认不动作、按注入时钟只跑一次、锁被占时跳过。"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import Database
from app.services.backup_remote import BackupRemoteService
from app.services.backup_schedule import BackupScheduleService
from app.services.ops_lock import acquire, release
from tests.fake_s3_server import BUCKET, FakeS3

ACCESS_KEY = "AKIATESTKEY12345"
SECRET_VALUE = "SCHEDULE-SECRET-DO-NOT-LEAK"
PREFIX = "schedule-prefix"


@pytest.fixture()
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "UNIFIED_BACKUP_ENCRYPTION_KEY", base64.b64encode(b"s" * 32).decode()
    )
    monkeypatch.setenv("UNIFIED_BACKUP_S3_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("UNIFIED_BACKUP_S3_SECRET_ACCESS_KEY", SECRET_VALUE)
    fake = FakeS3().start()
    directory = tmp_path / "data" / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(directory / "unified-backup-20261105-090000.zip", "w") as bundle:
        bundle.writestr("dump.sql", "select 1;")
    settings = SimpleNamespace(
        db_path=str(tmp_path / "data" / "unified.db"),
        backup_remote_enabled=False,
        backup_remote_interval_sec=3600,
        backup_remote_auto_upload=False,
        backup_remote_keep=14,
    )
    db = Database(Path(settings.db_path))
    remote = BackupRemoteService(settings, db)
    remote.save_config(
        {
            "endpoint": fake.endpoint,
            "region": "us-east-1",
            "bucket": BUCKET,
            "path_style": True,
            "prefix": PREFIX,
        }
    )
    schedule = BackupScheduleService(settings, db, remote)
    try:
        yield SimpleNamespace(
            settings=settings, db=db, remote=remote, schedule=schedule, fake=fake, dir=directory
        )
    finally:
        fake.stop()


def test_disabled_does_nothing_and_makes_no_call(stack):
    result = asyncio.run(stack.schedule.tick(now=1000))
    assert result["reason"] == "disabled"
    assert stack.fake.requests == []
    assert stack.remote.state()["last_attempt_at"] == 0


def test_enabled_runs_once_per_interval(stack):
    stack.settings.backup_remote_enabled = True
    first = asyncio.run(stack.schedule.tick(now=1000))
    assert first["ok"] is True, first
    calls = len(stack.fake.requests)
    assert calls > 0

    second = asyncio.run(stack.schedule.tick(now=1001))
    assert second["skipped"] is True and second["reason"] == "not_due"
    assert len(stack.fake.requests) == calls

    third = asyncio.run(stack.schedule.tick(now=1000 + 3600))
    assert third["ok"] is True, third
    assert len(stack.fake.requests) > calls


def test_busy_lock_skips_round_without_uploading(stack):
    stack.settings.backup_remote_enabled = True
    lock = acquire(stack.dir, "backup")
    try:
        result = asyncio.run(stack.schedule.tick(now=1000))
    finally:
        release(lock)
    assert result["skipped"] is True
    assert result["reason"] == "busy"
    assert stack.fake.requests == []
    assert stack.remote.state()["last_attempt_at"] == 0


def test_missing_key_is_recorded_then_auto_resumes(stack, monkeypatch):
    stack.settings.backup_remote_enabled = True
    monkeypatch.delenv("UNIFIED_BACKUP_ENCRYPTION_KEY", raising=False)
    blocked = asyncio.run(stack.schedule.tick(now=1000))
    assert blocked["reason"] == "key_missing"
    state = stack.remote.state()
    assert state["auth_blocked"] is True
    assert state["last_error_code"] == "key_missing"
    assert "密钥" in state["last_error"]
    assert stack.fake.requests == []

    monkeypatch.setenv(
        "UNIFIED_BACKUP_ENCRYPTION_KEY", base64.b64encode(b"s" * 32).decode()
    )
    resumed = asyncio.run(stack.schedule.tick(now=1001))
    assert resumed["ok"] is True, resumed
    assert stack.remote.state()["auth_blocked"] is False


def test_auth_blocked_is_not_retried_until_config_changes(stack):
    stack.settings.backup_remote_enabled = True
    stack.fake.forced_error = (403, "AccessDenied", "nope")
    first = asyncio.run(stack.schedule.tick(now=1000))
    assert first["ok"] is False
    assert stack.remote.state()["auth_blocked"] is True
    calls = len(stack.fake.requests)

    stack.fake.forced_error = None
    second = asyncio.run(stack.schedule.tick(now=1001))
    assert second["skipped"] is True
    assert second["reason"] == "auth_blocked"
    assert len(stack.fake.requests) == calls

    # 管理员改连接参数后解除阻塞，下一轮才会重新尝试
    stack.remote.save_config({"region": "us-east-1"})
    assert stack.remote.state()["auth_blocked"] is False
    third = asyncio.run(stack.schedule.tick(now=1002))
    assert third["ok"] is True, third
