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
-- 2FA 登录挑战：账号密码通过但缺验证码时签发，一次性消费
CREATE TABLE IF NOT EXISTS login_challenges(
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0
);
-- 恢复码：一次性，hash 存；used_at 非空即失效
CREATE TABLE IF NOT EXISTS totp_recovery_codes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  used_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_totp_recovery_user ON totp_recovery_codes(username, used_at);
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
  created_at INTEGER NOT NULL,
  -- 归属（users.username）：与 api_keys.owner_user_id 同口径。
  -- NULL = 共享/无主，非 admin 不可见
  owner_user_id TEXT
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
  error TEXT,
  -- 运维排查标记：只服务 triage，不参与计费；NULL 表示未排查
  resolved_at INTEGER,
  resolved_by TEXT NOT NULL DEFAULT ''
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

-- ===== M3-1 消费侧计费域：只算钱、不拦人 =====
-- 价格表 append-only：改价只插新行，(provider, model, token_class, effective_from) 是版本键
CREATE TABLE IF NOT EXISTS price_books(
  id INTEGER PRIMARY KEY AUTOINCREMENT,            -- 行 id；billing_ledger 通过快照间接引用
  provider TEXT NOT NULL,                          -- 适配器 id；'*' = 通配（当前排他解析优先精确行）
  model TEXT NOT NULL,                             -- 解析后的实际模型名；'*' = 通配
  token_class TEXT NOT NULL CHECK (token_class IN ('input','output','cache_read','cache_write','reasoning')),
  price_micro_per_1m INTEGER NOT NULL CHECK (price_micro_per_1m >= 0), -- 每 1M token 的 micro-USD（1e-6 美元）
  currency TEXT NOT NULL DEFAULT 'USD',            -- M3 只落 USD，不做多币种
  effective_from INTEGER NOT NULL,                 -- 生效 epoch 秒；未来时间=定时生效
  created_by TEXT NOT NULL DEFAULT '',             -- 操作者用户名（审计）
  note TEXT NOT NULL DEFAULT '',                   -- 价格来源与口径说明
  UNIQUE (provider, model, token_class, effective_from)
);
CREATE INDEX IF NOT EXISTS idx_price_books_lookup ON price_books(token_class, provider, model, effective_from);

-- 每请求价格快照：准入时解析一次，整行冻结，ledger 只引用快照 id
CREATE TABLE IF NOT EXISTS price_book_snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  resolved_at INTEGER NOT NULL,                    -- 解析时间（= 请求准入时刻，不是落库时刻）
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  lines_json TEXT NOT NULL DEFAULT '{}'            -- {"input":{"price_id":1,"price_micro_per_1m":2500000},...}
);

-- 消费侧流水：每请求一行，金额已定格，可重算可审计
CREATE TABLE IF NOT EXISTS billing_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  key_id INTEGER,                                  -- 与 usage_records 同口径：匿名/失效密钥写 NULL
  key_name TEXT NOT NULL DEFAULT '',
  billing_account_id INTEGER,                      -- 冗余自 api_keys（M3-2 补列后回填），M3-1 恒 NULL
  provider TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  status INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,         -- OpenAI: prompt - cached；Anthropic: input_tokens
  output_tokens INTEGER NOT NULL DEFAULT 0,        -- 已扣除 reasoning 的可见输出
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  billed_micro INTEGER NOT NULL DEFAULT 0,         -- 定格金额 micro-USD，只增不改成负数（退款走负行）
  price_snapshot_id INTEGER,                       -- 指向 price_book_snapshots.id
  estimated INTEGER NOT NULL DEFAULT 0,            -- 1=金额不完整/为估值（命中未定价档）
  note TEXT NOT NULL DEFAULT '',                   -- usage_missing / unpriced:input,output / ...
  log_id INTEGER                                   -- 关联 request_logs.id（settle 写入），/api/logs 按此精确回填逐条金额
);
CREATE INDEX IF NOT EXISTS idx_billing_ledger_ts ON billing_ledger(ts);
-- 覆盖 unscoped 配额聚合：SUM(billed_micro) 不回表（实测乱序 20 万行 1978ms -> 22ms）
CREATE INDEX IF NOT EXISTS idx_billing_ledger_key ON billing_ledger(key_id, ts, billed_micro);
CREATE INDEX IF NOT EXISTS idx_billing_ledger_account ON billing_ledger(billing_account_id, ts);

-- ===== M3-2 配额域：按网关密钥（u2a_）设日/月额度，可再按 provider/模型收窄 =====
-- 金额与请求数都从 billing_ledger 聚合，不再维护第二套累加器
CREATE TABLE IF NOT EXISTS quota_policies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_id INTEGER NOT NULL,                        -- api_keys.id（调用方维度）
  provider TEXT NOT NULL DEFAULT '',             -- 空 = 不限 provider；否则精确匹配适配器 id
  model TEXT NOT NULL DEFAULT '',                -- 空 = 不限模型；否则精确匹配解析后的模型名
  window TEXT NOT NULL CHECK (window IN ('day','month')),
  limit_micro INTEGER NOT NULL DEFAULT 0,         -- 金额上限 micro-USD，0 = 不设
  limit_requests INTEGER NOT NULL DEFAULT 0,      -- 请求数上限，0 = 不设
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (key_id, provider, model, window)
);
CREATE INDEX IF NOT EXISTS idx_quota_policies_key ON quota_policies(key_id, enabled);

-- 阈值（百分比），同一策略可设多个，如 80 / 100
CREATE TABLE IF NOT EXISTS quota_alert_thresholds(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  policy_id INTEGER NOT NULL,
  percent INTEGER NOT NULL CHECK (percent >= 1),
  created_at INTEGER NOT NULL,
  UNIQUE (policy_id, percent)
);

-- 告警历史：独立表而不是 audit_logs，理由见路由模块 docstring
CREATE TABLE IF NOT EXISTS quota_alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  policy_id INTEGER NOT NULL,
  threshold_percent INTEGER NOT NULL,
  window TEXT NOT NULL,
  period_start INTEGER NOT NULL,                  -- 触发时所在周期的起点，周期翻转后自然允许新告警
  triggered_at INTEGER NOT NULL,
  metric TEXT NOT NULL DEFAULT '',                -- micro / requests，说明是哪类额度跨阈值
  used_micro INTEGER NOT NULL DEFAULT 0,
  limit_micro INTEGER NOT NULL DEFAULT 0,
  used_requests INTEGER NOT NULL DEFAULT 0,
  limit_requests INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'quota',              -- quota / provider_failure / usage_missing_ratio
  subject TEXT NOT NULL DEFAULT '',                -- 全局规则的对象（如 provider id），配额规则留空
  -- 状态机：open -> acked -> closed，closed 只能 reopen 回 open；
  -- silenced_until 到期后读取端自动按 open 呈现，状态列本身不写 'silenced'
  status TEXT NOT NULL DEFAULT 'open',
  acked_at INTEGER,
  closed_at INTEGER,
  silenced_until INTEGER NOT NULL DEFAULT 0,
  UNIQUE (policy_id, threshold_percent, period_start)
);
CREATE INDEX IF NOT EXISTS idx_quota_alerts_policy ON quota_alerts(policy_id, period_start);

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
-- 任务状态流转事件：任务详情抽屉按它逐条展示步骤；只记步骤/状态/原因，不落 proxy 与 payload
CREATE TABLE IF NOT EXISTS grok_task_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  step TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok',
  detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_grok_task_events_task ON grok_task_events(task_id, id);

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

-- 定时健康检测历史：只做可用性观测，不参与路由/计费/告警
CREATE TABLE IF NOT EXISTS provider_health_checks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  finished_at INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'unsupported')),
  latency_ms INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT ''          -- 已脱敏的错误摘要，绝不落上游原始 key
);
CREATE INDEX IF NOT EXISTS idx_provider_health_provider ON provider_health_checks(provider, id);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        _restrict(path.parent, 0o700)
        conn = self.connect()
        try:
            # journal_mode 是库文件头里的持久属性，建库时设一次即可；以前每条连接
            # 重复设一遍，在已 WAL 的库上是纯往返（实测 0.7ms/次，含首触 -shm）
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            # M3-1 的 price_books / price_book_snapshots / billing_ledger 是纯新表，
            # 靠 SCHEMA 里的 CREATE TABLE IF NOT EXISTS 自动补到已有库，不需要 ALTER。
            # 轻量迁移：给已有库补列（CREATE TABLE IF NOT EXISTS 不会改旧表）
            _ensure_column(conn, "accounts", "meta_json", "TEXT NOT NULL DEFAULT '{}'")
            # 用户账号体系：注册邮箱唯一、2FA 状态与登录失败冷却计数。
            # 均为新增列，老库补列时只写默认值，不改动已有行的业务字段。
            _ensure_column(conn, "users", "email", "TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")
            _ensure_column(conn, "users", "totp_secret", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "users", "totp_pending_secret", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "users", "totp_enabled", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "users", "totp_last_counter", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "users", "failed_login_count", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "users", "last_failed_at", "INTEGER NOT NULL DEFAULT 0")
            # 注册口径统一：历史自助注册账号的 role='user' 与 viewer 权限等价，启动时幂等映射；只匹配 user，不碰 admin
            conn.execute("UPDATE users SET role = 'viewer' WHERE role = 'user'")
            # 账号归属列：老库没有，启动时幂等补列；非 admin 只看 owner_user_id = 自己的行
            _ensure_column(conn, "accounts", "owner_user_id", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts(owner_user_id)")
            # 归属列：老库没有，启动时幂等补列；补完再建索引，避免旧库上索引先于列失败
            _ensure_column(conn, "api_keys", "owner_user_id", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_owner ON api_keys(owner_user_id)")
            _ensure_column(conn, "request_logs", "key_id", "INTEGER")
            _ensure_column(conn, "request_logs", "key_name", "TEXT NOT NULL DEFAULT ''")
            # 前缀写时固化：删 key 后历史日志仍要能回答「这条请求是哪个 key 发的」；
            # 老行为 NULL，读取端回退 join，两边都拿不到才给 null
            _ensure_column(conn, "request_logs", "key_prefix", "TEXT")
            # 失败限流按 (key_id, ts) 聚合 request_logs，索引让窗口查询不退化成全表扫描
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_key_ts ON request_logs(key_id, ts)")
            # 告警规则扩展：老库补 kind/subject 列，已有配额告警默认 kind='quota'
            _ensure_column(conn, "quota_alerts", "kind", "TEXT NOT NULL DEFAULT 'quota'")
            _ensure_column(conn, "quota_alerts", "subject", "TEXT NOT NULL DEFAULT ''")
            # 告警状态机与静默到期：只补列，不改已有行（老告警默认 status='open'）
            _ensure_column(conn, "quota_alerts", "status", "TEXT NOT NULL DEFAULT 'open'")
            _ensure_column(conn, "quota_alerts", "acked_at", "INTEGER")
            _ensure_column(conn, "quota_alerts", "closed_at", "INTEGER")
            _ensure_column(conn, "quota_alerts", "silenced_until", "INTEGER NOT NULL DEFAULT 0")
            # 日志排查标记：纯运维元数据，billing_ledger 一行不动
            _ensure_column(conn, "request_logs", "resolved_at", "INTEGER")
            _ensure_column(conn, "request_logs", "resolved_by", "TEXT NOT NULL DEFAULT ''")
            # 逐条扣费关联键：老库 billing_ledger 没有 log_id，补完列再建索引（同 request_logs 的顺序）
            _ensure_column(conn, "billing_ledger", "log_id", "INTEGER")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_billing_ledger_log ON billing_ledger(log_id)")
            # F2 缺陷修复：空池拒绝也要留痕，给流水加可空的原因字段
            _ensure_column(conn, "sub2_ledger", "reason", "TEXT")
            # M2 起 codebuddy 默认指向管理端 7864（M1 默认是数据面 7863）；
            # 全等匹配：只有仍是出厂默认值的行才迁，用户改过的 URL 一律不动
            conn.execute(
                "UPDATE providers SET base_url = 'http://127.0.0.1:7864' "
                "WHERE id = 'codebuddy' AND base_url = 'http://127.0.0.1:7863'"
            )
            # F1：三家占位桩改成主仓内置实现，清掉旧的本地 sidecar 地址（同样只迁出厂默认值）
            conn.execute(
                "UPDATE providers SET base_url = 'https://opencode.ai/zen' "
                "WHERE id = 'opencode' AND base_url = 'http://127.0.0.1:7869'"
            )
            conn.execute(
                "UPDATE providers SET base_url = '' WHERE id = 'sub2api' AND base_url = 'http://127.0.0.1:7870'"
            )
            conn.execute(
                "UPDATE providers SET base_url = '' WHERE id = 'grok-register' AND base_url = 'http://127.0.0.1:7866'"
            )
            # 热路径索引：建表/补列之后统一接线；定义升级靠 ensure_indexes 比对重建
            from .perf_indexes import ensure_indexes

            ensure_indexes(conn)
            conn.commit()
            # S-05：库里存上游 key 与会话 token，权限在库文件创建时设一次就够；
            # 以前每条连接对 db/-wal/-shm 各 chmod 一次，纯属热路径无效系统调用
            for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
                _restrict(candidate, 0o600)
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
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


def put_settings(conn: sqlite3.Connection, items: dict[str, str]) -> None:
    """批量写 settings 但不提交：调用方负责 commit / rollback，多项写入要么全成要么全退。"""
    conn.executemany(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(items.items()),
    )


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
