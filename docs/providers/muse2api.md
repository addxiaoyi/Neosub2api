# muse2api 协议契约（clean-room 提炼）

> 依据：`czg86389-hub/muse2api` 在 commit `1cd01682533dbf2c015fa879a8dc09534b50b4a3`
> （2026-11-02，v1.6.6）的仓库内容；与上一条基线 `fa3b992ff3ffde395c779b1084066c7bc1530d6a`
> （v1.6.5）逐文件对比。本文件只记录协议事实，不含上游源码片段。
> 主仓实现：`apps/control/app/adapters/muse.py`，**状态：仓内实现（直连）**——不再继承
> `OpenAICompatAdapter`，改为直连 Muse 官方 OpenAI 兼容入口（Bearer API key），
> SSE 逐 chunk 直通；`base_url` 仍可指回旧 sidecar 保持兼容（MIT，本仓自研）。

## 1. 对外协议与端点

上游是 Python（FastAPI + Uvicorn）单进程服务，把 muse.ai 网页账号额度反代成 API。
默认监听 `18610`（`Dockerfile` 的 `MUSE2API_PORT`、`docker-compose.yml` 端口映射）。
管理页面与推理 API 共用同一进程。

| 方法 | 路径 | 说明 | 我方是否接入 |
| --- | --- | --- | --- |
| GET | `/v1/models` | 固定返回 3 个模型，结构见 §3.4 | 是 |
| POST | `/v1/chat/completions` | OpenAI Chat Completions，支持 `stream` | 是 |
| POST | `/v1/responses` | OpenAI Responses 子集（Codex 默认） | 否 |
| POST | `/v1/images/generations` | 文生图 | 否 |
| POST | `/v1/images/edits` | 图生图 / 参考图编辑（multipart 可重复 `image`） | 否 |
| POST | `/v1/images/tasks`、`GET /v1/images/tasks/{task_id}` | 异步生图任务 | 否 |
| POST | `/v1/videos`、`/v1/videos/generations`、`GET /v1/videos/{task_id}` | 异步生视频任务 | 否 |
| GET | `/v1/media/{name}` | 生成物静态资源，**无需鉴权** | 否 |
| GET | `/healthz` | `{"status":"ok","time":<unix>}`，**无需鉴权** | 可选 |
| GET | `/readyz` | `{"status":"ready"\|"no_account","accounts":N,"browser_running":bool}`，**无需鉴权** | 可选 |

证据：路由注册在 `app.py:1133`（models）、`app.py:1469`（chat）、`app.py:1645`（responses）、
`app.py:1225-1264`（images）、`app.py:1360-1401`（videos）、`app.py:1731`（media）、
`app.py:1120/1125`（healthz/readyz）。

兼容边界：

- `model` 字段只用于下游兼容，**不影响上游路由**；muse.ai 网页是自动路由 agent，没有模型选择器（`app.py:1473-1475`）。
- `temperature`、`max_tokens`、`response_format`、`tool_choice` 能接收但不产生对应行为（`app.py:518-526`）。
- 原生没有 function calling；带 `tools` 时上游做文本协议注入与 JSON 回解析（`app.py:1477-1492`），默认关闭（`config.tool_protocol`）。
- 对话、生图、生视频共用同一个浏览器实例，靠全局锁串行（`app.py:1480`）。

## 2. 鉴权

| 场景 | 形态 | 证据 |
| --- | --- | --- |
| 客户端访问 `/v1/*` | `Authorization: Bearer <MUSE2API_KEY>` | `app.py:319-328` |
| Key 为空时 | 鉴权整体关闭（直接放行） | `app.py:320-321` |
| `/healthz`、`/readyz`、`/v1/media/*` | 不鉴权 | `app.py:1120/1125/1731` |
| 管理接口 | 同一 `MUSE2API_KEY` | `admin.html`「接入文档」页 |
| 上游 muse.ai 凭据 | 账号池里的 Cookie（`hatch_sess` 等），不向客户端透出 | §5 |

`MUSE2API_KEY` 形态是 `m2a_` 前缀的随机串（README 示例 `m2a_your_secret_key`），
但服务端不做前缀校验，只做全等比较（`app.py:326-327`）。

## 3. 请求 / 响应核心字段

### 3.1 Chat Completions 请求

`app.py:509-526` 的 `ChatRequest`：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `model` | string | `muse-spark` | 别名经 `MODEL_ALIASES` 归一，未知名字原样透传 |
| `messages` | array | `[]` | OpenAI 角色消息；空则回退读顶层 `prompt` |
| `stream` | bool | `false` | 是否 SSE |
| `temperature` / `max_tokens` | number | null | 接收但不生效 |
| `timeout` | int | null | 覆盖服务端 `MUSE2API_CHAT_TIMEOUT` |
| `prompt` | string | null | 兼容把提示词放顶层的客户端 |
| `tools` / `tool_choice` / `response_format` / `stream_options` | object | null | 只为「能读到」，`stream_options.include_usage` 例外，见 §3.3 |

`model` 归一规则在 `app.py:286-315`：`gpt-4o`、`gpt-5`、`claude-sonnet-4`、`deepseek-chat`
等一律映射到 `muse-spark`；`dall-e-3`、`gpt-image-1` 映射到 `muse-image`；`sora`、`veo`
映射到 `muse-video`。

### 3.2 非流式响应

标准 `chat.completion`（`app.py:1592-1600`）：

```json
{
  "id": "chatcmpl-<24 hex>",
  "object": "chat.completion",
  "created": 1730000000,
  "model": "muse-spark",
  "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "..."}}],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
}
```

`usage` 恒为 0（`app.py:1592`）——上游拿不到真实 token 计数。`finish_reason` 为
`stop`，带工具调用时为 `tool_calls`（`app.py:1598`）。

### 3.3 流式事件

- 帧格式：`data: <json>\n\n`（`app.py:1426-1427`），**没有 `event:` 行**。
- 响应头：`Cache-Control: no-cache, no-transform`、`X-Accel-Buffering: no`、`Connection: keep-alive`（`app.py:1462-1466`）。
- 首包立即发 `choices[0].delta = {"role":"assistant"}`（`app.py:1519`）。
- 文本按 2 字符切片平滑输出（`app.py:1446-1459`）。
- 结束帧 `finish_reason` 为 `stop` 或 `tool_calls`（`app.py:1526/1555/1560`）。
- 仅当请求带 `stream_options.include_usage=true` 时，在 `[DONE]` 前追加一个
  `choices: []` + `usage` 的全零分片（`app.py:1437-1443`、`1561-1565`）。
- 终止标记：`data: [DONE]\n\n`（`app.py:1566`）。
- **流开始后的错误不走 HTTP 状态码**，而是发一个 `data: {"error":{...}}` 帧
  （`app.py:1568-1579`），客户端需要按帧内 `code` 判断。

`/v1/responses` 的 SSE 用 `event: <name>\ndata: <json>\n\n`（`app.py:1641-1642`），
与本仓无关，记录备查。

### 3.4 Models

`GET /v1/models` 返回 `{"object":"list","data":[...]}`（`app.py:1133-1135`），
`data` 固定 3 项（`app.py:271-278`）：

| id | object | owned_by |
| --- | --- | --- |
| `muse-spark` | `model` | `muse` |
| `muse-image` | `model` | `muse` |
| `muse-video` | `model` | `muse` |

每项额外带 `description` 字段（OpenAI 规范外扩展，客户端可忽略）。

## 4. 错误码与限流

`/v1/*` 统一转成 OpenAI 错误包（`app.py:246-257`）：

```json
{"error": {"message": "...", "type": "invalid_request_error", "param": null, "code": 400}}
```

`type` 映射：404 → `not_found_error`；429 → `rate_limit_error`；≥500 → `server_error`；
其余 → `invalid_request_error`（`app.py:236-243`）。参数校验失败回 422 同结构
（`app.py:260-269`）。

| 状态 | 触发条件 | 证据 |
| --- | --- | --- |
| 400 | `messages` 为空；没有可用账号 | `app.py:1484`、`1498` |
| 401 | 缺少 / 错误 `Bearer` Key；Muse 会话失效 | `app.py:323/327`、`1588` |
| 404 | 任务或账号不存在 | `app.py:216`、`1236` |
| 409 | 账号正在执行任务；`Idempotency-Key` 复用但请求不同 | `app.py:218`、`1182` |
| 422 | 请求体字段校验失败 | `app.py:266-268` |
| 429 | 生图队列满（`Image queue is full; retry later`） | `app.py:1191` |
| 502 | 上游生成失败 / 超时 | `app.py:1590`、`1567` |

限流只有生图队列一处（`app.py:1191`），chat 没有显式 429。账号失效会在流式帧里返回
`type: auth_error, code: 401`（`app.py:1570`）。

## 5. 账号导入规则（v1.6.6 变更）

这条不影响本仓适配器（我们只调用 `/v1/*`），但部署时要知道。

| 项 | v1.6.5（基线） | v1.6.6（当前） | 证据 |
| --- | --- | --- | --- |
| 必填 Cookie | `hatch_sess` + `hatch_vml` | **只要求 `hatch_sess`** | `engine.py:45`、`store.py:17` |
| `hatch_vml` 语义 | 短效校验令牌，决定账号寿命（约 2 天） | 可选工作区令牌，缺失不阻止导入 | `engine.py:44-45`、`admin.html:336` |
| 导入 `essential_missing` | 可能返回 `["hatch_sess","hatch_vml"]` | 缺会话时只返回 `["hatch_sess"]` | `tests/test_session_health.py:137`（新版） |
| 浏览器扩展 / 油猴 | 1.0.1 / 1.1.1 | **必须手动升级到 1.0.2 / 1.1.2** | `extension/manifest.json:4`、`tools/muse2api_cookie_importer.user.js:4` |

注意：**服务端升级不会自动升级已加载的未打包扩展**，需要在
`chrome://extensions` / `edge://extensions` 点「重新加载」（`README.md:129-133`、
`admin.html:290`）。旧扩展仍会因缺 `hatch_vml` 拒绝导入。

账号是否可用仍以上游认证为准：导入成功只表示记录已保存（`tests/test_session_only_import.py:12-22`）。

## 6. 我方实现需要对齐的最小集

1. `base_url` 默认指向 Muse 官方 OpenAI 兼容入口 `https://api.meta.ai`（该默认域为公开
   资料推断，**未用真实凭证验证**；账号体系不同时直接改 `base_url`），`api_key` 以
   `Authorization: Bearer` 注入；旧部署可指回 sidecar `http://127.0.0.1:7865`；
   `options.cookie` 可带 `hatch_sess`，供回退到网页网关时使用。
2. `GET /v1/models` 必须返回 `{"object":"list","data":[3 项]}`；非空即视为在线。
3. `POST /v1/chat/completions` 非流式返回 `chat.completion`，`usage` 全零属正常，不能当异常。
   本仓对该通道按全零如实记账：`billing_ledger.note=usage_zero:muse`，金额 0，不做 token 估算。
4. 流式必须逐帧转发 `data:`，保留 `[DONE]`；不要在 `usage` 全零时中断。
5. 鉴权失败按 401 处理；`/v1/*` 错误包是 `{"error":{message,type,param,code}}`，可直接透传。
6. 模型名不需要改写，上游自己会把常见别名归一；本仓只透传 `model`。
7. 我方只声明 `openai.chat.completions` 能力位（`chat`、`models`），不使用 `responses` / 生图 / 生视频。

本仓不需要、也不应该实现的：

- 账号导入、Cookie 校验、代理池、签到、令牌续期——都在 sidecar 内部。
- 浏览器扩展 / 油猴脚本的分发。

## 7. 版本核对记录

| 基线 | 新版本 | 差异范围 | 判定 |
| --- | --- | --- | --- |
| `fa3b992`（v1.6.5） | `1cd0168`（v1.6.6） | 16 个文件：`app.py` 仅版本号（`app.py:68`）；`engine.py:45`、`store.py:17` 的 Cookie 必填项；`admin.html` 文案与升级提示；扩展 1.0.2 / 油猴 1.1.2；`version.json`；相关测试 | **协议未变，适配器零改动** |

核对方式：`git diff --stat fa3b992 1cd0168` 显示 16 个文件、91 行新增、55 行删除；
路由注册行号（`app.py:1120-1731`）与基线逐行一致，`/v1/chat/completions`、`/v1/models`
的函数体无改动，SSE 辅助函数（`app.py:1426-1466`）无改动，`Dockerfile` /
`docker-compose.yml` / `requirements.txt` / `LICENSE` 均不在 diff 中。

## 8. 部署版本要求（仅回退到 sidecar 时适用）

> 仓内实现直连官方入口，正常部署不需要 sidecar；把 `base_url` 指回旧服务时才受本节约束。

**sidecar 必须 ≥ v1.6.6，浏览器扩展必须 1.0.2，油猴脚本必须 1.1.2。**

| 组件 | 最低版本 | 检查方式 | 不满足的后果 |
| --- | --- | --- | --- |
| muse2api sidecar | **v1.6.6** | `/openapi.json` 的 `info.version`；或容器内 `cat /app/version.json`（`Dockerfile:30`） | v1.6.5 及更早把 `hatch_vml` 当必填，已登录账号被拒导入 |
| 浏览器扩展 | **1.0.2** | `chrome://extensions` / `edge://extensions` 显示的版本号 | 旧扩展缺 `hatch_vml` 时返回 `essential_missing`，导入失败 |
| 油猴脚本 | **1.1.2** | Tampermonkey 面板版本号 | 同上 |

依据：`version.json`（`"version": "1.6.6"`）、`engine.py:45`、`store.py:17`、
`extension/manifest.json:4`、`tools/muse2api_cookie_importer.user.js:4`。

升级检查项（每次部署后逐条确认，任一不通过就不要联调）：

1. sidecar 版本 ≥ 1.6.6；容器内 `version.json` 与镜像标签一致。
2. 扩展显示 `1.0.2`，并在扩展页点「重新加载」——**服务端升级不会自动更新未打包扩展**。
3. 油猴脚本显示 `1.1.2`；多个浏览器个人资料要各自升级。
4. 重新导入后点「测试」：导入成功只表示记录已保存，可用性以上游会话验证为准。

版本差异与导入规则详见 §5；扩展升级提示文案见 `README.md:129-133`、`admin.html:290`。
