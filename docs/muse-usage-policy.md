# muse 通道 usage 恒 0：计费口径决策

> 状态：待决策。本文只做方案比较，不含代码改动。
> 依据：2026-11-02 读到的 `apps/control/app/` 与 `config/prices.yaml`；muse 协议事实见
> `docs/providers/muse2api.md`（sidecar v1.6.6）。

---

## 0. 结论

推荐**方案 A（如实计 0）立即上线**，同时补一个标注修正：把"上游返回全零 usage"与"根本没有 usage"区分开。
方案 B 只作为"财务必须确认这条通道收入"时的可选开关，默认关闭；上线前必须先补齐 muse 别名模型的价格行。
方案 C 不建议用于单请求记账：依赖未文档化的额度接口，且并发/账号轮换下无法归因到 key，只能做总量对账。

---

## 1. 现状复核（写方案前必须先对齐的事实）

| 事实 | 位置 |
| --- | --- |
| muse 非流式响应固定返回 `usage: {prompt_tokens:0, completion_tokens:0, total_tokens:0}`，流式仅在客户端传 `stream_options.include_usage=true` 时补一个全零 usage 分片 | `docs/providers/muse2api.md` §3.2、§3.3 |
| `settle` 把**非空 dict** 的 usage 视为"有 usage"：全零对象走计价分支，`billed_micro=0`、`unpriced=[]`、`note=""`、`estimated=0` | `app/billing.py` `settle` |
| 因此 muse 非流式请求当前落的是**一条看起来完全正常的 0 元流水**，和"真实免费请求"无法区分 | `app/billing.py` `settle`；`app/db.py` `billing_ledger` |
| 背景里说的 `note="usage_missing"` 只覆盖另一种情况：流式请求没传 `include_usage`，`usage_from_stream` 返回 `None` | `app/billing.py` `settle`；`app/routers/v1.py` `_proxy` 流式分支 |
| `billing_ledger.model` 记的是**下游请求名**，不是上游真实模型：`registry.resolve` 命中 muse 后返回原 model | `app/registry.py` `resolve`；`app/routers/v1.py` `_proxy` |
| muse 常用的两个别名 `gpt-4o-mini`、`claude-sonnet` 在 `price_books` 里**没有价格行**（种子只覆盖 `gpt-4o`、`claude-sonnet-4` 等 12 个模型名） | `config/prices.yaml`；`config/model-aliases.yaml` |
| muse 上游会把 `gpt-4o`、`claude-sonnet-4` 等几乎所有名字归一成 `muse-spark`，而 `muse-spark` 自身没有价格行 | `docs/providers/muse2api.md` §3.1 |
| muse 只声明 `openai.chat.completions`（无 `/v1/messages`） | `app/adapters/muse.py` `spec` |
| M3-1 价格解析与每请求快照已就绪，`settle` 已接收准入时冻结的 `pricing` | `app/billing.py` `resolve_prices / record_price_snapshot / settle`；`app/routers/v1.py` `_price_snapshot` |

两个直接推论：

1. **方案 A 不是"什么都不用做"**。当前账面上 muse 流量与"合法的 0 元请求"混在一起，必须先修正标注，否则"缺口明确"做不到。
2. **方案 B 也不是"接个估算就能收到钱"**。muse 别名缺价格行，估算出来的 token 数套不到价，仍然是 0 元。

---

## 2. 三方案一句话对比

| 方案 | 账上表现 | 可审计性 | 误差 | 改动量 | 主要风险 |
| --- | --- | --- | --- | --- | --- |
| A 如实计 0 | 每请求 0 元，标注 `usage_zero` | 高，原始 usage 与日志齐全 | 0（对账），收入缺口 100% | 约 1 个函数、无表结构变更 | 几乎无；运营要知道这条通道不产收入 |
| B 字符估算 | 每请求按估算 token 计价，`estimated=1` | 中，可复算金额但无法验证 token 数 | 可见文本 ±15–30%；工具调用/内部多步会显著低估 | 新增估算模块 + 3 处网关挂钩 + 价格行补齐 | 计费争议；开关误开污染其他 provider |
| C 额度反推 | 单请求仍 0，按窗口写一条汇总调整行 | 低，只能验证总量 | 批级接近真实；单请求只能取平均 | 新表 + 轮询 + 上游接口探测 | 依赖未文档化接口；并发无法归因；账号轮换 |

---

## 3. 方案 A：如实计 0

### 3.1 实现改动点（文件级）

| 文件 | 改动 |
| --- | --- |
| `apps/control/app/billing.py` | `settle()` 增加一个判定：`usage` 存在但五类 token 全为 0 时，`note="usage_zero"`；`provider` 为 muse 时可写 `usage_zero:muse`。金额仍 0，`estimated` 保持 0（这是"确定没有计量"，不是估值） |
| `apps/control/tests/test_billing.py` | 新增 1 例：全零 usage 落 `note="usage_zero"`；再 1 例：`usage=None` 仍为 `usage_missing`，两者不混淆 |

不改 `v1.py`、不改表结构、不改价格表。`settle` 的判定在写库前完成，不影响现有快照逻辑。

### 3.2 对 `billing_ledger` 字段的影响

- 金额列、五类 token 列、`estimated`、`price_snapshot_id` 全部不变。
- 仅 `note` 新增一个枚举值 `usage_zero`（可带 provider 后缀）。
- 历史行保持原样，不回填：回填等于修改已发生流水，破坏 append-only。

### 3.3 可审计性

最高。`request_logs`、`usage_records`（原始全零 JSON）、`billing_ledger` 三处齐全，任何人可用原始 usage 复算出 0 元。
配合 `provider='muse'` 过滤即可精确回答"这条通道这个月免费放出去多少请求"。

### 3.4 误差量级

金额误差为 0；**收入缺口是 100%**。缺口不是"算错"，是"没数可算"，标注后可以按请求数×参考价估出机会成本，但那是分析口径，不入账。

### 3.5 上线风险

- 极低。唯一注意点：`note` 是枚举型字段，`docs/API-REFERENCE.md` §7 与前端若按旧枚举展示，需要同步补 `usage_zero` 的说明（文档改动，不改前端代码）。
- 统计页 `today_tokens/week_tokens` 对 muse 仍显示 0，属预期，不应报异常。

### 3.6 回滚方式

把 `note` 改回空字符串即可；没有迁移、没有新表。已写入的 `usage_zero` 行保留，作为历史事实。

---

## 4. 方案 B：字符估算兜底

### 4.1 实现改动点（文件级）

| 文件 | 改动 |
| --- | --- |
| `apps/control/app/usage_estimate.py`（新建） | `estimate_tokens(text)`：按字符类型加权（CJK 约 1.4 字/token，ASCII 约 4 字/token，JSON/代码约 3 字/token）；`estimate_request_usage(payload)` 遍历 `messages`（含 system、content parts、tool_calls.arguments、历史轮次）；`estimate_reply_usage(body)` 解析非流式 `choices[].message.content` 与 `tool_calls.arguments`；`estimate_stream_usage(chars)` 把流式累计字符折算成 token |
| `apps/control/app/billing.py` | `settle(..., fallback_usage=None)`：真实 usage 缺失或全零时改用 `fallback_usage`；写 `estimated=1`，`note="estimate_chars:v1,in=<chars>,out=<chars>"`。把字符数写进 note 是为了让口径可复核，只写"估算"无法审计 |
| `apps/control/app/routers/v1.py` | `_proxy` 准入后按 provider 判定是否启用估算，计算输入估算并随请求传递；非流式在拿到 `body` 后算输出；流式在 `body_stream()` 里累计 `delta.content` 字符（现在只攒 usage 分片，需要额外计数），结束后传给 `_record_safe` |
| `apps/control/app/routers/pricing.py` | 新增 `GET/POST /api/billing/policy`（admin 写、登录读），策略存 `settings` 表的 `billing` 组：`{"estimate_usage": {"enabled": false, "providers": ["muse"], "chars_per_token": {"cjk": 1.4, "ascii": 4.0}}}`。**不要塞进 `/api/settings/gateway`**：`save_gateway_config` 会用 `{"allow_anonymous": allow}` 整体覆盖，新增键会被静默清掉 |
| `apps/control/app/routers/stats.py` | `summary` 增加 `estimated_requests` 与 `estimated_credit`（`billing_ledger WHERE estimated=1` 求和），让报表能把估算金额与真实金额分开 |
| `config/prices.yaml` 或 `POST /api/pricing/books` | 必须补 `gpt-4o-mini`、`claude-sonnet` 两个别名名的价格行；否则估算 token 无价可套。价格取对应上游模型（`gpt-4o` / `claude-sonnet-4`）的官网价，`note` 写明"muse 别名，按上游模型价" |

刻意不做的事：

- 不用 UTF-8 字节数估算。中文 UTF-8 3 字节/字，按字节会把中文 token 数高估约 2 倍，且随内容类型漂移。
- 不改 `request_logs.prompt_tokens/completion_tokens`。这两列是"上游原始用量"，填估算值会污染全局 token 统计，且该表没有 `estimated` 标记可区分。估算只进 `billing_ledger`。
- 不给所有 provider 默认开启。只允许白名单内的 provider 走估算。

### 4.2 对 `billing_ledger` 字段的影响

- 复用现有五类 token 列与 `billed_micro`、`estimated`、`note`，**零新增列**。
- `estimated=1` 是唯一可靠的"这笔是估的"标记；`note` 记录估算版本与字符数，供复核。
- `price_snapshot_id` 照常写入（价格仍然来自真实价格行）。

### 4.3 可审计性

中等。金额可以用 `tokens × 快照价` 精确复算，但**token 数本身无法验证**——我们不存请求/响应正文，只有 note 里的字符数。
把 `chars` 写进 note 后，至少能验证"字符数→token 数"这一层换算是否按同一口径执行；换算系数是否接近上游真实分词，无从验证。

### 4.4 误差量级

以 `muse-spark` 自动路由 agent 的实际行为为准，分三层：

| 误差来源 | 方向 | 量级 |
| --- | --- | --- |
| 中英混排、代码、JSON 的分词差异 | 双向 | 纯中文约 ±15%；混排与代码 ±30% |
| `tool_calls.arguments`、推理/思考内容未计入 | 低估 | 工具密集轮次少记 20–60% |
| 图片/附件输入未计入 | 低估 | 有视觉输入时接近 100% |
| 上游 agent 内部多步调用、内部系统提示词 | 低估 | 对上游真实额度消耗可能只记到 1/2–1/3 |
| 历史多轮上下文 | 双向 | 我们按完整 messages 计；若上游截断则高估 |

结论：**只能保证"可见文本"的账面自洽（±30% 内），不能声称接近上游真实消耗**。这也是不建议把它当收入确认依据的核心原因。

算例（gpt-4o 官网价 input \$2.50/M、output \$10.00/M，CJK 1.4 字/token）：

- 输入 400 汉字 → 286 token → `(286 × 2_500_000 + 500_000) // 1_000_000` = **715 micro**
- 输出 200 汉字 → 143 token → `(143 × 10_000_000 + 500_000) // 1_000_000` = **1430 micro**
- `billed_micro = 2145` → **\$0.002145**，`estimated=1`，`note="estimate_chars:v1,in=400,out=200"`

### 4.5 上线风险

- **计费争议**：客户看到的金额来自推算，无法用上游账单自证。必须在前端/账单标注"估算"。
- **别名缺价**：漏补 `gpt-4o-mini` / `claude-sonnet` 会导致估算跑完仍是 0 元 + `estimated=1`，看起来"已计费"实际没有。
- **开关外溢**：策略若配成全局开关，会把 codebuddy 等有真实 usage 的 provider 也走估算。必须按 provider 白名单。
- **与 M3-2 配额冲突**：若配额窗口直接累加 `billed_micro`，估算金额会变成"按猜的数字拦人"。配额聚合必须排除 `estimated=1` 的行，或单独加安全系数。
- **性能**：流式逐帧累计字符对现有转发路径是纯内存操作，开销可忽略；但请求侧遍历 messages 要在准入时做一次，长上下文请求会增加毫秒级延迟。

### 4.6 回滚方式

1. `POST /api/billing/policy` 把 `estimate_usage.enabled` 置 `false`，立即对新请求生效（读策略在准入时进行）。
2. 已写入的 `estimated=1` 行**不修改**。若财务要求冲正，追加负金额行（`billed_micro` 为负、`estimated=1`、`note="estimate_reversal"`、原 `key_id`），保持 append-only。
3. 不需要删表、不需要数据迁移。`usage_estimate.py` 留着不影响关闭态。

---

## 5. 方案 C：按上游账户额度反推

### 5.1 前置条件（必须先确认，否则方案不成立）

muse2api 当前**没有文档化的额度接口**：`docs/providers/muse2api.md` §1 的端点表只有 `/v1/*`、`/healthz`、`/readyz`，管理页 `admin.html` 与推理接口共用同一 `MUSE2API_KEY`，但管理端路由未记录。
落地前必须读上游 `app.py` 确认：是否存在账号/额度查询路由、返回的额度单位（credit / 请求数 / 剩余次数）、是否按账号区分。任一项缺失，本方案直接不成立。

### 5.2 实现改动点（文件级）

| 文件 | 改动 |
| --- | --- |
| `apps/control/app/db.py` | 新增 `upstream_quota_samples(id, provider, account_id, sampled_at, remaining, unit, raw_json)`，只追加不更新 |
| `apps/control/app/adapters/muse.py` | 新增 `async def quota()`，请求上游管理端额度路由，解析为 `{account_id, remaining, unit}`；接口缺失时返回 `None` 而不是抛错 |
| `apps/control/app/main.py` | lifespan 里起一个轮询任务（间隔可配，默认 300 秒），失败只记日志；关闭时取消任务 |
| `apps/control/app/billing.py` | 新增 `reconcile_quota_delta(...)`：取相邻两次采样差额，写一条**汇总调整行**（`key_id=NULL`、`model='muse-spark'`、`estimated=1`、`note="quota_delta"`、`price_snapshot_id=NULL`） |
| `apps/control/app/routers/pricing.py` | 新增 `GET /api/billing/quota-samples` 与 `POST /api/billing/reconcile`（admin） |

### 5.3 对 `billing_ledger` 字段的影响

- 单请求行保持方案 A 的形态（0 元、`usage_zero`），**不填估算金额**。
- 汇总调整行借用现有列：`key_id` 为 NULL、`model` 写 `muse-spark`、金额写差额、`note="quota_delta"`。
- 若业务坚持"按 key 分摊"，需要新增 `allocated_micro` 列或新表，等于把不可观测的归因硬编码，不建议。
- 因此报表必须区分三种行：真实计费（`estimated=0`）、估算（`estimated=1` 且有 token）、额度对账（`estimated=1` 且 `key_id IS NULL`）。

### 5.4 可审计性

低到中。额度采样本身可审计（原始 JSON 落库），但：

- 单请求金额不可复核，任何一条流水都只能说"这批请求平均花了这么多"。
- 失败的请求、重试、浏览器里非 API 的消耗都会混进差额，无法剔除。
- 额度充值/重置会产生负增量，需要人工判断，自动对账会出错。

### 5.5 误差量级

- **批级**：准确度取决于轮询间隔与额度粒度。两个采样点之间只有总量可信。
- **单请求**：只能取平均。设额度最小单位 `G`、单请求平均成本 `C`，量化误差约为 `G / C`；当 `C < G`（便宜请求）时，单请求层面完全失真。
- **并发**：同一窗口内 N 个并发请求，无法拆分到具体 key；muse sidecar 内部还会在账号池里轮换，请求→账号映射对我方不可见，跨账号额度更是不可加总。

### 5.6 上线风险

- 依赖未文档化接口，上游升级（如 v1.6.7）可能直接破坏轮询。
- 轮询会与推理共用同一进程/同一 Key，可能触发上游限流或管理端鉴权失效。
- "额度"的语义未必是 token：若上游按"请求数/积分"计，套价格表会得到错误金额。
- 对账行与真实流水混在同一张表，报表口径容易出错。

### 5.7 回滚方式

1. 停轮询（配置项置 0 或直接回滚 `main.py` lifespan）。
2. 停 `POST /api/billing/reconcile`，表保留无害。
3. 已写入的 `quota_delta` 行不修改；如判断口径错误，追加一条反向调整行并在 `note` 写明原因。

---

## 6. 推荐与理由

**选 A，并立刻补 `usage_zero` 标注。** 理由：

1. muse 侧没有 token 级事实，任何金额都是二次推算；把推算值放进 `billing_ledger` 会让"真实计费"与"估算计费"在同一张表里混居，之后做配额、对账、客户账单都要额外过滤，长期成本高于短期收益。
2. 现在的价格表是**厂商官网标价**，不是 muse 的结算成本。方案 B 会在"虚构的价格"上再叠一层"虚构的用量"，两重不确定叠加，数字不具备说服力。
3. 方案 A 的缺口是**可度量**的：按 `provider='muse'` 统计请求数，乘参考价即可给出"机会成本"报表，且不污染账本。
4. 方案 C 的归因能力在架构上就不成立（账号轮换 + 并发），投入产出比最差。

**不建议现在做 B 的理由**：别名价格行缺失、估算系数无法用上游真值校准、M3-2 配额若复用金额会误拦。三者都要额外工程，且都不能提高数字的可信度。

### 渐进路径（推荐执行顺序）

| 阶段 | 触发条件 | 动作 |
| --- | --- | --- |
| P0（本次） | 立即 | 方案 A + `usage_zero` 标注；报表按 provider 统计 muse 请求数与 0 收入 |
| P1 | 财务要求"muse 通道必须计入收入" | 先补 `gpt-4o-mini` / `claude-sonnet` 价格行（与方案无关，本来就缺）；再实现方案 B，默认关闭，按 provider 白名单开启，所有行 `estimated=1` |
| P2 | muse sidecar 发布真实 usage（`usage` 非零，或新增 token 字段） | 关闭估算开关，新请求走真实 usage；历史估算行保留 `estimated=1` 不回填；对账报表把两个时期分段展示 |
| P3（仅当 P1 被否决且财务坚持总额对账） | 上游确认存在额度接口 | 才评估方案 C，且只做月度汇总调整，不做单请求分摊 |

切换判据要可观测，不靠主观判断：P2 的判据是"连续 24 小时内 muse 非流式响应的 `usage.prompt_tokens` 不再恒为 0"，由 `usage_records.usage_json` 直接查得。

---

## 7. 若采纳 A：改动清单、切换与回滚

### 7.1 改动清单（按顺序）

1. `apps/control/app/billing.py` `settle`：全零 usage → `note="usage_zero"`；`usage=None` 保持 `usage_missing`。
2. `apps/control/tests/test_billing.py`：新增 2 例（全零 usage 标注、缺失 usage 不混用），并确认现有 `estimated` 断言不变。
3. `docs/API-REFERENCE.md` §7：`note` 枚举补 `usage_zero`，说明"上游返回全零用量，金额 0，非异常"。
4. `docs/providers/muse2api.md` §6：补一句"本仓对 muse 的计费口径为如实 0，流水标 `usage_zero`"。
5. 报表侧（不改前端代码）：`/api/stats/summary` 现有字段即可支持按 provider 分析；如需页面展示，另开任务。

### 7.2 切换步骤

1. 部署上述改动（无表结构变更，已有库直接启动）。
2. 用 muse 发一笔非流式请求，查 `billing_ledger` 最新行：`billed_micro=0`、`estimated=0`、`note='usage_zero:muse'`。
3. 再发一笔流式且不带 `stream_options.include_usage` 的请求，确认落 `note='usage_missing'`，与上一条可区分。
4. 记录基线：`SELECT COUNT(*), SUM(billed_micro) FROM billing_ledger WHERE provider='muse'`，作为缺口报表起点。

### 7.3 回滚步骤

1. 回滚 `billing.py` 的 note 分支（单文件、无迁移）。
2. 已写入的 `usage_zero` 行保留；报表按时间点切分即可。
3. 不需要清理任何表或索引。

---

## 8. 仍需上游确认的问题（决定后续能否走 P2）

1. muse2api 是否有 roadmap 要补真实 usage；若永远不补，P1 的估算就是长期方案，需要财务先接受误差口径。
2. `stream_options.include_usage` 默认行为是否会在未来版本改为"总是返回 usage"；若会，流式账可直接转真实。
3. 上游 agent 的内部多步调用是否计入 muse.ai 账号额度（决定方案 B 的系数是否需要整体放大）。
4. 管理端是否存在额度查询路由（决定方案 C 是否成立），需读上游 `app.py` 路由表确认，不能凭 `admin.html` 有账号卡片推断。
