# muse2api 协议面扩展方案（responses / images / videos）

> 状态：**方案，未实施**。本文只描述改动点与风险，不含任何代码改动。
> 依据：`czg86389-hub/muse2api` commit `1cd01682533dbf2c015fa879a8dc09534b50b4a3`（v1.6.6）。
> 现状：本仓只接 `/v1/chat/completions` 与 `/v1/models`
> （`apps/control/app/adapters/muse.py:22-23`），其余协议上游已提供但未接入。

## 1. 现状与可复用的骨架

| 层 | 位置 | 现状 |
| --- | --- | --- |
| 适配器 | `apps/control/app/adapters/muse.py:7-24` | `capabilities` 只有 `chat`/`models`；`protocols=("openai.chat.completions",)` |
| 通用转发 | `apps/control/app/adapters/openai_compat.py:62-63` | 只有 `chat_completion()`；`list_models()` 见同文件 `38-46` |
| 客户端 | `apps/control/app/adapters/client.py:106-149` | `forward()` 只支持 JSON 或原始字节，`_headers()` 固定 `content-type: application/json`（`client.py:52-59`） |
| 能力位 | `apps/control/app/adapters/base.py:6-16` | 无 `images` / `videos`；`responses` 已存在 |
| 网关路由 | `apps/control/app/routers/v1.py:212-238` | 已有 `/v1/chat/completions`、`/v1/messages`、`/v1/responses`、`/v1/models` |
| 鉴权 / 记账 | `routers/v1.py:39-62`（`_gateway_key`）、`65-101`（`_record`） | 可复用 |
| usage 折算 | `apps/control/app/billing.py:111-117` | 已兼容 `input_tokens` / `output_tokens` |

`/v1/responses` 的路由与能力位已经在网关里，只是 `MuseAdapter` 没覆盖 `responses()`
（继承 `base.py:119-120` 的 501）。images / videos 则完全没有路由。

## 2. 三个协议的上游形状

### 2.1 OpenAI Responses（`POST /v1/responses`）

| 项 | 值 | 证据 |
| --- | --- | --- |
| 请求字段 | `model`、`input`（string 或数组）、`instructions`、`stream`、`max_output_tokens`、`timeout`、`tools`、`store` | `app.py:529-539` |
| 子集边界 | 只做「文本进 → 文本出」；`input` 里的 `message` / `input_text` / `function_call` / `function_call_output` 会被压成普通 messages | `app.py:1610-1638` |
| 非流式响应 | `{id:"resp_…", object:"response", created_at, status, model, output:[{type:"message", id, role:"assistant", status, content:[{type:"output_text", text, annotations}]}], usage}` | `app.py:1667-1676`、`1728` |
| usage | `input_tokens` / `output_tokens` / `total_tokens`，**恒为 0** | `app.py:1675-1676` |
| 流式帧 | `event: <name>\ndata: <json>\n\n`，**与 chat 的纯 `data:` 不同** | `app.py:1641-1642` |
| 事件序列 | `response.created` → `response.output_item.added` → `response.content_part.added` → `response.output_text.delta`(N) → `response.output_text.done` → `response.output_item.done` → `response.completed` | `app.py:1681-1710` |
| 无 `[DONE]` | 结束靠 `response.completed` | `app.py:1708-1710` |
| 流内错误 | `response.failed` 帧，HTTP 状态仍是 200 | `app.py:1711-1715` |
| 响应头 | 复用 `_SSE_HEADERS`（`Cache-Control: no-cache, no-transform` 等） | `app.py:1462-1466`、`1716` |
| 非流式错误 | 400（`input` 为空）/ 401（会话失效）/ 502（生成失败） | `app.py:1654`、`1723-1726` |

与 chat 的差异：帧多了 `event:` 行；usage 键名换成 `input_tokens`/`output_tokens`；
结束标记换成 `response.completed`；错误改走帧内 `response.failed`。

### 2.2 Images（`/v1/images/*`）

| 端点 | 行为 | 证据 |
| --- | --- | --- |
| `POST /v1/images/generations` | 同步生成，返回 `{created, data:[…]}` | `app.py:1245-1261`、`1139-1147` |
| `POST /v1/images/generations` + `"async": true` | 立即返回 `202 {id, task_id, object:"image.task", status:"queued", progress, created_at}` | `app.py:1252-1253`、`1220-1222` |
| `POST /v1/images/tasks` | 显式排队，支持 `Idempotency-Key` 头 | `app.py:1225-1229`、`1167-1187` |
| `GET /v1/images/tasks/{task_id}` | 轮询；完成后把 `_image_response` 合并进任务体 | `app.py:1232-1242` |
| `POST /v1/images/edits` | 图生图；**multipart/form-data 或 JSON 都收**，`image` 可重复 | `app.py:1264-1356` |
| `GET /v1/media/{name}` | 生成物静态文件，**不鉴权** | `app.py:1731-1738` |

请求字段（`app.py:471-483`）：`prompt`、`model`（默认 `muse-image`）、`n`、`size`、
`aspect_ratio`、`response_format`（`url` | `b64_json`）、`timeout`（1–600）、`extra`、
`image` / `images` / `reference_image`、`async`。

响应条目（`app.py:1139-1147`）：`revised_prompt`、`url`、`size`、`kind`、`bytes`；
`response_format=b64_json` 时换成 `b64_json` 并去掉 `url`。

关键差异：`n` 声明了但响应固定 1 条；参考图有上限（逐张 10 MiB、总计 24 MiB，
`app.py:1293-1306`）；队列满返回 `429 Image queue is full; retry later`（`app.py:1191`）；
`Idempotency-Key` 复用但请求体不同返回 409（`app.py:1181-1182`）。

### 2.3 Videos（`/v1/videos/*`）

| 端点 | 行为 | 证据 |
| --- | --- | --- |
| `POST /v1/videos`、`/v1/videos/generations` | 建任务、立刻返回（**HTTP 200，不是 202**）：`{id, task_id, object:"video.task", status:"queued", progress:10, created_at}` | `app.py:1360-1397` |
| `GET /v1/videos/{task_id}`、`/v1/videos/generations/{task_id}` | 轮询；`succeeded/success/done` 归一成 `completed`，补 `url` 与 `video.url` | `app.py:1400-1422` |

请求字段（`app.py:486-498`）：`prompt`、`model`（默认 `muse-video`）、`duration`、`size`、
`aspect_ratio`、`resolution`、`timeout`、`extra`、`image` / `images` / `image_url` / `reference_image`。

关键差异：视频只有异步路径，没有取消端点；`progress` 只报里程碑（15 / 100），
Muse 不给百分比（`app.py:1412-1414`）；默认超时 600s（`config.py:103`），
生成物通过 `/v1/media/{name}` 取。

## 3. 我方需要改的适配器方法与能力位（只列改动，不落代码）

### 3.1 适配器

| 文件 | 改动 | 说明 |
| --- | --- | --- |
| `apps/control/app/adapters/muse.py:22` | `capabilities` 增加 `responses=True`（images/videos 需要新能力位，见 3.3） | 网关 `_resolve` 用它做准入（`routers/v1.py:131`） |
| `apps/control/app/adapters/muse.py:23` | `protocols` 追加 `"openai.responses"` | 仅元数据，供前端展示 |
| `apps/control/app/adapters/muse.py` | 新增 `responses(self, payload, *, stream)` | 覆盖 `base.py:119-120` 的 501；实现为 `self._client().forward("/v1/responses", payload, stream=stream, accept="text/event-stream")` |
| `apps/control/app/adapters/muse.py` | 新增 `image_generation() / image_edit() / image_task() / video_create() / video_task() / media()` | 协议形状与 chat 不同，不能塞进 `chat_completion` |
| `apps/control/app/adapters/client.py:52-59` | `_headers()` 需要允许调用方指定 `content-type` | `images/edits` 的 multipart 走不通当前固定 JSON 头 |
| `apps/control/app/adapters/client.py:106-149` | `forward()` 需要支持 multipart 或直接透传原始请求体 + 原 content-type | 同上 |

### 3.2 网关路由（`apps/control/app/routers/v1.py`）

| 方法 | 路径 | 新函数 | 鉴权 |
| --- | --- | --- | --- |
| POST | `/v1/responses` | **已存在**（`v1.py:222-224`），只需适配器就位 | 现有 `_gateway_key` |
| POST | `/v1/images/generations` | 新增 | 同上 |
| POST | `/v1/images/edits` | 新增（需原样透传 multipart） | 同上 |
| POST | `/v1/images/tasks`、GET `/v1/images/tasks/{task_id}` | 新增（可选） | 同上 |
| POST | `/v1/videos`、GET `/v1/videos/{task_id}` | 新增 | 同上 |
| GET | `/v1/media/{name}` | 新增（否则 `url` 字段指向上游，客户端直接跨域拉 sidecar） | 建议复用 key 校验 |

记账：新增路由都要复用 `_record`（`v1.py:65-101`），否则统计/计费出现断档。
`_gateway_key` 不需要改——鉴权头仍是 `Authorization: Bearer`。

### 3.3 能力位（跨会话改动，需与并行开发协调）

`apps/control/app/adapters/base.py:6-16` 的 `CAPABILITY_KEYS` 目前没有 `images` / `videos`。
按 `docs/UPSTREAM-SYNC.md` 第 96 行的约定，**新增能力位属于跨会话改动**，
需要与正在改 `apps/control/app/` 的会话协调后再动；`__init__.py` 的 `BUILDERS` 不受影响
（provider 不变）。

（2026-11-05 已更正）：`CAPABILITY_KEYS` 已并入 `embeddings` / `images` / `videos` / `media` / `rerank` / `usage` / `count_tokens` 七个扩展位，`muse` 的 `images` / `videos` / `media` 已改走 `stub_capabilities(...)` 正常参数；扩展位新增在同会话内即可完成，不再按跨会话改动处理。

## 4. 最小可交付顺序

1. **responses 先行**。路由与能力位都已存在（`v1.py:222-224`、`base.py:6-16`），
   适配器只加一个转发方法；后端是同一套文本生成，行为差异集中在校验
   `event:` 帧与 `response.completed` 结束标记。改动面最小、可独立回滚。
2. **images 次之**。需要新增能力位 + multipart 透传 + 媒体文件代理，
   但请求是同步的（`async` 可选），失败语义清晰，适合在 responses 稳定后做。
3. **videos 最后**。只有异步任务 + 长轮询（默认 600s，`config.py:103`），
   还要决定任务状态是否落库；且上游没有取消端点，网关无法"帮助"用户中断。
   放在最后可以避免一开始就引入任务表。

## 5. 风险与口径差异

| 风险 | 具体差异 | 影响 |
| --- | --- | --- |
| 错误码口径 | chat 没有显式 429；images 队列满 429（`app.py:1191`）；`Idempotency-Key` 冲突 409（`1182`）；任务不存在 404（`1236`）；responses 流内错误是 `response.failed` 帧而非 HTTP 状态（`1711-1715`） | 网关不能按 chat 的"429=限流"统一处理；前端要按端点区分 |
| usage 统计 | chat 三键全零（`app.py:1592`）；responses 是 `input_tokens`/`output_tokens` 全零（`1675-1676`）；images/videos **完全没有 usage** | 按 token 计费会恒为 0；流式 responses 的 usage 在 `data.response.usage`，当前 `billing.py:74-88` 只读顶层与 `message.usage`，**会漏取** |
| 媒体地址 | `url` 由 `MUSE2API_PUBLIC_BASE` 决定绝对/相对（`app.py:907-915`）；未配置时是相对路径 | 不代理 `/v1/media/*` 时客户端会 404 或直连 sidecar |
| 超时 | 非流式默认 120s（`openai_compat.py:29`），生图默认 `MUSE2API_IMAGE_TIMEOUT=240s`、视频 600s | 生图必须按端点覆盖 timeout，否则网关先断 |
| 请求体 | `images/edits` 支持 multipart，当前 `client.py:52-59` 固定 JSON | 需要客户端层扩展，否则只能支持 JSON 变体 |
| 状态码 | 视频建任务返回 200（`app.py:1396`），不是 202 | 客户端若依赖 202 语义会误判 |

## 6. 回归验证清单（实施后）

```powershell
# 控制面单测
.\.runtime\python\python.exe -m pytest apps\control\tests -q

# 端到端冒烟
powershell -ExecutionPolicy Bypass -File scripts\smoke.ps1

# 上游清单自检
powershell -NoProfile -File scripts\check-upstreams.ps1
```

手动补测：

- responses 非流式 + 流式各一次：确认 `event:` 帧逐条透传、`response.completed` 结束后连接关闭。
- responses 流式 usage：确认新的 `data.response.usage` 能被 `billing` 取到（否则记为 0 并说明）。
- images 同步 + `async` 各一次：确认 `202` 与任务体结构、`url` 可下载。
- videos 建任务 + 轮询到 `completed`：确认 600s 内不超时、`url` 可下载。
- 错误码：401 / 404 / 409 / 429 / 502 各构造一次，确认网关返回结构与 chat 一致。
