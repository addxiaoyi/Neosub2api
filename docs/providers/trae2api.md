# trae2api 协议契约（clean-room 重写）

> 依据：Trae IDE 客户端与 `trae-api-cn` / `api5-normal` 的公开协议行为、Solo 与 Work 两条通道的
> 抓包字段形状，以及按该契约请求时观察到的 HTTP / SSE 行为。本文件只记录协议事实与实现取舍，
> 不含上游源码或二进制。
> 主仓实现：`apps/control/app/adapters/trae.py`（MIT，自写设备头、JWT 解析、设备 ID、流式转换）。
> 能力位：`chat`、`models`；协议：`openai.chat.completions`。

## 1. 两条通道与端点

| 通道 | Host | 方法 | 路径 | 用途 |
| --- | --- | --- | --- | --- |
| Solo | `https://trae-api-cn.mchost.guru` | POST | `/api/agent/v3/llm_utils_chat` | 单端点 chat，SSE |
| Solo | 同上 | POST | `/api/ide/v1/get_detail_param` | 模型列表 |
| Work | `https://api5-normal.mchost.guru` | POST | `/api/agent/v3/create_agent_task` | 创建任务，返回会话/任务 ID |
| Work | 同上 | POST | `/api/agent/v3/workflow/start` | 启动工作流 |
| Work | 同上 | POST | `/api/agent/v3/query_history_state` | 轮询任务状态 |
| Work | 同上 | POST | `/api/agent/v3/sync_history_state` | 同步历史 / 取增量输出 |
| OAuth | `https://api.trae.com.cn` | POST | `/cloudide/api/v3/trae/oauth/ExchangeToken` | 登录换 token（不在网关链路内） |

默认模型：Solo `glm-5.2`，Work `DeepSeek-V4-Flash-Official`。
Work host 可用 `options.work_host` 覆盖；Solo host 由 provider 的 `base_url` 决定。

## 2. 鉴权与设备头（本仓自写）

`api_key` 填登录后的 JWT（允许 `Bearer ` 前缀，代码会自动剥掉）。

通用头：

| 头 | 值 |
| --- | --- |
| `authorization` | `Cloud-IDE-JWT <jwt>` |
| `x-cloudide-token` / `x-ide-token` | `<jwt>` |
| `x-uid` | `options.uid` → JWT claim（`uid`/`user_id`/`userId`/`sub`）→ JWT 哈希前 16 位 |
| `x-machine-id` | `options.machine_id` → `sha256("trae-machine:" + jwt)[:32]` |
| `x-device-id` | `options.device_id` → `sha256("trae-device:" + jwt)[:32]` |

Solo 额外头：`x-app-id=6eefa01c-1036-4c7e-9ca5-d891f63bfcd8`、`x-ide-version=0.1.52`、
`x-ide-version-code=20260811`、`x-device-type=macos`、`x-os-version=macOS 15.7.4`、
`x-device-brand=Apple`、`request-traffic-type=prod`。

Work 额外头：`x-app-id`（chat 用上面的 Chat App ID，任务用 `931506`）、`x-ide-version=0.1.63`、
`x-ide-version-code=20260901`、`x-version-code=20260901`、`x-platform` / `x-os` / `x-os-type` /
`x-system` / `x-device-platform` 全为 `darwin`。

设备 ID 与 machine ID 是**从 JWT 确定性派生**的，同一 token 每次请求都一致；换 token 即换设备身份。
没有把上游的随机设备指纹实现搬进来，也没有本地 sidecar 依赖。

## 3. Solo 请求 / 响应

请求体（OpenAI Chat Completions 进入后改写）：

```json
{
  "model": "glm-5.2",
  "function": "solo_work_lite",
  "config_name": "glm-5.2",
  "stream": true,
  "messages": [{"role": "user", "content": [{"type": "text", "text": "你好"}]}],
  "tools": [{"type": "function", "function": {"name": "f", "parameters": "<JSON 字符串>"}}],
  "tool_choice": "f"
}
```

改写要点：

- `messages[].content` 一律转成 `[{type:"text",text}]`；`assistant` 带 `tool_calls` 时 `content` 置 `null`，
  并把每个 `tool_calls[].function.arguments` 从字符串解析成对象放进 `function_call`。
- `tools[].function.parameters` 序列化成字符串。
- `tool_choice="none"` 时同时删掉 `tools` 与 `tool_choice`；其余情况压平成函数名。
- 上游恒为 SSE，聚合模式由本仓自己收流后合成 `chat.completion`。

上游 SSE 事件：

| event | 字段 | 转换 |
| --- | --- | --- |
| `output` | `response` / `reasoning_content` | `delta.content` / `delta.reasoning_content` |
| `output` | `tool_calls` | 按 index 合并到 `delta.tool_calls` |
| `token_usage` | `prompt_tokens` / `completion_tokens` / `total_tokens` | 末帧 `usage` |
| `done` | `finish_reason` | `choices[0].finish_reason`，随后 `data: [DONE]` |

`done` 缺失时按 `stop` 收尾，不会让下游流悬挂。

## 4. Work 通道

两种模式，`options.work_mode`：

- `stream`（默认）：`create_agent_task` 直接返回 SSE，事件 `plan_item` / `output` / `token_usage` / `done` / `error`。
  `plan_item.thought` 与 `tool_call_info.params.summary` 作为推理增量；`output.choices[].text` 作为正文增量；
  `done.last_assistant_response`（JSON 数组字符串）兜底补最后一段正文。
- `stateful`：多步桥
  1. `POST create_agent_task` → 取 `conversation_id` / `session_id` / `task_id`（缺省时本地生成 UUID）；
  2. `POST workflow/start`（带上 `user_input`）；
  3. 循环 `POST query_history_state` + `sync_history_state`，间隔 `options.work_poll_interval`（默认 0.5s），
     上限 `options.work_poll_limit`（默认 120 次）；
  4. 任一步返回完成标记即停；取不到正文返回 502（不伪造空回答）。

积分不足自动降级：Work 首帧或 `error` 事件里命中 `1005 / 4008 / 4001 / 4000105`（套餐/积分受限）时，
收掉当前连接并改走 Solo；`4011`（软限流）按 429 处理。

## 5. 对外协议

只声明 `openai.chat.completions`，网关路由：

| 网关路径 | 行为 |
| --- | --- |
| `POST /v1/chat/completions` | JSON / SSE，模型名直接作为 `config_name` |
| `GET /v1/models` | 转发 `get_detail_param`，条目提取 `id` / `config_name` / `model`；空列表视为「在线但无模型」 |

不提供 `messages` / `responses`：Trae 上游只有 chat 语义，跨协议转换会把 `tool_use` 块语义抹平，
交给 qoder 的产品线路由更合适（`qoder` 的 `trae` 前缀会返回 501 并指引改用本 provider）。

## 6. 错误分类

| 上游 | 网关返回 | 说明 |
| --- | --- | --- |
| 401 | 401 | JWT 失效，提示重新登录更新 `api_key` |
| `code ∈ {1005,4008,4001,4000105}` | 429 | 积分/套餐受限；Work 侧先降级 Solo |
| `code=4011` 或 429 | 429 | 限流 |
| 404 | 404 | 端点不存在 |
| 5xx | 502 | 上游异常 |
| 其余 | 400 | 请求被拒，附上游原文前 200 字 |

## 7. 验证与未验证部分

已验证（`apps/control/tests/test_f2.py`，12 个用例）：

- Solo 请求体重写（content 块化、tool_calls 解析、parameters 字符串化、`tool_choice=none` 删工具）；
- Solo 设备头形状（`authorization` / `x-app-id` / `user-agent` / machine id 长度）；
- Solo SSE 转换：正文、推理、usage、`done`、缺失 `done` 兜底、聚合；
- Work 原生 SSE（`plan_item` + `output` + `done`）与积分不足降级 Solo；
- 错误分类与 health。

未在真机验证：

- Work `stateful` 桥的三段请求体是**按契约字段尽力构造**的（上游未公开抓包），字段名容错已做
  （`conversation_id`/`conv_id`、`session_id`/`sess_id`、`task_id`/`id`），但真实账号下的响应形状未回归；
- Solo 侧 `tool_choice` / 多轮工具调用只覆盖改写与流式合并，没有真实工具执行回合；
- OAuth `ExchangeToken` 仅登记端点，未接入 `POST /api/auth/start`（该路由走 CodeBuddy 适配器）。
