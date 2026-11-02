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
queued ──claim──> running ──成功──> succeeded
  │                  │
  │                  ├─失败且 attempts < max──> queued（指数退避）
  │                  └─失败且 attempts = max──> failed
  └─cancel──> cancelled
failed / cancelled ──retry──> queued（attempts 归零）
```

### 5.3 控制面 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/grok/register/tasks` | 入队（管理员） |
| GET | `/api/grok/register/tasks` | 任务列表 |
| GET | `/api/grok/register/tasks/{id}` | 任务详情 |
| POST | `/api/grok/register/tasks/{id}/cancel` | 取消（管理员） |
| POST | `/api/grok/register/tasks/{id}/retry` | 重试（管理员） |
| GET | `/api/grok/register/worker` | 队列/并发状态 |

### 5.4 执行器边界

主仓不含浏览器自动化，也不内置 Camoufox/CloakBrowser。执行器通过
`GROK_REGISTER_EXECUTOR_URL` 指向外部注册运行时（HTTP POST，返回 JSON）；
未配置时任务以明确原因失败（`未配置注册执行器`），不会假装注册成功。
单测与链路验证通过注入假执行器覆盖状态流转。

## 6. 我方实现需要对齐的最小集

1. 任务入队、并发控制（全局上限 + 单任务 workers 上限）、生命周期字段。
2. 状态机：queued → running → succeeded/failed/cancelled，失败按 `max_attempts` 指数退避重入队。
3. 取消：queued 直接取消；running 取消 asyncio 任务并落 `cancelled`。
4. 重试：仅 `failed`/`cancelled` 可重试，`attempts` 归零后重新入队。
5. 管理员 API 与会话鉴权；日志/结果以 `result_json`/`error` 返回，不做 SSE。
6. 不做：真实浏览器注册、邮箱渠道实现、GrokIQ 探针、CPA/Grok2API 产物生成（属外部执行器职责）。
