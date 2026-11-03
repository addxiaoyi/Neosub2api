# codar2api 协议契约与接入结论（clean-room 提炼）

> 结论先行：**协议层自研，账号自动化留在 sidecar**。codar2api 自身是 MIT 的独立
> Go 网关，对外已经提供 OpenAI Chat / Responses 与 Anthropic Messages 端点；它的主要
> 工作量在 Codar 账号登录、令牌续期、签到与代理池，这些能力依赖 Codar 私有接口。
> 主仓已把「模型协议转发 + SSE + 多 token 轮换 + 401 续期」改为仓内直连
> （`apps/control/app/adapters/codar.py`），不复制、不内置上游源码或二进制；
> 扫码/短信登录、新 IP 验证、签到仍由 sidecar 完成。
>
> 依据：`AiXiaoc/codar2api` 在 commit `d7f50246c49ef5cc3df8eb014e79397cfdd0f8b5`
> （2026-11-01，发布 v0.1.3）的仓库内容：README、LICENSE、go.mod，以及
> `internal/gateway/` 下的路由、鉴权、转发、调度、上游与用量模块。
> 本文件只记录协议与行为事实，不包含上游源码片段。主仓实现：**已落地**，
> `apps/control/app/adapters/codar_sidecar.py` 与 `apps/control/app/adapters/codar.py`，
> 两者分界见第 10 节，接入点见第 6 节。
>
> **2026-11-02 更新**：协议层已改为仓内直连实现（`codar.py`），不再依赖 sidecar
> 的模型转发；扫码/短信登录、新 IP 验证、签到仍必须留在 sidecar。第 10 节给出逐项分界。

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
| 主仓自研协议层 | **高（限协议层）** | 模型端点没有协议转换：codar2api 把同一路径拼到 `APIBase` 后原样转发（`relay.go:260-263`），并用 Bearer 注入账号 JWT（`relay.go:275`）。因此只要有 token，就能用 Python 直接复现转发、SSE 与模型目录 | **已采用**（`codar.py`） |
| 主仓自研账号自动化 | **低** | `/api/user/self`（`upstream.go:287`）、`/v1/gettoken`（`upstream.go:356`）、`/v1/daily-sign`（`upstream.go:448`）、`/api/codar/activities/free-model/claim`（`upstream.go:467`）、`/v1/codar/usage/daily`（`upstream.go:636`）；登录包含微信扫码/短信/新 IP 验证（`login.go:33-54`、`login.go:217`）。无公开规范且随客户端版本变化 | 不做，留在 sidecar |
| **sidecar（保留）** | **高** | 上游已经暴露标准 OpenAI/Anthropic 端点（第 1 节），且为 MIT（`LICENSE:1-3`）；账号登录、签到、代理池仍由它承担 | **保留** |
| 独立工具 | 中 | codar2api 自身可独立部署；但绕开控制面后，网关 Key、用量归因、模型别名都不生效 | 不采用 |
| 不建议接入 | 可行但浪费 | 与 codebuddy2api / qoder-switch 的账号池 + 双协议能力高度重叠（第 8 节） | 不作为主方案 |

## 6. 本仓接入点（已落地）

两个适配器并存：`codar.py` 走仓内直连（推荐），`codar_sidecar.py` 保留为旁路。
上游源码/二进制不进入仓库。

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `apps/control/app/adapters/codar.py` | 已新增 | `CodarAdapter(id="codar")`；直连 `APIBase`：models / chat / messages / responses，多 token 轮换 + 401 续期；登录类方法继承基类 501 |
| `apps/control/app/adapters/codar_sidecar.py` | 已新增 | `CodarSidecarAdapter(id="codar_sidecar")`；chat/models 继承 `OpenAICompatAdapter`；`messages()` 转发 `/v1/messages`；默认 Base URL `http://127.0.0.1:8801` |
| `apps/control/app/adapters/__init__.py` | 已改动 | `codar` 与 `codar_sidecar` 均已导入并注册进 `BUILDERS`（`test_providers.py` 有断言覆盖） |
| `apps/control/app/config.py` | 已改动 | `Settings.codar_sidecar_base`，读 `CODAR_SIDECAR_BASE` |
| `apps/control/app/registry.py` | 已改动 | `ENV_BASE_FIELDS["codar_sidecar"] = "codar_sidecar_base"`，地址来源按 `env_base_pids` 判定 |
| `docker-compose.yml` / `.env.example` | 已改动 | 仅新增 `CODAR_SIDECAR_BASE` 条目与注释（默认关闭） |
| `providers/codar2api/README.md` | 已新增 | 上游地址、MIT、启动方式、端口、不改动上游源码 |
| `docs/providers/codar2api.md` | 本文件 | 协议契约、接入结论与分界 |

`codar.py` 能力位（`adapters/base.py:6-16` 的 `CAPABILITY_KEYS`）：

| 能力位 | 取值 | 理由 |
| --- | --- | --- |
| `chat` | true | 直连 `/v1/chat/completions` |
| `messages` | true | 直连 `/v1/messages`，补 `anthropic-version: 2023-06-01` |
| `models` | true | 读私有目录 `/v1/codar/models` |
| `responses` | true | 直连 `/v1/responses`；上游不实现时原样回 404/501，本地不伪造 |
| `oauth` / `checkin` / `credits` / `register` | false | 仍属 sidecar 的账号自动化 |
| `sidecar` | false | 仓内进程内实现 |

`codar.py` 的 `ProviderSpec`：`runtime="仓内 Python 适配器（无 sidecar）"`、
`license="MIT（仅参考上游公开行为，未复制源码）"`、`risk_level="safe"`、
`protocols=("openai.chat.completions", "anthropic.messages", "openai.responses")`。
`codar_sidecar` 的规格保持不变（`sidecar` 能力位为 true，`runtime` 为独立 Go 进程）。

## 7. 与现有 provider 的能力重叠

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

`codar.py`（仓内直连）已按以下口径实现；`codar_sidecar.py` 只覆盖其中 1-3 的透传部分。

1. `GET /v1/models`：读取私有目录 `/v1/codar/models` 并解析 `data[].id`；空列表视为
   「在线但无模型」，不报不可达。
2. `POST /v1/chat/completions`：JSON 与 SSE 直通，不改写响应体、不重算 usage。
3. `POST /v1/messages`：原样转发，补 `anthropic-version: 2023-06-01`；协议差异由 Codar
   上游处理，主仓不做协议转换。
4. `POST /v1/responses`：原样转发；上游不支持时把 404/501 原样回给调用方，不伪造成功。
5. 鉴权：客户端 key 由控制面网关管理；Codar 账号 JWT 走 provider `api_key` 或
   `options.tokens`，以 `Authorization: Bearer` 注入，不进入请求体、不返回前端。
6. 错误：401/402/403/429/5xx 原样回传状态码与正文；不把 429 改写成 200。
7. 多 token：默认轮询（`options.tokens` 与 `api_key`），单次请求最多尝试 3 个 token，
   与上游 `store.go:174` 的默认尝试数一致。
8. 401 续期：用失效 token 调 `POST {pay_base}/v1/token/refresh`，取 `data.token` 或
   `token` 替换后立即重试一次（依据 `upstream.go:469-477` 的 `renew()`）。
   `pay_base` 默认 `https://api.kuaipaper.com`，可用 `options.pay_base` 覆盖。
9. 不重试非 401 错误：400/403/413/422 等直接回传，避免同一请求在多层重复放大。

## 9. 不做 / 待确认

- 不复刻 Codar 登录（微信扫码、短信、新 IP 验证）、签到、免费模型领取、代理池、
  账号调度与管理面板；这些继续由 codar2api sidecar 承担。
- 不把 codar2api 源码或编译产物放进主仓；sidecar 由用户自行部署。
- **未用真实 Codar 账号验证**：本轮测试使用假上游。真实账号下的模型清单、`tier`/
  余额判断、403 封控粒度、`/v1/responses` 是否可用仍需实测确认。
- `codar_sidecar` 仍不声明 `responses` 能力位；仓内 `codar.py` 才声明并转发。
- Codar 平台的服务条款限制「仅管理本人名下账号」，转售/共享风险由部署方承担；
  这一点不因上游是 MIT 而消失。
- 面板创建的 Key 必须把模型范围设为「全部」或包含实际使用的模型，否则
  `/v1/chat/completions` 会以 403 拒绝（`auth.go:357-361`、`app.go:319-321`）。
  仓内直连不经过这层 Key，不适用该限制。

## 10. 仓内实现 vs 仍需 sidecar（2026-11-02）

| 能力 | 仓内 `codar.py` | 仍需 sidecar | 依据 |
| --- | --- | --- | --- |
| `GET /v1/models` | 已自研：`GET {base}/v1/codar/models` → `data[].id` | 否 | `app.go:285-305`、`upstream.go:210-245` |
| `POST /v1/chat/completions`（JSON） | 已自研：按原路径转发 + Bearer + UA | 否 | `relay.go:260-279` |
| `POST /v1/chat/completions`（SSE） | 已自研：逐块透传，不改帧 | 否 | `relay.go:412-449` |
| `POST /v1/messages`（含 SSE） | 已自研：原路径转发并补 Anthropic 版本头 | 否 | `relay.go:290-292` |
| `POST /v1/responses` | 已自研转发；上游未实现时原样回 404/501 | 否（登录仍要） | `app.go:189-202`、`relay.go:260` |
| 多 token 轮换 | 已自研：`options.tokens` 轮询，最多 3 次 | 否 | `store.go:174`、`scheduler.go:83-115` |
| 401 续期 | 已自研：`{pay_base}/v1/token/refresh` | 否 | `upstream.go:469-477` |
| 初始 token 获取 | 用户自行粘贴 JWT | 是（扫码/短信/读本机 config.json） | `login.go:33-54`、`login.go:217` |
| 新 IP 验证 / 人机校验 | 不实现 | 是 | `login.go:217`、`upstream.go:59-81` |
| 每日签到 / 领免费模型 | 不实现 | 是 | `upstream.go:448`、`upstream.go:467` |
| 账号池调度 / 冷却 / 代理池 | 不实现（仅 token 轮换） | 是 | `scheduler.go:50-127`、`upstream.go:59-81` |
| 管理面板与本地 Key | 不实现 | 是 | `app.go:341-343`、`auth.go:333-361` |

一句话：**能离线复现的协议层全部在仓内；任何需要人机交互或浏览器会话的链路都留在
sidecar。** 不使用 sidecar 时，用户只需提供一个有效的 Codar 账号 JWT。

token 来源（任选其一）：

1. 已部署 codar2api 时，从它的管理面板复制账号 token（面板「粘贴 JWT」导入的就是它）；
   数据目录下的账号 JSON 里也有同一字段。
2. 本机安装了 Codar 客户端时，读 `~/.codar/config.json` 中的账号 token。
3. token 形如 `eyJ...` 的三段 JWT；只填进服务商页 `api_key`（或 `options.tokens`），
   不会进入请求体，也不会回显到前端。

