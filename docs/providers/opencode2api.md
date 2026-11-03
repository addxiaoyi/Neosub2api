# opencode2api 协议契约（clean-room 提炼）

> 依据：上游项目公开 README、`config.example.json` 示例配置、OpenCode Zen/Go 的公开协议说明，
> 以及按该契约发起请求时观察到的 HTTP 行为。本文件只记录协议事实，不包含上游源码片段。
> 主仓实现：`apps/control/app/adapters/opencode.py`（独立重写，MIT）。

## 1. 对外协议与端点

上游是 OpenCode Zen / Zen Go 的 OpenAI 风格网关，默认 API 监听 `127.0.0.1:8080`，
管理端口 `8081`（WebUI，独立会话，不属于推理协议）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/models` | 当前配置下可路由的模型；附带上下文长度、最大输入/输出、价格、推理档位等元数据 |
| POST | `/v1/chat/completions` | OpenAI Chat Completions，JSON 或 SSE |
| POST | `/v1/responses` | OpenAI Responses |
| POST | `/v1/messages` | Anthropic Messages，JSON 或 SSE |
| POST | `/v1/systemone` | System One 结构化决策；与 chat/responses 不做协议转换，原样转发 |
| GET | `/healthz` | 就绪与资源摘要，**不需要鉴权** |

兼容边界（README 明示）：

- 只做上述路由；不提供 embeddings、文件上传、图像生成、Responses 的检索/取消。
- 不保存会话历史，历史由客户端提供。
- 跨协议请求会经过中间表示，部分字段无等价物（例如 Chat 的 `seed`、JSON 输出约束不会转发到 Messages）。

## 2. 鉴权

| 场景 | 形态 |
| --- | --- |
| 客户端访问网关 | `Authorization: Bearer <local key>` 或 `x-api-key: <local key>` |
| 健康检查 | 无鉴权 |
| 上游 Zen 凭证 | 网关配置里的 `zen_keys[]`，独立于本地 key，不作为客户端凭据 |
| 上游 Go 凭证 | `go_keys[]` |
| 匿名免费通道 | `anonymous: true` 时使用 OpenCode `public` 凭据访问匿名上游 |
| 管理端 | WebUI 账号密码（示例 `admin` + `webui.password`），会话与推理 API 分离 |

关键点：本地 key（`server_keys`）只用于客户端到网关；上游 key 由网关持有，不向客户端透出。
刷新机制：无 OAuth 刷新流程；`zen_keys` / `go_keys` 是静态密钥数组，失效后由使用方替换。

## 3. 请求 / 响应核心字段

### 3.1 Chat Completions

请求（OpenAI 兼容）：

```json
{
  "model": "MODEL_ID",
  "messages": [{"role": "user", "content": "Hello"}],
  "stream": false
}
```

响应为 OpenAI `chat.completion` 结构；`usage` 位于响应体 `usage` 字段。
额外约定：响应头带 `x-request-id`，用于关联日志。

### 3.2 Anthropic Messages

请求头必须带 `anthropic-version: 2023-06-01`，鉴权用 `x-api-key`；请求体含
`model`、`max_tokens`、`messages`。响应为 Anthropic `message` 结构，
`usage.input_tokens` / `usage.output_tokens`。

### 3.3 Models

`GET /v1/models` 返回 `{"object":"list","data":[...]}`，条目至少含 `id`；
可选字段：`context_window` / `context_length`、`max_input`、`max_output`、
`cost`（美元/百万 token）、`reasoning_efforts`、`default_reasoning_effort`。

### 3.4 流式事件

- OpenAI 路由：`Content-Type: text/event-stream`，每行 `data: {json}`，末帧 `data: [DONE]`。
- Anthropic 路由：`event: <type>` + `data: {json}`，事件序列
  `message_start` → `content_block_start` → `content_block_delta` → `content_block_stop`
  → `message_delta` → `message_stop`。
- 流式 usage：OpenAI 在末个 chunk 的 `usage`；Anthropic 在 `message_delta.usage`。
- `performance.first_event_timeout_seconds` 开启时，网关会等到首个完整 data 事件才向下游释放；
  超时前可换 key/出口重试，已释放的流不会重放。默认 `0`（关闭）。

## 4. 错误码与限流

| 状态 | 语义 | 是否换 key/出口重试 |
| --- | --- | --- |
| 400 / 413 / 422 | 请求级错误（如 `context_length_exceeded`、请求体超限） | 否，立即结束 |
| 401 | 上游鉴权失败 | 是（换 key） |
| 403 | IP 级滥用封禁只影响当前出口；普通 403 计入匿名熔断阈值 | IP 级换出口，普通 403 可重试 |
| 408 | 上游超时 | 是 |
| 429 | 上游限流 | 是 |
| 5xx | 上游服务错误 | 是 |

限制：

- API 请求体上限 **32 MiB**。
- 认证层级按 `prefer` 顺序尝试；每层单独应用 `retry.max_attempts`（示例配置为 3）。
- 请求总超时 `retry.timeout_seconds`（示例 300s）；连接超时默认 5s。
- 匿名通道有独立熔断：连续失败达到 `models.anonymous_backoff.threshold` 时整条匿名通道暂停，
  暂停窗口按 `base_hours` 倍增，上限 `max_hours`；单个普通 403/429 不直接打开熔断。
- 未认证的匿名请求可被上游拒绝或限流，网关不保证成功。

## 5. 我方实现需要对齐的最小集

1. `GET /v1/models`：转发 + 解析 `data[].id`；空列表视为「在线但无模型」，不报不可达。
2. `POST /v1/chat/completions`：JSON 与 SSE 直通，不改写响应体，不重算 usage。
3. `POST /v1/messages`：`x-api-key` + `anthropic-version: 2023-06-01`，SSE 直通。
4. 鉴权：客户端 key 由控制面网关管理；上游 key 走 provider 的 `api_key` / `options.extra_keys`，
   不进入请求体、不返回前端。
5. 重试：仅对 401/403/408/429/5xx 换 key 重试；400/413/422 立即返回；默认 3 次。
6. 层级：`options.tier = "zen" | "go"`，Go 通道在路径前加 `/go`；默认 `zen`。
7. 不做：Responses、System One、WARP、自动匿名出口轮换、模型元数据缓存（后续批次再评估）。
