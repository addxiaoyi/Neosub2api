# ZCode 协议契约（仓内实现）

> 状态：**仓内实现（直连）**。实现在 `apps/control/app/adapters/zcode.py`，
> 不复制上游源码，只按协议行为重写；本仓自研，MIT。
> 依据：`addxiaoyi/ZCode2api` 对 ZCode 客户端 3.16.3 的实测记录（上游为逆向结论，
> 官方未公开契约）。**未用真实凭证验证**的项在 §6 单列。

## 1. 上游端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/zcode-plan/anthropic/v1/messages` | 唯一对话端点，Anthropic Messages 协议，支持 `stream` |
| GET | `/api/v1/zcode-plan/billing/balance?app_version=3.16.3` | 额度查询，本仓用于 `health()` |

默认 `base_url = https://zcode.z.ai`。不再需要 ZCode2api sidecar。

## 2. 鉴权头

同一枚账号 JWT 同时出现在两处：

| 头 | 值 |
| --- | --- |
| `authorization` | `Bearer <JWT>` |
| `x-api-key` | `<JWT>`（不带 Bearer 前缀） |
| `anthropic-version` | `2023-06-01` |
| `anthropic-beta` | `mid-conversation-system-2026-04-07` |
| `x-device-mid` | 设备指纹，**必需**；缺失回 3001 |
| `x-session-id` | `session_<sha256("session:" + uid)[:32]>` |
| `x-query-id` / `x-request-id` / `x-zcode-trace-id` | 每次请求随机 |
| `user-agent` | `ZCode/3.16.3`（可被 `options.user_agent` 覆盖） |
| `x-platform` / `x-title` / `x-release-channel` | `win32` / `zcode-cli` / `stable` |

`uid` 取值顺序：`options.uid` → JWT 中段 `sub|user_id|uid|id` → `sha256(token)[:16]`。
`x-device-mid` 由 `machine:<uid>` 的 md5 拼成 UUIDv4 形态，同一账号稳定不变。

## 3. system 指纹（最容易踩的坑）

上游对 `system` 做**内容指纹校验**：

- `system` 必须是 ZCode 客户端官方原文（约 10701 字符），否则回 **3012**。
- 因此本仓**不内置**该原文，由部署者通过 `options.official_system`
  （或 `options.official_system_prompt`）注入，支持 `str` 或 Anthropic 文本块数组
  （数组项可带 `cache_control`）。
- 有 official system 时，应用层 system（`options.system_prompt_override` 或请求里的
  system/developer 消息）被降级为
  `<pi_system_instructions>...</pi_system_instructions>` 插到首条 user 里。

## 4. 流式帧

`event:` + `data:` 双行，事件序列：

```
message_start → content_block_start → content_block_delta(N) → content_block_stop
→ message_delta(stop_reason/usage) → message_stop
```

- `text_delta` → OpenAI `content`
- `thinking_delta` → OpenAI `reasoning_content`
- `input_json_delta` → 累积为 `tool_calls[].function.arguments`
- 流内 `error` 事件转成一条 `data: {"error":{...}}` 帧后结束

本仓 `chat_completion(stream=True)` 把上述事件翻译成 OpenAI chunk 逐帧下发，
`messages(stream=True)` 则原样透传（保留 `event:` 名与错误帧）。

## 5. 错误码

| 上游码 / 状态 | 本仓映射 | 是否换 key |
| --- | --- | --- |
| 3012 | 403，提示配置 `options.official_system` | 否 |
| 1005 / 3009 | 429（限流） | 是 |
| 3001 | 400（设备指纹无效） | 否 |
| 401 | 401 | 是 |
| 429 | 429 | 是 |
| 404 | 404 | 否 |
| 400 / 422 | 原码 | 否 |
| 5xx | 502 | 是 |

## 6. 未能验证的部分

- 真实 JWT 与官方 system 原文均未拿到，**3012 之外的链路未做真机回归**。
- `billing/balance` 只用于健康探测，响应字段按 `{"code":0,"data":{...}}` 宽松解析；
  真实字段名（`balance` 的单位/币种）未核对。
- `thinking` / `output_config.effort` 是否对所有套餐生效未验证。
- 设备指纹派生算法是「同输入稳定派生」，不保证与官方客户端逐字节一致；
  若 3001 复现，优先用 `options.uid` 对齐账号。

## 7. 适配器选项（键名沿用代理版）

| 键 | 作用 |
| --- | --- |
| `system_prompt_override` | 应用层 system（优先级高于请求里的 system） |
| `official_system` / `official_system_prompt` | 官方 system 原文（缺失即 3012） |
| `uid` | 覆盖设备指纹来源 |
| `timeout` / `proxy` / `extra_headers` | 请求层覆盖 |
| `max_tokens` / `effort` / `platform` / `user_agent` | 请求默认值 |
| `models` | 追加模型目录（默认 `GLM-5.3-Flash`、`GLM-5.3`） |
| `extra_keys` / `max_attempts` | 多凭证轮换（`api_key` + `extra_keys`） |
