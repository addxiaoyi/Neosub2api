# codar2api 协议契约与接入结论（clean-room 提炼）

> 结论先行：**sidecar**。codar2api 自身是 MIT 的独立 Go 网关，对外已经提供
> OpenAI Chat / Responses 与 Anthropic Messages 端点；它的主要工作量在 Codar 账号
> 登录、令牌续期、签到与代理池，这些能力依赖 Codar 私有接口，主仓 clean-room 重写
> 既不必要也不稳定。主仓只需一个 HTTP 代理适配器，不复制、不内置上游源码或二进制。
>
> 依据：`AiXiaoc/codar2api` 在 commit `d7f50246c49ef5cc3df8eb014e79397cfdd0f8b5`
> （2026-11-01，发布 v0.1.3）的仓库内容：README、LICENSE、go.mod，以及
> `internal/gateway/` 下的路由、鉴权、转发、调度、上游与用量模块。
> 本文件只记录协议与行为事实，不包含上游源码片段。主仓实现：**尚未落地**，
> 计划文件见第 6 节。

## 0. 项目事实

| 项 | 观测值 | 证据 |
| --- | --- | --- |
| 语言 / 框架 | Go 1.26.4，仅标准库 | `go.mod:1-3`；README「没有第三方 Go 依赖」 |
| 形态 | 单进程 Web 网关，管理面板静态资源编译进二进制 | `internal/gateway/web/`；README 架构图 |
| 上游是什么 | 科搭 Codar（SpeedAI）账号背后的私有 API | 默认 `https://api.claw.speedai.com`（`internal/gateway/app.go:49`）；账号/余额走 `https://api.kuaipaper.com`，网页会话走 `https://codar-web.speedai.com`（README 配置表） |
| 对外协议 | OpenAI Chat / Responses、Anthropic Messages、Anthropic count_tokens | `internal/gateway/app.go:196-202` 的 `isChat()`；README API 端点表 |
| 客户端鉴权 | 本地 API Key：`Authorization: Bearer` 或 `x-api-key`，前缀 `sk-codar-` | `internal/gateway/auth.go:333-340`、`auth.go:259-260` |
| 管理端鉴权 | 管理员密码 → PBKDF2-HMAC-SHA256；Cookie 会话 | `auth.go:19-35`；README「安全与合规」 |
| 存储 | 数据目录内 JSON/JSONL 文件，原子替换写入；无外部数据库 | README「数据卷」表；`auth.go:82` 读 `keys.json`；`migrate.go:49` 列出数据文件 |
| 部署 | Dockerfile + compose，单容器，数据卷；默认映射 `127.0.0.1:8801` | `compose.yaml`、`Dockerfile`；README「快速开始」 |
| 许可证 | **MIT** | `LICENSE:1-3`（MIT License, Copyright (c) 2026 AiXiaoc） |

独有能力（相对纯转发网关）：

- 账号入口：微信扫码、手机号验证码、粘贴 JWT、读取本机 Codar 客户端 `config.json`
  （`internal/gateway/login.go:33-54`、`login.go:217` 起；README「账号管理」）。
- 后台任务：每日签到、领取免费模型活动、令牌到期前续期、余额/活动刷新
  （`internal/gateway/upstream.go:448`、`:467`、`:478`）。
- 出口管理：HTTP/HTTPS/SOCKS5/SOCKS5H 代理池、账号绑定、轮询出口、
  403 拦截页识别为「出口 IP 封控」（`internal/gateway/upstream.go:59-81`）。
- 按账号并发与权重：四种调度策略、账号级 `max_concurrency`（`scheduler.go:50-127`）。

## 1. 对外协议与端点

| 方法 | 路径 | 说明 | 证据 |
| --- | --- | --- | --- |
| GET | `/v1/models` | 当前 Key 可见模型；按 Key 的 `models` 白名单过滤 | `app.go:292-312` |
| POST | `/v1/chat/completions` | OpenAI Chat Completions，JSON 或 SSE | `app.go:196-202`、`app.go:280-330` |
| POST | `/v1/responses` | OpenAI Responses，JSON 或 SSE | 同上；README API 表 |
| POST | `/v1/messages` | Anthropic Messages，JSON 或 SSE | 同上 |
| POST | `/v1/messages/count_tokens` | Anthropic token 计数，不计入统计 | 同上；README「流式行为细节」 |
| GET | `/ping`、`/health` | 健康检查，无需鉴权，不返回账号数据 | `app.go:234-237` |
| POST | `/auth/login` | 管理面板登录，同源校验 | `app.go:258-278` |

协议转换不在网关内做：四个模型端点把同一路径拼到上游基址后原样转发
（`relay.go:261-263`），OpenAI 与 Anthropic 的差异由 Codar 上游自行处理
（README「多协议转发」）。因此对客户端而言，它就是标准的双协议上游。

## 2. 鉴权

| 场景 | 形态 | 证据 |
| --- | --- | --- |
| 模型 API | `Authorization: Bearer <key>`，缺失时读 `x-api-key` | `auth.go:333-340` |
| 本地 Key 生成 | `sk-codar-` + 32 字节随机数的 hex（`random(32)`） | `auth.go:259-260` |
| 本地 Key 校验 | 请求 key 做 SHA-256 后与库内 `hash` 常量时间比较；同时校验启停、到期、模型白名单 | `auth.go:344-361` |
| 本地 Key 存储 | 完整 key 留在 `keys.json` 以便面板复制；列表接口不返回完整 key | `auth.go:20`、`auth.go:260`、`auth.go:304` |
| 管理端 | 管理员密码 PBKDF2-HMAC-SHA256（21 万次迭代）+ 随机盐；会话 Cookie | `auth.go:19-35`；README「安全与合规」 |
| 上游凭证 | 账号 JWT 以 `Authorization: Bearer` 注入转发请求 | `relay.go:277`；`upstream.go:157-159` |
| 令牌刷新 | 无 OAuth；`POST /v1/token/refresh` 走 Codar 付费服务刷新 JWT | `upstream.go:478` |

关键点：客户端 key 只用于「客户端 → codar2api」；Codar 账号 JWT 由网关持有，
不向客户端透出。管理面板会话不能代替模型 API Key（`app.go:280-286`）。

## 3. 请求 / 响应核心字段

### 3.1 透传与改写

- **自动透传默认开启**：未被规则修改的 JSON 正文按原始字节转发，保留扩展字段、
  数字精度、查询参数与大部分普通请求头；鉴权头被替换为账号 JWT，以下头被过滤：
  `authorization`、`x-api-key`、`api-key`、`cookie`、代理鉴权、连接控制头
  （`internal/gateway/forwarding.go:45-54`；README「自动透传」）。
- **自定义 UA**：默认 `codar-desktop/0.3.3`；关闭开关后 API 沿用调用方 UA
  （`internal/gateway/useragent.go:3-13`）。
- **Anthropic 版本头**：请求 `/v1/messages` 且调用方未带 `anthropic-version` 时，
  网关补 `2023-06-01`（`relay.go:292-294`）。
- **身份提示词改写**：可选地把系统/开发者提示词开头的固定文本替换为中性文本，
  或在首行前加 `.` + 零宽空格（`internal/gateway/compat.go:5-9`、`compat.go:124-152`）。
  这是 codar2api 的独有行为，不属于 OpenAI/Anthropic 标准。

### 3.2 Models

`GET /v1/models` 返回：

```json
{"object":"list","data":[{"id":"MODEL_ID","object":"model","owned_by":"codar"}]}
```

只暴露 `id`/`object`/`owned_by`（`app.go:308`），不返回上游的价格、上下文长度等
元数据；这些元数据只保留在管理面板的模型页。

### 3.3 流式事件

- OpenAI 路由：`text/event-stream`，逐字节透传上游 SSE；识别 `data: [DONE]` 作为终态。
- Anthropic 路由：`event: <type>` + `data: {json}`；识别 `message_stop` 作为终态。
- Responses 路由：识别 `response.completed` 作为终态。
- 终态识别与用量解析在 `internal/gateway/usage.go:69-107`；流式管道在
  `relay.go:412-449`，32 KiB 缓冲、逐块 flush、命中终态即结束，不等待连接关闭。
- 流式 usage：OpenAI 从末 chunk 的 `usage` 提取；Anthropic 从 `message_start` /
  `message_delta` 的 usage 提取（`usage.go:21-39` 兼容 `prompt_tokens`/`input_tokens`、
  `completion_tokens`/`output_tokens`、`cached_tokens`/`cache_read_input_tokens`）。
- 响应头：`X-Codar-Account` 返回本次账号名，`X-Request-Id` 透传上游请求 id
  （`relay.go:406-411`、`app.go:225`）。

### 3.4 请求体与错误体

- 请求体上限 **8 MiB**（`app.go:165`）。
- 网关自身错误体为
  `{"ok":false,"error":{"message":"...","type":"gateway_error"}}`（`app.go:157-158`），
  不是 OpenAI/Anthropic 标准错误结构；上游错误则原样回写状态码与正文
  （`relay.go:370-376`）。

## 4. 错误码、调度与限流

### 4.1 状态码

| 状态 | 语义 | 行为 | 证据 |
| --- | --- | --- | --- |
| 401 | 无/无效/停用/过期 API Key；上游令牌失效 | 本地直接拒绝；上游 401 标记该账号令牌失效并换号 | `auth.go:351-356`、`relay.go:68-77` |
| 402 | 余额不足 | 账号余额冷却，只暂停收费模型；换号 | `relay.go:84-88` |
| 403 | 账号受限 `account_restricted` | 标记受限并持久保存，换号 | `upstream.go:46-53`、`relay.go:55-62` |
| 403 | 出口 IP 被边缘节点拦截（HTML 而非 JSON） | 标记当前出口封控，换其他出口账号 | `upstream.go:59-81` |
| 404 | 面板资源不存在 | 直接返回 | `app.go:341-343` |
| 429 | 全局/账号并发已满；上游限流 | 并发满返回 `Retry-After: 1`；上游 429 冷却并换号 | `app.go:323-329`、`relay.go:219-222`、`relay.go:80-83` |
| 502 | 上游不可达、模型目录获取失败、上游非 JSON | 原样/明确报错 | `app.go:294-296`、`upstream.go:180-195` |
| 503 | 池内无可用账号；`No available channel` | 无账号时 503；后者是模型级错误，不换号 | `relay.go:50-52`、`relay.go:391` |
| 400/413/422 | 请求级错误 | 不换号，原样返回 | `relay.go:53`、`relay.go:370-376` |

### 4.2 换号策略

`retryable()` 的判定顺序（`relay.go:44-54`）：

1. 响应体出现 `account_restricted` → 可重试（先标记受限）。
2. `503` 且正文含 `no available channel` → **不重试**，直接返回。
3. 状态 `0`（网络错误）、`401`、`402`、`429`、`>=500` → 可重试。
4. 其他（400/403/413/422 等）→ 不重试。

单次请求最多尝试账号数默认 3（`store.go:174`；README「错误分类」）。
流式响应一旦开始输出就不再换号（`relay.go:378-380`）。

### 4.3 调度与冷却

| 项 | 语义 | 证据 |
| --- | --- | --- |
| 策略 | `round_robin`（默认）、`weighted_round_robin`、`sticky`、`least_connections` | `scheduler.go:83-115` |
| 粘性会话标识 | 头 `X-Session-Id` / `X-Conversation-Id` / `Session-Id`；体 `session_id` / `conversation_id` / `prompt_cache_key` / `metadata.user_id`；最后用首条用户消息摘要 | `relay.go:14-43` |
| 账号可用性 | 关闭、受限、无令牌、令牌失效、冷却中、出口封控、并发满、收费模型余额 ≤ 0 均跳过 | `store.go:615-670` |
| 冷却时长 | 429 → 60s；402/余额 → 600s；网络/5xx → 30s（均可在面板改） | `store.go:171-173`、`relay.go:78-88` |
| 全局并发 | `CODAR_MAX_CONCURRENT` 默认 128；超出 429 | `cmd/codar/main.go:73`、`app.go:323-329` |
| 账号并发 | `max_concurrency` 默认 0（不限）；满了 429 + `Retry-After: 1` | `scheduler.go:50-53`、`relay.go:219-222` |

## 5. 我方接入形态判定

| 方案 | 可行性 | 代码证据 | 结论 |
| --- | --- | --- | --- |
| 主仓 clean-room 重写适配器 | **低** | 真正的上游不是 OpenAI 兼容服务，而是 Codar 私有接口：`/api/user/self`（`upstream.go:287`）、`/v1/gettoken`（`upstream.go:356`）、`/v1/daily-sign`（`upstream.go:448`）、`/api/codar/activities/free-model/claim`（`upstream.go:467`）、`/v1/token/refresh`（`upstream.go:478`）、`/v1/codar/usage/daily`（`upstream.go:636`）；登录还包含微信扫码/短信/新 IP 验证（`login.go:33-54`、`login.go:217`）。这些都没有公开协议规范，且会随 Codar 客户端版本变化 | 不做 |
| **sidecar（推荐）** | **高** | 上游已经暴露标准 OpenAI/Anthropic 端点（第 1 节），且为 MIT（`LICENSE:1-3`）；主仓只需 HTTP 代理，不需要理解 Codar 私有协议 | **采用** |
| 独立工具 | 中 | codar2api 自身可独立部署；但绕开控制面后，网关 Key、用量归因、模型别名都不生效 | 不采用 |
| 不建议接入 | 可行但浪费 | 与 codebuddy2api / qoder-switch 的账号池 + 双协议能力高度重叠（第 8 节） | 不作为主方案 |

选择 sidecar 的合规前提：上游源码/二进制**不进入主仓**，适配器只做 HTTP 调用；
`codar2api` 由用户在外部目录自行 clone 与启动。

## 6. 计划新增文件与能力位

> 本轮只做分析，未改动 `apps/control/app/`。以下为落地时需要新增/改动的文件。

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `apps/control/app/adapters/codar_sidecar.py` | 新增 | `ProviderSpec(id="codar_sidecar")` + 继承 `OpenAICompatAdapter`；`messages()` 转发 `/v1/messages`；默认 Base URL `http://127.0.0.1:8801` |
| `apps/control/app/adapters/__init__.py` | 改动 | 导入 `codar_sidecar` 并加入 `BUILDERS` |
| `providers/codar2api/README.md` | 新增（可选） | 上游参考来源与本地启动说明，格式对齐 `providers/trae2api/README.md` |
| `docs/providers/codar2api.md` | 新增 | 本文件 |

能力位（`adapters/base.py:6-16` 的 `CAPABILITY_KEYS`）：

| 能力位 | 取值 | 理由 |
| --- | --- | --- |
| `chat` | true | `/v1/chat/completions` 直通 |
| `messages` | true | `/v1/messages` 直通，含 SSE |
| `models` | true | `/v1/models` 返回 `data[].id` |
| `responses` | false | 上游有 `/v1/responses`，但本批不实现，不声明假能力 |
| `oauth` | false | 登录在 sidecar 内完成，主仓不参与 |
| `checkin` | false | 签到在 sidecar 内自动执行，没有对外 API |
| `credits` | false | 余额在 sidecar 内管理，没有对外 API |
| `register` | false | 上游没有批量注册；账号由人工扫码/短信/粘贴令牌导入 |
| `sidecar` | true | 独立进程 |

`ProviderSpec` 其它字段建议：`runtime="独立进程 sidecar（Go）"`、`license="MIT"`、
`risk_level="safe"`、`protocols=("openai.chat.completions", "anthropic.messages")`。

## 7. 与现有 10 家的能力重叠

| 现有上游 | 与本上游的重叠点 | 重叠程度 |
| --- | --- | --- |
| codebuddy2api | 账号池 + OpenAI/Anthropic 双协议 + 管理面板 + 签到/额度 | **高** |
| qoder-trae-workbuddy-switch | 多账号网关 + 双协议 + 429 配额语义 | **高** |
| sub2api | 账号池、配额/冷却、429 确定性失败 | 中（codar2api 没有对外分组配额分发） |
| muse2api / gptGrok2api / ZCode2api | 只是 OpenAI/Anthropic 兼容上游 | 中（模型来源不同） |
| trae2api | OpenAI 兼容 sidecar | 中（协议面相同，账号体系不同） |
| opencode2api | 无 | 低 |
| grok-register | 都涉及账号获取，但 Codar 只有人工登录，没有批量注册编排 | 低 |
| Deepseek2Go | 无（PoW/登录/协议都不同） | 无 |

结论：codar2api 不提供新的模型能力，提供的是 **Codar 账号自动化**。如果主仓只需要
「多一个 OpenAI/Anthropic 上游」，sidecar 是最低成本；如果目标是复用它的签到/领免费
模型/续期，则必须接受这些能力留在 sidecar 内，主仓不重复实现。

## 8. 我方实现需要对齐的最小集

1. `GET /v1/models`：转发并解析 `data[].id`；空列表视为「在线但无模型」，不报不可达。
2. `POST /v1/chat/completions`：JSON 与 SSE 直通，不改写响应体、不重算 usage。
3. `POST /v1/messages`：原样转发；流式与非流式都由上游处理，主仓不做协议转换。
4. 鉴权：客户端 key 由控制面网关管理；sidecar key 走 provider `api_key`，
   以 `Authorization: Bearer` 注入，不进入请求体、不返回前端。
5. 错误：401/402/429/5xx 原样回传状态码与正文；不把 sidecar 的 429 改写成 200。
6. 健康检查：`GET /health` 或 `/ping`（无需 key），区分「sidecar 不在线」与
   「在线但账号池为空」（后者表现为 `/v1/models` 空列表或 503）。
7. 不重试：sidecar 已经做账号级换号与冷却，适配器不再换 key、不再重放请求，
   避免一个请求在两层各重试 3 次。

## 9. 不做 / 待确认

- 不复刻 Codar 登录（微信扫码、短信、新 IP 验证）、签到、免费模型领取、令牌续期、
  代理池、管理面板。
- 不把 codar2api 源码或编译产物放进主仓；sidecar 由用户自行部署。
- **未用真实 Codar 账号验证**：本轮没有可用凭证，端点与字段来自上游代码与 README；
  真实账号下的模型清单、`tier`/余额判断、403 封控粒度仍需实测确认。
- `POST /v1/responses` 上游支持但主仓本批不声明；后续要接，需单独实现
  `forward("/v1/responses")` 并用真实 SSE 验证。
- Codar 平台的服务条款限制「仅管理本人名下账号」，转售/共享风险由部署方承担；
  这一点不因上游是 MIT 而消失。
- 面板创建的 Key 必须把模型范围设为「全部」或包含实际使用的模型，否则
  `/v1/chat/completions` 会以 403 拒绝（`auth.go:357-361`、`app.go:319-321`）。
