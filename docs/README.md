# 文档索引

这里是 Neosub2api（仓库目录仍为 unified-2api）的 `docs/` 总入口。按「你先要解决什么问题」分组，每条一句话说明它解决什么问题。仓库根 [README](../README.md) 负责「是什么、怎么起、有哪些能力、边界在哪」，本页负责「接下来读哪篇」。

## 先选一条阅读路径

| 你的目标 | 建议顺序 |
| --- | --- |
| 第一次接触，想先看到界面 | [README](../README.md) → [DEMO](DEMO.md) → [GETTING-STARTED](GETTING-STARTED.md) |
| 要接客户端、调 `/v1` | [API-REFERENCE](API-REFERENCE.md) → [examples](examples.md) → [error-contract](error-contract.md) |
| 要上线、做运维 | [DEPLOYMENT](DEPLOYMENT.md) → [DEPLOYMENT-PROD](DEPLOYMENT-PROD.md) → [SCHEDULING](SCHEDULING.md) → [compliance](compliance.md) → [security-retest-2026-11-03](security-retest-2026-11-03.md) |
| 页面报错或请求失败 | [TROUBLESHOOTING](TROUBLESHOOTING.md) → [error-contract](error-contract.md) → [PERFORMANCE](PERFORMANCE.md) |
| 要新增一家 provider | [ADDING-A-PROVIDER](ADDING-A-PROVIDER.md) → [PROTOCOL-MATRIX](PROTOCOL-MATRIX.md) → [PROVIDERS](PROVIDERS.md) |

## 上手

| 文档 | 解决什么问题 |
| --- | --- |
| [GETTING-STARTED.md](GETTING-STARTED.md) | 从环境要求、`.env`、前端构建到第一个 `/v1/chat/completions` 的完整启动步骤。 |
| [DEMO.md](DEMO.md) | 用 `UNIFIED_DEMO_MODE` 一键起假上游与演示数据，十分钟内看到有数据的界面，以及怎么清除、为什么不能用于生产。 |
| [examples.md](examples.md) | Chat / Responses / Messages 三个协议的 curl、Python、Node 调用示例，含流式与非流式、两种鉴权头与常见错误码。 |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | 按症状查启动失败、401/403/404/429、模型解析、上游超时与前端空白等问题。 |
| [ADDING-A-PROVIDER.md](ADDING-A-PROVIDER.md) | 新增一家 provider 的现行步骤：先定接入形态，再写适配器、能力位、测试与文档。 |

## 运维

| 文档 | 解决什么问题 |
| --- | --- |
| [DEPLOYMENT.md](DEPLOYMENT.md) | docker compose 部署、端口映射、数据目录与升级方式。 |
| [DEPLOYMENT-PROD.md](DEPLOYMENT-PROD.md) | 生产硬化版部署：nginx、HTTPS、备份恢复、权限与安全响应头。 |
| [OPS-RUNBOOK.md](OPS-RUNBOOK.md) | 上线前检查、启停、常见故障判断与已知运行前提。 |
| [BACKUP.md](BACKUP.md) | 归档内容、密钥在归档中的放法、CLI 与界面恢复路径。 |
| [PERFORMANCE.md](PERFORMANCE.md) | 网关热路径的 SQL 次数、索引、压测数据与吞吐瓶颈，说明为什么 SQLite 单写者是上限。 |
| [SCHEDULING.md](SCHEDULING.md) | 账号池的分档冷却、熔断、粘性、权重、轮转组，以及 codar 四档调度策略；含各适配器接线现状与 `readiness` 口径澄清。 |
| [UPSTREAM-SYNC.md](UPSTREAM-SYNC.md) | 上游仓库的巡检与同步流程：先评估、后更新，以及如何避免把上游代码直接拷进主仓。 |
| [UPSTREAM-CHECK-RUNBOOK.md](UPSTREAM-CHECK-RUNBOOK.md) | 上游巡检脚本的令牌要求、限流表现、`--update` 语义与 CI 判定口径。 |
| [upstream-drift-2026-11-04.md](upstream-drift-2026-11-04.md) | 2026-11-04 对 codar2api 与 qoder-trae-workbuddy-switch 的逐文件读码结论：哪些上游改动会影响我方 sidecar。 |
| [schema-migration-audit.md](schema-migration-audit.md) | 老库升级的迁移路径、退化演练与静默降级清单。 |
| [model-map-migration.md](model-map-migration.md) | `model_map` 从单别名迁移到「多候选 + 权重」的方案与兼容边界。 |

## 契约与协议

| 文档 | 解决什么问题 |
| --- | --- |
| [API-REFERENCE.md](API-REFERENCE.md) | `/api/*` 与 `/v1/*` 的端点、参数、鉴权、权限、错误码与响应形状。 |
| [CONTRACT.md](CONTRACT.md) | 用 `scripts/contract_check.py` 核对「前端调用的路径后端是否真的有」，把幽灵端点检查变成一条命令。 |
| [error-contract.md](error-contract.md) | 三套错误壳（OpenAI / Anthropic / `detail`）的状态码矩阵、结构化拒因与已知偏差。 |
| [PROTOCOL-MATRIX.md](PROTOCOL-MATRIX.md) | 14 家 provider 的能力位、鉴权头、上游入口与协议差异，以适配器代码为准。 |
| [PROVIDERS.md](PROVIDERS.md) | 逐家 provider 的用途、必填配置、协议、风险等级与 readiness。 |
| [model-routing.md](model-routing.md) | 模型别名、`<provider>/<model>`、`x-provider` 与严格解析 / 兜底的判定顺序。 |
| [pricing-sources.md](pricing-sources.md) | 价格簿的数据来源、单位（micro-USD）、维护方式与不可核价时的处理。 |
| [muse-usage-policy.md](muse-usage-policy.md) | muse 通道 usage 恒 0 时的计费口径决策，避免把「无 usage」当成「免费」。 |
| [I18N.md](I18N.md) | 五种语言的键组织、回退链、`phrases` 反向表与 `scripts/check_i18n.py` 的 CI 校验。 |

## 现状与缺口

| 文档 | 解决什么问题 |
| --- | --- |
| [FEATURE-GAP.md](FEATURE-GAP.md) | 功能缺口账本：六档口径、逐项证据、已落地能力对照与旧结论更正留档。 |
| [RISK-REGISTER.md](RISK-REGISTER.md) | 已识别风险的触发条件、影响面、缓解动作与责任边界。 |
| [consistency-report.md](consistency-report.md) | 并行工作结论的一致性复核（2026-11-03 快照，个别结论已被后续提交改写）。 |

## 安全与合规

| 文档 | 解决什么问题 |
| --- | --- |
| [security-audit.md](security-audit.md) | 早期安全与合规审计的结论、风险分级与修复记录。 |
| [security-retest-2026-11-03.md](security-retest-2026-11-03.md) | 2026-11-03 权限矩阵、`u2a_` 泄露面与 SSRF 边界的复测结果，含仍未关闭的低危项。 |
| [compliance.md](compliance.md) | 许可证与合规边界：哪些上游可 clean-room 重写、哪些必须 sidecar、哪些禁止商用。 |
| [authz-audit.md](authz-audit.md) | `/api/` 与 `/v1/` 全端点的鉴权矩阵、逐条判定与前端调用面交叉核对。 |
| [secret-leak-audit.md](secret-leak-audit.md) | 凭据与密钥泄露面的只读审计：已知可控项与按严重度排序的修法。 |
| [dependency-audit.md](dependency-audit.md) | 依赖高危清单、许可证一致性与升级优先级（只出建议，不代执行升级）。 |
| [../THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md) | 完整第三方许可证清单与商用前 checklist。 |

## 设计与历史

以下文档记录当时的方案或审计快照，**不代表当前实现**；与代码冲突时以代码和 [API-REFERENCE](API-REFERENCE.md) 为准。

| 文档 | 解决什么问题 |
| --- | --- |
| [unification-audit.md](unification-audit.md) | 2026-11-03 全站一致性审计：P0/P1/P2 问题、端点覆盖矩阵、权限不一致、幽灵端点与 i18n 完整性。 |
| [m3-billing-plan.md](m3-billing-plan.md) | M3 配额、计费、白名单与 Postgres 支持的设计方案与取舍。 |
| [FRONTEND-BASELINE.md](FRONTEND-BASELINE.md) | 前端基准与红线：哪些能改、哪些不能改，视觉与导航以哪份上游为基准。 |
| [FRONTEND-SKELETON.md](FRONTEND-SKELETON.md) | 前端骨架的可扩展边界：组件、设计语言与可复用的布局约定。 |
| [ux-review.md](ux-review.md) | 2026-11-02 六个页面的 UX 走查记录与截图证据。 |
| [ux-fix-triage.md](ux-fix-triage.md) | UX 问题的 A/B/C 分级与修法建议。 |
| [demo-walkthrough.md](demo-walkthrough.md) | 2026-11-03 演示模式有数据走查：九个业务页与设置页的逐页结论、截图口径与遗留问题。 |
| [design/README.md](design/README.md) | 已判定不采用的设计落地说明与原型存档，仅作历史参考。 |
| [design/frontend-integration-plan.md](design/frontend-integration-plan.md) | 前端功能落位与逐页面实现计划（历史方案，冲突时以 FRONTEND-BASELINE 为准）。 |
| [design/ia-proposal.md](design/ia-proposal.md) | 前端信息架构与品牌方案（已判定不采用，只作存档）。 |

## 逐家上游（`providers/`）

| 文档 | 解决什么问题 |
| --- | --- |
| [codar2api.md](providers/codar2api.md) | codar2api 的 clean-room 协议契约与接入结论。 |
| [codar_sidecar.md](providers/codar_sidecar.md) | 主仓对 codar2api sidecar 的 HTTP 透传适配器说明。 |
| [deepseek-feasibility.md](providers/deepseek-feasibility.md) | DeepSeek PoW 第一阶段可行性验证，结论为不实现。 |
| [deepseek2go-feasibility.md](providers/deepseek2go-feasibility.md) | Deepseek2Go PoW 的 F2-B 评估，结论为不实现。 |
| [gptgrok2api.md](providers/gptgrok2api.md) | 仓内 GPT/Grok 实现：模型前缀分流、媒体子工程与鉴权细节。 |
| [grok-register.md](providers/grok-register.md) | Grok 注册执行器的协议契约与调用时序。 |
| [muse2api.md](providers/muse2api.md) | muse2api 的 clean-room 协议契约。 |
| [muse2api-protocols.md](providers/muse2api-protocols.md) | muse2api 的 `responses` / `images` / `videos` 协议面扩展方案。 |
| [opencode2api.md](providers/opencode2api.md) | opencode2api 的 clean-room 协议契约。 |
| [qoder.md](providers/qoder.md) | Qoder 协议的 clean-room 重写：COSY 加密与请求形状。 |
| [qoder-adapter.md](providers/qoder-adapter.md) | 内置直连 Qoder 适配器：自定义 base64、AES-128-CBC 与多身份处理。 |
| [sub2api.md](providers/sub2api.md) | sub2api 的 clean-room 协议契约：分组、账号池与订阅配额。 |
| [trae2api.md](providers/trae2api.md) | trae2api 的 clean-room 重写：Solo 与 Work 两条通道。 |
| [trae-adapter.md](providers/trae-adapter.md) | 内置直连 Trae 适配器：不走 sidecar 的两条通道实现。 |
| [zcode2api.md](providers/zcode2api.md) | ZCode 的仓内协议契约与端点说明。 |

## 其他目录

| 路径 | 内容 |
| --- | --- |
| [providers/](providers/) | 逐家上游的协议契约与实现说明，逐篇索引见上一节。 |
| [ux-shots/](ux-shots/) | UX 走查与页面状态的截图证据。 |
| [site/](site/) | 静态文档站构建脚本与产物。 |
| [design/prototype.html](design/prototype.html) | 早期导航与页面原型，仅作存档，不代表当前界面。 |

## 维护约定

- 行为变化按日期写进仓库根 [CHANGELOG.md](../CHANGELOG.md)，每条注明会影响谁。
- 新增或重命名文档时，同时更新本索引；不要让文档只能靠搜索找到。
- 文档里的命令默认从仓库根执行；示例路径使用仓内相对路径，不写本机绝对路径。
- 各文档由对应模块负责人维护；本索引只负责指路，不替它们做结论。
