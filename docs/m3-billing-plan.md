# M3 设计方案：配额、计费、白名单与 Postgres 支持

适用范围：`apps/control/app/`（控制面 + 网关一体进程）。只覆盖 M3 的设计与拆解，不改 M2 已交付行为。
文档基于 2026-11-02 读到的代码写成；引用格式为 `文件 → 函数`。

---

## 0. 现状基线（读到的事实）

| 事实 | 位置 |
| --- | --- |
| `usage_records` 只存上游原始 usage JSON（`usage_json`），无价格、无金额 | [db.py](../apps/control/app/db.py) `SCHEMA`；[billing.py](../apps/control/app/billing.py) `record_usage` |
| 请求落库链路：`_proxy → _record_safe → _record`，同一连接里写 `request_logs` + `usage_records`，`asyncio.shield + to_thread` 保证客户端断开也不丢记录 | [v1.py](../apps/control/app/routers/v1.py) `_record / _record_safe / _proxy` |
| usage 解析已统一 OpenAI（`prompt_tokens/completion_tokens`）与 Anthropic（`input_tokens/output_tokens`），流式从 SSE chunk 合并（`message_start`/`message_delta`） | [billing.py](../apps/control/app/billing.py) `totals / extract_usage / usage_from_stream / usage_in_chunk` |
| 已有自研配额范式：`QuotaStore.charge` 在**同一事务**里 `UPDATE quota_used = quota_used + ?`（账号、分组各一次）+ 写 `sub2_ledger`，单位是 token 数不是金额；`require_group` 在准入侧 429 | [sub2api_store.py](../apps/control/app/adapters/sub2api_store.py) `charge / require_group / pick_account_credentials` |
| sub2api 域扣减失败被吞掉（不改变已返回响应），靠流水对账兜底 | [sub2api.py](../apps/control/app/adapters/sub2api.py) `Sub2ApiAdapter._charge` |
| 密钥只有 `id/name/key_hash/prefix/enabled/created_at/last_used_at`，无配额、无模型白名单（注释明确留给 M3） | [keys.py](../apps/control/app/routers/keys.py) 模块 docstring；API-REFERENCE §6 |
| 统计的 `today_credit/week_credit/total_credit` 已由 `billing_ledger` 的 `SUM(billed_micro)` 填充（M3-1 已落地，展示层换算美元，见 API-REFERENCE §7） | [stats.py](../apps/control/app/routers/stats.py) `summary`；API-REFERENCE §7 |
| 持久化 = 单文件 SQLite，`connect()` 设 `PRAGMA journal_mode=WAL`、轻量迁移靠 `_ensure_column`；SQL 裸写在 8 个文件里 | [db.py](../apps/control/app/db.py) `Database.connect / _ensure_column` |
| 配置体系：`settings` 表存 JSON（`gateway`/`model_map`/`upstream_config`），`get_json_setting/set_json_setting` | [settings.py](../apps/control/app/routers/settings.py) `_read_config` |
| 鉴权：网关口是 `api_keys.key_hash = sha256(token)`；控制台是 cookie 会话（admin/viewer） | [v1.py](../apps/control/app/routers/v1.py) `_gateway_key`；[security.py](../apps/control/app/security.py) |

两个域要分清，M3 全程不混淆：

- **供给侧额度**（已有）：`sub2_groups / sub2_accounts / sub2_ledger`，单位 token，语义是"上游订阅号还剩多少量"。M3 **不动它的语义**。
- **消费侧计费**（M3 新增）：谁拿 `api_keys` 调网关、按价格表折成金额、按配额策略拦截。新表、新链路，与供给侧并存。

---

## 1. 配额模型

### 1.1 三层额度与叠加规则

| 层 | 归属 | 载体 |
| --- | --- | --- |
| 用户级 | `billing_accounts`（新增表，消费主体；**不是** `users` 控制台账号） | `api_keys.billing_account_id` 外键 |
| 分组级 | 自由标签分组（如 `team-a / internal / 试用`） | `api_keys.group_label` + `quota_policies` 按 label 匹配 |
| 密钥级 | 单把 `api_keys` | `quota_policies` owner_type='key' |

**叠加语义：三层中任意一层超限即拒绝，不做抵扣顺序。** 评估顺序 key → group → user（行数最少的最先查，短路快）。未配置策略的层视为不限。

注意：`api_keys` 目前没有任何 owner/分组字段（[keys.py](../apps/control/app/routers/keys.py) `create_key`），M3-2 通过 `_ensure_column` 风格迁移补 `billing_account_id / group_label`（现有库平滑升级沿用 [db.py](../apps/control/app/db.py) 的轻量迁移惯例）。

### 1.2 时间窗口

- 窗口类型：`day / month / total` 三种，`total` 无周期。
- 表达：**惰性周期行**，不跑定时清零任务。扣费时按当前时间算出 `period_start`（日界/月界），对 `(owner, window, period_start)` 做 UPSERT 累加；旧周期行自然留存即历史用量。
- 日/月边界时区：新增设置键 `billing.tz`（默认 `Asia/Shanghai`），与 [stats.py](../apps/control/app/routers/stats.py) 目前"按服务器本地日期分桶"的行为对齐；写进配置而不是硬编码。

### 1.3 超额行为

`quota_policies.on_exceed` 字段枚举 `reject / degrade / queue`，**M3 只实现 `reject`（429）**：

- `reject`：准入检查返回 429，错误体带 `{"error": {"type": "quota_exceeded", "layer": "key|group|user", "window": "day|month|total", "retry_after": <到窗口重置的秒数>}}`。与现有 sub2api 域的 429 语义一致（[sub2api_store.py](../apps/control/app/adapters/sub2api_store.py) `require_group`）。
- `degrade`（降级到低价模型）、`queue`（排队等待窗口重置）：字段预留，实现进 §7 不建议清单。

**准入检查不做预估预扣**：只判断 `used >= limit` 是否已成立。在途请求造成的透支以单个请求的实际费用封顶（单进程、请求时长短，可接受）。理由见 §7。

### 1.4 并发与速率限制

限额**存 DB**（`rate_limits` 表，`rpm / tpm / max_inflight`，0=不限），**执行在进程内存**：网关在 `_gateway_key` 拿到 key 行后过一道进程内 `RateGate`（每 key 一个令牌桶 + 信号量）。

- 为什么不上 DB/Redis 计数：当前是单进程 uvicorn（[main.py](../apps/control/app/main.py) `create_app`），DB 计数会给每个请求加一次写事务；多实例分布式限流进 §7。
- 流式请求的并发占用：从 `_proxy` 转发开始到 `body_stream` 结束（含客户端断开时的 finally）持有信号量，挂在 [v1.py](../apps/control/app/routers/v1.py) `_proxy` 的 try/finally 上。

### 1.5 DDL 草案（消费侧配额域）

```sql
-- 消费主体（用户级配额的归属）。与 users(控制台账号) 无关
CREATE TABLE IF NOT EXISTS billing_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,   -- PG: BIGINT GENERATED ALWAYS AS IDENTITY
  name TEXT NOT NULL UNIQUE,              -- 显示名，如 "zhang.san" 或 "客服团队"
  note TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);

-- 配额策略：三层共用一张表，owner_type 区分层
CREATE TABLE IF NOT EXISTS quota_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_type TEXT NOT NULL CHECK (owner_type IN ('user','group','key')),
  owner_id TEXT NOT NULL,                 -- user→billing_accounts.id；group→api_keys.group_label；key→api_keys.id（均转 TEXT 存储，统一索引）
  window TEXT NOT NULL CHECK (window IN ('day','month','total')),
  limit_micro INTEGER NOT NULL,           -- 限额，单位 micro-USD（1e-6 美元）；0=不限
  on_exceed TEXT NOT NULL DEFAULT 'reject' CHECK (on_exceed IN ('reject','degrade','queue')),
  degrade_model TEXT NOT NULL DEFAULT '', -- on_exceed=degrade 时的目标模型；M3 不实现，仅预留
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at INTEGER NOT NULL,
  UNIQUE (owner_type, owner_id, window)
);

-- 窗口用量：惰性周期行，(owner, window, period_start) 唯一，扣费时 UPSERT
CREATE TABLE IF NOT EXISTS quota_windows (
  owner_type TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  window TEXT NOT NULL,
  period_start INTEGER NOT NULL,          -- 周期起点 epoch 秒；total 窗口恒 0
  used_micro INTEGER NOT NULL DEFAULT 0,  -- 已用金额 micro-USD，只增不减（退款走负流水）
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (owner_type, owner_id, window, period_start)
);

-- 速率/并发限额（数值存 DB，执行在进程内存）
CREATE TABLE IF NOT EXISTS rate_limits (
  owner_type TEXT NOT NULL CHECK (owner_type IN ('user','group','key')),
  owner_id TEXT NOT NULL,
  rpm INTEGER NOT NULL DEFAULT 0,         -- 每分钟请求数，0=不限
  tpm INTEGER NOT NULL DEFAULT 0,         -- 每分钟 token 数（按 request_logs 汇总列口径），0=不限
  max_inflight INTEGER NOT NULL DEFAULT 0,-- 同时在途请求数，0=不限
  enabled INTEGER NOT NULL DEFAULT 1,
  UNIQUE (owner_type, owner_id)
);
```

`api_keys` 迁移补列（`_ensure_column` 同款机制）：

```sql
ALTER TABLE api_keys ADD COLUMN billing_account_id INTEGER;        -- 空=不归属任何消费主体（向后兼容：匿名/存量 key 不计用户层）
ALTER TABLE api_keys ADD COLUMN group_label TEXT NOT NULL DEFAULT '';
ALTER TABLE api_keys ADD COLUMN allowed_models TEXT NOT NULL DEFAULT '';  -- 逗号分隔模型名；空=不限（§4）
ALTER TABLE api_keys ADD COLUMN allowed_ips TEXT NOT NULL DEFAULT '';     -- 逗号分隔 IP/CIDR；空=不限（§4）
```

需要改的代码：

| 文件 → 函数 | 改动 |
| --- | --- |
| [v1.py](../apps/control/app/routers/v1.py) `_gateway_key` | SELECT 带出新四列；返回 dict 增加 `billing_account_id / group_label / allowed_models / allowed_ips`；过 IP 白名单与 `RateGate` |
| [v1.py](../apps/control/app/routers/v1.py) `_proxy` | 转发前调配额准入检查（新增 `billing.check_quota`），429 走现有 `HTTPException` 通道；`_resolve` 后过模型白名单 |
| [billing.py](../apps/control/app/billing.py) 新增 `check_quota / settle` | 见 §2.4 |
| [keys.py](../apps/control/app/routers/keys.py) `create_key / update_key / _row_to_public` | 增删改新四列；`_row_to_public` 输出归属与白名单（不回显敏感项无变化） |
| 新增 `routers/quota.py` | `billing_accounts` 与 `quota_policies / rate_limits` 的 CRUD（viewer 可读、admin 可写，沿用 [security.py](../apps/control/app/security.py) `current_user / admin_user`） |

---

## 2. 计费换算

### 2.1 换算链路

```
上游 usage JSON（已落 usage_records.usage_json）
  → 分类拆解（billing.split_usage，新增）
  → 按 token_class 查价格快照（§3）
  → 整数换算 cost_micro
  → 写 billing_ledger + UPSERT quota_windows（与 request_logs 同一事务）
```

拆解规则（新增 `billing.split_usage`，输入是现有 `usage_json` 原文，不依赖 `totals` 的折叠结果）：

| token_class | OpenAI 字段 | Anthropic 字段 |
| --- | --- | --- |
| `input` | `prompt_tokens - prompt_tokens_details.cached_tokens` | `input_tokens` |
| `output` | `completion_tokens - completion_tokens_details.reasoning_tokens` | `output_tokens` |
| `cache_read` | `prompt_tokens_details.cached_tokens` | `cache_read_input_tokens` |
| `cache_write` | （无此概念，0） | `cache_creation_input_tokens` |
| `reasoning` | `completion_tokens_details.reasoning_tokens` | （0） |

- 缓存命中：`cache_read` 按低价档计价，**从 input 里扣出来单独算**，不与普通 input 重复计（OpenAI 的 `prompt_tokens` 含 cached 部分）。
- 推理 token：按 output 价计（`reasoning` 类价格行缺省时回落到 `output` 价），已在拆解时从 output 扣除，不双计。
- 图片/多模态输入：上游（OpenAI/Anthropic）已把图片折算进 `prompt_tokens/input_tokens` 返回，M3 按 token 计即可，**不做独立图片计价**（§7）。
- usage 缺失（上游没返回或解析不到）：`billing_ledger` 记 `billed_micro=0, estimated=0, note='usage_missing'`；设置键 `billing.charge_on_missing_usage`（默认 false）开启时按 `max_tokens` 上限预估值计，`estimated=1`。默认免费但留痕，宁可少收不可错收。

### 2.2 精度与舍入

**决策：金额全程整数，单位 micro-USD（1 美元 = 1,000,000 micro），禁止 REAL/float 参与累加。**

- 价格存 `price_micro_per_1m`（每 1M token 的 micro-USD，整数）。
- 单行换算：`cost_micro = (tokens × price_micro_per_1m + 500_000) // 1_000_000`（四舍五入到 micro，整数运算）。每个 token_class 单独算再相加——单请求最大误差 ≤ 2.5 micro，可忽略。
- 累加：`quota_windows.used_micro`、`SUM(billing_ledger.billed_micro)` 都是整数列相加，无浮点漂移。
- 展示层：`stats` 的 `*_credit` 输出 `micro / 1e6` 的 USD 数值（仅此一处转 float，只读不落库）。
- 为什么不用 DECIMAL/NUMERIC：SQLite 的 NUMERIC 亲和性实际按 REAL/INTEGER 存，不可信；整数 micro 在两个方言下行为完全一致。

### 2.3 与现有范式的关系

`QuotaStore.charge`（[sub2api_store.py](../apps/control/app/adapters/sub2api_store.py)）已经验证了"同一事务里 UPDATE 累计列 + INSERT 流水"在 SQLite WAL 下可行。M3 消费侧沿用同一范式，差异只在：累计列换成 `quota_windows` 的 UPSERT、金额换整数 micro、流水多带价格快照字段。

### 2.4 计费 DDL

```sql
-- 消费侧流水：每请求一行，金额已定格，可重算可审计
CREATE TABLE IF NOT EXISTS billing_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  key_id INTEGER,                         -- 与 usage_records 一致：匿名/失效密钥写 NULL
  key_name TEXT NOT NULL DEFAULT '',
  billing_account_id INTEGER,             -- 冗余自 api_keys，便于按用户聚合
  provider TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',         -- 解析后的实际模型
  status INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  billed_micro INTEGER NOT NULL DEFAULT 0,-- 定格金额 micro-USD
  price_snapshot_id INTEGER,              -- 指向 price_book_snapshots.id，§3
  estimated INTEGER NOT NULL DEFAULT 0,   -- 1=usage 缺失时按预估值计
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_billing_ledger_ts ON billing_ledger(ts);
CREATE INDEX IF NOT EXISTS idx_billing_ledger_key ON billing_ledger(key_id, ts);
CREATE INDEX IF NOT EXISTS idx_billing_ledger_account ON billing_ledger(billing_account_id, ts);
```

需要改的代码：

| 文件 → 函数 | 改动 |
| --- | --- |
| [billing.py](../apps/control/app/billing.py) | 新增 `split_usage / resolve_prices / compute_cost_micro / settle / check_quota`；`record_usage` 不动 |
| [v1.py](../apps/control/app/routers/v1.py) `_record` | 写完 `request_logs` 后调 `billing.settle(conn, ...)`，**同一 conn、同一 commit**（沿用现有原子性，失败一起回滚，不会像 sub2api 域那样出现"响应已发但流水缺失"） |
| [stats.py](../apps/control/app/routers/stats.py) `summary / daily` | `*_credit` 从 `billing_ledger` 的 `SUM(billed_micro)` 填（替换恒 0 占位）；注意 `daily` 现在全表扫行后 Python 分桶，M3 顺手改成 SQL `GROUP BY date` 还是保持现状，改动时按性能实测决定（**待确认**：行数量级） |

---

## 3. 价格表

### 3.1 结构与版本化

**决策：价格行 append-only，改价 = 插新行，永不 UPDATE/DELETE 旧行。** 版本即 `effective_from`，解析规则一条 SQL 定格。

```sql
CREATE TABLE IF NOT EXISTS price_books (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,                 -- 适配器 id；'*'=通配
  model TEXT NOT NULL,                    -- 解析后的模型名；'*'=通配
  token_class TEXT NOT NULL CHECK (token_class IN ('input','output','cache_read','cache_write','reasoning')),
  price_micro_per_1m INTEGER NOT NULL CHECK (price_micro_per_1m >= 0),
  currency TEXT NOT NULL DEFAULT 'USD',   -- M3 只落 USD；前端展示汇率走设置键 billing.fx_rate
  effective_from INTEGER NOT NULL,        -- 生效时间 epoch 秒；可设未来时间实现"定时生效"
  created_by TEXT NOT NULL DEFAULT '',    -- 操作者（审计）
  note TEXT NOT NULL DEFAULT '',
  UNIQUE (provider, model, token_class, effective_from)
);
CREATE INDEX IF NOT EXISTS idx_price_books_lookup ON price_books(provider, model, token_class, effective_from);

-- 价格快照：一次解析的结果整行冻住，billing_ledger 只引用快照 id
CREATE TABLE IF NOT EXISTS price_book_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  resolved_at INTEGER NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  lines_json TEXT NOT NULL                -- {"input": {"price_id": 12, "price_micro_per_1m": ...}, ...}
);
```

解析 SQL（`billing.resolve_prices`，对每个 token_class 执行一次）：

```sql
SELECT id, price_micro_per_1m FROM price_books
WHERE token_class = ? AND effective_from <= ?
  AND provider IN (?, '*') AND model IN (?, '*')
ORDER BY
  (provider <> '*') DESC, (model <> '*') DESC,  -- 精确优先于通配
  effective_from DESC                            -- 同精度取最新生效
LIMIT 1;
```

### 3.2 变更历史、灰度、防"两套价格"

- **历史**：append-only 天然是历史版本表；任何一笔 `billing_ledger` 可经 `price_snapshot_id → lines_json` 重算出当时的定价依据。
- **灰度生效**：用 `effective_from` 设未来时间戳实现"到点切换"；按 model/provider 维度分批上新价就是灰度粒度。按流量百分比的灰度不做（§7）。
- **防两套价格**（请求过程中价格被改）：
  1. `_proxy` 准入时调 `resolve_prices` 一次，写入 `price_book_snapshots`，把 `snapshot_id` 挂在请求上下文上；
  2. 流式响应在流结束才 settle（[v1.py](../apps/control/app/routers/v1.py) `body_stream` 的 finally），用的仍是准入时的 snapshot；
  3. 改价不触碰旧行，所以在途请求的任何时刻重查都得到同一结果——快照只是性能优化（少查 5 次），正确性由 append-only 本身保证。

需要改的代码：

| 文件 → 函数 | 改动 |
| --- | --- |
| [v1.py](../apps/control/app/routers/v1.py) `_proxy` | 准入时 `resolve_prices` 并保存 snapshot；`body_stream` 的 `_record_safe` 调用带上 snapshot_id |
| [v1.py](../apps/control/app/routers/v1.py) `_record` | 签名加 `price_snapshot_id`，透传给 `settle` |
| 新增 `routers/pricing.py` | `GET /api/pricing/books`（含历史）、`POST /api/pricing/books`（插新行，admin）、`GET /api/pricing/resolve?provider=&model=`（干跑预览，便于核对解析结果） |
| [settings.py](../apps/control/app/routers/settings.py) | 无改动；`billing.fx_rate` 等设置沿用 `get_json_setting` 体系 |

---

## 4. 白名单与风控（最小实现）

### 4.1 IP 白名单

- 字段：`api_keys.allowed_ips`，逗号分隔的精确 IP 或 CIDR（`ipaddress` 标准库解析，不引第三方包）；空 = 不限。
- 执行点：[v1.py](../apps/control/app/routers/v1.py) `_gateway_key`，key 认证通过后校验。客户端 IP 取 `request.client.host`；是否信任 `X-Forwarded-For` 由设置键 `gateway.trust_xff`（默认 false）控制——默认不信任，防伪造。
- 拒绝码：403 `{"detail": "该密钥不允许从此 IP 调用"}`。

### 4.2 模型白名单

- `api_keys.allowed_models`，逗号分隔，**匹配解析后的实际模型名**（即 [v1.py](../apps/control/app/routers/v1.py) `_resolve` 返回的 `effective`），防止用别名绕过。
- 执行点：`_proxy` 里 `_resolve` 之后、转发之前。拒绝码 403。
- 通配：支持尾部 `*`（如 `claude-*`），实现就是前缀比较，不引正则。

### 4.3 爆破与异常用量检测（最小）

- **爆破**：进程内计数器按 IP 统计 401 次数（`_gateway_key` 抛 401 处），10 分钟窗口超 30 次 → 该 IP 锁 10 分钟（423 或 429，选一，建议 429），同时写 `audit_logs`（表已存在，[db.py](../apps/control/app/db.py) `SCHEMA`）。不新增表。
- **异常用量**：`settle` 里顺带做一次比较——该 key 当日 `used_micro` > 过去 7 天日均 × `billing.anomaly_multiplier`（默认 10）→ 插一行 `risk_events`（新表），默认**只告警不自动停 key**；`billing.auto_disable_on_risk`（默认 false）开启时把 `api_keys.enabled` 置 0。控制台在 stats 页读 `risk_events` 未读条数做横幅。

```sql
CREATE TABLE IF NOT EXISTS risk_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL,                     -- brute_force_lock / usage_spike
  subject TEXT NOT NULL,                  -- IP 或 key_id
  detail TEXT NOT NULL DEFAULT '',
  acknowledged INTEGER NOT NULL DEFAULT 0
);
```

需要改的代码：[v1.py](../apps/control/app/routers/v1.py) `_gateway_key`（IP 白名单 + 爆破计数）、`_proxy`（模型白名单）；[billing.py](../apps/control/app/billing.py) `settle`（异常检测）；新增 `services/rate_gate.py`（令牌桶 + 爆破计数，进程内状态集中一处）。

---

## 5. SQLite → Postgres 双支持

### 5.1 抽象方案

新增 `app/db/` 包，把 [db.py](../apps/control/app/db.py) 拆成：

```
app/db/
  __init__.py        # build_backend(settings) 按 UNIFIED_DB_BACKEND 返回后端
  base.py            # Backend 协议：connect() / dialect / schema_ddl / ensure_column
  sqlite_backend.py  # 现有 Database 原样搬入，行为不变
  pg_backend.py      # psycopg(v3)，row_factory=dict_row，连接即设 lock_timeout
  sql.py             # q()：把 '?' 占位符翻译成 '%s'（仅 pg 后端）；DDL 两方言各一份
```

原则：**SQL 文本继续以 `?` 书写，驱动层统一翻译**，业务代码零改动或极少改动。不走 ORM——现有 SQL 都是直白 CRUD，引 SQLAlchemy 是杀鸡用牛刀。

### 5.2 方言差异与替代方案

| 点 | SQLite 现状 | PG 方案 | 涉及位置 |
| --- | --- | --- | --- |
| 占位符 | `?` | `%s`（`q()` 翻译） | 全部 `conn.execute` 站点 |
| 自增主键 | `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT GENERATED ALWAYS AS IDENTITY` | [db.py](../apps/control/app/db.py) `SCHEMA`（DDL 分方言） |
| 取新 id | `cursor.lastrowid` | `INSERT ... RETURNING id` | [sub2api_store.py](../apps/control/app/adapters/sub2api_store.py) `add_account`；[keys.py](../apps/control/app/routers/keys.py) `create_key` |
| 补列迁移 | `PRAGMA table_info` | `information_schema.columns` | [db.py](../apps/control/app/db.py) `_ensure_column` |
| UPSERT | `ON CONFLICT(x) DO UPDATE SET` | 同语法（PG 15- 已兼容此形态） | [db.py](../apps/control/app/db.py) `set_setting`；[accounts.py](../apps/control/app/routers/accounts.py) `_sync_provider`；无需改 |
| WAL | `PRAGMA journal_mode=WAL`（读者不阻塞写者） | **不需要替代**：PG 的 MVCC 默认就是该语义，`connect()` 里按方言跳过 PRAGMA 即可 | [db.py](../apps/control/app/db.py) `Database.connect` |
| 写超时 | `sqlite3.connect(timeout=30)` 忙等 | `lock_timeout` / `idle_in_transaction_session_timeout` 会话参数 | 后端 `connect()` 内部 |
| 行访问 | `sqlite3.Row`（`row["x"]`） | `psycopg.rows.dict_row` | 各站点 `row["x"]` 用法两方言一致，无需改 |
| 布尔 | INTEGER 0/1 | 沿用 SMALLINT/INTEGER（不用 BOOLEAN，避免两方言值域差异） | DDL |

### 5.3 必须改的代码路径（逐文件）

| 文件 → 函数 | 改动性质 |
| --- | --- |
| [db.py](../apps/control/app/db.py) 全文件 | 拆进 `app/db/`；`Database.connect` 的 PRAGMA 移到 sqlite 后端 |
| [config.py](../apps/control/app/config.py) `load_settings` | 新增 `UNIFIED_DB_BACKEND`（默认 `sqlite`）、`UNIFIED_PG_DSN` |
| [main.py](../apps/control/app/main.py) `create_app` | `Database(settings.db_path)` 换成 `build_backend(settings)` |
| [billing.py](../apps/control/app/billing.py) `record_usage / usage_by_key` + M3 新增函数 | SQL 过 `q()` |
| [v1.py](../apps/control/app/routers/v1.py) `_gateway_key / _record` | 同上 |
| [stats.py](../apps/control/app/routers/stats.py) `summary / daily` | 同上 |
| [settings.py](../apps/control/app/routers/settings.py) `_read_config / save_upstream_config / get_gateway_config / save_gateway_config / get_model_map / save_model_map / list_users / add_user / update_user / remove_user` | 同上 |
| [keys.py](../apps/control/app/routers/keys.py) `list_keys / create_key / update_key / revoke_key` | 同上 + `lastrowid → RETURNING` |
| [security.py](../apps/control/app/security.py) `create_session / revoke_user_sessions / session_user` | SQL 过 `q()` |
| [registry.py](../apps/control/app/registry.py) `_seed / _rows / _provider_configs / update / resolve` | 同上 |
| [accounts.py](../apps/control/app/routers/accounts.py) `_sync_provider / list_accounts / set_note / set_disabled / remove_account` | 同上 |
| [sub2api_store.py](../apps/control/app/adapters/sub2api_store.py) `QuotaStore` 全部方法 | 同上 + `add_account` 的 `lastrowid → RETURNING` |
| routers/auth.py | **待确认**：登录/登出端点内部是否直接 `conn.execute`（大概率只调 `security.create_session`，动手前 grep 一次 `conn.execute` 确认） |
| services/grok_register.py（grok_tasks 表属主） | **待确认**：任务队列读写函数的 SQL 站点清单 |

### 5.4 迁移与回滚

新增 `scripts/migrate_sqlite_to_pg.py`：

1. 停控制面进程（一次性切换，不做双写，§7）；
2. 在 PG 执行 PG 方言 DDL 建全表；
3. 按固定顺序导数据：`users → sessions → providers → accounts → api_keys → request_logs → usage_records → settings → audit_logs → sub2_groups → sub2_accounts → sub2_ledger → grok_tasks → billing_accounts → quota_policies → quota_windows → rate_limits → price_books → price_book_snapshots → billing_ledger → risk_events`；
4. 每个自增表导完 `setval` 序列到 `MAX(id)`；
5. 校验：逐表比行数 + `billing_ledger` 比 `SUM(billed_micro)`、`sub2_ledger` 比 `SUM(cost)`；
6. 切 `UNIFIED_DB_BACKEND=postgres` 启动。

**回滚**：SQLite 文件全程只读不动；切回 `UNIFIED_DB_BACKEND=sqlite` 重启即回到迁移前状态。切换窗口内 PG 侧新产生的数据会丢（接受：控制面非高可用目标，窗口分钟级）。

**切换风险**：① `ON CONFLICT` 语义在两方言下对 NULL 的处理差异（`billing_ledger.key_id` 可空但不在唯一键里，无影响）；② 时区——全部时间戳是 epoch 秒整数列，无 timestamptz 陷阱；③ 事务隔离：PG 默认 READ COMMITTED 与 SQLite WAL 读快照行为接近，`charge`/`settle` 的单事务写法两侧一致；④ grok 任务队列的 `status/next_run_at` 轮询若在 PG 下并发实例运行需要 `SELECT ... FOR UPDATE SKIP LOCKED`（**待确认**：当前是否单实例运行 grok_service——[main.py](../apps/control/app/main.py) lifespan 看是进程内单实例，M3 不改）。

---

## 6. 实施拆解

### M3-1 计费内核（算钱，不拦人）

| 项 | 内容 |
| --- | --- |
| 改动文件 | [db.py](../apps/control/app/db.py)（SCHEMA 加 `price_books / price_book_snapshots / billing_ledger`）；[billing.py](../apps/control/app/billing.py)（新增 `split_usage / resolve_prices / compute_cost_micro / settle`）；[v1.py](../apps/control/app/routers/v1.py)（`_proxy` 准入解析快照、`_record` 接 `settle`）；[stats.py](../apps/control/app/routers/stats.py)（填 `*_credit`）；新增 `routers/pricing.py`；[main.py](../apps/control/app/main.py) 注册路由 |
| 验收标准 | ① 录入已知价格后打 100 个请求，`SUM(billed_micro)` 与手工整数值完全相等；② 请求进行中改价，在途流式请求仍按准入快照计费；③ OpenAI 带 `cached_tokens` 与 Anthropic 带 `cache_read_input_tokens` 的 usage 拆解正确、不重复计费；④ `usage_records` 原有直通落库行为不变（回归） |
| 测试要点 | 整数换算边界（1 token × 低价、大额 token 不溢出）；`*` 通配与精确行优先级；同精度多版本取最新 `effective_from`；usage 缺失走 `note='usage_missing'` 且金额 0 |
| 风险 | 低。全部是新表 + `_record` 尾部追加，不改现有响应路径；最大风险是 `_proxy` 准入多一次解析查询——快照表写入与价格查询都在 `to_thread` 写库路径上，对延迟影响可忽略 |

### M3-2 配额、白名单与风控（开始拦人）

| 项 | 内容 |
| --- | --- |
| 改动文件 | [db.py](../apps/control/app/db.py)（加 `billing_accounts / quota_policies / quota_windows / rate_limits / risk_events`，`api_keys` 补四列）；[billing.py](../apps/control/app/billing.py)（`check_quota`、窗口 UPSERT、异常检测）；新增 `services/rate_gate.py`；[v1.py](../apps/control/app/routers/v1.py)（`_gateway_key` 接 IP 白名单/RateGate/爆破计数，`_proxy` 接模型白名单与配额准入）；[keys.py](../apps/control/app/routers/keys.py)（新字段 CRUD）；新增 `routers/quota.py`；[stats.py](../apps/control/app/routers/stats.py) 输出风险事件数 |
| 验收标准 | ① key 日限额 1000 micro，打到超限后下一请求 429，错误体含 `layer/window/retry_after`；② 跨日界后窗口自动重置（改 `billing.tz` 或 mock 时间验证）；③ `allowed_ips` 外 IP 403、`allowed_models` 外模型 403（别名绕过测试：用 model_map 别名请求被拦）；④ rpm 超限 429；⑤ 同一 key 超限后，未配置策略层的其他 key 不受影响；⑥ 爆破锁定后合法 key 从同 IP 也被拒（验证锁定在 IP 维度） |
| 测试要点 | 三层叠加（各层分别构造超限）；窗口 UPSERT 并发（`to_thread` 下两请求同时 settle 不丢更新——靠单事务 UPSERT 保证）；`on_exceed='degrade'` 配置存在时行为仍是 reject（明确拒绝未实现枚举的语义） |
| 风险 | 中。`_gateway_key` 是每请求热路径，新增一次 key 行 SELECT（已在做）+ 内存 RateGate（纳秒级）+ 配额检查一次 SELECT——可接受；注意 `_gateway_key` 目前每次请求开关一次连接，M3-2 把配额检查并入同一连接，避免再开 |

### M3-3 Postgres 双支持

| 项 | 内容 |
| --- | --- |
| 改动文件 | 新增 `app/db/` 包（`base / sqlite_backend / pg_backend / sql`）；[db.py](../apps/control/app/db.py) 内容迁入并保留兼容 re-export；[config.py](../apps/control/app/config.py)、[main.py](../apps/control/app/main.py) 按 §5.3 表逐文件过 `q()`；新增 `scripts/migrate_sqlite_to_pg.py` |
| 验收标准 | ① 测试套件在 SQLite 与 PG（docker-compose 起临时库）两侧全绿；② 迁移脚本跑完后逐表行数与金额 SUM 校验一致；③ 回滚演练：切回 sqlite 启动，数据与迁移前一致；④ `keys.create_key` 与 `sub2api_store.add_account` 在 PG 下返回正确新 id（RETURNING 路径） |
| 测试要点 | `ON CONFLICT DO UPDATE` 双方言；`_ensure_column` 双方言；WAL→MVCC 下并发读不阻塞写（起线程压测 `_record` + `stats.summary` 并发）；DDL 两套无漂移（测试：两后端建表后 `information_schema`/PRAGMA 列清单一致） |
| 风险 | 高。改动面是全仓 SQL 站点（§5.3 表 20+ 函数），靠"先抽象后迁移"两步走控制：第一步只抽象不切换（SQLite 行为回归全绿），第二步才接 PG。grok 任务队列并发语义是已知坑，先确认单实例前提（§5.4 ④） |

---

## 7. 不建议现在做的事

| 事项 | 为什么拖慢进度 |
| --- | --- |
| 分布式限流 / Redis 配额计数 | 单进程 uvicorn 下是纯负资产：多一个基础设施依赖、多一类故障模式；等多实例部署真实出现再做 |
| 按流量百分比的价格灰度 | append-only + `effective_from` + 快照已解决"两套价格"问题；百分比灰度要引入分桶键与一致性哈希，收益为零 |
| 预估预扣 / 余额冻结 | 在途透支已被单请求费用封顶；预扣要处理"流式中途取消的解冻"，复杂度翻倍 |
| `on_exceed = degrade / queue` 的实现 | degrade 要维护"低价模型映射 + 重解析路由"，queue 要引入等待队列与超时语义；字段已预留，等真实运营诉求出现 |
| 图片/多模态独立计价维度 | 上游已把图片折进 token 返回，独立计价需要逐模态拆解请求体，先按 token 计费跑一个里程碑，拿真实账单数据验证缺口再补 |
| 双写迁移 / 零停机切换 | 控制面是内部系统，分钟级停写可接受；双写要处理两库一致性对账，是 M3-3 工作量翻倍的典型来源 |
| 对账系统、发票、充值/退款流水 | `billing_ledger` 字段已留全（价格快照、estimated 标记），需要时导出 CSV 即可；提前做就是猜需求 |
| 把 sub2_* 供给侧 token 配额改造成金额域 | 语义不同（上游订阅余量 vs 下游消费限额），合并会同时搞砸两个域；保持并存、各算各的 |
| 控制台 billing 大屏 / 汇率多币种 | `stats` 的 credit 字段补上即满足"看得见钱"；可视化大屏属于展示层迭代，不进 M3 |
