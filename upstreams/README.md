# 上游追踪清单

这份清单记录 `unified-2api` 参考、代理或重写的上游仓库，以及我们**实际读过**的 commit。
它解决两个问题：

1. 谁是我们能力的来源，当时读的是哪一版。
2. 上游动了之后，哪些改动只是文档层面，哪些必须动适配器。

清单本身不是许可证公告。许可证以各仓 `LICENSE` 文件为准，商业使用前请先看 `THIRD-PARTY-NOTICES.md`。

## 清单结构

`upstreams.yaml` 顶层三个键：

| 键 | 说明 |
| --- | --- |
| `version` | 清单格式版本，当前为 `1`。字段结构变更时才递增。 |
| `updated_at` | 整份清单最后一次人工确认的日期（`YYYY-MM-DD`）。 |
| `upstreams` | 仓库记录列表，见下。 |

单条记录字段：

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `repo` | `owner/name` | GitHub 仓库全名。脚本靠它拼 `git ls-remote` 地址。 |
| `license` | SPDX 风格字符串或 `unknown` | 从上游 `LICENSE` 文件标题提取，不做美化。 |
| `license_source` | `LICENSE 文件` / `README 声明` / `未声明` | 许可证结论的证据来源。`未声明` 时 `license` 必须是 `unknown`。 |
| `integration` | `in-repo-rewrite` / `sidecar` / `reference-only` / `not-integrated` | 我们和它的关系，见下一节。 |
| `analyzed_commit` | 40 位 SHA 或 `unknown` | 分析时 `git rev-parse HEAD` 得到的真实值，不手填、不推测。 |
| `analyzed_at` | `YYYY-MM-DD` | 首次把该 commit 读完的日期。`-Update` 时同步刷新。 |
| `our_files` | 相对主仓根的文件列表 | 我们对应实现的适配器、服务、路由与契约文档。不存在的能力不要写。 |
| `protocols` | 协议标识列表 | 如 `openai.chat.completions`、`anthropic.messages`、`openai.responses`。没有对外协议就写 `[]`。 |
| `notes` | 字符串 | 语言栈、注意事项、许可证风险、未落地计划。写事实，不写猜测。 |

### integration 四种形态

- **in-repo-rewrite**：只看协议，主仓自研实现。上游若为 GPL/LGPL，本仓实现必须保持 MIT 且不链接上游代码。
- **sidecar**：上游作为独立进程运行，本仓通过 HTTP 代理。适合上游依赖私有签名 / 登录态 / 浏览器环境、无法干净重写的情况。**不要把上游源码或二进制放进主仓**，`providers/<name>/README.md` 只写获取与启动方式。
- **reference-only**：只读它的协议做设计参照，本仓暂不接入。
- **not-integrated**：评估后决定不接，保留记录避免重复调研。

## 日常怎么用

### 手动检查是否有更新

```powershell
# 主仓根目录执行
powershell -File scripts/check-upstreams.ps1
```

输出三态：`一致` / `有更新` / `无法访问`。默认只读，不改文件。

### 确认要跟进后更新基线

```powershell
powershell -File scripts/check-upstreams.ps1 -Update
```

`-Update` 只会改写**能访问且确有更新**的那些记录：

- `analyzed_commit` → 远端当前 HEAD
- `analyzed_at` → 今天

**它不负责读代码。** 更新基线只表示"我已经看过这个 commit"。正确顺序是先按 `docs/UPSTREAM-SYNC.md` 走完跟进流程，再执行 `-Update`。

### 给 CI 用

```powershell
powershell -File scripts/check-upstreams.ps1 -Json
```

输出单行 JSON 数组，每项含 `repo`、`status`、`analyzed_commit`、`latest_commit`、`behind`、`message`。
`status` 取值：`ok` / `outdated` / `unreachable`。`behind` 仅在能拿到 GitHub compare 结果时是数字，否则为 `null`。

### 指定别的清单文件

```powershell
powershell -File scripts/check-upstreams.ps1 -Manifest ..\other.yaml
```

## 更新流程（摘要）

1. `check-upstreams.ps1` 看哪些仓 `有更新`。
2. 按 `docs/UPSTREAM-SYNC.md` 判定更新性质：只改文档 / 要改适配器 / 触发退出。
3. 需要时在**临时目录**浅克隆新版本，只读协议，更新 `docs/providers/<name>.md`。
4. 改完适配器后跑回归（见 `docs/UPSTREAM-SYNC.md` 的验证清单）。
5. `check-upstreams.ps1 -Update` 刷新基线。
6. 同一次提交里更新 `upstreams.yaml`、契约文档、`THIRD-PARTY-NOTICES.md`（若许可证有变）。
7. 顺手把顶层 `updated_at` 改成今天。

## 维护约定

- YAML 是**受控格式**，脚本用正则逐行读。别改成多行数组、别调整缩进、别加列表项内注释。
- `analyzed_commit` 必须是 `git rev-parse HEAD` 原样输出。拿不到就写 `unknown`，在 `notes` 里写清楚原因。
- 新增仓库时同时补 `our_files` 和 `protocols`，否则同步时无法判断影响面。
- `license` 变化（尤其 MIT → 限制性协议）必须**先**更新 `THIRD-PARTY-NOTICES.md` 并决定退出策略，再改本清单。

## 已知局限

- 主仓当前不是 git 仓库，`analyzed_commit` 只针对上游，不记录本仓版本。
- `git ls-remote` 取到的是默认分支 HEAD，不是 tag；上游发版不回主干时可能漏报。
- GitHub API 兜底受匿名速率限制（每小时 60 次），11 个仓一次检查足够，频繁跑会被限流。
- `behind` 依赖 GitHub compare API，需要清单里有可比较的基线 SHA；`unknown` 或 API 失败时返回 `null`。
