# 全站一致性审计（Neosub2api）

- 审计日期：2026-11-03
- 范围：`apps/control`（FastAPI 控制面）、`apps/web`（Next.js 前端）、`docs/`
- 方法：只读静态审计。后端路由用正则抽取 `APIRouter` 前缀 + 装饰器，前端路径用全量 `'/api/...'` 字面量抽取，i18n 用脚本做叶子键对齐；未启动服务、未跑测试、未执行 git。
- 口径：**以代码为准**。文档与代码不一致时按代码记录，并标注「文档过时」。
- 红线：不改前端外观/导航结构，不建议重做架构；外观以 `docs/FRONTEND-BASELINE.md` 为准。
- 产出：仅新增本文件，未修改任何源码或既有文档。
- 复核（2026-11-03，提交 `dc82454` → `e6cf315` 之后）：按工作区代码与最近一次全量 `pytest` 基线（2026-11-04 实测：443 passed, 1 skipped, 0 failed, 0 xfailed，收集 444 条，量级约 444）就地更新状态；原审计记录保留，修正处以「状态」「修正记录」标注。
- 文档收口（2026-11-03）：新增 [docs/README.md](README.md) 与 [CHANGELOG.md](../CHANGELOG.md)，重写仓库根 [README.md](../README.md)。测试数量以最近一次全量 `pytest` 结果为准（2026-11-04 实测量级约 444），历史快照里的 `250` 不再作为当前基线；本轮新增的 4 个 `/api/settings/runtime-config*` 端点补入附录 A8，附录 F 同步记录已修与仍未修的文档矛盾。

## 0. 结论概览

| 级别 | 数量 | 含义 |
| --- | --- | --- |
| P0 | 4 | 页面/核心功能当前直接不可用，或会崩溃 |
| P1 | 8 | 明确的功能缺口、权限/契约冲突、用户可见错误 |
| P2 | 10 | 一致性债、死代码、文档漂移，暂不影响主流程 |

后端管理端点（不含 `/v1/*`）共 **87** 个（原 83，本轮新增 4 个运行参数端点）；前端调用但后端不存在的「幽灵端点」按路径计原为 **27 条**（15 组），本轮改指真实端点 8 条、随页面删除 16 条；原剩的 **3 条**死方法已从 `api.ts` 删除，2026-11-04 复跑 `scripts/contract_check.py` 实测幽灵端点 0、方法不匹配 0（见附录 C）。原记录写的「17 个」是按行计数、把多路径行算成 1 导致的低估，已按路径重算。

### 处理状态（2026-11-03 复核；2026-11-04 部分条目再核）

| 条目 | 状态 | 最终做法 |
| --- | --- | --- |
| P0-1 模型页端点 | 已修 | 前端改调 `GET /api/models`，后端同步扩展条目字段 |
| P0-2 任务页幽灵端点 | 已降级删除 | 页面、入口、账号页跳转与 i18n 整体删除 |
| P0-3 OAuth 双入口 | 已修 | 两个弹窗改调 `/api/auth/start` 与 `/api/auth/poll` |
| P0-4 Playground 429 | 已修（前端） | 补 429 映射 + 对象 `detail` 取 `message`；后端 `detail.message` 仍含 micro-USD 原文 |
| P1-1 用量页 5 个 404 | 已降级删除 | 移除 5 个未实现调用与对应图表/按钮 |
| P1-2 更新面板 404 | 已降级删除 | 删除「开始更新/固定 ref」入口，只保留只读检查 |
| P1-3 重启路径 | 已修 | `api.ts` 改指 `/api/accounts/restart` |
| P1-4 三语言 i18n | 已修 | 三语言补齐；实测真实漏翻 0（见条目修正记录） |
| P1-5 `common.delete` | 已修 | 5 个 locale 均补上该键 |
| P1-6 错误呈现不统一 | 未修 | 本轮未动 |
| P1-7 viewer 设置页 403 | 已修 | `isAdmin` 守卫 + `ProvidersSection` 早退 |
| P1-8 令牌选项冲突 | 已修 | UI 收敛为只读作用域，创建只发 `name` |
| P2-1 micro 换算 | 已修 | 统一到 `lib/format`（`usdToMicro` / `fmtUsd` / `fmtMicroUsd`） |
| P2-2 日志原始 JSON | 已修 | 日志页对配额拒绝结构化渲染（`failure_rate_limited` 尚未结构化） |
| P2-3 viewer 自动 admin 请求 | 已修 | `accounts`、`dashboard` 均已加 `isAdmin` 守卫（2026-11-04 复核 `dashboard/page.tsx:79`） |
| P2-4 审计别名 | 未修 | 两个路径仍并存 |
| P2-5 Upstash 测试按钮 | 已修 | 按钮已移除 |
| P2-6 无调用方方法 | 已修 | OAuth 批量与认领计划已删；`credits` / `reloadState` / `reloadUpstream` 也已从 `api.ts` 删除（2026-11-04 复核） |
| P2-7 集成方案文档过时 | 未修 | 本轮仅改 `docs/` 的事实性段落，未重写该方案文档 |
| P2-8 grok 文档自相矛盾 | 已修 | 原记「部分修：`frontend-integration-plan.md` 顶部加 `/tasks` 删除注记」；2026-11-04 已按代码统一口径为「无界面入口」，原句留档 |
| P2-9 页面白名单冲突 | 未修 | 未补批准记录 |
| P2-10 后端中文文案 | 部分修 | 网关路径有稳定错误码；管理面仍中文硬编码 |

---

## P0

### P0-1 模型页整页取数失败：前端调 `/api/model-catalog`，后端只有 `/api/models`

- **问题**：模型页挂载即调用 `modelApi.catalog()`，但后端没有 `/api/model-catalog` 路由；页面 `catch` 后只剩错误态，模型列表、筛选、统计全部不可用。
- **证据**：
  - `apps/web/lib/api.ts:296` → `get<ModelCatalog>('/api/model-catalog', ...)`
  - `apps/web/app/(main)/models/page.tsx:157` → `await modelApi.catalog(realm, force)`
  - `apps/control/app/routers/models.py:28` → 只有 `GET /api/models`（返回 `models/source/count`，无 `source_label/via/summary/cached` 等 catalog 字段）
- **影响**：`/models` 100% 无数据；错误提示是裸 404，用户无法自助恢复。
- **建议修法**：二选一——后端补 `GET /api/model-catalog`（复用各 adapter 的 `list_models()` 组装 `ModelCatalog`），或前端改调 `/api/models` 并删掉依赖 catalog 专属字段的展示块。
- **状态（2026-11-03）**：已修。没有新增 `/api/model-catalog`；前端删掉 `modelApi.catalog()`，模型页改用 `upstreamApi.models()` → `GET /api/models`（`apps/web/lib/api.ts:223`）。后端同时把该端点扩成聚合契约（`provider` / `providers` / `source` / `provider_states` / `unavailable_reason` / `fallback_reason`，支持 `?provider=`），见 API-REFERENCE §5。
- **改动面**：后端新增 1 个只读端点（小，约 40 行）；或前端 1 页 + 类型（中）。

### P0-2 任务页核心功能与三个日志区全部 404

- **问题**：任务页的签到记录、上游日志、任务日志、任务执行/停止/采集、领取计划，前端都调用了后端不存在的端点。后端 `routers/` 下没有任何 `checkin-logs`、`task-logs`、`upstream/logs`、`task-run`、`task-claim-schedule` 路由。
- **证据**：
  - `apps/web/lib/api.ts:244-252`（checkin-logs / task-logs / upstream logs）
  - `apps/web/lib/api.ts:276-282`（task-run / task-claim-schedule）
  - `apps/web/app/(main)/tasks/page.tsx:176-178`（三路 `allSettled` 全部指向幽灵端点）
  - `apps/web/app/(main)/tasks/page.tsx:216`（采集任务日志）
  - `apps/web/components/common/tasks/TaskRunnerPanel.tsx:44,70,84`
  - `apps/web/components/common/accounts/AccountTaskDialog.tsx:64,99,112`
- **影响**：`/tasks` 页三个日志区永远为空；「预览/领奖/点亮/停止」按钮必失败；账号行内的「活动任务」同样不可用。这是整页级功能缺失，不只是少个入口。
- **建议修法**：先做产品取舍——若要保留任务能力，后端补 `task-run` 与日志端点；若确认下线，则前端移除 `TaskRunnerPanel`、`AccountTaskDialog` 与三个日志区，避免留下死按钮。
- **状态（2026-11-03）**：已降级删除。`/tasks` 页面、`TaskRunnerPanel`、`AccountTaskDialog`、dock 导航项、账号页跳转，以及 5 个语言文件里的任务相关键全部删除；后端仍然没有这些路由，所以没有补接口。附录 C 中本条的 5 行共 **9 条**路径（签到记录 2、任务日志 3、上游日志 1、任务执行 2、认领计划 1）已从前端消失。
  - 数量口径：本轮记录为「9 条路径」。外部说法中的「11 个」在本工作区无法核对（相关代码已删、旧文件不在仓库，且未做 git 追溯），此处不采信，标为**未核实**。
  - i18n 同口径：删除键数按原提交记录为「141 个键/语言」，工作区只能确认残留为 0，具体条数**未核实**。
- **改动面**：后端新增 1 个 router（大，含 runner 状态机）；或前端删 3 个组件/区块（中）。

### P0-3 OAuth 添加账号的两个入口必失败

- **问题**：账号页头部的「添加 OAuth」「批量 OAuth」走 `/api/oauth/*`，后端只提供 `/api/auth/start|poll`；两个弹窗均未接后端真实路径。单个「扫码添加」走的是 `accountApi.start()`（`/api/auth/start`），是正常的。
- **证据**：
  - `apps/web/components/common/accounts/OAuthDialog.tsx:86,99,174,185` → `oauthStart` / `oauthPoll`
  - `apps/web/components/common/accounts/BatchOAuthDialog.tsx:181,196` → `oauthStart` / `oauthPoll`
  - `apps/web/lib/api.ts:154-189` → `/api/oauth/start`、`/api/oauth/{id}/poll`、`/api/oauth/batch*`
  - 后端仅有 `apps/control/app/routers/auth.py:140`（`POST /api/auth/start`，ADMIN）与 `:151`（`GET /api/auth/poll`，ADMIN）
- **影响**：管理员点击两个显眼按钮必定 404；批量入口完全不可用。用户会误判为账号服务故障。
- **建议修法**：把两个弹窗切到 `/api/auth/start|poll`（保留现有 UI），或按 `docs/design/frontend-integration-plan.md:90`「注册任务不进界面」的取向直接移除这两个按钮，只保留 `AddAccountDialog`。
- **状态（2026-11-03）**：已修。`OAuthDialog.tsx` 与 `BatchOAuthDialog.tsx` 都改走 `accountApi.start()` / `accountApi.poll()`，`/api/oauth/*` 的批量方法已从 `api.ts` 删除；弹窗 UI 保留。
- **改动面**：2 个组件 + `api.ts`（中）；若删按钮则小。

### P0-4 Playground 配额 429 会造成消息气泡渲染崩溃，并泄漏原始 micro 数值

- **问题**：网关配额超限返回 429，`detail` 是结构化对象；Playground 未映射 429，且把 `j.detail` 直接赋给消息 `content`（对象），React 渲染对象子节点会抛错。同时后端拒绝文案直接拼 `micro-USD` 原始数值。
- **证据**：
  - `apps/web/app/(main)/playground/page.tsx:40-45` → `statusKey` 只映射 401/502/503，**没有 429**
  - `apps/web/app/(main)/playground/page.tsx:170-185` → `detail = j.detail || j.error?.message`，随后 `content: text2`
  - `apps/control/app/routers/v1.py:127-131` → 429 `detail` 为 `denial` 对象
  - `apps/control/app/billing.py:463-466` → `已用 {used} micro-USD，上限 {limit} micro-USD`
- **影响**：任何被配额限流的 key 在 Playground 一发请求，对话区直接报 React 渲染错误；`/api/logs` 里也能看到同一条原始 micro 文案。
- **建议修法**：`statusKey` 增加 `429 → playground.err429`（用 `reset_after` 与 USD 化后的上限做插值）；对对象型 `detail` 统一取 `detail.message` 作为兜底。
- **状态（2026-11-03）**：已修（前端）。`statusKey()` 增加 `429 → playground.err429`，`readableDetail()` 对对象型 `detail` 统一取 `.message`，消息气泡不再收到对象。残留：后端 `billing.py` 的 `detail.message` 仍拼 `micro-USD` 原始数值（`used / limit` 未换算），经 API 直接读到时仍不可读；日志页已就地换算（见 P2-2）。
- **改动面**：`playground/page.tsx` 1 文件、`i18n` 5 个 locale 各加 1 键（小）。

---

## P1

### P1-1 用量页 5 个统计调用无后端端点，每次加载都弹 404 提示

- **问题**：`statsApi.byModel/byKey/upstream` 与 `rebuildUsage/repairUsage` 均无后端实现；`load()` 对 `allSettled` 中任意 rejected 都弹错误，因此打开页面或每次心跳（60s）都会出现一次失败提示。
- **证据**：
  - `apps/web/lib/api.ts:394-409`
  - `apps/web/app/(main)/stats/page.tsx:110-113`（by-model / by-key 进入 `allSettled`）
  - `apps/web/app/(main)/stats/page.tsx:119`（任一 rejected → `notify.err`）
  - `apps/web/app/(main)/stats/page.tsx:122`（upstream 单独拉，失败已静默）
  - 后端仅有 `apps/control/app/routers/stats.py:35`（summary）与 `:103`（daily）
- **影响**：页面主体可用，但持续报错；by-model/by-key 图表恒空，`rebuild/repair` 按钮点了没反应。
- **建议修法**：先把未实现的 3 个前端调用从 `load()` 移除并隐藏对应图表/按钮；后续补齐后端时再加回。不要用静默 catch 掩盖 404。
- **状态（2026-11-03）**：已降级删除。`stats` 页不再调用 `byModel` / `byKey` / `upstream` / `rebuildUsage` / `repairUsage`（页面中已无这些引用），对应图表、按钮与 `allSettled` 分支一并移除，只保留 `summary` 与 `daily` 两个真实端点；`api.ts` 里的 5 个方法也已删除。
- **改动面**：`stats/page.tsx` 1 文件（中）。

### P1-2 设置页「一键更新 / 固定上游 ref」按钮 404

- **问题**：`systemApi.startUpdate()` 与 `setUpstreamRef()` 指向不存在的 `POST /api/system/update`、`POST /api/system/upstream-ref`；后端 system 路由只有 4 个 GET。
- **证据**：
  - `apps/web/lib/api.ts:458-462`
  - `apps/web/components/common/settings/UpdatePanel.tsx:162,177,365,386,592`
  - `apps/control/app/routers/system.py:79,105,116,146`（changelog / versions / check-update / update-status，全部 GET）
- **影响**：更新面板的「开始更新」「固定/取消 ref」必失败；只剩「检查更新」可用，但 UI 未把二者区分成可用/不可用状态。
- **建议修法**：后端补两个写端点，或前端把未实现的按钮改为禁用并附「当前部署不支持在线更新」说明。
- **状态（2026-11-03）**：已降级删除。前端删掉「开始更新」「固定/取消 ref」按钮与 `systemApi.startUpdate` / `setUpstreamRef`，`UpdatePanel` 只保留「检查更新」等只读能力；后端仍是 4 个 GET，未新增写端点。
- **改动面**：后端 2 端点（中）；或前端 1 组件（小）。

> **（2026-11-05 已更正，原证据行保留作历史）** 证据里的 `check-update` 已于 2026-11-05 下线（`apps/control/app/routers/system.py` 不再注册该路由），`system.py:116` 的 handler 已不存在；当前 `system.py` 只有 changelog / versions 两个 GET，`update-status` 已迁到 `apps/control/app/routers/update.py`。
>
> **这条为什么重要**：按「4 个 GET」核对路由数会把已删除端点算进现状，去 `system.py:116` 找实现只会扑空；也会让更新面板对接时误以为旧壳仍可读，从而漏掉 `update-status` 已换文件的迁移判断。

### P1-3 「强制重启上游」路径写错，管理员点击必 404

- **问题**：前端调 `POST /api/restart`，后端实际路由是 `POST /api/accounts/restart`。
- **证据**：
  - `apps/web/app/(main)/accounts/page.tsx:307` → `accountApi.restart()`
  - `apps/web/lib/api.ts:271` → `post('/api/restart')`
  - `apps/control/app/routers/accounts.py:236` → `@router.post("/restart")`，前缀为 `/api/accounts`
- **影响**：账号页唯一的「重启上游」操作不可用；后端能力存在但前端接错。
- **建议修法**：`api.ts:271` 改成 `/api/accounts/restart`。
- **状态（2026-11-03）**：已修。`accountApi.restart()` 现请求 `/api/accounts/restart`（`apps/web/lib/api.ts:216`）。
- **改动面**：1 行。

### P1-4 日语/韩语/繁体整段回落中文（原文记为「各缺 261 键」，实际真实缺口 42 条，已修正）

- **问题**（原始记录，数字有误，见下方修正）：`ja/ko/zh-TW` 与 `en + zh-CN` 的键集合相比各缺 261 个叶子键（其中约 20 个是复数形式差异：en 用 `.one/.other` 对象、三种语言用单数串，属结构差异，净缺口约 240）。缺整段的主要是 `providers`(80)、`quota`(79)、`pricing`(38)、`security`(21)、`playground`(13)，另有 `nav.quota`、`nav.pricing` 两个导航键。
- **证据**（原始）：
  - `apps/web/lib/i18n/locales/{ja,ko,zh-TW}.json`（脚本对齐结果：missing=261，extra=0）
  - `apps/web/components/common/layout/ManagementBar.tsx:450,456` → `t('nav.quota')` / `t('nav.pricing')`
  - 回退逻辑：`apps/web/lib/i18n/index.ts` 找不到键时回落 `zh-CN`，再回落键名
- **影响**：非中英用户看到的导航、配额、服务商、定价、安全、演练场页面大量中文；这三套语言实际处于「半成品」状态。
- **建议修法**：补齐上述 5 段 + 2 个导航键；把这套「键集合对齐」脚本接进 CI，新增键必须五语言同 PR。
- **状态（2026-11-03）**：已修。`zh-TW` / `ja` / `ko` 各补 249 条、`zh-CN` 与 `en` 各补 5 条。补齐条数取自提交记录；工作区只剩结果、旧文件不在仓库，逐条数量**未核实**，以下以当前实测为准。
- **修正记录：为什么「261」不成立**：

  1. 261 把三类性质不同的差异混成了一个数（按原快照构成推算）：**约 42 条真实缺文案 + 203 条 `phrases.*` + 16 个复数结构变体**。
  2. `phrases` 不是界面文案，而是 `tp()` 用的「后端中文原文 → 译文」反向表（`lib/i18n/index.ts:124-133`）。`zh-CN` 是原文语言，按设计不建这张表、直接回退中文；其余四语言需要它。它和 UI 文案是两个工作流，混在一起统计会误导排期。
  3. 261 还是个会漂移的快照：统计之后 `zh-CN` 与 `en` 各新增 5 个键，并集变大，所以「261 − 249」推不出最终状态（261 − 249 + 10 ≠ 16），这个数已不可复现，不能继续当基线引用。
  4. 真正需要人工翻译的是那 42 条；按 261 立项会把工作量放大约 6 倍。

- **当前实测**（用工作区 `.runtime` Python 展开五个 locale 的叶子键，与 `en ∪ zh-CN` 比较）：

  | locale | 叶子键 | 相对并集缺少 | 相对并集多余 | 结论 |
  | --- | --- | --- | --- | --- |
  | `zh-CN` | 1170 | 219 | 0 | 203 个 `phrases.*`（设计上回退中文原文，不算漏翻）+ 16 个复数变体 |
  | `en` | 1381 | 8 | 0 | 缺 8 个中文单数键（`format.minutes` 等），结构差异 |
  | `zh-TW` | 1373 | 16 | 0 | 仅差复数变体，**真实漏翻 0** |
  | `ja` | 1373 | 16 | 0 | 同上 |
  | `ko` | 1373 | 16 | 0 | 同上 |

  并集共 1389 个叶子键；`common.delete` 已在五个 locale 全部存在（P1-5）。
- **改动面**：3 个 JSON（大，纯文本，无逻辑风险）。

### P1-5 `common.delete` 在全部 5 个 locale 都不存在，按钮显示字面量

- **问题**：服务商凭据删除确认框使用 `t('common.delete')`，但 `common` 段没有 `delete` 键（`en.json:86` 的 `"delete"` 属于 `quota` 段，不是 `common`）。
- **证据**：
  - `apps/web/components/common/settings/ProvidersSection.tsx:677`
  - `apps/web/lib/i18n/locales/en.json:5-26`（`common` 段无 `delete`；`:86` 在 `quota` 段）
  - 脚本核对：`common.delete` 在 `en/ja/ko/zh-CN/zh-TW` 全部为 `N`
- **影响**：所有语言下删除确认按钮都显示 `common.delete`；这是当前唯一一个「所有语言都坏」的键。
- **建议修法**：5 个 locale 的 `common` 段各加 `"delete"`。
- **状态（2026-11-03）**：已修。五个 locale 的 `common` 段都有 `common.delete`。
- **改动面**：5 文件各 1 行。

### P1-6 同一类失败在三处呈现不同：上游 401/502、内网地址、保存失败

- **问题**：错误契约没有统一。
  1. 网关 `/v1/*`：上游 `>=400` 时**原样透传上游响应体**（这是协议兼容所需，合理）；但管理面同一失败只给「上游 /x 返回 401」，上游 body 仅进日志。
  2. Playground 把所有 401 都显示为「密钥无效」，即使 401 来自上游而非本地 key 校验。
  3. 内网地址被拒：provider 保存路径返回 4xx，设置页保存上游路径却返回 **HTTP 200 + `{ok:false}`**，前端因此不会走错误分支。
- **证据**：
  - `apps/control/app/routers/v1.py:287-301`（透传上游 body）
  - `apps/control/app/adapters/client.py:79-86`（管理面 `ProviderError(status, "上游 {path} 返回 {status}")`）
  - `apps/web/app/(main)/playground/page.tsx:41`（401 统一映射 `playground.err401`）
  - `apps/control/app/net_guard.py:94-125`（长文案拒绝内网/环回/元数据地址）
  - `apps/control/app/registry.py:297,415`（转 `ProviderError(400, ...)`）
  - `apps/control/app/routers/settings.py:211-217`（`push` 失败返回 200 + `{ok:false, message}`）
- **影响**：同一个「上游 401」在网关、管理面、演练场得到三种文案；设置页保存内网地址时前端显示「已保存」，但实际下发失败，状态码为 200。
- **建议修法**：保留网关透传（协议要求）；统一管理面错误为 `{detail, code}` 且非 2xx；设置页 `push.ok=false` 时返回 4xx/5xx，前端再统一走 `errText`。
- **状态（2026-11-03）**：未修。本轮只处理 P0/P1 中的端点与 i18n 问题，三处呈现差异保持原样，仍按原建议排期。
- **改动面**：后端 3 处 + 前端 2 处（中）。

### P1-7 只读用户打开设置页会静默 403，界面把「没权限」显示成「没配置」

- **问题**：`settings/page.tsx` 无条件拉 `upstream` 与 `model-map`（两者均 `admin_user`），`ProvidersSection` 无条件拉 `gateway`（也是 `admin_user`）。viewer 拿到 403 后 `allSettled` 静默，`cfg` 保持空，页面显示「未加载」。
- **证据**：
  - `apps/web/app/(main)/settings/page.tsx:873-877`
  - `apps/web/components/common/settings/ProvidersSection.tsx:112-118`
  - `apps/control/app/routers/settings.py:150`（upstream GET，ADMIN）、`:222`（gateway GET，ADMIN）、`:268`（model-map GET，ADMIN）
  - 只读提示只在 `apps/web/app/(main)/settings/page.tsx:1409` 的上游 tab 出现
- **影响**：viewer 无法区分「管理员还没配置」与「自己没有权限」；每次进设置页产生 2-3 个 403。
- **建议修法**：`isAdmin === false` 时跳过这三个请求，直接渲染只读占位（已有 `settings/tokensAdminRequired` 同类文案可复用）。
- **状态（2026-11-03）**：已修。`settings/page.tsx` 拉 `upstream` / `model-map` 前判 `isAdmin`，`ProvidersSection` 在非管理员时直接 `return`（`ProvidersSection.tsx:115`），viewer 进设置页不再产生这三个 403。
- **改动面**：2 文件（小）。

### P1-8 令牌面板提供后端明确拒绝的选项

- **问题**：UI 提供 `scope: admin` 与 30/90/365 天过期；后端 `_reject_unsupported()` 对 `scope != readonly` 和任何非空 `expires_at` 直接 400。同时 400 文案是中文硬编码。
- **证据**：
  - `apps/web/components/common/settings/TokensPanel.tsx:50-52,105-106,120,139-146`
  - `apps/control/app/routers/tokens.py:51-55`（两条 400 拒绝）
  - `apps/control/app/routers/tokens.py:53,55`（中文硬编码）
- **影响**：管理员按 UI 选项创建令牌必失败；这是「前端允许、后端禁止」的契约冲突，不是权限问题。
- **建议修法**：UI 收敛为 `readonly` + `never`（与后端一致），或后端放开 admin scope / 过期时间；拒绝文案改为可翻译的错误码。
- **状态（2026-11-03）**：已修（前端）。`TokensPanel` 创建令牌只提交 `name`，不再提供 `scope` / 到期选项；列表只展示后端已有值。后端拒绝逻辑未改。
- **改动面**：1 组件 + 可能 `types.ts`（小）；若后端放开则中。

---

## P2

### P2-1 micro-USD ↔ USD 换算有两处独立实现

- **问题**：配额策略页自定义 `MICRO_PER_USD` 与 `usd()`；定价页直接写 `Math.round(usd * 1e6)`。两处都正确，但没有共用实现。
- **证据**：
  - `apps/web/app/(main)/quota/policies/page.tsx:55,57-58,284,665,870`
  - `apps/web/app/(main)/pricing/page.tsx:136`
- **影响**：未来改精度/四舍五入/展示格式要改两处；容易只改一处造成两页金额不一致。
- **建议修法**：抽 `lib/money.ts`（`usdToMicro` / `microToUsd` / `fmtUsd`），两页替换。
- **状态（2026-11-03）**：已修。换算集中到 `apps/web/lib/format.ts`（`usdToMicro` / `fmtUsd` / `fmtMicroUsd`），`quota/policies`、`pricing`、`logs`、`playground` 均改为 import，页面里不再有 `1e6` / 手写四舍五入。文件名与原文建议的 `lib/money.ts` 不同，功能一致。
- **改动面**：新增 1 个小模块 + 2 页（小）。

### P2-2 日志抽屉直接展示配额拒绝的原始 JSON（含 micro-USD）

- **问题**：网关拒绝请求时把 `denial` 整个 JSON 写进 `request_logs.error`；日志页原样展示，用户看到 `{"type":"quota_exceeded","used":123456,...}` 和 `micro-USD`。
- **证据**：
  - `apps/control/app/routers/v1.py:96-101`（写入 `json.dumps(denial)`）
  - `apps/web/app/(main)/logs/page.tsx:303`（`detail.error` 原样渲染）
- **影响**：日志详情对普通用户不可读，且泄漏内部单位；与 P0-4 是同一文案在两处的表现。
- **建议修法**：日志页对 `type === 'quota_exceeded'` 做结构化渲染（USD + 重置时间），其他错误保留原文。
- **状态（2026-11-03）**：已修。日志页按 `quota_exceeded` 分支渲染 `used/limit`（`fmtMicroUsd`）与重置时间，不再打印原始 JSON；`failure_rate_limited` 仍走原文分支（新错误，未纳入结构化）。
- **改动面**：`logs/page.tsx` 1 文件（小）。

### P2-3 viewer 每次进账号页/仪表盘都自动请求 ADMIN 端点

- **问题**：`accounts` 页挂载后无条件调用 `refreshCredits(false)`（ADMIN），`dashboard` 同样；viewer 每次进入都吃 403，且被 `catch` 静默。
- **证据**：
  - `apps/web/app/(main)/accounts/page.tsx:117-140`
  - `apps/web/app/(main)/dashboard/page.tsx:76`
  - `apps/control/app/routers/accounts.py:122`（`refresh-credits`，ADMIN）
- **影响**：每次进页多一次必然失败的请求；无用户可见错误，但服务端日志/审计会持续出现 403 噪声。
- **建议修法**：与 P1-7 同口径，`isAdmin` 为 false 时跳过；或后端允许 viewer 读缓存值、仅 `force=true` 限管理员。
- **状态（2026-11-03 部分已修；2026-11-04 复核已修）**：`accounts/page.tsx:113` 已加 `if (!caps.credits || !isAdmin) return`；`dashboard/page.tsx:79` 的 `refreshCredits(false)` 也已放入 `isAdmin` 分支（`useAuth` 见 `:55`），viewer 进仪表盘不再自动打 admin 端点。
- **改动面**：2 文件（小）。

### P2-4 审计日志有两个等价端点

- **问题**：同一函数同时注册 `/api/audit-logs` 与 `/api/security/audit`；前端只用前者。
- **证据**：`apps/control/app/routers/security.py:112-113`
- **影响**：OpenAPI 出现两条重复路径，后续改一处容易漏另一条。
- **建议修法**：保留 `/api/audit-logs`，把别名标记 `deprecated=True` 或删除（前端无引用）。
- **状态（2026-11-03）**：未修。两条路径仍并存。
- **改动面**：1 行。

### P2-5 Upstash 测试按钮固定返回「M1 未接入」

- **问题**：设置页有「测试 Upstash」按钮，后端固定返回 `ok:false, message:"M1 未接入 Upstash"`。
- **证据**：
  - `apps/control/app/routers/settings.py:296-316`
  - `apps/web/lib/api.ts:429-430`
- **影响**：按钮永远失败，且文案把内部里程碑编号暴露给用户。
- **建议修法**：未接入期间隐藏按钮，或改成「即将支持」的禁用态。
- **状态（2026-11-03 已移除前端按钮；2026-11-04 后端已改真实实现）**：设置页组件里已无 Upstash 相关按钮（`components/common/settings` 下无匹配）；`POST /api/settings/upstash/test` 已从桩改为真实只读探测——`POST {url}/pipeline` 下发 `[["PING"]]` + Bearer，`trust_env=False`、超时 10s，失败走 `{detail, error{type, code}}` 外壳（`apps/control/app/routers/settings.py:799-801`，错误码表 `docs/API-REFERENCE.md:610-619`），不再是固定文案。
- **改动面**：1 组件（小）。

### P2-6 前端 `api.ts` 存在无 UI 调用方的方法，其中 1 个指向不存在的读端点

- **问题**：`accountApi.credits`（`/api/accounts/{file}/credits` 后端不存在）、`oauthBatchStart/Status/List`、`taskClaimSchedule/saveTaskClaimSchedule`、`settingsApi.reloadState/reloadUpstream` 均无 UI 调用；前两组还各自带着幽灵端点。
- **证据**：
  - `apps/web/lib/api.ts:205-213`（credits）
  - `apps/web/lib/api.ts:172-189`（oauth batch）
  - `apps/web/lib/api.ts:280-282`（claim schedule）
  - `apps/web/lib/api.ts:432-435`（reload state / reload）
- **影响**：后续维护者会误以为这些能力已接通；类型与后端契约持续漂移。
- **建议修法**：删除方法或统一标注 `@deprecated` + 指向真实端点；`credits` 若要保留，后端补端点或改用 `refresh-credits`。
- **状态（2026-11-03 部分已修；2026-11-04 已修）**：`oauthBatchStart/Status/List`、`taskClaimSchedule/saveTaskClaimSchedule` 已随任务页删除；`accountApi.credits`、`settingsApi.reloadState`、`settingsApi.reloadUpstream` 也已从 `apps/web/lib/api.ts` 删除（全仓 `reload-state`、`upstash/reload`、`accounts/{file}/credits` 均 0 命中，后端本就没有这三个路由），复跑 `scripts/contract_check.py` 实测幽灵端点 0、方法不匹配 0。
- **改动面**：`api.ts` 1 文件（小）。

### P2-7 文档过时：称 `tokens.py` / `system.py` 未挂载，实际已挂载

- **问题**：集成方案文档说控制面只挂载 11 个模块，`tokens.py`、`system.py` 未 `include`，并把 `/api/tokens`、`/api/system/changelog`、`/api/system/update-status` 列为 P0 阻塞项；代码里这些路由已挂载且前端在用。
- **证据**：
  - `docs/design/frontend-integration-plan.md:10,12,198-200,299-301`
  - `apps/control/app/main.py:179-181`（模块元组包含 `tokens`、`system`，并逐个 `include_router`）
- **影响**：按文档派活会做重复工作；新人会误判后端能力。
- **建议修法**：更新该文档为「已挂载」，删除对应 P0 阻塞项。
- **状态（2026-11-03）**：未修。本轮只修正事实性数字与状态，未重写集成方案文档。
- **改动面**：文档 4 处（本次不改，仅记录）。

### P2-8 文档自相矛盾：grok 注册任务「已有入口」与「不进界面」

- **问题**：同一文档一处说注册任务入队已有（任务页 TaskRunnerPanel），另一处说注册任务不进界面；代码事实是后端有 6 个 grok 端点、前端 `api.ts` 完全没有 `/api/grok/*`，TaskRunnerPanel 用的是 `/api/task-run`（与 grok 无关）。
- **证据**：
  - `docs/design/frontend-integration-plan.md:61`（称已有入口）
  - `docs/design/frontend-integration-plan.md:90`（称不进界面）
  - `apps/control/app/routers/grok.py:26-72`
  - 全量前端路径抽取结果中无 `/api/grok`
- **影响**：grok 注册能力的「是否该有页面」无法从文档判定。
- **建议修法**：明确取舍并在文档写死一条；若不做页面，同时清理 `docs` 中的「任务页已有」表述。
- **状态（2026-11-03）**：部分处理。任务页已整体删除，「任务页 TaskRunnerPanel」这一前提不再成立；`docs/design/frontend-integration-plan.md` 顶部已加删除注记（另两份设计文档本就标注「仅作存档」），grok 是否单独做页面仍未定论。
- **状态（2026-11-04）**：已修。按代码实际状态统一为一套口径——`apps/web` 全量路径抽取无 `/api/grok/*`，后端 `apps/control/app/routers/grok.py:26-74` 有 6 个端点，故结论是**无界面入口**：`frontend-integration-plan.md` §1.3 的「已有（任务页 TaskRunnerPanel）/已有」与 §3 落位表的「① / 任务页 / 已有」均改为「无 UI / 不进界面」并保留原句作历史，与 §1.5「不进界面」一致；`grok` 是否另做页面仍属后续产品决策，本文只固化当前事实。
- **改动面**：文档 2 处（本次不改）。（2026-11-04 补记：实际改了 3 处——`frontend-integration-plan.md` 顶部状态注记、§1.3 grok 表格、§3 落位表注册任务行。）

### P2-9 `/quota`、`/pricing` 新增页面与基准红线 L48 冲突，但 L65 又列入清单

- **问题**：`FRONTEND-BASELINE.md` L48「不得新增页面」，而 L60-68 的页面清单已包含 `/quota`、`/pricing`；当前导航也确实新增了这两项。文档自身矛盾，缺少「批准记录」。
- **证据**：
  - `docs/FRONTEND-BASELINE.md:48`
  - `docs/FRONTEND-BASELINE.md:60-68`
  - `apps/web/components/common/layout/ManagementBar.tsx:450-458`
- **影响**：后续评审无法判断这两个页面是「允许的新增」还是「违规新增」。
- **建议修法**：在 baseline 增加一条白名单/批准记录，明确 `/quota`、`/pricing` 为批准新增；不改 UI。
- **状态（2026-11-03）**：未修。页面清单已更新（移除 `/tasks`），但白名单与批准记录仍未补。
- **改动面**：文档 1 处。

### P2-10 后端中文错误文案未进入 i18n 短语表，非中文用户会看到中文

- **问题**：服务端错误文案是中文硬编码，前端 `errText()` 只用 `tp()` 查 `phrases` 表；表里没有的短语原样返回中文。现有短语表无法覆盖新增文案（如配额、令牌拒绝）。另有少量 UI 硬编码英文列名。
- **证据**：
  - `apps/web/lib/api.ts:59-69`（`errText` → `tp(raw)`）
  - `apps/web/lib/i18n/index.ts:125-132`（`table?.[source] ?? source`）
  - 硬编码示例：`apps/control/app/billing.py:463-466`、`apps/control/app/routers/v1.py:55,64`、`apps/control/app/routers/tokens.py:53,55`、`apps/control/app/net_guard.py:94-125`
  - UI 硬编码示例：`apps/web/app/(main)/logs/page.tsx:300-301,307`（`Prompt Token` / `Completion Token` / `User-Agent`）
- **影响**：en/ja/ko 用户在配额超限、令牌创建失败、内网地址被拒等场景会突然看到中文；体验割裂。
- **建议修法**：后端改为稳定错误码（如 `quota_exceeded`、`token_scope_unsupported`），前端按码查 i18n；短期可先把高频文案补进 `phrases`。
- **状态（2026-11-03）**：部分修。网关的配额拒绝、模型解析、失败限流已经带稳定 `type/code`（前端可判码），但管理面（令牌、设置、net_guard）仍是中文硬编码，`phrases` 也未随新增文案扩容；非中文用户仍会在这些路径看到中文。
- **改动面**：后端错误返回结构（中）+ 5 个 locale（小）；短期补短语表则小。

---

## 附录 A：后端管理端点覆盖矩阵（87 条，不含 `/v1/*`）

图例：**有入口** = 页面/按钮/表单可触达；**缺页面** = 需要界面但当前没有；**无须界面** = 有意不做或由其他流程覆盖。

### A1. 账号（12）

| 端点 | 鉴权 | 前端入口 | 结论 |
| --- | --- | --- | --- |
| `GET /api/accounts` | user | accounts 页、dashboard、quickAdd | 有入口 |
| `POST /api/accounts/sync` | admin | 无 | 无须界面（GET 列表已自动同步，`accounts.py:81`） |
| `POST /api/accounts/refresh-credits` | admin | 页头「刷新积分」(isAdmin, `accounts/page.tsx:815`)；自动刷新已加 isAdmin 守卫（`:113`），dashboard 也已守卫（`dashboard/page.tsx:79`，2026-11-04 复核） | 有入口（P2-3 已修） |
| `POST /api/accounts/checkin-all` | admin | 页头「全部签到」(isAdmin, `:838`) | 有入口 |
| `PUT /api/accounts/{file}/note` | admin | 行操作 `AccountNoteDialog.tsx:57` | 有入口 |
| `POST /api/accounts/{file}/disabled` | admin | 行操作 `accounts/page.tsx:747` | 有入口 |
| `DELETE /api/accounts/{file}` | admin | 行操作 `:760` | 有入口 |
| `POST /api/accounts/{file}/checkin` | admin | 行操作 `:678` | 有入口 |
| `POST /api/accounts/{file}/test` | admin | 行操作 `:702` | 有入口 |
| `POST /api/accounts/{file}/refresh` | admin | 行操作 `:706` | 有入口 |
| `POST /api/accounts/{file}/clear-cooling` | admin | 行操作 `:719` | 有入口 |
| `POST /api/accounts/restart` | admin | 页头「强制重启」，已改指本路径 | 有入口（P1-3 已修） |

### A2. 鉴权 / 会话（6）

| 端点 | 鉴权 | 前端入口 | 结论 |
| --- | --- | --- | --- |
| `POST /api/login` | 无 | `/login` | 有入口 |
| `POST /api/logout` | 无 | 顶栏退出 | 有入口 |
| `GET /api/me` | 无 | 全局 `useAuth` | 有入口 |
| `POST /api/auth/start` | admin | `AddAccountDialog.tsx:105` | 有入口 |
| `GET /api/auth/poll` | admin | `AddAccountDialog.tsx:120` | 有入口 |
| `POST /api/sessions/revoke` | user | 顶栏「退出所有设备」`ManagementBar.tsx:600` | 有入口 |

### A3. Grok 注册（6）

| 端点 | 鉴权 | 前端入口 | 结论 |
| --- | --- | --- | --- |
| `POST /api/grok/register/tasks` | admin | 无 | 缺页面（或按 docs 不做，见 P2-8） |
| `GET /api/grok/register/tasks` | user | 无 | 缺页面 |
| `GET /api/grok/register/tasks/{id}` | user | 无 | 缺页面 |
| `POST /api/grok/register/tasks/{id}/cancel` | admin | 无 | 缺页面 |
| `POST /api/grok/register/tasks/{id}/retry` | admin | 无 | 缺页面 |
| `GET /api/grok/register/worker` | user | 无 | 缺页面（运维观测，可长期不做界面） |

### A4. 密钥 / 令牌 / 日志（10）

| 端点 | 鉴权 | 前端入口 | 结论 |
| --- | --- | --- | --- |
| `GET /api/keys` | user | keys 页 | 有入口 |
| `POST /api/keys` | admin | keys 页（isAdmin，`:127`） | 有入口 |
| `PATCH /api/keys/{id}` | admin | keys 页 | 有入口 |
| `DELETE /api/keys/{id}` | admin | keys 页 | 有入口 |
| `GET /api/tokens` | user | 设置·令牌（仅 isAdmin 渲染） | 有入口 |
| `POST /api/tokens` | admin | 同一面板 | 有入口（契约冲突，P1-8） |
| `PATCH /api/tokens/{id}` | admin | 同一面板 | 有入口 |
| `DELETE /api/tokens/{id}` | admin | 同一面板 | 有入口 |
| `GET /api/logs` | user | logs 页 | 有入口 |
| `POST /api/logs/clear` | admin | logs 页（isAdmin，`:113`） | 有入口 |

> 处理状态（2026-11-03）：`GET /api/keys` 已按 `owner_user_id` 收敛可见范围（admin 全部、非 admin 仅自有），`POST /api/keys` 支持 `owner_user_id`；`POST /api/tokens` 的 UI 已只提交 `name`（P1-8 已修）。

### A5. 模型 / 定价（6）

| 端点 | 鉴权 | 前端入口 | 结论 |
| --- | --- | --- | --- |
| `GET /api/status` | user | dashboard / accounts 心跳 | 有入口 |
| `GET /api/models` | user | models 页、playground 模型下拉、quota/pricing 选择器 | 有入口（P0-1 已修） |
| `GET /api/pricing/books` | user | pricing 页 | 有入口 |
| `POST /api/pricing/books` | admin | pricing 页（isAdmin，`:182`） | 有入口 |
| `GET /api/pricing/resolve` | user | pricing 干跑 | 有入口 |
| `POST /api/pricing/import` | admin | pricing 页 | 有入口 |

### A6. 服务商 / 凭据（6）

| 端点 | 鉴权 | 前端入口 | 结论 |
| --- | --- | --- | --- |
| `GET /api/providers` | user | 设置·服务商 | 有入口 |
| `PATCH /api/providers/{id}` | admin | 同页（viewer 禁用控件，`:300+`） | 有入口 |
| `GET /api/provider-credentials` | user | 设置·服务商·凭据 | 有入口 |
| `POST /api/provider-credentials` | admin | 同区块 | 有入口 |
| `PATCH /api/provider-credentials/{id}` | admin | 同区块 | 有入口 |
| `DELETE /api/provider-credentials/{id}` | admin | 同区块 | 有入口 |

### A7. 配额 / 订阅池（15）

| 端点 | 鉴权 | 前端入口 | 结论 |
| --- | --- | --- | --- |
| `GET /api/quota/policies` | user | quota/policies 页 | 有入口 |
| `POST /api/quota/policies` | admin | 同页（isAdmin，`:356`） | 有入口 |
| `DELETE /api/quota/policies/{id}` | admin | 同页 | 有入口 |
| `GET /api/quota/status` | user | 同页 | 有入口 |
| `GET /api/quota/alerts` | user | 同页 | 有入口 |
| `GET /api/sub2api/groups` | user | quota 页 | 有入口 |
| `POST /api/sub2api/groups` | admin | quota 页（isAdmin，`:351`） | 有入口 |
| `PATCH /api/sub2api/groups/{id}` | admin | 同页 | 有入口 |
| `DELETE /api/sub2api/groups/{id}` | admin | 同页 | 有入口 |
| `GET /api/sub2api/accounts` | user | quota 页 | 有入口 |
| `POST /api/sub2api/accounts` | admin | 同页 | 有入口 |
| `PATCH /api/sub2api/accounts/{id}` | admin | 同页 | 有入口 |
| `DELETE /api/sub2api/accounts/{id}` | admin | 同页 | 有入口 |
| `POST /api/sub2api/accounts/{id}/reset` | admin | 同页 | 有入口 |
| `GET /api/sub2api/ledger` | user | quota 页 | 有入口 |

> 处理状态（2026-11-03）：`/api/quota/*` 三个 GET 已改为「admin 全部 / 非 admin 仅自有密钥（按 `api_keys.owner_user_id`）」，不再是纯 admin；`/api/sub2api/{groups,accounts,ledger}` 仍为 `admin`，viewer 在查库前 403。上表鉴权列保留原审计时的判断，以本注为准；详见 API-REFERENCE §1/§9。

### A8. 设置 / 用户 / 安全 / 统计 / 系统（26）

| 端点 | 鉴权 | 前端入口 | 结论 |
| --- | --- | --- | --- |
| `GET /api/settings/upstream` | admin | 设置·上游（viewer 已不再请求） | 有入口（P1-7 已修） |
| `POST /api/settings/upstream` | admin | 设置·上游保存 | 有入口 |
| `POST /api/settings/secret/reveal` | admin | 无 | 无须界面（明文 secret 不下发浏览器，安全取向） |
| `GET /api/settings/gateway` | admin | 设置·服务商（viewer 已早退） | 有入口（P1-7 已修） |
| `POST /api/settings/gateway` | admin | 同页开关 | 有入口 |
| `GET /api/settings/model-map` | admin | 设置·模型映射（viewer 已不再请求） | 有入口（P1-7 已修） |
| `POST /api/settings/model-map` | admin | 同页保存 | 有入口 |
| `GET /api/settings/runtime-config` | admin | 设置·运行参数 | 有入口（本轮新增） |
| `POST /api/settings/runtime-config` | admin | 同区块保存 | 有入口（本轮新增） |
| `GET /api/settings/runtime-config/export` | admin | 同区块「导出 JSON」 | 有入口（本轮新增） |
| `POST /api/settings/runtime-config/import` | admin | 同区块「导入 JSON」 | 有入口（本轮新增） |
| `POST /api/settings/upstash/test` | admin | 无（按钮已移除） | 无须界面（P2-5 已修；2026-11-04 起为真实只读探测） |
| `GET /api/users` | user | 设置·用户 | 有入口 |
| `POST /api/users` | admin | 同页 | 有入口 |
| `PATCH /api/users/{username}` | admin | 同页 | 有入口 |
| `DELETE /api/users/{username}` | admin | 同页 | 有入口 |
| `GET /api/security/guard` | user | security 页 | 有入口 |
| `GET /api/security/login-failures` | user | security 页 | 有入口（非 admin 仅 `scope=self`） |
| `GET /api/audit-logs` | user | security 页 | 有入口（非 admin 仅 `actor=自己`，查他人 403） |
| `GET /api/security/audit` | user | 无 | 无须界面（等价别名，P2-4） |
| `GET /api/stats/summary` | user | stats 页 | 有入口 |
| `GET /api/stats/daily` | user | stats 页 | 有入口 |
| `GET /api/system/changelog` | user | 设置·更新日志 | 有入口 |
| `GET /api/system/versions` | user | 设置·更新 | 有入口 |
| `GET /api/system/check-update` | user | 设置·更新 | 有入口 |
| `GET /api/system/update-status` | user | 设置·更新 | 有入口 |

> **（2026-11-05 已更正，原表格 `check-update` 行保留作历史）** `GET /api/system/check-update` 已于 2026-11-05 下线（`apps/control/app/routers/system.py` 不再注册该路由），前端对它的调用为零；本表里存活的是 `changelog` / `versions` / `update-status` 三行。
>
> **这条为什么重要**：把已删除端点记为「有入口」会让端点盘点虚增一条、并在 UI 复核时去找一个不存在的「检查更新」入口；实际设置·更新页读的是 `update-status`。

> 注：`/v1/chat/completions`、`/v1/messages`、`/v1/responses`、`GET /v1/models` 属网关协议面，不计入管理端点审计。

## 附录 B：权限不一致清单

| # | 端点/按钮 | 后端 | 前端 | 结论 |
| --- | --- | --- | --- | --- |
| B1 | `POST /api/accounts/refresh-credits` | admin | 按钮按 isAdmin 隐藏；accounts 与 dashboard 自动请求均已加守卫（2026-11-04 复核） | 前端多余请求（P2-3 已修） |
| B2 | `GET /api/settings/upstream` / `model-map` | admin | 设置页对所有角色无条件请求（已加守卫） | 静默 403，界面误显示「未加载」（P1-7 已修） |
| B3 | `GET /api/settings/gateway` | admin | 服务商区块对所有角色无条件请求（已早退） | 同上（P1-7 已修） |
| B4 | `POST /api/tokens` 的 `scope/expires_at` | 只允许 `readonly` + 无到期 | UI 已只提交 `name` | 参数契约冲突已由前端收敛（P1-8 已修） |
| B5 | 批量/单个 OAuth 按钮 | 后端无 `/api/oauth/*` | 已改调 `/api/auth/*`，按钮仅 isAdmin 可见 | 端点缺失已修（P0-3） |

其余写端点（账号行操作、keys、pricing、providers、credentials、quota、sub2api、users、logs clear）均已做到「前端按 isAdmin 隐藏/禁用 + 后端 admin_user」，未发现「后端要 admin、前端对 viewer 仍可点」的情况。

## 附录 C：前端调用但后端不存在的「幽灵端点」

复核（2026-11-03）：原 **27 条路径 / 15 组**，改指真实端点 8 条、删除 16 条，剩 **3 条**死代码；再复核（2026-11-04）：这 3 条也已从 `api.ts` 删除，复跑 `scripts/contract_check.py` 实测幽灵端点 0、方法不匹配 0，下表仅作历史记录。

| 前端路径 | 现状 | 处理 |
| --- | --- | --- |
| `/api/model-catalog` | 已改为 `/api/models` | P0-1 已修 |
| `/api/oauth/start`、`/api/oauth/{id}/poll`、`DELETE /api/oauth/{id}` | 已改为 `/api/auth/start`、`/api/auth/poll` | P0-3 已修 |
| `/api/oauth/batch`、`/api/oauth/batch/{id}`、`GET /api/oauth/batch` | 方法已删 | P0-3 已修 |
| `/api/restart` | 已改为 `/api/accounts/restart` | P1-3 已修 |
| `/api/checkin-logs`、`/api/checkin-logs/clear` | 已删 | P0-2 已降级删除 |
| `/api/task-logs`、`/api/task-logs/collect`、`/api/task-logs/clear` | 已删 | 同上 |
| `/api/upstream/logs` | 已删 | 同上 |
| `/api/task-run`、`/api/task-run/stop` | 已删 | 同上 |
| `/api/task-claim-schedule` | 已删 | 同上 |
| `/api/stats/by-model`、`/api/stats/by-key`、`/api/stats/upstream` | 已删 | P1-1 已降级删除 |
| `/api/stats/rebuild-usage`、`/api/stats/repair-usage` | 已删 | 同上 |
| `/api/system/update`、`/api/system/upstream-ref` | 已删 | P1-2 已降级删除 |
| `/api/accounts/{file}/credits` | 方法已从 `api.ts` 删除 | 已修（2026-11-04） |
| `/api/upstream/reload-state` | 方法已从 `api.ts` 删除 | 已修（2026-11-04） |
| `/api/settings/upstash/reload` | 方法已从 `api.ts` 删除 | 已修（2026-11-04） |

## 附录 D：i18n 完整性

- 文件：`apps/web/lib/i18n/locales/{zh-CN,en,zh-TW,ja,ko}.json`
- 结构与回退：`apps/web/lib/i18n/index.ts` 点号查找；缺失回落 `zh-CN`，再回落键名；`tp()` 走 `phrases.*` 反向表（`index.ts:125-132`）。

| 对比 | 结果 | 判定 |
| --- | --- | --- |
| `en` vs `zh-CN` | en 多 219 个键：`phrases` 203 + `format/credit/metric/dashboard` 复数变体 16 | **有意**：`phrases` 是中文源文反向表，`zh-CN` 不需要；复数对象是英文语法需要 |
| `zh-CN` vs `en` | zh 多 8 个单数键（`format.minutes` 等） | 同上，结构差异 |
| `ja` / `ko` / `zh-TW` vs `en ∪ zh-CN` | 各 1373 个叶子键，缺 16（全为 `en` 复数变体），无多余键 | **真实漏翻 0**；原记录的「各缺 261」已作废（见 P1-4 修正记录） |

净缺口按段（原始审计快照，已被上表实测取代，保留作历史）：

| 段 | 原缺失数 | 现在 |
| --- | --- | --- |
| `providers` | 80 | 0 |
| `quota` | 79 | 0 |
| `pricing` | 38 | 0 |
| `security` | 21 | 0 |
| `playground` | 13 | 0 |
| `format` / `credit` / `dashboard` 的复数变体 | 约 20（有意） | 16（有意，结构差异） |
| `nav.quota`、`nav.pricing` | 2 | 0 |
| `risk` 3 / `logs` 2 / `updatePanel` 2 / `keys` 1 | 8 | 0 |

- 全语言共同缺失：`common.delete` —— 已修（P1-5）。
- 硬编码未走 i18n 的可见文案：`logs/page.tsx:300-301,307`（`Prompt Token` / `Completion Token` / `User-Agent`）；后端中文错误见 P2-10。

## 附录 E：页面可达性与导航差异

路由（`apps/web/app` 下全部 `page.tsx`，14 个文件、14 条路由；`/tasks` 已删除）：

| 路由 | 可点入口 | 结论 |
| --- | --- | --- |
| `/` | 根入口 | 可达 |
| `/login` | `(auth)` 路由，未登录重定向 | 可达 |
| `/dashboard` | dock `nav.dashboard` | 可达 |
| `/accounts` | dock `nav.accounts`、quickAdd | 可达 |
| `/quota` | dock `nav.quota` | 可达 |
| `/quota/policies` | quota 页内 Link（`quota/page.tsx:345-359`） | 可达（二级） |
| `/pricing` | dock `nav.pricing` | 可达 |
| `/keys` | dock `nav.keys` | 可达 |
| `/models` | dock `nav.models` | 可达（数据已接 `/api/models`） |
| `/playground` | dock `nav.playground` | 可达 |
| `/stats` | dock `nav.stats` | 可达 |
| `/logs` | dock `nav.logs` | 可达 |
| `/security` | dock `nav.security` | 可达 |
| `/settings` | dock `nav.settings` + 顶栏 URL | 可达 |

**未发现孤立页面**：每条路由都有 dock 或页内 Link 入口。

与基准 `codebuddy2api`（main `92b8b91`）导航差异：

| 差异 | 基准 | 当前 | 判定 |
| --- | --- | --- | --- |
| 新增 `/quota`、`/pricing` | 无 | `ManagementBar.tsx:450-458` | 需补批准记录（P2-9） |
| 删除 `/red-packets` | 有 | 无 | **允许**：`FRONTEND-BASELINE.md:68` 明确按业务删除 |
| `/providers` 并入设置页 | 独立页 | 设置·服务商 tab | **允许**：`FRONTEND-BASELINE.md:65,68` |
| `quickAdd` | 有 | 有（`ManagementBar.tsx:505-510`，isAdmin） | 保留 |
| dock 形状/分组/入口数量 | — | 沿用原 dock | 未发现违规改动 |

## 附录 F：文档与代码不一致（按代码记录）

| 文档位置 | 文档说法 | 代码事实 | 结论 |
| --- | --- | --- | --- |
| `docs/design/frontend-integration-plan.md:10,12,198-200,299-301` | 只挂载 11 个模块；`tokens.py`、`system.py` 未挂载 | `apps/control/app/main.py:179-181` 已包含并挂载 `tokens`、`system` | **文档过时** |
| `docs/design/frontend-integration-plan.md:63-65` | grok 注册任务入队「已有（任务页 TaskRunnerPanel）」 | 前端无 `/api/grok/*`；TaskRunnerPanel 调 `/api/task-run`，后端无此路由 | **文档错误**（已在文件顶部加删除注记；2026-11-04 已改写为「无 UI」并保留原句作历史） |
| `docs/design/frontend-integration-plan.md:90` | 注册任务不进界面 | 与 L63 自相矛盾 | **文档自相矛盾**（2026-11-04 已消解：以本行「不进界面」为准，§1.3 的「已有」表述已改写留档，两处口径统一为「无界面入口」） |
| `docs/FRONTEND-BASELINE.md:48` | 不得新增页面 | `:60-68` 又把 `/quota`、`/pricing` 列入清单 | **文档自相矛盾**，需批准记录 |
| `README.md`、`GETTING-STARTED.md`、`FRONTEND-BASELINE.md`、`docs/design/*` | 页面清单与导航仍含 `/tasks` | `/tasks` 页面与相关幽灵端点已整体删除 | **已修**（2026-11-03，页面清单移除、设计文档加删除注记） |
| `README.md`、`docs/ADDING-A-PROVIDER.md:161`、`docs/FRONTEND-SKELETON.md:100`、`docs/security-audit.md:122`、`docs/ux-review.md:54` | 测试基线写作固定值（README 旧为约 425，其余称「当前基线 250」） | 本次提交实测约 425 passed，且会继续增长；2026-11-04 全量实测 443 passed + 1 skipped + 0 failed + 0 xfailed，收集 444 条 | **已修**（2026-11-03 改写为「以最近一次全量 `pytest` 结果为准」；当前基线以 2026-11-04 实测值为准，量级约 444） |
| `docs/API-REFERENCE.md` 配额读权限段 | `api_keys` 无归属字段，读接口只放行 admin | 已有 `owner_user_id`，非 admin 可读自有密钥 | **已修**（2026-11-03） |
| `docs/API-REFERENCE.md` §8 | 未列 `/api/settings/runtime-config`、`/export`、`/import` 四个端点 | 代码已实现，设置页「运行参数」在用 | **已修**（2026-11-03，§8 补入四个端点的字段、admin-only 与「先全量校验再单事务写入、异常 rollback」语义） |
| `docs/API-REFERENCE.md:478,486,494` | `GET /api/settings/upstream`、`/gateway`、`/model-map` 标「登录」 | 三个 GET 均为 `admin_user` | **已修**（2026-11-03，三处改为「管理员」，并复核全篇其余权限标注与路由依赖一致） |
| `docs/CONTRACT.md`、`docs/DEMO.md`、`docs/security-retest-2026-11-03.md` | 命令与路径写死本机盘符绝对路径（`[盘符]:\qwq\...`） | 换机器或换目录即失效，与 README 的相对路径约定不一致 | **已修**（2026-11-03，改为仓库相对路径；复扫 `docs/**/*.md` 残留 0 行，见 consistency-report §8 修复记录） |

---

## 最该先修的 5 条

1. **P0-1** 模型页端点错位 —— 已修（改调 `/api/models`）。
2. **P0-2** 任务页/任务执行器幽灵端点 —— 已降级删除（页面与入口整体移除）。
3. **P0-4** Playground 429 崩溃与 micro 泄漏 —— 已修（前端 429 映射 + 对象 detail 兜底）。
4. **P1-3** 重启上游路径 1 行修正 —— 已修（`/api/accounts/restart`）。
5. **P1-4/P1-5** i18n —— 已修（`common.delete` 五语言齐全，三语言真实漏翻 0）。

## 看似问题、实为有意取舍

| 现象 | 为什么不是问题 |
| --- | --- |
| `/api/accounts/sync` 无按钮 | `GET /api/accounts` 每次已自动同步（`accounts.py:81`），手动强同步留给排障/CLI |
| `/api/settings/secret/reveal` 无入口 | 明文 secret 刻意不下发浏览器，安全取向 |
| `/api/security/audit` 无前端 | 是 `/api/audit-logs` 的兼容别名，前端只用后者 |
| `zh-CN` 少 203 个 `phrases.*` 键 | `phrases` 是「中文源文 → 目标语言」反向表，源语言不需要 |
| `en` 的 `.one/.other` 对象看起来「多键」 | 英文复数语法需要；日/韩/繁用单数串，属结构差异 |
| `/red-packets`、`/claim` 消失 | `FRONTEND-BASELINE.md:68` 明确按业务整条删除，不应再补 |
| `/providers` 无独立页 | 已并入设置页「服务商」tab，baseline 明确允许 |
| 网关原样透传上游 4xx/5xx body | 下游协议兼容所需；与管理面错误契约分开看 |
| grok worker 端点无界面 | 属运维观测，长期不做界面也合理（但需在文档写死）；2026-11-04 已在 `frontend-integration-plan.md` §1.3 与落位表写死为「无界面入口」 |

---

## 附录 G：2026-11-04 变更事实索引

> 原拟作为「附录 D」，本文件附录 D 已被「i18n 完整性」占用，顺延为 G。只登记证据与落点，不重复正文叙述。

| 事实 | 一句话说明 | 代码/配置证据（文件:行） | 对应文档落点 |
| --- | --- | --- | --- |
| 1. 模型解析判定顺序 | 显式 provider 判定依次为本地存在性 404 → 头/前缀冲突 400 → 停用 503，同源时按 `provider/` 前缀剥离 | `apps/control/app/model_resolve.py:286-296`（顺序与「冲突必须先于停用」，另见 `:100`）、`:171-180`（404 `provider_not_found`，常量 `:39`）、`:183`（400 `conflicting_provider_hints`，常量 `:35`）、`:85-96`（503 `provider_disabled`，常量 `:38`）、`:294-296` 与 `:112-117`（同源剥离） | `docs/error-contract.md:67-71` |
| 2. `billing_ledger.log_id` 行级关联与五档 | 扣费用 `log_id` 与日志行精确关联，日志页「扣费」列给五档 `credit_status`，`unpriced` 徽标点开即现查价格 | `apps/control/app/db.py:174`（`log_id` 列）、`apps/control/app/routers/logs.py:8-12`（五档口径）、`:168-187` 与 `:216-218`（join 与判定）、`apps/web/app/(main)/logs/page.tsx:344,403-422`（列与徽标）、`:159-174,441-453`（`unpriced` 调 `pricingApi.resolve` 现查） | `docs/API-REFERENCE.md:1073-1075` |
| 3. 热路径覆盖索引与老库升级 | 两棵覆盖索引 + 一棵带 `WHERE` 谓词的部分索引；老库升级只补 `log_id` 列不回填，存量日志显示 `unlinked` | `apps/control/app/perf_indexes.py:35,39,52`（三索引）、`:54`（`WHERE error IS NOT NULL`）、`apps/control/app/db.py:286-288`（`_ensure_column` 加列，无回填语句）、`apps/control/app/routers/logs.py:180-187`（`bl.log_id IS NULL → unlinked`） | `docs/PERFORMANCE.md:355,442`；`docs/OPS-RUNBOOK.md:68`；`docs/BACKUP.md:137` |
| 4. 单请求建连 8 → 4 → 2 | 分三轮收口，最终每请求 2 次连接（准入 1 次 + `_record` 1 次），第三次收口把 `registry.get` 的两条元数据读并入准入段连接 | `docs/PERFORMANCE.md:392-393`（2026-11-04 最终值与收口方式；原文写「第三次收口」，非「二次」） | `docs/PERFORMANCE.md:392-393,431-432` |
| 5. `ProviderSpec.limitations` | 能力受限以 `limitations` 声明，`deepseek_sidecar` 的 messages 只支持非流式，501 报错与声明共用同一常量 | `apps/control/app/adapters/base.py:108`（字段）、`apps/control/app/adapters/deepseek_sidecar.py:18`（常量）、`:48`（声明）、`:56-57`（501 引用同一常量） | `docs/API-REFERENCE.md:318,493` |
| 6. 许可证四档与风险分级 | MIT 7 家入主仓，GPL-3.0 / LGPL-3.0 仅进程隔离，PolyForm-NC 与无 LICENSE 商用禁用；`risk_level` 三档配 `risk_note`，`qoder` 直连 `safe`、`qoder_sidecar` `danger` | `upstreams/README.md:15-18`（四档表与 11 家清单）、`apps/control/app/adapters/opencode.py:73`、`sub2api.py:67`、`qoder.py:391`（`risk_note`）、`qoder.py:390`（`safe`）、`qoder_sidecar.py:40`（`danger`） | `upstreams/README.md:15-18`；`docs/ADDING-A-PROVIDER.md:88` |
| 7. `BUILDERS` 14 ↔ 上游 11 家 | 14 个适配器注册项对应 11 家上游，trae / qoder / codar 各有直连与 sidecar 双路径、各占 2 | `apps/control/app/adapters/__init__.py:20-35`（14 条 `BUILDERS`，双路径为 `trae/trae_sidecar`、`qoder/qoder_sidecar`、`codar/codar_sidecar`）、`upstreams/README.md:15-18`（11 家） | `docs/design/ia-proposal.md:46`（14 个适配器）；`docs/dependency-audit.md:70`（11 家上游）；14↔11 对照仅本表记录 |
| 8. 构建上下文与宿主端口 | `.dockerignore` 把构建上下文从 905.7MB 压到 6.3MB；Windows + Docker Desktop 只为 `127.0.0.1` 建宿主端口 | `docker-compose.yml:61`（控制面 `127.0.0.1:8000:80`）、`:87`（控制面 `127.0.0.1:8300:8300`） | `CHANGELOG.md:53`、`deploy/README.md:98,102`（905.7MB → 6.3MB）；`deploy/README.md:77`（仅 `127.0.0.1`） |
| 9. 日志扣费列渲染细节 | 金额列 `hidden md:table-cell` 只在 ≥768px 展示，窄屏把口径压成一行 10px 文本（代码断点是 `md`，不是 390），金额为 null 显 `—` | `apps/web/app/(main)/logs/page.tsx:344,403`（`hidden md:table-cell`）、`:386-387`（`md:hidden text-[10px]` 单行）、`:405-406`（金额 `fmtMicroUsd`）、`apps/web/lib/format.ts:270-271`（非数值返回 `—`） | 仅本表记录（`390` 仅是测试视口，见 `docs/design/README.md:85`） |

