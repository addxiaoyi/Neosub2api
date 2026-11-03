# 上游同步流程

这份文档回答一个问题：巡检脚本报出「有更新」之后，接下来做什么。

上游清单见 [`upstreams/upstreams.yaml`](../upstreams/upstreams.yaml)，字段说明见 [`upstreams/README.md`](../upstreams/README.md)。

## 基本原则

1. **基线只代表"读过"，不代表"跟上了"。** `analyzed_commit` 是分析过的版本，不是我们要追的版本。先评估影响，再 `-Update`。
2. **上游源码不进主仓。** 分析在独立临时目录做，只提炼协议写进 `docs/providers/*.md`。
3. **许可证优先于功能。** 上游许可证一变，先处理合规，再谈跟进。
4. **一次跟进一次提交。** 清单、契约文档、适配器、`THIRD-PARTY-NOTICES.md` 一起改，方便回滚。

## 触发方式

| 来源 | 频率 | 说明 |
| --- | --- | --- |
| `.github/workflows/upstream-sync.yml` | 每周一 02:00 UTC（北京时间 10:00） | 跑 `scripts/check_upstreams.py`，有 `updated` / `error` 时开 issue 或追加评论，同时写 job summary。**不提交清单、不让构建失败。** |
| `.github/workflows/upstream-watch.yml` | 每周一 09:00（北京时间） | 旧链路（PowerShell + `git ls-remote`），保留作交叉核对。 |
| 手动 | 需要时 | `python scripts/check_upstreams.py` 或 `powershell -File scripts/check-upstreams.ps1` |
| 发版前 | 每次发布 | 至少确认清单里没有长期 `outdated` 的条目。 |

## 巡检脚本 `scripts/check_upstreams.py`

只用 Python 标准库（`urllib`），走 GitHub REST API：先取 `GET /repos/{owner}/{name}`（默认分支、`archived` / `disabled`、改名检测），再取 `GET /repos/{owner}/{name}/commits/{branch}`（HEAD 的 sha 与提交时间）。

```bash
python scripts/check_upstreams.py            # 文本输出
python scripts/check_upstreams.py --json     # 单行 JSON，CI 消费
python scripts/check_upstreams.py --update   # 回写 checked_at；不动 analyzed_commit
```

三种状态：

| 状态 | 含义 |
| --- | --- |
| `unchanged` | HEAD 与 `analyzed_commit` 一致；或 HEAD 已偏移但提交早于 `checked_at`（已在巡检中见过、基线刻意未推进）。 |
| `updated` | HEAD 与基线不一致，且提交时间不早于 `checked_at` → 出现新 commit。 |
| `error` | 404（删除 / 改名 / 私有化）、改名为别的仓库、`archived` / `disabled`、限流、网络失败。 |

退出码：`0` 全部 `unchanged`；`1` 有 `updated`；`2` 有 `error`（可能同时有 `updated`）。单家失败不影响其余仓库，最后按汇总给码。

匿名调用是 60 次/小时（每家 2 次请求，11 家共 22 次）；设置 `GITHUB_TOKEN` / `GH_TOKEN` 环境变量即自动带上，脚本不回显、不写盘。

`--update` 只改 `checked_at`（逐行正则替换，保留缩进、行序、换行符与 BOM），**不推 `analyzed_commit`**——基线推进仍由 `check-upstreams.ps1 -Update` 或人工在读完新 commit 后完成。

## 第一步：判断改动性质

先看 diff，不要先看代码结构。在临时目录做浅克隆并对比基线：

```powershell
$tmp = Join-Path $env:TEMP "upstream-audit-$(Get-Random)"
git clone --depth 1 https://github.com/<owner>/<repo>.git $tmp
git -C $tmp log --oneline -n 20

# 有旧基线时可以直接比文件
git -C $tmp fetch --depth 1 origin <analyzed_commit>
git -C $tmp diff --stat <analyzed_commit> HEAD
```

只看这些文件（按优先级）：

| 文件类型 | 为什么看 |
| --- | --- |
| `LICENSE` / `COPYING` / `NOTICE` | 许可证变化优先级最高。 |
| 路由注册、handler、SDK 客户端 | 端点和鉴权头会不会变。 |
| 请求/响应结构体、SSE 事件名 | 字段和流式事件会不会变。 |
| 错误码映射、状态码常量 | 错误语义会不会变。 |
| 模型列表来源、`/v1/models` 处理 | 模型 id 和结构会不会变。 |
| `Dockerfile`、启动参数、环境变量、默认端口 | sidecar 的启动方式和默认地址会不会变。 |

文档、CI、测试、前端样式、README 文案的改动一律不影响协议。

## 第二步：判定「协议变了」

满足**任意一条**就算协议变化：

- 端点路径、HTTP 方法、`Content-Type` 发生变化，或新增/删除对外端点。
- 鉴权头名称、格式、前缀（如 `Bearer` / `x-api-key`）、Key 形态发生变化。
- 请求体必填字段、字段类型、枚举值变化；默认行为变化（例如 `stream` 默认值翻转）。
- 响应体结构变化：`choices` / `content` / `usage` 的位置或类型改变。
- SSE 事件名、`data:` 分帧格式、结束标记（`[DONE]` / `message_stop`）变化。
- 错误体结构或错误码语义变化（例如 429 从"限流"变成"余额不足"）。
- `/v1/models` 返回结构或模型 id 命名规则变化。
- 默认端口、启动命令、必需环境变量变化（只影响 sidecar 形态）。

只是内部重构、依赖升级、日志/UI 调整、测试补充 → **协议未变**。

## 第三步：按类别处置

### A. 协议未变（只动了内部实现）

- 只需更新 `analyzed_commit` / `analyzed_at`。
- 契约文档可以在 `依据` 一节追加一行新的 commit 说明，也可以不动。
- 适配器不改。
- 如果上游修了影响行为的 bug（例如错误码映射），即使"协议未变"也要评估是否需要在代理层对齐，写进 `notes`。

### B. 协议变了，但只影响文档描述

适用：新增可选字段、新增端点但我们暂不支持、模型列表扩容、错误文案变化。

- 更新 `docs/providers/<name>.md` 的端点表 / 字段表 / 错误码表。
- 在 `docs/providers/<name>.md` 的「最小对齐集」标注暂不支持的部分。
- 适配器只在需要暴露新能力时才改。
- `upstreams.yaml` 的 `protocols` 同步更新。

### C. 协议变了，必须改适配器

适用：必填字段变化、SSE 格式变化、鉴权头变化、错误码语义变化、模型 id 规则变化。

| 接入形态 | 改哪里 |
| --- | --- |
| `in-repo-rewrite` | 对应 `apps/control/app/adapters/<name>.py`；必要时改 `routers/` 与 `services/`。 |
| `sidecar` | 先升级/重启 sidecar 到新版本，再改 `apps/control/app/adapters/<name>_sidecar.py` 的 `default_base_url`、健康检查、模型映射。**仍不复制源码**。 |
| `reference-only` / `not-integrated` | 只更新契约文档与 `notes`，不动代码。 |

改完必须同步更新：

- `docs/providers/<name>.md`
- `upstreams/upstreams.yaml` 的 `analyzed_commit`、`analyzed_at`、`protocols`、`notes`
- 若新增能力位，检查 `apps/control/app/adapters/base.py` 的 `CAPABILITY_KEYS` 是否已覆盖（**新增能力位属于跨会话改动，需与并行开发协调**）
- 若新增 provider，还要改 `apps/control/app/adapters/__init__.py` 的 `BUILDERS`

> 注意：当前有并行会话在改 `apps/control/app/` 与 `apps/web/`。适配器改动请单独提交，避免和它们的改动混在一起。

### D. 触发退出

适用：许可证收紧、上游删除仓库、上游改为闭源分发、上游要求接入方签署额外条款。

见文末「许可证变化与退出策略」。

## 第四步：回归验证

改完适配器后至少跑：

```powershell
# 1) 控制面单测
.\.runtime\python\python.exe -m pytest apps\control\tests -q

# 2) 端到端冒烟（启动假上游 + 控制面，校验网关关键路径）
powershell -ExecutionPolicy Bypass -File scripts\smoke.ps1

# 3) 上游清单自检
powershell -File scripts\check-upstreams.ps1
```

按改动类型补测：

| 改动 | 必测 |
| --- | --- |
| 请求字段 | 非流式一次、流式一次，确认 `usage` 仍能取到。 |
| SSE 格式 | `curl -N` 看分帧，确认前端能逐块渲染、能正常结束。 |
| 鉴权 | 错误 Key 返回 401，禁用 Key 返回 403，Key 模型范围生效。 |
| 错误码 | 上游 429 / 402 / 5xx 都能映射成网关一致的结构。 |
| 模型列表 | `GET /v1/models` 非空；空列表时 `health()` 按"在线但空"处理。 |
| sidecar 启动 | 默认端口、健康检查路径、启动参数与环境变量与文档一致。 |

记录结果时写清楚：命令、实际输出摘要、失败项。不要只写「已验证」。

## 第五步：刷新基线

确认代码和文档都跟进后：

```powershell
powershell -File scripts/check-upstreams.ps1 -Update
```

只更新有更新且可访问的记录，`analyzed_at` 改成当天。然后把 `upstreams.yaml` 顶层 `updated_at` 改成今天。

**顺序不能反。** 先 `-Update` 再评估，等于把"没看过"的 commit 标记成"看过"，下次检查就永远不会再提示。

## 巡检记录

本节按日期倒序记录每次远端巡检的结论。巡检只确认"HEAD 是否偏移、偏移是否触碰协议、仓库是否异常"，**不推进 `analyzed_commit`**；读数与结论写在 `upstreams/upstreams.yaml` 的 `checked_at` 与 `notes`。

### 2026-11-03

当天做了两次巡检：上午用 `powershell -File scripts\check-upstreams.ps1 -Json`（`git ls-remote`），下午用 `python scripts/check_upstreams.py`（GitHub API）。下表是下午那次的结果，11 家全部可访问，`archived=false`、`disabled=false`，无 404 / 改名 / 私有化。上午 08:00Z 之后又有三家上游落了新提交，所以与上午那次相比多了 2 家偏移。

| 仓库 | 基线 → 远端 HEAD | 状态 | 依据 / 影响 |
| --- | --- | --- | --- |
| `addxiaoyi/codebuddy2api` | `92b8b91` → 同值 | unchanged | HEAD 未移动（2026-09-25T18:35:40Z）。 |
| `czg86389-hub/muse2api` | `1cd0168` → 同值 | unchanged | HEAD 未移动（2026-11-02T10:26:22Z）。 |
| `kaibush/grok-register` | `a5eddc2` → `5521150` | updated | 偏移 2 个 commit，文件仅 `.github/data/stars-history.json` 与 `docs/images/stars-trend-{dark,light}.svg`（stars 统计）。无代码、无路由、无协议；基线不推进。 |
| `jasonxu114514/opencode2api` | `58f5667` → 同值 | unchanged | HEAD 未移动（2026-10-29T20:16:18Z）；GPL-3.0 隔离约束不变。 |
| `addxiaoyi/gptGrok2api` | `0335d62` → 同值 | unchanged | HEAD 未移动（2026-09-28T07:39:48Z）。 |
| `addxiaoyi/ZCode2api` | `25c57e6` → 同值 | unchanged | HEAD 未移动（2026-10-31T05:56:16Z）。 |
| `Wei-Shaw/sub2api` | `4a4a83e` → 同值 | unchanged | HEAD 未移动（2026-11-02T03:17:44Z）；LGPL-3.0 隔离约束不变。 |
| `JeffHu0912/trae2api` | `ec20c08` → 同值 | unchanged | HEAD 未移动（2026-09-08T03:35:49Z）。 |
| `ColudAI/Deepseek2Go` | `3d9f10c` → 同值 | unchanged | 默认分支为 `master`，HEAD 未移动（2025-09-15T06:56:06Z）；仍无 LICENSE。 |
| `NextAgentX/qoder-trae-workbuddy-switch` | `93d873c` → `930d149` | updated | 12 个 commit（2026-11-03T07:12:17Z）。协议侧只新增观测：`protocol/usage_tap.rs` 的 `UsageReport` 加 `cached_tokens`，`routes/{chat,messages}.rs` 仅补该日志字段；端点、鉴权头、请求/响应结构未变。sidecar 无需升级，适配器不用改。 |
| `AiXiaoc/codar2api` | `d7f5024` → `ba05367` | updated | v0.1.4 单次发布提交（2026-11-03T04:14:53Z）。改动集中在 `internal/gateway/`：compat 身份前缀、`proxy_retry`、`request_blocklist`、账号导入、管理面板与测试；未见对外端点/响应结构变化。跟进方案见下。 |

**协议变化：0 家。仓库异常：0 家（无 404 / 改名 / 私有化）。无法检查：0 家。** 结果：unchanged 8 / updated 3 / error 0，脚本退出码 `1`。

待人工确认（巡检不改清单地址，也不推基线）：

- `AiXiaoc/codar2api` 是 sidecar 形态，是否把 `providers/codar2api` 的获取版本刷到 v0.1.4，需要先看它 release notes 里有没有启动参数/端口变化；本次 diff 里 `Dockerfile` 只改了 `VERSION` 注入方式，默认端口与入口未变。
- `kaibush/grok-register` 的 `notes` 目前仍写「HEAD 已到 0cca5cb」，实际已到 `5521150`；这不影响协议判断，下次跟进时顺手改掉。
- `opencode2api` 本次实跑有一次 TLS `UNEXPECTED_EOF`（重试后成功），属网络抖动，非上游问题。

许可证：本次巡检未重新读取 `LICENSE`，沿用当天上午 `gh api repos/<repo>` 的结论——未发现任何许可证变更，GitHub 报告的 `license` 与清单一致（MIT / GPL-3.0 / LGPL-3.0 / `none`）。两处需要人工解释：

- `NextAgentX/qoder-trae-workbuddy-switch` 在 GitHub API 里是 `NOASSERTION`（GitHub 的许可证识别器不认 PolyForm），以仓内 `LICENSE` 文件首行为准，仍是 **PolyForm Noncommercial 1.0.0**，商用限制不变。
- `ColudAI/Deepseek2Go` 无 `LICENSE` 文件（raw 取回 404），仍是默认 All Rights Reserved，只允许 sidecar 运行。

## 许可证变化与退出策略

### 需要立刻停下来处理的信号

- `LICENSE` 文件消失，或从明确许可证变成"未声明"。
- MIT / Apache-2.0 → GPL / AGPL / LGPL / PolyForm / BUSL / 自定义限制性协议。
- 新增"禁止商用""禁止再分发""禁止用于竞品"等条款。
- README 增加与 `LICENSE` 冲突的声明（以 `LICENSE` 为准，但要记录冲突）。

### 按接入形态退出

| 形态 | 风险 | 退出动作 |
| --- | --- | --- |
| `in-repo-rewrite` | 低（只要没复制代码） | 保留自研实现；把 `integration` 改成 `not-integrated`，`notes` 写明退出原因与日期；删除契约文档里对上游代码结构的引用。 |
| `sidecar` | 中（分发/部署上游二进制） | 停止在新版本中附带 sidecar 获取说明；`providers/<name>/README.md` 标注"已因许可证变更移除"；必要时从 `deploy/` 与 `docker-compose.yml` 移除服务；把 `integration` 改成 `not-integrated`。 |
| `reference-only` | 低 | 停止引用；`integration` 改成 `not-integrated`。 |

### 已发布版本的处置

1. 立即停止更新基线（不要 `-Update`）。
2. 在 `THIRD-PARTY-NOTICES.md` 记录变更日期、旧许可证、新许可证、影响范围。
3. 评估已发布版本是否需要撤回：若只是 sidecar 获取方式，移除文档即可；若已分发二进制，需要发布新版本并说明。
4. 如果上游从 MIT 改成限制性协议且我们已经 clean-room 重写过，**自研实现可以保留**——前提是确认没有复制上游代码。保留证据（契约文档 + 提交记录）。

### 当前的已知风险条目

| 仓库 | 风险 |
| --- | --- |
| `jasonxu114514/opencode2api` | GPL-3.0。主仓必须保持 clean-room 自研（`adapters/opencode.py`），不得引入上游代码。 |
| `Wei-Shaw/sub2api` | LGPL-3.0。不得链接上游库；`adapters/sub2api.py` 为自研。 |
| `NextAgentX/qoder-trae-workbuddy-switch` | PolyForm Noncommercial 1.0.0，禁止商用。`qoder_sidecar.py` 只能用于非商业场景；商用走自研 `qoder.py`。 |
| `ColudAI/Deepseek2Go` | 无 LICENSE，默认 All Rights Reserved。只允许以 sidecar 运行，不分发二进制、不复制源码。 |

## 记录规范

每次跟进在 `upstreams.yaml` 的 `notes` 里留一句结论，例如：

```yaml
notes: "2026-11-02 跟进 1cd0168：新增 /v1/images，未接入；chat 协议无变化。"
```

不要写「已更新」这种没有信息量的话。半年后回看时，需要知道当时判断了什么。

巡检只写 `checked_at` 与 `notes`；`analyzed_commit` 只在真正读完新 commit 后才推进，并在 notes 里写明推进原因。

## 常见误区

- **把 `git ls-remote` 的成功当成"协议没变"。** 它只能说明有/没有新 commit。
- **只看 README 判断许可证。** 以 `LICENSE` 文件为准；README 声明只作为补充证据。
- **`-Update` 之后才看 diff。** 顺序反了会永久丢失更新提示。
- **sidecar 升级后不测错误码。** 很多上游在升级时悄悄改了 429/402 的语义。
- **把上游代码"参考"进适配器。** 一旦复制，GPL/LGPL 仓的许可证就会传导到主仓。
