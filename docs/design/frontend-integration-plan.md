# 前端功能落位与实现计划

- 基准壳：`codebuddy2api` 的 `web/`（迁移后为本仓 `apps/web/`），见 [FRONTEND-BASELINE.md](file:///d:/qwq/项目/unified-2api/docs/FRONTEND-BASELINE.md)。
- 目标：把 12 个上游仓库的后端能力落进基准壳界面。
- 范围：只规划，不改 `apps/`，不写代码。
- 数据来源：`apps/control/app/routers/*.py`（控制面已落地路由）、`apps/web/lib/api.ts`（前端已声明的接口）、`docs/providers/*.md`、各上游 README 与前端目录结构。

## 0. 先对齐一个事实

控制面实际挂载的路由只有 11 个模块（`main.py:160`）：accounts / auth / grok / keys / models / pricing / providers / settings / stats / sub2api / v1。

`routers/tokens.py`、`routers/system.py` **文件存在但未被 `main.py` 挂载**——这正是已知阻塞项 `/api/tokens`、`/api/system/changelog`、`/api/system/update-status` 的根因（不是没写，是没 include）。

此外 `apps/web/lib/api.ts` 声明了一批控制面**根本没有的端点**（OAuth 批量、checkin-logs、task-logs、upstream/logs、task-run、task-claim-schedule、model-catalog、playground/*、logs/*、stats/by-model|by-key|upstream、security/*、upstream/reload-state、audit-logs）。这些页面现在要么 404、要么是迁移残留的死代码。本计划的"落位"必须先区分「后端已有」和「后端没有」。

---

## 1. 能力清单（按仓库）

判定口径：「已有 UI」= 控制面有路由且 `apps/web` 有页面/组件调用；「无 UI」= 控制面有路由但前端没接，或前端接了但路由不存在。

### 1.1 codebuddy2api（基准，MIT，sidecar）

后端能力（控制面已落地）：

| 能力 | 路由 | UI 状态 |
| --- | --- | --- |
| 账号列表/同步 | `GET/POST /api/accounts`、`/sync` | 已有（账号页） |
| 积分刷新 | `POST /api/accounts/refresh-credits` | 已有（账号页） |
| 全部签到 | `POST /api/accounts/checkin-all` | 已有（账号页） |
| 单账号签到/测试/刷新/清冷却 | `POST /api/accounts/{file}/{checkin,test,refresh,clear-cooling}` | 已有（账号页） |
| 备注/禁用/删除 | `PUT /note`、`POST /disabled`、`DELETE` | 已有 |
| 重启运行时 | `POST /api/accounts/restart` | 已有（账号页） |
| 上游运行配置 10 组 | `GET/POST /api/settings/upstream` | 已有（设置·运行配置 tab） |
| 网关匿名开关 | `GET/POST /api/settings/gateway` | **无 UI**（路由有，设置页没接） |
| 模型映射 | `GET/POST /api/settings/model-map` | 已有（设置·模型映射 tab） |
| 用户管理 | `GET/POST/PATCH/DELETE /api/users` | 已有（设置·用户 tab） |
| 价格表 | `GET/POST /api/pricing/books`、`/resolve`、`/import` | **无 UI**（路由有，无页面） |
| 上游状态/模型聚合 | `GET /api/status`、`GET /api/models` | 已有（仪表盘/模型页） |
| 用量汇总/按日 | `GET /api/stats/summary`、`/daily` | 已有（仪表盘/统计页） |

codebuddy 自身还有但控制面未接入的能力（来自上游管理端）：OAuth 批量登录、签到日志、任务日志、上游实时日志、定时任务调度（task-run）、积分领取调度（task-claim-schedule）、请求日志、审计日志、上游重载状态。这些在 `api.ts` 里有声明但控制面无路由。

### 1.2 muse2api（MIT，sidecar）

后端能力：

| 能力 | 路由/来源 | UI 状态 |
| --- | --- | --- |
| chat / models 代理 | `/v1/chat/completions`、`/v1/models` | 走网关，无独立 UI |
| 账号导入（hatch_sess） | sidecar 内部 `admin.html` | **不进界面**（sidecar 自管） |
| 生图/生视频/responses | muse 上游 `/v1/images/*`、`/videos/*`、`/responses` | 控制面未接入路由，**无 UI**（见 muse2api-protocols.md，未实施） |
| usage 统计 | muse `usage` 恒 0 | 统计页会显示 0，属已知口径（muse usage 恒 0） |

### 1.3 grok-register（MIT，in-repo-rewrite，一等服务）

后端能力（`/api/grok/register/*`）：

| 能力 | 路由 | UI 状态 |
| --- | --- | --- |
| 注册任务入队 | `POST /tasks` | 已有（任务页 TaskRunnerPanel） |
| 任务列表/详情 | `GET /tasks`、`/tasks/{id}` | 已有 |
| 取消/重试 | `POST /tasks/{id}/cancel`、`/retry` | 已有 |
| worker 状态 | `GET /worker` | 已有 |
| 执行器配置 | `GROK_REGISTER_EXECUTOR_URL`（环境变量） | **无 UI**（执行器未交付，配置走环境变量） |

grok-register 上游自身的能力（控制面未重写）：邮箱渠道选择、代理配置、浏览器引擎、CPA/Grok2API 产物生成、失败截图、连通性检查、重新登录日志。这些按契约文档属「外部执行器职责」，不进控制面。

### 1.4 opencode2api（GPL-3.0，in-repo-rewrite，clean-room）

后端能力：

| 能力 | 路由/来源 | UI 状态 |
| --- | --- | --- |
| chat / messages / models 代理 | `/v1/*` | 走网关，无独立 UI |
| 上游 key 配置（zen_keys / go_keys） | provider `api_key` / `options.extra_keys` | 服务商页配置（已有 ProviderPanel） |
| 匿名通道 / 重试策略 | opencode 上游 WebUI（8081） | **不进界面**（clean-room 不复制上游管理端） |
| System One / Responses | 上游有，控制面未声明 | 无 UI |

### 1.5 gptGrok2api（MIT，sidecar）

后端能力：

| 能力 | 路由/来源 | UI 状态 |
| --- | --- | --- |
| chat / responses / messages / images / videos | `/v1/*` | 走网关（chat/models 已接；images/videos 控制面未接入） |
| 账号池调度/换号/冷却 | sidecar 内部 | **不进界面** |
| 代理出口池 | sidecar 管理控制台 | **不进界面** |
| 图片图库/文件存储 | sidecar `data/files/` | **不进界面**（无控制面路由） |
| 注册任务（Grok 账号） | sidecar 管理控制台 | **不进界面**（grok-register 走控制面一等服务，不重复） |
| 实时请求监控 | sidecar 管理控制台 | **不进界面** |

### 1.6 ZCode2api（MIT，sidecar，Node 单文件）

后端能力：

| 能力 | 路由/来源 | UI 状态 |
| --- | --- | --- |
| chat / messages 代理 | `/v1/*` | 走网关，无独立 UI |
| 账号配置 | provider api_key | 服务商页配置 |

ZCode2api 无独立管理前端（单文件 `zcgw-server.mjs`），无可借结构。

### 1.7 sub2api（LGPL-3.0，in-repo-rewrite）

后端能力（`/api/sub2api/*`，控制面已落地）：

| 能力 | 路由 | UI 状态 |
| --- | --- | --- |
| 分组 CRUD | `GET/POST/PATCH/DELETE /groups` | **无 UI** |
| 账号池 CRUD | `GET/POST/PATCH/DELETE /accounts` | **无 UI** |
| 账号额度重置 | `POST /accounts/{id}/reset` | **无 UI** |
| 流水查询 | `GET /ledger` | **无 UI** |
| 复合路由/多平台价格表/订阅/订单/支付 | 上游有，控制面未实现 | **无 UI**（不做，见 §7） |

sub2api 是控制面后端能力最完整但前端完全缺失的一块。

### 1.8 trae2api（MIT，sidecar + 自研直连）

后端能力：

| 能力 | 路由/来源 | UI 状态 |
| --- | --- | --- |
| Solo/Work chat 代理 | `/v1/chat/completions` | 走网关 |
| 模型列表 | `GET /v1/models` | 模型页 |
| 设备身份/JWT 配置 | provider api_key + options | 服务商页配置 |
| OAuth 换 token | `POST /api/auth/start`（走 codebuddy 适配器） | 账号页扫码登录 |

### 1.9 Deepseek2Go（All Rights Reserved，sidecar）

后端能力：

| 能力 | 路由/来源 | UI 状态 |
| --- | --- | --- |
| chat / messages 代理 | `/v1/*` | 走网关 |
| PoW 求解 | sidecar 内部 `module.wasm` | **不进界面**（无控制面路由，许可不明） |
| 账号登录/Token | sidecar 内部 | **不进界面** |

### 1.10 qoder-trae-workbuddy-switch（PolyForm NC，sidecar）

后端能力：

| 能力 | 路由/来源 | UI 状态 |
| --- | --- | --- |
| chat / messages / responses 代理 | `/v1/*` | 走网关 |
| 双地域/三产品线路由 | provider options | 服务商页配置 |
| 账号令牌续期 | sidecar 内部 | **不进界面** |

### 1.11 codar2api（MIT，sidecar）

后端能力：

| 能力 | 路由/来源 | UI 状态 |
| --- | --- | --- |
| chat / messages / models 代理 | `/v1/*` | 走网关 |
| 微信/短信/JWT 登录 | sidecar 管理面板 | **不进界面** |
| 签到/领免费模型/令牌续期 | sidecar 后台任务 | **不进界面** |
| 代理池/账号调度 | sidecar 内部 | **不进界面** |

### 1.12 Neosub2api（本仓 unified-2api）

即本控制面自身，能力 = §1.1–§1.11 的并集 + 网关 `/v1/*` + 密钥 `/api/keys` + 服务商 `/api/providers`。

---

## 2. 各仓库前端结构要点（只描述结构，可借的标★）

| 仓库 | 前端形态 | 结构要点 | 可借 |
| --- | --- | --- | --- |
| codebuddy2api | Next + shadcn（基准壳） | 底部 dock 5 项；设置 7 tab（运行配置/模型映射/用户/令牌/系统/更新日志/关于）；表格+详情抽屉+Dialog 确认 | 基准本身 |
| sub2api | Vue3 + Vite + Tailwind | 管理端侧边栏分组：Dashboard / Groups / Accounts / Channels / Proxies / Users / Subscriptions / Orders / Usage / PromoCodes / Redeem / Affiliates / Announcements / AuditLog / Backup / Plugins / RiskControl / Settings；表格+分页+批量操作+Drawer 详情 | ★ Groups/Accounts/Usage 三页的表格列结构与筛选交互可直接映射到基准表格组件 |
| gptGrok2api | 管理控制台（Go 内嵌） | 账号 / 代理 / 图片图库 / 日志 / 实时请求 / 提示词 / 注册任务 / 备份 / 系统设置 | ★ 「实时请求」流式列表结构可借到请求日志页；其余不借 |
| grok-register | 单页 `admin.html` | 配置表单 + 任务启动/停止 + 轮询日志面板 + 账号结果表 | ★ 日志面板的「启动→轮询→停止」状态机可借到任务页 |
| muse2api | 单页 `admin.html` | 账号导入表单 + 模型/会话状态卡片 | 不借（能力在 sidecar 内） |
| opencode2api | WebUI（8081） | 模型列表 + key 配置 + 匿名通道开关 | 不借（clean-room 不复制） |
| qoder-switch | Rust Axum + package.json | 管理面板（结构未公开详细） | 不借 |
| codar2api | Go 内嵌静态资源 | 账号管理 + 代理池 + 签到任务 | 不借（能力在 sidecar 内） |
| 其余（ZCode2api/trae2api/Deepseek2Go） | 无独立前端或仅 API | — | 不借 |

**借法结论**：只有 sub2api 的 Groups/Accounts/Usage 三页和 gptGrok2api 的实时请求列表值得把结构映射进基准壳；其余上游前端要么能力留在 sidecar 内、要么许可证不允许复制。

---

## 3. 落位表

基准壳现有页面：仪表盘、账号、设置（7 tab）、密钥、个人信息。本仓 `apps/web` 已扩展出服务商、统计、日志、模型、测试台、任务、安全七页（非基准，属已落地扩展，本计划沿用为「已有页面」）。

处置：①=放进已有页面/tab；②=新增页面；③=不进界面。

| 能力 | 后端路由 | 处置 | 落位 | 理由 |
| --- | --- | --- | --- | --- |
| 账号池管理 | `/api/accounts` | ① | 账号页 | 已有 |
| 积分刷新/签到 | `/api/accounts/refresh-credits`、`/checkin-all` | ① | 账号页 | 已有 |
| 上游运行配置 | `/api/settings/upstream` | ① | 设置·运行配置 tab | 已有 |
| 网关匿名开关 | `/api/settings/gateway` | ① | 设置·运行配置 tab（底部独立区块） | 路由已有，UI 缺失；放运行配置 tab 底部，用 Switch + 二次确认 |
| 模型映射 | `/api/settings/model-map` | ① | 设置·模型映射 tab | 已有 |
| 用户管理 | `/api/users` | ① | 设置·用户 tab | 已有 |
| 访问令牌 | `/api/tokens`（未挂载） | ① | 设置·令牌 tab（先挂载路由） | TokensPanel 已存在，只差后端挂载 |
| 更新日志 | `/api/system/changelog`（未挂载） | ① | 设置·更新日志 tab（先挂载路由） | ChangelogPanel 已存在 |
| 系统更新状态 | `/api/system/update-status`（未挂载） | ① | 设置·系统 tab（先挂载路由） | UpdatePanel 已存在 |
| 系统版本 | `/api/system/versions` | ① | 设置·关于 tab | 已有 |
| 服务商注册表 | `/api/providers` | ① | 服务商页 | 已有 |
| 网关密钥 | `/api/keys` | ① | 密钥页 | 已有 |
| 用量汇总/趋势 | `/api/stats/summary`、`/daily` | ① | 仪表盘 + 统计页 | 已有 |
| 模型目录 | `/api/models`、`/api/status` | ① | 模型页 + 仪表盘 | 已有 |
| 服务商健康 | `/api/status` | ① | 仪表盘 | 已有 |
| 注册任务 | `/api/grok/register/tasks` | ① | 任务页 | 已有 |
| **分组管理** | `/api/sub2api/groups` | ② | 新增「配额」页（dock 或更多入口） | 路由完整，前端 0 实现；sub2api 核心能力 |
| **账号池管理** | `/api/sub2api/accounts`、`/reset` | ② | 配额页·账号池 tab | 与分组同属配额域，不放账号页避免与「控制面账号」混淆 |
| **流水查询** | `/api/sub2api/ledger` | ② | 配额页·流水 tab | 配额域 |
| **价格表管理** | `/api/pricing/books`、`/resolve`、`/import` | ② | 新增「定价」页（二级入口，放设置内或独立） | 路由完整无 UI；模型定价是网关计费依据 |
| **请求日志** | 控制面无路由（`api.ts` 调 `/api/logs`） | ② | 日志页（需先补后端路由） | 页面已存在但后端缺路由；request_logs 表已有（stats 在用） |
| **安全与 IP 管控** | 控制面无路由（`api.ts` 调 `/api/security/*`） | ② | 安全页（需先补后端路由） | 页面已存在但后端缺路由；net_guard.py 已有实现 |
| **测试台** | 控制面无路由（`api.ts` 调 `/api/playground/*`） | ② | 测试台页（需先补后端路由） | 页面已存在；可复用 `/v1/chat/completions` 直连，不一定需独立路由 |
| **按模型/按密钥用量分解** | 控制面无路由（`api.ts` 调 `/api/stats/by-model`、`/by-key`） | ② | 统计页新增两个分解 tab（需先补后端路由） | request_logs 表可直接 group by |
| **用量修复/重建** | `/api/stats/repair-usage`、`/rebuild-usage`（无路由） | ② | 统计页·操作区（需补路由） | 运维操作，低频 |
| **审计日志** | 控制面无路由（`api.ts` 调 `/api/audit-logs`） | ② | 安全页·审计 tab（需补路由） | security.audit_event 已写库 |
| **OAuth 批量登录** | 控制面无路由（`api.ts` 调 `/api/oauth/*`） | ③ | 用 API/脚本 | 仅 codebuddy 适用，且扫码登录 `/api/auth/start` 已覆盖单账号；批量 OAuth 属高频自动化，脚本更合适 |
| **签到日志/任务日志** | 控制面无路由 | ③ | 用 API/脚本 | 日志量大、排障用，不进主界面 |
| **上游实时日志** | 控制面无路由 | ③ | 用 API/脚本 | sidecar 日志应在 sidecar 处看 |
| **定时任务调度（task-run）** | 控制面无路由 | ③ | 用 API/脚本 | 调度配置应走配置文件，UI 易误触 |
| **积分领取调度（task-claim-schedule）** | 控制面无路由 | ③ | 用 API/脚本 | 同上 |
| **上游重载状态** | 控制面无路由 | ③ | 用 API/脚本 | 运维排障用 |
| **模型目录（catalog）** | 控制面无路由 | ③ | 用 API/脚本 | `/api/models` 已够，catalog 是冗余 |
| sidecar 内部能力（登录/签到/续期/代理池/图库） | sidecar 自管 | ③ | 不进界面 | 许可证与职责边界，控制面不复制 |

**新增页面汇总**：配额（分组/账号池/流水 3 tab）、定价、日志（补路由）、安全（补路由）、测试台（复用 v1）。其中日志/安全/测试台页面壳已存在，只缺后端路由；配额和定价是全新页面。

---

## 4. 视觉约束

- **必须复用**：`components/ui/table`、`components/ui/dialog`、`components/ui/alert-dialog`、`components/ui/badge`（只用 `secondary` / `outline` / `destructive` 三种 variant）、`components/ui/input`、`components/ui/select`、`components/ui/switch`、`components/ui/tabs`、`components/ui/sheet`、`components/ui/sonner`、`components/common/layout/EmptyState`、`components/common/layout/ConfirmDialog`。
- **禁止**：新增 CSS token 文件、新增全局样式、新配色、新徽章体系、新字体、渐变/发光/阴影自定义。
- **新页面的布局**：复用 `app/(main)/layout.tsx` 的容器 + `PageHeader`；表格列宽与 `ProviderPanel` 一致。
- **唯一需要新增的组件**：配额页的「分组-账号池」两级导航。理由：基准壳没有「页内两级对象」的现成组件（设置页是 tab 平铺，账号页是单表）。实现方式：左侧 `ScrollArea` 列分组名 + 右侧表格，复用 `table` 和 `badge`，不引入新样式。
- **风险表达**：沿用现有 red/amber/green 徽章（ProviderPanel 已有），不新增档位。

---

## 5. 分阶段实施

每阶段独立可验证、可回滚。阶段内先补后端路由（如缺），再接前端。

### P0：挂载已写好的死路由（0.5 天）

- 目标：让设置页的令牌/更新日志/系统三个 tab 不再 404。
- 涉及文件：
  - `apps/control/app/main.py:15-27` import 加 `tokens`、`system`；`main.py:160` include_router 加 `tokens`、`system`。
- 验收：设置页切到「令牌」tab 能看到密钥列表（与密钥页同数据，scope 显示 readonly）；「更新日志」tab 显示 CHANGELOG 解析结果；「系统」tab 的更新状态显示 available=false。
- 风险：低。两个路由都是薄封装，无新存储。

### P1：网关匿名开关 + 配额页（3–4 天）

- 目标：把控制面已有的两块后端能力（gateway 配置、sub2api）落进 UI。
- 涉及文件：
  - 前端：`apps/web/app/(main)/settings/page.tsx` 运行配置 tab 底部加 gateway Switch；新建 `apps/web/app/(main)/quota/page.tsx` + `apps/web/components/common/quota/`（GroupsPanel / AccountsPanel / LedgerPanel，复用 table/dialog/badge）。
  - 后端：无新增（路由已全）。
- 验收：
  - 设置·运行配置 tab 底部出现「允许匿名调用网关」Switch，开启时弹确认，保存走 `POST /api/settings/gateway`。
  - 配额页能建分组、建账号、重置额度、看流水；数据来自 `/api/sub2api/*`。
- 风险：中。sub2api 是 LGPL 重写，前端不得引入上游代码；配额页的两级导航是唯一新增组件。

### P2：补请求日志 + 安全 + 统计分解的后端路由（3–4 天）

- 目标：让已存在页面壳（日志/安全/统计）能真正工作。
- 涉及文件：
  - 后端：新增 `routers/logs.py`（`/api/logs`、`/api/logs/clear`，读 `request_logs` 表）；新增 `routers/security.py`（`/api/security/config`、`/rules`、`/logs`，复用 `net_guard.py`）；`stats.py` 加 `/by-model`、`/by-key`、`/repair-usage`；`settings.py` 加 `/audit-logs`（读 `audit_logs` 表）。
  - 前端：`apps/web/app/(main)/logs/page.tsx`、`security/page.tsx`、`stats/page.tsx` 接真实数据。
- 验收：日志页能按时间/密钥筛选请求记录；安全页能增删 IP 规则、看访问日志；统计页能按模型/密钥分解用量。
- 风险：中。`net_guard.py` 已有实现但需确认表结构；request_logs 表字段需与前端 `RequestLog` 类型对齐。

### P3：定价页 + 测试台（2–3 天）

- 目标：价格表可编辑；测试台可用。
- 涉及文件：
  - 前端：新建 `apps/web/app/(main)/pricing/page.tsx`（价格表表格 + 解析预览 + YAML 导入 Dialog）；`playground/page.tsx` 直接调 `/v1/chat/completions`（带管理员会话或专用 key）。
  - 后端：`pricing.py` 路由已有；测试台不需新路由。
- 验收：定价页能 CRUD 价格条目、解析模型名、导入 YAML；测试台选模型能发起 chat 并流式显示。
- 风险：低–中。测试台的鉴权需确认（管理员会话能否直调 /v1）。

### P4：审计日志 + 用量修复（1–2 天）

- 目标：审计可查、用量可修复。
- 涉及文件：
  - 后端：`/api/audit-logs`（P2 已加则跳过）；`/api/stats/repair-usage`、`/rebuild-usage`。
  - 前端：安全页·审计 tab；统计页操作区。
- 验收：安全页能看管理员操作审计；统计页能触发用量修复并显示修复条数。
- 风险：低。

---

## 6. 依赖与阻塞

### 已确认的后端缺口（阻塞前端）

| 端点 | 状态 | 阻塞的页面 | 处理 |
| --- | --- | --- | --- |
| `/api/tokens` | 路由文件存在，未挂载 | 设置·令牌 tab | P0 挂载 |
| `/api/system/changelog` | 路由文件存在，未挂载 | 设置·更新日志 tab | P0 挂载 |
| `/api/system/update-status` | 路由文件存在，未挂载 | 设置·系统 tab | P0 挂载 |
| `/api/logs` | 无路由 | 日志页 | P2 新增 |
| `/api/security/*` | 无路由 | 安全页 | P2 新增 |
| `/api/stats/by-model`、`/by-key` | 无路由 | 统计页分解 | P2 新增 |
| `/api/audit-logs` | 无路由 | 安全·审计 | P2/P4 新增 |
| `/api/oauth/*`、`/checkin-logs`、`/task-logs`、`/upstream/logs`、`/task-run`、`/task-claim-schedule`、`/upstream/reload-state`、`/api/model-catalog` | 无路由 | 对应 api.ts 声明 | 不进界面（§3 处置③），前端应删死代码 |

### 需要真实凭证才能验证的能力

- 账号签到/积分刷新（codebuddy 真实账号）
- 服务商连通性测试（各 provider 的 api_key）
- 测试台真实对话（需可用服务商 + 模型）
- 配额流水（需真实请求产生 billing_ledger）

### 已知口径问题（非阻塞，但 UI 要处理）

- muse2api `usage` 恒 0：统计页对 muse 的用量显示 0 属正常，不能报异常。
- grok 注册执行器未交付：任务页入队后任务会以「未配置执行器」失败，UI 要显示该原因，不能假装成功。
- `api_keys` 表无 `expires_at` / `last_used_ip` / `created_by`：令牌页这些字段显示「—」，不能编造。

---

## 7. 不做清单

| 不做 | 理由 |
| --- | --- |
| sub2api 的订阅/订单/支付/兑换码/分销 | 控制面未实现，且属商业 SaaS 能力，不在多服务商网关范围 |
| gptGrok2api 的图片图库/文件存储/代理池 | 能力在 sidecar 内，控制面不复制 |
| codar2api 的微信/短信登录、签到、代理池 | sidecar 自管，许可证虽 MIT 但私有接口不稳定 |
| Deepseek2Go 的 PoW 求解器 | 许可证不明（All Rights Reserved），且需逆向 |
| opencode2api 的匿名通道/重试策略 UI | GPL 上游，clean-room 不复制管理端 |
| 大改导航结构（侧栏/分组） | 违反 FRONTEND-BASELINE.md §3；新页面走 dock 或设置二级入口 |
| 品牌改名 | 违反 FRONTEND-BASELINE.md §3 |
| 新增视觉组件（除配额页两级导航） | 违反基准壳约束 |
| OAuth 批量登录 UI | 仅 codebuddy 适用，扫码登录已覆盖单账号，批量走脚本 |
| 签到日志/任务日志/上游实时日志 UI | 排障数据，量大，走 API/脚本 |
| 定时任务调度 UI | 调度走配置文件，UI 易误触 |
