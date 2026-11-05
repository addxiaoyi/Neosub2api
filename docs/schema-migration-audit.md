# Schema 迁移路径审计（2026-11-04）

**结论**：老库（2026-11-03 之前建）升级到当前 `db.py` 的全部迁移路径**幂等、无数据丢失**（除 4 条 providers 数据迁移的有意改写外，逐行指纹不变；二遍启动零变化；zip 回滚后指纹复原）；但存在**明确的静默降级**（存量日志 `credit_status=unlinked`、`key_prefix` 老行为 NULL、老 key 无主对非 admin 归零）和**一个真实缺陷**（providers UPDATE 按端口子串匹配，会覆盖自定义 URL）。真实库 `data/unified.db` 与**工作树版** `db.py` 差 1 列 1 索引（`accounts.owner_user_id` / `idx_accounts_owner`），系并行实例的未提交改动尚未落到库上——只报告，不修。

---

## 1. 审计基线与方法

| 项 | 说明 |
|---|---|
| 审计对象 | 工作树版 `apps/control/app/db.py`（相对 HEAD `867bd2b` 有未提交改动 `+7-1`，内容为 `accounts.owner_user_id` 补列/索引，见 §7-P2） |
| 老库钉死 | 任务给的 `git show HEAD:...` 不可用：审计期间 HEAD 已漂移（会话中从中间态前进到 `867bd2b 2026-11-04 16:11`）。改为**钉显式 commit 哈希**导出：`git show b5772fb:apps/control/app/db.py`（`b5772fb 2026-11-02 19:39 initial baseline`，**严格早于 11-03**）与 `git show f92c64e:apps/control/app/db.py`（`f92c64e 2026-11-03 17:08`，中生代）。两份导出经内容验证确属「老」：b5772fb **零** `billing_ledger`/`key_prefix`/`log_id`/`owner_user_id` 匹配；f92c64e `billing_ledger`(156) 表内**无** `log_id`、`idx_billing_ledger_key`(176) 为旧两列、ensure 迁移链止于 `reason`(279) 无 key_prefix/log_id |
| 演练库 | 全部在 `tmp/schema_drill_20261104/work/`；脚本在仓库外工作区（`temp_20261102_23\drill\`），仓库内仅新建本报告 |
| 真实库 | `data/unified.db` **原文件从未打开**：`Copy-Item` 文件级复制 db+wal+shm 到 `tmp/.../work/real_copy/`，一切检查在副本上做，`PRAGMA integrity_check=ok` |
| git | 仅 `show`/`log`/`status`/`diff`（只读）；未跑 pytest/npm/构建；未起服务 |

## 2. 迁移路径总表

`Database.__init__`（`db.py:264-323`）启动顺序：

| # | 路径 | 位置 | 幂等性 | 回填语义 |
|---|---|---|---|---|
| 1 | `executescript(SCHEMA)`：20 张 `CREATE TABLE IF NOT EXISTS` | `db.py:274`；表定义 8-261 | ✅ 幂等；**对已存在旧表零改动**（老表不会长出新列，靠路径 2 补） | 新表直接带全部列（含 `billing_ledger.log_id`、配额三表 UNIQUE） |
| 2 | `_ensure_column` ×8（`PRAGMA table_info` 查无则 `ALTER TABLE ADD COLUMN`，定义 `db.py:339-342`） | `278` accounts.meta_json / `280` accounts.owner_user_id / `283` api_keys.owner_user_id / `285` request_logs.key_id / `286` key_name / `289` key_prefix / `293` billing_ledger.log_id / `296` sub2_ledger.reason | ✅ 幂等（见列即跳过） | **只加列不回填**：280/283/285/289/293/296 老行为 NULL。带 DEFAULT 的 278（`'{}'`）、286（`''`）由 SQLite 读取时补默认 |
| 3 | 补列后直接建索引 ×4（`CREATE INDEX IF NOT EXISTS`，紧跟能力保证列先存在） | `281` idx_accounts_owner / `284` idx_api_keys_owner / `291` idx_request_logs_key_ts / `294` idx_billing_ledger_log | ✅ 幂等；**不升级已有定义** | — |
| 4 | `ensure_indexes`（`perf_indexes.py:64-74`，接线 `db.py:314-316`）：`_norm_sql`（`perf_indexes.py:59-61`）归一比对，不一致 → DROP → CREATE | 三条热路径索引 `perf_indexes.py:33-55` | ✅ 首次重建定义后幂等；**这是唯一会升级旧索引定义的路径** | 把老库旧两列 `idx_billing_ledger_key(key_id, ts)` 升级为三列（含 `billed_micro`） |
| 5 | providers 数据迁移 UPDATE ×4 | `299-300` codebuddy `:7863→:7864`（REPLACE）/ `304-305` opencode `LIKE '%:7869%'→'https://opencode.ai/zen'` / `308` sub2api `LIKE '%:7870%'→''` / `311` grok-register `LIKE '%:7866%'→''` | ✅ 幂等（改写后 WHERE 不再命中） | 有意改写存量值；**WHERE 是端口子串匹配而非「默认值全等」→ 见 §7-P1** |
| 6 | UNIQUE 约束 | 仅随建表一次到位：`quota_policies:197` `(key_id, provider, model, window)`、`quota_alert_thresholds:207` `(policy_id, percent)`、`quota_alerts:223` `(policy_id, threshold_percent, period_start)`、`price_books:145` | ✅ 老库无这些表 → 首启新建即带；**无「ALTER 新增 UNIQUE」路径** | 三表与其 UNIQUE 同 commit `ff22710 (2026-11-03 14:18)` 引入 → 不存在「有表无 UNIQUE」的存量形态 |
| 7 | `commit` + chmod（Windows no-op 被 `_restrict` 吞） | `317`、`320-321`（`db.py:331-336`） | ✅ | — |

**事务/崩溃安全**：Python sqlite3 隐式事务——DML 才开事务，DDL 逐条自动提交。迁移中途崩溃后重跑即续跑，所有步骤幂等，无半成品状态。

## 3. 已知事实核对

| # | 事实 | 判定 | 代码位置（工作树行号） |
|---|---|---|---|
| 1 | `key_prefix TEXT` 可空补列，老行为 NULL、不回填 | ✅ 成立 | `db.py:289`（`TEXT` 无 DEFAULT；注释 `287-288` 自述「老行留 NULL、读取端回退 join」）。f92c64e/b5772fb 均无此列无此 ensure（老文件 grep 零匹配） |
| 2 | `log_id INTEGER` 只 ALTER 加列，存量日志 `credit_status=unlinked` | ✅ 成立（实测复现） | `db.py:293`（注释 `292`）。读取端 `logs.py:178-185` `_credit_of`：`ledger WHERE log_id IN (...)` 查不到 → 成功行 `unlinked`、失败行 `not_billed`（`logs.py:12` 文档自述）。**且不弱关联**（`logs.py:8-9` 明确拒绝 key+model+ts 弱关联）→ f92c64e 演练中账面已有 2 条流水也连不上，日志页照旧 `unlinked` |
| 3 | 配额告警三表 `UNIQUE(policy_id, threshold_percent, period_start)` | ⚠️ 表述需校正 | 该 UNIQUE **只属 `quota_alerts` 一张表**（`db.py:223`）。三张配额表**各有各的** UNIQUE：`197`/`207`/`223`。若原意是「三表都带 UNIQUE」→ 成立；若指三表共享该三列 UNIQUE → 不成立 |
| 4 | 三条 perf 索引（两条覆盖 + 一条部分，WHERE 内嵌 DDL） | ✅ 成立（逐字保真实测） | `perf_indexes.py:33-37` scope 覆盖 `(key_id, provider, model, ts, billed_micro)`；`41-45` key 覆盖 `(key_id, ts, billed_micro)`；`51-55` 部分索引，`WHERE status >= 400 OR COALESCE(error, '') <> ''` 谓词在 `54` 行，随建索引 DDL 存入 `sqlite_master` |

## 4. 退化演练结果

命令（PowerShell，仓库根执行）：

```
python <工作区>\drill\drill.py        # 造老库 → 迁移两遍 → 全项验证（日志 tee 到 tmp/schema_drill_20261104/drill.log）
python <工作区>\drill\sha_compare.py  # 迁移前 zip 副本 vs 迁移后，按迁移前列集投影的 sha256 对照
```

**行数对照**（迁移前 → 迁移后；两遍启动后不变）：

- `old_b5772fb.db`：13 表 → 20 表。既有表逐一不变：`users 2 / api_keys 2 / request_logs 4 / providers 5 / usage_records 1 / settings 1 / audit_logs 1 / sub2_* 各 1 / grok_tasks 1 / accounts 0 / sessions 0`；新增 7 表全 0 行（billing_ledger、price_books、price_book_snapshots、quota_policies、quota_alert_thresholds、quota_alerts、provider_credentials）。
- `old_f92c64e.db`：20 表 → 20 表，**全部行数不变**（含 `billing_ledger 2 / quota_policies 1 / quota_alert_thresholds 1 / quota_alerts 1 / price_books 1 / provider_credentials 1`）。

**sha256 对照**（迁移前列集投影，`sha_compare.log` 摘要）：两库各 13/20 张表中，**除 `providers`（数据迁移有意改写，见下行）外全部 `match=True`**——例：`request_logs pre=post=5698ecb6ebdab5e446d118f21c2ab7f0`、`billing_ledger pre=post=52ac4ca01c8f7528c1c5f919124ba3af`、`users pre=post=8a0ac5d8c731aa6c02543c691257df73`。

**providers 行级 diff**（唯一数据改写，两条路径实测；**注**：本节数据来自 2026-11-04 的旧 LIKE 实现，当前工作树版本的等价实测见 §9.3，结论相反——自定义地址保留）：

- 自定义 URL 库（b5772fb 演练）：`codebuddy https://gw.example.com:7863/v1 → https://gw.example.com:7864/v1`（端口被改）；`opencode https://my-oc.internal:7869/zen → https://opencode.ai/zen`（自定义地址整体丢失）；sub2api/grok-register 默认地址 → `''`（预期行为）。
- 默认 URL 库（f92c64e 演练）：opencode `:7869 → https://opencode.ai/zen`、sub2api/grok-register → `''`（全部预期）；codebuddy `:7864` 不动（预期）。

**其余验证项**：

| 项 | 结果 |
|---|---|
| 二遍启动幂等 | ✅ PASS ×2：`after2 == after1` 完全一致（含 `sqlite_master` 全量） |
| 新增列只加不回填 | ✅ PASS ×2：b5772fb `api_keys.owner_user_id=[null]`、`request_logs.key_prefix=[null]`（空表列 `[]`）；f92c64e `billing_ledger.log_id=[null]`（2 条存量流水全 NULL）、`key_prefix=[null]` |
| 部分索引谓词逐字保真 | ✅ `sqlite_master` 原文 `CREATE INDEX idx_request_logs_failure ON request_logs(key_id, ts) WHERE status >= 400 OR COALESCE(error, '') <> ''` 与 `perf_indexes.INDEXES` 归一比对一致；scope/key 两覆盖索引同样保真 |
| 旧索引升级 | ✅ f92c64e `idx_billing_ledger_key` 两列 `(key_id, ts)` → 三列 `(key_id, ts, billed_micro)` 重建成功 |
| UNIQUE 生效 | ✅ 向 `quota_alerts` 插重复 `(1, 80, day, 1793577600)` → `IntegrityError: UNIQUE constraint failed: quota_alerts.policy_id, quota_alerts.threshold_percent, quota_alerts.period_start` |
| 回滚演练 | ✅ PASS ×2：`backup_pre_migration.zip` 解压恢复 → 快照与迁移前**逐字段一致** |
| API 读取（**直接 import 生产模块** `app.routers.logs`，非复刻） | ✅ 两库 4 行日志全部读出、零异常（排除回填缺失致 500）；明细见 §5 |
| 真实库副本 | ✅ `integrity_check=ok`；20 表齐全、`key_prefix`/`log_id`/两 UNIQUE/三 perf 索引全就位；**差异仅 2 项**：缺列 `accounts.owner_user_id`、缺索引 `idx_accounts_owner` → §7-P2 |

真实库行数（副本读取）：`request_logs 56 / billing_ledger 20 / api_keys 10 / sessions 106 / audit_logs 185 / sub2_ledger 32 / usage_records 56 / quota_alerts 9 / providers 15` 等 20 表。

## 5. 静默降级清单（列在表里 ≠ 语义齐全）

实测值来自生产函数 `_key_prefix`/`_credit_of`（direct-import `app.routers.logs`）：

| # | 场景 | 老库升级后表现 | 用户可见面 | 定性 |
|---|---|---|---|---|
| 1 | 存量成功日志无关联流水 | `credit=null`、`credit_status='unlinked'`（两库 id1/id4 均实测；f92c64e 明明有 2 条流水也因 `log_id NULL` 连不上） | 日志页金额列 `fmtMicroUsd(null) → '—'`（`format.ts:270-273`、`page.tsx:406`）、徽标「未关联」灰底（`page.tsx:118/136`） | 设计内降级（`logs.py:12` 自述），但**历史账单在日志页全部不可见**，且与「写流水异常」共用同一状态码，语义被稀释 |
| 2 | `key_prefix` 老行 NULL | API 给回退值：key 尚存 → join 掩码 `u2a_ab12****`；key 已删 → `null`（orphan 实测 `None`） | **web 前端全量 grep `key_prefix` 零命中**→ 无 UI 影响，仅下游 API 消费者可感知 null | 低危；回退逻辑实测有效、无 500 |
| 3 | `owner_user_id` NULL 的老 key | 非 admin 归属收窄 `key_id IN (SELECT id FROM api_keys WHERE owner_user_id = ?)`（`logs.py:116`）：b5772fb 演练 **alice 可见 0/4**；f92c64e **alice 可见 1/4** | 非 admin 用户升级后日志页直接空（老 key 全无主） | 设计内（`db.py` 归属注释、`logs.py:14-16` 声明），但对用户是**静默的「日志全没了」**，建议配升级公告 |
| 4 | `key_name` 空（匿名/等价默认值行） | API 返回 `''` | `l.key_name || '—'` 显示 `—`（`page.tsx:371`、详情 `519`） | 展示层兜底，无崩溃 |
| 5 | `credit=null` | — | `fmtMicroUsd` 非数字返回 `'—'`，**不显示 US$0.00**（`page.tsx:526` 注释有意为之） | 避免误导为「扣了 0 分」，兜底正确 |
| 6 | accounts/api_keys 归属列 NULL | 非 admin 不可见对应账号行 | 同 #3 口径 | 设计内 |

## 6. 演练中排除的非缺陷

- 首轮指纹曾报 4 处「稳定列指纹变化」——系演练脚本自身缺陷（`SELECT *` 在 ALTER 后多出新列，元组变长），改为**按迁移前列集逐行比对**后全部 PASS；已修正重跑，非 `db.py` 问题。
- `sqlite_sequence` 曾被报为「SCHEMA 之外的表」——SQLite AUTOINCREMENT 内部表，过滤后非差异。

## 7. 真实缺陷与最小修法（按严重度；只报告不修）

### P1 · providers 数据迁移会覆盖自定义 URL（数据损坏，低频但每次启动重扫）

> **实现状态（2026-11-05 补记）**：本条描述的是 2026-11-04 文件的 LIKE 写法；工作树当前实现已改为默认值全等匹配（代码变更非本轮，本轮未修任何代码）。该写的实测结果见 §9.3：带 `:7863/:7869/:7870/:7866` 子串的自定义地址全部原样保留。本条旧结论保留用于追溯，勿据其判断当前代码。

`db.py:299-311` 的 WHERE 用**端口子串**匹配（`LIKE '%:7863%'` 等），与注释 `297/302`（「只迁没动过的存量」「只迁默认值」）意图不符。实测：自定义网关 `https://gw.example.com:7863/v1` 被改成 `:7864/v1`（端口静默漂移，网关直连错端口）；自定义 opencode 兼容地址整体被 `https://opencode.ai/zen` 覆盖。该逻辑自 `b5772fb` 就存在（老文件 `161-173`），**每次启动重扫**，一旦用户配置含这些端口子串即被改写（改写后不可逆）。

**最小修法**：WHERE 改为与当年默认值**全等**——如 `base_url = 'http://127.0.0.1:7863'`（codebuddy 用等值判断替代 REPLACE，`http://127.0.0.1:7863`→`http://127.0.0.1:7864` 整串替换）；opencode/sub2api/grok-register 同理改 `= 'http://127.0.0.1:7869'` 等。一处 WHERE 文本替换，幂等性保持（全等命中→改写→不再命中）。

### P2 · 真实库 schema 与代码声明差 1 列 1 索引（只报告，按约束不修）

`data/unified.db`（副本实测）缺 `accounts.owner_user_id` 列与 `idx_accounts_owner` 索引——对应 `db.py:280-281`，属于**工作树未提交改动**（`git diff db.py`：`+7-1`，即 SCHEMA `accounts` 加列 + 迁移补列 + 建索引）；库是被**已提交 HEAD 版**迁移过的，落后这段在途改动。其余声明（`key_prefix`/`log_id`/配额 UNIQUE/三条 perf 索引）全部就位。

**影响**：下次用当前工作树代码启动即自动幂等补齐（演练已在两个退化库上验证该路径：补列全 NULL、建索引、二遍零变化、数据无损）→ **自愈，无需手工修**。决策留给维护者：合入该改动前真实库会持续报此差异。

### P3 · 首启索引重建持写锁（一次性、可断点续跑）

`ensure_indexes` 的 DROP+CREATE 与 4 条 `CREATE INDEX IF NOT EXISTS`（`db.py:281/284/291/294`）在**有数据的表**上建索引持有 SQLite 单写锁。当前真实库规模（`request_logs 56 / billing_ledger 20` 行）毫秒级无感；若未来到百万行级，首启会有可见写锁等待。DDL 逐条自动提交 + 全幂等 → 中途崩溃重跑即续。**无需现在处理**，量级起来后可在维护窗口预热（或 `PRAGMA` 观察）。

### P4 · 语义观测项（非 bug，建议关注）

- `unlinked` 把「历史账连不上」与「写流水异常」混同（`logs.py:12` 自认），统计口径上两者不可分。
- 存量成功日志在 stats 页金额可统计（`SUM(billed_micro)` 直聚流水，不依赖 `log_id`）但日志页明细显示「未关联」——**两页数字可能对不上的观感**。

## 8. 证据产物（全部在 `tmp/`，已被 gitignore）

```
tmp/schema_drill_20261104/
├── drill.log                 # 演练全量输出（PASS=15 / FAIL=1，FAIL 即 §7-P2）
├── sha_compare.log           # 迁移前后稳定列 sha256 对照
├── drill_result.json         # 结构化结果（含 API 直连生产函数的逐行输出、导入模式证据）
└── work/
    ├── old_b5772fb.db / old_f92c64e.db   # 迁移后（二遍启动终态）
    ├── backup_pre_migration.zip          # 迁移前备份（回滚验证用，已验证恢复后指纹复原）
    ├── backup_post_migration.zip         # 迁移后快照
    ├── rollback_check/                   # zip 解压恢复的对照副本
    └── real_copy/                        # data/unified.db 的文件级副本（原文件未打开）
```

老库构造源：`git show b5772fb:apps/control/app/db.py`、`git show f92c64e:apps/control/app/db.py`（导出件在仓库外工作区，脚本 `drill/drill.py`、`drill/sha_compare.py` 同址）。

## 9. 老库升级实测增补（2026-11-05）

本轮只做实测，**未修改任何代码**，也未做 git 写操作。测试对象是 2026-11-05 工作树版 `apps/control/app/db.py`（SHA256 `8AD545B62C8956A8BC31AAEF69FE7145D782DCE47243D09EC373D3EEBA168B41`）。它比 §1 记录的 `+7-1` 版本又多出若干在途改动（`users.email`/2FA、`quota_alerts.kind/subject`、providers 全等匹配、`users.role` 归一、`login_challenges`/`totp_recovery_codes`），因此 §2/§4/§5 的 20 表口径与旧行号只对应 11-04 文件；**当前代码已是 22 表**。

### 9.1 「旧行值不变」的已声明例外：`users.role` 的 `user → viewer`

除 providers 的设计内迁移外，本轮唯一被改写的旧行业务字段是 `users.role`：三份样本各命中 1 行（`alice`，`role='user' → 'viewer'`），`admin` 行不变。

**为什么是例外而不是缺陷**：

- 改写来自 `db.py:307-308` 的幂等 UPDATE（`WHERE role = 'user'`），二次启动不再命中；注释明确只匹配 `user`、不碰 `admin`。
- 鉴权只区分 admin：`security.py:102-104` 的 `admin_user` 只判断 `role != "admin"`，其余角色一律按非 admin 处理；`user` 与 `viewer` 在当前代码里没有任何分支差异。
- 注册接口本身已写 `viewer`（`routers/auth.py:261-263`），不会再产生新的 `user` 行；该映射只清理历史自助注册遗留值。
- 因此这是**口径统一**，不涉及提权/降权，也不伴随数据丢失；唯一代价是原始字符串 `'user'` 不可恢复。

**记录要求**：后续任何「旧行值逐字段不变」的断言都必须把这条映射列入白名单，否则会把预期行为误报为回归；白名单之外若出现 `users.role` 改写，仍按缺陷处理。

### 9.2 三份老库样本与迁移结果

样本与脚本在 `tmp/old-schema-probe/`；旧 `db.py` 用 `git show <commit>:apps/control/app/db.py` 导出后按旧包路径加载建库，再灌真实行（非空库，覆盖「旧行值不变」类断言）。三份样本都含用户（1 admin + 2 普通用户，其中 1 个 legacy `role='user'`）、6 个 provider、request_logs、usage_records、settings、audit_logs、sub2 组/账号/流水、grok 任务、sessions。

| 样本 | 来源 commit | 迁移前表数 / 真实行 | 缺什么 |
|---|---|---|---|
| `s1_oldest_defaults.db` | `b5772fb` 最老基线 | 13 表 / 38 行 | billing/quota/provider_credentials 全部、`users.email`+2FA、`accounts/api_keys.owner_user_id`、`request_logs.key_prefix`、`billing_ledger.log_id`、`quota_alerts.kind/subject`、`login_challenges`、`totp_recovery_codes` |
| `s2_mid_custom.db` | `f92c64e` 中期 | 20 表 / 55 行 | `log_id`、`key_prefix`、`kind/subject`、`users.email`+2FA、`accounts.owner_user_id`；providers 全是用户自定义地址（故意带 `:7863/:7869/:7870/:7866` 子串） |
| `s3_head_committed.db` | `867bd2b` 当前 HEAD 已提交版 | 20 表 / 55 行 | `kind/subject`、`users.email`+2FA、`accounts.owner_user_id`；`key_prefix`/`log_id` 含真实值与 NULL 混合；providers 混合默认值与自定义值 |

说明：最老版本没有配额/账务表，无法构造配额行；配额与账务数据由 S2、S3 覆盖。

| 实测项 | S1 | S2 | S3 |
|---|---|---|---|
| `Database()` 迁移 | ✅ 无异常 | ✅ | ✅ |
| 迁移后表数 | 22（新建 9 张全 0 行） | 22（新建 2 张全 0 行） | 22（新建 2 张全 0 行） |
| 旧表行数 | 13/13 一致 | 20/20 一致 | 20/20 一致 |
| 旧业务字段逐字段 | ✅（仅 §9.1 的 role 例外） | ✅ | ✅ |
| 新列默认 | `email=NULL`、totp 各列 `''/0`、`owner_user_id=NULL`、`key_prefix=NULL` | 同左 + `kind='quota'`、`subject=''`、`log_id=NULL` | 同左；已有 `key_prefix`/`log_id` 原值保留 |
| 索引 | 6 个新索引 + 3 条 perf 索引定义全部匹配；`idx_billing_ledger_key` 已含 `billed_micro` | ✅ | ✅ |
| `core.selfcheck` | ✅ 22/22 表、`row_counts drift={}`、admin=1、`integrity_check=ok`、FK 0 | ✅ | ✅ |
| 二次启动幂等 | ✅ 逻辑快照 SHA + 库文件 SHA 均一致（`17f79776…` / `468b450f…`） | ✅（`e1402d0a…` / `1c1ad1d1…`） | ✅（`b41766fe…` / `eb17df54…`） |

`selfcheck` 的清单行数是「迁移前各表行数 + 新表 0 行」，所以它同时验证了「行数不漂移」；`drift={}` 表示零差异。

### 9.3 providers `base_url` 实测：全等匹配有效，自定义地址未被改写

迁移前 → 迁移后（尚未启动控制面）：

| 样本 | 地址 | 迁移前 | 迁移后 | 判定 |
|---|---|---|---|---|
| S1 | codebuddy | `http://127.0.0.1:7863` | `http://127.0.0.1:7864` | 出厂默认，按预期迁移 |
| S1 | opencode | `http://127.0.0.1:7869` | `https://opencode.ai/zen` | 出厂默认，按预期迁移 |
| S1 | sub2api / grok-register | `:7870` / `:7866` | `''` | 出厂默认，按预期清空 |
| S1 | deepseek_sidecar | `http://127.0.0.1:7872` | 原样保留 | 旧种子地址，保留 |
| S2 | codebuddy | `https://gw.example.com:7863/v1` | **原样保留** | 自定义地址，含 `:7863` 子串 |
| S2 | opencode | `https://my-oc.internal:7869/zen` | **原样保留** | 自定义地址，含 `:7869` 子串 |
| S2 | sub2api | `https://s2a.example.com:7870/v1` | **原样保留** | 自定义地址，含 `:7870` 子串 |
| S2 | grok-register | `https://gr.example.com:7866/api` | **原样保留** | 自定义地址，含 `:7866` 子串 |
| S2 | qoder_sidecar / deepseek | 自定义 / `:5001` | 原样保留 | — |
| S3 | codebuddy | `http://127.0.0.1:7864` | 原样保留 | 已是新默认，等式不命中 |
| S3 | sub2api | `http://127.0.0.1:7870` | `''` | 出厂默认，按预期清空 |
| S3 | opencode / grok-register / qoder_sidecar | 自定义 | **原样保留** | — |

**结论**：这是「全等匹配」修复有效的直接实测证据，替换了 §4 基于旧 LIKE 实现的静态推演结论（§4 的「自定义 URL 被覆盖」只适用于 11-04 文件）。未覆盖的变体：默认值带尾斜杠、大小写/协议差异等；按全等匹配预期都会保留，但本轮没有实测样本。

### 9.4 启动后的正常副作用（不是迁移改写用户数据）

用三份老库分别起真实 uvicorn 实例（S1@8104、S2@8105、S3@8104）：三次都启动成功，`/healthz` 200，`/api/health/ready` 200 且 `database.ok=true`。停止后端口无 LISTEN、无残留监听进程（仅 `TIME_WAIT`）。

启动后观测到两类**运行时**变化，与迁移无关：

- providers 行数 6 → 14：registry 为表里缺失的适配器补种默认行（8 行）。
- 已存在行的 `base_url` 若等于当前兜底值/环境值，被 registry 清空：codebuddy `7864`、opencode `zen`、deepseek `5001`、qoder `7873` → `''`；S1 的旧 7872 因不等于兜底值而保留。自定义地址全部保留。

启动后整库逐字段对比：**只有 `providers` 表变化**（补种 + 上述清空），其余业务表逐行一致（S1 其余 12 张、S2/S3 其余 19 张）；`login_challenges`/`totp_recovery_codes` 仍为 0 行；启动后 `selfcheck` 仍通过。

**给后续审计的提醒**：不要把「启动后 providers 行数变多」或「兜底值地址变空」当成迁移改写用户数据——前者是 registry 补种，后者是「未配置」语义归一；两者都发生在 `Database()` 之后。

### 9.5 复现方式

系统 Python：`C:\Users\l\AppData\Local\Programs\Python\Python311\python.exe`（不用 `.runtime\python`）。

```
python tmp\old-schema-probe\probe\build_samples.py    # git show 导出旧 db.py → 建 3 份老库并灌真实行
python tmp\old-schema-probe\probe\verify_migration.py # 迁移 + 旧值比对 + 新列/新表/索引 + selfcheck + 二次启动幂等
python tmp\old-schema-probe\probe\start_instances.py  # 端口 8104/8105 起真实实例、验证健康与启动后数据
python tmp\old-schema-probe\probe\diff_post_start.py  # 迁移后 vs 启动后整库逐表 diff
```

产物：`tmp/old-schema-probe/samples/`（三份老库 + `manifest.json`）、`tmp/old-schema-probe/results/migration.json`、`results/server.json`、`tmp/old-schema-probe/versions/`（导出的旧 `db.py`）、`tmp/old-schema-probe/work/`（迁移与启动副本、日志）。

### 9.6 仍属静态推演、本轮未实测的部分

1. **真实库**：本轮完全没打开 `data/unified.db`；真实数据的脏形态（历史空 email、重复 email、异常 role 值、超大行数）未覆盖，§4 的真实库副本结论仍是 11-04 的。
2. **更老的 schema**：比 `b5772fb` 更早、缺 `accounts.meta_json` / `sub2_ledger.reason` / `request_logs.key_id` 的形态无法从仓库历史构造，对应补列路径仍只有静态推演（§2 路径 2）。
3. **崩溃续跑**：迁移中途进程被杀/断电后的断点续跑未实测，事务与 DDL 逐条提交的结论仍是静态推理。
4. **并发首启**：两个实例同时对同一老库执行补列/建索引未实测。
5. **回滚**：§4 的 zip 恢复演练未在新样本上重跑。
6. **新列/新表的业务语义**：本轮只验证「建表成功、旧数据不动」；`users.email` 唯一索引在真实重复数据下的行为、2FA 注册/恢复码、`login_challenges` 过期清理、全局告警 `kind/subject` 去重均未跑。
7. **大库首启 DDL 持锁时长**（§7-P3）未测，仍为量级推算。
8. **全等匹配的地址变体**（尾斜杠、大小写、协议差异）未构造样本实测。
