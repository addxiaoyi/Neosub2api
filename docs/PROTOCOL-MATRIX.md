# 协议差异表（Provider Protocol Matrix）

本表是 14 家已注册 provider 的协议事实清单，**以 `apps/control/app/adapters/*.py` 与 `ProviderSpec` 为准**，不引用旧文档口径。与 `PROVIDERS.md` 冲突时改本表；与代码冲突时改本表。

复核方式（在仓库根执行；改代码后输出应与下面三张表逐行一致）：

```powershell
python -c "import sys,inspect; sys.path.insert(0,'apps/control'); import app.adapters as p; from app.adapters.base import ProviderAdapter as B; mods=[m for m in vars(p).values() if inspect.ismodule(m)]; cls=[c for m in mods for c in vars(m).values() if isinstance(c,type) and issubclass(c,B) and c is not B]; [print(c.spec.id,[k for k,v in c.spec.capabilities.items() if v],c.spec.protocols,c.spec.risk_level) for c in cls if getattr(c,'spec',None)]"
```

## 1. 能力位

`✓` = `capabilities[...] is True`，`—` = `False`，调用返回 `501 capability_unsupported`（`base.py` 的桩）。`register` 不是推理能力位。

| provider | chat | messages | responses | register | runtime | readiness |
| --- | :-: | :-: | :-: | :-: | --- | --- |
| `codebuddy` | ✓ | ✓ | — | — | 管理端直连（7864 → 7863） | needs_config |
| `muse` | ✓ | — | — | — | 内置直连 | needs_config |
| `grok-register` | — | — | — | ✓ | 控制面任务服务 | needs_config |
| `gptgrok` | ✓ | — | — | — | 内置直连 | needs_config |
| `zcode` | ✓ | ✓ | — | — | 内置直连 | needs_config |
| `opencode` | ✓ | ✓ | — | — | 内置直连 | ready |
| `sub2api` | ✓ | ✓ | — | — | 内置直连（账号池分发） | needs_config |
| `codar` | ✓ | ✓ | ✓ | — | 内置直连 | needs_config |
| `codar_sidecar` | ✓ | ✓ | — | — | sidecar 直通（8801） | needs_config |
| `trae_sidecar` | ✓ | — | — | — | sidecar 直通（7871） | needs_config |
| `deepseek_sidecar` | ✓ | ✓（仅非流式） | — | — | sidecar 直通（5001） | needs_config |
| `qoder_sidecar` | ✓ | ✓ | — | — | sidecar 直通（7873） | needs_config |
| `qoder` | ✓ | ✓ | ✓ | — | 内置直连（COSY） | ready |
| `trae` | ✓ | — | — | — | 内置直连（Solo/Work） | ready |

`responses` 只有 `codar`、`qoder` 两家声明，与 `API-REFERENCE.md` §「/v1/responses」一致。

## 2. 鉴权与上行入口

| provider | 鉴权头 | 上游对话入口 |
| --- | --- | --- |
| `codebuddy` | `Authorization: Bearer wbt_…`（管理端作用域 token） | 管理端 `/v1/chat/completions`、`/v1/messages`（转发到 workbuddy2api 7863） |
| `muse` | `Authorization: Bearer <api_key>`；可附 `cookie: hatch_sess=…` | OpenAI 兼容入口的 `chat_path`（默认 `/v1/chat/completions`） |
| `grok-register` | 无；控制面不持上游凭据 | 外部执行器 `GROK_REGISTER_EXECUTOR_URL`（env，非 provider 地址） |
| `gptgrok` | ChatGPT：`Authorization: Bearer <accessToken>` + `oai-device-id` / `oai-session-id` + OAI 指纹头；Grok：`Cookie: sso=…; sso-rw=…` | ChatGPT `POST /backend-api/conversation`；Grok `POST /rest/app-chat/conversations/new` |
| `zcode` | `authorization: Bearer <JWT>` 与 `x-api-key: <JWT>` 双写 + `anthropic-version` / `anthropic-beta` + 必需 `x-device-mid` | `POST /api/v1/zcode-plan/anthropic/v1/messages` |
| `opencode` | OpenAI：`Authorization: Bearer`；Anthropic：`x-api-key` + `anthropic-version: 2023-06-01` | 上游 `/v1/chat/completions`、`/v1/messages`（`tier=go` 加 `/go` 前缀） |
| `sub2api` | `Authorization: Bearer <账号 api_key>` | 账号自带 `upstream_base` 下的 `/v1/chat/completions`、`/v1/messages` |
| `codar` | `Authorization: Bearer <Codar JWT>`（多 token 轮询）+ `user-agent: codar-desktop/…`；Anthropic 额外 `anthropic-version`（可配 `anthropic-beta`） | `/v1/chat/completions`、`/v1/messages`、`/v1/responses` |
| `codar_sidecar` | `Authorization: Bearer sk-codar-…`（sidecar 本地 key） | sidecar `/v1/chat/completions`、`/v1/messages` |
| `trae_sidecar` | 控制面不填（账号池与 OAuth 在 sidecar 内） | sidecar `/v1/chat/completions` |
| `deepseek_sidecar` | 控制面 api_key 可空（登录与刷新在 sidecar 内） | `/v1/chat/completions`；Anthropic **非标** `/anthropic/v1/messages` |
| `qoder_sidecar` | `Authorization: Bearer <sidecar key>` | sidecar `/v1/chat/completions`、`/v1/messages` |
| `qoder` | `authorization: Bearer COSY.<payload>.<sig>` + `cosy-key` / `cosy-date` / `cosy-user` / `cosy-machineid` / `cosy-version` / `cosy-clientip` | `POST /algo/api/v2/service/pro/sse/agent_chat_generation?Encode=1`；workbuddy 走 `/v2/chat/completions` |
| `trae` | `authorization: Cloud-IDE-JWT <jwt>` + `x-cloudide-token` + `x-ide-token` + `x-uid` / `x-app-id`（Solo / Work 两套头） | Solo `POST /api/agent/v3/llm_utils_chat`；Work `create_agent_task` 或 `workflow/start` + 轮询 |

## 3. 流式与 SSE

「透传」= 控制面只搬字节，不改帧；「翻译」= 适配器逐帧改写成对外协议的帧。

| provider | chat 流式 | messages 流式 | responses 流式 | 上游帧形态 |
| --- | :-: | :-: | :-: | --- |
| `codebuddy` | ✓ 透传 | ✓ 透传 | ✗ 501 | OpenAI `data:` / Anthropic `event:`+`data:` |
| `muse` | ✓ 透传 | ✗ 501 | ✗ 501 | OpenAI `data:` + `[DONE]` |
| `grok-register` | — | — | — | 无 SSE（任务状态机） |
| `gptgrok` | ✓ 翻译 | ✗ 501 | ✗ 501 | ChatGPT：`data:` 帧，增量语义 `{"o":"append","v":…}`；Grok：逐行 JSON `result.response.token`（`isThinking` 区分思维链），`isSoftStop` / `finalMetadata` 收尾 |
| `zcode` | ✓ 翻译 | ✓ 透传 | ✗ 501 | Anthropic 事件：`message_start` → `content_block_start` → `content_block_delta` → `content_block_stop` → `message_delta` → `message_stop` |
| `opencode` | ✓ 透传 | ✓ 透传 | ✗ 501 | OpenAI `data:` / Anthropic `event:`+`data:` |
| `sub2api` | ✓ 透传（边转发边攒 usage） | ✓ 透传 | ✗ 501 | 由账号上游决定，OpenAI / Anthropic 原样 |
| `codar` | ✓ 透传 | ✓ 透传 | ✓ 透传 | OpenAI `data:` / Anthropic `event:` / Responses `event:` |
| `codar_sidecar` | ✓ 透传 | ✓ 透传 | ✗ 501 | 同上（sidecar 侧决定） |
| `trae_sidecar` | ✓ 透传 | ✗ 501 | ✗ 501 | OpenAI `data:` + `[DONE]` |
| `deepseek_sidecar` | ✓ 透传 | **✗ 恒 501** | ✗ 501 | OpenAI `data:` + `[DONE]`；Anthropic 端点无流式实现 |
| `qoder_sidecar` | ✓ 透传 | ✓ 透传 | ✗ 501（sidecar 有 `/v1/responses`，控制面未声明） | OpenAI / Anthropic |
| `qoder` | ✓ 翻译 | ✓ 合成 | ✓ 合成 | 上游是**信封行** `{"body":"<内层 JSON 或 [DONE]>","statusCodeValue":200}`，内层才是 OpenAI chunk；对外 messages 合成 `message_start…message_stop`，responses 合成 `response.created` → `response.output_item.added` → `response.output_text.delta` → `response.output_text.done` → `response.completed` |
| `trae` | ✓ 翻译 | ✗ 501 | ✗ 501 | 上游内部事件名 `output` / `token_usage` / `done`（异常走 error 帧）；Solo 上行强制 `stream=true` |

## 4. 错误码风格

| provider | 错误载体 | 关键映射 |
| --- | --- | --- |
| `codebuddy` | HTTP 状态 + 管理端 body 原样 | 会话级管理员操作被拒是 **403**，不掩盖 |
| `muse` | HTTP 状态 + 厂商 body | 401/403/408/429/5xx 可重试并换 key；其余 4xx 直接返回（`retryable=False`） |
| `grok-register` | 任务状态 + `failure_reason` | 未配执行器 → 入队后 `failed`，不伪造成功 |
| `gptgrok` | 流内 `{"error":{code,message}}` | ChatGPT 流内错误 → 502；Grok `code=8` / rate limit → 429 |
| `zcode` | Anthropic `error` 帧 + 业务码 | 3012 → 403（system 指纹缺失，不可重试）；1005 / 3009 → 429（可换 key）；3001 → 400（x-device-mid 缺失）；401 → 401；429 → 429；404 → 404；5xx → 502 |
| `opencode` | HTTP 状态 | 400/413/422 立即返回；401/403/408/429/5xx 换 key 重试（0.5s→2s） |
| `sub2api` | HTTP 状态 + 配额语义 | 未建分组/无账号 → 503；账号池全冷却或额度耗尽 → 429；账号级 429/5xx → 冷却 60s 后换号 |
| `codar` | HTTP 状态 | 401 → `POST {pay_base}/v1/token/refresh` 续期后重试，全失败才 401 |
| `codar_sidecar` | sidecar 原样 HTTP | OpenAI / Anthropic 外壳照搬 |
| `trae_sidecar` / `deepseek_sidecar` / `qoder_sidecar` | sidecar 原样 HTTP | 控制面不改写，连不上 → 502/503 |
| `qoder` | 信封 `statusCodeValue` + 内层 `code` | HTTP 401 或 code `12153` / 文案 `SessionDead` / `AuthRejected` / `HardCredit` → **429** 并带 `session_dead` 标记（需重新登录）；HTTP 429 → 429（带 `Retry-After`）；404 → 404；5xx → 502；`trae` 产品线显式 501 |
| `trae` | 业务 `code` | `{1005,4008,4001,4000105}` → 429（Work 首帧即积分错误时自动降级 Solo）；`4011` / 429 → 429；401 → 401；404 → 404；5xx → 502 |

## 5. 许可证风险

| 级别 | provider | 依据 |
| --- | --- | --- |
| safe | `codebuddy`、`gptgrok`、`muse`、`trae_sidecar`、`codar_sidecar`、`opencode`、`sub2api`、`codar`、`qoder`、`trae`、`zcode`、`grok-register` | 上游 MIT，或本仓 clean-room 重写（MIT） |
| danger | `deepseek_sidecar` | 上游 Deepseek2Go **无 LICENSE**（All Rights Reserved），商用前必须移除 |
| danger | `qoder_sidecar` | 上游 **PolyForm Noncommercial 1.0.0**，禁止任何商用 |

风险级取自 `ProviderSpec.risk_level`；证据与完整清单见 `THIRD-PARTY-NOTICES.md`。

## 6. 已知坑（按踩坑概率排序）

1. **`deepseek_sidecar` 的 messages 流式恒 501**：`messages(stream=True)` 直接 `raise ProviderError(501)`，因为 sidecar 的 `/anthropic/v1/messages` 只实现非流式。别把它写成「支持流式」，客户端会拿不到任何帧。
2. **`deepseek_sidecar` 的 Anthropic 路径非标**：`/anthropic/v1/messages`，不是 `/v1/messages`，改成标准路径会 404。
3. **`muse` / `trae` / `trae_sidecar` / `gptgrok` 没有 messages**：调用 `/v1/messages` 是 501，不是 400；模型路由命中后由网关统一拒绝。
4. **`codar_sidecar` 与 `qoder_sidecar` 的 responses 未声明**：上游 sidecar 可能有该端点，但控制面声明为 False，调用 501；要开就得先补适配器与测试。
5. **`qoder` 的 `trae` 产品线 501**：COSY 网关不接 Trae 模型，改用 provider `trae`。
6. **`zcode` 缺 `official_system` 必回 3012**：上游做 system 内容指纹校验，适配器不内置官方原文。
7. **`gptgrok` 缺 sentinel token 可能 403**：ChatGPT 通道无法本地生成 sentinel，只能 `options.sentinel_token` 注入。
8. **`trae` 的 Work 通道只在首帧积分错误时降级 Solo**：流到一半出错不换通道，避免重复计费。
9. **`sub2api` 池空必 503**：没建分组/账号时不是 401，是 503。
10. **`codebuddy` 的 403 是权限而非鉴权失败**：`wbt_` token 对删除账号 / 重启 / 设置下发可能被拒，属会话级管理员限制。

## 7. 与既有文档的核对结论

逐条比对 `PROVIDERS.md`、`API-REFERENCE.md`、`docs/providers/*.md` 后，**未发现与代码冲突的硬性表述**，因此本表没有「纠正旧结论」的条目。旧文档只有三处给了结论、没给协议细节，本表补齐：

1. `deepseek_sidecar`：旧文只写「仅非流式」，本表把「恒 501」写进能力位与流式两列，避免被理解成「参数不支持时降级」。
2. `qoder`：旧文只写「信封 SSE」，本表给出信封字段与外层/内层关系，以及 messages / responses 是适配器合成而非透传。
3. `gptgrok`：旧文只写「OpenAI 协议」，本表区分 ChatGPT 的 `data:` 增量帧与 Grok 的逐行 JSON 两种上游形态。

若后续发现旧文档与本表冲突，以 `adapters/*.py` 为唯一事实来源，改文档不改代码语义。
