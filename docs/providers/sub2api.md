# sub2api 协议契约（clean-room 提炼）

> 依据：上游公开 README（订阅配额分发平台）、`docs/COMPOSITE_GROUPS.md`、`docs/admin-api-keys.md`，
> 以及 OpenAI / Anthropic 公开协议说明与实际请求观察。本文件只记录协议事实，不含上游源码。
> 主仓实现：`apps/control/app/adapters/sub2api.py` + `sub2api_store.py`（独立重写，MIT）。

## 1. 对外协议与端点

sub2api 是对外暴露「用户 API Key」的 AI 网关，内部维护分组、账号池与订阅配额。
对外推理端点按 OpenAI / Anthropic 协议兼容：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/chat/completions` | OpenAI Chat Completions，JSON 或 SSE |
| POST | `/v1/messages` | Anthropic Messages，JSON 或 SSE |
| POST | `/v1/responses` | OpenAI Responses（复合分组可按 `responses` 路由域解析） |
| POST | `/v1/messages/count_tokens` | Anthropic token 计数 |
| GET | `/v1/models` | 当前 key 可用模型 |
| POST | `/v1/embeddings`、`/v1/images/*` | 按分组与账号平台能力开放 |
| 其他 | `/v1beta/models/{model}:generateContent` | Gemini 原生路径，按 `{model}` 解析路由 |

管理面（不是推理协议）：

- `GET /api/v1/admin/groups/:id/composite-routes` 及其 POST/PUT/DELETE/preview。
- 受限管理员密钥通过 `x-api-key` 调用，仅开放权限目录登记过的接口。
- 普通管理员使用 JWT 会话；受限密钥不能管理自身或创建其他管理员密钥。

## 2. 鉴权

| 场景 | 形态 |
| --- | --- |
| 用户调用模型 | `Authorization: Bearer <user api key>` 或 `x-api-key: <user api key>` |
| 受限管理员 API | `x-api-key: <restricted admin key>`，按资源规则逐项授权 |
| 管理控制台 | 管理员 JWT 会话 |
| 刷新机制 | 无 OAuth 刷新；用户 key 由平台创建/轮换，管理员受限 key 支持轮换与撤销 |

上游账号凭证（订阅账号的 cookie/token）保存在平台账号池内，不向客户端返回。

## 3. 请求 / 响应核心字段

### 3.1 请求

标准 OpenAI / Anthropic 请求体。平台在分发前做两项改写：

1. **分组解析**：用户 key 绑定分组（或复合分组），分组决定可用账号平台与配额。
2. **模型改写**：复合分组的显式路由把客户端 `model`（`public_model`）改写为 `upstream_model` 再分发。

复合路由字段：

| 字段 | 取值 | 语义 |
| --- | --- | --- |
| `public_model` | 字符串 | 客户端请求的模型名 |
| `match_type` | `exact` / `prefix` | 匹配方式 |
| `target_platform` | `anthropic` / `openai` / `gemini` / `grok` / `kimi` / `glm` / `deepseek` / `minimax` / `antigravity` | 目标平台 |
| `upstream_model` | 字符串，可空 | 为空时复用 `public_model` |
| `endpoint` | `any` / `messages` / `count_tokens` / `responses` / `chat_completions` / `embeddings` / `images` / `gemini` | 路由域 |
| `priority` | 整数 | 数值小者优先 |
| `enabled` | 布尔 | 关闭后不参与运行时解析 |

解析顺序：显式路由优先于内置识别；多条命中时依次比较 exact > prefix、endpoint 精确 > any、
更长前缀、更小 priority、更小 route id。未知/歧义模型 **fail closed**（返回客户端错误，不猜平台）。

### 3.2 响应与 usage

响应保持上游原协议结构；`usage` 原样返回。平台基于 usage 做后付费计量与配额扣减。
流式与 OpenAI / Anthropic 标准一致：

- OpenAI：`data: {json}` … `data: [DONE]`，usage 通常在末个 chunk。
- Anthropic：`event: <type>` + `data: {json}`，usage 在 `message_start` 与 `message_delta`。

## 4. 订阅配额语义

这是 sub2api 与普通 OpenAI 兼容网关的核心差异：

1. **分组（group）**：用户 key → 分组；分组有订阅类型（`subscription` 或按量）、平台（含 `composite`）。
2. **账号池（account pool）**：每个分组绑定一组上游账号；请求按账号可用性选择。
3. **配额扣减**：请求完成后按 usage 扣减分组配额；订阅计划授予的是「分组访问权」，
   每次请求仍按解析出的具体平台做配额检查与计费。
4. **复合分组**：一个分组可跨平台路由，但计费/配额仍落到解析出的具体平台，不额外生成价格记录。

## 5. 错误码与限流

| 状态 | 语义 |
| --- | --- |
| 401 | 用户 key 无效 / 缺失 |
| 403 | key 无权访问该分组、账号被禁用、平台不匹配 |
| 404 | 模型/路由不存在（未知模型 fail closed 时也可能返回 4xx 客户端错误） |
| 429 | 配额耗尽、账号池无可用账号（全部冷却）、上游限流 |
| 502 / 503 | 上游不可达或账号池未配置 |
| 5xx | 上游错误 |

限流/配额行为要点：

- 配额耗尽是 **确定性 429**，不应回落到其他分组或静默换 key。
- 账号级限流会触发该账号冷却，平台在冷却期内改用池内其他账号；全部冷却时对外 429。
- 复合分组按 `target_platform` 分别做配额检查，不能用 A 平台余额抵扣 B 平台请求。

## 6. 我方落库模型（自设计，不复用上游表结构）

主仓用 SQLite 三张表实现同一语义（表定义见 `apps/control/app/db.py`）：

| 表 | 关键字段 | 作用 |
| --- | --- | --- |
| `sub2_groups` | `id`、`name`、`platform`、`quota_limit`、`quota_used`、`reset_at`、`enabled` | 分组与总配额；`quota_limit=0` 表示不限 |
| `sub2_accounts` | `group_id`、`upstream_base`、`api_key`、`weight`、`quota_limit`、`quota_used`、`enabled`、`cooldown_until`、`last_used_at` | 账号池、账号级配额与冷却 |
| `sub2_ledger` | `group_id`、`account_id`、`model`、`prompt_tokens`、`completion_tokens`、`cost`、`status` | 每次请求的扣减流水 |

选择算法：同组内 `enabled=1`、未冷却、账号额度未耗尽，按 `last_used_at ASC, weight DESC, id ASC`
取一个；请求失败（429/5xx）写冷却并换下一个账号，最多 3 次。扣减单位为主仓统一口径的 token 数。

## 7. 我方实现需要对齐的最小集

1. `POST /v1/chat/completions` 与 `POST /v1/messages`：转发到分组所选账号的 `upstream_base`，
   JSON 与 SSE 直通。
2. `GET /v1/models`：从池内可用账号发现模型；全部不可用返回空列表（不假装在线）。
3. 分组解析：provider `options.group` 指定默认分组；未配置返回 503，不静默成功。
4. 账号选择、冷却与重试：只对 429/5xx 换账号，400/413/422 立即返回。
5. 配额扣减：非流式从响应 `usage` 扣减；流式在流结束时从含 usage 的 SSE 帧扣减。
6. 429：分组额度耗尽或账号池无可用账号时返回 429，并写 `sub2_ledger`（`status=429`）。
7. 管理 API：分组、账号池、流水与重置额度的 CRUD（`/api/sub2api/*`，管理员会话）。
8. 不做：复合路由注册表、多平台价格表、支付/订阅计划、Gemini 原生路径（后续批次再评估）。

## 8. 明确不做（2026-11-07 拍板）

> 原句逐字留档：「sub2api 实时 —— 不做。」「#5 sub2api 图片异步/tasks/batches 也不做 —— 上游只有 `images/generations|edits`，没有 tasks/batches 端点，本地另造异步语义属自造契约。」

- **实时（Realtime）**：上游只有 `GET /v1/responses` 的 WebSocket 升级说法，不是 OpenAI Realtime 语义；本文 §1 记录的对外端点是 `POST /v1/responses`（HTTP，JSON 或 SSE），本仓适配器也按 HTTP 转发（`apps/control/app/adapters/sub2api.py:241-242`）。本仓没有 WS 入口（`apps/control/app/routers/v1.py` 的路由全为 HTTP，全仓无 `@router.websocket` 注册）。**上游未提供契约，本方不实现；若将来需要，须先有上游一手契约。**
  误判后果：按「待实现」排期会自建 WS 入口与另一套事件模型，客户端按 OpenAI Realtime 的事件名与会话生命周期对接不会兼容。
- **图片异步 / tasks / batches（#5）**：§1 端点清单（第 19 行）只有 `/v1/embeddings` 与 `/v1/images/*`，没有 tasks / batches；本仓适配器也只声明并转发 `images_generations` / `images_edits`（`apps/control/app/adapters/sub2api.py:29-30,74,248,251`），没有 `image_task`。**上游未提供契约，本方不实现；不排期。**
  误判后果：按「待实现」排期会在本地自建任务表与轮询协议（自造契约）；把网关的 `/v1/tasks`（`apps/control/app/routers/v1.py:1608-1664`）当 sub2api 异步图片，则协议不同源，调用方按 sub2api 语义对接拿不到预期的任务视图。
