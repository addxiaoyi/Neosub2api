# 功能缺口账本（FEATURE-GAP）

> 判定时点：2026-11-05。这是快照，不是承诺。代码、测试、文档三者冲突时以代码为准；本文件随时间必然过时，复核请直接看工作区，不要拿旧结论当依据。

## 1. 判定口径

只认代码与测试的实际状态，分三档：

| 档位 | 判定标准 |
| --- | --- |
| 已实现 | 控制面路由、运行时逻辑、前端入口、测试覆盖四件齐全。测试可以是独立用例，也可以是稳定夹具覆盖；只在本机未入库的，必须在 §5 标注。只有路由没有前端入口的，不算已实现。 |
| 半截 | 主链路能用，但缺至少一个能指出来的具体点：没有前端入口、没有测试、默认关闭、只做代理不透传、只在某一个 provider 生效。 |
| 未实现 | 仓内没有对应路由或运行时逻辑，或者只有文档规划。 |

证据一律写「文件:行」或「文件:函数名」，均指当前工作区。指不出来的写「未核实」，不猜行号、不引用旧盘点结论。

工作区当时有 139 项未提交改动（100 项修改 + 39 项未跟踪条目），见 §5；另有会话正在改 `apps/control`、`apps/web`，读到的代码可能是半成品。遇到明显半成品（例如路由缺处理分支、前端引用了尚不存在的接口）就在证据列标「实现中」，既不据此判已完成，也不据此判缺失。

## 2. 总表

| 编号 | 功能 | 对标仓库 | 现状档位 | 证据位置（当前工作区） | 阻塞原因 |
| --- | --- | --- | --- | --- | --- |
| E-01 | 账号批量动作（禁用/启用/删除/重启/签到，逐条结果） | codebuddy2api、sub2api | 已实现 | `apps/control/app/routers/accounts.py:640`（`POST /accounts/batch`）、`:573`（`/restart`）、`:355`（`/checkin-all`）；`apps/control/tests/test_backend_gaps.py:97,127,151`；`apps/web/app/(main)/accounts/page.tsx:1267-1288`（批量操作条） | 无（代码与测试均未入库） |
| E-02 | 账号导入 + 保存前试连 | codebuddy2api | 已实现 | `accounts.py:368`（`/import`）、`:432`（`/import/probe`）；`tests/test_accounts_import.py:73-246`；`apps/web/components/common/accounts/ImportAccountDialog.tsx` | 无（未入库） |
| E-03 | 访问令牌批量动作 | 控制面自建（各上游共用） | 已实现 | `apps/control/app/routers/keys.py:205`（`POST /batch`）；`tests/test_keys_batch.py:55-198`（含超限 400、不泄密、单行审计）；`apps/web/app/(main)/keys/page.tsx:210`（批量操作条） | 测试未入库 |
| E-04 | 请求日志导出 CSV | sub2api（系统日志）、控制面自建 | 已实现 | `apps/control/app/routers/logs.py:304`（`GET /logs/export`）；`tests/test_logs_export.py:104-197`（同筛选、6 类凭据脱敏、公式注入中和、超限报错不截断）；`apps/web/app/(main)/logs/page.tsx:253` | 前端整页未入库 |
| E-05 | 模型映射导入 / 导出 / 回滚 | codebuddy2api（模型映射页） | 已实现 | `apps/control/app/routers/settings.py:866,887,1085`；`tests/test_model_map_transfer.py:40-136`；`apps/web/app/(main)/settings/page.tsx:1848` | 组件与测试未入库 |
| E-06 | Antigravity / Sora / Claude Code 客户端兼容面 | sub2api | 已实现 | `apps/control/app/routers/v1.py:890-984`；`tests/test_v1_client_compat.py:224-500` | 未入库；只对声明了 compat surface 的 provider 生效，其余 501（有意） |
| E-07 | TOTP 两步验证 + 恢复码 | 控制面自建 | 已实现 | `apps/control/app/routers/auth.py:321-462`（status/setup/enable/disable/verify）；`apps/control/app/totp.py`；`apps/web/components/common/layout/ManagementBar.tsx:643`；`tests/test_user_account_system.py:163-253` | `totp.py` 未入库 |
| E-08 | 多用户与角色体系 | 控制面自建 | 已实现 | `settings.py:1430-1502`（users CRUD）；`auth.py` 注册开关 / 登录冷却 / 会话轮换；`tests/test_user_account_system.py:60-278` | 测试未入库 |
| E-09 | 跨 provider 故障转移 | 控制面自建（多上游共性） | 已实现 | `apps/control/app/model_resolve.py:388`（`resolve_targets`）；`v1.py:426-427`（尝试序列与上限）；`settings.py:532,537`（`/api/settings/failover` 读写，带 env 锁定提示）；`apps/web/app/(main)/settings/RuntimeConfigSection.tsx:66`（开关/上限 UI 与来源提示）；`tests/test_failover.py:340-543` | 未入库 |
| E-10 | sub2api 订阅账号池管理面（分组/账号/重置/流水） | sub2api | 已实现 | `apps/control/app/routers/sub2api.py:57-150`（groups 读写删、accounts 读写删、reset、ledger）；`apps/web/app/(main)/quota/page.tsx:57`（三个 tab）；`quota/policies/page.tsx`；测试由 `tests/test_capability_truth.py:364`、`tests/test_billing_responses_usage.py:272` 当夹具调用（无独立用例） | 无 |
| E-11 | 指标与告警（Prometheus 指标、配额/连败/usage_missing 告警） | sub2api Ops（观测部分） | 已实现 | `apps/control/app/routers/health.py:410`（`GET /metrics`，`:430` `GET /metrics/json`）；`apps/control/app/alerting.py:192`（`evaluate_alerts`，两处指标读取时都会触发）；`quota.py:367`（`GET /alerts`） | 告警在读取时评估，无独立调度（与 P-03 的缺口不重复计） |
| E-12 | 备份 / 恢复（本地归档） | 控制面自建 | 已实现（工作区未入库） | `apps/control/app/routers/backup.py:164,191,227`；`apps/web/components/common/settings/BackupPanel.tsx`；`settings/page.tsx:2018`；`tests/test_backup_api.py:70-467` | 全部未入库（API/面板/测试为 `??`，`settings/page.tsx` 为未提交修改）；远端与定时见 U-03 |
| E-13 | qoder 会话搬运 | qoder-trae-workbuddy-switch | 已实现（受上游路径语义限制） | `apps/control/app/routers/qoder_sessions.py:38`（导出）、`:43`（导入），均为整包透传；`apps/web/components/common/settings/QoderSessionsPanel.tsx:21-204`；`ProvidersSection.tsx:812`；`tests/test_qoder_sessions_proxy.py:37,49` | 上游 `SessionCopy` 只认绝对路径，包留在 sidecar 主机，浏览器既不能上传也不能下载——这是上游协议边界，不是本仓漏做 |
| P-01 | provider 定时健康检测 | sub2api（账号健康）、codebuddy2api | 半截 | 调度器 `apps/control/app/services/provider_health.py:46-150`；API `providers.py:30,43`；`main.py:148-157` 启动/停止；判定内核 `services/provider_probe.py`；测试 `tests/test_provider_health.py:123-235` | 前端无入口；默认关闭（`config.py:126` `health_check_enabled: bool = False`，测试 `:218` 也锁定这个默认） |
| P-02 | `/quota/policies` 页面 | sub2api | 半截（基线未登记） | `apps/control/app/routers/quota.py:174-285`；`apps/web/app/(main)/quota/policies/page.tsx`；`FRONTEND-BASELINE.md:67` 页面清单只登记 `/quota` | 需基线登记或产品确认，不是代码缺口 |
| P-03 | sub2api Ops 排障面 | sub2api | 半截 | 已有等价观测：`health.py:410`、`alerting.py`、`logs.py:260`；缺的能力在 `routers/` 全目录无对应路由（本轮 grep 无命中） | 需产品决策是否自建排障台；上游 LGPL 隔离（`upstreams/upstreams.yaml:87,94`） |
| U-01 | 在线更新执行 | codebuddy2api（系统更新页）、sub2api | 未实现 | `apps/control/app/routers/system.py:1-6`（模块说明）+ `:87-89`（`update_summary` 恒返回 `available=False`）+ `:147-160`（`/update-status` 固定 `running=False`、`updater_found=False`）；`UpdatePanel.tsx:61-62` 的 `has_update` 分支是死代码 | 无分发通道与签名校验方案，需设计决策 |
| U-02 | 未定价模型 token 估算计费 | gptGrok2api、muse2api | 未实现（有意不做） | `apps/control/app/billing.py:320-323`（只打 `usage_zero` / `usage_missing` 标，不估 token）；`docs/muse-usage-policy.md:11-12`；`docs/m3-billing-plan.md:161` 的 `billing.charge_on_missing_usage` 在代码中不存在 | 需财务决策并先补价格行；估算误差写的是「可见文本 ±15–30%」（`muse-usage-policy.md:43`） |
| U-03 | 备份远端目标（S3 等）与定时计划 | 控制面自建 | 未实现（有意不做） | `docs/BACKUP.md:174`（「不做加密、不上传远端」）；`backup.py` 无远端参数与调度 | 需对象存储凭据、失败重试与保留策略设计 |
| U-04 | `/tasks` 页面恢复 | sub2api / codebuddy2api（任务页） | 未实现（有意删除） | 仓库无 `tasks` 路由（`apps/web/app` 下无该目录）；`FRONTEND-BASELINE.md:70` 记录整条链路已删 | 与基线删页决定冲突，需产品决策 |
| U-05 | gptGrok 图库 / 提示词 | gptGrok2api | 未实现（有意不做） | `docs/design/frontend-integration-plan.md:99`（「图片图库/文件存储 不进界面」）；控制面无对应路由 | 需产品决策是否自建；存储与权限是新增面 |
| U-06 | sub2api iframe 嵌入 | sub2api | 未实现（有意不做） | 全仓无 `iframe`（本轮 grep 无命中）；`PROVIDERS.md:257` 取舍 #10「只做协议层，不做各家上游 Web UI」 | 与全局设计边界冲突；LGPL 隔离 |

## 3. 逐项说明

### 3.1 已实现（E-01 ~ E-13）

**E-01 账号批量动作。** 路由 `accounts.py:640` 支持按文件名列表执行批量动作，测试覆盖了部分失败时继续执行、逐条返回错误、凭据回收。前端账号页有批量选择入口。缺的不是能力，是入库。

**E-02 账号导入。** `accounts.py:369` 是单条导入：先建统一凭据（`registry.create_credential`），再写 `accounts` 行并把 `credential_id` 记进 `meta_json`，响应不回显明文。`:432` 是保存前试连，禁用 provider 直接 400。测试 `test_accounts_import.py:73-246` 覆盖脱敏、JSON 位置报错、凭据类型非法、sidecar provider 拒绝、非管理员拒绝。前端用 `ImportAccountDialog.tsx`。

**E-03 令牌批量。** `keys.py:205` 只允许 enable / disable / delete（`:148`），单次上限 200（`:150`），测试覆盖超限 400、部分失败继续、响应不含明文密钥、只写一行审计。

**E-04 日志导出。** `logs.py:304` 与列表页共用同一套筛选；导出时对 6 类凭据做脱敏，并把 `=`、`+` 开头的单元格前置制表符以防 CSV 公式注入；超过上限直接报错而不是静默截断。

**E-05 模型映射搬运。** 导出下载当前映射，导入分 merge / overwrite 两种语义并逐条报告改动，回滚恢复上一版。

**E-06 客户端兼容面。** `v1.py:890-984` 挂了 Claude Code、Antigravity、Sora 三组路径，包含 `v1beta` 模型列表、媒体签名路径与匿名 GET 白名单。没有 compat surface 的 provider 返回 501，这是设计，不是缺口。

**E-07 TOTP。** 登录、绑定、验证、关闭全链路，恢复码一次性使用，重启计数器防重放；关闭必须同时给密码和验证码。

**E-08 多用户。** viewer / admin 两角色，注册开关、登录冷却与管理员豁免、会话令牌轮换；旧 `user` 角色有迁移测试。

**E-09 故障转移。** 5xx、429、首字节前流失败会换目标；客户端 400 不重试；显式指定 provider 时固定不重试；尝试次数有上限；开关与上限可运行时配置。

**E-10 sub2api 管理面。** 分组、账号、额度重置、扣减流水四块后端已入库；前端在 `/quota` 三个 tab 里。

**E-11 指标与告警。** 两个出口：`/api/metrics`（Prometheus 文本，admin 或抓取令牌可读）与 `/api/metrics/json`（旧版 JSON，保持字段不变）。两条路径都会先跑 `evaluate_alerts`，失败只记日志、不带走指标读取；告警表复用 `quota_alerts`，按 `(policy_id, threshold_percent, period_start)` 去重。

**E-12 备份 / 恢复。** 归档、校验、恢复、自检失败不改线上库、恢复失败从快照回滚、WAL 打开时仍可用，测试覆盖了这些关口。这是本地归档，远端与定时不在内。

**E-13 qoder 会话搬运。** 控制面把导出、导入整包透传给 `qoder_sidecar`，前端面板两个方向都做，回包原样展示、不替上游改写措辞。限制来自上游 `SessionCopy` 只认绝对路径：包落在 sidecar 主机，浏览器不能上传也不能下载。要把包搬回浏览器，得 sidecar 先提供流式出口，这一步不在本仓控制范围内。

### 3.2 半截（P-01 ~ P-03）

**P-01 provider 定时健康检测。** 后端是完整的：`ProviderHealthService` 有启停、定时 tick、单次触发、历史写入与裁剪、快照读取，`/api/providers/health` 和 `/api/providers/health/run` 都能用，`main.py` 挂了生命周期，`tests/test_provider_health.py:123-235` 覆盖了关闭态不动作、开启态执行、三档结果落库、错误摘要脱敏、只写历史表不改 `enabled`、快照、admin-only、默认关闭。唯一的缺口是前端：没有任何入口能看历史或手动触发。另外默认 `UNIFIED_HEALTH_CHECK_ENABLED=false`，要常开等于按间隔持续打上游，这需要产品与成本判断。

**P-02 `/quota/policies`。** 页面与 API 都可用，`/quota` 页顶部有入口。缺的是基线登记：`FRONTEND-BASELINE.md:67` 的页面清单只有 `/quota`，§3 对「新增页面」的约束以 §4 现状备注为准，这个子页没出现在任何一处。这不是代码缺口，PR 审查时会被当作未登记入口问一次。

**P-03 sub2api Ops 排障面。** 我们有自己的等价观测（`/metrics`、告警、日志、导出），但没有上游 Ops 的那几块：告警规则 CRUD（只做了配额阈值）、告警事件确认/静默/关闭、失败请求重试与「标记已解决」、系统日志采集、并发与实时流量、网关运行时状态。`routers/` 全目录没有对应路由。补齐前要先定：是自建排障台，还是明确不做、把这块写进 `PROVIDERS.md` 的取舍表。

### 3.3 未实现（U-01 ~ U-06）

**U-01 在线更新。** 后端明确「本部署不提供在线更新」，`check-update` 与 `update-status` 共用同一份 `available=False` 摘要。前端 `UpdatePanel` 仍保留了 `manager.has_update` / `upstream.has_update` 分支，服务端永远不会返回 true，属死代码。要补齐，需要先有发布通道、包签名与回滚方案。

**U-02 token 估算计费。** 现在只把「上游返回全零」和「根本没有 usage」分成 `usage_zero` / `usage_missing` 两种标注，不做估算。`m3-billing-plan.md:161` 写的 `billing.charge_on_missing_usage` 开关在代码里不存在，文档与实现有漂移。要补齐，前提是财务确认要收这条通道的钱，并先给 muse 别名补价格行——否则估出来的 token 套不到价，仍是 0 元。

**U-03 备份远端与定时。** `BACKUP.md:174` 明说不加密、不上传远端。所以这是有意不做的取舍，不是漏做；真要补，前置是对象存储凭据管理、失败重试、保留策略。

**U-04 `/tasks`。** 页面、9 条幽灵路径已按业务决定整体删除，基线也不再保留。要恢复必须推翻那条决定，且与 §3「不新增页面」冲突。

**U-05 gptGrok 图库 / 提示词。** 设计口径明确「图片图库/文件存储不进界面」（`frontend-integration-plan.md:99`），控制面也没有对应路由。要补，得先有图库路由与存储、权限模型，等于新做一块能力，而不只是加个页面。

**U-06 sub2api iframe。** 全仓没有 iframe。取舍 #10 明确「只做协议层，不做各家上游 Web UI」。嵌 iframe 等于把上游 Web 面板当产品界面，与这条边界直接冲突，也会把 LGPL 的边界问题带回来。

## 4. 旧结论更正留档

体例：原句逐字保留，紧跟一个更正块，块内写清误判后果。

### 4.1 前端「sub2api 完全缺失」

> sub2api 是控制面后端能力最完整但前端完全缺失的一块。
>
> —— `docs/design/frontend-integration-plan.md:126`

**（2026-11-05 已更正）** 这句话和它上面 118-123 行的「无 UI」表格已过期。`apps/web/app/(main)/quota/page.tsx:57` 已实现分组 / 账号 / 流水三个 tab，页面本身已入库；额度重置走账号 tab。实际仍缺的只有 Ops 排障面（见 P-04），不是「前端完全缺失」。
**误判后果**：按旧句排期会把已经做完的 CRUD 再排一遍，或误判 `quota` 页是越界新增；同时把真正缺的排障面当成「顺带就有了」。

### 4.2 gptGrok images / videos「控制面未接入」

> chat / responses / messages / images / videos | `/v1/*` | 走网关（chat/models 已接；images/videos 控制面未接入）
>
> —— `docs/design/frontend-integration-plan.md:96`

**（2026-11-05 已更正）** `routers/v1.py:844-869` 已挂 `/v1/images/generations`、`/v1/images/edits`、`/v1/images/tasks/{task_id}`、`/v1/videos`、`/v1/videos/{task_id}`、`/v1/media/{name}`，`/v1` 路由总数为 14 条（`v1.py:806-884`）。这几条尚在工作区未入库（`M apps/control/app/routers/v1.py`）。
**误判后果**：会把已经写好的图片/视频转发当成缺口重做；更糟的是有人据此在 `PROTOCOL-MATRIX` 的「仍不支持」清单里留下错误条目，客户端按 501 处理而实际可用。

### 4.3 旧「11 项真缺失」清单

原清单没有落成仓内文档，只在会话里口口相传，无法逐字留档。按本文件的口径复核后：账号批量、令牌批量、日志导出、模型映射搬运、Antigravity/Sora 兼容面、备份恢复六类已经在工作区实现（多数尚未入库），不再算「完全没有」；qoder 会话已落地（E-13，受上游路径语义限制）。剩下仍成立的是 P-01 / P-03 / U-01 / U-02 / U-03 与前端三条（U-04 ~ U-06）。旧数字 11 已不可引用。

## 5. 本轮新增但未进 CI 的证据

`git status --porcelain` 只读结果（2026-11-05，未做任何 add / commit / stash / checkout）：

- 判定时合计 139 行：100 项 `M`（修改）+ 39 项 `??`（未跟踪，其中部分为目录）。写完本文件后再测为 141 行（100 `M` + 41 `??`）：本文件自身 +1，另外 +1 在判定之后由并行会话写入，未归属核实。这说明这份清单本身在变动，引用前请重跑 `git status`。
- `.github/workflows/ci.yml` 有未提交改动：`+104 / -1`，新增了「pytest 零 skip 门」「备份恢复演练（backup → restore → Python 复查）」「`scripts/backup_api_smoke.py` API 关口」三道门。
- 未入库的后端代码（节选，均 `??`）：`apps/control/app/routers/backup.py`、`apps/control/app/routers/qoder_sessions.py`、`apps/control/app/totp.py`、`apps/control/app/alerting.py`、`apps/control/app/services/provider_health.py`、`apps/control/app/services/provider_probe.py`、`scripts/backup_api_smoke.py`。
- 未入库的测试：`apps/control/tests/` 共 57 个测试文件，其中 20 个未跟踪（`??`）。关键的包括 `test_backup_api.py`、`test_backend_gaps.py`、`test_keys_batch.py`、`test_logs_export.py`、`test_model_map_transfer.py`、`test_provider_health.py`、`test_qoder_sessions_proxy.py`、`test_failover.py`、`test_user_account_system.py`、`test_v1_client_compat.py`。
- 未入库的前端（节选，均 `??`）：`apps/web/app/(main)/logs/page.tsx`、`apps/web/components/common/settings/{BackupPanel,ModelMapTransfer,QoderSessionsPanel}.tsx`、`apps/web/components/common/security/ClearLoginFailuresDialog.tsx`。
- 已修改但未入库的关键文件（节选）：`apps/control/app/routers/{v1,accounts,keys,logs,settings,providers,quota,auth}.py`、`apps/control/app/{main,config,billing,db}.py`、`apps/web/app/(main)/settings/page.tsx`、`apps/web/app/(main)/quota/policies/page.tsx`、`apps/web/lib/api.ts`、`apps/web/lib/i18n/locales/zh-CN.json`。
- `apps/control/app/routers/sub2api.py`、`apps/control/app/routers/system.py`、`apps/web/app/(main)/quota/page.tsx`、`scripts/backup_core.py` 为已入库且未改动。
- 本文件引用的 `docs/RISK-REGISTER.md` 与 `docs/schema-migration-audit.md` 自身也是 `??`（未入库）；`docs/FRONTEND-BASELINE.md`、`docs/PROVIDERS.md`、`docs/PROTOCOL-MATRIX.md`、`docs/BACKUP.md`、`docs/design/frontend-integration-plan.md` 均为 `M`（已修改未提交）。也就是说，§4 更正所依据的四份口径文档，当前版本也还没有进版本库。

**本地跑绿不等于 CI 跑绿。** 本轮只做代码与测试文件核对，没有执行任何测试，也不声称上面这些用例跑过。20 个未入库测试文件（上列 10 个只是关键项）一个都不在版本库里，`.github/workflows/ci.yml` 的零 skip 门与两道备份演练也还没提交。当前 CI 能跑到的只是旧用例；本文件标「已实现」的条目，依据是「代码 + 用例存在」，不是「用例通过」。入库前必须先在干净 CI 环境实跑，尤其是依赖假上游、端口与临时目录的备份与 failover 用例。

## 6. 复核方式

对任一编号，按证据列的文件:行直接打开当前工作区核对；拿不到对应函数或路由即视为「未核实」，按 §1 重新归档，不要沿用本文件。发现本文件与代码不符时，按 §4 体例追加更正块，不删原句。
