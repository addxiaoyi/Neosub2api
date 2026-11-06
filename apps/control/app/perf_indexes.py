"""热路径索引补丁：补缺失索引，定义升级时比对重建；不改表结构、不改查询。

db.py 的 SCHEMA 已经带了一批索引，但几条准入侧查询缺配套索引：
失败限流按 (key_id, ts) 扫 request_logs（已有索引，够用但会扫到窗口内成功行），
配额用量按 key + 周期 + 可选 provider/model 聚合 billing_ledger（只有 (key_id, ts)）。

接线（由维护者统一改，本模块刻意不 import db，避免循环依赖）：
在 apps/control/app/db.py 的 Database.__init__ 中，`conn.executescript(SCHEMA)` 之后加一行

    from .perf_indexes import ensure_indexes
    ensure_indexes(conn)

或在 apps/control/app/main.py 的 create_app 里 `db = Database(settings.db_path)` 之后加

    from .perf_indexes import ensure_indexes
    _conn = db.connect(); ensure_indexes(_conn); _conn.close()

两个位置等价，选一处即可；重复执行幂等。
"""

from __future__ import annotations

import re
import sqlite3

# 每条都对应一条真实热查询，注释写清「命中哪条 SQL」与写放大代价。
INDEXES = (
    # 配额窗口聚合（带 provider/model 收窄）：billing.py:window_usage
    #   WHERE key_id = ? AND ts >= ? AND ts < ? AND provider = ? AND model = ?
    # billed_micro 进索引后聚合是纯覆盖扫描（EXPLAIN 显示 USING COVERING INDEX），不回表。
    # 实测（20 万行、日窗口）：乱序 rowid 2180ms -> 38ms，顺序 rowid 56ms -> 26ms。
    # 代价：每次 ledger INSERT 多维护一列，20 万行顺序插入 1449ms -> 1546ms（+7%，噪声边缘）。
    (
        "idx_billing_ledger_scope",
        "CREATE INDEX IF NOT EXISTS idx_billing_ledger_scope "
        "ON billing_ledger(key_id, provider, model, ts, billed_micro)",
    ),
    # 配额窗口聚合（无 provider/model 收窄）：规划器在 unscoped 形态选 SCHEMA 的这条 key 索引，
    # 不选 scope 索引，所以它也要带 billed_micro 才能覆盖。
    # 实测（20 万行、乱序 rowid）：1978ms -> 22ms。SCHEMA 已同步新定义，本条负责重建存量库。
    (
        "idx_billing_ledger_key",
        "CREATE INDEX IF NOT EXISTS idx_billing_ledger_key "
        "ON billing_ledger(key_id, ts, billed_micro)",
    ),
    # 失败限流：billing.py:check_failure_rate
    #   WHERE key_id = ? [AND provider = ?] AND ts >= ? AND status <> 429
    #         AND (status >= 400 OR COALESCE(error, '') <> '')
    # 部分索引只收录失败行，窗口内成功行不进索引，COUNT 不必逐行回表判状态。
    # provider 进索引是为了按 (key, provider) 隔离计数，不让一家上游的故障连坐其他 provider。
    # 实测（20 万行全部落在 60s 窗口）：945ms -> 1.75ms；规划器确实选中该索引。
    # 代价：仅失败写入时多一次谓词判断 + 一条索引项；成功路径几乎零成本。
    (
        "idx_request_logs_failure",
        "CREATE INDEX IF NOT EXISTS idx_request_logs_failure "
        "ON request_logs(key_id, provider, ts) WHERE status >= 400 OR COALESCE(error, '') <> ''",
    ),
)


def _norm_sql(sql: str) -> str:
    # sqlite_master 原样存建索引文本，比对前抹掉空白、分号与 IF NOT EXISTS 的写法差异
    return re.sub(r"\s+", "", sql).lower().replace("ifnotexists", "").replace(";", "")


def ensure_indexes(conn: sqlite3.Connection) -> None:
    """幂等建索引；定义比对，不一致先 DROP 再建（IF NOT EXISTS 升不了级）。
    调用方负责连接生命周期，本函数内部 commit。"""
    for name, ddl in INDEXES:
        current = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
        ).fetchone()
        if current is not None and _norm_sql(current[0]) != _norm_sql(ddl):
            conn.execute(f"DROP INDEX {name}")
        conn.execute(ddl)
    conn.commit()


def index_names() -> list[str]:
    return [name for name, _ddl in INDEXES]
