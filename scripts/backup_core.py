#!/usr/bin/env python3
"""unified-2api 备份/恢复核心实现（Windows 与 Linux 共用）。

scripts/backup.ps1、backup.sh、restore.ps1、restore.sh 都只是这一层的薄封装，
参数翻译完就交给本文件，避免两个平台各写一套逻辑最后悄悄跑偏。

三条硬规则：
1. 绝不直接复制正在写入的库。快照走 SQLite 的读路径（VACUUM INTO，退化到
   Connection.backup），WAL 里已提交的事务会被包含。
2. 归档是自描述的单文件：manifest.json 记录每个成员的 sha256、时间戳、
   应用版本与每表行数；归档在旁再落一份 .sha256 供外部比对。
3. 恢复前一定先把当前库原样留存一份（哪怕它已经坏了），自检失败自动回滚。

演示数据（provider=demo_local、密钥名以 "[demo] " 开头）默认不进入归档，
清单里记录删了多少行；需要保留时显式 --include-demo。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

CORE_VERSION = "1.0.0"
MANIFEST_VERSION = 1

ARCHIVE_RE = re.compile(r"^unified-backup-\d{8}-\d{6}\.zip$")
PRE_RESTORE_RE = re.compile(r"^pre-restore-\d{8}-\d{6}$")

DEMO_PROVIDER_ID = "demo_local"
DEMO_MARK = "[demo] "
DEMO_SETTINGS = ("demo_seed", "demo_disabled_providers")

# 这些缺失即说明恢复对象不是本项目的库
REQUIRED_TABLES = (
    "users",
    "sessions",
    "providers",
    "accounts",
    "api_keys",
    "request_logs",
    "usage_records",
    "settings",
    "audit_logs",
    "provider_credentials",
    "billing_ledger",
    "price_books",
    "quota_policies",
)

# 归档里收集的仓库配置文件；目录不存在时跳过
CONFIG_DIRS = ("config", "providers", "upstreams")

EXIT_FAIL = 1
EXIT_VERIFY = 2


class BackupError(RuntimeError):
    """可预期的运维错误：给一句结论，不打堆栈。"""


# ---------------------------------------------------------------- 基础工具


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def app_version(root: Path) -> str:
    source = root / "apps" / "control" / "app" / "__init__.py"
    if not source.is_file():
        return "unknown"
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', source.read_text(encoding="utf-8"))
    return match.group(1) if match else "unknown"


def open_db(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute("PRAGMA query_only = ON")
    return conn


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in names}


def database_stats(path: Path) -> dict:
    conn = open_db(path, readonly=True)
    try:
        checks = {
            "integrity_check": "; ".join(str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()),
            "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            "schema_version": int(conn.execute("PRAGMA schema_version").fetchone()[0]),
            "page_size": int(conn.execute("PRAGMA page_size").fetchone()[0]),
            "page_count": int(conn.execute("PRAGMA page_count").fetchone()[0]),
            "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
            "tables": table_counts(conn),
        }
    finally:
        conn.close()
    return checks


def snapshot(src: Path, dest: Path) -> str:
    """把 src 的一致性快照写到 dest，返回使用的方法名。

    VACUUM INTO 是 SQLite 官方的导出路径：一个读事务里遍历已提交页，
    写出的文件不带 WAL 残留。只有它不可用（<3.27）才退化到 Connection.backup。
    """
    if not src.is_file():
        raise BackupError(f"source database not found: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    conn = open_db(src)
    try:
        try:
            conn.execute("VACUUM INTO ?", (str(dest),))
            method = "vacuum_into"
        except sqlite3.Error:
            if dest.exists():
                dest.unlink()
            target = sqlite3.connect(str(dest))
            try:
                conn.backup(target)
            finally:
                target.close()
            method = "backup_api"
    finally:
        conn.close()

    if not dest.is_file() or dest.stat().st_size == 0:
        raise BackupError("snapshot produced an empty file")
    _chmod600(dest)
    return method


def _chmod600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- 演示数据


def count_demo_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """按 demo 标记数一遍，用于清单留证（不动数据）。"""
    targets = (
        ("api_keys", "SELECT COUNT(*) FROM api_keys WHERE name LIKE ?", (DEMO_MARK + "%",)),
        ("providers", "SELECT COUNT(*) FROM providers WHERE id = ?", (DEMO_PROVIDER_ID,)),
        ("accounts", "SELECT COUNT(*) FROM accounts WHERE provider = ? OR file LIKE 'demo\\_%' ESCAPE '\\'", (DEMO_PROVIDER_ID,)),
        ("provider_credentials", "SELECT COUNT(*) FROM provider_credentials WHERE provider = ?", (DEMO_PROVIDER_ID,)),
        ("price_books", "SELECT COUNT(*) FROM price_books WHERE provider = ?", (DEMO_PROVIDER_ID,)),
        ("request_logs", "SELECT COUNT(*) FROM request_logs WHERE provider = ?", (DEMO_PROVIDER_ID,)),
        ("usage_records", "SELECT COUNT(*) FROM usage_records WHERE provider = ?", (DEMO_PROVIDER_ID,)),
        ("billing_ledger", "SELECT COUNT(*) FROM billing_ledger WHERE provider = ?", (DEMO_PROVIDER_ID,)),
        ("price_book_snapshots", "SELECT COUNT(*) FROM price_book_snapshots WHERE provider = ?", (DEMO_PROVIDER_ID,)),
        (
            "quota_policies",
            "SELECT COUNT(*) FROM quota_policies WHERE provider = ? "
            "OR key_id IN (SELECT id FROM api_keys WHERE name LIKE ?)",
            (DEMO_PROVIDER_ID, DEMO_MARK + "%"),
        ),
    )
    found: dict[str, int] = {}
    for table, sql, params in targets:
        if not has_table(conn, table):
            continue
        if table == "quota_policies" and not has_table(conn, "api_keys"):
            continue
        count = int(conn.execute(sql, params).fetchone()[0])
        if count:
            found[table] = count
    return found


def strip_demo(conn: sqlite3.Connection) -> dict[str, int]:
    """从快照副本里删掉演示数据，规则与 apps/control/app/demo.py 的 clear_demo_data 对齐。

    只改快照文件，源库不受影响。顺序上先删配额（引用密钥 id），最后删 provider。
    """
    removed: dict[str, int] = {}

    def drop(table: str, sql: str, params: tuple = ()) -> None:
        if not has_table(conn, table):
            return
        deleted = int(conn.execute(sql, params).rowcount or 0)
        if deleted:
            removed[table] = removed.get(table, 0) + deleted

    demo_policy_ids: list[int] = []
    if has_table(conn, "quota_policies"):
        demo_policy_ids = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM quota_policies WHERE provider = ? "
                "OR key_id IN (SELECT id FROM api_keys WHERE name LIKE ?)",
                (DEMO_PROVIDER_ID, DEMO_MARK + "%"),
            ).fetchall()
        ]
    if demo_policy_ids:
        marks = ",".join("?" for _ in demo_policy_ids)
        drop("quota_alerts", f"DELETE FROM quota_alerts WHERE policy_id IN ({marks})", tuple(demo_policy_ids))
        drop(
            "quota_alert_thresholds",
            f"DELETE FROM quota_alert_thresholds WHERE policy_id IN ({marks})",
            tuple(demo_policy_ids),
        )
        drop("quota_policies", f"DELETE FROM quota_policies WHERE id IN ({marks})", tuple(demo_policy_ids))

    drop("request_logs", "DELETE FROM request_logs WHERE provider = ?", (DEMO_PROVIDER_ID,))
    drop("usage_records", "DELETE FROM usage_records WHERE provider = ?", (DEMO_PROVIDER_ID,))
    drop("billing_ledger", "DELETE FROM billing_ledger WHERE provider = ?", (DEMO_PROVIDER_ID,))
    drop("price_book_snapshots", "DELETE FROM price_book_snapshots WHERE provider = ?", (DEMO_PROVIDER_ID,))
    drop(
        "accounts",
        "DELETE FROM accounts WHERE provider = ? OR file LIKE 'demo\\_%' ESCAPE '\\'",
        (DEMO_PROVIDER_ID,),
    )
    drop("provider_credentials", "DELETE FROM provider_credentials WHERE provider = ?", (DEMO_PROVIDER_ID,))
    drop("price_books", "DELETE FROM price_books WHERE provider = ?", (DEMO_PROVIDER_ID,))
    drop("api_keys", "DELETE FROM api_keys WHERE name LIKE ?", (DEMO_MARK + "%",))
    drop("providers", "DELETE FROM providers WHERE id = ?", (DEMO_PROVIDER_ID,))

    # 演示模式曾临时停用零配置 provider，复原后再删清单，避免把停用状态带进生产
    paused: list[str] = []
    if has_table(conn, "settings"):
        row = conn.execute("SELECT value FROM settings WHERE key = ?", ("demo_disabled_providers",)).fetchone()
        if row:
            try:
                value = json.loads(row[0])
            except json.JSONDecodeError:
                value = None
            if isinstance(value, list):
                paused = [item for item in value if isinstance(item, str) and item != DEMO_PROVIDER_ID]
    if paused and has_table(conn, "providers"):
        ts = int(time.time())
        restored = 0
        for pid in paused:
            restored += int(
                conn.execute(
                    "UPDATE providers SET enabled = 1, updated_at = ? WHERE id = ? AND enabled = 0",
                    (ts, pid),
                ).rowcount or 0
            )
        if restored:
            removed["providers_restored"] = restored
    if has_table(conn, "settings"):
        drop("settings", "DELETE FROM settings WHERE key = ?", ("demo_seed",))
        drop("settings", "DELETE FROM settings WHERE key = ?", ("demo_disabled_providers",))

    conn.commit()
    conn.execute("VACUUM")
    return removed


def demo_residue(conn: sqlite3.Connection) -> dict[str, int]:
    return count_demo_rows(conn)


# ---------------------------------------------------------------- 归档构建


def collect_repo_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for name in CONFIG_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            found.append(path)
    return found


def copy_repo_files(root: Path, staging: Path, files: list[Path]) -> list[dict]:
    records: list[dict] = []
    for source in files:
        rel = source.relative_to(root).as_posix()
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        records.append({"member": rel, "size_bytes": target.stat().st_size, "sha256": sha256_path(target)})
    return records


def build_manifest(*, source_label: str, method: str, db_file: Path, members: list[dict], demo: dict,
                   root: Path) -> dict:
    stats = database_stats(db_file)
    return {
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "created_at_epoch": int(time.time()),
        "tool": {
            "name": "unified-2api backup",
            "version": CORE_VERSION,
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "app_version": app_version(root),
        "source": {"database": Path(source_label).name, "repo": str(root)},
        "method": method,
        "database": {
            "member": "db/unified.db",
            "size_bytes": db_file.stat().st_size,
            "sha256": sha256_path(db_file),
            "integrity_check": stats["integrity_check"],
            "user_version": stats["user_version"],
            "schema_version": stats["schema_version"],
            "page_size": stats["page_size"],
            "page_count": stats["page_count"],
            "journal_mode": stats["journal_mode"],
        },
        "tables": stats["tables"],
        "totals": {"tables": len(stats["tables"]), "rows": sum(stats["tables"].values())},
        "demo": demo,
        "files": members,
    }


def write_zip(staging: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(staging).as_posix())


def prune(out_dir: Path, keep: int) -> list[str]:
    """只清理本工具产出的文件；pre-restore 快照按同一预算单独轮转。"""
    removed: list[str] = []
    archives = sorted(
        (item for item in out_dir.iterdir() if item.is_file() and ARCHIVE_RE.match(item.name)),
        reverse=True,
    )
    for stale in archives[keep:]:
        stale.unlink()
        removed.append(stale.name)
        sidecar = stale.with_name(stale.name + ".sha256")
        if sidecar.exists():
            sidecar.unlink()
            removed.append(sidecar.name)

    pre_dirs = sorted(
        (item for item in out_dir.iterdir() if item.is_dir() and PRE_RESTORE_RE.match(item.name)),
        reverse=True,
    )
    for stale in pre_dirs[keep:]:
        shutil.rmtree(stale)
        removed.append(stale.name)
    return removed


def allocate_archive(out_dir: Path) -> Path:
    for _ in range(5):
        candidate = out_dir / f"unified-backup-{now_stamp()}.zip"
        if not candidate.exists():
            return candidate
        time.sleep(1)
    raise BackupError("cannot allocate a unique archive name, retry in a second")


def make_archive(*, db_source: Path, out_dir: Path, keep: int, include_demo: bool, method: str,
                 source_label: str, root: Path, already_snapshot: bool) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="u2a-backup-"))
    try:
        db_file = staging / "db" / "unified.db"
        if already_snapshot:
            db_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db_source, db_file)
        else:
            method = snapshot(db_source, db_file)

        demo: dict = {
            "excluded": not include_demo,
            "provider_id": DEMO_PROVIDER_ID,
            "marker": DEMO_MARK,
        }
        conn = open_db(db_file)
        try:
            found = count_demo_rows(conn)
            demo["found"] = found
            demo["found_rows"] = sum(found.values())
            if not include_demo:
                removed = strip_demo(conn)
                demo["removed"] = removed
                demo["removed_rows"] = sum(removed.values())
                residue = demo_residue(conn)
                if residue:
                    raise BackupError(f"demo data still present after filtering: {residue}")
            else:
                demo["note"] = "demo rows kept because --include-demo was requested"
        finally:
            conn.close()

        members = copy_repo_files(root, staging, collect_repo_files(root))
        manifest = build_manifest(
            source_label=source_label, method=method, db_file=db_file, members=members,
            demo=demo, root=root,
        )
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        target = allocate_archive(out_dir)
        temp_zip = out_dir / f".{target.name}.part"
        write_zip(staging, temp_zip)
        digest = sha256_path(temp_zip)
        os.replace(temp_zip, target)
        target.with_name(target.name + ".sha256").write_text(
            f"{digest}  {target.name}\n", encoding="utf-8"
        )
        _chmod600(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    removed = prune(out_dir, keep)

    print(f"backup: {target}")
    print(f"archive sha256: {digest}")
    print(
        f"database: sha256={manifest['database']['sha256']} size={manifest['database']['size_bytes']} "
        f"integrity={manifest['database']['integrity_check']} user_version={manifest['database']['user_version']} "
        f"schema_version={manifest['database']['schema_version']} method={method}"
    )
    print(f"tables: {manifest['totals']['tables']} rows={manifest['totals']['rows']}")
    print(_rows_line("rows", manifest["tables"]))
    print(
        f"demo: excluded={demo['excluded']} found={demo.get('found_rows', 0)} "
        f"removed={demo.get('removed_rows', 0)} detail={json.dumps(demo.get('removed') or {}, ensure_ascii=False, sort_keys=True)}"
    )
    print(f"config files: {len(members)} ({', '.join(item['member'] for item in members)})" if members
          else "config files: 0")
    print(f"app_version={manifest['app_version']}")
    if removed:
        print(f"pruned: {', '.join(removed)} (keep={keep})")
    return target


def _rows_line(title: str, counts: dict[str, int]) -> str:
    return f"{title}: " + ", ".join(f"{name}={value}" for name, value in sorted(counts.items()))


# ---------------------------------------------------------------- 归档读取与校验


def archive_digest(archive: Path) -> str | None:
    """有 .sha256 就强校验归档本身；返回实际 sha256，缺失时返回 None。"""
    sidecar = archive.with_name(archive.name + ".sha256")
    actual = sha256_path(archive)
    if not sidecar.is_file():
        return None
    expected = sidecar.read_text(encoding="utf-8").split()
    if not expected or len(expected[0]) != 64:
        raise BackupError(f"malformed checksum file: {sidecar}")
    if expected[0].lower() != actual:
        raise BackupError(f"archive sha256 mismatch: expected={expected[0]} actual={actual}")
    return actual


def extract_zip(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise BackupError(f"archive is corrupt at member: {bad}")
        for info in zf.infolist():
            resolved = (target / info.filename).resolve()
            if target.resolve() not in resolved.parents and resolved != target.resolve():
                raise BackupError(f"unsafe member path in archive: {info.filename}")
        zf.extractall(target)


def member_path(base: Path, member: str) -> Path:
    path = (base / member).resolve()
    root = base.resolve()
    if root not in path.parents and path != root:
        raise BackupError(f"unsafe member path: {member}")
    return path


def verify_members(staging: Path, manifest: dict) -> None:
    problems: list[str] = []
    record = manifest.get("database") or {}
    member = record.get("member") or "db/unified.db"
    db_file = member_path(staging, member)
    if not db_file.is_file():
        problems.append(f"database member missing: {member}")
    else:
        if record.get("size_bytes") != db_file.stat().st_size:
            problems.append(
                f"database size mismatch: manifest={record.get('size_bytes')} actual={db_file.stat().st_size}"
            )
        actual = sha256_path(db_file)
        if record.get("sha256") != actual:
            problems.append(f"database sha256 mismatch: manifest={record.get('sha256')} actual={actual}")
    for item in manifest.get("files") or []:
        path = member_path(staging, item["member"])
        if not path.is_file():
            problems.append(f"member missing: {item['member']}")
        elif path.stat().st_size != item.get("size_bytes"):
            problems.append(f"member size mismatch: {item['member']}")
        elif sha256_path(path) != item.get("sha256"):
            problems.append(f"member sha256 mismatch: {item['member']}")
    if problems:
        raise BackupError("; ".join(problems))


def validate_manifest(manifest: object) -> dict:
    """结构不对就明确拒绝，别让畸形清单在后面抛 KeyError。"""
    if not isinstance(manifest, dict):
        raise BackupError("manifest is not a json object")
    for key, kind in (("database", dict), ("files", list), ("tables", dict), ("totals", dict), ("demo", dict)):
        if not isinstance(manifest.get(key), kind):
            raise BackupError(f"manifest field is invalid: {key}")
    version = manifest.get("manifest_version")
    if not isinstance(version, int) or version < 1:
        raise BackupError("manifest version is invalid")
    if version > MANIFEST_VERSION:
        raise BackupError("manifest version is newer than this tool")
    return manifest


def load_archive(archive: Path, *, require_sidecar: bool = False) -> tuple[Path, dict, Path]:
    if not archive.is_file():
        raise BackupError(f"archive not found: {archive}")
    digest = archive_digest(archive)
    if digest is None and require_sidecar:
        raise BackupError(f"checksum file missing next to {archive.name}; refusing unverified restore")
    staging = Path(tempfile.mkdtemp(prefix="u2a-restore-"))
    try:
        extract_zip(archive, staging)
        manifest_path = staging / "manifest.json"
        if not manifest_path.is_file():
            raise BackupError(f"manifest.json missing in {archive.name}; refusing to restore an unverifiable archive")
        manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
        if str(manifest["database"].get("integrity_check")) != "ok":
            raise BackupError(
                f"snapshot integrity_check failed at creation: {manifest['database'].get('integrity_check')}"
            )
        verify_members(staging, manifest)
        db_file = member_path(staging, manifest["database"].get("member") or "db/unified.db")
        integrity = database_stats(db_file)["integrity_check"]
        if integrity != "ok":
            raise BackupError(f"extracted database integrity_check failed: {integrity}")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return db_file, manifest, staging


def print_load_summary(archive: Path, digest: str | None, manifest: dict) -> None:
    print(f"archive: {archive}")
    print(f"archive sha256: {digest or '<no .sha256 sidecar>'}")
    print(
        f"manifest: version={manifest.get('manifest_version')} app={manifest.get('app_version')} "
        f"created_at={manifest.get('created_at')} method={manifest.get('method')}"
    )
    print(
        f"database: sha256={(manifest.get('database') or {}).get('sha256')} "
        f"size={(manifest.get('database') or {}).get('size_bytes')} "
        f"tables={(manifest.get('totals') or {}).get('tables')} rows={(manifest.get('totals') or {}).get('rows')}"
    )
    demo = manifest.get("demo") or {}
    print(
        f"demo: excluded={demo.get('excluded')} found={demo.get('found_rows', 0)} "
        f"removed={demo.get('removed_rows', 0)}"
    )
    print(f"config files: {len(manifest.get('files') or [])}")


# ---------------------------------------------------------------- 恢复


def pre_restore(db: Path, backups_dir: Path) -> Path:
    """把当前库原样留存到 pre-restore-<stamp>/，文件名统一 *.bak。

    这里刻意用字节拷贝而不是在线快照：恢复最常见的场景就是「库已经坏了」，
    坏库上的 VACUUM INTO 会直接失败，而我们需要的是留证与回滚能力。
    前提是服务已停止（Windows 上打开中的文件也拷贝不了）。
    """
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / f"pre-restore-{now_stamp()}"
    while target.exists():
        time.sleep(1)
        target = backups_dir / f"pre-restore-{now_stamp()}"
    target.mkdir(parents=True)

    saved: dict[str, dict] = {}
    for suffix in ("", "-wal", "-shm"):
        src = Path(f"{db}{suffix}")
        if not src.is_file():
            continue
        dst = target / f"{db.name}.bak{suffix}"
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            raise BackupError(
                f"cannot copy current database ({src.name}): {exc}. "
                "Stop the control plane first, then retry (Windows locks open db files)"
            ) from exc
        saved[dst.name] = {"size_bytes": dst.stat().st_size, "sha256": sha256_path(dst)}

    record = {
        "kind": "pre-restore",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "reason": "snapshot of the target database taken before replacing it",
        "note": "raw byte copy of db/-wal/-shm; requires the service to be stopped",
        "files": saved,
    }
    (target / "pre-restore.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def selfcheck(db: Path, manifest: dict) -> dict:
    """只读自检：可查询、表齐全、行数与清单一致、管理员账号在、完整性与无演示残留。"""
    conn = open_db(db, readonly=True)
    try:
        conn.execute("SELECT 1").fetchone()
        counts = table_counts(conn)
        expected = manifest.get("tables") or {}
        missing = sorted(name for name in REQUIRED_TABLES if name not in counts)
        absent = sorted(name for name in expected if name not in counts)
        extra = sorted(name for name in counts if name not in expected)
        drift = {
            name: {"manifest": int(want), "actual": int(counts.get(name, 0))}
            for name, want in expected.items()
            if int(counts.get(name, 0)) != int(want)
        }
        admins = int(conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]) \
            if "users" in counts else 0
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        check_demo = bool((manifest.get("demo") or {}).get("excluded"))
        residue = demo_residue(conn) if check_demo else {}
        checks = {
            "select_1": True,
            "required_tables": {"ok": not missing, "missing": missing},
            "table_count": {
                "ok": not absent and not extra,
                "manifest": len(expected),
                "actual": len(counts),
                "missing": absent,
                "extra": extra,
            },
            "row_counts": {"ok": not drift, "drift": drift},
            "admin_account": {"ok": admins >= 1, "count": admins},
            "demo_free": {"ok": not residue, "residue": residue},
            "integrity_check": integrity,
            "foreign_key_check": {"ok": not fk_rows, "violations": len(fk_rows)},
        }
    finally:
        conn.close()

    ok = (
        checks["select_1"]
        and checks["required_tables"]["ok"]
        and checks["table_count"]["ok"]
        and checks["row_counts"]["ok"]
        and checks["admin_account"]["ok"]
        and checks["demo_free"]["ok"]
        and checks["integrity_check"] == "ok"
    )
    return {"ok": ok, "counts": counts, "checks": checks}


def rollback(db: Path, pre_dir: Path) -> None:
    backup = pre_dir / f"{db.name}.bak"
    if not backup.is_file():
        raise BackupError(f"rollback source missing: {backup}")
    for suffix in ("-wal", "-shm"):
        stale = Path(f"{db}{suffix}")
        if stale.exists():
            stale.unlink()
    shutil.copy2(backup, db)
    for suffix in ("-wal", "-shm"):
        saved = pre_dir / f"{db.name}.bak{suffix}"
        if saved.is_file():
            shutil.copy2(saved, Path(f"{db}{suffix}"))


def restore_config_files(staging: Path, repo: Path, manifest: dict) -> list[str]:
    written: list[str] = []
    for item in manifest.get("files") or []:
        member = item.get("member", "")
        if not member.startswith(tuple(name + "/" for name in CONFIG_DIRS)):
            continue
        source = member_path(staging, member)
        target = member_path(repo, member)
        if sha256_path(source) != item.get("sha256"):
            raise BackupError(f"member sha256 mismatch before write: {member}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.restore.tmp")
        shutil.copy2(source, temp)
        os.replace(temp, target)
        written.append(member)
    return written


def wait_ready(url: str, timeout_sec: int, interval_sec: float = 0.5) -> dict:
    """轮询 /api/health/ready，checks.database.ok 为 true 才算通过；503 也读 body。"""
    deadline = time.time() + timeout_sec
    last: dict = {"ok": False, "detail": "not attempted"}
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                status = resp.status
                body = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read()
        except urllib.error.URLError as exc:
            last = {"ok": False, "detail": f"unreachable: {exc.reason}"}
            time.sleep(interval_sec)
            continue
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            last = {"ok": False, "status": status, "detail": "response is not json"}
            time.sleep(interval_sec)
            continue
        database = (payload.get("checks") or {}).get("database") or {}
        last = {"ok": bool(database.get("ok")), "status": status, "database": database}
        if last["ok"]:
            return last
        time.sleep(interval_sec)
    return last


# ---------------------------------------------------------------- 子命令


def cmd_backup(args) -> int:
    if args.keep < 1:
        raise BackupError("keep must be >= 1")
    db = Path(args.db)
    make_archive(
        db_source=db, out_dir=Path(args.out_dir), keep=args.keep, include_demo=args.include_demo,
        method="", source_label=str(db), root=repo_root(), already_snapshot=False,
    )
    return 0


def cmd_pack(args) -> int:
    if args.keep < 1:
        raise BackupError("keep must be >= 1")
    snapshot_file = Path(args.snapshot)
    make_archive(
        db_source=snapshot_file, out_dir=Path(args.out_dir), keep=args.keep,
        include_demo=args.include_demo, method=args.method, source_label=args.source_label,
        root=repo_root(), already_snapshot=True,
    )
    return 0


def cmd_verify(args) -> int:
    archive = Path(args.archive)
    digest = archive_digest(archive)
    if digest is None and args.require_sidecar:
        raise BackupError(f"checksum file missing next to {archive.name}")
    db_file, manifest, staging = load_archive(archive, require_sidecar=args.require_sidecar)
    try:
        print_load_summary(archive, digest, manifest)
        print(_rows_line("manifest rows", manifest.get("tables") or {}))
        print("verify: ok")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def cmd_inspect(args) -> int:
    db = Path(args.db)
    if not db.is_file():
        raise BackupError(f"database not found: {db}")
    stats = database_stats(db)
    print(
        f"database: size={db.stat().st_size} integrity={stats['integrity_check']} "
        f"user_version={stats['user_version']} schema_version={stats['schema_version']} "
        f"journal_mode={stats['journal_mode']}"
    )
    print(_rows_line("rows", stats["tables"]))
    return 0


def cmd_latest(args) -> int:
    out_dir = Path(args.out_dir)
    if not out_dir.is_dir():
        raise BackupError(f"backup directory not found: {out_dir}")
    archives = sorted(
        (item for item in out_dir.iterdir() if item.is_file() and ARCHIVE_RE.match(item.name)),
        reverse=True,
    )
    if not archives:
        raise BackupError(f"no backup archive in {out_dir}")
    print(archives[0])
    return 0


def cmd_restore(args) -> int:
    archive = Path(args.archive)
    db = Path(args.db)
    backups_dir = Path(args.backups)
    repo = Path(args.repo)

    digest = archive_digest(archive)
    if digest is None:
        if args.require_sidecar:
            raise BackupError(f"checksum file missing next to {archive.name}; refusing unverified restore")
        if not args.skip_verify:
            print("warning: 归档没有 .sha256 旁注，只做 manifest 成员校验", file=sys.stderr)
    db_file, manifest, staging = load_archive(archive, require_sidecar=False)
    try:
        print_load_summary(archive, digest, manifest)
        print(f"target: {db}")

        before = None
        if db.is_file():
            conn = None
            try:
                conn = open_db(db, readonly=True)
                before = table_counts(conn)
            except sqlite3.Error:
                before = None
            finally:
                if conn is not None:
                    conn.close()
        print(_rows_line("rows before restore", before) if before else "rows before restore: <unreadable/absent>")

        pre_dir = None
        if db.is_file():
            pre_dir = pre_restore(db, backups_dir)
            print(f"pre-restore snapshot: {pre_dir} ({(db.name)}.bak)")

        db.parent.mkdir(parents=True, exist_ok=True)
        staged = db.parent / f".{db.name}.restore-{now_stamp()}.tmp"
        try:
            shutil.copy2(db_file, staged)
            _chmod600(staged)
            os.replace(staged, db)
        except OSError as exc:
            raise BackupError(
                f"cannot replace {db}: {exc}. Stop the control plane first, then retry"
            ) from exc
        finally:
            if staged.exists():
                staged.unlink(missing_ok=True)
        # 旧库的 WAL/SHM 必须清掉，否则新库会读到别人的半页
        for suffix in ("-wal", "-shm"):
            stale = Path(f"{db}{suffix}")
            if stale.exists():
                stale.unlink()

        result = selfcheck(db, manifest)
        print(_rows_line("rows after restore", result["counts"]))
        print(f"selfcheck: ok={result['ok']} checks={json.dumps(result['checks'], ensure_ascii=False, sort_keys=True)}")

        if not result["ok"]:
            if pre_dir:
                rollback(db, pre_dir)
                print(f"rolled back: selfcheck failed, original database restored from {pre_dir}")
            raise BackupError("restored database failed selfcheck")

        if args.with_config:
            written = restore_config_files(staging, repo, manifest)
            print(f"config restored: {len(written)} files ({', '.join(written)})")
        else:
            print(f"config archived but not restored ({len(manifest.get('files') or [])} files, use --with-config)")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if args.ready_url:
        ready = wait_ready(args.ready_url, args.ready_timeout)
        print(f"ready: {json.dumps(ready, ensure_ascii=False, sort_keys=True)}")
        if not ready["ok"]:
            raise BackupError("service is not ready: database check did not report ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="unified-2api backup/restore core")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backup", help="snapshot the database and write one .zip archive")
    p.add_argument("--db", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--keep", type=int, default=14)
    p.add_argument("--include-demo", action="store_true", help="keep demo rows instead of filtering them")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("pack", help="wrap an existing snapshot (e.g. taken inside a container) into an archive")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--keep", type=int, default=14)
    p.add_argument("--include-demo", action="store_true")
    p.add_argument("--method", default="external-snapshot")
    p.add_argument("--source-label", default="unified.db")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("verify", help="check archive sha256 and every member against the manifest")
    p.add_argument("--archive", required=True)
    p.add_argument("--require-sidecar", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("restore", help="verify, back up the current database, replace it, then selfcheck")
    p.add_argument("--archive", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--backups", required=True)
    p.add_argument("--repo", default=str(repo_root()))
    p.add_argument("--with-config", action="store_true")
    p.add_argument("--ready-url", default="")
    p.add_argument("--ready-timeout", type=int, default=20)
    p.add_argument("--require-sidecar", action="store_true")
    p.add_argument("--skip-verify", action="store_true", help="emergency only: tolerate a missing .sha256 sidecar")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("inspect", help="read-only stats of a database")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("latest", help="print the newest archive in a directory")
    p.add_argument("--out-dir", required=True)
    p.set_defaults(func=cmd_latest)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_VERIFY if args.command in {"verify", "restore"} else EXIT_FAIL
    except sqlite3.Error as exc:
        print(f"error: sqlite: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except (zipfile.BadZipFile, ValueError, KeyError) as exc:
        print(f"error: malformed archive: {exc}", file=sys.stderr)
        return EXIT_VERIFY if args.command in {"verify", "restore"} else EXIT_FAIL
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
