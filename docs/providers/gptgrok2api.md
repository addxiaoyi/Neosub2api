# GPT/Grok 协议契约（仓内实现）

> 状态：**仓内实现（直连）**。实现在 `apps/control/app/adapters/gptgrok.py`，
> 不复制上游源码，只按协议行为重写；本仓自研，MIT。
> 依据：`addxiaoyi/gptGrok2api` 的实测记录（对 ChatGPT Web 与 Grok Web 的逆向结论）。
> **未用真实凭证验证**的项在 §6 单列。

## 1. 通道与端点

按模型名前缀分流（`options.channel` 可强制指定 `chatgpt` / `grok`）：

| 通道 | 默认域 | 对话端点 | 健康探测 |
| --- | --- | --- | --- |
| ChatGPT | `https://chatgpt.com` | `POST /backend-api/conversation` | `GET /backend-api/me` |
| Grok | `https://grok.com` | `POST /rest/app-chat/conversations/new` | `GET /rest/models` |

`options.chatgpt_base` / `options.grok_base` 可单通道覆盖上游域；
把 `base_url` 改成非默认值时，两通道统一走该入口（联调/私有网关用）。

## 2. 鉴权

| 通道 | 形态 |
| --- | --- |
| ChatGPT | `authorization: Bearer <accessToken>` + OAI 指纹头 |
| Grok | `cookie: sso=<token>; sso-rw=<token>`（凭证本身已是 Cookie 串时原样使用） |

ChatGPT 指纹头（`options.fingerprint=true` 时注入）：

| 头 | 值 |
| --- | --- |
| `user-agent` | 桌面 Chrome UA（可被 `options.user_agent` 覆盖） |
| `oai-device-id` | `md5("device:" + token)` 的 UUIDv4 形态 |
| `oai-session-id` | `md5("session:" + token)` 的 UUIDv4 形态 |
| `oai-client-version` / `oai-client-build-number` | `options.oai_client_version` / `oai_client_build_number` |
| `openai-sentinel-chat-requirements-token` | `options.sentinel_token`（可选注入） |
| `x-openai-target-path` / `x-openai-target-route` | `/backend-api/conversation` |

`options.fingerprint` 关闭时**不注入任何自定义头**（`extra_headers` 也不注入），
与代理版语义一致。`options.proxy`、`options.timeout` 沿用。

## 3. 请求体

ChatGPT：`action=next`、`messages[].author.role`、`content.parts`、
`conversation_mode.kind=primary_assistant`、`parent_message_id=client-created-root`、
`timezone_offset_min=-480`。多轮对话按 OpenAI messages 逐条映射。

Grok：`message` 是把所有轮次拍平成 `[role]: text` 的 transcript；
`modeId` 由模型名推 `fast|expert|heavy|auto`，`isReasoning` 取模型名含 `reasoning`；
其余开关（`temporary`、`disableMemory`、`disableSearch`、`sendFinalMetadata` 等）固定为默认值。

两家都没有原生 function calling：`tools` 不注入，tool 消息降级成 `[tool result]` 文本。（原表述，2026-11-04 已更正，见下）

> 2026-11-04 更正（上句原文保留作历史）：原表述把「本仓不做」说成了「上游也没有」。上游在分析基线
> `0335d62` 已有工具调用实现，源文件 `internal/protocol/tools.go`，做法是提示词注入——把工具清单与
> `<tool_calls>` XML 输出格式写进请求提示词，再解析模型回吐的调用块，并非 OpenAI 原生 `tools` 字段。
> 正确口径：上游不支持 OpenAI 原生 `tools` 字段，但通过提示词注入实现了等价工具调用能力；本仓尚未复刻
> 该注入逻辑，带 `tools` 的请求当前会被降级（工具定义不注入、tool 消息压成 `[tool result]` 文本，
> `gptgrok.py:190-192`）。这是**已知缺口**，不是「上游也不支持」。

> 2026-11-04 再更正（上块结尾「本仓尚未复刻…属已知缺口」已过时，原句保留作历史）：本仓**已实现**
> 提示词注入式工具调用（`apps/control/app/adapters/gptgrok.py`）。注入机制：请求侧拼 `[available tools]`
> （工具名 / description / parameters schema）与 `[tool call protocol]` 输出协议（`gptgrok.py:128-142`），
> `tool_choice` 的 `required` 模式附加强制调用指令（`:138-141`）；模型以行首 `[tool_call] ` + 单行 JSON
> 回吐，由 `_ToolCallScanner`（`:145-`）增量解析回标准 OpenAI `tool_calls`（`id` / `type: function` /
> `function.name` / `function.arguments`，`_tool_call_item` `:235-242`），流式跨帧拼装、有调用时
> `finish_reason=tool_calls`；多轮 `tool` 消息保留 `tool_call_id`（`[tool result <id>]`，`:389-392`）。
> **与上游的差异**：本仓输出格式为自定的 `[tool_call]` + 单行 JSON，与上游 `<tool_calls>` XML 属同机制
> 不同表达，不是复刻上游源码（`gptgrok.py:47` 注释即此意）。**图片分流**：GPT 通道透传 `image_url`
> （远程 URL 与 base64 data URL 两形态，`:57-59`）；Grok 通道请求体只有文本 transcript，带图显式 400
> （`:721-727`）。能力位声明未变，`tools` 不再降级。

## 4. 流式帧

ChatGPT：`data: {json}` SSE，`data: [DONE]` 收尾；取
`message.author.role=assistant` 的 `content.parts` 累积文本，按前缀差算出增量；
`message.status ∈ {finished_successfully, finished_details, finished}` 结束。

Grok：**裸 JSON 行**（每行一个对象）；取 `result.response.token` 增量，
`isThinking=true` 的 token 归入 `reasoning_content`；`isSoftStop` 或 `finalMetadata` 结束。

本仓把两种帧统一翻译成 OpenAI chunk（`role` → `content` / `reasoning_content` →
`finish_reason=stop` → `[DONE]`），逐帧下发，不拼接整包。

## 5. 错误

| 状态 | 本仓处理 | 是否换 key |
| --- | --- | --- |
| 401 / 403 | 原码（403 提示风控或 sentinel） | 是 |
| 429 | 原码；Grok 流内 `error.code=8` 也归 429 | 是 |
| 5xx | 原码 | 是 |
| 400 / 413 / 422 | 原码 | 否 |
| 404 | 原码 | 否 |

## 6. 未能验证的部分

- **未用真实账号跑通**：ChatGPT 的 sentinel token 本地无法生成，缺失时上游可能直接 403；
  本仓只支持 `options.sentinel_token` 手工注入。
- `GET /backend-api/me`、`GET /rest/models` 作为健康探测端点是**推断**，未实测；
  失败时 `health()` 返回 `connected=false`，可用 `options.health_path` 改探针。
- ChatGPT 的 `oai-device-id`/`oai-session-id` 派生算法不保证与官方客户端一致，
  仅保证同一凭证稳定；`options.extra_headers` 可覆盖为真实值。
- Grok 的 `modeId` 与 `modelName` 组合是否被上游接受未验证。
- 两通道都没有 token 计数，本仓不返回 `usage`（记账侧按 `usage_missing` 处理）。
- **grok SSO + 出口 IP 绑定：不做（2026-11-07 拍板）。** 上游只有 README 级说法，没有可核实的一手代码证据——既没有匹配字段，也没有校验周期；本仓凭据只认 `sso` Cookie 串，不做登录态复用或出口 IP 与账号的绑定。**上游未提供契约，本方不实现**，不排期（档位登记见 [FEATURE-GAP.md](../FEATURE-GAP.md) U-07）。误判后果：按「待实现」排期只能照 README 猜字段与校验周期，等于自造协议。

## 7. 适配器选项（键名沿用代理版）

| 键 | 作用 |
| --- | --- |
| `fingerprint` | 是否注入 OAI 指纹头与 `extra_headers` |
| `proxy` | HTTP 代理 |
| `extra_headers` | 自定义头（覆盖同名默认头） |
| `timeout` | 单次上游超时，默认 180s |
| `channel` | 强制通道（`chatgpt` / `grok`），同时过滤模型目录 |
| `chatgpt_base` / `grok_base` | 单通道上游域覆盖 |
| `sentinel_token` / `oai_client_version` / `oai_client_build_number` | ChatGPT 指纹细节 |
| `models` / `health_path` / `health_channel` | 模型目录与健康探测覆盖 |
| `extra_keys` / `max_attempts` | 多凭证轮换（`api_key` + `extra_keys`） |
