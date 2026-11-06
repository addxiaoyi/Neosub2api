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

上游密钥（settings.provider_config 里的 api_key 等、provider_credentials.secret）
在归档里一律不是明文：配了 UNIFIED_BACKUP_ENCRYPTION_KEY 就逐字段 AES-256-GCM
封装，恢复时保持封装态原样落库（解封只发生在应用读取时）；没配就清空这些字段
并在 stderr 与清单里明确记录，恢复后提示重新粘贴，绝不静默产出带明文密钥的归档。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
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
EXIT_BUSY = 3

# 上游密钥封装：密钥只从环境变量读，与远端备份共用同一把（变量含义已在 .env.example 写明）
SECRET_KEY_ENV = "UNIFIED_BACKUP_ENCRYPTION_KEY"
# 会被逐字段封装/清空的 settings 键：provider_config 是界面写入的上游密钥主体，
# upstream_config 里的 upstash/同名密钥字段同理
SECRET_SETTING_KEYS = ("provider_config", "upstream_config")
SECRET_CREDENTIAL_TABLE = "provider_credentials"
# 与 apps/control/app/routers/settings.py 的 SECRET_KEYS 口径一致，另加几个实际出现在
# provider_config.options 里的名字（sentinel_token / extra_keys / cookies）
SECRET_FIELD_NAMES = frozenset({
    "api_key", "apikey", "token", "secret", "password",
    "access_token", "refresh_token", "cookie", "cookies", "authorization",
    "client_secret", "sentinel_token", "extra_keys",
})
SECRET_FIELD_SUFFIXES = ("_key", "_secret", "_token", "_password")


class BackupError(RuntimeError):
    """可预期的运维错误：给一句结论，不打堆栈。"""


class TargetBusyError(BackupError):
    """目标库看起来还在被服务使用：拒绝整体替换，单独给一个退出码区分于校验失败。"""


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


# ---------------------------------------------------------------- 上游密钥封装


_crypto_module = None


def backup_crypto():
    """按文件路径加载控制面的加密实现，避免把整个 app 包（及其依赖）拖进来。"""
    global _crypto_module  # noqa: PLW0603
    if _crypto_module is None:
        source = repo_root() / "apps" / "control" / "app" / "services" / "backup_crypto.py"
        if not source.is_file():
            raise BackupError(f"加密组件缺失：找不到 {source}，无法处理归档里的上游密钥")
        spec = importlib.util.spec_from_file_location("unified_backup_crypto", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        _crypto_module = module
    return _crypto_module


def _is_secret_field(name: str) -> bool:
    lowered = name.strip().lower()
    return lowered in SECRET_FIELD_NAMES or lowered.endswith(SECRET_FIELD_SUFFIXES)


def _scrub_secret_value(value, scrub, tally: dict, row_key: str) -> tuple[object, int]:
    """只处理「名字像密钥」字段的值；列表（如 options.extra_keys）逐元素处理。"""
    if isinstance(value, str):
        if not value.strip():
            return value, 0
        stored, bucket = scrub(value)
        tally[bucket][row_key] += 1
        return stored, 1
    if isinstance(value, list):
        out: list = []
        count = 0
        for item in value:
            new_item, changed = _scrub_secret_value(item, scrub, tally, row_key)
            out.append(new_item)
            count += changed
        return out, count
    if isinstance(value, dict):
        return _scrub_secret_fields(value, scrub, tally, row_key)
    return value, 0


def _scrub_secret_fields(node, scrub, tally: dict, row_key: str) -> tuple[object, int]:
    if isinstance(node, dict):
        out: dict = {}
        count = 0
        for name, value in node.items():
            if isinstance(name, str) and _is_secret_field(name):
                new_value, changed = _scrub_secret_value(value, scrub, tally, row_key)
            else:
                new_value, changed = _scrub_secret_fields(value, scrub, tally, row_key)
            out[name] = new_value
            count += changed
        return out, count
    if isinstance(node, list):
        out_list: list = []
        count = 0
        for item in node:
            new_item, changed = _scrub_secret_fields(item, scrub, tally, row_key)
            out_list.append(new_item)
            count += changed
        return out_list, count
    return node, 0


def _probe_sealed(node, is_sealed, on_sealed) -> int:
    """按内容前缀找出封装态；只读遍历，不改写任何节点，密文交给回调逐个校验。"""
    if isinstance(node, dict):
        return sum(_probe_sealed(value, is_sealed, on_sealed) for value in node.values())
    if isinstance(node, list):
        return sum(_probe_sealed(item, is_sealed, on_sealed) for item in node)
    if isinstance(node, str) and is_sealed(node):
        on_sealed(node)
        return 1
    return 0


def protect_secrets(conn: sqlite3.Connection) -> dict:
    """归档前处理上游密钥，返回要写进 manifest 的 secrets 记录。

    三类结果分开计数：`already_sealed`（库里本来就是封装态）、`sealed_now`（本次封装）、
    `not_sealed`（没配密钥被清空）。只要有一行受保护就不报 `policy=none`，避免
    「库已整体封存」的归档被误报成未处理。

    配了 UNIFIED_BACKUP_ENCRYPTION_KEY：逐字段 AES-256-GCM 封装，恢复时保持封装态。
    没配：清空这些字段（行还在，界面能看出「这条凭据需要重新粘贴」）并在 stderr 显式告警——
    宁可恢复后要人重贴，也不把明文密钥写进归档。
    """
    crypto = backup_crypto()
    key = None
    reason = ""
    try:
        key = crypto.load_key()
    except Exception as exc:  # 缺密钥/格式坏都按「未配置」处理，行为一致
        reason = str(exc)

    def scrub(text: str) -> tuple[str, str]:
        if crypto.is_sealed(text):
            return text, "already_sealed"
        if key is None:
            return "", "not_sealed"
        return crypto.seal_text(text, key), "sealed_now"

    info: dict = {
        "policy": "none",
        "algorithm": "aes-256-gcm",
        "key_env": SECRET_KEY_ENV,
        "settings_keys": [],
        "already_sealed": {"settings_fields": 0, "provider_credentials_rows": 0},
        "sealed_now": {"settings_fields": 0, "provider_credentials_rows": 0},
        "not_sealed": {"settings_fields": 0, "provider_credentials_rows": 0},
    }
    if reason:
        info["reason"] = reason

    for setting_key in SECRET_SETTING_KEYS:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (setting_key,)).fetchone()
        if row is None:
            continue
        try:
            node = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(node, dict):
            continue
        cleaned, touched = _scrub_secret_fields(node, scrub, info, "settings_fields")
        if not touched:
            continue
        # 全是已封存时 cleaned 与 node 相等，不必回写
        if cleaned != node:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = ?",
                (json.dumps(cleaned, ensure_ascii=False), setting_key),
            )
        info["settings_keys"].append(setting_key)

    if has_table(conn, SECRET_CREDENTIAL_TABLE):
        rows = conn.execute(
            f"SELECT id, secret FROM {SECRET_CREDENTIAL_TABLE} WHERE secret <> ''"
        ).fetchall()
        for row in rows:
            text = row["secret"]
            if not isinstance(text, str) or not text.strip():
                continue
            stored, bucket = scrub(text)
            info[bucket]["provider_credentials_rows"] += 1
            if stored != text:
                conn.execute(
                    f"UPDATE {SECRET_CREDENTIAL_TABLE} SET secret = ? WHERE id = ?",
                    (stored, row["id"]),
                )
    conn.commit()

    # UPDATE 只改写页内内容，旧明文页可能留在 freelist；VACUUM 重建后归档里才真的搜不到。
    # 快照副本统一落成 delete 日志模式，免得上游密钥加密后的内容还留在 -wal 里。
    mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if mode != "delete":
        conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("VACUUM")

    if sum(info["already_sealed"].values()) + sum(info["sealed_now"].values()):
        info["policy"] = "encrypted"
    elif sum(info["not_sealed"].values()):
        info["policy"] = "stripped"
    # 旧字段保持「本次封装」口径，外部脚本仍可只读这两个键
    info["settings_fields"] = info["sealed_now"]["settings_fields"]
    info["provider_credentials_rows"] = info["sealed_now"]["provider_credentials_rows"]

    if key is None and sum(info["not_sealed"].values()):
        print(
            f"warning: 未配置 {SECRET_KEY_ENV}（或不是 32 字节 base64），归档已清空上游密钥字段："
            "恢复后需在「服务商」/「凭据」页重新粘贴，否则 provider 不可用。"
            "需要原样还原时请先配置该环境变量再备份。",
            file=sys.stderr,
        )
    return info


def verify_sealed_secrets(db: Path, manifest: dict) -> dict:
    """恢复前确认归档里的封装态密钥能被当前密钥解开；不改写解压副本。

    封装态原样落库，解封只发生在应用读取时。缺密钥/密钥不匹配都在动目标库之前失败，
    不会出现「恢复一次就变成明文」或静默降级。
    """
    raw = manifest.get("secrets")
    info = raw if isinstance(raw, dict) else {}
    declared = str(info.get("policy") or "none")
    crypto = backup_crypto()
    counts = {"settings_fields": 0, "provider_credentials_rows": 0}
    sealed: list[str] = []

    conn = open_db(db, readonly=True)
    try:
        for setting_key in SECRET_SETTING_KEYS:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (setting_key,)
            ).fetchone()
            if row is None:
                continue
            try:
                node = json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(node, dict):
                continue
            counts["settings_fields"] += _probe_sealed(node, crypto.is_sealed, sealed.append)

        if has_table(conn, SECRET_CREDENTIAL_TABLE):
            for row in conn.execute(f"SELECT secret FROM {SECRET_CREDENTIAL_TABLE}"):
                if crypto.is_sealed(row["secret"]):
                    sealed.append(row["secret"])
                    counts["provider_credentials_rows"] += 1
    finally:
        conn.close()

    if not sealed:
        return {
            "policy": declared,
            "verified_fields": 0,
            "provider_credentials_rows": 0,
            "sealed": counts,
        }

    try:
        key = crypto.load_key()
    except Exception as exc:
        raise BackupError(
            f"归档里的上游密钥已用 {SECRET_KEY_ENV} 加密封装，恢复需要同一把密钥（当前不可用：{exc}）。"
            "配好环境变量后重试；拿不到密钥就只能放弃还原，恢复后到「服务商」/「凭据」页重新粘贴。"
        ) from exc

    try:
        for token in sealed:
            crypto.unseal_text(token, key)
    except (crypto.CryptoVerifyError, ValueError) as exc:
        raise BackupError(
            f"归档里的上游密钥用当前 {SECRET_KEY_ENV} 解不开：{exc}。"
            "密钥不匹配时不要继续恢复，否则 provider 会拿到无法使用的密钥"
        ) from exc

    return {
        "policy": "encrypted",
        "verified_fields": counts["settings_fields"],
        "provider_credentials_rows": counts["provider_credentials_rows"],
        "sealed": counts,
    }


def unseal_secrets(db: Path, manifest: dict) -> dict:
    """兼容面板路由的旧入口（apps/control/app/routers/backup.py 仍按此名调用）。

    行为已改为「只校验、不改写」；新代码请直接用 verify_sealed_secrets。
    """
    return verify_sealed_secrets(db, manifest)


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
                   root: Path, secrets: dict) -> dict:
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
        "secrets": secrets,
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
            secrets_info = protect_secrets(conn)
        finally:
            conn.close()

        members = copy_repo_files(root, staging, collect_repo_files(root))
        manifest = build_manifest(
            source_label=source_label, method=method, db_file=db_file, members=members,
            demo=demo, root=root, secrets=secrets_info,
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
    print(
        f"secrets: policy={secrets_info['policy']} "
        f"already_sealed={_bucket_text(secrets_info['already_sealed'])} "
        f"sealed_now={_bucket_text(secrets_info['sealed_now'])} "
        f"not_sealed={_bucket_text(secrets_info['not_sealed'])} key_env={SECRET_KEY_ENV}"
    )
    print(f"config files: {len(members)} ({', '.join(item['member'] for item in members)})" if members
          else "config files: 0")
    print(f"app_version={manifest['app_version']}")
    if removed:
        print(f"pruned: {', '.join(removed)} (keep={keep})")
    return target


def _rows_line(title: str, counts: dict[str, int]) -> str:
    return f"{title}: " + ", ".join(f"{name}={value}" for name, value in sorted(counts.items()))


def _bucket_text(bucket: object) -> str:
    """secrets 的分桶计数压成一行：fields=本次涉及字段数/credentials=涉及行数。"""
    data = bucket if isinstance(bucket, dict) else {}
    return f"fields={data.get('settings_fields', 0)}/credentials={data.get('provider_credentials_rows', 0)}"


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
    secrets = manifest.get("secrets")
    if isinstance(secrets, dict):
        print(
            f"secrets: policy={secrets.get('policy') or 'none'} "
            f"already_sealed={_bucket_text(secrets.get('already_sealed'))} "
            f"sealed_now={_bucket_text(secrets.get('sealed_now'))} "
            f"not_sealed={_bucket_text(secrets.get('not_sealed'))} "
            f"key_env={secrets.get('key_env')}"
        )
    else:
        print("secrets: policy=plaintext (旧版本归档，未做上游密钥封装)")
    print(f"config files: {len(manifest.get('files') or [])}")


# ---------------------------------------------------------------- 恢复


def _handle_held(path: Path) -> bool:
    """Windows：以「不允许任何人共享」的方式打开文件，失败于占用类错误即说明还有别人拿着句柄。

    SQLite 在 Windows 上打开库与 -wal/-shm 时用的是 FILE_SHARE_READ|WRITE，
    所以控制面只要还连着库，这个探测就会被拒绝；文件根本不存在（错误 2）不算占用。
    macOS/Linux 没有强制共享语义，这里恒返回 False，靠下面的 SQLite 写锁探测兜底。
    """
    if os.name != "nt" or not path.exists():
        return False
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    ERROR_NOT_FOUND = 2
    SHARING_VIOLATION = 32
    LOCK_VIOLATION = 33

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    )
    handle = kernel32.CreateFileW(
        str(path), GENERIC_READ, 0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
    )
    invalid = ctypes.c_void_p(-1).value
    if handle != invalid:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    error = ctypes.get_last_error()
    if error in (ERROR_NOT_FOUND,):
        return False
    return error in (SHARING_VIOLATION, LOCK_VIOLATION)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_owner(db: Path) -> str | None:
    """data 目录的 *.pid 与仓库 .run/start.json 里记的控制面进程还活着就返回原因。

    控制面是每请求开关连接，空闲时没有任何文件句柄，只能靠它自己留下的 pid 线索；
    两种来源都没有时交由下面的句柄/写锁探测兜底。
    """
    for candidate in sorted(db.parent.glob("*.pid")):
        try:
            pid = int(candidate.read_text(encoding="utf-8").strip().split()[0])
        except (OSError, ValueError, IndexError):
            continue
        if _pid_alive(pid):
            return f"{candidate.name} 指向的控制面进程 {pid} 仍在运行"

    state = repo_root() / ".run" / "start.json"
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
        pid = int(data.get("api_pid") or 0)
        # 状态文件不记 db 路径，只对仓库默认数据目录负责，别误伤指向别处的恢复
        default_db = (Path(data.get("root") or repo_root()) / "data" / "unified.db").resolve()
        if pid and db.resolve() == default_db and _pid_alive(pid):
            return f"scripts/start 起的控制面进程 {pid} 仍在运行（.run/start.json）"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def _wal_leftover(db: Path) -> bool:
    """有非空 -wal 说明上个实例没正常退出（正常关闭会 checkpoint 并删掉 -wal/-shm）。

    这时 -wal 里可能还有已提交但未落主库的事务，直接换文件等于静默丢数据，
    所以按「可疑占用」处理，要人显式确认。
    """
    wal = Path(f"{db}-wal")
    try:
        return wal.is_file() and wal.stat().st_size > 0
    except OSError:
        return False


def target_in_use(db: Path) -> str | None:
    """返回「目标库仍被占用」的人话原因；判断不了就返回 None，不拦正常恢复。"""
    for item in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
        if _handle_held(item):
            return f"{item.name} 仍被另一个进程打开（Windows 文件共享冲突）"

    owner = _lock_owner(db)
    if owner:
        return owner

    if _wal_leftover(db):
        return f"存在非空的 {db.name}-wal，上个实例可能没有正常退出，残留事务可能被丢弃"

    # 句柄探测抓不到「进程闲置、按请求开关连接」的场景，再看一眼有没有未结束的写事务：
    # 这时换文件，对方会把自己的旧事务写到新库上。
    conn = None
    try:
        conn = sqlite3.connect(str(db), timeout=0.2)
        conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            return f"数据库上还有未结束的写事务（{exc}）"
    except sqlite3.Error:
        # 库已损坏时恢复恰恰是来救它的，探测失败不构成拦截理由
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return None


def assert_restore_target_idle(db: Path, *, force: bool) -> None:
    if force or not db.is_file():
        return
    reason = target_in_use(db)
    if reason is None:
        return
    raise TargetBusyError(
        f"目标库还在被使用：{reason}。CLI 恢复是整文件替换，服务在跑时替换会失败或让写入错乱。\n"
        f"  先停控制面再重试：bash scripts/start.sh --stop / powershell -File scripts\\start.ps1 -Stop；\n"
        f"  容器部署用 docker compose stop control（或 docker stop <容器名>）。\n"
        f"  不停服请改用管理界面的在线恢复：POST /api/system/restore。\n"
        f"  确认没有进程在用、仍要强行替换，加 --force（Windows 上仍可能因文件占用失败）。"
    )


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

    # 换文件之前先确认没有人在用：这一步拦的是「服务还在跑就恢复」，
    # 与归档校验无关，所以放在最前面，避免先花时间解压再失败。
    assert_restore_target_idle(db, force=bool(getattr(args, "force", False)))
    if getattr(args, "force", False) and db.is_file():
        print("warning: 已跳过占用检查（--force），替换时仍可能被系统文件锁拒绝", file=sys.stderr)

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

        # 只校验能否解开；不改写解压副本，封装态原样落库
        secrets_state = verify_sealed_secrets(db_file, manifest)
        if secrets_state["policy"] == "encrypted":
            print(
                f"secrets: sealed fields={secrets_state['verified_fields']} "
                f"credentials={secrets_state['provider_credentials_rows']} 已保留封装态（应用读取时解封）"
            )
        elif "secrets" not in manifest:
            print(
                "warning: 该归档由未做密钥封装的旧版本生成，内含明文上游密钥，请按敏感文件保管",
                file=sys.stderr,
            )
        # 混合归档（部分已封存、部分没配密钥被清空）也要把清空这件事说出来
        secrets_meta = manifest.get("secrets")
        not_sealed = secrets_meta.get("not_sealed") if isinstance(secrets_meta, dict) else None
        if isinstance(not_sealed, dict) and sum(not_sealed.values()):
            print(
                "warning: 归档里的上游密钥在备份时已被清空（当时未配置 "
                f"{SECRET_KEY_ENV}），恢复后需在「服务商」/「凭据」页重新粘贴，否则 provider 不可用",
                file=sys.stderr,
            )

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
    p.add_argument(
        "--force", action="store_true",
        help="skip the in-use guard and replace the database even if a service may still hold it",
    )
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
    except TargetBusyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BUSY
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
