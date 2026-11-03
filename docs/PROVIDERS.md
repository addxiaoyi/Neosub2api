# 服务商（Providers）

各家 provider 的逐家说明。运行态信息（启停、地址、密钥、风控开关）通过管理页「服务商」或 `PATCH /api/providers/{id}` 下发，保存即生效；静态清单来自 `apps/control/app/adapters/` 里的 ProviderSpec 定义。

当前适配器共 14 个，下方总览逐行列出，「状态」列区分**内置直连**（控制面内完成协议对接）与 **sidecar 直通**（控制面只做透明转发，账号池/鉴权/状态都在上游独立进程里）。**新增一家的现行步骤**见 [ADDING-A-PROVIDER.md](ADDING-A-PROVIDER.md)，不要再参考已归档的 `FRONTEND-SKELETON.md`。

「能力位」含义：`chat` = `/v1/chat/completions`；`messages` = `/v1/messages`；`models` = 模型列表；`oauth` = 有 OAuth 账号通道；`checkin` / `credits` = 签到 / 积分刷新（账号页按钮）；`register` = 注册任务；`sidecar` = 上游为独立进程。

许可证风险速览（完整清单与合规 checklist 见 [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md)）：

| 风险级 | provider |
| --- | --- |
| safe（MIT/Apache/BSD 或本仓自研 MIT） | codebuddy、muse、gptgrok、zcode、opencode、sub2api、grok-register、codar、codar_sidecar、trae_sidecar、qoder、trae |
| danger（商用前必须移除或取得书面授权） | deepseek_sidecar（无 LICENSE）、qoder_sidecar（PolyForm-NC） |

## 总览

| id | 名称 | 上游 | 协议 | 默认地址 | 能力位 | 状态 | 风险 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `codebuddy` | CodeBuddy | 腾讯 CodeBuddy（addxiaoyi/codebuddy2api） | OpenAI + Anthropic | `http://127.0.0.1:7864` | chat, messages, models, oauth, checkin, credits | 内置直连（经管理端） | safe |
| `muse` | Muse2API | czg86389-hub/muse2api（协议参考） | OpenAI | `https://api.meta.ai` | chat, models | 内置直连 | safe |
| `grok-register` | Grok Register | kaibush/grok-register（协议参考） | 无（注册任务） | — | register | 内置（非推理） | safe |
| `gptgrok` | GPT/Grok 2API | addxiaoyi/gptGrok2api（协议参考） | OpenAI | `https://chatgpt.com` | chat, models | 内置直连 | safe |
| `zcode` | ZCode 2API | addxiaoyi/ZCode2api（协议参考） | OpenAI + Anthropic | `https://zcode.z.ai` | chat, messages, models | 内置直连 | safe |
| `opencode` | OpenCode 2API | jasonxu114514/opencode2api（协议参考，主仓 clean-room 重写） | OpenAI + Anthropic | `https://opencode.ai/zen` | chat, messages, models | 内置直连 | safe |
| `sub2api` | Sub2API | Wei-Shaw/sub2api（协议参考，主仓 clean-room 重写） | OpenAI + Anthropic | —（账号自带） | chat, messages, models | 内置直连 | safe |
| `codar` | Codar (仓内直连) | Codar 私有 API（行为参考 AiXiaoc/codar2api） | OpenAI + Anthropic + Responses | `https://api.claw.speedai.com` | chat, messages, models, responses | 内置直连 | safe |
| `codar_sidecar` | Codar (codar2api) | AiXiaoc/codar2api（Go sidecar） | OpenAI + Anthropic | `http://127.0.0.1:8801` | chat, messages, models, sidecar | sidecar 直通 | safe |
| `trae_sidecar` | Trae Solo+Work (trae2api) | JeffHu0912/trae2api | OpenAI | `http://127.0.0.1:7871` | chat, models, oauth, credits, sidecar | sidecar 直通 | safe |
| `deepseek_sidecar` | DeepSeek (Deepseek2Go) | ColudAI/Deepseek2Go | OpenAI + Anthropic（非标路径） | `http://127.0.0.1:5001` | chat, messages, models, sidecar | sidecar 直通 | **danger** |
| `qoder_sidecar` | Qoder Switch (PolyForm-NC) | NextAgentX/qoder-trae-workbuddy-switch | OpenAI + Anthropic | `http://127.0.0.1:7873` | chat, messages, models, sidecar | sidecar 直通 | **danger** |
| `qoder` | Qoder（COSY） | Qoder 客户端协议（主仓 clean-room 重写） | OpenAI + Anthropic + Responses | `https://gateway.qoder.com.cn` | chat, messages, models, responses | 内置直连 | safe |
| `trae` | Trae（Solo / Work） | Trae IDE 客户端协议（主仓 clean-room 重写） | OpenAI | `https://trae-api-cn.mchost.guru` | chat, models | 内置直连 | safe |

---

## 就绪状态

`GET /api/providers` 的每条记录带两个字段，用来区分「填个 key 就能跑」和「还得再配东西才通」：

- `readiness`：`ready` = 填好 api_key / base_url 就能调用；`needs_config` = 还有核实过的卡点，光填 key 不够。
- `requires`：字符串数组，缺什么（给人看的短句，带症状说明）；`ready` 时为空数组。

只标注**已核实**的卡点。没有真机或文档证据的依赖不写进 `needs_config`，宁可先算 `ready`。

同一接口还带 `base_url_warning` / `credential_base_warnings`，用于提示数据库里残留的历史默认地址（当前只有 `deepseek_sidecar` 的 `http://127.0.0.1:7872`，真实默认是 5001）。它们只提示、不改值，字段定义见 [API-REFERENCE.md](API-REFERENCE.md) §3。

### 本地地址（loopback）规则

`GET /api/providers` 每条记录带 `allows_local_base_url`，值由 `spec.default_base_url` 推导，不写死 provider 名单：

- 默认地址本身是 loopback（`127.0.0.0/8`、`::1`、`localhost`）的 provider 为 `true`：当前是 `codebuddy`（管理端 7864）与四家 sidecar（`trae_sidecar`、`deepseek_sidecar`、`qoder_sidecar`、`codar_sidecar`）。这些 provider 的 `base_url` 与凭据 `base_url` 可以直接填 loopback，不需要再开 `UNIFIED_ALLOW_PRIVATE_UPSTREAMS`。
- 其余 provider 为 `false`：填 loopback 会被 400 拒绝，错误信息会点明「这家未开放本地地址」以及正确做法。
- 放行范围只有 loopback：`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`、链路本地 `169.254.0.0/16` 继续拦截；`169.254.169.254` / `fd00:ec2::254` 云元数据地址任何情况下都拒绝。
- `UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1` 仍是全局开关（语义未变）：开启后跳过所有 IP 段检查，叠加时优先于 per-provider 判断；它不改变 `allows_local_base_url` 的取值，唯一例外是元数据地址仍被拒绝。
- 新增 sidecar 只要把 `default_base_url` 写成 loopback 地址，就自动进入白名单，不用改 `net_guard.py`。

| id | readiness | requires（缺什么） | 缺了会怎样 | 补齐后怎么验证 |
| --- | --- | --- | --- | --- |
| `codebuddy` | needs_config | `7864` 管理端进程 + `wbt_` token | 账号同步、签到、设置下发全部失败（管理端删除/重启还可能 403） | 起 7864 管理端并在「服务商」页填 `wbt_` token，点「账号同步」能拉回账号列表即通 |
| `grok-register` | needs_config | `GROK_REGISTER_EXECUTOR_URL` 指向浏览器执行器 | 留空则任务入队后以明确原因失败（不假装成功） | 配上执行器 URL 后提交一个注册任务，状态能离开 `queued` 走到成功/进行中，而不是 `failed` |
| `sub2api` | needs_config | 先建分组与账号（`/api/sub2api/groups`、`/api/sub2api/accounts`） | 池空或分组未建时请求直接 **503** | 建好 `default` 分组和一个账号后发 `/v1/chat/completions`，返回 200 即通 |
| `zcode` | needs_config | `official_system`（官方 system 原文，约 10701 字符） | 上游做 system 内容指纹校验，返回 **3012** | 在「服务商」页填 `options.official_system` 的原文，再用 zcode 模型发一条 `/v1/chat/completions`，不再出现 3012 |
| `gptgrok` | needs_config | `sentinel_token`（ChatGPT sentinel token，本地无法生成） | ChatGPT 通道上游可能返回 **403** | 手工注入 `options.sentinel_token` 后请求一个 `gpt-*` 模型；Grok 通道（`grok-*`）用 `sso` 凭证单独验 |
| `muse` | needs_config | 官方 OpenAI 兼容入口的 `api_key`（网页 `hatch_sess` 账号没有 HTTP 对话端点） | 网页账号拿不到 HTTP 对话入口，调用不通 | 填官方入口的 api_key，请求 `/v1/models` 能返回模型清单即通（默认域 `https://api.meta.ai` 是推断值，尚未用真实凭证验证，必要时改 base_url） |
| `deepseek_sidecar` | needs_config | sidecar 进程（Deepseek2Go，默认端口 5001） | 控制面连不上上游，健康检查报连接失败 | 起 sidecar 后 `curl http://127.0.0.1:5001/v1/models`，再在「服务商」页点测活 |
| `trae_sidecar` | needs_config | sidecar 进程（trae2api，默认端口 7871） | 同上，连不上 | 起 sidecar 后 `curl http://127.0.0.1:7871/v1/models` |
| `qoder_sidecar` | needs_config | sidecar 进程（buddy-switch-gateway，默认端口 7873） | 同上，连不上 | 起 sidecar 后 `curl http://127.0.0.1:7873/v1/models` |
| `codar` | needs_config | Codar 账号 JWT（扫码/短信登录仍需 codar2api sidecar） | 未填时模型列表与对话全部 401；token 过期且续期失败时同样 401 | 在「服务商」页 api_key 或 `options.tokens` 填 JWT，点测活能返回模型数量即通 |
| `codar_sidecar` | needs_config | sidecar 进程（codar2api，默认端口 8801） | 同上，连不上 | 起 sidecar 后 `curl http://127.0.0.1:8801/v1/models`；调用时另填 sidecar 本地 API Key |

其余 3 家为 `ready`：`opencode`、`trae`、`qoder`——填好 api_key / base_url 即可调用。

> `codar` 与 `codar_sidecar` 是同一上游的两种接入方式，有意同时保留：前者在仓内直连 Codar 私有 API（自带 token 轮换与 401 续期），后者透传到独立 codar2api 进程（扫码/短信登录、签到留在 sidecar）。两者 provider id、默认地址、凭据来源都不同，可按需启停其一或同时启用；请求按 provider 显式选择，不做自动回退，因此不会互相抢占账号或 token。

推断 vs 核实：`zcode` 的 3012、`gptgrok` 的 sentinel/403、四家 sidecar 需要独立进程、`codar` 需要账号 JWT、`codebuddy` 需要 7864 + `wbt_`、`grok-register` 需要执行器 URL、`sub2api` 需要先建分组账号，都有仓内文档或实测依据；`gptgrok` 的两个健康探针端点（`/backend-api/me`、`/rest/models`）与 `muse` 的默认域 `https://api.meta.ai` 仍是**推断未实测**，只影响验证方式，不影响上面的就绪判定。

---

## 1. codebuddy — CodeBuddy

- **上游**：codebuddy2api 项目。控制面对接它的 **Python 管理端（默认 7864）**，Go 数据面（workbuddy2api，7863）由管理端代理。账号管理、设置下发走管理端 `/api/*`；chat/models 也经管理端网关转发。
- **协议**：OpenAI Chat Completions + Anthropic Messages 双协议。
- **必填配置**：管理端的作用域化 API Token（`wbt_` 前缀，`Authorization: Bearer wbt_...`）。可在「服务商」页填 api_key，或用环境变量 `CODEBUDDY_API_KEY` 兜底（页面配置优先）。地址默认 `CODEBUDDY_BASE`（compose 里指向 `host.docker.internal:7864`）。
- **能力**：14 家里唯一支持账号全套操作的——账号同步、签到（单个/全部）、测活、刷新、清冷却、启停、删除、重启、积分刷新、设置下发（写管理端 config.json 并触发重载）。
- **已知限制**：管理端的删除账号 / 重启 / 设置下发要求**会话级管理员**，`wbt_` token 可能被拒（403）；控制面如实透传管理端错误，不掩盖。真实账号行为需启动管理端并配好 token 验证。
- **杂项**：realm 过滤（cn/global）仅对这家生效；「账号」页的 realm 列、`/api/accounts/refresh-credits`、`checkin-all`、`restart` 都固定作用于它。

## 2. muse — Muse2API

- **上游**：czg86389-hub/muse2api（MIT，协议参考）；仓内自研，直连 Muse 官方 OpenAI 兼容入口。
- **协议**：仅 OpenAI Chat Completions（无 `/v1/messages`，调用返回 501）。
- **必填配置**：api_key 以 `Authorization: Bearer` 注入；Base URL 默认 `https://api.meta.ai`（公开资料推断，未用真实凭证验证；旧部署可指回 sidecar `http://127.0.0.1:7865`）。
- **options**：`timeout`（默认 300）、`cookie`（`hatch_sess`）、`extra_keys` / `max_attempts`、`chat_path` / `models_path` / `health_path`。
- **已知限制**：网页账号（`hatch_sess`）只有浏览器 DOM / Noise-XX WebSocket 对话通道，没有 HTTP 对话端点，本仓不复刻该通道；默认官方入口与鉴权未用真实凭证验证（链路验证走假上游）。

## 3. grok-register — Grok Register

- **是什么**：不是推理 provider，没有 chat/models 协议，`protocols` 为空。是控制面内一等的**注册任务编排服务**：任务队列、并发控制、状态机、指数退避重试，浏览器执行外置。
- **协议/契约**：见 [providers/grok-register.md](providers/grok-register.md)；API 见 [API-REFERENCE.md](API-REFERENCE.md) 第 12 节。
- **必填配置**：环境变量 `GROK_REGISTER_EXECUTOR_URL`（外部浏览器运行时的 HTTP 入口）。**留空则任务入队后以明确原因失败**（不假装注册成功）。
- **options**：无。
- **已知限制**：执行器不随仓交付；邮箱渠道、浏览器自动化、CPA/Grok2API 产物生成都是外部执行器职责。并发上限来自 `GROK_REGISTER_MAX_CONCURRENCY` / `GROK_REGISTER_MAX_WORKERS`。

## 4. gptgrok — GPT/Grok 2API

- **上游**：addxiaoyi/gptGrok2api（MIT，协议参考）；仓内自研，直连 ChatGPT Web 与 Grok Web。
- **协议**：仅 OpenAI Chat Completions。
- **必填配置**：Base URL 默认 `https://chatgpt.com`（`grok-*` 模型自动走 `https://grok.com`）；api_key 填 ChatGPT accessToken 或 Grok `sso`。
- **options**（风控开关）：
  - `proxy`：出口代理 URL，只作用于本 provider 的上游请求；
  - `fingerprint`：`true` 时注入 OAI 指纹头与 `extra_headers`；
  - `extra_headers`：自定义头 dict；`fingerprint` 关闭时不注入；
  - `channel` / `chatgpt_base` / `grok_base` / `sentinel_token` / `timeout` / `extra_keys`；
  - `max_attempts`（换 key 重试次数，默认 3，夹逼 1–10 且不超过 密钥数×2）、`key_cooldown`（失败密钥冷却秒数，默认 30）。
- **已知限制**：未用真实账号验证；ChatGPT sentinel token 本地无法生成，只能 `options.sentinel_token` 手工注入；健康探测端点为推断。

## 5. zcode — ZCode 2API

- **上游**：addxiaoyi/ZCode2api（MIT，协议参考）；仓内自研，直连 `https://zcode.z.ai` 的 Anthropic Messages 端点。
- **协议**：OpenAI Chat（Anthropic 事件翻译成 chunk）+ Anthropic Messages（原样透传）。
- **必填配置**：api_key 填账号 JWT（同时作 `authorization` 与 `x-api-key`）；Base URL 默认 `https://zcode.z.ai`。
- **options**：`system_prompt_override`（应用层 system，chat 生效）、`official_system` / `official_system_prompt`（官方 system 原文，缺失即 3012）、`uid`（设备指纹来源）、`timeout` / `proxy` / `extra_headers` / `max_tokens` / `models` / `extra_keys` / `max_attempts` / `key_cooldown`。
- **已知限制**：上游对 system 做内容指纹校验（3012），官方原文不在本仓内置；真实 JWT 未测，3012 之外的链路未做真机回归。

## 6. opencode — OpenCode 2API

- **上游**：OpenCode Zen / Zen Go。jasonxu114514/opencode2api 仅作协议参考，本适配器是主仓 clean-room 重写（直连上游，**不再需要 opencode2api 进程**）。
- **协议**：OpenAI + Anthropic 双协议（Anthropic 走 `anthropic-version: 2023-06-01` + `x-api-key`），SSE 直通。
- **必填配置**：上游密钥——api_key 或 `options.extra_keys`（数组，可配多个做轮换）；都没有时调用返回 503。
- **options**：
  - `tier`：`zen`（默认）或 `go`；`go` 会在路径前拼 `/go`（base_url 已以 `/go` 结尾时不重复拼）；
  - `extra_keys`：备用密钥数组；
  - `max_attempts`：换 key 重试次数（默认 3，夹逼 1–10，且不超过 密钥数×2）；
  - `timeout`：上游超时秒数（默认 300）；
  - `proxy` / `extra_headers`：出口代理与自定义头。
- **重试语义**：`400/413/422` 属请求级错误，立即返回不重试；`401/403/408/429/5xx` 换 key 重试，退避 0.5s→1s→2s（封顶 2s）。
- **已知限制**：真实 Zen 凭证未验证过，`tier=go` 路径前缀与真实模型清单需实际账号确认。

## 7. sub2api — Sub2API

- **上游**：Wei-Shaw/sub2api 仅作协议参考，本适配器是主仓 clean-room 重写的**订阅配额分发**：分组 → 账号池 → token 配额扣减 → 耗尽 429。不依赖 sub2api 进程；每个账号自带 `upstream_base` + `api_key`。
- **协议**：OpenAI + Anthropic 双协议，转发目标由账号的 upstream_base 决定。
- **必填配置**：先用 `/api/sub2api/groups` 建分组、`/api/sub2api/accounts` 加账号（见 [API-REFERENCE.md](API-REFERENCE.md) 第 9 节），否则调用返回 503。
- **options**：`group`（使用的分组 id，默认 `default`）；`timeout`（默认 120）；`proxy`。
- **分发语义**：账号按 last_used_at 升序 + weight 降序取用；上游 429/5xx 时冷却该账号 60 秒并换号重试（最多 3 次）；额度按 usage 的 token 总数扣到分组与账号两侧；分组额度耗尽或池空返回 429。流式响应边转发边攒 usage，流结束扣减。
- **已知限制**：扣减失败不改变已返回的响应（流水缺失靠对账发现）；模型列表取分组内一个账号的上游 `/v1/models`。

## 8. trae_sidecar — Trae Solo+Work (trae2api)

- **上游**：JeffHu0912/trae2api（MIT，Go 独立进程）。sidecar 自身是完整的 Trae Solo 裸模型 + Work 积分双通道 2api 服务（Work 余量不足自动降级 Solo），账号池与 OAuth 全在 sidecar 里。
- **协议**：仅 OpenAI（无 `/v1/messages`）。
- **必填配置**：把 sidecar 跑起来（默认端口 7871，环境变量 `TRAE_SIDECAR_BASE` 指向它）；上游凭证在 sidecar 自身处管理，控制面不需要 api_key。
- **options**：通用项（`timeout` / `proxy` / `extra_headers`）。
- **已知限制**：纯透明转发；账号池健康度、积分探测都在 sidecar 侧看，控制面状态页只能反映「上游 /v1/models 通不通」。属 F2 重写候选。

## 9. deepseek_sidecar — DeepSeek (Deepseek2Go)

- **上游**：ColudAI/Deepseek2Go——chat.deepseek.com 官方站代理，含 WASM PoW；Go 独立进程，登录与 token 刷新自己管。
- **协议**：OpenAI（流式+非流式）+ Anthropic，但 Anthropic 路径**非标准**（`/anthropic/v1/messages`，适配器做了重写），且**仅非流式**——流式 `/v1/messages` 直接 501。
- **必填配置**：sidecar 跑起来（上游默认 5001，环境变量 `DEEPSEEK_SIDECAR_BASE`）；凭证在 sidecar 侧。
- **options**：通用项。
- **风险**：上游仓库**无 LICENSE**（版权全保留）。本 provider 仅供个人评估，**商用前必须移除** sidecar 及适配器（详见 THIRD-PARTY-NOTICES.md）。

## 10. qoder_sidecar — Qoder Switch (PolyForm-NC)

- **上游**：NextAgentX/qoder-trae-workbuddy-switch（Rust Axum gateway，buddy-switch-gateway）。多产品线多地域账号池 + 降级链，账号切换自己管。
- **协议**：sidecar 暴露 `/v1/chat/completions`、`/v1/models`、`/v1/messages`、`/v1/responses`；控制面适配 chat + messages + models（`/v1/responses` 不在统一网关协议内，未暴露）。
- **必填配置**：sidecar 跑起来（默认 7873，环境变量 `QODER_SIDECAR_BASE`）；凭证在 sidecar 侧。
- **options**：通用项。
- **风险**：上游为 **PolyForm Noncommercial 1.0.0**，禁止任何商用（含内部商业项目与托管服务）。作者书面授权前不得商用（详见 THIRD-PARTY-NOTICES.md）。

## 11. codar — Codar（仓内直连）

- **形态**：内置直连。clean-room 重写 Codar 私有 API 客户端，不经 codar2api sidecar；多 token 轮换、401 自动续期（`POST {pay_base}/v1/token/refresh`）都在适配器内完成。
- **协议**：OpenAI Chat + Anthropic Messages + OpenAI Responses（`/v1/codar/models` 读模型目录）。
- **必填配置**：`api_key` 或 `options.tokens` 填 Codar 账号 JWT（可多个，按请求轮询）。缺了模型列表与对话全部 401。
- **options**：`tokens` / `token`（多账号 token）、`pay_base`（续期服务地址，默认 `https://api.kuaipaper.com`）、`refresh`（默认开）、`timeout` / `proxy` / `user_agent` / `extra_headers` / `anthropic_beta`。
- **不实现**：微信扫码/短信登录、新 IP 验证、每日签到、领免费模型——依赖浏览器或人机交互，且随客户端版本变化，继续留在 `codar_sidecar`。
- **实现细节与已知限制**：见 [providers/codar2api.md](providers/codar2api.md)；与 `codar_sidecar` 的取舍见上「就绪状态」的说明。

## 12. codar_sidecar — Codar (codar2api)

- **形态**：sidecar 直通。主仓只做 HTTP 透传；账号登录、令牌续期、签到、代理池全在 codar2api 进程里。
- **协议**：OpenAI Chat + Anthropic Messages（`/v1/responses` 未声明，调用 501）。
- **必填配置**：sidecar 跑起来（默认 8801，环境变量 `CODAR_SIDECAR_BASE`）；`api_key` 填 sidecar 的本地 API Key（`sk-codar-` 前缀）。
- **实现细节、错误语义与已知限制**：见 [providers/codar_sidecar.md](providers/codar_sidecar.md)；上游契约见 [providers/codar2api.md](providers/codar2api.md)。

## 13. qoder — Qoder（COSY，内置直连）

- **形态**：内置直连。clean-room 重写 COSY 签名客户端（自定义 base64 / AES-128-CBC / RSA 全标准库自写），不经任何 sidecar。
- **协议**：OpenAI Chat + Anthropic Messages + OpenAI Responses 三套全接；`workbuddy` 产品线仅 chat，`trae` 产品线 501（改用 provider `trae`）。
- **必填配置**：`api_key` 填 Qoder access token；区域/产品线/代理/身份字段全走 `options`（见实现文档），无专用环境变量。
- **实现细节、路由规则、错误语义与已知限制**：见 [providers/qoder-adapter.md](providers/qoder-adapter.md)；协议契约见 [providers/qoder.md](providers/qoder.md)。

## 14. trae — Trae（Solo / Work，内置直连）

- **形态**：内置直连。clean-room 重写 Trae 客户端，直连 Solo 单端点与 Work 有状态桥，不经 trae2api sidecar；Work 积分受限时自动降级 Solo。
- **协议**：仅 OpenAI Chat + 模型列表（无 `messages` / `responses`，调用 501）。
- **必填配置**：`api_key` 填登录后的 Cloud-IDE JWT；通道选择 `options.channel`（`solo` 默认 / `work`），无专用环境变量。
- **实现细节、请求改写、错误语义与已知限制**：见 [providers/trae-adapter.md](providers/trae-adapter.md)；协议契约见 [providers/trae2api.md](providers/trae2api.md)。

---

## 附：多密钥轮换与冷却（KeyPool）

实现：`apps/control/app/adapters/key_pool.py`。当前接入的是 `zcode` 与 `gptgrok`；其余多密钥适配器（`opencode`、`muse`、`codar`）仍走各自的重试循环，没有冷却表。

- **状态按实例隔离**：每个适配器实例各有一份游标与冷却表，模块级不存可变状态，同进程多实例互不打乱顺序。
- **轮换**：`pick()` 在可用密钥上按游标轮询并推进；`peek()` 只读不推进，供热身/健康探测用。
- **冷却**：只有 401 / 403 / 429 视为「这把 key 不可用」，进入冷却（默认 30 秒，`options.key_cooldown` 可调），期间跳过，到期自动回池；5xx 与网络错误只换 key 重试，不冷却。
- **全部冷却**：不再空转等待，直接返回上游最后一次错误码（没有上游错误时 429），消息里带密钥数与原因。
- **重试上限**：`options.max_attempts` 默认 3，夹逼 1–10，且不超过 密钥数×2；重试退避 0.5s→1s→2s（封顶 2s）。

---

## 附：sidecar 端口速查

| 端口 | 服务 |
| --- | --- |
| 7863 | workbuddy2api Go 数据面（codebuddy，由管理端代理） |
| 7864 | codebuddy2api Python 管理端 |
| 7871 | trae2api sidecar |
| 5001 | Deepseek2Go sidecar |
| 7873 | qoder buddy-switch-gateway |
| 8801 | codar2api sidecar |

以上都是**各上游的默认端口**，控制面侧的地址随时可在「服务商」页改（`base_url`）；`providers/` 目录只保留上游参考来源说明（[README.md](../README.md) 与 THIRD-PARTY-NOTICES.md 有完整说明），不是运行依赖——除五个外部进程（codebuddy 管理端、codar / trae / deepseek / qoder 四家 sidecar）需要你自行部署外，其余 provider 均由控制面内置直连上游。
