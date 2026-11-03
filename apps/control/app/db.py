import json
import os
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'viewer',
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS providers(
  id TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  base_url TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts(
  file TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  uid TEXT NOT NULL,
  nickname TEXT NOT NULL DEFAULT '',
  enterprise_id TEXT NOT NULL DEFAULT '',
  realm TEXT NOT NULL DEFAULT 'cn',
  expires_at INTEGER NOT NULL DEFAULT 0,
  credits REAL,
  note TEXT NOT NULL DEFAULT '',
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  last_used_at INTEGER,
  -- 归属：users 的主键是 username（TEXT），所以这里存用户名而不是数字 id。
  -- NULL = 无主（老库存量），非 admin 不可见
  owner_user_id TEXT
);
CREATE TABLE IF NOT EXISTS request_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  provider TEXT,
  model TEXT,
  status INTEGER NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  stream INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS usage_records(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  key_id INTEGER,
  key_name TEXT NOT NULL DEFAULT '',
  provider TEXT,
  model TEXT,
  status INTEGER NOT NULL,
  stream INTEGER NOT NULL DEFAULT 0,
  usage_json TEXT NOT NULL DEFAULT '',
  latency_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sub2_groups(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  platform TEXT NOT NULL DEFAULT 'openai',
  quota_limit REAL NOT NULL DEFAULT 0,
  quota_used REAL NOT NULL DEFAULT 0,
  reset_at INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sub2_accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  upstream_base TEXT NOT NULL DEFAULT '',
  api_key TEXT NOT NULL DEFAULT '',
  weight INTEGER NOT NULL DEFAULT 1,
  quota_limit REAL NOT NULL DEFAULT 0,
  quota_used REAL NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  cooldown_until INTEGER NOT NULL DEFAULT 0,
  last_used_at INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sub2_accounts_group ON sub2_accounts(group_id, enabled, cooldown_until);
CREATE TABLE IF NOT EXISTS sub2_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  group_id TEXT NOT NULL,
  account_id INTEGER,
  model TEXT NOT NULL DEFAULT '',
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  status INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_sub2_ledger_group ON sub2_ledger(group_id, ts);
CREATE TABLE IF NOT EXISTS grok_tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL DEFAULT 'queued',
  count INTEGER NOT NULL DEFAULT 1,
  workers INTEGER NOT NULL DEFAULT 1,
  email_provider TEXT NOT NULL DEFAULT '',
  proxy TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  error TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  started_at INTEGER,
  finished_at INTEGER,
  next_run_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_grok_tasks_queue ON grok_tasks(status, next_run_at);

-- 统一凭据库：每家 provider 可存多条凭证，按 (sort_order, id) 顺序轮换取用
CREATE TABLE IF NOT EXISTS provider_credentials(
  id INTEGER PRIMARY KEY AUTOINCREMENT,      -- 行 id，API 与审计都以它为准
  provider TEXT NOT NULL,                    -- 适配器 id（对应 providers.id）
  label TEXT NOT NULL DEFAULT '',            -- 人可读名称，如「主账号 / 备用号」
  base_url TEXT NOT NULL DEFAULT '',         -- 留空则回落到 providers.base_url → env → spec 默认
  secret TEXT NOT NULL DEFAULT '',           -- 上游密钥（明文落库，理由见交付说明）
  enabled INTEGER NOT NULL DEFAULT 1,        -- 0 = 保留但暂不参与轮换
  sort_order INTEGER NOT NULL DEFAULT 0,     -- 越小越先取；同值按 id 升序
  note TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_credentials_order
  ON provider_credentials(provider, enabled, sort_order, id);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        _restrict(path.parent, 0o700)
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            # 轻量迁移：给已有库补列（CREATE TABLE IF NOT EXISTS 不会改旧表）
            _ensure_column(conn, "accounts", "meta_json", "TEXT NOT NULL DEFAULT '{}'")
            # 归属列：老库没有，启动时幂等补列；补完再建索引，避免旧库上索引先于列失败
            _ensure_column(conn, "api_keys", "owner_user_id", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_owner ON api_keys(owner_user_id)")
            _ensure_column(conn, "request_logs", "key_id", "INTEGER")
            _ensure_column(conn, "request_logs", "key_name", "TEXT NOT NULL DEFAULT ''")
            # F2 缺陷修复：空池拒绝也要留痕，给流水加可空的原因字段
            _ensure_column(conn, "sub2_ledger", "reason", "TEXT")
            # M2 起 codebuddy 默认指向管理端 7864（M1 默认是数据面 7863），只迁移没动过的存量
            conn.execute(
                "UPDATE providers SET base_url = REPLACE(base_url, ':7863', ':7864') "
                "WHERE id = 'codebuddy' AND base_url LIKE '%:7863%'"
            )
            # F1：三家占位桩改成主仓内置实现，清掉旧的本地 sidecar 地址（只迁默认值）
            conn.execute(
                "UPDATE providers SET base_url = 'https://opencode.ai/zen' "
                "WHERE id = 'opencode' AND base_url LIKE '%:7869%'"
            )
            conn.execute(
                "UPDATE providers SET base_url = '' WHERE id = 'sub2api' AND base_url LIKE '%:7870%'"
            )
            conn.execute(
                "UPDATE providers SET base_url = '' WHERE id = 'grok-register' AND base_url LIKE '%:7866%'"
            )
            conn.commit()
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # 库里存上游 key 与会话 token，文件只允许属主读写（S-05）
        for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            _restrict(candidate, 0o600)
        return conn


def _restrict(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        # Windows 不支持 POSIX 权限位；容器/主机侧由卷权限兜底
        pass


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_json_setting(conn: sqlite3.Connection, key: str, default: Any) -> Any:
    raw = get_setting(conn, key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def set_json_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    set_setting(conn, key, json.dumps(value, ensure_ascii=False))
