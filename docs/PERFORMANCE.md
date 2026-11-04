# 网关性能与压测报告

范围：`apps/control` 的 `/v1/chat/completions` 热路径（准入 → 配额 → 价格快照 → 上游 → 落账）。
结论基于代码走查 + 本机实测（Windows 11 / 嵌入式 CPython / D 盘 SQLite WAL）。
基准脚本：`scripts/bench/load.py`；索引补丁：`apps/control/app/perf_indexes.py`。

## 0. 结论先行

### 接入前（旧代码，早先空闲机基线，未接入索引）

1. **吞吐天花板是 SQLite 单写者，不是索引。** 三个并发档位（8/32/64）成功吞吐稳定在 28–34 QPS，
   p95 随并发线性上涨（321ms → 1116ms → 2771ms）。原因：每个成功请求要提交 **2 次写事务**
   （`last_used_at` 的 UPDATE + 落账 INSERT），实测 2 次 commit 的写路径上限约 20–50 请求/s，
   与实测 28–34 QPS 吻合。延迟 ≈ 并发 × 30ms，是排队而不是计算。
2. **每请求新建 `httpx.AsyncClient` 是本机最大的隐形开销。** Windows 证书库让 `AsyncClient`
   构造耗时 4–5s（单次上游请求 1.2s）。裸跑 8 并发只有 **0.7 QPS / p50 11.9s**；把
   `SSLContext` 缓存后同一份代码可达 29 QPS。生产代码按 provider 复用 client（含连接池）后，
   这部分开销应消失。
3. **索引只缺两条，且只在长窗口/大流水下才明显。** `request_logs` 的失败限流查询有
   `(key_id, ts)`，但窗口内成功行也要逐行回表：20 万失败行全落在 60s 窗口时 **945ms → 1.75ms**。
   `billing_ledger` 只有 `(key_id, ts)`，provider/model 收窄的配额策略下 **44ms → 26ms**（1.7x）。
   两条索引的写放大合计使 20 万行插入从 57ms 涨到 94ms（**+63%**）。
4. **没有出现 `database is locked`**（0 次响应、0 次日志）。`sqlite3.connect(timeout=30)` 把锁竞争
   转成延迟，所以 p99 的膨胀（64 并发 2.8s）就是写锁排队时间。
5. 失败限流的成本是 **O(窗口内失败行数)**，不是全表扫：`idx_request_logs_key_ts` 先把范围收窄到
   60s 窗口。按 4.8µs/行的实测换算，2 千行窗口约 10ms，20 万行窗口约 945ms。
   当前 30 QPS 下窗口约 2 千行，因此压测里看不出来（洪峰或高 QPS 时才会暴露）。

### 接入后（2026-11-03 复跑，同机存在其它并发任务，数字偏低，以 2026-11-04 干净复跑为准）

6. **上游 client 复用生效，裸跑提升约 27x。** `adapters/client.py` 按
   `(base_url, 凭据指纹, 代理, 附加头)` 进程级复用 `AsyncClient`（走连接池），并共享一份
   `SSLContext`。裸跑（`--no-ssl-shim`）8 并发从接入前的 **0.7 QPS / p50 11.9s** 提升到
   **19.2 QPS / p50 344ms / p95 476ms**（QPS 约 27x，p50 约 35x）。
7. **`last_used_at` 空提交已消除。** UPDATE 命中 0 行（60s 节流窗口内）不再 `commit`，写事务数
   从“2 次”降为“1 次”。不过空 commit 本来就是低成本操作，本次 10s、单 key 的压测里吞吐变化在
   噪声内；吞吐收益要在未节流的 key 或更长时间跨度上才看得出来。
8. **两条索引已接入启动流程。** `db.py` 建表/补列之后调用 `ensure_indexes(conn)`，`IF NOT EXISTS`
   保证幂等，旧库启动时自动补索引。10s 压测只有约 200 行流水，索引收益要等窗口内失败行上万/
   20 万才显现（既有微基准 945ms → 1.75ms）。
9. **垫片口径下接入前后基本持平**（chat 8 并发 19.3 → 19.4 QPS，32 并发 20.0 → 19.7）。
   垫片已经消掉了 client 构造噪声，剩下的瓶颈是 SQLite 写串行与本机并发负载；两种口径不要混着比。

### 接入后（2026-11-04 干净条件复跑，本轮取数基准）

10. 干净条件下裸跑三档（chat、`--no-ssl-shim`、每档两轮取均值）：8 / 32 / 64 并发 =
    **32.0 / 33.1 / 34.9 QPS**，p50 = 226 / 846 / 1492ms，p95 = 322 / 1531 / 3502ms。
    对比接入前裸跑 8 并发 0.7 QPS ≈ **46x**；比 11-03 干扰环境的 19.2 / 18.3 / 16.1 高 67% / 81% / 117%
    ——11-03 偏低是环境不干净所致，接入后数字以本轮为准（原始数据与环境声明见 3.3）。
11. 单并发 28.3 QPS / p50 29.9ms 与三档吞吐（30~35 QPS）几乎持平 → 每请求串行段 ≈**29ms**，
    加并发只买到 13%~23%；假上游为 async、全链路本机回环、三轮 0 错误 0 `locked`，
    拐点在网关单进程内部，不在网络层。
12. 单请求延迟构成（微基准复刻生产 SQL，分母为单并发 p50 29.9ms）：只读路径 9.4ms + 写事务 8.9ms
    = 18.2ms ≈ **61%**，其中**写入占 ≈30%**、读占 ≈31%、上游+框架 ≈11.7ms；
    读路径 9.4ms 里约 9.1ms 是 5 次 `db.connect()` 的 PRAGMA+chmod（单次 1.82ms），SQL 本身合计 <0.3ms。
13. 「拐点在 SQLite 单写者」的旧表述需修正：同款写事务在独立进程纯写可达 108 TPS（1 线程）/
    407 TPS（8 线程争用，p50 2.3ms），是网关 34 QPS 的 3~12 倍；按 9ms/次估算 34 QPS 下写锁占用率
    仅 ≈31%，写锁并未打满。准确表述：**瓶颈是网关单进程内每请求同步 DB 访问的全套成本**
    （6~7 次 `connect` + 事件循环内 4 段同步读 + 写事务）串行叠加 ≈29ms，SQLite 相关占 61%。
14. 索引在更大数据量下的实测（5 万 / 20 万行全部落窗）：失败限流 COUNT 224.7 → 2.4ms、
    731.5 → 6.2ms（93x / 117x）；ledger scoped 聚合 142.8 → 37.9ms、1434.8 → 372.6ms（3.8x / 3.9x），
    规划器均选中 perf 索引。压测档数据量（每轮几百行）下两条查询亚毫秒、不参与拐点；
    20 万行/日窗口下聚合仍 372ms（逐行回表）为索引后残余风险（见第 4 节）。

## 1. 代码层面：每请求库查询数

以下按显式 `x-provider`（跳过目录解析）、无配额策略、模型未定价统计：

| 阶段 | SQL 语句 | 语句数 | 连接数 | 写事务 | 数据量增长后是否退化 |
| --- | --- | --- | --- | --- | --- |
| `_gateway_key` 鉴权 | `SELECT api_keys(key_hash,enabled=1)` + `UPDATE last_used_at` + (命中时) commit | 2 | 1 | 0~1 | 60s 节流命中 0 行时不再 commit；只有未节流的 key 才写 |
| `_resolve` → `registry.get(pid)` | `SELECT providers` / `SELECT settings(provider_config)` / `SELECT provider_credentials(enabled=1 ORDER BY sort_order,id)` / `SELECT settings` | 4 | 2 | 0 | 表小，O(1)；provider 数不变 |
| `_quota_guard` | `SELECT quota_policies` (+ 每命中策略 1 次窗口聚合) | 1 | 1 | 0 | O(策略数 × 窗口内 ledger 行数)，日窗口下随时间线性 |
| `_failure_guard` | `SELECT COUNT request_logs ... WHERE key_id=? AND ts>=? AND (status>=400 OR COALESCE(error,'')<>'')` | 1 | 1 | 0 | O(窗口内失败行数)；失败洪水时线性，见第 3 节 |
| `_price_snapshot` | 5 次 `SELECT price_books`（每个 token_class 一次） | 5 | 1 | 0 | O(log M)，M = price_books 行数（很小）；5 次可合并成 1 次 `IN` |
| 上游调用 | 复用进程级 `AsyncClient` 与连接池（首次建连） | – | – | – | 固定开销降到一次性，见第 3 节 |
| `_record`（`asyncio.to_thread`） | `INSERT request_logs` + `INSERT usage_records` + (`INSERT price_book_snapshots`) + `INSERT billing_ledger` + `SELECT quota_policies`(evaluate_alerts) + commit | 4 | 1 | 1 | 写事务固定；告警聚合 O(窗口行数) |
| **合计** | | **17** | **7** | **1~2** | |

变体：

| 情形 | 增量 | 合计语句 |
| --- | --- | --- |
| 命中一条配额策略 | `_quota_guard` +1（窗口聚合）；`evaluate_alerts` +2（窗口聚合 + 阈值） | 20 |
| 模型命中定价 | +1 `INSERT price_book_snapshots` | 18（+策略则 21） |
| 不带 `x-provider`（目录匹配） | +1 `settings(model_map)`；`registry.enabled()` = 1 次 `SELECT providers` + 每个启用 provider 4 条语句 | 默认只启用 codebuddy：23；14 家全启用：75 |
| 被配额拒绝 | 省上游/定价/落账，仍写 1 条拒绝日志 | 9 |
| 被失败限流拒绝 | 同上 | 9 |

补充：每个 `db.connect()` 还会执行 `PRAGMA journal_mode=WAL`，并对 db / -wal / -shm 各做一次
`os.chmod`。7 次连接 = 7 次 PRAGMA + 21 次 chmod，是固定但可观的每请求成本。
（2026-11-04 起已改：PRAGMA/chmod 移到建库时执行一次，单请求建连 8 → 4 次，见 3.3
「连接成本优化」；本段保留改前状态。）
`Database` 没有设置 `PRAGMA synchronous`，WAL 下默认 `FULL`，每次 commit 都 fsync。

## 2. 索引层面：现有索引 vs 热查询

| 热查询 | 现有索引 | 判定 |
| --- | --- | --- |
| `api_keys WHERE key_hash=? AND enabled=1` | `key_hash` 上的 UNIQUE 自动索引 | 够 |
| `providers` 全表 / `settings(key='provider_config')` | 主键 / 表小 | 够 |
| `provider_credentials WHERE provider=? AND enabled=1 ORDER BY sort_order,id` | `idx_provider_credentials_order(provider,enabled,sort_order,id)` | 够，且免排序 |
| `request_logs WHERE key_id=? AND ts>=? AND (status>=400 OR COALESCE(error,'')<>'')` | `idx_request_logs_key_ts(key_id,ts)` | 范围够，但窗口内每行都要回表判 `status`/`error` → 缺部分索引 |
| `billing_ledger WHERE key_id=? AND ts>=? AND ts<? [AND provider=? AND model=?]` | `idx_billing_ledger_key(key_id,ts)` | key 维度够；provider/model 收窄时仍扫该 key 的全部流水 → 缺 scope 索引 |
| `quota_policies WHERE key_id=? AND enabled=1 AND (provider='' OR provider=?) AND (model='' OR model=?) ORDER BY id` | `idx_quota_policies_key(key_id,enabled)` | 够（策略数小，UNIQUE 也提供 rowid 序） |
| `price_books WHERE token_class=? AND provider IN (?, '*') AND model IN (?, '*') AND effective_from<=? ORDER BY 表达式 DESC LIMIT 1` | `idx_price_books_lookup(token_class,provider,model,effective_from)` | 前缀够；`ORDER BY (provider<>'*') DESC` 是表达式，只能对 ≤4 个候选做小排序 |
| `quota_alert_thresholds WHERE policy_id=?` / `quota_alerts` | UNIQUE 自动索引 + `idx_quota_alerts_policy` | 够 |
| `usage_records WHERE ts>=? GROUP BY key_id,key_name` | 无索引 | 非请求热路径（后台统计），但 O(全表)。暂不加，需要时加 `(ts,key_id)` |

补的索引（`perf_indexes.py`，`CREATE INDEX IF NOT EXISTS` + 定义比对升级）：

| 索引 | 命中的查询 | 实测收益（20 万行） | 写放大 |
| --- | --- | --- | --- |
| `idx_request_logs_failure ON request_logs(key_id, ts) WHERE status >= 400 OR COALESCE(error,'') <> ''` | 失败限流 COUNT | 945ms → **1.75ms**（约 540x），EXPLAIN 确认走该索引 | 只在失败行上多一条索引项 + 一次谓词；成功路径几乎零成本 |
| `idx_billing_ledger_scope ON billing_ledger(key_id, provider, model, ts)` | provider/model 收窄的配额窗口聚合 | 44ms → **26ms**（1.7x，受随机回表限制） | ledger 每行多一棵 b-tree；20 万行批量插入 57ms → 94ms（**+63%**） |

两条索引的收益与写放大都在 `.runtime` 下的微基准里实测过；3.0 表主体数字是**未接入新索引**时跑的。
2026-11-04 又按「5 万/20 万行、全部落窗」的最坏口径复测（原 `.runtime` 微基准已不在），
结果与取法见 3.3 索引小节。

### 接线状态（已接入）

- 接线位置：`apps/control/app/db.py`，`conn.executescript(SCHEMA)` 与补列之后、`conn.commit()` 之前：
  ```python
  from .perf_indexes import ensure_indexes
  ensure_indexes(conn)
  ```
- 幂等：均为 `CREATE INDEX IF NOT EXISTS`；`ensure_indexes` 比对 sqlite_master 定义，不一致先
  DROP 再建（`IF NOT EXISTS` 升不了级），旧库启动自动重建。2026-11-04 起 ledger 两条已升级为
  覆盖索引（见 3.3「连接成本优化」次目标 a）。
- 未改 `main.py`，避免与其它任务并发冲突。

## 3. 实测层面

环境：假上游 `scripts/fake_upstream.py` + 临时数据目录 + 控制面端口 18127（`UNIFIED_TEST_CTRL_PORT`）。
命令：

```bash
python scripts/bench/load.py --concurrency 8,32,64 --duration 10 --provider codebuddy
```

场景定义：`chat` = 纯转发（假上游 200）；`quota` = 该 key 有一条宽松请求数配额（走配额检查 + 告警评估）；
`ratelimit` = 模型名带 `err-500`（100% 上游失败），窗内上限 10，失败后全被 429 拒绝。

#### 3.0 接入前基线（早先空闲机，未接索引，带 SSL 垫片）

| 场景 | 并发 | 请求数 | QPS(成功) | p50 ms | p95 ms | p99 ms | 错误率 | 状态分布 | locked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| chat | 8 | 294 | 29.4 | 239.8 | 321.2 | 370.5 | 0.0 | 200:294 | 0 |
| chat | 32 | 335 | 33.5 | 876.6 | 1115.7 | 1323.4 | 0.0 | 200:335 | 0 |
| chat | 64 | 278 | 27.8 | 2009.5 | 2771.1 | 2798.4 | 0.0 | 200:278 | 0 |
| quota | 8 | 287 | 28.7 | 241.6 | 335.6 | 368.4 | 0.0 | 200:287 | 0 |
| quota | 32 | 331 | 33.1 | 855.0 | 1123.1 | 1668.1 | 0.0 | 200:331 | 0 |
| quota | 64 | 307 | 30.7 | 1974.2 | 2691.1 | 3059.1 | 0.0 | 200:307 | 0 |
| ratelimit | 8 | 389 | 0.0 | 172.1 | 268.4 | 352.9 | 1.0 | 429:373, 502:16 | 0 |
| ratelimit | 32 | 393 | 0.0 | 709.1 | 892.0 | 894.6 | 1.0 | 429:393 | 0 |
| ratelimit | 64 | 412 | 0.0 | 1490.9 | 2187.7 | 2422.3 | 1.0 | 429:412 | 0 |

`database is locked`：**响应 0 次，控制面日志 0 次**。
`ratelimit` 里的 16 个 502 是前 16 个真正打到假上游 `err-500` 的请求（网关把上游 500 映射成 502），
其余 429 都来自失败限流。

#### 3.0 表读表要点（接入前基线）：

- 三档并发吞吐不动（28–34 QPS），说明瓶颈是串行资源；p50 ≈ 并发 × 30ms 是排队特征。
- `quota` 与 `chat` 差异在噪声内（8 并发 p50 239.8 vs 241.6）：配额检查在当前数据量下只多 1–3 条聚合，
  被写串行掩盖。
- `ratelimit` 总请求量更高（64 并发 41.2/s vs 27.8/s），因为拒绝路径省掉上游、定价和 ledger 写入，
  但仍要提交 1 条拒绝日志，所以照样被写锁卡住。

#### 3.1 接入前后 A/B（同一时段、同一 10s 档位、均带 SSL 垫片）

旧代码副本保留“每请求新建 client + 无条件 commit + 未接索引”，与接入后代码顺序跑，尽量抵消时间/负载差异。
本机当时有其它并发任务，绝对值低于 3.0 的空闲机基线，**只看前后相对变化**。

| 场景 | 并发 | 接入前 QPS | 接入前 p50 / p95 | 接入后 QPS | 接入后 p50 / p95 |
| --- | --- | --- | --- | --- | --- |
| chat | 8 | 19.3 | 334 / 485 ms | 19.4 | 365 / 452 ms |
| chat | 32 | 20.0 | 1404 / 1864 ms | 19.7 | 1342 / 2211 ms |
| chat | 64 | 24.3 | 2621 / 3603 ms | 20.9 | 2354 / 5657 ms |
| quota | 8 | 20.4 | 333 / 445 ms | 18.4 | 365 / 574 ms |
| quota | 32 | 25.7 | 1107 / 1604 ms | 10.8 | 2279 / 3447 ms |
| quota | 64 | 20.7 | 2793 / 4023 ms | 11.5 | 5054 / 7377 ms |
| ratelimit | 8 | –（全 429） | 329 / 445 ms | –（全 429） | 317 / 584 ms |
| ratelimit | 32 | –（全 429） | 1444 / 1701 ms | –（全 429） | 1396 / 1953 ms |
| ratelimit | 64 | –（全 429） | 2680 / 3494 ms | –（全 429） | 2863 / 3572 ms |

读表：垫片已经消掉 client 构造开销，接入前后的差异主要来自本机负载波动（quota 32/64 尤其明显），
不能据此判断 commit/索引无效；它们的作用点见 3.2 与“瓶颈量化”。

#### 3.2 接入后裸跑（2026-11-03，同机有其它并发任务，数字以 3.3 为准；生产口径 `--no-ssl-shim`）

命令（端口用未占用的 18141/18142）：

```bash
UNIFIED_TEST_CTRL_PORT=18141 UNIFIED_TEST_FAST_PORT=18142 \
  python scripts/bench/load.py --concurrency 8,32,64 --duration 10 --provider codebuddy --no-ssl-shim
```

| 场景 | 并发 | 请求数 | QPS(成功) | p50 ms | p95 ms | p99 ms |
| --- | --- | --- | --- | --- | --- | --- |
| chat | 8 | 192 | 19.2 | 344.1 | 475.6 | 565.6 |
| chat | 32 | 183 | 18.3 | 1392.5 | 2531.3 | 3067.7 |
| chat | 64 | 161 | 16.1 | 3123.0 | 6694.7 | 9018.4 |
| quota | 8 | 160 | 16.0 | 404.4 | 542.6 | 558.0 |
| quota | 32 | 157 | 15.7 | 1636.3 | 2907.3 | 3573.8 |
| quota | 64 | 151 | 15.1 | 3366.6 | 6393.6 | 8317.1 |
| ratelimit | 8 | 208 | –（全 429） | 295.6 | 450.8 | 498.0 |
| ratelimit | 32 | 226 | –（全 429） | 1283.6 | 1511.9 | 1555.3 |
| ratelimit | 64 | 228 | –（全 429） | 2585.4 | 3448.1 | 3504.9 |

对照接入前的裸跑基线（3.0 第 2 条）：**0.7 QPS / p50 11.9s（8 并发）→ 19.2 QPS / p50 344ms**，
说明 client 复用已经把 Windows 证书库/构造开销从每请求成本里拿掉。
`database is locked` 仍为 0；32/64 并发 p95 偏高是当时本机并发任务造成的排队，不是新回归。

#### 3.3 接入后（2026-11-04 干净条件复跑，主口径）

**环境声明**

- 本机 Windows、嵌入式 CPython（`.runtime`，httpx 0.28.1、`trust_env=False`，服务环境已剥除
  HTTP(S)_PROXY 等代理变量）、控制面**单进程 uvicorn** + 独立假上游 uvicorn + 压测客户端，
  全部本机回环；SQLite WAL 在本机 D 盘，32 逻辑核。
- 开跑前检查：`Get-Process` 无 pytest/无其它压测，8340–8350 段无监听。**干扰声明**：本机存在其它
  会话的常驻控制面 uvicorn（预检见 8010 / 18155，收尾时另见 8403，起始区间未知，均空闲挂起、
  未见流量），未触碰、可能构成轻微背景负载，未做排除。
- 端口：第 1 轮 8340（控制面）/ 8341（假上游），第 2 轮 8342/8343，单并发参照轮 8344/8345；
  数据目录为 `tmp/` 下 `tempfile` 临时目录（每档轮次独立起服、全新库全新密钥），仓库 `data/` 未触碰；
  跑完压测进程与临时库已全部回收。
- 口径：chat 场景裸跑 `--no-ssl-shim`，每档 10s；**每档两轮 = 两次独立起服，取两轮均值为稳定值**
  （两轮 QPS 极差：8 档 12%、32 档 2.7%、64 档 4.6%）；三轮全部 `error=0`、`locked=0`
  （`locked` = 30s busy_timeout 内拿不到写锁的次数）。
- 命令：`python scripts/bench/load.py --concurrency 8,32,64 --duration 10 --provider codebuddy
  --scenario chat --no-ssl-shim --json`（端口 `UNIFIED_TEST_CTRL_PORT` / `UNIFIED_TEST_FAST_PORT`
  按上表，`TEMP`/`TMP` 指向 `tmp/`）。

**两轮原始数据（chat 裸跑）**

| 并发 | 轮次 | 请求数 | QPS(成功) | p50 ms | p95 ms | p99 ms | 错误 | locked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 8 | 第 1 轮 | 300 | 30.0 | 237.3 | 348.8 | 385.4 | 0 | 0 |
| 8 | 第 2 轮 | 339 | 33.9 | 214.7 | 295.8 | 527.9 | 0 | 0 |
| 8 | **稳定值（两轮均值）** | – | **32.0** | **226.0** | **322.3** | 456.7 | 0 | 0 |
| 32 | 第 1 轮 | 335 | 33.5 | 821.1 | 1530.6 | 1984.7 | 0 | 0 |
| 32 | 第 2 轮 | 326 | 32.6 | 870.5 | 1531.0 | 1854.1 | 0 | 0 |
| 32 | **稳定值（两轮均值）** | – | **33.1** | **845.8** | **1530.8** | 1919.4 | 0 | 0 |
| 64 | 第 1 轮 | 341 | 34.1 | 1504.4 | 3530.7 | 4431.1 | 0 | 0 |
| 64 | 第 2 轮 | 357 | 35.7 | 1479.0 | 3472.2 | 4662.6 | 0 | 0 |
| 64 | **稳定值（两轮均值）** | – | **34.9** | **1491.7** | **3501.5** | 4546.9 | 0 | 0 |

单并发参照轮（8344/8345，10s）：283 请求、**28.3 QPS**、p50 29.9 / p95 35.9 / p99 107.1ms、
错误 0、`locked` 0。
SSL 垫片口径与 quota/ratelimit 场景本轮未重跑（主口径为 chat 裸跑；3.0/3.1/3.2 数字保留作历史对照）。

**延迟构成：SQLite 写入占比（微基准，复刻生产 SQL/函数，与压测分进程运行）**

临时库上按生产实现逐段计时（每段预热 5 次、跑 300 次取中位；段内含生产同款 `db.connect()` 开销），
以单并发端到端 p50 29.9ms 为分母：

| 段 | p50 ms | 说明 |
| --- | --- | --- |
| `db.connect()` 单独 | 1.82 | PRAGMA journal_mode=WAL + db/-wal/-shm 各 1 次 chmod |
| 只读 / auth | 1.86 | key_hash SELECT + last_used UPDATE（稳态 60s 节流内命中 0 行、不 commit） |
| 只读 / registry | 2.00 | providers / provider_config / 凭据 / settings 4 条小表读 |
| 只读 / 配额策略 | 1.85 | 无策略时只有 matching_policies 1 条 SELECT |
| 只读 / 失败限流 | 1.79 | 窗口 COUNT（窗内 300 条成功行、命中 0 失败） |
| 只读 / 价格 | 1.88 | resolve_prices |
| **只读合计** | **9.4** | 端到端的 **≈31%**；其中 ≈9 成是 5 次 connect 的 PRAGMA+chmod，SQL 本身合计 <0.3ms |
| **写事务** | **8.9**（mean 11.2 / p95 13.7） | INSERT request_logs + record_usage + settle + commit（含 evaluate_alerts），**≈30%** |
| **DB 合计** | **18.2** | **≈61%** |
| 上游 + 框架（残差） | ≈11.7 | 29.9 − 18.2：假上游 RTT + uvicorn/httpx/JSON |

注：分段 p50 之和 ≠ 端到端 p50，占比为约数口径；微基准进程与压测分离，冷热状态不同。
本表为**改前口径**；2026-11-04 已把 PRAGMA/chmod 移出热路径、单请求建连 8 → 4 次，
见下方「连接成本优化」。

**写事务争用（隔离 GIL 的独立进程，同款写事务压同一临时库，每档 5s）**

| 线程 | 完成事务 | TPS | p50 ms | p95 ms | locked |
| --- | --- | --- | --- | --- | --- |
| 1 | 545 | 107.9 | 7.98 | 11.86 | 0 |
| 8 | 2107 | 406.8 | 2.33 | 18.04 | 0 |
| 32 | 2034 | 315.8 | 2.35 | 42.32 | 0 |

（1 线程档为含冷启动的首轮，后续争用档反而更快，疑为页缓存/杀毒冷启动成本，取趋势不取绝对。）
→ SQLite 写路径单独可支撑 **108~407 TPS**，为网关 34 QPS 的 3~12 倍；按 9ms/次、34 QPS 折算
写锁占用率仅 ≈31%。**写锁没有打满，34 QPS 的拐点不是写锁等待**（与「瓶颈量化」第 1 条的
20–50 请求/s 折算口径不同：那组是接入前 2 次写事务、把单线程延迟当全局服务时间；本轮为接入后
1 次写事务 + 并行折算，以本轮为接入后基准，旧折算保留作接入前参考）。

**索引在 5 万 / 20 万行下的影响（灌临时库实测）**

口径：失败行全部落 60s 窗（对齐 3.0 表的最坏场景）、ledger 行全部落当日窗口、4 组
provider/model 均匀分布；WITH/WITHOUT 通过在临时库上 DROP/CREATE 两条 perf 索引切换，
各预热 2 次取 5 次中位数，EXPLAIN QUERY PLAN 核对走索引：

| 数据量 | 查询 | 无索引 | 有索引 | 提速 | 执行计划（无 → 有） |
| --- | --- | --- | --- | --- | --- |
| 5 万行 | 失败限流 COUNT | 224.7ms | **2.4ms** | 93x | idx_request_logs_key_ts → idx_request_logs_failure |
| 5 万行 | ledger 日窗口聚合 | 142.8ms | **37.9ms** | 3.8x | idx_billing_ledger_key → idx_billing_ledger_scope |
| 20 万行 | 失败限流 COUNT | 731.5ms | **6.2ms** | 117x | idx_request_logs_key_ts → idx_request_logs_failure |
| 20 万行 | ledger 日窗口聚合 | 1434.8ms | **372.6ms** | 3.9x | idx_billing_ledger_key → idx_billing_ledger_scope |

- 失败 COUNT 与第 1 节旧微基准（20 万行 945ms → 1.75ms）量级一致（本轮 731.5 → 6.2ms）；
  ledger 与旧口径的 44ms → 26ms 不可直接比：旧口径窗口内命中行少，本轮是**全部行都落在当日窗口**
  的最坏口径（20 万行中 5 万行命中 scoped）。
- 本轮压测档数据量（每轮几百行）下两条查询均亚毫秒，不参与 34 QPS 拐点。
- 残余风险：20 万行/日窗口下 scoped 聚合仍有 372ms——索引命中后仍逐行回表取 `billed_micro`，
  对应第 4 节的覆盖索引/归档建议。

**连接成本优化（2026-11-04 授权改动：改前 → 改后）**

1. 改前单次 `db.connect()` 成本分解（临时库、300 次分段计时取中位）：

| 段 | p50 ms | 说明 |
| --- | --- | --- |
| `sqlite3.connect` 本体 | 0.191 | |
| `PRAGMA journal_mode=WAL` | 0.726 | 每条连接都跑；journal_mode 是库文件头持久属性，已 WAL 时是纯往返，大头是首触 -shm |
| chmod ×3（db/-wal/-shm） | 0.161 | 权限只需建库时设一次，却在热路径上每连接执行 |
| `close()` | 0.349 | 最后连接关闭触发 checkpoint + 删 -wal/-shm |
| **合计** | **1.443**（p95 1.699） | 对照：SQL 本身 <0.05ms；复用连接上 SELECT 仅 0.003ms |

2. 改法（三个方向实测取舍）：

- 连接池（方向 1）**实测否决**：Windows 上连接打开期间 `shutil.rmtree(临时目录)` 报 WinError 32，
  而测试 `_DbCase.tearDown` 会在 Database 存活时删临时目录 → 进程内池化必破
  test_billing/test_quota，需连带改测试基建，超出本次授权。
- 固定动作只做一次（方向 2）：PRAGMA 移到 `Database.__init__` 建库时执行一次；chmod 同样只在
  库文件创建后设一次；`connect()` 瘦身为 connect + row_factory。
- 合并调用点（方向 3）：准入段（gateway/quota/failure/price/resolve）5 次连接合并为 1 次共用
  （函数改签名显式传 conn）；`registry.get` 2 次属其它会话文件、`_record` 1 次在
  `asyncio.to_thread` 工作线程内自开（线程边界隔离），均不动。单请求建连 **8 → 4 次**
  （§1 表口径 7 次，未单列 `resolve_model` 里 `_enabled_ids` 自开的那次）。
- 语义保持（硬约束）：连接随请求创建、随请求关闭，**不跨请求共享**；共用连接上 UPDATE 命中
  0 行显式 `rollback()` 回掉隐式 BEGIN，后续配额/价格读留在 autocommit、不进写事务；
  配额/失败限流的 except 路径显式 rollback，等价旧实现 close 的隐式回滚。

3. 连接税前后：改前 8 × 1.443 ≈ **11.54ms/请求** → 改后 4 × 1.420 ≈ **5.68ms/请求**（−51%）。
   改后单次 connect+首查+close p50 **1.420ms**、p95 2.097ms——PRAGMA/chmod 单独省下的 ~0.9ms
   中大头被首条查询的 -shm 首触吸收，**主要收益来自连接次数减半**；复用地板价 0.003ms/次
   仍不可达（方向 1 否决理由，见上）。

4. 压测前后（chat、`--no-ssl-shim`、每档 10s、独立新库、8340–8343 端口）：

| 并发 | 改前 QPS（今日确认轮） | 改后 QPS（两轮均值） | ΔQPS | 改前 p50 / p95 ms | 改后 p50 / p95 ms（均值） |
| --- | --- | --- | --- | --- | --- |
| 8 | 38.9 | **48.2** | **+23.8%** | 183.4 / 246.6 | **136.0 / 216.3** |
| 32 | 36.5 | **49.3** | **+34.9%** | 765.9 / 1352.2 | **522.8 / 965.0** |
| 64 | 41.6 | **42.9** | +3.0% | 1231.5 / 2812.8 | **1133.5 / 2674.1** |

改后两轮原始（8/32/64）：**45.3 / 48.0 / 42.2** 与 **51.0 / 50.5 / 43.5**，全部 error=0、locked=0。
口径声明：改前为 1 轮、改后 2 轮取均值，不完全对称；昨日文档基线 32.0/33.1/34.9 与今日改前
38.9/36.5/41.6 有 +12~22% 环境漂移，前后对比只用今日改前。**64 档只 +3%**——高并发下写事务
排队主导，连接税占比变小，收益集中在 8/32 档。

5. ledger 覆盖索引（次目标 a，**已改**）：`idx_billing_ledger_scope` →
   `(key_id, provider, model, ts, billed_micro)`；另实测 unscoped 形态（无 provider/model 收窄，
   demo 种子策略即 `provider='' AND model=''`）规划器选的是 SCHEMA 的 `idx_billing_ledger_key`、
   不是 scope 索引，故它也升级为 `(key_id, ts, billed_micro)`。`ensure_indexes` 增加定义比对
   （sqlite_master.sql 归一化，不一致先 DROP 再建——`IF NOT EXISTS` 升不了级），SCHEMA 同步
   新定义，新库启动不触发重建（已单独验证）。

| 20 万行形态 | 查询 | 改前（旧索引） | 改后（覆盖） | 提速 |
| --- | --- | --- | --- | --- |
| 顺序 rowid | scoped 日窗口聚合 | 51.4ms | **24.9ms** | 2.1x |
| 乱序 rowid | scoped 日窗口聚合 | 1987.3ms | **28.5ms** | **70x** |
| 乱序 rowid | unscoped 日窗口聚合 | 1978.3ms | **21.7ms** | **91x** |

EXPLAIN 均确认 `USING COVERING INDEX`；20 万行顺序插入 1316 → 1395ms（**+6%**，噪声边缘）。
上文索引小节的 372.6ms 成因（命中行逐行回表）已被消除：本轮是最坏口径（20 万行全部命中
scoped）仍只有 28.5ms。

6. 次目标 b（ledger 归档/分区）**结论（2026-11-04 隔离副本四档实测改写；旧值：「行数到
   百万级再上按日保留期/归档表」，改动原因：旧结论仅凭 34 QPS 线性外推、未实测）：
   总表口径放宽到 ≥200 万行，窗口口径收紧到单 key 50 万行，且归档解决不了日窗口延迟**。
   - 事实（实测：SQLite 3.45.1 / Python 3.11.9 / Win10 build 26100 / AMD Family 25 Model 97
     32 逻辑核 / 单进程串行、每读新建连接含 connect 税；ts 全落当日窗口 = 最坏口径；
     每操作 1 冷态 + 2 预热 + 29 计入统计；四档 EXPLAIN 均 `USING COVERING INDEX`）：

     | 行数（两表各） | (a) 按 key 聚合 中位/p95 | (b) scoped 聚合 中位/p95 | (c1) 单写入 中位/p95 | (c2) 写+告警评估 中位/p95 | 归档删 10% 持锁 | 全库 VACUUM |
     | --- | --- | --- | --- | --- | --- | --- |
     | 10 万 | 11.6 / 17.4ms | 4.8 / 6.5ms | 9.3 / 12.9ms | 18.5 / 24.1ms | 0.52s（2.0 万行） | 0.47s |
     | 50 万 | 65.7 / 74.1ms | 27.4 / 30.7ms | 10.0 / 12.7ms | 73.7 / 80.6ms | 5.5s（10.0 万行） | 2.5s |
     | 100 万 | 127.1 / 138.2ms | 56.1 / 78.5ms | 11.8 / 19.9ms | 138.4 / 156.3ms | 7.9s（20.0 万行） | 4.4s |
     | 200 万 | 250.3 / 287.9ms | 100.5 / 110.1ms | 10.0 / 13.4ms | 253.0 / 274.7ms | 25.8s（40.0 万行） | 9.7s |

     (a)=单 key 独占当日全部流水（最坏口径，多 key 按份额摊薄）；(b)=4 组 provider/model
     均匀命中 1/4；(c1)=无配额策略 key 纯写；(c2)=有启用 day 策略 key（settle 内
     evaluate_alerts 每写多做一次全窗口聚合，c2−c1=9/64/127/243ms ≈ (a) 列，互相印证）。
   - 依据数字：聚合严格线性 **125ms/百万行** → 原外推「290 万行 ≈0.4s」实测斜率为
     0.36s（误差 ~10%，**原外推本身成立**）；但 100 万窗口行 127ms 已是单请求总预算
     ~29ms 的 4 倍、50 万行 66ms → 同步配额路径应在 **单 key 窗口 50 万行**前介入
     （分摊/预聚合/异步），早于原「百万级」；写路径与总表规模无关（10.0ms@200 万，
     探针 280 万行 12.5ms）→ 总表归档可放宽至 **≥200 万行**再做。
   - 归档操作自身成本（实测）：10% 分片单事务删除持锁 0.52→5.5→7.9→25.8s（超线性，
     越晚做单次锁越长）；大分片单事务删 100 万行（表的 55%）持锁 **65.0s**；全库
     VACUUM 持 EXCLUSIVE 0.47→9.7s；`VACUUM INTO` 导出 0.18→7.5s → 归档必须分块 +
     低峰执行，不能在线整段跑。
   - 机制修正（推断，基于 WHERE 形态 + 单次对照探针）：归档删的是窗口外旧行，而
     window_usage 的 WHERE 只覆盖当日 → 归档**不降低日窗口聚合成本**；探针显示窗口外
     +100 万行使窗口读 161→223ms（单次对照、含页缓存噪声，推断级），归档真实收益是
     索引体积/写维护/备份空间。反证：去 perf 索引克隆上同查询 0.9/7.1/14/29~34s。
   - 「现在不需要，代码不动」维持（现库 258KB、10 万行窗口 11.6ms）。

**指定测试**：`test_billing.py`、`test_logs.py`、`test_logs_stats_scope.py`、`test_quota.py`、
`test_model_resolve.py` → **92 passed**（连接改动后、索引改动后各复跑一次，均通过）。

**本次改动文件**：`apps/control/app/db.py`（PRAGMA/chmod 移到建库一次；SCHEMA key 索引升级覆盖）、
`apps/control/app/routers/v1.py`（准入段单连接显式传参 + 回滚语义）、
`apps/control/app/model_resolve.py`（`conn` 可选参数，own/borrowed 借连接）、
`apps/control/app/perf_indexes.py`（两条覆盖索引定义 + 定义比对重建）。

**剩余瓶颈（未解决，如实列）**：

1. 连接仍短命：单次 connect+首查+close ≈1.42ms，其中约 0.7ms 是 WAL 索引首触；真正归零要
   跨请求复用（0.003ms/次），被 Windows 文件锁 + 测试 tearDown 否决，需连接池连带测试基建
   改造，超出本次授权。
2. 64 并发档吞吐几乎不动（+3.0%）：写事务排队主导，写事务 8.9ms/次未动
   （`PRAGMA synchronous=NORMAL` 仍是未做的 P1）。
3. 单请求建连 8 → 4 → **2**（2026-11-04 最终值）：第三次收口把 `registry.get` 的两条元数据读
   （`_snapshot`、`_provider_configs`）改为借准入段那条连接，仅剩准入 1 次 + `_record` 1 次。
4. ledger 聚合 O(窗口行数) 线性不变（2026-11-04 实测 125ms/百万行）：归档不降当日
   窗口（WHERE 只看当日行），降延迟靠控窗口内行数（单 key 50 万行前介入）；归档只解决
   表体积与写维护，阈值改口径见上 b 结论。
5. 改前 1 轮 vs 改后 2 轮，且同机存在其它会话常驻 uvicorn（预检 8010/18155/8395/8396，
   未触碰），统计置信有限；结论看趋势（8/32 档两位数提升、locked=0）不看绝对值。

**瓶颈判断（依据）**

1. 吞吐三档 30~35 QPS 与单并发 28.3 QPS 持平 → 串行段 ≈29ms/请求（1000/34.9），加并发无收益；
   假上游 async + 全链路回环 + 0 错误 0 `locked` → 排除网络层与上游。
2. 延迟构成中 SQLite 相关 ≈61%（写入 30% + 读路径 31%）→ 排除「纯上游/框架瓶颈」。
3. 独立进程写争用 108~407 TPS ≫ 34 QPS、写锁占用率 ≈31% → 排除「写锁等待本身」作为直接上限；
   结合代码（`_gateway_key` / `_quota_guard` / `_failure_guard` / `_price_snapshot` 均为**同步 SQL、
   直接阻塞事件循环**，每请求 6~7 次 `connect` 各含 PRAGMA+chmod ≈1.8ms）→ 结论：
   **拐点在网关单进程内每请求同步 DB 访问的全套成本串行叠加**；「不是网络层」维持原判，
   「SQLite 单写者」修正为「SQLite 相关同步成本占 61%，其中写锁未打满」。
   （2026-11-04 更新：该条描述为改前状态；连接成本优化后 8/32 档 QPS 到 48.2 / 49.3、
   64 档 42.9，串行段仍是主制约，见上「连接成本优化」。）
4. 读/写在高并发下为何无法重叠到更高吞吐，需进程级 profiling 定量（事件循环内同步读、GIL 争用为
   嫌疑），本轮未做，见第 5 节未测项。

### 瓶颈量化

1. **写串行（结构性上限仍在）**：接入前多线程对同一 DB 做两次写事务（UPDATE+commit，INSERT+commit），
   8/32/64 线程实测 **18.3 / 51.9 / 31.9 ms/请求**，折算 20–50 请求/s，与压测 28–34 QPS 对齐。
   接入后节流窗口内 `last_used_at` 命中 0 行不再 commit，写事务降为 1 次；但空 commit 成本低，
   本次短压测看不出吞吐台阶变化。
   （2026-11-04 更新：接入后 1 次写事务口径的独立进程争用复测见 3.3——108~407 TPS、写锁占用率仅
   ≈31%；本条 20–50 请求/s 为接入前 2 次写事务的旧折算，接入后瓶颈构成以 3.3 为准。）
2. **每请求新建上游 client（已修复）**：旧实现 `AsyncClient` 构造 4.8s（其中 `SSLContext`
   加载本机证书库 4 次 × 1.2s），单请求上游调用 1.2s；裸跑（`--no-ssl-shim`）8 并发
   **0.7 QPS / p50 11.9s**。接入后同一裸跑口径 **19.2 QPS / p50 344ms**，说明这部分开销完全由
   client 生命周期造成，与网关逻辑无关。
3. **失败限流窗口扫描**：`945ms / 200k 行 ≈ 4.8µs/行`。当前 30 QPS × 60s ≈ 2 千行 → 约 10ms/请求，
   被写串行掩盖；若失败洪水把窗口打到 20 万行，单次准入就会多出近 1 秒。
   接入部分索引后同规模约 0.02ms/请求，20 万行窗口回落到 1.75ms。

## 4. 改进建议（按优先级）

本轮已完成 P0 两项与 P1 的索引接线；2026-11-04 又完成 P1 的连接降本与 ledger 覆盖索引
（见 3.3「连接成本优化」）。下表保留完整清单，未完成项仍需评估。

| 优先级 | 改动 | 依据 | 状态 |
| --- | --- | --- | --- |
| P0 | 上游 client 按 provider 复用（进程级 `AsyncClient` + 连接池 + 共享 SSLContext） | 裸跑 0.7 QPS → 19.2 QPS | 已完成 |
| P0 | 把每请求写事务从 2 次降到 1 次：`last_used_at` UPDATE 命中 0 行时不 commit | 写路径实测上限 20–50 请求/s | 已完成 |
| P1 | 接入 `perf_indexes.py`（两条索引） | 失败限流 945ms → 1.75ms；scoped 配额聚合 44ms → 26ms；代价 +63% 插入耗时 | 已完成（启动时接线） |
| P1 | `PRAGMA synchronous=NORMAL`（WAL 下） | 去掉每次 commit 的 fsync；风险：掉电可能丢最后几条已提交记录，需业务确认 | 未做 |
| P1 | `resolve_prices` 5 次查询合并为 1 次 `IN (token_class)` | 每请求少 4 条语句，收益小但零风险 | 未做 |
| P1 | 每请求 6~7 次 `db.connect()` 降本：PRAGMA/chmod 只在首次连接执行，或跨阶段复用只读连接 | 2026-11-04 微基准：connect 单次 1.82ms，读路径 9.4ms 中约 9 成是 connect 开销，约占单请求 29.9ms 的 30%（见 3.3） | 已完成（方向 2+3：PRAGMA/chmod 建库一次 + 准入段合并 8→4 次；连接税 11.5 → 5.7ms，压测 8/32 档 +24%/+35%；复用被 Windows 文件锁否决，见 3.3） |
| P1 | `idx_billing_ledger_scope` 升级为覆盖索引（含 `billed_micro`）或按窗口归档 ledger | 2026-11-04 规模实测：20 万行/日窗口聚合有索引仍 372ms，逐行回表（见 3.3） | 已完成（scope 与 `idx_billing_ledger_key` 均升级为覆盖索引，乱序 20 万行 1987 → 28.5ms；归档结论见 3.3 次目标 b：当前不做） |
| P2 | 目录解析路径用短 TTL 的 registry 快照缓存（现在是每请求 4 条语句 × 启用 provider 数） | 无 `x-provider` 时默认 23 条语句，14 家全启用 75 条 | 未做 |
| P2 | 对 `request_logs` / `usage_records` / `billing_ledger` 设保留期或归档 | ledger 只增；2026-11-04 四档实测：写路径与表规模无关（10ms@200 万）、窗口聚合 125ms/百万行、归档 10% 分片持锁 0.5→25.8s（见 3.3 b） | 未做（阈值改口径：单 key 窗口 50 万行前介入聚合延迟、总表 ≥200 万再归档；2026-11-04） |
| P3 | 写路径改成单写者队列/批量提交，或迁 Postgres | SQLite 单写者是结构性上限；批量提交能把 N 次 fsync 摊成 1 次 | 未做 |

## 5. 风险与前提

- 绝对数字受本机磁盘 fsync、杀毒软件、嵌入式 Python、同机并发任务影响；**趋势**（并发不涨、延迟线性、无 locked）
  比绝对值可靠，换 Linux 容器需重跑同一脚本。
- 压测默认加载 `scripts/bench/serve.py` 的 `SSLContext` 缓存垫片，只用于隔离 Windows 证书库噪声；
  它不改变业务代码。裸跑用 `--no-ssl-shim`，两种口径不要混用。
- 索引会让写入变慢（实测 +63%/20 万行），高 QPS 下要重新评估；部分索引的谓词与
  `check_failure_rate` 的 SQL 文本强耦合，改 SQL 时必须同步改索引谓词。
- 本次已接入两条索引、client 复用与空提交跳过；2026-11-04 增加连接成本优化与 ledger 覆盖索引
  （见 3.3「连接成本优化」）。3.0 表是**接入前基线**，3.1/3.2 表是**接入后**数字
  （3.2 为 11-03 干扰环境）；**3.3 为 2026-11-04 干净条件复跑、接入后主口径**。3.1 的绝对值受当时
  机器负载影响，只作 A/B 参考。
- 2026-11-04 轮未测项：SSL 垫片口径与 quota/ratelimit 场景重跑；29ms 串行段的进程级 profiling
  （嫌疑：事件循环内同步读、GIL 争用）；3.3 的微基准脚本为临时脚本、跑完即删，方法与口径已记录在
  3.3 文内可复现。
- 未改 `main.py` / `gateway_errors.py` / `routers/logs.py` / `routers/stats.py` / `apps/web`，未做任何 git 操作。
