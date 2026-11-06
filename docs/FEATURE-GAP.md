# 功能缺口账本（FEATURE-GAP）

> 判定时点：2026-11-05 首判；2026-11-06 二次复核（本版）。这是快照，不是承诺。代码、测试、文档三者冲突时以代码为准；本文件随时间必然过时，复核请直接看工作区，不要拿旧结论当依据。

## 1. 判定口径

只认代码与测试的实际状态，分三档（已实现 / 半截 / 未实现），另设三个状态档（待决策 / 待复核 / 不做）。后三档都不是已验收的缺口，也不能互相替代：

| 档位 | 判定标准 |
| --- | --- |
| 已实现 | 控制面路由、运行时逻辑、前端入口、测试覆盖四件齐全。测试可以是独立用例，也可以是稳定夹具覆盖；只在本机未入库的，必须在 §5 标注。只有路由没有前端入口的，不算已实现。 |
| 半截 | 主链路能用，但缺至少一个能指出来的具体点，且必须在阻塞原因里写清缺什么：没有前端入口、没有测试、默认关闭、只做代理不透传、只在某一个 provider 生效，或只做了写入方向而缺必需的反向环节（例如只有上传、没有恢复）。半截不得按已实现验收。 |
| 未实现 | 仓内没有对应路由或运行时逻辑，或者只有文档规划。 |
| 待决策 | 缺项已经具体到某个人拍某个头，拍板前没法开工。阻塞原因必须写「谁点头、点什么头」，只写「需产品决策」不合格。拍板前不排期。 |
| 待复核 | 范围或验收标准还不能由现有文档与代码判定，需要先复核。不得据此判已实现，也不得按已实现验收；复核后必须落到其余五档之一。 |
| 不做（非缺口） | 有意不做，不排期：目标能力已有等价承载，或属全局设计边界的明确排除项。归到这一档的条目不得进缺口清单，也不得按「未实现」排期。 |

证据一律写「文件:行」或「文件:函数名」，均指当前工作区。指不出来的写「未核实」，不猜行号、不引用旧盘点结论。

工作区当时有 139 项未提交改动（100 项修改 + 39 项未跟踪条目），见 §5；另有会话正在改 `apps/control`、`apps/web`，读到的代码可能是半成品。遇到明显半成品（例如路由缺处理分支、前端引用了尚不存在的接口）就在证据列标「实现中」，既不据此判已完成，也不据此判缺失。

**（2026-11-06 复核）** 上面「139 项」是 2026-11-05 快照。本次复核的时点在提交之前，那时 `git status --porcelain` 为 53 行（38 项 `M` + 15 项 `??` 条目，含本文件与 `.gitignore` 修改），按文件计 54 个（`apps/web/components/common/onboarding/` 展开为 `OnboardingWizard.tsx`、`OnboardingTodo.tsx` 两个文件）。复核之后本轮 10 个提交已入库（`2268d50`…`8b5b9e5`），工作区 `git status --porcelain` 为空；N-03 打包导入导出与 N-09 凭据引导也随这批提交进了库，不再存在「未提交」的残留。逐项见 §5 末块「2026-11-06 入库后更正」。

## 2. 总表

| 编号 | 功能 | 对标仓库 | 现状档位 | 证据位置（当前工作区） | 阻塞原因 |
| --- | --- | --- | --- | --- | --- |
| E-01 | 账号批量动作（禁用/启用/删除/重启/签到，逐条结果） | codebuddy2api、sub2api | 已实现 | `apps/control/app/routers/accounts.py:640`（`POST /accounts/batch`）、`:573`（`/restart`）、`:355`（`/checkin-all`）；`apps/control/tests/test_backend_gaps.py:97,127,151`；`apps/web/app/(main)/accounts/page.tsx:1267-1288`（批量操作条） | 无（后端文件首入库 b5772fb、批量操作在 81bb7d8、测试 ca7959b、前端 adf22d8 入库） |
| E-02 | 账号导入 + 保存前试连 | codebuddy2api | 已实现 | `accounts.py:368`（`/import`）、`:432`（`/import/probe`）；`tests/test_accounts_import.py:73-246`；`apps/web/components/common/accounts/ImportAccountDialog.tsx` | 无（已在 81bb7d8 入库，前端组件在 adf22d8 入库） |
| E-03 | 访问令牌批量动作 | 控制面自建（各上游共用） | 已实现 | `apps/control/app/routers/keys.py:205`（`POST /batch`）；`tests/test_keys_batch.py:55-198`（含超限 400、不泄密、单行审计）；`apps/web/app/(main)/keys/page.tsx:210`（批量操作条） | 无（后端批量与测试在 8b2d54d 入库、前端在 adf22d8 入库） |
| E-04 | 请求日志导出 CSV（管理员与普通用户） | sub2api（系统日志）、控制面自建 | 已实现 | `apps/control/app/routers/logs.py:64`（`_owner`）、`:71`（`_assert_key_owned`）、`:376`（`GET /logs/export`）、`:354`（`_audit_export`，动作名 `logs.export` 见 `:369`）；`tests/test_logs_export.py:113-137`（viewer 可导出）、`:139-194`（按 key 归属收窄、显式传他人 key 403、审计 `logs.export`）；`apps/web/app/(main)/logs/page.tsx:296-306`（导出按钮对所有登录用户可见） | 无（导出路由与测试在 82f03a2 入库、文件首入库 dc19c82，前端 eacc574 入库；2026-11-06 扩到普通用户，见 N-04） |
| E-05 | 模型映射导入 / 导出 / 回滚 | codebuddy2api（模型映射页） | 已实现 | `apps/control/app/routers/settings.py:866,887,1085`；`tests/test_model_map_transfer.py:40-136`；`apps/web/app/(main)/settings/page.tsx:1848` | 无（已在 65e88b9 入库，组件在 dafb3c4 入库） |
| E-06 | Antigravity / Sora / Claude Code 客户端兼容面 | sub2api | 已实现 | `apps/control/app/routers/v1.py:890-984`；`tests/test_v1_client_compat.py:224-500` | 无（已在 65e88b9 入库）；只对声明了 compat surface 的 provider 生效，其余 501（有意） |
| E-07 | TOTP 两步验证 + 恢复码 | 控制面自建 | 已实现 | `apps/control/app/routers/auth.py:321-462`（status/setup/enable/disable/verify）；`apps/control/app/totp.py`；`apps/web/components/common/layout/ManagementBar.tsx:643`；`tests/test_user_account_system.py:163-253` | 无（已在 8b2d54d 入库） |
| E-08 | 多用户与角色体系 | 控制面自建 | 已实现 | `settings.py:1430-1502`（users CRUD）；`auth.py` 注册开关 / 登录冷却 / 会话轮换；`tests/test_user_account_system.py:60-278` | 无（users CRUD 基线在 b5772fb，账号体系改动与测试在 8b2d54d 入库） |
| E-09 | 跨 provider 故障转移 | 控制面自建（多上游共性） | 已实现 | `apps/control/app/model_resolve.py:388`（`resolve_targets`）；`v1.py:426-427`（尝试序列与上限）；`settings.py:532,537`（`/api/settings/failover` 读写，带 env 锁定提示）；`apps/web/app/(main)/settings/RuntimeConfigSection.tsx:66`（开关/上限 UI 与来源提示）；`tests/test_failover.py:340-543` | 无（已在 65e88b9 入库；运行参数前端在 7f4d9a1 入库） |
| E-10 | sub2api 订阅账号池管理面（分组/账号/重置/流水） | sub2api | 已实现 | `apps/control/app/routers/sub2api.py:57-150`（groups 读写删、accounts 读写删、reset、ledger）；`apps/web/app/(main)/quota/page.tsx:57`（三个 tab）；`quota/policies/page.tsx`；测试由 `tests/test_capability_truth.py:364`、`tests/test_billing_responses_usage.py:272` 当夹具调用（无独立用例） | 无 |
| E-11 | 指标与告警（Prometheus 指标、配额/连败/usage_missing 告警） | sub2api Ops（观测部分） | 已实现 | `apps/control/app/routers/health.py:410`（`GET /metrics`，`:430` `GET /metrics/json`）；`apps/control/app/alerting.py:192`（`evaluate_alerts`，两处指标读取时都会触发）；`quota.py:367`（`GET /alerts`） | 告警在读取时评估，无独立调度（与 P-03 的缺口不重复计） |
| E-12 | 备份 / 恢复（本地归档） | 控制面自建 | 已实现 | `apps/control/app/routers/backup.py:164,191,227`；`apps/web/components/common/settings/BackupPanel.tsx`；`settings/page.tsx:2018`；`tests/test_backup_api.py:70-467` | 无（API / 测试 / 冒烟脚本已在 98d7b31 入库，面板在 dafb3c4 入库）；远端与定时见 U-03 |
| E-13 | qoder 会话搬运 | qoder-trae-workbuddy-switch | 已实现（受上游路径语义限制） | `apps/control/app/routers/qoder_sessions.py:38`（导出）、`:43`（导入），均为整包透传；`apps/web/components/common/settings/QoderSessionsPanel.tsx:21-204`；`ProvidersSection.tsx:812`；`tests/test_qoder_sessions_proxy.py:37,49` | 上游 `SessionCopy` 只认绝对路径，包留在 sidecar 主机，浏览器既不能上传也不能下载——这是上游协议边界，不是本仓漏做 |
| P-01 | provider 定时健康检测 | sub2api（账号健康）、codebuddy2api | 半截 | 调度器 `apps/control/app/services/provider_health.py:46-150`；API `providers.py:30,43`；`main.py:148-157` 启动/停止；判定内核 `services/provider_probe.py`；测试 `tests/test_provider_health.py:123-235` | 前端无入口；默认关闭（`config.py:126` `health_check_enabled: bool = False`，测试 `:218` 也锁定这个默认） |
| P-02 | `/quota/policies` 页面 | sub2api | 已实现 | 策略 CRUD `apps/control/app/routers/quota.py:193,244,308`（阈值 1-100 校验 `:236-241`），周期用量与告警列表 `:356,390`，告警确认 / 静默 / 关闭 / 重开与审计 `:473-566`；页面 `apps/web/app/(main)/quota/policies/page.tsx:373-384`（页头）、`:512`（删除确认）、`:771-830`（告警处置）；入口 `apps/web/app/(main)/quota/page.tsx:78-82,401`；基线登记 `FRONTEND-BASELINE.md:74`；测试 `tests/test_quota.py:526,559-600`、`tests/test_ops_triage.py:192-229` | 2026-11-05 基线登记完成，阻塞原因解除 |
| P-03 | sub2api Ops 排障面 | sub2api | 半截（2 真缺 + 2 待决策） | 已于 2026-11-05 复核实现：告警确认 / 静默 / 关闭 / 重开与审计 `quota.py:473-566`，失败请求标记已解决 / 取消 `logs.py:398-449`；真缺①系统日志采集：仓内只有请求日志（`logs.py:270,314`），`system.py` 仅 `:77` changelog、`:95` versions，无进程 / 上游日志路由；真缺②实时流量：无推送与聚合视图，只有指标快照（`health.py:419,439`），现有页面可用轮询承担一部分；待决策③运行时状态：`health.py:117,122` 仅存活 / 就绪，`settings.py:425` 是运行时配置，缺「并发 / 在途 / 调度器」看哪些指标的定义；待决策④告警规则管理面：规则参数只在 `config.py:132-140` 与 env 可配，无 DB CRUD（配额阈值走 `/quota/policies`） | 系统日志与实时流量需运维负责人拍板采集面、保留期与隐私边界；③运行时状态指标需运维负责人点头「采哪几项（并发 / 在途 / 调度器队列 / 失败率）」并写进验收口径；④告警规则管理面需产品负责人点头「是否要 DB CRUD 管理面（还是维持 env / config 可配）」，并要运维负责人确认「规则是否允许在页面改」。上游 LGPL 隔离仍成立（`upstreams/upstreams.yaml:87,94`） |
| U-01 | 在线更新执行 | codebuddy2api（系统更新页）、sub2api | 已实现 | `apps/control/app/routers/update.py:57`（`/update-status`）、`:62`（`/update/check`）、`:75`（`/update/apply`，`:83-91` 要求版本逐字一致且 `available`/`can_apply` 双闸）、`:106`（`/update/rollback`）；`services/update_feed.py:78-120`（能力探测与 `can_apply`）、`update_executor.py:4-5`（换码与重启路径）、`update_signing.py`、`update_manifest.py`、`update_boot.py`；前端 `apps/web/components/common/settings/UpdatePanel.tsx:42`（挂在 `apps/web/app/(main)/settings/page.tsx:2016`）；测试 `tests/test_update_manifest.py:68-162`、`tests/test_update_feed.py:125-193`、`tests/test_update_executor.py:117-295` | 无缺口，两个前置条件：①默认 `UNIFIED_UPDATE_ENABLED=false`（`config.py:149,222`），需运营显式开启；②需自备发布源 `UNIFIED_UPDATE_MANIFEST_URL` 与签名密钥（`config.py:150-151,224-225`）。容器部署还要求代码目录绑挂并配 `restart: unless-stopped`，否则探测直接 `can_apply=false`（`update_feed.py:101-108`）。实现已在 8253f2c、9349160 入库 |
| U-02 | 未定价模型 token 估算计费 | gptGrok2api、muse2api | 待决策 | `apps/control/app/billing.py:320-323`（只打 `usage_zero` / `usage_missing` 标，不估 token）；`docs/muse-usage-policy.md:11-12`；`docs/m3-billing-plan.md:161` 的 `billing.charge_on_missing_usage` 在代码中不存在；估算精度口径 `docs/muse-usage-policy.md:43`（可见文本 ±15–30%） | 需财务负责人点头：估算出的 token 是否计入额度与账单（即「猜出来的用量要不要收钱」），误差是写进账单还是只做提示。点头前还要先给 muse 别名补价格行，否则估出 token 仍套不到价 |
| U-03 | 备份远端目标（S3 等）与定时计划 | 控制面自建 | 已实现 | 端点 `apps/control/app/routers/backup_remote.py:88,94,114,140,166,222`（配置读取 / 保存、远端列表、手动上传、远端恢复、删除）；下载与解密 `services/backup_remote.py:397`（`download_archive`，先验 `.enc.sha256` 再解密）、`:475`（`commit_download`）、`:489`（`delete_remote`，连 sidecar 一起删）、`:527`（`upload`）、`:716`（按 keep 轮转）；加密 `services/backup_crypto.py`（AES-256-GCM，`crypto_backend` 报 lib/stdlib）；恢复复用 `routers/backup.py:182` 抽出的 `restore_archive()`，本地 `/restore` 在 `:250` 持锁后调用；定时 `services/backup_schedule.py:31-43,64-102`；S3 客户端 `services/s3_client.py`；四个开关 `config.py:183-186,262-269`；lifespan 启停 `main.py:161-162,175,182`；前端 `apps/web/components/common/settings/BackupRemoteSection.tsx`（由 `BackupPanel.tsx` 渲染）；测试 `tests/test_backup_remote.py`、`tests/test_backup_schedule.py`、`tests/test_s3_sigv4.py` | 无缺口；默认 `UNIFIED_BACKUP_REMOTE_ENABLED=false`、开关关闭时零外呼，凭据只从 `UNIFIED_BACKUP_S3_*` 环境变量读，需运营显式开启并配齐。已入库：加密后端 4d7e54a、S3 客户端 9ebff30、路由与调度 500e671、前端面板 622fa9f；分段回放见 §3.3 末块与 §4.11 末块 |
| U-04 | `/tasks` 页面恢复 | sub2api / codebuddy2api（任务页） | 待决策 | 仓库无 `tasks` 路由（`apps/web/app` 下无该目录）；`FRONTEND-BASELINE.md:70` 记录整条链路已删 | 需产品负责人点头：是恢复已删的 `/tasks` 页面（等于推翻基线删页决定，并新增页面，与 §3「不新增页面」冲突），还是确认维持删除。点头前两件事都算未定 |
| U-05 | gptGrok 图库 / 提示词 | gptGrok2api | 待决策 | `docs/design/frontend-integration-plan.md:99`（「图片图库/文件存储 不进界面」）；控制面无对应路由 | 需产品负责人点头：是自建图库与提示词管理面（等于新增存储、权限与页面三块），还是维持现有「不进界面」口径。这是范围取舍，不是技术阻塞 |
| U-06 | sub2api iframe 嵌入 | sub2api | 不做（已合并承载，非缺口） | 配额域已 1:1 落在 `/quota` 三个 tab（`apps/web/app/(main)/quota/page.tsx:57,407-423`，页头入口 `:78-82,401`）与 `/quota/policies`（`apps/control/app/routers/quota.py:193,244,308` 策略 CRUD、`:356,390,473-566` 状态与告警处置审计；页面 `apps/web/app/(main)/quota/policies/page.tsx:373-384,512`）；后端只有分组 / 账号 / 流水三类路由（`apps/control/app/routers/sub2api.py:57-150`；表结构 `apps/control/app/adapters/sub2api_store.py:3`）；上游其余栏目属 `docs/PROVIDERS.md:249` 取舍 #2 的排除项，`routers/` 无对应路由；本仓安全头禁止自身被嵌入（`apps/control/app/main.py:213,217`、`deploy/nginx.conf:254`） | 无（iframe 方案已由等价页面替代；上游栏目清单是文档级证据 `docs/design/frontend-integration-plan.md:181`，仓内无上游前端源码可核对） |
| N-01 | 一键启动脚本（Windows / Linux，状态文件与退出码分类） | 控制面自建 | 半截 | `scripts/start.ps1:24-25`（`.run/start.json` 路径）、`:36-41`（`Fail` 统一退出码）、`:189-194`（Python 缺失 / 版本低 = 3）、`:227`（前端产物缺失 = 2）、`:298`（迁移失败 = 5）、`:310-314`（端口全占 = 4）、`:331`（uvicorn 启动，无 `--reload`）、`:339,345,347`（端口抢占 / 库锁 / 健康超时 = 6）、`:364-367`（前端启动失败 = 7）、`:441`（状态文件写入）；`scripts/start.sh:13`（`start.json`）、`:37-40`（`fail`）、`:61-63`（=3）、`:193-194`（=2）、`:200`（=3）、`:219,236`（=5）、`:242-244`（=4）、`:269-275`（=6）、`:291-292`（=7）；`scripts/serve-web.mjs:19`（`/api`、`/v1` 等反代前缀）；`deploy/README.md:184-186`（首轮实跑记录） | 缺自动化用例：仓内无脚本级测试，`scripts/e2e/run-smoke.ps1` 也不调用 `start.ps1` / `start.sh`，只有 `deploy/README.md` 的首轮手工实跑记录。退出码 1-7 两端一致，功能链路完整，但按 §1 不得按已实现验收 |
| N-02 | 首次运行向导（六步、可跳过、库内完成标志、重置入口） | 控制面自建 | 已实现 | `apps/control/app/routers/onboarding.py:52`（读，`current_user`）、`:61`（写）、`:95`（重置，写与重置均 `admin_user`）；`apps/control/app/main.py:39,324-325`（路由注册）；`apps/control/tests/test_onboarding.py:19-97`（默认未完成、合并去重、步数收敛、跳过清理、重置重开、匿名拒绝、viewer 写 403）；`apps/web/lib/onboarding.ts`；`apps/web/components/common/onboarding/{OnboardingWizard,OnboardingTodo}.tsx`；`apps/web/app/(main)/layout.tsx:44`（自动打开仅管理员）、`:96`（非管理员不挂载组件）；`apps/web/app/(main)/settings/page.tsx:2029-2059`（`isAdmin` 内「重新运行向导」）；`apps/web/app/(main)/keys/page.tsx:345-349`（「向导创建」徽标）；五语同键（`apps/web/lib/i18n/locales/{zh-CN,zh-TW,en,ja,ko}.json`，键 `onboarding.*` / `keys.badgeFromWizard` / `settings.onboardingRerunTitle`） | 无（已入库：`65d71a0`；`onboarding.py`、`test_onboarding.py`、两个向导组件与 `lib/onboarding.ts` 均随该提交进库） |
| N-03 | 配置与账号打包导入导出（merge / overwrite / dry-run、事务回滚、默认不含明文） | 控制面自建 | 已实现 | 三个端点均 `admin_user`：导出 `apps/control/app/routers/config_transfer.py:38`、预检 `:63`、写入 `:83`（`:85-88` 拒绝 dry-run 与缺 `confirm`）；打包默认脱敏 `apps/control/app/config_bundle.py:69`（`:108,130,148` 仅 `include_secrets` 才带 `api_key` / `secret` / `meta_json`，`:155-156` 写 `contains_secrets` 与警告）、校验 `:177`、差异 `:327`（create / overwrite / skip / fail 逐条）、单事务写入 `:445`（`:481` `BEGIN IMMEDIATE`、`:617-619` 任一步失败 `rollback`）；路由注册 `apps/control/app/main.py:34,324`；CLI `scripts/config_export.py:35-50`、`scripts/config_import.py:67-105`；前端 `apps/web/components/common/settings/ConfigTransferPanel.tsx:72-153`（下载 / 选文件即预检 / merge·overwrite 切换 / 确认后写入），挂在 `apps/web/app/(main)/settings/page.tsx:2022`（`isAdmin` 内），客户端 `apps/web/lib/api.ts:715-725`；测试 `apps/control/tests/test_config_transfer.py:78`（默认脱敏）、`:116`（dry-run 前后行数不变）、`:128`（坏字段整批拒绝、0 行落库）、`:145`（中途失败回滚）、`:161`（不带 secret 的覆盖不清空库里密钥）、`:174,200`（HTTP 端点） | 无。已入库：后端与 CLI 与测试随 `77a0faf`，前端 `ConfigTransferPanel.tsx` 随 `d397290`（`config_bundle.py`、`routers/config_transfer.py`、`scripts/config_{export,import}.py`、`test_config_transfer.py` 均在该提交内）。本轮实跑该测试文件：8 passed（端口 8061 被占时 8 条全 skip，改 `UNIFIED_TEST_CTRL_PORT=8098` / `UNIFIED_TEST_FAST_PORT=8099` 后通过） |
| N-05 | 移动端适配（表格横向提示 / 导入弹窗高度 / 150% 缩放胶囊换行） | 控制面自建 | 半截 | `apps/web/components/ui/table.tsx:55`（`common.tableScrollHint`）；五语键 `apps/web/lib/i18n/locales/{zh-CN,zh-TW,en,ja,ko}.json`（`common.tableScrollHint`）；`apps/web/components/animate-ui/radix/dialog.tsx:197`（`max-h-[calc(100dvh-1rem)]` + `overflow-y-auto`）；`apps/web/app/(main)/accounts/page.tsx:1294,1321`（筛选条 `flex-wrap` + 胶囊 `whitespace-normal`） | 缺视觉回归用例，且本轮只读代码、未在 150% 缩放实机复核；`apps/web` 无截图基线或 e2e 用例覆盖这三处 |
| N-06 | sidecar 存活五分类（alive / auth / timeout / refused / http_error） | sub2api / 四家 sidecar | 已实现 | `apps/control/app/services/sidecar_status.py:38`（状态枚举注释）、`:124-201`（五类探测分类）、`:214-231`（贴到 `/api/providers`，非 alive 转 `needs_config` 并带原因）、`:234-257`（并发刷新 + TTL）；`apps/control/app/routers/providers.py:29`（`GET /api/providers/sidecars`）；`apps/control/tests/test_sidecar_link.py:132,145,157,165,173`（五态逐一断言）、`:347-357`；前端 `ProvidersSection.tsx:532-542`（alive 徽标）与 `:544-550`（异常原因） | 无（路由与测试在 7d57aed 入库） |
| N-07 | Grok 注册执行器协议 v1 | gptGrok2api / grok-register | 已实现 | `apps/control/app/services/grok_register.py:4-5,24`（协议常量 `grok-register-executor/v1`）、`:339-351`（执行与落库）；`apps/control/scripts/grok_executor_stub.py:3-12,126,149,164-165`（提交 / 轮询 / 取消）；`apps/control/app/routers/grok.py:26-84`（任务创建 / 列表 / 详情 / 取消 / 重试 / worker，全 `admin_user`）；`apps/web/components/common/settings/GrokRegisterTasks.tsx:101-221`；`apps/control/tests/test_grok_register.py:73-178`（完成落账号、执行器不可达、失败重试、超时区分、坏响应、重启恢复孤儿任务、取消）；`docs/providers/grok-register.md:7` | 无（协议与 stub 在 620408e 入库；任务面只对管理员开放，符合设计） |
| N-08 | 新增 / 变更端点：`POST /providers/{id}/token/validate`、`GET /api/providers/sidecars`、`POST /api/onboarding/reset` | 控制面自建 | 已实现 | token/validate：`apps/control/app/routers/providers.py:89-127`（admin 依赖、写审计、错误分类），审计动作 `providers.token.validate` 见 `tests/test_sub2api_codar_gaps.py:231-237`；前端 `apps/web/lib/api.ts:655-658`、`ProvidersSection.tsx:393`，测试 `apps/control/tests/test_sub2api_codar_gaps.py:211-246`（含 viewer 403）；sidecars：`providers.py:29-43`，前端无独立按钮，但同一 `sidecar_status` 已由 `/api/providers` 贴到列表并在 `ProvidersSection.tsx:532-550` 展示；onboarding/reset：`onboarding.py:95-112`，前端 `settings/page.tsx:2047-2052`、`lib/api.ts:319`，测试 `test_onboarding.py:63-78` | 无（三个端点均已在库：`token/validate` 与 `sidecars` 见既有提交，`onboarding/reset` 随 `65d71a0` 进库） |
| N-09 | 凭据失效后引导重新粘贴 | 控制面自建（全部 provider） | 已实现 | 后端四态判定 `apps/control/app/services/credential_state.py:17-21`、`:96-140`（`classify`；`:129-135` 重新粘贴后旧证据作废）；只读接口贴状态并改写 `requires` `apps/control/app/registry.py:374-399`；可判程度 `:431-440`（探活自报 401/403 的适配器：`apps/control/app/adapters/codebuddy.py:99`、`sidecar_link.py:24`），其余靠真实请求留痕 / JWT 过期 / 明确写「判不出」；适配器文案 `apps/control/app/adapters/codar.py:53,108,200-202`、`qoder.py:621-622`；用例 `apps/control/tests/test_credential_state.py:47-351`（24 条，含 `list_public` 失效需重贴、重贴后复位、探活不回 401 时不误判）；前端①服务商页失效徽标 + 「重新粘贴」按钮 `apps/web/components/common/settings/ProvidersSection.tsx:579-599`，动作 `:500-512`（展开该行、聚焦密文输入框，管理员外的按钮禁用 `:592`），跨页 hash 直达 `:521-532`；②导入弹窗顶部红条 + 「去服务商页重贴」`apps/web/components/common/accounts/ImportAccountDialog.tsx:145-159`；③账号页把状态透传给弹窗 `apps/web/app/(main)/accounts/page.tsx:170-171`；类型 `apps/web/lib/providers.ts:134-140`；四个文案键在五个语言包里各出现一次（`providers.credentialInvalidated`、`providers.repasteCredential`、`accounts.importCredentialInvalidated`、`accounts.repasteProvider`） | 无（已入库：`60122e4`；`credential_state.py` 与 `test_credential_state.py` 为新增文件，库内字段改动与前端改动一并随该提交进库）。残留（不阻塞主链路）：`credential_state_note`（探活不回传 401/403 时为何只能给「未知」）只在接口与类型里（`apps/web/lib/providers.ts:140`、`apps/web/components/common/accounts/account-ops.ts:38`），页面未渲染；前端无自动化用例，与 N-02 同口径 |
| N-10 | `/api/onboarding` 94 次 404（结案） | 控制面自建 | 不做（非缺口） | 路由已注册：`apps/control/app/main.py:39,324-325`；用例覆盖：`apps/control/tests/test_onboarding.py:19-97`；启动脚本不带热重载：`scripts/start.ps1:331`（`uvicorn ... app.main:app`，无 `--reload`） | 已结案：预览后端跑的是 8240 旧进程（启动早于路由文件写入），重启即恢复，不是代码缺陷。94 次这个计数属会话观测，仓内无法复核 |
| N-11 | 四家 sidecar 上游许可证（结案） | trae2api / Deepseek2Go / qoder-switch / codar2api | 不做（非缺口） | `THIRD-PARTY-NOTICES.md:30-33`（汇总表）、`:74-77`（trae2api = MIT）、`:79-82`（Deepseek2Go 无 LICENSE，按 All Rights Reserved）、`:84-87`（qoder-switch = PolyForm-Noncommercial-1.0.0）、`:89-92`（codar2api = MIT） | 已登记，非缺口；商用边界按表中 safe / warn / danger 执行 |

## 3. 逐项说明

### 3.1 已实现（E-01 ~ E-13）

**E-01 账号批量动作。** 路由 `accounts.py:640` 支持按文件名列表执行批量动作，测试覆盖了部分失败时继续执行、逐条返回错误、凭据回收。前端账号页有批量选择入口。缺的不是能力，是入库。

**（2026-11-05 已更正）** 末句「缺的不是能力，是入库」已过期：批量路由在 `81bb7d8`、测试在 `ca7959b`、前端批量条在 `adf22d8` 入库（`accounts.py` 文件基线为 `b5772fb`），工作树对该组无未提交改动。§3.1 其余条目同理，均已随分组提交入库，不再需要「等入库」这一步。
**误判后果**：按「只差入库」读，会把已落地功能挂在待办上等一次并不存在的提交动作，或在盘点时再算一遍「未入库工作量」。

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

### 3.2 半截（P-01、P-03）与已结案（P-02）

**P-01 provider 定时健康检测。** 后端是完整的：`ProviderHealthService` 有启停、定时 tick、单次触发、历史写入与裁剪、快照读取，`/api/providers/health` 和 `/api/providers/health/run` 都能用，`main.py` 挂了生命周期，`tests/test_provider_health.py:123-235` 覆盖了关闭态不动作、开启态执行、三档结果落库、错误摘要脱敏、只写历史表不改 `enabled`、快照、admin-only、默认关闭。唯一的缺口是前端：没有任何入口能看历史或手动触发。另外默认 `UNIFIED_HEALTH_CHECK_ENABLED=false`，要常开等于按间隔持续打上游，这需要产品与成本判断。

**P-02 `/quota/policies`。** 页面与 API 都可用，`/quota` 页顶部有入口。缺的是基线登记：`FRONTEND-BASELINE.md:67` 的页面清单只有 `/quota`，§3 对「新增页面」的约束以 §4 现状备注为准，这个子页没出现在任何一处。这不是代码缺口，PR 审查时会被当作未登记入口问一次。

**（2026-11-05 已更正）** 原句唯一的阻塞点「基线未登记」已在同一天解除：`FRONTEND-BASELINE.md:74` 的 §4 现状备注已按 `/quota` 二级子页登记（不是导航项、不新增入口数量）。复核实现后也没有别的代码缺口——策略 CRUD 带阈值 1-100 校验（`quota.py:193,244,308`、`:236-241`），周期用量与告警列表（`:356,390`），告警确认 / 静默 / 关闭 / 重开且写审计（`:473-566`）；前端同一页可建、改、删并处置告警（`apps/web/app/(main)/quota/policies/page.tsx:378,449-533,771-830`）；测试覆盖 `tests/test_quota.py:526,559-600` 与 `tests/test_ops_triage.py:192-229`。档位改为「已实现」，阻塞原因改为「2026-11-05 基线登记完成，阻塞原因解除」。
**误判后果**：按「半截（基线未登记）」排期，会把已经可用的页面压在待办里不做验收，或在 PR 审查时把 `/quota` 页头的既有入口当成越界新增入口再问一次；更糟的是把「半截」当成有代码缺口，去补一个并不存在的能力。

**P-03 sub2api Ops 排障面。** 我们有自己的等价观测（`/metrics`、告警、日志、导出），但没有上游 Ops 的那几块：告警规则 CRUD（只做了配额阈值）、告警事件确认/静默/关闭、失败请求重试与「标记已解决」、系统日志采集、并发与实时流量、网关运行时状态。`routers/` 全目录没有对应路由。补齐前要先定：是自建排障台，还是明确不做、把这块写进 `PROVIDERS.md` 的取舍表。

**（2026-11-05 已更正）** 原句的缺口清单有一半已过期。复核实现：告警事件确认 / 静默 / 关闭 / 重开已落地并写审计（`quota.py:473-566`，状态机测试 `tests/test_ops_triage.py:192-229`）；失败请求「标记已解决」已落地（`logs.py:425,448`，落 `request_logs.resolved_at` / `resolved_by`，审计 `logs.py:398-443`）。仍真缺两项：①系统日志采集——仓内只有请求日志（`logs.py:270,314`），`system.py` 只有 `:77` changelog、`:95` versions，没有进程 / 上游日志的采集路由，现有日志页承担不了；②实时流量——没有推送或聚合视图，只有指标快照（`health.py:419,439`），现有页面可用轮询承担一部分，但不是实时流量面。待复核两项：③运行时状态——`health.py:117,122` 只有存活 / 就绪，`settings.py:425` 是运行时配置，缺的是「并发 / 在途 / 调度器」到底看哪些指标、验收标准是什么；④告警规则管理面——规则参数现在只在 `config.py:132-140` 与 env 里可配（`alerting.py:202-234` 按配置评估三条规则），没有 DB CRUD（配额阈值走 `/quota/policies`），是否要管理面属产品取舍。`routers/` 全目录无路由这句只对①②成立，对③④不成立。
**误判后果**：按旧句排期会重做已经上线的告警状态机与失败请求标记；也会把「全目录无对应路由」当成所有子项都缺，看不出「部分已实现、部分真缺、部分只是没定口径」，验收时无法按子项逐条关掉。

### 3.3 已实现、半截、待决策与不做（U-01 ~ U-06）

**U-01 在线更新。** 后端明确「本部署不提供在线更新」，`check-update` 与 `update-status` 共用同一份 `available=False` 摘要。前端 `UpdatePanel` 仍保留了 `manager.has_update` / `upstream.has_update` 分支，服务端永远不会返回 true，属死代码。要补齐，需要先有发布通道、包签名与回滚方案。

> **（2026-11-05 已更正）** `GET /api/system/check-update` 已于 2026-11-05 下线（`apps/control/app/routers/system.py` 不再注册该路由），请求它现在直接 404，前端对它的调用已是零引用；旧投影逻辑只存在于并行会话的未提交工作态，入库前已随端点一并去掉，仓库任何一次提交里都没有 `check_update_legacy`，本块初稿的「旧投影一并删除」即指这件事，不是 `update.py` 里的删除动作。`manager.has_update` / `upstream.has_update` 是旧壳独有的字段，随端点一起消失，`/api/system/update-status` 只回 `available` / `can_apply` / `latest_version` 等新契约字段。在线更新是否可用改由 `UNIFIED_UPDATE_ENABLED` 与部署能力决定，不再是恒 `false` 的固定摘要。
>
> **读旧响应会得到的误判**：把 `check-update` 当成仍在服务的「恒无更新」接口，会对着 404 判断「更新通道只是没开」；继续按 `manager.has_update` / `upstream.has_update` 找前端死代码，会去读一个已经被删掉的字段，而真正需要看的是 `available && can_apply && latest_version > current_version` 这条判断。另：若按早先那句「旧投影 `check_update_legacy` 一并删除」去 `git log` 找这次删除，会一无所获，进而怀疑文档在编造改动——正确事实是它从未入库，只存在于未提交的工作态。

**（2026-11-05 已更正）** 本段正文的「本部署不提供在线更新」「共用同一份 `available=False` 摘要」「`has_update` 是死代码」三句均已过期；上面那块只更正了 `check-update` 下线，没有改档位。复核实现：`routers/update.py:57,62,75,106` 提供 status / check / apply / rollback，apply 要求 `confirm_version` 与 latest 逐字一致且 `available`、`can_apply` 双闸（`:83-91`）；能力探测在 `services/update_feed.py:78-120`，换码与重启在 `update_executor.py:4-5`，签名、清单、自举分别是 `update_signing.py`、`update_manifest.py`、`update_boot.py`；前端 `UpdatePanel.tsx:42` 挂在 `settings/page.tsx:2016`，按 `updater_found` / `can_apply` 分别提示（`UpdatePanel.tsx:295-300`）；测试 `test_update_manifest.py:68-162`、`test_update_feed.py:125-193`、`test_update_executor.py:117-295`。档位改为「已实现」。
**两个前置条件（不是缺口）**：①默认关闭 `UNIFIED_UPDATE_ENABLED=false`（`config.py:149,222`），要运营显式开启；②要自备发布源 `UNIFIED_UPDATE_MANIFEST_URL` 与签名密钥（`config.py:150-151,224-225`）。容器里还得把代码目录绑挂进来并配 `restart: unless-stopped`，否则探测直接给 `can_apply=false` 并带原因（`update_feed.py:101-108`），这是部署前提，不是代码没写完。
**误判后果**：按「未实现」排期会重写一套已经可用、带签名校验与回滚的执行链；反过来，以为打开开关就能用、忽略发布源与绑挂，上线后只会看到 `can_apply=false`，还查不到该找运营还是找运维。

**U-02 token 估算计费。** 现在只把「上游返回全零」和「根本没有 usage」分成 `usage_zero` / `usage_missing` 两种标注，不做估算。`m3-billing-plan.md:161` 写的 `billing.charge_on_missing_usage` 开关在代码里不存在，文档与实现有漂移。要补齐，前提是财务确认要收这条通道的钱，并先给 muse 别名补价格行——否则估出来的 token 套不到价，仍是 0 元。

**（2026-11-05 已更正）** 原句把「等财务确认」写成了「有意不做」，这两个状态不能同时成立。代码侧确实没有估算（`billing.py:320-323` 只打 `usage_zero` / `usage_missing` 标），但补不补卡在拍板，不是已经决定不做。档位改为「待决策」，点头对象与内容写死：财务负责人确认「估算出的 token 是否计入额度与账单、误差是写进账单还是只做提示」；点头前必须先给 muse 别名补价格行（`docs/muse-usage-policy.md:43` 的 ±15–30% 只描述可见文本误差）。
**误判后果**：按「有意不做（未实现）」归档会把它从决策队列里摘掉，财务永远等不到这个议题；反过来按「未实现」排进工程工期，会先写估算逻辑，最后因为没定价而算不出钱。

**U-03 备份远端与定时。** `BACKUP.md:174` 明说不加密、不上传远端。所以这是有意不做的取舍，不是漏做；真要补，前置是对象存储凭据管理、失败重试、保留策略。

**（2026-11-05 已更正）** 旧段写于实现之前，已过期，但**不是全做完**。已落地：配置读取 / 保存、远端列表、手动上传四个端点 `routers/backup_remote.py:66,72,90,112`；加密上传把本地归档写成 `<prefix>/<归档名>.enc`，密文 sha256 再写成 `.zip.enc.sha256`，上传后 HEAD 校验大小与 sha256（`services/backup_remote.py:96-97,392-413`），加密为 AES-256-GCM（`services/backup_remote.py:473-478`、`services/backup_crypto.py:223-274`），密钥只从 `UNIFIED_BACKUP_ENCRYPTION_KEY` 读、缺失时 `encryption_key()` 直接抛错并拒绝上传（`backup_remote.py:264-280`）；按 keep 轮转并连 sidecar 一起删（`:508-527`）；失败按 30s / 2m / 10m 重试，403 标 `auth_blocked` 且不泄露密钥。定时计划与 `provider_health` 同构：60 秒一 tick，开关默认关闭时 `due` 短路、零外呼（`services/backup_schedule.py:31-43,56-90`）；四个开关 `config.py:183-186,262-269`；lifespan 启停 `main.py:161-162,175,182`。S3 客户端 `services/s3_client.py:278-299`（SigV4、path-style、重试）。
**为什么算半截而不是已实现**：远端恢复没有做。`routers/backup.py:177-193` 的 `restore` 仍然只用 `RestoreIn.name` 拼本地备份目录，既不下载远端对象、也不解密；`s3_client.get_object`（`s3_client.py:283`）与 `backup_crypto.decrypt_bytes`（`backup_crypto.py:276`）只有测试调用，生产链路没有接。备份的价值在恢复——只能上传、不能从远端恢复，磁盘全毁时远端那份密文取不回来，等于没有备份。另外前端没有远端备份入口（`apps/web` 搜不到 `backup-remote` 引用），按 §1 只有路由没有前端入口也不算已实现。档位改为「半截（远端上传与定时已实现，缺远端恢复）」。
**如实标注**：本轮证据文件都在工作区、尚未提交——`?? apps/control/app/routers/backup_remote.py`、`?? apps/control/app/services/{backup_remote,backup_schedule,s3_client,backup_crypto}.py`、`?? apps/control/tests/{test_backup_remote,test_backup_schedule,test_s3_sigv4}.py`、`?? apps/control/tests/fake_s3_server.py`、`M apps/control/app/config.py`、`M apps/control/app/main.py`，不是已入库。
**误判后果**：按旧句「有意不做」归档，会以为这块从没动工，重复写加密上传与调度；反过来按「已实现」验收，会漏掉远端恢复，真出事时才发现远端只有一堆取不回来的密文。

**（2026-11-05 已更正，本批入库后）** 上面三段里的「缺远端恢复」「前端无入口」「尚未提交」三条全部过期，逐条给证据：远端恢复是 `routers/backup_remote.py:166`（`POST /backups/restore-remote`，`:181-187` 先校验归档名白名单、再要求 `confirm` 与 `name` 逐字相同，否则 400 且不动活库），链路上 `services/backup_remote.py:397`（`download_archive`：下载 `.zip.enc` → 校验 `.enc.sha256` → `backup_crypto` 解密落名）、`:475`（`commit_download`），随后调用 `routers/backup.py:182` 抽出的 `restore_archive()`；本地 `/restore`（`routers/backup.py:250`）改为持锁后调用同一段，对外行为不变。远端删除是 `routers/backup_remote.py:222` → `services/backup_remote.py:489`，连 `.enc.sha256` sidecar 一起删；列表在 `:114` 增加 `has_sidecar` / `s3_reachable` / `credential_source` / `crypto_backend`。前端入口是 `apps/web/components/common/settings/BackupRemoteSection.tsx`（由 `BackupPanel.tsx` 渲染），凭据缺失时点名三个 `UNIFIED_BACKUP_S3_*` 变量，探活 502 时保留配置区可编辑。这些文件已入库：加密后端 4d7e54a、S3 客户端 9ebff30、路由与调度 500e671、前端面板 622fa9f。档位改为「已实现」。
**误判后果**：继续按「半截」读，会重复实现一套下载 / 解密链，或者以为磁盘全毁时只能手工救；反过来把「已实现」当验收通过而跳过配置——默认关闭，凭据只从环境变量取，不配 `UNIFIED_BACKUP_S3_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` 时 `configured=false`，手动上传返回 409（`routers/backup_remote.py:157-158`），不会「看起来能点但什么也没发生」。

**U-04 `/tasks`。** 页面、9 条幽灵路径已按业务决定整体删除，基线也不再保留。要恢复必须推翻那条决定，且与 §3「不新增页面」冲突。

**（2026-11-05 已更正）** 原句的「已按业务决定整体删除」只描述了删的动作，没写清这是一次可逆的产品决策；档位「未实现（有意删除）」和阻塞原因「需产品决策」也因此互相取消。档位改为「待决策」，点头对象与内容写死：产品负责人确认「是否恢复已删的 `/tasks` 页面（接受推翻基线删页决定、新增页面并触碰 §3 约束），还是确认维持删除」。
**误判后果**：按「未实现（有意删除）」归档会让人以为删除已经定案，后续任务页需求只能重新争一遍；反过来按「未实现」排期，会在没点头前先去恢复页面，直接撞上 §3 的不新增页面约束。

**U-05 gptGrok 图库 / 提示词。** 设计口径明确「图片图库/文件存储不进界面」（`frontend-integration-plan.md:99`），控制面也没有对应路由。要补，得先有图库路由与存储、权限模型，等于新做一块能力，而不只是加个页面。

**（2026-11-05 已更正）** 原句写「有意不做」，但同一段又要求产品决定「是否自建」，两句话互相取消。现状是设计口径写着「不进界面」（`frontend-integration-plan.md:99`）、控制面确无路由，但要不要翻这个口径没有定论。档位改为「待决策」，点头对象与内容写死：产品负责人确认「是否自建图库与提示词管理面，并接受新增存储、权限、页面三块成本」；不点头就维持「不进界面」。
**误判后果**：按「有意不做（未实现）」归档，会让人以为口径已经闭合，后续再提图库需求只能重新考古；反过来按「未实现」排期，会在产品没点头前先动存储与权限模型。

**U-06 sub2api iframe。** 结案为「不做（已合并承载，非缺口）」。iframe 原本要承载的配额域已 1:1 落在 `/quota` 的分组 / 账号池 / 流水三个 tab（`apps/web/app/(main)/quota/page.tsx:57,407-423`），页头另有 `/quota/policies`（配额策略与告警）入口（`:78-82,401`）；后者有确认弹窗（`apps/web/app/(main)/quota/policies/page.tsx:512`）、错误回显（`:165-233` 的 `setPoliciesError` / `setStatusError` / `setAlertsError`）与操作审计（`apps/control/app/routers/quota.py:295,319,467-470`），比只读 iframe 更完整。上游其余栏目（Channels / Proxies / Subscriptions / Orders / PromoCodes / Redeem / Affiliates / Announcements / Plugins）属取舍 #2 的排除项（`docs/PROVIDERS.md:249`），后端从未建对应路由：`apps/control/app/routers/sub2api.py:57-150` 只有 groups / accounts / ledger，`apps/control/app/adapters/sub2api_store.py:3` 的表结构也只这三张。此外本仓安全头明确禁止自身被嵌入：`apps/control/app/main.py:213`（`X-Frame-Options: DENY`）、`:217`（CSP `frame-ancestors 'none'`）、`deploy/nginx.conf:254`。上游栏目清单是文档级证据（`docs/design/frontend-integration-plan.md:181`），仓内没有上游前端源码可核对。

### 3.4 近期改动复核（N-01 ~ N-11，2026-11-06）

**判定边界。** 本节只写本轮能在工作区看到的东西。N-03 / N-09 此前被记为「仓内没有可核对的路由 / 组件」的并行会话半成品；2026-11-06 第三次复核已逐文件读到实现与前端入口，对应段落与总表档位均已据实改写。以下所有「测试存在」均指测试文件与断言存在；只有 N-03 本轮实跑了 `test_config_transfer.py`（8 passed），其余条目没有执行 pytest、没有启动服务、没有做浏览器实测。

**N-01 一键启动。** `start.ps1` 与 `start.sh` 都实现了：写 `.run/start.json`、按 pid 停止上一实例、端口段扫描、控制面健康检查、可选静态前端、失败分 7 类退出码。`serve-web.mjs` 负责 `/api`、`/v1` 等前缀反代与静态文件。`deploy/README.md:184-186` 记录首轮实跑通过。缺的是自动化：没有脚本级测试，`scripts/e2e/run-smoke.ps1` 也不走这两个入口。按 §1 归「半截（缺测试）」，不按已实现验收。

**N-02 首次向导。** 后端三端点 + 8 条用例，前端向导与待办条只在管理员下挂载（`layout.tsx:44,96`），设置页重置入口在 `isAdmin` 分支内，密钥页有「向导创建」徽标。五语键同键同序。没有前端自动化用例，这一点与 E 系前端条目相同，不影响「已实现」判定。相关文件随 `65d71a0` 入库。

**N-03 打包导入导出。** 本轮逐文件核对后升为「已实现」，依据均能指到行：后端三端点 `routers/config_transfer.py:38,63,83`（全 `admin_user`，`:85-88` 拒绝 dry-run 直写与缺 `confirm`）；导出默认不含明文 `config_bundle.py:69`（`:108` 的 `api_key`、`:130` 的 `secret`、`:148` 的 `meta_json` 均只在 `include_secrets=True` 时出现，`:155-156` 回 `contains_secrets` 与警告）；非法字段整批拒绝在 `validate_bundle:177` + `plan_import:327`，写入前 `apply_import:445` 检查 `fail` 并抛 `BundleRejected`（`:465-468`），单事务 `:481` `BEGIN IMMEDIATE`，异常 `:617-619` `rollback`；CLI 两个脚本各自接同一套函数（`scripts/config_export.py:35-50`、`scripts/config_import.py:67-105`）；前端 `ConfigTransferPanel.tsx:72-153` 负责下载 / 预检 / 模式切换 / 确认写入，`settings/page.tsx:2022` 在 `isAdmin` 内挂载，`lib/api.ts:715-725` 是客户端。用例 `test_config_transfer.py` 覆盖默认脱敏（`:78`）、dry-run 不写库（`:116`）、坏字段整批拒绝（`:128`）、中途失败回滚（`:145`）、不带 secret 的覆盖不清空密钥（`:161`）、HTTP 端点只读与拒绝（`:174,200`）。本轮实跑该文件：8 passed（端口 8061 被占时全部 skip，改用 8098/8099 后通过）。

**N-04 日志导出扩围（并入 E-04，档位不变）。** 后端去掉管理员前置，改为按 key 归属收窄；显式传他人 `key_id` 返回 403 而不是空集；成功、被拒、超限、异常四类都写 `logs.export` 审计。前端导出按钮不再按角色隐藏（`logs/page.tsx:296-306`）。有 viewer 用例，E-04 维持「已实现」，只更新证据与范围。

**N-05 移动端。** 三处都是 CSS 层改动：表格底部提示（`table.tsx:55`）、弹窗 `max-h` 与滚动（`dialog.tsx:197`）、账号页服务商筛选条与胶囊换行（`accounts/page.tsx:1294,1321`）。没有截图基线或视觉回归用例，本轮也没有实机放大到 150% 复核，归「半截（缺视觉回归用例）」。

**N-06 sidecar 五分类。** `_status` 对 401 → `auth`、超时 → `timeout`、连接拒绝 → `refused`、非 2xx → `http_error`、2xx → `alive`；`decorate_public` 把非 alive 转成 `needs_config` 并带原因，列表页据此展示。五态各有一条断言。已实现。

**N-07 Grok 协议 v1。** 协议常量、提交 / 轮询 / 取消、stub、任务路由、前端任务面板与 7 条用例齐全；stub 纯标准库，不依赖浏览器。已实现。

**N-08 三端点。** `token/validate` 有 admin 依赖、审计与 viewer 403 用例；`sidecars` 是快照端点，前端没有独立按钮，但同一状态已贴到 `/api/providers` 并在服务商列表展示，不算「只有路由没有入口」；`onboarding/reset` 有设置页入口与 2 条用例。已实现。

**N-09 凭据失效引导。** 本轮核实前端已接入，升为「已实现」。后端把四态与「需要重新粘贴」的 `requires` 贴到只读接口（`registry.py:374-399`），判定内核 `credential_state.py:96-140`，重新粘贴后旧证据作废（`:129-135`）；可判到什么程度由 `registry.py:431-440` 决定，codebuddy 与四家 sidecar 走探活自带 401/403（`adapters/codebuddy.py:99`、`adapters/sidecar_link.py:24`），其余按真实请求留痕 / JWT 过期 / 明确「判不出」。前端有三处入口：服务商页的失效徽标与「重新粘贴」按钮（`ProvidersSection.tsx:579-599`，动作在 `:500-512` 展开该行并聚焦密文框，非管理员禁用），账号页把 `credential_state` 透传给导入弹窗（`accounts/page.tsx:170-171`），弹窗顶部红条给出「去服务商页重贴」直达链接（`ImportAccountDialog.tsx:145-159` → `/settings#provider-<id>`，由 `ProvidersSection.tsx:521-532` 消费 hash）。四个文案键在五语各命中一次。用例 `test_credential_state.py` 共 24 条（原记「20 条」只数了同步用例，另有 4 条 async），覆盖失效需重贴、重贴后复位、探活不回 401 时不误判。残留不阻塞：`credential_state_note` 只在接口与类型里（`lib/providers.ts:140`、`accounts/account-ops.ts:38`），页面未渲染；前端无自动化用例。

**N-10 / N-11 结案。** 404 是旧进程未加载新路由（启动脚本无 `--reload`），许可证已在 `THIRD-PARTY-NOTICES.md` 登记；两条都不再算缺口，不再排期。

## 4. 旧结论更正留档

体例：原句逐字保留，紧跟一个更正块，块内写清误判后果。

### 4.1 前端「sub2api 完全缺失」

> sub2api 是控制面后端能力最完整但前端完全缺失的一块。
>
> —— `docs/design/frontend-integration-plan.md:126`

**（2026-11-05 已更正）** 这句话和它上面 118-123 行的「无 UI」表格已过期。`apps/web/app/(main)/quota/page.tsx:57` 已实现分组 / 账号 / 流水三个 tab，页面本身已入库；额度重置走账号 tab。实际仍缺的只有 Ops 排障面（见 P-03），不是「前端完全缺失」。
**误判后果**：按旧句排期会把已经做完的 CRUD 再排一遍，或误判 `quota` 页是越界新增；同时把真正缺的排障面当成「顺带就有了」。

### 4.2 gptGrok images / videos「控制面未接入」

> chat / responses / messages / images / videos | `/v1/*` | 走网关（chat/models 已接；images/videos 控制面未接入）
>
> —— `docs/design/frontend-integration-plan.md:96`

**（2026-11-05 已更正）** `routers/v1.py:844-869` 已挂 `/v1/images/generations`、`/v1/images/edits`、`/v1/images/tasks/{task_id}`、`/v1/videos`、`/v1/videos/{task_id}`、`/v1/media/{name}`，`/v1` 路由总数为 14 条（`v1.py:806-884`）。这几条尚在工作区未入库（`M apps/control/app/routers/v1.py`）。
**（2026-11-05 入库复核）** 上一块末尾「尚在工作区未入库」是写块当时的状态：`v1.py` 的路由改动已随 `65e88b9`（适配器协议面与客户端兼容端点）入库，工作树对该文件无未提交改动。结论（已接入、不再是缺口）不变，只有「未入库」这半句过期。
**误判后果**：会把已经写好的图片/视频转发当成缺口重做；更糟的是有人据此在 `PROTOCOL-MATRIX` 的「仍不支持」清单里留下错误条目，客户端按 501 处理而实际可用。

### 4.3 旧「11 项真缺失」清单

原清单没有落成仓内文档，只在会话里口口相传，无法逐字留档。按本文件的口径复核后：账号批量、令牌批量、日志导出、模型映射搬运、Antigravity/Sora 兼容面、备份恢复六类已经在工作区实现（多数尚未入库），不再算「完全没有」；qoder 会话已落地（E-13，受上游路径语义限制）。剩下仍成立的是 P-01 / P-03 / U-01 / U-02 / U-03 与前端三条（U-04 ~ U-06）。旧数字 11 已不可引用。

**（2026-11-05 更正）** 上一句的「前端三条（U-04 ~ U-06）」已过期：U-06 已结案为「不做（已合并承载，非缺口）」（见 §4.4），仍在前端缺口里的是两条（U-04、U-05），连同 P-01 / P-03 / U-02 / U-03（半截，缺远端恢复）。U-06 留在旧句里是结案前的遗留，不是新发现的缺口。

**（2026-11-05 已更正，本批入库后）** 上一块清单里的 U-03 已不成立：远端恢复、远端删除与前端入口都已落地并入库（500e671 / 622fa9f，见 §3.3 末块），U-03 档位改为「已实现」。故当前仍开的缺口是 P-01 / P-03 / U-02 与前端两条（U-04、U-05）。
**（2026-11-05 入库复核）** 上一句「多数尚未入库」同样过期：账号批量、令牌批量、日志导出、模型映射搬运、Antigravity/Sora 兼容面、备份恢复六类的路由、测试与前端均已入库，hash 见 §2 对应行。这里只剩「六类不再算完全没有」这一结论仍然成立。
**误判后果**：按这句做范围盘点，会把「iframe 嵌入」当成仍待决策的前端缺口带进下一轮排期，并掩盖它已被 `/quota` 三个 tab 与 `/quota/policies` 承载的事实；按「多数尚未入库」读，还会把六类已完成功能重新排进实现队列。

### 4.4 把 U-06 当作「未实现」缺口

> | U-06 | sub2api iframe 嵌入 | sub2api | 未实现（有意不做） | 全仓无 `iframe`（本轮 grep 无命中）；`PROVIDERS.md:257` 取舍 #10「只做协议层，不做各家上游 Web UI」 | 与全局设计边界冲突；LGPL 隔离 |
>
> **U-06 sub2api iframe。** 全仓没有 iframe。取舍 #10 明确「只做协议层，不做各家上游 Web UI」。嵌 iframe 等于把上游 Web 面板当产品界面，与这条边界直接冲突，也会把 LGPL 的边界问题带回来。
>
> —— 本文件 §2 总表与 §3.3，2026-11-05 判定时点

**（2026-11-05 已更正）** 旧行只回答了「iframe 做不做」，没回答「iframe 要承载的东西有没有别处承载」，于是把「不做」误归到「未实现」。实际已 1:1 承载：配额域在 `/quota` 三个 tab（`apps/web/app/(main)/quota/page.tsx:57,407-423`），策略与告警在 `/quota/policies`（`apps/control/app/routers/quota.py:193,244,308` 策略 CRUD、`:356,390,473-566` 状态与告警处置审计；页面 `apps/web/app/(main)/quota/policies/page.tsx:373-384,512`），带确认弹窗、错误回显与审计，强于只读 iframe。上游其余栏目（Channels / Proxies / Subscriptions / Orders / PromoCodes / Redeem / Affiliates / Announcements / Plugins）属取舍 #2 的排除项（`docs/PROVIDERS.md:249`），后端从未建对应路由（`apps/control/app/routers/sub2api.py:57-150`；`apps/control/app/adapters/sub2api_store.py:3`）。本仓安全头也禁止自身被嵌入（`apps/control/app/main.py:213,217`、`deploy/nginx.conf:254`）。档位改为「不做（已合并承载，非缺口）」。
**边界说明**：上游栏目清单只有文档级证据（`docs/design/frontend-integration-plan.md:181`），仓内没有上游前端源码可核对。这不影响结论——排除项由 `PROVIDERS.md:249` 的取舍 #2 锁定，不依赖栏目清单是否完整。
**误判后果**：按「未实现」排期会去重做已经存在的配额页（`/quota` 三个 tab 与 `/quota/policies`）；或者在发现配额页已存在后，转而去用 iframe 补回上游控制台——后者同时违反取舍 #2/#10 与本仓安全头（`X-Frame-Options: DENY` 与 `frame-ancestors 'none'` 让嵌入在本仓内根本不可用）。这条是旧结论的遗留，不是 2026-11-05 新发现的缺口。

### 4.5 P-02「半截（基线未登记）」

> | P-02 | `/quota/policies` 页面 | sub2api | 半截（基线未登记） | `apps/control/app/routers/quota.py:174-285`；`apps/web/app/(main)/quota/policies/page.tsx`；`FRONTEND-BASELINE.md:67` 页面清单只登记 `/quota` | 需基线登记或产品确认，不是代码缺口 |
>
> **P-02 `/quota/policies`。** 页面与 API 都可用，`/quota` 页顶部有入口。缺的是基线登记：`FRONTEND-BASELINE.md:67` 的页面清单只有 `/quota`，§3 对「新增页面」的约束以 §4 现状备注为准，这个子页没出现在任何一处。这不是代码缺口，PR 审查时会被当作未登记入口问一次。
>
> —— 本文件 §2 总表与 §3.2，2026-11-05 判定时点

**（2026-11-05 已更正）** 原句唯一指出的阻塞点「基线未登记」在当天解除：`FRONTEND-BASELINE.md:74` 的 §4 现状备注已按 `/quota` 二级子页登记，不是导航项、不新增入口数量。复核实现后也没有别的代码缺口：策略 CRUD 带阈值 1-100 校验（`apps/control/app/routers/quota.py:193,244,308`、`:236-241`），周期用量与告警列表（`:356,390`），告警确认 / 静默 / 关闭 / 重开且写审计（`:473-566`）；前端同一页可建、改、删并处置告警（`apps/web/app/(main)/quota/policies/page.tsx:378,449-533,771-830`）；测试覆盖 `tests/test_quota.py:526,559-600` 与 `tests/test_ops_triage.py:192-229`。档位改为「已实现」，阻塞原因写「2026-11-05 基线登记完成，阻塞原因解除」。
**误判后果**：按「半截（基线未登记）」排期会把已经可用的页面压在待办里不做验收，或在 PR 审查时把 `/quota` 页头的既有入口当成越界新增入口再问一次；更糟的是把「半截」当成有代码缺口，去补一个并不存在的能力。

### 4.6 U-01「未实现」

> | U-01 | 在线更新执行 | codebuddy2api（系统更新页）、sub2api | 未实现 | `apps/control/app/routers/system.py:1-6`（模块说明）+ `:87-89`（`update_summary` 恒返回 `available=False`）+ `:147-160`（`/update-status` 固定 `running=False`、`updater_found=False`）；`UpdatePanel.tsx:61-62` 的 `has_update` 分支是死代码 | 无分发通道与签名校验方案，需设计决策 |
>
> **U-01 在线更新。** 后端明确「本部署不提供在线更新」，`check-update` 与 `update-status` 共用同一份 `available=False` 摘要。前端 `UpdatePanel` 仍保留了 `manager.has_update` / `upstream.has_update` 分支，服务端永远不会返回 true，属死代码。要补齐，需要先有发布通道、包签名与回滚方案。
>
> —— 本文件 §2 总表与 §3.3，2026-11-05 判定时点

**（2026-11-05 已更正）** 旧行与旧段停留在「恒 `available=False`」的旧壳实现上，而端点、执行链与前端面板早已换新：`routers/update.py:57,62,75,106` 提供 status / check / apply / rollback（apply 带版本逐字确认与 `available`/`can_apply` 双闸，`:83-91`）；能力探测 `services/update_feed.py:78-120`，换码与重启 `update_executor.py:4-5`，签名 / 清单 / 自举分别为 `update_signing.py`、`update_manifest.py`、`update_boot.py`；前端 `UpdatePanel.tsx:42` 挂在 `settings/page.tsx:2016`；测试 `test_update_{manifest,feed,executor}.py` 覆盖验签、拒降级、zip slip、超时、回滚与 watchdog。档位改为「已实现」。
**两个前置条件（不是缺口）**：默认 `UNIFIED_UPDATE_ENABLED=false`（`config.py:149,222`）；需自备发布源与签名密钥（`config.py:150-151,224-225`）；容器部署还需代码目录绑挂 + `restart: unless-stopped`，否则 `can_apply=false`（`update_feed.py:101-108`）。
**误判后果**：按旧行排期会重写一套已可用的签名更新链；或以为打开开关即可，忽略发布源与绑挂，上线后只看到 `can_apply=false` 却查不到原因归属。

### 4.7 P-03「缺口清单全缺」

> | P-03 | sub2api Ops 排障面 | sub2api | 半截 | 已有等价观测：`health.py:410`、`alerting.py`、`logs.py:260`；缺的能力在 `routers/` 全目录无对应路由（本轮 grep 无命中） | 需产品决策是否自建排障台；上游 LGPL 隔离（`upstreams/upstreams.yaml:87,94`） |
>
> **P-03 sub2api Ops 排障面。** 我们有自己的等价观测（`/metrics`、告警、日志、导出），但没有上游 Ops 的那几块：告警规则 CRUD（只做了配额阈值）、告警事件确认/静默/关闭、失败请求重试与「标记已解决」、系统日志采集、并发与实时流量、网关运行时状态。`routers/` 全目录没有对应路由。补齐前要先定：是自建排障台，还是明确不做、把这块写进 `PROVIDERS.md` 的取舍表。
>
> —— 本文件 §2 总表与 §3.2，2026-11-05 判定时点

**（2026-11-05 已更正）** 该清单把已实现项与真缺项混在一起。复核后：告警确认 / 静默 / 关闭 / 重开已实现（`quota.py:473-566`，测试 `test_ops_triage.py:192-229`），失败请求标记已解决已实现（`logs.py:425,448`，审计 `:398-443`）。真缺只剩两项：系统日志采集（`logs.py:270,314` 只读请求日志，`system.py:77,95` 只有 changelog / versions，无进程 / 上游日志路由）与实时流量（只有 `health.py:419,439` 快照，无推送 / 聚合，现有页面可用轮询承担一部分）。另两项标「待复核」：运行时状态（`health.py:117,122` 仅存活 / 就绪，看哪些指标未定义）、告警规则管理面（规则只在 `config.py:132-140` 与 env 可配，无 DB CRUD，是否要管理面属产品取舍）。档位改为「半截（2 真缺 + 2 待复核）」。
**误判后果**：按旧句会把已上线的告警状态机与失败请求标记重做一遍；也会因为「全目录无对应路由」漏掉「部分已实现、部分真缺、部分只是没定口径」的分层，验收无法逐子项关掉。

### 4.8 U-02「有意不做」

> | U-02 | 未定价模型 token 估算计费 | gptGrok2api、muse2api | 未实现（有意不做） | `apps/control/app/billing.py:320-323`（只打 `usage_zero` / `usage_missing` 标，不估 token）；`docs/muse-usage-policy.md:11-12`；`docs/m3-billing-plan.md:161` 的 `billing.charge_on_missing_usage` 在代码中不存在 | 需财务决策并先补价格行；估算误差写的是「可见文本 ±15–30%」（`muse-usage-policy.md:43`） |
>
> **U-02 token 估算计费。** 现在只把「上游返回全零」和「根本没有 usage」分成 `usage_zero` / `usage_missing` 两种标注，不做估算。`m3-billing-plan.md:161` 写的 `billing.charge_on_missing_usage` 开关在代码里不存在，文档与实现有漂移。要补齐，前提是财务确认要收这条通道的钱，并先给 muse 别名补价格行——否则估出来的 token 套不到价，仍是 0 元。
>
> —— 本文件 §2 总表与 §3.3，2026-11-05 判定时点

**（2026-11-05 已更正）** 「有意不做」与「等财务确认」不能并存。代码侧确实不估（`billing.py:320-323`），但补齐与否卡在拍板。档位改为「待决策」，阻塞原因写死点头对象：财务负责人确认「估算 token 是否计入额度与账单、误差写进账单还是只做提示」；点头前须先给 muse 别名补价格行。
**误判后果**：按「有意不做」归档会把它摘出决策队列，财务议题永远不出现；按「未实现」排期则会先写估算代码，最后因无价格行算不出金额。

### 4.9 U-05「有意不做」

> | U-05 | gptGrok 图库 / 提示词 | gptGrok2api | 未实现（有意不做） | `docs/design/frontend-integration-plan.md:99`（「图片图库/文件存储 不进界面」）；控制面无对应路由 | 需产品决策是否自建；存储与权限是新增面 |
>
> **U-05 gptGrok 图库 / 提示词。** 设计口径明确「图片图库/文件存储不进界面」（`frontend-integration-plan.md:99`），控制面也没有对应路由。要补，得先有图库路由与存储、权限模型，等于新做一块能力，而不只是加个页面。
>
> —— 本文件 §2 总表与 §3.3，2026-11-05 判定时点

**（2026-11-05 已更正）** 旧行同时写「有意不做」与「需产品决策」，两句互相取消。当前只是设计口径写着「不进界面」、控制面无路由，口子并未闭合。档位改为「待决策」，点头对象写死：产品负责人确认「是否自建图库与提示词管理面，并接受存储 / 权限 / 页面三块新增成本」。
**误判后果**：按「有意不做」归档会让人以为已有结论，后续提需求只能重新考古；按「未实现」排期则会在没点头前先动存储与权限模型。

### 4.10 U-04「有意删除」

> | U-04 | `/tasks` 页面恢复 | sub2api / codebuddy2api（任务页） | 未实现（有意删除） | 仓库无 `tasks` 路由（`apps/web/app` 下无该目录）；`FRONTEND-BASELINE.md:70` 记录整条链路已删 | 与基线删页决定冲突，需产品决策 |
>
> **U-04 `/tasks`。** 页面、9 条幽灵路径已按业务决定整体删除，基线也不再保留。要恢复必须推翻那条决定，且与 §3「不新增页面」冲突。
>
> —— 本文件 §2 总表与 §3.3，2026-11-05 判定时点

**（2026-11-05 已更正）** 旧行把「删了」当成「定了」，但同一行又要求产品决策，两句互相取消。删除动作属实（`apps/web/app` 下无 `tasks` 目录），但「维持删除还是恢复」没有闭合。档位改为「待决策」，点头对象写死：产品负责人确认「恢复 `/tasks`（接受推翻基线删页决定、新增页面并触碰 §3），还是确认维持删除」。
**误判后果**：按「有意删除」归档会让后来者以为删除已经定案，再提任务页需求只能从头考古；按「未实现」排期则会在没点头前先恢复页面，直接撞 §3 的不新增页面约束。

### 4.11 U-03「未实现（有意不做）」

> | U-03 | 备份远端目标（S3 等）与定时计划 | 控制面自建 | 未实现（有意不做） | `docs/BACKUP.md:174`（「不做加密、不上传远端」）；`backup.py` 无远端参数与调度 | 需对象存储凭据、失败重试与保留策略设计 |
>
> **U-03 备份远端与定时。** `BACKUP.md:174` 明说不加密、不上传远端。所以这是有意不做的取舍，不是漏做；真要补，前置是对象存储凭据管理、失败重试、保留策略。
>
> —— 本文件 §2 总表与 §3.3，2026-11-05 判定时点

**（2026-11-05 已更正）** 旧行与旧段都过期：远端上传与定时已落地（AES-256-GCM 加密上传 → `.zip.enc` 与 `.zip.enc.sha256`、HEAD 校验、30s/2m/10m 重试、按 keep 轮转、缺加密密钥拒绝上传、默认关闭零外呼），档位改为「半截（远端上传与定时已实现，缺远端恢复）」。证据与行号见 §3.3 同条更正块；这些文件都在工作区、尚未提交，不是已入库。
**误判后果**：按「有意不做」读，会把已写好的加密上传与调度当成不存在，重复实现并重复设计凭据与重试；按「已实现」关掉，则会在磁盘全毁时才发现没有远端恢复路径，远端只有取不回来的密文。

**（2026-11-05 已更正，本批入库后）** 上一块的「半截」与「尚未提交」都已过期：远端恢复 `POST /api/system/backups/restore-remote`（`routers/backup_remote.py:166`）、远端删除 `DELETE /api/system/backups/remote/{name}`（`:222`）与前端入口 `apps/web/components/common/settings/BackupRemoteSection.tsx` 均已落地，并按主题入库（路由与调度 500e671、前端面板 622fa9f、加密后端 4d7e54a、S3 客户端 9ebff30、依赖 6759077）。U-03 档位由「半截」改为「已实现」，证据与行号见 §3.3 末块。
**误判后果**：拿这块当「还差恢复」去排期，会重复实现下载 / 解密链路；把「已实现」直接当运营可用，则会忽略默认关闭 + 凭据只走环境变量这两个前提。

### 4.12 `/api/onboarding` 94 次 404「疑似路由缺失」

> 会话观测：预览环境对 `/api/onboarding` 连续记录 94 次 404，曾被当作「向导路由没上线」的代码缺陷。
>
> —— 会话结论，本文件未逐字收录，无法逐字留档

**（2026-11-06 结案）** 不是代码缺陷。路由已注册（`apps/control/app/main.py:39,324-325`），端点与用例都在（`apps/control/tests/test_onboarding.py:19-97`）。旧预览后端 8240 的启动时间早于 `onboarding.py` 写入，且 `scripts/start.ps1:331` 的 uvicorn 命令不带 `--reload`，旧进程不会加载后加的路由，于是持续 404；重启进程即恢复。归「不做（非缺口）」，不移入缺口清单。94 次这个计数与进程启动时刻无法从仓库复核，只能作为会话观测保留。
**误判后果**：按代码缺陷排期会去重写已经存在的路由与用例，或在复盘时把它算进缺陷密度；真正该做的是把预览进程的重启流程固定下来。

### 4.13 四家 sidecar 许可证「未知」

> 会话旧结论：四家 sidecar 的许可证情况不明，可能阻塞分发。
>
> —— 会话结论，本文件未逐字收录，无法逐字留档

**（2026-11-06 结案）** 已在 `THIRD-PARTY-NOTICES.md` 逐家登记并可核对：trae2api = MIT（`:30,74-77`）、Deepseek2Go 无 LICENSE / 按 All Rights Reserved（`:31,79-82`）、qoder-switch = PolyForm-Noncommercial-1.0.0（`:32,84-87`）、codar2api = MIT（`:33,89-92`）。归「不做（非缺口）」；分发与商用边界按表中 safe / warn / danger 执行，Deepseek2Go 与 qoder-switch 只保留进程隔离、不分发上游代码。
**误判后果**：按「未知」处理会重复做许可证考古，或误把 qoder-switch 当宽松许可而商用；反过来把 MIT 的两家当风险也会拖慢可选 sidecar 的发布。

## 5. 本轮新增的证据（2026-11-05 入库复核）

**（2026-11-05 已更正）** 本节是 2026-11-05 之前的工作区快照，不能当现状读。此后代码已分组入库，本次只读核对时 HEAD 为 `7e7805b`。照旧读会得到两类误判：一是以为 E-01~E-13 的代码 / 测试 / 组件还挂在工作区外，从而重复排期、重复实现；二是把「本地跑绿 ≠ CI 跑绿」推成「这些用例从未进 CI」，据此重写本该由 CI 覆盖的用例。
**复核事实（只读 git，未 add / commit / stash / checkout）**：HEAD 为 `7e7805b`。`git status --short` **非空**：`M apps/control/app/config.py`、`M apps/control/app/main.py`、`M apps/control/app/routers/settings.py`（并行会话的 U-03 备份远端接入，2026-11-05 复核时已落地并注册路由 `main.py:182`，档位见 §3.3）、`M docs/FEATURE-GAP.md`、`M docs/FRONTEND-BASELINE.md`（本次文档任务）。未跟踪 9 项：`apps/control/app/routers/backup_remote.py`、`apps/control/app/services/{backup_crypto,backup_remote,backup_schedule,s3_client}.py`、`apps/control/tests/{test_backup_remote,test_backup_schedule,test_s3_sigv4}.py`、`apps/control/tests/fake_s3_server.py`——全部属于 U-03 备份远端，**尚未提交，不能算已入库**。**E-01~E-13 证据列点名的文件没有一个出现在上面两份清单里**，因此它们的「已入库」判断成立；`apps/control/tests/` 现有 63 个已被跟踪的 `test_*.py`，未跟踪 3 个（上列备份远端用例）。按文件首次入库提交：`accounts.py`、`keys.py`、`v1.py`、`sub2api.py`、`auth.py`、`settings/page.tsx`、`UpdatePanel.tsx` = b5772fb；`accounts/page.tsx` 批量条、`ImportAccountDialog.tsx`、`keys/page.tsx` 批量条 = adf22d8；`accounts.py` 批量操作、`provider_probe.py`、`test_accounts_import.py` = 81bb7d8；`test_backend_gaps.py` = ca7959b；`totp.py`、`auth.py` TOTP、`keys.py` 批量、`test_keys_batch.py`、`test_user_account_system.py` = 8b2d54d；`provider_health.py`、`alerting.py`、`test_provider_health.py`、`logs.py` 导出、`test_logs_export.py` = 82f03a2；`settings.py` 模型映射与 failover、`v1.py` compat、`qoder_sessions.py`、`test_model_map_transfer.py`、`test_v1_client_compat.py`、`test_failover.py`、`test_qoder_sessions_proxy.py` = 65e88b9；`RuntimeConfigSection.tsx` = 7f4d9a1；`backup.py`、`test_backup_api.py`、`backup_api_smoke.py` = 98d7b31；`update.py` = 8253f2c；`update_feed.py` = 9349160；`BackupPanel.tsx`、`ModelMapTransfer` 相关设置页 = dafb3c4；`logs/page.tsx` = eacc574；`quota/page.tsx`、`quota/policies/page.tsx` = 925f15c；`RISK-REGISTER.md`、`schema-migration-audit.md` = fbe49d5；`ci.yml` 已跟踪（首入库 b5772fb），工作树无改动。
**误判后果**：继续按旧快照读，会对已入库的功能重复排期、重复写测试，并把「CI 是否覆盖这些用例」建立在错误的「未入库」前提上。

**（2026-11-05 已更正，本批入库后）** 上一段的 HEAD 与清单都已过期：U-03 的 9 个未跟踪文件已按主题入库——`services/backup_crypto.py` 4d7e54a、`services/s3_client.py` 9ebff30、`services/{backup_remote,backup_schedule}.py` 与 `routers/backup_remote.py` 及 `test_backup_remote.py` / `test_backup_schedule.py` / `test_s3_sigv4.py` / `fake_s3_server.py` 500e671、前端 `BackupRemoteSection.tsx` 622fa9f；`config.py` / `main.py` / `routers/settings.py` 的改动也随 500e671 与 9ebff30 入库，工作树此时为空；`apps/control/tests/` 已跟踪的 `test_*.py` 由 63 变为 66（新增上列三份）。E-01~E-13 的「已入库」判断不受影响。
**误判后果**：照旧快照核对暂存区，会去找 9 个 `??` 文件来确认「还没入库」，而它们已经在 500e671 里；据此再提交一次会造成重复实现，或误判 CI 缺少覆盖。

以下为 2026-11-05 前快照，逐字保留：

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

**（2026-11-05 入库复核）** 上一段的「20 个未入库测试文件」与「`ci.yml` 还没提交」已过期：判定时点之后的分组提交里，`apps/control/tests/` 的 63 个 `test_*.py` 已被跟踪；2026-11-05 新增的 U-03 用例（`test_backup_remote.py`、`test_backup_schedule.py`、`test_s3_sigv4.py` 与夹具 `fake_s3_server.py`）仍在工作区未跟踪，不计入这 63 个。`ci.yml` 已跟踪（首入库 `b5772fb`）且工作树无改动。所以「这些用例从未进 CI」不成立，能确认的只是「已进版本库」。本轮没有执行任何测试，也没有查 CI 运行记录，**用例是否在 CI 里实跑通过仍未核实**，不能把「已入库」当成「已跑绿」。
**误判后果**：继续按这段读，会去重写已经在 `ci.yml` 覆盖范围内的用例，或把「未入库」推成「CI 从没跑过」而绕开 CI 另建一套验证。

**（2026-11-06 二次复核快照）** 只读核对，未做 add / commit / push。HEAD `d906399`，分支 `baseline/unified-2api`，相对 `origin/baseline/unified-2api` ahead 47。`git status --porcelain` 为 53 行（含本文件与 `.gitignore` 修改）：38 `M` + 15 `??` 条目；`-uall` 为 54 行（`apps/web/components/common/onboarding/` 展开为 `OnboardingWizard.tsx` / `OnboardingTodo.tsx`，按文件计 54 个）。`??` 项：`apps/control/app/{config_bundle.py,routers/config_transfer.py,routers/onboarding.py,services/credential_state.py}`、`apps/control/tests/{test_config_transfer,test_credential_state,test_onboarding}.py`、`apps/web/components/common/onboarding/`（两个组件）、`apps/web/components/common/settings/ConfigTransferPanel.tsx`、`apps/web/lib/onboarding.ts`、`scripts/{config_export.py,config_import.py,serve-web.mjs}`、`deploy/{.env.prod-bind.example,docker-compose.prod-bind.yml}`。本轮只改本文件与 `.gitignore`；N-03 的 `test_config_transfer.py` 已实跑 8 passed（8098/8099 端口），其余条目没有执行 pytest、没有启动服务、没有做浏览器实测，N-02 / N-04 / N-06 / N-07 / N-08 的「测试存在」不等于「本轮跑绿」。并行会话仍可能继续改动 `apps/control` / `apps/web`，条目数以复核时 `git status` 为准。
**误判后果**：把「测试文件存在」当「本轮已验证」，会在交付时漏掉真实失败；把并行会话的半成品当稳定实现，会在下一轮盘点时反复改档。

**（2026-11-06 入库后更正）** 上一段是复核时点（提交前）的快照：ahead 47、`git status` 53 行 / 按文件 54 个，都只用来证明「复核发生在提交之前」，不构成任何条目结论。复核之后本轮按主题拆成 10 个提交入库（`2268d50` 一键启动、`d5959f4` 五语文案、`77a0faf` 配置打包后端、`100285d` 日志导出、`60122e4` 凭据失效、`65d71a0` 首次向导、`d397290` 设置页接线、`f0bdf9f` 移动端、`da0d35a` 部署模板、`8b5b9e5` 文档与忽略规则），提交后 `git status --porcelain` 为空，分支相对 `origin/baseline/unified-2api` ahead 57。上段列出的 `??` 文件现已全部在库，N-02 / N-03 / N-09 不再有「未提交」这一残留；其余条目「测试存在 ≠ 本轮跑绿」的结论不受影响。
**误判后果**：继续按上段读，会去找那 15 个 `??` 条目来确认「还没入库」，而它们已经在上面 10 个提交里；据此再提交一次会造成重复实现。

## 6. 复核方式

对任一编号，按证据列的文件:行直接打开当前工作区核对；拿不到对应函数或路由即视为「未核实」，按 §1 重新归档，不要沿用本文件。发现本文件与代码不符时，按 §4 体例追加更正块，不删原句。

## 7. 流程阻塞（非功能缺口）

### 7.1 push 被 GitHub secret scanning 拦截

- **现状**：本地分支 `baseline/unified-2api` 相对 `origin/baseline/unified-2api` ahead 57 个提交，push 未完成。仓库内没有任何可核对的告警原文（全仓检索 `secret scanning` / `push protection` 无命中），命中的文件、规则名、是否误报均**未核实**。
- **两条出路**（都必须先确认命中内容，不得强行绕过）：
  1. **确属误报**：由仓库管理员在 GitHub 的 Security → Secret scanning 对该告警标记 false positive / 加 allowlist，然后重推同一提交。不要改代码去迁就扫描器，也不要用 push protection bypass 口令。
  2. **确属真实泄露**：先把命中的凭据在对应上游轮换作废，再改成环境变量或本地未入库配置，按仓库历史清理流程重写该提交（必要时 `git filter-repo`）后重推；不得用 `--no-verify`、`--force` 或关闭 push protection 绕过。
- **影响**：不阻塞本地开发与验收，只阻塞对外发布。任何「已发布 / 已上线」结论在 push 成功前都不成立；本条不计入功能缺口，也不按功能缺口排期。

## 8. 待决策 / 已知风险（未修）

这两条都不是功能缺口，也不写成已解决；放在这里是为了别在下一次盘点时被当成「已修复」划掉。

### 8.1 Docker 生产控制面 healthcheck 启动窗口偏短，可能导致前端永远不起

- **现状**：控制面探活打 `/healthz`，`deploy/docker-compose.prod-bind.yml:67-74` 为 `interval: 30s`、`timeout: 5s`、`retries: 3`、`start_period: 120s`（注释自称「给足启动窗口」）；`deploy/docker-compose.prod.yml:36-41` 三个参数相同但 `start_period` 只有 `20s`。前端 `web` 以 `depends_on: control: condition: service_healthy` 等控制面健康后才启动（prod-bind `:80-82`、prod `:56-58`、根 `deploy/docker-compose.yml:75-77`）。
- **风险**：首次启动要建库、跑迁移、回放大库，超过 `start_period` 后再连续 3 次（间隔 30s）失败才判 `unhealthy`——prod-bind 约 210s、prod 约 110s。Docker 的 `restart: unless-stopped` 只在**进程退出**时重启容器，不会因 `unhealthy` 重启；于是控制面进程还在跑、状态显示 unhealthy，而 `web` 因条件未满足一直不启动，表现为「控制面在跑、前端永远起不来」。人工 `docker compose restart` 能恢复，但自动恢复没有。
- **未修 / 待决策**：由运维或部署负责人定 `start_period` 取值（或改 `depends_on` 条件、加 `restart` 之外的补偿）。本次只登记，不改 compose。

### 8.2 CLI 恢复是文件整体替换，不是在线备份，必须停服

- **现状**：`scripts/backup_core.py:844` 的 `cmd_restore` 先校验归档，再 `pre_restore()` 留存当前库，然后替换——库是 `:882-884` 把暂存库 `os.replace` 到目标路径，配置文件同样 `shutil.copy2` + `os.replace`（`:727-740`）。`pre_restore()` 的注释写明前提是服务已停止（`:618-623`），失败分支的报错也要求先停服（`:642-644`：「Stop the control plane first, then retry (Windows locks open db files)」）。
- **风险**：服务运行中执行 CLI 恢复时，Windows 上库文件被占用会直接失败；即便侥幸替换成功，也可能与在线写入交错，丢掉快照之后的已提交事务。在线恢复应走管理界面的 `POST /api/system/restore`（`apps/control/app/routers/backup.py:249-263`，`restore_archive` 在 `:182`，全程持 `_busy_guard`），不用 CLI 顶替。
- **文档状态**：`docs/BACKUP.md:152,156` 已写明「恢复前必须停服务」且只对 CLI 路径成立、管理界面不停服也能恢复；本条只登记为已知风险，不再重复改 BACKUP.md。
