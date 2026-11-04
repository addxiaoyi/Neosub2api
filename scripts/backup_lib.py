#!/usr/bin/env python3
"""unified-2api SQLite 备份/恢复核心实现。

scripts/backup.ps1、backup.sh、restore.ps1、restore.sh 都是这一层的薄封装，
两个平台跑同一套快照、清单与校验逻辑，避免 ps/bash 各写一遍悄悄跑偏。

三条硬规则：
1. 绝不直接复制正在写入的库文件。快照走 SQLite 自己的读路径
   （VACUUM INTO，退化时用 Connection.backup），WAL 里已提交的事务会被包含。
2. 每份备份都必须有 manifest（sha256 + integrity_check + 每表行数），
   恢复时先比对清单，不一致就停手，不碰目标库。
3. 恢复前一定先把当前库原样留存一份（哪怕它已经坏了），
   否则事后既没法回滚，也没法解释当初为什么恢复。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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

MANIFEST_VERSION = 1

# 只认本工具自己产出的名字，清理时不碰目录里别人的文件
ARTIFACT_RE = re.compile(r"^(unified-\d{8}-\d{6})\.(db|zip)$")
PRE_BACKUP_RE = re.compile(r"^pre-restore-\d{8}-\d{6}$")

# 缺失即视为「恢复对象不是本项目的库」
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

EXIT_VERIFY = 2


class BackupError(RuntimeError):
    """可预期的运维错误：给一句话结论，不打堆栈。"""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
    return counts


def collect_stats(path: Path) -> dict:
    """只读地收集一个库文件的事实：完整性、版本、每表行数。"""
    conn = open_db(path)
    try:
        integrity = "; ".join(str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall())
        stats = {
            "integrity_check": integrity,
            "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            "schema_version": int(conn.execute("PRAGMA schema_version").fetchone()[0]),
            "page_size": int(conn.execute("PRAGMA page_size").fetchone()[0]),
            "page_count": int(conn.execute("PRAGMA page_count").fetchone()[0]),
            "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
            "tables": table_counts(conn),
        }
    finally:
        conn.close()
    return stats


def snapshot(src: Path, dest: Path) -> str:
    """把 src 的一致性快照写到 dest，返回使用的方法名。

    VACUUM INTO 是 SQLite 官方的「导出一致性副本」路径：它在一个读事务里
    遍历已提交页，写出的文件不含 WAL 残留，也不会读到半页。只有它不可用时
    才退化到 Connection.backup（同样是官方在线备份 API，但不会压缩空洞）。
    """
    src = Path(src)
    dest = Path(dest)
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
            # VACUUM INTO 需要 3.27+；退化前清掉可能留下的半成品
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
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return method


def build_manifest(artifact: Path, method: str, source_label: str) -> dict:
    stats = collect_stats(artifact)
    return {
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "created_at_epoch": int(time.time()),
        "tool": "unified-2api backup",
        "method": method,
        "source": {"name": Path(source_label).name},
        "sqlite_version": sqlite3.sqlite_version,
        "database": {
            "file": artifact.name,
            "size_bytes": artifact.stat().st_size,
            "sha256": sha256_of(artifact),
            "integrity_check": stats["integrity_check"],
            "user_version": stats["user_version"],
            "schema_version": stats["schema_version"],
            "page_size": stats["page_size"],
            "page_count": stats["page_count"],
            "journal_mode": stats["journal_mode"],
        },
        "tables": stats["tables"],
        "totals": {"tables": len(stats["tables"]), "rows": sum(stats["tables"].values())},
    }


def write_manifest(artifact: Path, manifest: dict) -> Path:
    path = artifact.with_suffix(".json")
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def compress_artifact(artifact: Path) -> Path:
    """把 <stamp>.db + <stamp>.json 压成 <stamp>.zip，内部固定叫 unified.db / manifest.json。"""
    manifest_path = artifact.with_suffix(".json")
    zip_path = artifact.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(artifact, "unified.db")
        zf.write(manifest_path, "manifest.json")
    artifact.unlink()
    manifest_path.unlink()
    return zip_path


def prune(out_dir: Path, keep: int) -> list[str]:
    """按份数清理本工具产出的备份；pre-restore 快照用同一预算单独轮转。"""
    removed: list[str] = []
    groups: dict[str, list[Path]] = {}
    for item in sorted(Path(out_dir).iterdir()):
        match = ARTIFACT_RE.match(item.name)
        if match and item.is_file():
            groups.setdefault(match.group(1), []).append(item)

    for stamp, files in sorted(groups.items(), reverse=True)[keep:]:
        sibling = Path(out_dir) / f"unified-{stamp}.json"
        for file in files + ([sibling] if sibling.exists() else []):
            file.unlink()
            removed.append(file.name)

    pre_dirs = sorted(
        (item for item in Path(out_dir).iterdir() if item.is_dir() and PRE_BACKUP_RE.match(item.name)),
        reverse=True,
    )
    for stale in pre_dirs[keep:]:
        shutil.rmtree(stale)
        removed.append(stale.name)
    return removed


def pick_artifact(out_dir: Path) -> Path:
    found = [item for item in Path(out_dir).iterdir() if ARTIFACT_RE.match(item.name) and item.is_file()]
    if not found:
        raise BackupError(f"no backup artifact in {out_dir}")
    return max(found, key=lambda p: p.name)


def load_artifact(artifact: Path) -> tuple[Path, Path, Path | None]:
    """返回 (db 文件, manifest 文件, 需要清理的临时目录)。"""
    artifact = Path(artifact)
    if artifact.is_dir():
        db = artifact / "unified.db"
        if not db.is_file():
            candidates = sorted(artifact.glob("*.db"))
            if not candidates:
                raise BackupError(f"no .db inside {artifact}")
            db = candidates[0]
        return db, artifact / "manifest.json", None

    if artifact.suffix.lower() == ".zip":
        tmp = Path(tempfile.mkdtemp(prefix="u2a-restore-"))
        try:
            with zipfile.ZipFile(artifact) as zf:
                zf.extractall(tmp)
        except zipfile.BadZipFile as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            raise BackupError(f"backup zip is unreadable: {exc}") from exc
        db = tmp / "unified.db"
        if not db.is_file():
            candidates = sorted(tmp.glob("*.db"))
            if not candidates:
                shutil.rmtree(tmp, ignore_errors=True)
                raise BackupError("zip contains no .db")
            db = candidates[0]
        return db, tmp / "manifest.json", tmp

    if not artifact.is_file():
        raise BackupError(f"backup artifact not found: {artifact}")
    return artifact, artifact.with_suffix(".json"), None


def verify_artifact(artifact: Path, skip_hash: bool = False) -> tuple[Path, dict, Path | None]:
    db, manifest_path, cleanup = load_artifact(artifact)
    if not manifest_path.is_file():
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
        raise BackupError(
            f"manifest missing for {Path(artifact).name}; refusing to restore an unverifiable file "
            "(legacy backups need --skip-verify, which is unsafe)"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(manifest.get("manifest_version", 0)) > MANIFEST_VERSION:
        raise BackupError("manifest version is newer than this tool")

    record = manifest.get("database") or {}
    problems: list[str] = []
    actual_size = db.stat().st_size
    if record.get("size_bytes") != actual_size:
        problems.append(f"size mismatch: manifest={record.get('size_bytes')} actual={actual_size}")
    if not skip_hash:
        actual_hash = sha256_of(db)
        if record.get("sha256") != actual_hash:
            problems.append(f"sha256 mismatch: manifest={record.get('sha256')} actual={actual_hash}")
    if not skip_hash and str(manifest.get("database", {}).get("integrity_check", "")) != "ok":
        problems.append(f"snapshot integrity_check failed: {record.get('integrity_check')}")

    if problems:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
        raise BackupError("; ".join(problems))
    return db, manifest, cleanup


def pre_backup(db: Path, backups_dir: Path) -> Path:
    """恢复前留存当前库：原样字节拷贝（含 -wal/-shm），不解析内容。

    这里不能用 VACUUM INTO 之类的快照工具，因为「当前库已经坏了」正是最常见的
    恢复场景；坏库上的在线快照会直接失败，而我们要的是留证与回退能力。
    前提是服务已停止，读清单里会写明这一点。
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
        dst = target / f"{db.name}{suffix}"
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            raise BackupError(
                f"cannot copy current database ({src.name}): {exc}. "
                "Stop the control plane first, then retry (Windows locks open db files)"
            ) from exc
        saved[dst.name] = {"size_bytes": dst.stat().st_size, "sha256": sha256_of(dst)}

    record = {
        "kind": "pre-restore",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "reason": "restore target snapshot taken before replacing the database",
        "note": "raw byte copy of db/-wal/-shm; requires the service to be stopped",
        "files": saved,
    }
    (target / "pre-restore.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def read_counts(path: Path) -> dict[str, int] | None:
    """尽力而为地读行数：目标库坏掉时返回 None，不中断恢复流程。"""
    try:
        conn = open_db(path)
        try:
            return table_counts(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def selfcheck(db: Path, manifest: dict) -> dict:
    """只读自检：能 SELECT 1、关键表都在、行数与清单一致、至少有一个管理员。"""
    checks: dict[str, object] = {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        conn.execute("SELECT 1").fetchone()
        checks["select_1"] = True
        counts = table_counts(conn)
        missing = [name for name in REQUIRED_TABLES if name not in counts]
        checks["required_tables"] = {"ok": not missing, "missing": missing}
        expected = manifest.get("tables") or {}
        drift = {
            name: {"manifest": int(want), "actual": int(counts.get(name, 0))}
            for name, want in expected.items()
            if int(counts.get(name, 0)) != int(want)
        }
        checks["row_counts"] = {"ok": not drift, "drift": drift}
        checks["users_nonzero"] = {"ok": int(counts.get("users", 0)) >= 1, "count": int(counts.get("users", 0))}
        checks["integrity_check"] = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()

    ok = (
        checks["select_1"]
        and checks["required_tables"]["ok"]
        and checks["row_counts"]["ok"]
        and checks["users_nonzero"]["ok"]
        and checks["integrity_check"] == "ok"
    )
    return {"ok": ok, "counts": counts, "checks": checks}


def wait_ready(url: str, timeout_sec: int, interval_sec: float = 0.5) -> dict:
    """轮询 /api/health/ready，database 子检查 ok 才算通过。503 也要读 body。"""
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
        last = {
            "ok": bool(database.get("ok")),
            "status": status,
            "database": database,
            "unhealthy": payload.get("unhealthy"),
            "providers_ok": bool(((payload.get("checks") or {}).get("providers") or {}).get("ok")),
        }
        if last["ok"]:
            return last
        time.sleep(interval_sec)
    return last


def print_counts(title: str, counts: dict[str, int] | None) -> None:
    if counts is None:
        print(f"{title}: <unreadable>")
        return
    rows = ", ".join(f"{name}={value}" for name, value in sorted(counts.items()))
    print(f"{title}: {rows}")


def cmd_snapshot(args) -> int:
    method = snapshot(Path(args.src), Path(args.dest))
    print(f"snapshot: {args.dest} (method={method})")
    return 0


def cmd_finalize(args) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact = Path(args.artifact)
    if not artifact.is_file():
        raise BackupError(f"artifact not found: {artifact}")
    manifest = build_manifest(artifact, args.method, args.source_label)
    manifest_path = write_manifest(artifact, manifest)
    if args.compress:
        artifact = compress_artifact(artifact)
    removed = prune(out_dir, args.keep)
    print(f"backup: {artifact}")
    print(f"manifest: {artifact.with_suffix('.json') if artifact.suffix == '.db' else 'manifest.json (inside zip)'}")
    print(
        f"database: size={manifest['database']['size_bytes']} sha256={manifest['database']['sha256']} "
        f"integrity={manifest['database']['integrity_check']} user_version={manifest['database']['user_version']} "
        f"schema_version={manifest['database']['schema_version']}"
    )
    print_counts("rows", manifest["tables"])
    print(f"tables: {manifest['totals']['tables']} total_rows={manifest['totals']['rows']}")
    if removed:
        print(f"pruned: {', '.join(removed)} (keep={args.keep})")
    return 0


def cmd_make(args) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    for _ in range(5):
        artifact = out_dir / f"unified-{stamp}.db"
        if not artifact.exists():
            break
        time.sleep(1)
        stamp = now_stamp()
    else:
        raise BackupError("cannot allocate a unique backup name, retry in a second")

    method = snapshot(Path(args.src), artifact)
    manifest = build_manifest(artifact, method, args.source_label)
    write_manifest(artifact, manifest)
    if args.compress:
        artifact = compress_artifact(artifact)
    removed = prune(out_dir, args.keep)

    print(f"backup: {artifact}")
    print(
        f"database: size={manifest['database']['size_bytes']} sha256={manifest['database']['sha256']} "
        f"integrity={manifest['database']['integrity_check']} user_version={manifest['database']['user_version']} "
        f"schema_version={manifest['database']['schema_version']} method={method}"
    )
    print_counts("rows", manifest["tables"])
    print(f"tables: {manifest['totals']['tables']} total_rows={manifest['totals']['rows']}")
    if removed:
        print(f"pruned: {', '.join(removed)} (keep={args.keep})")
    return 0


def cmd_inspect(args) -> int:
    db = Path(args.db)
    if not db.is_file():
        raise BackupError(f"database not found: {db}")
    stats = collect_stats(db)
    print(
        f"database: size={db.stat().st_size} integrity={stats['integrity_check']} "
        f"user_version={stats['user_version']} schema_version={stats['schema_version']} "
        f"journal_mode={stats['journal_mode']}"
    )
    print_counts("rows", stats["tables"])
    return 0


def cmd_latest(args) -> int:
    print(pick_artifact(Path(args.out_dir)))
    return 0


def cmd_verify(args) -> int:
    db, manifest, cleanup = verify_artifact(Path(args.artifact), args.skip_hash)
    try:
        print(f"verified: {args.artifact}")
        print(f"sha256: {manifest['database']['sha256']}")
        print(f"created_at: {manifest['created_at']} integrity={manifest['database']['integrity_check']}")
        print_counts("manifest rows", manifest["tables"])
        return 0
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


def cmd_selfcheck(args) -> int:
    db = Path(args.db)
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = selfcheck(db, manifest)
    print_counts("current rows", result["counts"])
    print_counts("manifest rows", manifest.get("tables") or {})
    print(f"selfcheck: ok={result['ok']} checks={json.dumps(result['checks'], ensure_ascii=False, sort_keys=True)}")
    return 0 if result["ok"] else EXIT_VERIFY


def cmd_restore(args) -> int:
    artifact = Path(args.artifact)
    db = Path(args.db)
    backups_dir = Path(args.backups)
    stamp = now_stamp()

    verified_db, manifest, cleanup = verify_artifact(artifact, args.skip_verify)
    try:
        print(f"artifact: {artifact}")
        print(f"artifact sha256: {manifest['database']['sha256']} integrity={manifest['database']['integrity_check']}")
        print(f"target: {db}")

        before = read_counts(db) if db.is_file() else None
        print_counts("rows before restore", before)

        pre_dir = None
        if db.is_file():
            pre_dir = pre_backup(db, backups_dir)
            print(f"pre-restore snapshot: {pre_dir}")

        db.parent.mkdir(parents=True, exist_ok=True)
        staged = db.parent / f".{db.name}.restore-{stamp}.tmp"
        shutil.copy2(verified_db, staged)
        try:
            os.chmod(staged, 0o600)
        except OSError:
            pass

        replaced = None
        if db.is_file():
            replaced = db.parent / f"{db.name}.replaced-{stamp}"
            os.replace(db, replaced)
        os.replace(staged, db)
        # 旧库的 WAL/SHM 必须清掉：留着只会让新库读到别人的半页
        for suffix in ("-wal", "-shm"):
            stale = Path(f"{db}{suffix}")
            if stale.exists():
                stale.unlink()

        result = selfcheck(db, manifest)
        print_counts("rows after restore", result["counts"])
        print(f"selfcheck: ok={result['ok']} checks={json.dumps(result['checks'], ensure_ascii=False, sort_keys=True)}")

        if not result["ok"]:
            if replaced and replaced.exists():
                os.replace(replaced, db)
                print("rolled back: selfcheck failed, original database put back")
            raise BackupError("restored database failed selfcheck; target was rolled back")

        if replaced and replaced.exists():
            replaced.unlink()
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)

    if args.ready_url:
        ready = wait_ready(args.ready_url, args.ready_timeout)
        print(f"ready: {json.dumps(ready, ensure_ascii=False, sort_keys=True)}")
        if not ready["ok"]:
            raise BackupError("service is not ready: database check did not report ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="unified-2api sqlite backup/restore core")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("snapshot", help="write a consistent snapshot of a database")
    p.add_argument("--src", required=True)
    p.add_argument("--dest", required=True)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("finalize", help="add manifest, optional compression and retention to an existing snapshot")
    p.add_argument("--artifact", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--keep", type=int, default=14)
    p.add_argument("--compress", action="store_true")
    p.add_argument("--method", default="external")
    p.add_argument("--source-label", default="unified.db")
    p.set_defaults(func=cmd_finalize)

    p = sub.add_parser("make", help="snapshot + manifest + compression + retention in one shot")
    p.add_argument("--src", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--keep", type=int, default=14)
    p.add_argument("--compress", action="store_true")
    p.add_argument("--source-label", default="unified.db")
    p.set_defaults(func=cmd_make)

    p = sub.add_parser("inspect", help="read-only stats of a database")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("latest", help="print the newest backup artifact in a directory")
    p.add_argument("--out-dir", required=True)
    p.set_defaults(func=cmd_latest)

    p = sub.add_parser("verify", help="check an artifact against its manifest")
    p.add_argument("--artifact", required=True)
    p.add_argument("--skip-hash", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("selfcheck", help="read-only post-restore assertions")
    p.add_argument("--db", required=True)
    p.add_argument("--manifest", required=True)
    p.set_defaults(func=cmd_selfcheck)

    p = sub.add_parser("restore", help="verify, snapshot current db, replace it, then selfcheck")
    p.add_argument("--artifact", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--backups", required=True)
    p.add_argument("--ready-url", default="")
    p.add_argument("--ready-timeout", type=int, default=20)
    p.add_argument("--skip-verify", action="store_true", help="emergency only: legacy artifact without manifest")
    p.set_defaults(func=cmd_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"make", "finalize"} and args.keep < 1:
        print("error: keep must be >= 1", file=sys.stderr)
        return 1
    try:
        return int(args.func(args) or 0)
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_VERIFY if args.command in {"verify", "restore"} else 1
    except sqlite3.Error as exc:
        print(f"error: sqlite: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
