# 文档索引

这里是 Neosub2api（仓库目录仍为 unified-2api）的 `docs/` 总入口。按「你先要解决什么问题」分组，每条一句话说明它解决什么问题。仓库根 [README](../README.md) 负责「是什么、怎么起、有哪些能力、边界在哪」，本页负责「接下来读哪篇」。

## 先选一条阅读路径

| 你的目标 | 建议顺序 |
| --- | --- |
| 第一次接触，想先看到界面 | [README](../README.md) → [DEMO](DEMO.md) → [GETTING-STARTED](GETTING-STARTED.md) |
| 要接客户端、调 `/v1` | [API-REFERENCE](API-REFERENCE.md) → [examples](examples.md) → [error-contract](error-contract.md) |
| 要上线、做运维 | [DEPLOYMENT](DEPLOYMENT.md) → [DEPLOYMENT-PROD](DEPLOYMENT-PROD.md) → [compliance](compliance.md) → [security-retest-2026-11-03](security-retest-2026-11-03.md) |
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
| [PERFORMANCE.md](PERFORMANCE.md) | 网关热路径的 SQL 次数、索引、压测数据与吞吐瓶颈，说明为什么 SQLite 单写者是上限。 |
| [UPSTREAM-SYNC.md](UPSTREAM-SYNC.md) | 上游仓库的巡检与同步流程：先评估、后更新，以及如何避免把上游代码直接拷进主仓。 |
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

## 安全与合规

| 文档 | 解决什么问题 |
| --- | --- |
| [security-audit.md](security-audit.md) | 早期安全与合规审计的结论、风险分级与修复记录。 |
| [security-retest-2026-11-03.md](security-retest-2026-11-03.md) | 2026-11-03 权限矩阵、`u2a_` 泄露面与 SSRF 边界的复测结果，含仍未关闭的低危项。 |
| [compliance.md](compliance.md) | 许可证与合规边界：哪些上游可 clean-room 重写、哪些必须 sidecar、哪些禁止商用。 |
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
| [design/README.md](design/README.md) | 已判定不采用的设计落地说明与原型存档，仅作历史参考。 |

## 其他目录

| 路径 | 内容 |
| --- | --- |
| [providers/](providers/) | 逐家上游的 clean-room 协议契约与实现说明（如 `opencode2api.md`、`sub2api.md`、`grok-register.md`）。 |
| [ux-shots/](ux-shots/) | UX 走查与页面状态的截图证据。 |
| [site/](site/) | 静态文档站构建脚本与产物。 |
| [design/prototype.html](design/prototype.html) | 早期导航与页面原型，仅作存档，不代表当前界面。 |

## 维护约定

- 行为变化按日期写进仓库根 [CHANGELOG.md](../CHANGELOG.md)，每条注明会影响谁。
- 新增或重命名文档时，同时更新本索引；不要让文档只能靠搜索找到。
- 文档里的命令默认从仓库根执行；示例路径使用仓内相对路径，不写本机绝对路径。
- 各文档由对应模块负责人维护；本索引只负责指路，不替它们做结论。
