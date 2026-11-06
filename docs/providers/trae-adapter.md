# trae（内置直连，本仓实现）

> 适配器：`apps/control/app/adapters/trae.py`（clean-room，设备头 / JWT 解析 / 设备 ID 全自写）。
> 本篇只写**本仓实现**的行为与配置；Solo / Work 通道的协议事实见 [trae2api.md](trae2api.md)。
> 能力位：`chat`、`models`；协议：`openai.chat.completions`（无 `messages` / `responses`，调用返回 501）。

## 1. 是什么

主仓内置的 Trae 客户端，**不经过 trae2api sidecar**，直连 Solo 与 Work 两条通道：

| 通道 | 上游 host | 形态 |
| --- | --- | --- |
| Solo（默认） | `https://trae-api-cn.mchost.guru`（provider `base_url`） | 单端点 `POST /api/agent/v3/llm_utils_chat`，上行强制 `stream=true` |
| Work | `https://api5-normal.mchost.guru`（`options.work_host` 可覆盖） | `create_agent_task` 流式（默认）或 `workflow/start` + 轮询桥 |

## 2. 鉴权与设备头

- `api_key` 填登录后的 **Cloud-IDE JWT**（带不带 `Bearer ` 前缀都行）。
- 头里同时带 `authorization: Cloud-IDE-JWT <jwt>`、`x-cloudide-token`、`x-ide-token`、`x-uid`、
  `x-app-id` 等；`x-machine-id` / `x-device-id` 默认从 JWT 派生（sha256 前 32 位），可用
  `options.machine_id` / `options.device_id` 覆盖；`x-uid` 从 JWT claims
  （`uid` / `user_id` / `userId` / `sub`）解析，解析不到取 sha256 前 16。
- Solo 与 Work 用两套头（`x-app-id`、版本号不同），适配器按通道自动切换。

## 3. 配置项

无专用环境变量。`api_key` 填一条 JWT，多账号时把其余 JWT 放进凭据库（`api_keys`）或 `options.extra_keys`；其余配置走 `options`：

| options 键 | 默认 | 说明 |
| --- | --- | --- |
| `channel` | `solo` | `solo` / `work` |
| `work_mode` | `stream` | Work 通道形态：`stream`（create_agent_task 流式）/ `stateful`（轮询桥） |
| `work_host` | `api5-normal.mchost.guru` | Work 通道 host |
| `uid` / `machine_id` / `device_id` | 派生 | 身份与设备头覆盖 |
| `work_poll_limit` / `work_poll_interval` | 120 / 0.5 | stateful 桥轮询上限与间隔 |
| `extra_keys` | 空 | 额外 JWT（与 `api_keys` 合并去重） |
| `key_weights` | 空 | 账号权重，交给 `CredentialPool` |
| `max_attempts` | 3 | 换号次数上限，夹逼 1–10，且不超过账号数 |
| `timeout` / `proxy` | 300 / 空 | 通用项 |

默认模型：Solo `glm-5.2`；Work `DeepSeek-V4-Flash-Official`（上行自动加 `__dev` 后缀）。

## 4. 请求改写（Solo，本仓行为）

- `content` 字符串统一包成 `[{"type": "text", "text": ...}]`。
- assistant 消息的 `tool_calls`：`function` 复制为 `function_call`，没有名字的调用丢弃，全丢则删整字段。
- `tool_choice` 归一化：`none`（含 `{"type":"none"}`）连 `tools`/`functions` 一起删；
  `auto` / `required` 保留；指定函数则转成函数名字符串。
- `tools[].function.parameters` 对象序列化成 JSON 字符串（上游要求）。

## 5. 错误语义

| 触发 | 网关得到 | 说明 |
| --- | --- | --- |
| HTTP 401 | 401 | JWT 被拒，需重新登录更新 api_key |
| code ∈ `{1005, 4008, 4001, 4000105}`，或文本含 `1005` + `plan` | 402 | 积分/套餐受限；**Work 首帧即此错误时自动降级到 Solo 重发同一请求**；多账号时换号重试并按池 900s 档冷却（`trae.py:337-339,924-958`） |
| HTTP 429 或 code `4011` | 429 | 限流 |
| HTTP 404 | 404 | 端点不存在 |
| HTTP 5xx | 502 | 上游服务异常 |
| 其余 | 400 | 请求被拒 |

> **（2026-11-06 已更正，原单元格逐字留档）** 原写「code ∈ `{1005, 4008, 4001, 4000105}`，或文本含 `1005` + `plan` → 429」。本地 `_classify_error` 实际回 402（`trae.py:337-339`）。误判后果：照旧表会把套餐/积分受限当限流，客户端按 429 退避重试而不是按 `insufficient_quota` 换号或停止；多账号池也会落 429 档（120s）而不是 402 档（900s）。

## 6. 已知限制

- `work_mode=stateful` 的轮询桥按契约字段尽力构造，**未在真机验证**；失败走统一错误分类，不伪造结果。
- usage 来自上游 `token_usage` 事件；不给则流水记 `usage_missing`。
- 上游流中断且没有 `done` 帧时，适配器补一帧 `finish_reason=stop` + `[DONE]` 收尾，避免客户端悬挂。
- Work 降级 Solo 只发生在首帧即积分错误时；流到一半出错不降级。
- 本适配器不产生 408；402 与 5xx（归一成 502）在存在其它可用账号时登记池冷却（`trae.py:202-207,337-346`）。408 档位属于池能力，未接线；唯一账号不冷却，避免把真实 401/402 伪装成全池 429。
- 流式 200 建立后的中途错误无法换号，也不会登记冷却；换号只覆盖响应头返回前的失败。
