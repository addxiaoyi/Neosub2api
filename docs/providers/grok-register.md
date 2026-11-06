# grok-register 协议契约（clean-room 提炼）

> 依据：上游公开 README、`WEB.md`，以及 `/api/health`、`/api/docs` 暴露的 HTTP 行为。
> 上游是注册控制台，**不是推理 provider**；没有 chat/models 协议。
> 主仓实现：`apps/control/app/services/grok_register.py` + `apps/control/app/routers/grok.py`（独立重写，MIT）。

## 1. 对外端点清单

上游为单进程 FastAPI Web 服务，默认 `127.0.0.1:8787`，OpenAPI 文档在 `/api/docs`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/stats` | 统计 + 任务状态 |
| GET | `/api/accounts` | 账号/注册结果列表 |
| POST | `/api/accounts/delete` | 删除记录，可选删除关联文件 |
| GET | `/api/accounts/{id}/failure-screenshot` | 失败现场截图 |
| GET / PUT | `/api/config` | 读写注册配置 |
| POST | `/api/job/start` | 启动注册任务 |
| POST | `/api/job/stop` | 停止注册任务 |
| GET | `/api/job/logs` | 轮询实时日志 |
| POST | `/api/connectivity` | 代理/邮箱等连通性检查 |
| GET | `/api/accounts/relogin/logs` | 重新登录日志 |
| POST | `/api/accounts/relogin/stop` | 停止重新登录 |
| POST | `/api/browser/kill-all` | 请求停止并终止全部托管浏览器 |

## 2. 鉴权

| 场景 | 形态 |
| --- | --- |
| Web 控制台 | 首次访问创建唯一管理员；登录后 Cookie 会话 |
| 会话属性 | HTTPS 默认 Secure Cookie；`GROK_WEB_COOKIE_SECURE=0` 可关闭 |
| 管理员凭据 | 哈希后存 `data/web_auth.json`，不写入 `config.json` |
| API 调用 | 复用同一个 Web 会话 Cookie；未登录访问业务 API 返回 401 |
| 刷新机制 | 无 token 刷新；会话过期后重新登录 |
| 外部联动 | 向 GrokIQ 发 Webhook 时带 `x-grokiq-token`；GrokIQ 回调同一头部 |

## 3. 任务请求 / 响应核心字段

`POST /api/job/start` 的配置字段（来自 `config.json` 与设置页）：

| 字段 | 语义 |
| --- | --- |
| `email_provider` | 邮箱渠道：Cloudflare / DuckMail / Mail.tm / YYDS / MailNest / OutlookEmail / CloudMail |
| `register_count` | 本次注册数量 |
| `register_workers` | 并发数，默认 1 |
| `proxy` | 注册与 OAuth 使用的 HTTP(S) 代理，支持 `http://user:password@host:port` |
| `browser_engine` | `camoufox`（默认）或 `cloakbrowser` |
| `browser_headless` | 本机可开；Docker 强制关闭 |
| `cpa_auto_add` | 注册后生成 CPA 授权 |
| `grok2api_auto_import` | 生成 JSON 后自动导入远程 Grok2API |
| `grok2api_auto_import_build/web/console` | 分通道导入开关 |

结果与状态：

- `GET /api/stats` 返回任务状态与统计；`GET /api/job/logs` 为轮询式日志（非 SSE）。
- 账号结果落在 SQLite（WAL），产物在 `data/accounts`、`data/cpa_auth`、`data/grok2api_auth`。
- 注册完成后可生成 CPA / Grok2API JSON，并可通过 Webhook 通知 GrokIQ。

未在公开文档中定义的字段（任务 id、进度百分比、单任务错误结构）：上游仅暴露「启动/停止 + 轮询日志」，
主仓按下面的自设计模型补齐，不猜测上游内部结构。

## 4. 错误码与限流

上游 README 未给出完整错误码表；可确认的行为：

| 状态 | 语义 |
| --- | --- |
| 401 | 未登录 / 会话失效 |
| 409（推断） | 已有任务运行中时再次启动——上游用「启动/停止」单任务模型，冲突由服务端拒绝 |
| 4xx | 配置校验失败（邮箱渠道缺字段、代理格式错误等） |
| 5xx | 浏览器启动失败、邮箱渠道不可达、上游页面结构变化 |

上游没有公开的 API 级限流参数；实际约束来自：

- 注册并发 `register_workers`；
- 出口代理与上游风控：同一出口 IP 触发风控后需更换出口再注册；
- 失败重试没有文档化上限（主仓实现里显式设定）。

## 5. 我方一等服务 + 任务表 + API

grok-register **不做成对外 provider**，只登记 `register` 能力位；实现为控制面内一等服务。

### 5.1 任务表 `grok_tasks`

| 字段 | 语义 |
| --- | --- |
| `id` | 任务 id |
| `status` | `queued` / `running` / `succeeded` / `failed` / `cancelled` |
| `count`、`workers` | 本批数量与并发（workers 由服务端夹逼到上限） |
| `email_provider`、`proxy` | 执行参数 |
| `payload_json` | 扩展参数（浏览器后端、导入开关等） |
| `result_json` | 执行器返回结果 |
| `attempts`、`max_attempts` | 已尝试次数与上限 |
| `error` | 最近一次失败原因 |
| `created_at`、`started_at`、`finished_at`、`next_run_at` | 生命周期与重试调度 |

### 5.2 状态机

```text
queued ──claim──> running ──执行器 done──> succeeded
  │                  │
  │                  ├─不可达/报错/超时/协议错误 且 attempts < max──> queued（指数退避）
  │                  └─同上 且 attempts = max──> failed（error 带原因）
  └─cancel──> cancelled
failed / cancelled ──retry──> queued（attempts 归零）
running ──控制面重启──> queued（有重试预算）或 failed（无预算），写 orphaned 事件
```

执行器侧阶段与本端状态是映射关系，不是同一套枚举：

| 执行器 `status` | 本端 `grok_tasks.status` | 说明 |
| --- | --- | --- |
| `pending` | `running` | 已提交，执行器尚未开始；本端不落 `pending` 状态，避免与队列的 queued 混淆 |
| `running` | `running` | 执行中（`progress.done/total` 可读） |
| `done` | `succeeded` | 结果（含 `accounts`）写入 `result_json`，账号落库 |
| `failed` | `failed` 或 `queued` | 有重试预算则退避重排，否则 failed |
| `cancelled` | `failed` 或 `queued` | 执行器报告被取消，按失败原因处理 |

失败原因分四类，`error` 列存完整中文原因（前端原样展示），日志侧另有 `kind`：

| kind | 触发 | `error` 示例 |
| --- | --- | --- |
| `unreachable` | 连接被拒、DNS 失败、连接超时 | `注册执行器不可达（http://127.0.0.1:9000）：All connection attempts failed` |
| `rejected` | HTTP 4xx/5xx、404 job | `注册执行器拒绝提交（HTTP 500）：stub: 执行器内部错误` |
| `timeout` | 总超时 `GROK_REGISTER_EXECUTOR_TIMEOUT_SEC` 用尽 | `注册执行器超时（900s 内未完成，job=abc123 最后阶段=running）` |
| `protocol` | 响应非 JSON、缺 `job_id`、未知阶段 | `注册执行器响应缺少 job_id（协议 v1 要求创建后返回 job_id）` |
| `reported_failure` | 执行器 `status=failed/cancelled` | `注册执行器报告失败：上游返回 403，注册被风控拦截` |

### 5.3 控制面 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/grok/register/tasks` | 入队（管理员） |
| GET | `/api/grok/register/tasks` | 任务列表 |
| GET | `/api/grok/register/tasks/{id}` | 任务详情 |
| POST | `/api/grok/register/tasks/{id}/cancel` | 取消（管理员） |
| POST | `/api/grok/register/tasks/{id}/retry` | 重试（管理员） |
| GET | `/api/grok/register/worker` | 队列/并发状态 |

### 5.4 执行器协议 v1（`grok-register-executor/v1`）

主仓不含浏览器自动化。执行器通过 `GROK_REGISTER_EXECUTOR_URL` 指向外部注册运行时，
必须是普通 HTTP 服务（可带路径前缀，不要尾斜杠）。四个动作的契约如下，全部 JSON。

**A. 提交** `POST {URL}/jobs`

```json
{
  "protocol": "grok-register-executor/v1",
  "task_id": 12,
  "count": 2,
  "workers": 1,
  "email_provider": "cloudflare",
  "proxy": "http://user:pass@host:port",
  "payload": {"browser_engine": "camoufox", "stub": {"mode": "success"}}
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `protocol` | string | 是 | 固定 `grok-register-executor/v1`；执行器可据此拒绝旧客户端 |
| `task_id` | int | 是 | 控制面任务 id，用于执行器侧幂等与日志关联 |
| `count` | int | 是 | 本批目标账号数 |
| `workers` | int | 是 | 执行器内并发（控制面已夹逼到 `GROK_REGISTER_MAX_WORKERS`） |
| `email_provider` | string | 是 | 可空串，表示执行器默认渠道 |
| `proxy` | string | 是 | 可空串 |
| `payload` | object | 是 | 透传扩展参数，执行器自行解释 |

成功响应 `200` 或 `201`：

```json
{"job_id": "abc123", "status": "pending"}
```

`job_id` 必填（字符串，建议 UUID/hex）；缺失按协议错误失败。

**B. 查询** `GET {URL}/jobs/{job_id}`

```json
{
  "job_id": "abc123",
  "status": "running",
  "progress": {"done": 1, "total": 2},
  "accounts": [
    {"uid": "grok-user-1", "nickname": "Alice", "cookie": "sso=...; sso-rw=...",
     "token": "eyJ...", "expires_at": 1767225600, "realm": "intl", "credits": 0}
  ],
  "error": ""
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | string | 仅四个值：`pending` / `running` / `done` / `failed`（`cancelled` 也接受） |
| `progress` | object | 可选；`done`/`total` 仅供观测，控制面不据此改状态 |
| `accounts` | array | `done` 时必填；每条至少要有 `uid`，无 `uid` 的条目会被丢弃 |
| `accounts[].cookie` / `token` | string | 至少一个非空；落库时 cookie 优先作为凭据 secret |
| `accounts[].expires_at` | int | Unix 秒，0 表示未知 |
| `error` | string | `failed` 时必填，前端原样展示 |

状态推进由执行器负责；控制面按 `GROK_REGISTER_POLL_INTERVAL_SEC`（默认 2s）轮询，
直到 `done`/`failed` 或总超时 `GROK_REGISTER_EXECUTOR_TIMEOUT_SEC`（默认 900s）。
`status=done` 但 `error` 非空按失败处理（防止把半失败当成功）。

**C. 取消** `POST {URL}/jobs/{job_id}/cancel`

- 成功：`200`，响应 `{"job_id": "...", "status": "cancelled"}`；执行器应立即停止浏览器并清理临时目录。
- 不存在：`404`；控制面只记告警，任务仍落 `cancelled`（本端状态为准）。

**D. 结果回传**

就放在 B 的响应里（`accounts` + `result`），不单独开下载端点：控制面拿到 `done` 后
把整个响应写入 `grok_tasks.result_json`，并额外加 `imported_accounts`（落库成功条数）。
账号写入 `accounts` 表（`provider='grok-register'`，`owner_user_id=NULL` 即 admin 可见），
cookie/token 镜像进 `provider_credentials`（`provider='grok-register'`）供后续复用。

### 5.5 无真执行器时如何验证

`apps/control/scripts/grok_executor_stub.py` 是纯标准库 HTTP 服务（无浏览器依赖），
覆盖 pending → running → done/failed 的完整流转：

```bash
python apps/control/scripts/grok_executor_stub.py --port 0
# stdout: PORT=<port>
GROK_REGISTER_EXECUTOR_URL=http://127.0.0.1:<port> \
GROK_REGISTER_POLL_INTERVAL_SEC=0.2 \
python -m uvicorn app.main:app --port 8010
```

任务 payload 可覆盖 stub 行为，便于演练各失败分支：

```json
{"stub": {"mode": "success", "polls": 2, "delay": 0.1}}
{"stub": {"mode": "fail"}}
{"stub": {"mode": "hang"}}
{"stub": {"mode": "badjson"}}
{"stub": {"mode": "http500"}}
```

`mode=hang` 配合调小 `GROK_REGISTER_EXECUTOR_TIMEOUT_SEC` 即可验证超时分支。
自动化验证见 `apps/control/tests/test_grok_register.py`（stub 绑定动态端口，不占固定端口）。

### 5.6 前端可见性

服务商配置页 grok-register 展开区的「注册任务」块（admin-only）读取
`/api/grok/register/tasks` 与 `/worker`：显示状态、目标/完成、失败原因（原样）、
重试/取消入口，以及执行器未配置的提前提示。执行器接通后前端无需改动。

## 6. 我方实现需要对齐的最小集

1. 任务入队、并发控制（全局上限 + 单任务 workers 上限）、生命周期字段。
2. 状态机：queued → running → succeeded/failed/cancelled，失败按 `max_attempts` 指数退避重入队。
3. 取消：queued 直接取消；running 取消 asyncio 任务、异步通知执行器 `POST /jobs/{id}/cancel` 并落 `cancelled`。
4. 重试：仅 `failed`/`cancelled` 可重试，`attempts` 归零后重新入队。
5. 管理员 API 与会话鉴权；日志/结果以 `result_json`/`error` 返回，不做 SSE。
6. 执行器 v1 四动作契约（提交/查询/取消/结果回传），失败四态可区分，控制面重启回收 running 行。
7. 注册产物落库：`accounts` 可见 + `provider_credentials` 留凭据，`result_json.imported_accounts` 可核对。
8. 不做：真实浏览器注册、邮箱渠道实现、GrokIQ 探针、CPA/Grok2API 产物生成（属外部执行器职责）。
