# 服务商（Providers）

各家 provider 的逐家说明。运行态信息（启停、地址、密钥、风控开关）通过管理页「服务商」或 `PATCH /api/providers/{id}` 下发，保存即生效；静态清单来自 `apps/control/app/adapters/` 里的 ProviderSpec 定义。

当前适配器共 14 个，下方总览逐行列出，「状态」列区分**内置直连**（控制面内完成协议对接）与 **sidecar 直通**（控制面只做透明转发，账号池/鉴权/状态都在上游独立进程里）。**新增一家的现行步骤**见 [ADDING-A-PROVIDER.md](ADDING-A-PROVIDER.md)，不要再参考已归档的 `FRONTEND-SKELETON.md`。

「能力位」含义：`chat` = `/v1/chat/completions`；`messages` = `/v1/messages`；`responses` = `/v1/responses`；`models` = 模型列表；`oauth` = 有 OAuth 账号通道；`checkin` / `credits` = 签到 / 积分刷新（账号页按钮）；`register` = 注册任务；`sidecar` = 上游为独立进程。本表只列核心对话能力位；`embeddings` / `images` / `videos` / `media` / `count_tokens` / `rerank` / `usage` 等扩展端点的真实归属以 [PROTOCOL-MATRIX.md](PROTOCOL-MATRIX.md) §1.1 为准，核心位与 §1 一致。

许可证风险速览（完整清单与合规 checklist 见 [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md)）：

| 风险级 | provider |
| --- | --- |
| safe（MIT/Apache/BSD 或本仓自研 MIT） | codebuddy、muse、gptgrok、zcode、opencode、sub2api、grok-register、codar、codar_sidecar、trae_sidecar、qoder、trae |
| danger（商用前必须移除或取得书面授权） | deepseek_sidecar（无 LICENSE）、qoder_sidecar（PolyForm-NC） |

## 总览

| id | 名称 | 上游 | 协议 | 默认地址 | 能力位 | 状态 | 风险 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `codebuddy` | CodeBuddy | 腾讯 CodeBuddy（addxiaoyi/codebuddy2api） | OpenAI + Anthropic | `http://127.0.0.1:7864` | chat, messages, models, oauth, checkin, credits | 内置直连（经管理端） | safe |
| `muse` | Muse2API | czg86389-hub/muse2api（协议参考） | OpenAI + Responses（responses 流式透传） | `https://api.meta.ai` | chat, models, responses | 内置直连 | safe |
| `grok-register` | Grok Register | kaibush/grok-register（协议参考） | 无（注册任务） | — | register | 内置（非推理） | safe |
| `gptgrok` | GPT/Grok 2API | addxiaoyi/gptGrok2api（协议参考） | OpenAI + Anthropic + Responses（messages / responses 流式为合成） | `https://chatgpt.com` | chat, messages, models, responses | 内置直连 | safe |
| `zcode` | ZCode 2API | addxiaoyi/ZCode2api（协议参考） | OpenAI + Anthropic | `https://zcode.z.ai` | chat, messages, models | 内置直连 | safe |
| `opencode` | OpenCode 2API | jasonxu114514/opencode2api（协议参考，主仓 clean-room 重写） | OpenAI + Anthropic | `https://opencode.ai/zen` | chat, messages, models | 内置直连 | safe |
| `sub2api` | Sub2API | Wei-Shaw/sub2api（协议参考，主仓 clean-room 重写） | OpenAI + Anthropic + Responses（responses 流式透传） | —（账号自带） | chat, messages, models, responses | 内置直连 | safe |
| `codar` | Codar (仓内直连) | Codar 私有 API（行为参考 AiXiaoc/codar2api） | OpenAI + Anthropic + Responses | `https://api.claw.speedai.com` | chat, messages, models, responses | 内置直连 | safe |
| `codar_sidecar` | Codar (codar2api) | AiXiaoc/codar2api（Go sidecar） | OpenAI + Anthropic + Responses（responses 由 sidecar 转发） | `http://127.0.0.1:8801` | chat, messages, models, responses, sidecar | sidecar 直通 | safe |
| `trae_sidecar` | Trae Solo+Work (trae2api) | JeffHu0912/trae2api | OpenAI | `http://127.0.0.1:7871` | chat, models, sidecar | sidecar 直通 | safe |
| `deepseek_sidecar` | DeepSeek (Deepseek2Go) | ColudAI/Deepseek2Go | OpenAI + Anthropic（非标路径） | `http://127.0.0.1:5001` | chat, messages, models, sidecar | sidecar 直通 | **danger** |
| `qoder_sidecar` | Qoder Switch (PolyForm-NC) | NextAgentX/qoder-trae-workbuddy-switch | OpenAI + Anthropic + Responses | `http://127.0.0.1:7873` | chat, messages, models, responses, sidecar | sidecar 直通 | **danger** |
| `qoder` | Qoder（COSY） | Qoder 客户端协议（主仓 clean-room 重写） | OpenAI + Anthropic + Responses | `https://gateway.qoder.com.cn` | chat, messages, models, responses | 内置直连 | safe |
| `trae` | Trae（Solo / Work） | Trae IDE 客户端协议（主仓 clean-room 重写） | OpenAI | `https://trae-api-cn.mchost.guru` | chat, models | 内置直连 | safe |

> 2026-11-05 已更正（原单元格值逐字留档）：`muse` 一行原为「协议 `OpenAI`、能力位 `chat, models`」；`gptgrok` 一行原为「协议 `OpenAI`、能力位 `chat, models`」；`sub2api` 一行原为「协议 `OpenAI + Anthropic`、能力位 `chat, messages, models`」。三处同因与代码不符：
> - `muse`：`apps/control/app/adapters/muse.py:101-109` 声明 `responses=True`、`:344-360` 实现；流式分支（`:349-355`）直接 `reply.iter_bytes()`，上游 `response.*` 事件**原样透传**，不重排不拼接；仍不声明 `messages`（理由见 §2 与 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) §8）。
> - `gptgrok`：`apps/control/app/adapters/gptgrok.py:400` 声明 `messages=True, responses=True`（`protocols` 见 `:401`），`:1323` / `:1353` 分别由 `_render_responses` / `_render_anthropic` 把 ChatGPT 帧**合成**为 Responses / Anthropic 帧——**流式也是合成，不是透传**。
> - `sub2api`：`apps/control/app/adapters/sub2api.py:67` 声明 `responses=True`、`:181` 的 `responses()` 走 `_forward(..., stream=stream)`，路径表 `:23` → `/v1/responses`，流式为**透传**（由账号上游决定）。
> 误判后果：客户端若按旧表以为 `muse` / `sub2api` 没有 `/v1/responses`、`gptgrok` 没有 `/v1/messages`，会绕开网关直连上游或降级到 chat 兼容层；`gptgrok` 的合成流被误记为不支持时，Anthropic / Responses 客户端会直接放弃该 provider。网关侧真实入口在 `apps/control/app/routers/v1.py:644`（messages）与 `:649`（responses）。核验口径与 [PROTOCOL-MATRIX.md](PROTOCOL-MATRIX.md) §1 / §3 一致。

> 2026-11-05 已更正（原单元格值逐字留档）：`codar_sidecar` 一行原为「协议 `OpenAI + Anthropic`、能力位 `chat, messages, models, sidecar`」。`apps/control/app/adapters/codar_sidecar.py:30-39` 实际声明 `responses=True`（同时声明 `count_tokens=True`），docstring `:7-9` 已写明「/v1/responses 经契约交叉验证后声明」，`:56-58` 的 `responses()` 与 `:60-62` 的 `count_tokens()` 都是 `forward(...)` —— **sidecar 原样转发，不是控制面合成**（选路时与 `gptgrok` / `qoder` 的「本进程合成」有本质差别）；`protocols` 见 `:40`。故协议列补 `Responses`、能力位列补 `responses`（`count_tokens` 属扩展位，按本表口径不列，端点归属见 [PROTOCOL-MATRIX.md](PROTOCOL-MATRIX.md) §1.1）。误判后果：客户端会把 `/v1/responses` 当成 501 而绕开该 sidecar 直连 codar2api，或降级到 chat；漏记 `count_tokens` 会让 Anthropic 客户端改走本地估算。**依据当前工作树改动（`git status` 显示 M，未提交），文档按工作区代码写，未入库。**

> 2026-11-04 更正（原单元格值留档）：`qoder_sidecar` 一行原写作协议「OpenAI + Anthropic」、能力位「chat, messages, models, sidecar」，即按「未接 `/v1/responses`」记载，与代码不符——`apps/control/app/adapters/qoder_sidecar.py:32` 声明 `responses: True`、`:55-57` 实现转发，`apps/control/app/routers/v1.py:479-481` 已挂 `/v1/responses` 路由，故协议列改为「OpenAI + Anthropic + Responses」、能力位补 `responses`。

> 2026-11-06 已更正（原单元格值逐字留档）：`trae_sidecar` 一行能力位原为「`chat, models, oauth, credits, sidecar`」。`apps/control/app/adapters/trae_sidecar.py:23-32` 实际为 `chat=True, messages=False, models=True, oauth=False, checkin=False, credits=False, register=False, sidecar=True`；该适配器只有 `__init__`（`:43`），没有 `start_login` / `refresh_credits` / `sync_accounts` 覆写，账号池与 OAuth 全在 sidecar 进程内。故去掉 `oauth` / `credits`，能力位改为 `chat, models, sidecar`。
> 误判后果：按旧格会把控制面当成能做 Trae OAuth 登录与积分刷新，去「账号」页找按钮或怀疑能力探测漏项；实际 `apps/control/app/routers/accounts.py:25-42` 的 `_account_ops` 按真实覆写与能力位判定，这两个按钮本就不会出现。

---

## 就绪状态

`GET /api/providers` 的每条记录带两个字段，用来区分「填个 key 就能跑」和「还得再配东西才通」：

- `readiness`：`ready` = 填好 api_key / base_url 就能调用（**只表示配置齐备，`ready` ≠ 真机跑通**）；`needs_config` = 还有核实过的卡点，光填 key 不够。
- `requires`：字符串数组，缺什么（给人看的短句，带症状说明）；`ready` 时为空数组。

只标注**已核实**的卡点；**「有代码」不等于「就绪」**，没有一条真实外呼跑通的 provider 一律按 `needs_config` 展示。

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
| `opencode` | needs_config | OpenCode Zen/Go 密钥（`api_key` 或 `options.extra_keys`，静态无刷新） | 未填时推理请求 **503**（模型发现可走匿名通道，不保证成功） | 带 key 请求 `/v1/models` 与 `/v1/chat/completions` 各一条，返回 200 且 usage 正常即通 |
| `trae` | needs_config | Trae 登录后的 **Cloud-IDE JWT**（`api_key`，或多条 JWT 放凭据库 / `api_keys` 与 `options.extra_keys`） | JWT 缺失或被拒时 **401**；Work 通道还需 `options.channel=work` 与账号积分 | Solo 发一条 `glm-5.2` 非流式请求返回 200；Work 通道另测一次流式与降级 |
| `qoder` | needs_config | Qoder **access token** + `uid` / `refresh_token` 身份字段（`api_key` + `options`） | 未填或会话失效报 **429 session_dead**，需重新登录换 token | 填真实 token 后请求一次 `/v1/models` 与 chat，能返回模型与正常 usage 即通 |

三家（`opencode`、`trae`、`qoder`）当前**没有任何真实外呼证据**：测试全部基于假上游/桩响应，文档也各自列有未真机验证项。因此不按 `ready` 展示，等各自跑通一条真实链路后再回填 `ready`。

> 2026-11-06 口径统一：`ready` 只表示「配置齐备、填 key 能调用」，**不等于真机跑通**；当前 14 家 provider 的 `ProviderSpec` 全部为 `needs_config`（`apps/control/app/adapters/codebuddy.py:129`、`opencode.py:83`、`qoder.py:459`、`trae.py:157` 等），没有任何一家按 `ready` 对外展示。上句只点名三家，是因为它们最容易被误当成「无卡点」；其余 11 家的卡点各自列在上表。此结论与 [PROTOCOL-MATRIX.md](PROTOCOL-MATRIX.md) §1 同步，原先把三家标成 `ready` 的口径一律作废。

> `codar` 与 `codar_sidecar` 是同一上游的两种接入方式，有意同时保留：前者在仓内直连 Codar 私有 API（自带 token 轮换与 401 续期），后者透传到独立 codar2api 进程（扫码/短信登录、签到留在 sidecar）。两者 provider id、默认地址、凭据来源都不同，可按需启停其一或同时启用；请求按 provider 显式选择，不做自动回退，因此不会互相抢占账号或 token。

推断 vs 核实：`zcode` 的 3012、`gptgrok` 的 sentinel/403、四家 sidecar 需要独立进程、`codar` 需要账号 JWT、`codebuddy` 需要 7864 + `wbt_`、`grok-register` 需要执行器 URL、`sub2api` 需要先建分组账号，都有仓内文档或实测依据；`gptgrok` 的两个健康探针端点（`/backend-api/me`、`/rest/models`）与 `muse` 的默认域 `https://api.meta.ai` 仍是**推断未实测**，只影响验证方式，不影响上面的就绪判定。

---

## 1. codebuddy — CodeBuddy

- **上游**：codebuddy2api 项目。控制面对接它的 **Python 管理端（默认 7864）**，Go 数据面（workbuddy2api，7863）由管理端代理。账号管理、设置下发走管理端 `/api/*`；chat/models 也经管理端网关转发。
- **协议**：OpenAI Chat Completions + Anthropic Messages 双协议；本地未接上游的 Responses 与 count_tokens（缺口，见下方「未接的扩展端点」）。
- **必填配置**：管理端的作用域化 API Token（`wbt_` 前缀，`Authorization: Bearer wbt_...`）。可在「服务商」页填 api_key，或用环境变量 `CODEBUDDY_API_KEY` 兜底（页面配置优先）。地址默认 `CODEBUDDY_BASE`（compose 里指向 `host.docker.internal:7864`）。
- **能力**：14 家里唯一支持账号全套操作的——账号同步、签到（单个/全部）、测活、刷新、清冷却、启停、删除、重启、积分刷新、设置下发（写管理端 config.json 并触发重载）。
- **已知限制**：管理端的删除账号 / 重启 / 设置下发要求**会话级管理员**，`wbt_` token 可能被拒（403）；控制面如实透传管理端错误，不掩盖。真实账号行为需启动管理端并配好 token 验证。
- **未接的扩展端点（缺口，不是取舍）**：上游管理端/数据面提供 `POST /v1/responses`，并注明 `/v1/messages/count_tokens` 可用（分析基线 `92b8b91` 的 README 写明 Responses 协议与 `count_tokens`，接口表列出 `POST /v1/responses`）。本地 `apps/control/app/adapters/codebuddy.py:119-127` 的 `stub_capabilities` 只声明 `chat / messages / models / oauth / checkin / credits`，没有 `responses` / `count_tokens`，也没有覆写 `responses()` / `count_tokens()`，故这两条在网关侧回 501（`apps/control/tests/test_v1_extension_endpoints.py:336-343` 已锁定该行为）；要接需补适配器转发、同步改判该用例并补转发用例，缺口登记见 [PROTOCOL-MATRIX.md](PROTOCOL-MATRIX.md) §1.2。
- **杂项**：realm 过滤（cn/global）仅对这家生效；「账号」页的 realm 列、`/api/accounts/refresh-credits`、`checkin-all`、`restart` 都固定作用于它。

## 2. muse — Muse2API

- **上游**：czg86389-hub/muse2api（MIT，协议参考）；仓内自研，直连 Muse 官方 OpenAI 兼容入口。
- **协议**：OpenAI Chat Completions + OpenAI Responses（responses 流式逐帧透传，`muse.py:344-360`）；**不支持** Anthropic `/v1/messages`，调用返回 501。（2026-11-05 已更正，原句逐字留档：「仅 OpenAI Chat Completions（无 `/v1/messages`，调用返回 501）。」——原句写死「仅」，会让客户端误判 muse 没有 `/v1/responses` 而绕行直连；真实能力位是 `muse.py:101-109` 的 `chat / models / responses`，外加 `images / videos / media` 三个转发端点（归属见 [PROTOCOL-MATRIX.md](PROTOCOL-MATRIX.md) §1.1）。）
- **必填配置**：api_key 以 `Authorization: Bearer` 注入；Base URL 默认 `https://api.meta.ai`（公开资料推断，未用真实凭证验证；旧部署可指回 sidecar `http://127.0.0.1:7865`）。
- **options**：`timeout`（默认 300）、`cookie`（`hatch_sess`）、`extra_keys` / `max_attempts`、`chat_path` / `models_path` / `health_path`。
- **账号池**：已接 `CredentialPool`（`muse.py:121`）。多 key 时按 sticky → weighted → 轮转选号（`:249-311`）；401/403/429/402/408/5xx 换号并登记分档冷却（401/403 600s、402 900s、429 120s、408/5xx 60s，唯一或无替代账号不冷却，`:230-236`）；成功回写健康度与粘性，流式读完才算成功（`:48-78`）；`health()` 返回 `rotation` / `cooling`（`:404-438`）。
- **已知限制**：网页账号（`hatch_sess`）只有浏览器 DOM / Noise-XX WebSocket 对话通道，没有 HTTP 对话端点，本仓不复刻该通道；默认官方入口与鉴权未用真实凭证验证（链路验证走假上游）。

## 3. grok-register — Grok Register

- **是什么**：不是推理 provider，没有 chat/models 协议，`protocols` 为空。是控制面内一等的**注册任务编排服务**：任务队列、并发控制、状态机、指数退避重试，浏览器执行外置。
- **协议/契约**：见 [providers/grok-register.md](providers/grok-register.md)；API 见 [API-REFERENCE.md](API-REFERENCE.md) 第 12 节。
- **必填配置**：环境变量 `GROK_REGISTER_EXECUTOR_URL`（外部浏览器运行时的 HTTP 入口，协议 `grok-register-executor/v1`）。**留空则任务入队后以明确原因失败**（不假装注册成功）。可选 `GROK_REGISTER_EXECUTOR_TIMEOUT_SEC`（默认 900）与 `GROK_REGISTER_POLL_INTERVAL_SEC`（默认 2）。
- **options**：无。
- **已知限制**：浏览器自动化不随仓交付（邮箱渠道、CPA/Grok2API 产物生成同属外部执行器职责）；仓内提供 `apps/control/scripts/grok_executor_stub.py` 仅用于无真执行器时验证链路，不做真实注册。并发上限来自 `GROK_REGISTER_MAX_CONCURRENCY` / `GROK_REGISTER_MAX_WORKERS`。

## 4. gptgrok — GPT/Grok 2API

- **上游**：addxiaoyi/gptGrok2api（MIT，协议参考）；仓内自研，直连 ChatGPT Web 与 Grok Web。
- **协议**：OpenAI Chat Completions + Anthropic Messages + OpenAI Responses；`messages` / `responses` 的流式由适配器**合成**（不是透传）。（2026-11-05 已更正，原句逐字留档：「仅 OpenAI Chat Completions。」——原句会让客户端以为没有 Anthropic / Responses 端点而绕行直连或降级到 chat 兼容层；真实声明见 `gptgrok.py:400`，实现见 `:1323`（responses）、`:1353`（messages）。）
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
- **重试语义**：`400/413/422` 属请求级错误，立即返回不重试；`401/402/403/408/429/5xx` 换 key 重试并登记分档冷却，退避 0.5s→1s→2s（封顶 2s）。
- **账号池**：已接 `CredentialPool` + `RotationStore`（`opencode.py:93-94`）。多 key 时按 组内粘性 → sticky → weighted → 轮转选号（`:188-274`）；401/403 600s、402 900s、429 120s、408/5xx 60s 分档冷却（档位表 `:24`，登记 `:169-175`），唯一或无替代账号不冷却；成功回写健康度与粘性，流式读完才算成功（`_TrackedReply` `:34-64`、包装 `:177-186`）；`health()` 返回 `rotation` / `cooling`（`:307-327`）。
- **已知限制**：真实 Zen 凭证未验证过，`tier=go` 路径前缀与真实模型清单需实际账号确认。

## 7. sub2api — Sub2API

- **上游**：Wei-Shaw/sub2api 仅作协议参考，本适配器是主仓 clean-room 重写的**订阅配额分发**：分组 → 账号池 → token 配额扣减 → 耗尽 429。不依赖 sub2api 进程；每个账号自带 `upstream_base` + `api_key`。
- **协议**：OpenAI + Anthropic + Responses 三协议，转发目标由账号的 upstream_base 决定；responses 流式为**透传**（`sub2api.py:181` → `_forward`，路径表 `:23`）。（2026-11-05 已更正，原句逐字留档：「OpenAI + Anthropic 双协议，转发目标由账号的 upstream_base 决定。」——原句漏记 responses，客户端会以为该 provider 只能 chat/messages 而绕行；真实声明见 `sub2api.py:67`。另有 embeddings / images / count_tokens / rerank / videos / usage 转发端点，归属见 [PROTOCOL-MATRIX.md](PROTOCOL-MATRIX.md) §1.1。）
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
- **协议**：sidecar 暴露 `/v1/chat/completions`、`/v1/models`、`/v1/messages`、`/v1/responses`；控制面适配 chat + messages + models + responses。（2026-11-04 更正，原句保留作历史：原记「`/v1/responses` 不在统一网关协议内，未暴露」，与代码不符——`adapters/qoder_sidecar.py:32` 已声明 `responses: True`、`:55-57` 已实现转发，`routers/v1.py:479-481` 已挂 `/v1/responses` 路由，带 `responses` 能力位的请求可正常透传到 sidecar。）
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
- **协议**：OpenAI Chat + Anthropic Messages + Responses（responses 与 count_tokens 均由 sidecar 原样转发，不是控制面合成）。（2026-11-05 已更正，原句逐字留档：「OpenAI Chat + Anthropic Messages（`/v1/responses` 未声明，调用 501）。」——原句会让客户端以为该 sidecar 的 `/v1/responses` 必然 501 而绕开直连 codar2api；真实声明见 `codar_sidecar.py:30-39`（`responses=True`、`count_tokens=True`），转发实现见 `:56-58`（`/v1/responses`）与 `:60-62`（`/v1/messages/count_tokens`），`protocols` 见 `:40`。依据当前工作树改动（未提交，`git status` 显示 M），文档按工作区代码写，未入库。）
- **必填配置**：sidecar 跑起来（默认 8801，环境变量 `CODAR_SIDECAR_BASE`）；`api_key` 填 sidecar 的本地 API Key（`sk-codar-` 前缀）。
- **实现细节、错误语义与已知限制**：见 [providers/codar_sidecar.md](providers/codar_sidecar.md)；上游契约见 [providers/codar2api.md](providers/codar2api.md)。

## 13. qoder — Qoder（COSY，内置直连）

- **形态**：内置直连。clean-room 重写 COSY 签名客户端（自定义 base64 / AES-128-CBC / RSA 全标准库自写），不经任何 sidecar。
- **协议**：OpenAI Chat + Anthropic Messages + OpenAI Responses 三套全接；`workbuddy` 产品线仅 chat，`trae` 产品线 501（改用 provider `trae`）。
- **必填配置**：每条 `provider_credentials` 一个账号，填纯 access token 或带身份字段的 JSON 凭据（`access_token` + 可选 `uid`/`user_type`/组织等）；区域/产品线/代理/身份默认值全走 `options`（见实现文档），无专用环境变量。
- **多账号与多身份**：已接 `CredentialPool`（`qoder.py:554,637-693,758-811`）。多凭据按 sticky → weighted → 轮转选号；身份字段随凭据走，`uid`/组织切换即时生效；401/402/403/408/429/5xx 换号并按池分档冷却（唯一账号不冷却，`qoder.py:591-606`）；会话按 `uid+token 指纹` 缓存、鉴权失败作废重握手（`:809`）；`health()` 返回 `accounts` / `rotation`（`:1714`）。
- **实现细节、路由规则、错误语义与已知限制**：见 [providers/qoder-adapter.md](providers/qoder-adapter.md)；协议契约见 [providers/qoder.md](providers/qoder.md)。

## 14. trae — Trae（Solo / Work，内置直连）

- **形态**：内置直连。clean-room 重写 Trae 客户端，直连 Solo 单端点与 Work 有状态桥，不经 trae2api sidecar；Work 积分受限时自动降级 Solo。
- **协议**：仅 OpenAI Chat + 模型列表（无 `messages` / `responses`，调用 501）。
- **必填配置**：`api_key` 填登录后的 Cloud-IDE JWT；通道选择 `options.channel`（`solo` 默认 / `work`），无专用环境变量。
- **账号池**：已接 `CredentialPool`（`trae.py:171,177-207,924-958`）。多 JWT 时按 sticky → weighted → 轮转选号，401/402/429/5xx 换号并按池分档冷却（唯一账号不冷却）；`health()` 返回 `rotation` / `cooling`（`:980-1019`）。408 在本适配器没有产生路径。
- **实现细节、请求改写、错误语义与已知限制**：见 [providers/trae-adapter.md](providers/trae-adapter.md)；协议契约见 [providers/trae2api.md](providers/trae2api.md)。

---

## 附：多密钥轮换与冷却（KeyPool）

实现：`apps/control/app/adapters/key_pool.py`。当前接入的是 `zcode` 与 `gptgrok`；其余多密钥适配器（`opencode`、`muse`、`codar`）仍走各自的重试循环，没有冷却表。

> **（2026-11-06 已更正，原句逐字留档）** 上句原写「当前接入的是 `zcode` 与 `gptgrok`；其余多密钥适配器（`opencode`、`muse`、`codar`）仍走各自的重试循环，没有冷却表。」现状已变：`zcode` 改用 `CredentialPool`（`apps/control/app/adapters/zcode.py:98`），`opencode` 用 `CredentialPool` + `RotationStore`（`opencode.py:93-94`），`codar` 用 `Scheduler`（`codar.py:170-171`），`trae` 用 `CredentialPool`（`trae.py:171`）；仍用 `KeyPool` 的只剩 `gptgrok`（`gptgrok.py:417`），`muse` 仍未接池。下面 KeyPool 的语义对 `gptgrok` 继续成立；`zcode` / `opencode` / `codar` 的分档冷却、熔断、粘性、权重与四档策略见 [SCHEDULING.md](SCHEDULING.md)。
> 误判后果：照旧句会以为 `opencode` / `codar` 没有冷却表而重复实现；排障时也会去 `key_pool.py` 找它们的冷却状态（实际在 `services/rotation.py`），并把「按状态码分档 + 连败升级/熔断」误当成 KeyPool 的单档 30 秒冷却。
> **（2026-11-06 接线 trae 后补充）** `trae` 现在也接池（`CredentialPool`），可产生的 401/402/429/5xx 会登记冷却（402 档 900s、5xx 档 60s，`trae.py:202-207,924-958`）；`opencode` 仍只登记 401/403/429（`opencode.py:198`），其 402/408/5xx 未接线（5xx 仅解绑粘性，`opencode.py:125-132`）；`trae` 不产生 408。
> **（2026-11-06 接线 opencode / muse 后补充）** 上一块的「`opencode` 仍只登记 401/403/429，其 402/408/5xx 未接线」与首块的「`muse` 仍未接池」均已过时：`muse` 已用 `CredentialPool`（`muse.py:121`），选号与失败登记见 `:249-311`，流式读完才回写健康度见 `:48-78`，`health()` 见 `:404-438`；`opencode` 的池分档已扩到 401/402/403/408/429/5xx（档位表 `opencode.py:24`），登记见 `:169-175`，选号见 `:188-274`，流式跟踪见 `:34-64,177-186`，`health()` 见 `:307-327`。两家都按「唯一或无替代账号不冷却」，档位与 `services/rotation.py:32-42` 对齐（401/403 600s、402 900s、429 120s、408/5xx 60s）；`gptgrok` 仍是 `key_pool.py` 的租约模型（`reserve/release/feedback`），与这两家的轮转池不是一套状态。
> 误判后果：照旧句会以为 `muse` 没有冷却表、`opencode` 的 402/408/5xx 只换号不冷却，排障时会去 `key_pool.py` 找状态（实际在 `services/rotation.py`），也会漏掉「流式拿到 200 头不算成功、要读完才回写」这一条。
> **（2026-11-06 接线 qoder 后补充）** `qoder` 也接池（`CredentialPool`）：多账号选号、401/402/403/408/429/5xx 分档冷却、唯一账号不冷却、流式读完才回写成功，均在 `qoder.py:591-693`；`fetch_models` / `health` 同样走池（`:1682,1720`），不再使用适配器私有的 `_cooldown` 表。

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

---

## 附：单个服务商自助排查（scripts/selfcheck_provider.py）

从**仓库根**运行（不是 `apps/control` 下）：

```powershell
python scripts/selfcheck_provider.py --provider demo_local --fake
python scripts/selfcheck_provider.py --provider zcode
python scripts/selfcheck_provider.py --provider zcode --live
python scripts/selfcheck_provider.py --list
```

五个阶段每阶段一行 PASS / SKIP / FAIL：配置、readiness、连通、流式、计费与日志（`credit_status`：billed / usage_missing / estimated / unpriced / not_billed / unlinked）。

- **默认**：只以只读方式打开真实配置库（`UNIFIED_DB_PATH`，默认 `data/unified.db`）读 `providers` / `provider_credentials` / `provider_config` / 上次探活结果，**绝不外呼**，连通与流式阶段 SKIP。
- **`--fake`**：只用于 `demo_local`。临时目录里建临时库并开启演示模式，复用 `scripts/fake_upstream.py` 的进程内假上游、真实适配器与网关；验证请求 → 响应 → usage → 计费 → 日志。**不读写真实配置库**。
- **`--live`**：先在真实库建一次性网关 key（`selfcheck-<随机>`，最小配额 2 次请求），进程内装配控制面 ASGI（不监听 13100/18090），经 `/v1/chat/completions` 打一次 `max_tokens=1` 的非流式 + 一次流式。`request_logs` / `usage_records` / `billing_ledger` 会留下本次记录，计费阶段打印三张表行数、`credit_status` 与金额；跑完无论成败都按临时 key 精确删除本次三表行、临时 key 与本次价格快照，不碰历史。无凭据 / 未启用 / sidecar 未起仍保持 SKIP，不外呼。
- sidecar 四家（`trae_sidecar` / `deepseek_sidecar` / `qoder_sidecar` / `codar_sidecar`）进程未起时，readiness 与连通阶段 SKIP（退出码 2），并打印仓内 README 的真实启动命令。

退出码：全 PASS = **0**；无 FAIL 但有 SKIP = **2**；有 FAIL = **1**；未知 id / 缺 `--provider` = **1**。最后一行固定打印「下一件该做的事」。

---

## 附：有意不做的取舍（2026-11-04 盘点）

下列 10 类功能（按 2026-11-06 对账细分为 18 项）**属于有意不做**，依据来自许可证隔离、设计边界或执行器外置，都有仓内出处，**不是能力缺口**；后续评审按「取舍」对待，不要重复按缺口提出。引用以章节锚点为准，行号仅作辅证。

| # | 功能 | 属于哪家上游 | 不做的依据（文件:行或条款） |
| --- | --- | --- | --- |
| 1 | `/v1/responses`、`/v1/systemone`、WARP 出口、匿名工具集注入、WebUI（8081）透传 | opencode2api（jasonxu114514/opencode2api） | **GPL-3.0 只能进程隔离**：`upstreams/upstreams.yaml:54`（license GPL-3.0）、`:62`（「上游代码只能进程隔离（sidecar）接入」）；取舍原文 `docs/providers/opencode2api.md:16,18`（上游确有这两个端点）与 `:109`（不做 Responses、System One、WARP、自动匿名出口轮换）、`docs/design/frontend-integration-plan.md:87-88`（匿名通道 WebUI 与 System One/Responses 不进界面）。注：「匿名工具集注入」为 2026-11-04 盘点项，仓内无独立行号，与本行同因（GPL 隔离） |
| 2 | 复合路由注册表、多平台价格表、支付/订阅、Gemini `/v1beta` | sub2api（Wei-Shaw/sub2api） | **LGPL-3.0 隔离**：`upstreams/upstreams.yaml:87`（license LGPL-3.0）、`:94`（「只能进程隔离（不链接上游库）」）；取舍原文 `docs/providers/sub2api.md:121`（不做复合路由注册表、多平台价格表、支付/订阅计划、Gemini 原生路径）、`docs/design/frontend-integration-plan.md:124`、`:337` |
| 3 | 控制面不做 trae 的 OAuth / 积分刷新；不做 `/v1/messages`、`/v1/responses` | trae2api（JeffHu0912/trae2api） | **设计边界 + 上游事实**：`apps/control/app/routers/accounts.py:21-23`（trae_sidecar 声明 oauth/credits，但账号在 sidecar 内部，主仓两个方法一个都没实现）、本文件 §8 trae_sidecar「已知限制」（账号池健康度、积分探测都在 sidecar 侧）、`docs/providers/trae2api.md:110-111`（Trae 上游只有 chat 语义，不提供 messages/responses）、`:139`（OAuth `ExchangeToken` 未接入 `POST /api/auth/start`）；许可证 MIT，非许可证原因（`upstreams/upstreams.yaml:98`） |
| 4 | PoW 自研求解器 | Deepseek2Go（ColudAI/Deepseek2Go） | **无 LICENSE，商用前必须移除**：`upstreams/upstreams.yaml:109`（license unknown）、`:117`（「仓库内无 LICENSE 文件…默认按 All Rights Reserved 处理，商用禁用」）、本文件 §9 deepseek_sidecar「风险」、`docs/design/frontend-integration-plan.md:146,340` |
| 5 | sidecar 路径只做 HTTP 代理（不复制上游代码）；`trae` 产品线 501 | qoder-switch（NextAgentX/qoder-trae-workbuddy-switch） | **PolyForm-NC 禁商用**：`apps/control/app/adapters/qoder_sidecar.py:3-5`（「不复制不修改」上游源码、仅作 HTTP 代理）、本文件 §10 qoder_sidecar「风险」；§13 qoder「协议」：`trae` 产品线 501 并指引改用 provider `trae` |
| 6 | 扫码/短信登录、每日签到、领免费模型、四策略账号调度、非 401 不换号 | codar2api（AiXiaoc/codar2api） | **设计边界**：`docs/providers/codar2api.md:263-264`（登录、签到、免费模型领取、代理池、账号调度与管理面板继续由 sidecar 承担）、`:175`（四策略 `round_robin`/`weighted_round_robin`/`sticky`/`least_connections` 在 sidecar）、`:289`（账号池调度不实现，仅 token 轮换）、本文件 §12 codar_sidecar「形态」；「非 401 不换号」指本仓直连只在 401 时续期换号（`apps/control/app/adapters/codar.py:185-200,219-233`）；许可证 MIT |
| 7 | 网页 DOM / Noise-XX 对话通道、账号池导入、代理、签到 | muse2api（czg86389-hub/muse2api） | **设计边界**：本文件 §2 muse「已知限制」（网页账号只有浏览器 DOM / Noise-XX WebSocket 通道，本仓不复刻）、`upstreams/upstreams.yaml:40`（同因改为直连官方入口）、`docs/design/frontend-integration-plan.md:55`（账号导入留 sidecar `admin.html`）；许可证 MIT |
| 8 | 浏览器执行器、邮箱渠道、CPA / Grok2API 产物、GrokIQ 联动 | grok-register（kaibush/grok-register） | **执行器外置**：`docs/providers/grok-register.md` §5.4「执行器协议 v1」（属外部执行器职责）、本文件 §3 grok-register「已知限制」（执行器不随仓交付）、`docs/design/frontend-integration-plan.md:77`（外部执行器职责，不进控制面）；执行器入口为 `GROK_REGISTER_EXECUTOR_URL`；许可证 MIT |
| 9 | **本项目取舍（非许可证限制）**：本仓不**转发**上游的原生 `/v1/responses`、`/v1/messages`、`/v1/images` 端点（上游确有这些端点：`upstreams/upstreams.yaml:73`「上游另有 responses/messages/images，本仓未接」），改为在网关层提供等价能力——`messages` / `responses` 由适配器在控制面**合成**（`gptgrok.py:400` 声明、`:1323` / `:1353` 实现，流式也是合成），图片输入走 chat 内嵌 `image_url`（`gptgrok.py:57-59`），独立 `/v1/images/generations` 仍不声明（调用 501，见 [PROTOCOL-MATRIX.md](PROTOCOL-MATRIX.md) §1.1）。上游管理控制台与 iCloud Privacy Mail sidecar 不透传 | gptGrok2api（addxiaoyi/gptGrok2api） | 上游为 **MIT、可入主仓**，未接是本项目自己选的，不是许可证不允许——`upstreams/upstreams.yaml:65`（license MIT）、`:73`（「上游另有 responses/messages/images，本仓未接」）；控制台与 iCloud sidecar 属上游自有面，不透传：`docs/design/frontend-integration-plan.md:97-101`（gptGrok 各项「不进界面」）、`:182`（上游管理控制台结构只借「实时请求」结构）、上游 README 能力概览（分析基线 `0335d62`：管理控制台、iCloud 隐私邮箱 sidecar） |
| 10 | 只做协议层，不做各家上游 Web UI；各家 Web 面板由控制面自建等价界面 | 全局（12 个上游） | **设计边界**：`docs/design/frontend-integration-plan.md:190`（借法结论：仅 sub2api 三页结构与 gptGrok 实时请求列表可借，其余「能力留在 sidecar 内、或许可证不允许复制」）、`:235`（sidecar 内部能力不进界面）、本文件文首说明（无章节锚点，引用待核；运行态信息一律由管理页「服务商」或 `PATCH /api/providers/{id}` 下发） |

> **第 6 条更正（2026-11-06，原句逐字留档）**：上表第 6 条原写「扫码/短信登录、每日签到、领免费模型、四策略账号调度、非 401 不换号」，并把「四策略账号调度」按 `docs/providers/codar2api.md:175,263-264,289` 归为 sidecar 才有的能力。现仓内 `codar` 直连已自带本地调度：`apps/control/app/services/scheduling.py:34` 的四档是 `sticky` / `priority` / `weighted` / `failover`，解析与降级规则在 `:69-135`，接在 `codar.py:170-171,242-248`；失败冷却用 `apps/control/app/services/rotation.py:260-281`。「非 401 不换号」也不成立：`codar.py:40-48` 的失败动作表把网络层/5xx 归 `rotate`、429 归 `cool`、401 归 `refresh`，换号与冷却都会发生。**仍有差异**：上游四策略是 `round_robin` / `weighted_round_robin` / `sticky` / `least_connections`（`docs/providers/codar2api.md:175`），本仓是 `priority` / `sticky` / `weighted` / `failover`，只有 `sticky` 同名且语义相近，不能一一对应验收；扫码/短信登录、签到、免费模型领取仍不做。
> 误判后果：继续按「不做」读，会把已落地的本仓调度记成缺口、重复排期；反过来若以为与 sidecar 四策略同名等价，会按 `least_connections` 找一个本仓不存在的实现，或把 `round_robin` 的验收标准套到 `failover` 上。

> **第 3 条引用更正（2026-11-06，原句逐字留档）**：上表第 3 条括注原写「`apps/control/app/routers/accounts.py:21-23`（trae_sidecar 声明 oauth/credits，但账号在 sidecar 内部，主仓两个方法一个都没实现）」。该括注已与代码不符：`apps/control/app/adapters/trae_sidecar.py:23-32` 的 `oauth` / `checkin` / `credits` 均为 `False`；`accounts.py:21-23` 现写「以实际覆写判定能力，不看 spec.capabilities 声明」，真实判定在 `accounts.py:25-42` 的 `_account_ops`（按 `_implements` 与能力位组合）；该适配器也只有 `__init__`（`trae_sidecar.py:43`）。第 3 条「控制面不做 trae 的 OAuth / 积分刷新」结论不变，依据改为上面两处。
> 误判后果：照旧括注会把「声明了能力位但没实现」当成缺陷，去补适配器方法或改能力位；实际是本来就不声明、账号能力留在 sidecar，补方法反而会越过许可证/进程边界。

> **第 9 条的原因性质与第 1、2、4、5 条不同**：1/2/4/5 是许可证导致的隔离或禁用（GPL / LGPL / 无 LICENSE / PolyForm-NC），9 是本项目主动选择不接（上游 MIT 允许），两者不可混为一谈。

> **第 9 条补记（2026-11-04，原句保留作历史）**：表中「`/v1/responses`、`/v1/messages`、`/v1/images` 未接」**仅指上游三个端点未接**，这一结论不变；但同日后续修复改变了两项关联现状——
> ① **工具调用已实现**（不属「未接」）：`apps/control/app/adapters/gptgrok.py` 以提示词注入实现，请求侧拼 `[available tools]`（工具名 / description / parameters schema）与 `[tool call protocol]`（`gptgrok.py:128-142`），模型以行首 `[tool_call] ` + 单行 JSON 回吐，`_ToolCallScanner` 增量解析回标准 OpenAI `tool_calls`（含 `id` / `type: function` / `function.name` / `function.arguments`），流式跨帧拼装、有调用时 `finish_reason=tool_calls`，多轮 tool 消息保留 `tool_call_id`（`[tool result <id>]`）；输出格式是本仓自定 `[tool_call]` JSON，与上游 `<tool_calls>` XML 同机制不同表达，非复刻上游源码；
> ② **图片按通道分流**：GPT 通道透传 `image_url`（远程 URL 与 base64 data URL 两形态，`gptgrok.py:57-59`），Grok 通道请求体只有文本 transcript、带图显式 400（`gptgrok.py:721-727`）——故「图片不支持」不再成立，未接的只是上游 `/v1/images` 端点。
> 详见 `docs/providers/gptgrok2api.md` §3 再更正块与 `providers/gptGrok2api/README.md` 对应段。

> **第 9 条措辞更正（2026-11-05，原句逐字留档）**：上表第 9 条原写「`/v1/responses`、`/v1/messages`、`/v1/images` 未接」（同一句见下方 2026-11-04 补记首句）。这句话把两层事实压在了一句话里，读者会直接读成「网关对这三个路径返回 501」。分层事实如下，两层不要混写：
> ① **上游端点层（上游有没有）**：gptGrok2api 上游**确有**这三个原生端点——`upstreams/upstreams.yaml:73` 写明「上游另有 responses/messages/images，本仓未接」。所以「未接」指的是本仓**不转发**它们，不是「上游不存在」。
> ② **网关能力位层（网关给不给）**：`messages` / `responses` 在网关**是有的**，由适配器在控制面**合成**（声明 `apps/control/app/adapters/gptgrok.py:400`，实现 `:1323`（responses）/ `:1353`（messages），流式同为合成，不是透传）；图片输入走 chat 内嵌 `image_url`（`:57-59`）。只有**独立**的 `/v1/images/generations` 未声明、调用 501——该端点的 provider 列里没有 gptgrok，见 [PROTOCOL-MATRIX.md](PROTOCOL-MATRIX.md) §1.1。
> ③ 原 2026-11-04 补记首句「仅指上游三个端点未接，这一结论不变」方向没错，但默认读者能自己分清「端点」与「能力位」；实测读法就是会误读，故由本块的分层写法取代，补记其余内容（工具调用已实现、图片按通道分流）仍然有效。
> 误判后果：客户端把「未接」当网关 501，会绕开网关直连 ChatGPT Web 或降级到 chat 兼容层——丢鉴权、配额、失败限流与记账；运维会把它当能力缺口，去适配器里重复开位或重复提 issue。与 [PROTOCOL-MATRIX.md](PROTOCOL-MATRIX.md) §1 / §1.1 / §1.2 已对表，无新矛盾。

> 变更记录（2026-11-05，格式迁移）：附表的 8 处自身行号引用（原行号 3 / 97 / 105 / 157 / 165 / 173 / 181 / 194）已迁移为章节锚点——§2 muse、§3 grok-register、§8 trae_sidecar、§9 deepseek_sidecar、§10 qoder_sidecar、§12 codar_sidecar、§13 qoder；其中原行号 3 指向文首说明、无对应章节，改写为「文首说明」并标注「引用待核」。仅迁移引用形式，未改任何能力位结论，未重排表格。附表导语的声明同步改为「引用以章节锚点为准，行号仅作辅证」。
