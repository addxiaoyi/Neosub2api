# 服务商（Providers）

10 家 provider 的逐家说明。运行态信息（启停、地址、密钥、风控开关）通过管理页「服务商」或 `PATCH /api/providers/{id}` 下发，保存即生效；静态清单来自 `apps/control/app/adapters/` 里的 ProviderSpec 定义。

「能力位」含义：`chat` = `/v1/chat/completions`；`messages` = `/v1/messages`；`models` = 模型列表；`oauth` = 有 OAuth 账号通道；`checkin` / `credits` = 签到 / 积分刷新（账号页按钮）；`register` = 注册任务；`sidecar` = 上游为独立进程。

「实现状态」两种：**已实现** = 控制面内置适配器完成协议对接；**sidecar 直通** = 控制面只做透明转发，账号池/鉴权/状态都在上游独立进程里管理。

许可证风险速览（完整清单与合规 checklist 见 [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md)）：

| 风险级 | provider |
| --- | --- |
| safe（MIT/Apache/BSD） | codebuddy、muse、gptgrok、zcode、trae_sidecar、opencode、sub2api、grok-register（自研） |
| danger（商用前必须移除或取得书面授权） | deepseek_sidecar（无 LICENSE）、qoder_sidecar（PolyForm-NC） |

## 总览

| id | 名称 | 上游 | 协议 | 默认地址 | 能力位 | 状态 | 风险 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `codebuddy` | CodeBuddy | 腾讯 CodeBuddy（addxiaoyi/codebuddy2api） | OpenAI + Anthropic | `http://127.0.0.1:7864` | chat, messages, models, oauth, checkin, credits | 已实现 | safe |
| `muse` | Muse2API | czg86389-hub/muse2api | OpenAI | `http://127.0.0.1:7865` | chat, models | 已实现 | safe |
| `grok-register` | Grok Register | kaibush/grok-register（协议参考） | 无（注册任务） | — | register | 已实现 | safe |
| `gptgrok` | GPT/Grok 2API | addxiaoyi/gptGrok2api | OpenAI | `http://127.0.0.1:7867` | chat, models | 已实现 | safe |
| `zcode` | ZCode 2API | addxiaoyi/ZCode2api | OpenAI + Anthropic | `http://127.0.0.1:8787` | chat, messages, models | 已实现 | safe |
| `opencode` | OpenCode 2API | jasonxu114514/opencode2api（协议参考，主仓 clean-room 重写） | OpenAI + Anthropic | `https://opencode.ai/zen` | chat, messages, models | 已实现 | safe |
| `sub2api` | Sub2API | Wei-Shaw/sub2api（协议参考，主仓 clean-room 重写） | OpenAI + Anthropic | —（账号自带） | chat, messages, models | 已实现 | safe |
| `trae_sidecar` | Trae Solo+Work (trae2api) | JeffHu0912/trae2api | OpenAI | `http://127.0.0.1:7871` | chat, models, oauth, credits, sidecar | sidecar 直通 | safe |
| `deepseek_sidecar` | DeepSeek (Deepseek2Go) | ColudAI/Deepseek2Go | OpenAI + Anthropic（非标路径） | `http://127.0.0.1:7872` | chat, messages, models, sidecar | sidecar 直通 | **danger** |
| `qoder_sidecar` | Qoder Switch (PolyForm-NC) | NextAgentX/qoder-trae-workbuddy-switch | OpenAI + Anthropic | `http://127.0.0.1:7873` | chat, messages, models, sidecar | sidecar 直通 | **danger** |

---

## 1. codebuddy — CodeBuddy

- **上游**：codebuddy2api 项目。控制面对接它的 **Python 管理端（默认 7864）**，Go 数据面（workbuddy2api，7863）由管理端代理。账号管理、设置下发走管理端 `/api/*`；chat/models 也经管理端网关转发。
- **协议**：OpenAI Chat Completions + Anthropic Messages 双协议。
- **必填配置**：管理端的作用域化 API Token（`wbt_` 前缀，`Authorization: Bearer wbt_...`）。可在「服务商」页填 api_key，或用环境变量 `CODEBUDDY_API_KEY` 兜底（页面配置优先）。地址默认 `CODEBUDDY_BASE`（compose 里指向 `host.docker.internal:7864`）。
- **能力**：10 家里唯一支持账号全套操作的——账号同步、签到（单个/全部）、测活、刷新、清冷却、启停、删除、重启、积分刷新、设置下发（写管理端 config.json 并触发重载）。
- **已知限制**：管理端的删除账号 / 重启 / 设置下发要求**会话级管理员**，`wbt_` token 可能被拒（403）；控制面如实透传管理端错误，不掩盖。真实账号行为需启动管理端并配好 token 验证。
- **杂项**：realm 过滤（cn/global）仅对这家生效；「账号」页的 realm 列、`/api/accounts/refresh-credits`、`checkin-all`、`restart` 都固定作用于它。

## 2. muse — Muse2API

- **上游**：czg86389-hub/muse2api（MIT），OpenAI 兼容端点直通。
- **协议**：仅 OpenAI Chat Completions（无 `/v1/messages`，调用返回 501）。
- **必填配置**：上游密钥（muse2api 的 `MUSE2API_KEY`）填到 api_key，以 `Authorization: Bearer` 注入；Base URL 默认 `http://127.0.0.1:7865`。
- **options**：`timeout`（上游超时秒数，默认 120）。
- **已知限制**：真实上游的鉴权头格式未用真实凭证验证过（链路验证走假上游）。

## 3. grok-register — Grok Register

- **是什么**：不是推理 provider，没有 chat/models 协议，`protocols` 为空。是控制面内一等的**注册任务编排服务**：任务队列、并发控制、状态机、指数退避重试，浏览器执行外置。
- **协议/契约**：见 [providers/grok-register.md](providers/grok-register.md)；API 见 [API-REFERENCE.md](API-REFERENCE.md) 第 10 节。
- **必填配置**：环境变量 `GROK_REGISTER_EXECUTOR_URL`（外部浏览器运行时的 HTTP 入口）。**留空则任务入队后以明确原因失败**（不假装注册成功）。
- **options**：无。
- **已知限制**：执行器不随仓交付；邮箱渠道、浏览器自动化、CPA/Grok2API 产物生成都是外部执行器职责。并发上限来自 `GROK_REGISTER_MAX_CONCURRENCY` / `GROK_REGISTER_MAX_WORKERS`。

## 4. gptgrok — GPT/Grok 2API

- **上游**：addxiaoyi/gptGrok2api（MIT），GPT 与 Grok 上游的 OpenAI 兼容反代。
- **协议**：仅 OpenAI Chat Completions。
- **必填配置**：Base URL 默认 `http://127.0.0.1:7867`；上游密钥填 api_key（如上游要求）。
- **options**（风控开关）：
  - `proxy`：出口代理 URL，只作用于本 provider 的上游请求；
  - `fingerprint`：`true` 时注入指纹头（UA、sec-ch-ua 等，来自 `extra_headers`）；
  - `extra_headers`：自定义头 dict；`fingerprint` 关闭时不注入。
- **已知限制**：Grok 指纹头字段需真实凭证确认。

## 5. zcode — ZCode 2API

- **上游**：addxiaoyi/ZCode2api（MIT），Node 独立服务（`zcgw-server.mjs`）。
- **协议**：OpenAI + Anthropic 双协议直通。
- **必填配置**：Base URL 默认 `http://127.0.0.1:8787`；上游密钥按需。
- **options**：`system_prompt_override`——非空字符串时，转发前把它作为 system 消息置顶注入（chat 协议）。上游自身另有 system prompt 改写（zcode-profile.json），本开关是控制面这层的可配置改写，**默认关闭**，避免叠加。
- **已知限制**：system 改写只作用于 chat；`/v1/messages` 原样转发不改写。

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
- **必填配置**：sidecar 跑起来（默认 7872，环境变量 `DEEPSEEK_SIDECAR_BASE`）；凭证在 sidecar 侧。
- **options**：通用项。
- **风险**：上游仓库**无 LICENSE**（版权全保留）。本 provider 仅供个人评估，**商用前必须移除** sidecar 及适配器（详见 THIRD-PARTY-NOTICES.md）。

## 10. qoder_sidecar — Qoder Switch (PolyForm-NC)

- **上游**：NextAgentX/qoder-trae-workbuddy-switch（Rust Axum gateway，buddy-switch-gateway）。多产品线多地域账号池 + 降级链，账号切换自己管。
- **协议**：sidecar 暴露 `/v1/chat/completions`、`/v1/models`、`/v1/messages`、`/v1/responses`；控制面适配 chat + messages + models（`/v1/responses` 不在统一网关协议内，未暴露）。
- **必填配置**：sidecar 跑起来（默认 7873，环境变量 `QODER_SIDECAR_BASE`）；凭证在 sidecar 侧。
- **options**：通用项。
- **风险**：上游为 **PolyForm Noncommercial 1.0.0**，禁止任何商用（含内部商业项目与托管服务）。作者书面授权前不得商用（详见 THIRD-PARTY-NOTICES.md）。

---

## 附：sidecar 端口速查

| 端口 | 服务 |
| --- | --- |
| 7863 | workbuddy2api Go 数据面（codebuddy，由管理端代理） |
| 7864 | codebuddy2api Python 管理端 |
| 7865 | muse2api |
| 7867 | gptGrok2api |
| 7871 | trae2api sidecar |
| 7872 | Deepseek2Go sidecar |
| 7873 | qoder buddy-switch-gateway |
| 8787 | ZCode2api（zcgw-server.mjs） |

以上都是**各上游的默认端口**，控制面侧的地址随时可在「服务商」页改（`base_url`）；`providers/` 目录只保留上游参考来源说明（[README.md](../README.md) 与 THIRD-PARTY-NOTICES.md 有完整说明），不是运行依赖——除四家 sidecar（trae/deepseek/qoder 及 codebuddy 管理端）需要你自行部署对应进程外，其余 provider 均由控制面直连上游。
