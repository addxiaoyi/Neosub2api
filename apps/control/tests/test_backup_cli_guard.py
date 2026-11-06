"""CLI 恢复的停服守卫：库被占用时拒绝并说明下一步，停服后仍能一把恢复。

全部跑在 tmp_path 下，归档由真实的 scripts/backup_core.py 现场生成，不碰仓库 data/。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from app.db import Database

REPO = Path(__file__).resolve().parents[3]
CORE = REPO / "scripts" / "backup_core.py"
PY = sys.executable


def _seed_db(path: Path) -> None:
    """用控制面自己的建表逻辑造一个可自检的库，再补上必须存在的 admin 行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    Database(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            ("admin", "x", "admin", 0),
        )
        conn.commit()
    finally:
        conn.close()


def _core(*args: str) -> subprocess.CompletedProcess:
    # 子进程输出里有中文，强制 utf-8 才不会在 Windows 控制台上退化成 GBK
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("CI", None)
    return subprocess.run(
        [PY, str(CORE), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )


def _archive(tmp_path: Path) -> Path:
    source = tmp_path / "src" / "unified.db"
    _seed_db(source)
    out_dir = tmp_path / "backups"
    done = _core("backup", "--db", str(source), "--out-dir", str(out_dir))
    assert done.returncode == 0, done.stderr
    archives = sorted(out_dir.glob("unified-backup-*.zip"))
    assert archives, done.stdout
    return archives[-1]


def _copy_source(tmp_path: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tmp_path / "src" / "unified.db", target)


def test_restore_refuses_when_database_is_still_open(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    target = tmp_path / "live" / "unified.db"
    _copy_source(tmp_path, target)

    # 模拟控制面在跑：先落一条已提交的数据，再挂着未提交的写事务
    holder = sqlite3.connect(target)
    holder.execute("INSERT INTO settings(key, value) VALUES('busy-marker', '1')")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO settings(key, value) VALUES('pending-marker', '1')")
    try:
        done = _core(
            "restore", "--archive", str(archive), "--db", str(target),
            "--backups", str(tmp_path / "pre"), "--repo", str(REPO),
        )
    finally:
        holder.rollback()
        holder.close()

    assert done.returncode == 3, (done.returncode, done.stderr)
    assert "还在被使用" in done.stderr
    assert "scripts/start.sh --stop" in done.stderr
    assert "POST /api/system/restore" in done.stderr
    assert "--force" in done.stderr
    # 被拒绝时目标库必须原样未动
    conn = sqlite3.connect(target)
    try:
        kept = conn.execute("SELECT COUNT(*) FROM settings WHERE key='busy-marker'").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM settings WHERE key='pending-marker'").fetchone()[0]
    finally:
        conn.close()
    assert kept == 1
    assert pending == 0


def test_restore_replaces_database_after_service_stopped(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    target = tmp_path / "live" / "unified.db"
    _copy_source(tmp_path, target)

    conn = sqlite3.connect(target)
    try:
        conn.execute("INSERT INTO settings(key, value) VALUES('stale-row', '1')")
        conn.commit()
    finally:
        conn.close()

    done = _core(
        "restore", "--archive", str(archive), "--db", str(target),
        "--backups", str(tmp_path / "pre"), "--repo", str(REPO),
    )
    assert done.returncode == 0, done.stderr
    assert "selfcheck: ok=True" in done.stdout
    assert list((tmp_path / "pre").glob("pre-restore-*"))

    conn = sqlite3.connect(target)
    try:
        left = conn.execute("SELECT COUNT(*) FROM settings WHERE key='stale-row'").fetchone()[0]
        admin = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
    finally:
        conn.close()
    assert left == 0, "恢复后不应保留旧库的数据"
    assert admin == 1
