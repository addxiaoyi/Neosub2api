# API 参考（API Reference）

依据 `apps/control/app/routers/` 下的实际路由整理（代码事实，非文档推导）。

- `/api/*`：管理面，登录态鉴权（HttpOnly Cookie `u2a_session`），部分仅管理员。
- `/v1/*`：模型网关，`Authorization: Bearer u2a_...` 密钥鉴权。
- `GET /healthz`：无鉴权。

通用约定：

- 错误响应统一为 `{"detail": "..."}`（FastAPI 默认结构 + ProviderError 处理器）。
- 时间戳均为 Unix 秒（整数）。
- 角色两种：`admin` / `viewer`。标注「管理员」的接口 viewer 调用返回 403。
- 请求体均为 JSON；`Content-Type: application/json`。

## 0. 健康检查

### `GET /healthz`

无鉴权。

```json
{"ok": true, "providers": 10, "db": "/data/unified.db"}
```

`providers` 为注册表内 provider 数量（不是已启用数）。

---

## 1. 网关（`/v1/*`，密钥鉴权）

鉴权规则（`routers/v1.py`）：

- 请求头 `Authorization: Bearer <u2a_密钥>`（`Bearer ` 前缀可省，裸 token 也接受）。
- 密钥按 SHA-256 比对 `api_keys` 表，仅 `enabled = 1` 的通过；命中即更新 `last_used_at`。
- 无鉴权头时：DB 里「网关鉴权」开关（`/api/settings/gateway`）优先；未配置过则回落到环境变量 `UNIFIED_ALLOW_ANONYMOUS_GATEWAY`（默认 false）。允许匿名时以 `anonymous` 身份记录，密钥归因为空。

### `POST /v1/chat/completions`

OpenAI Chat Completions 协议转发。

| 项 | 说明 |
| --- | --- |
| 鉴权 | 网关密钥 |
| 请求体 | 上游要求的 OpenAI 请求体原样转发；`model` 必填，缺失返回 400 |
| 请求头 | 可选 `x-provider: <provider_id>` 显式指定服务商 |
| 流式 | 请求体 `"stream": true` 时 SSE 直通（`text/event-stream`） |
| 日志 | 旁路写 `request_logs` + `usage_records`（流式从含 usage 的 SSE chunk 提取） |

模型解析顺序（`registry.resolve`）：

1. `x-provider` 头 → 用该 provider，模型名不变；
2. `model` 形如 `<provider_id>/<model>` 且前缀是已知 provider → 用该 provider，取后半段；
3. 「设置 → 模型别名」表命中 → 目标值再按规则 2 解析；
4. 兜底：第一个已启用且 `capabilities.chat` 为真的 provider；
5. 全都没有 → 503 `没有已启用的 chat provider`。

错误码：

| 状态码 | 含义 |
| --- | --- |
| 400 | 请求体不是合法 JSON / 不是 JSON 对象 / 缺 `model` |
| 401 | 缺少 `Authorization` 或密钥无效 |
| 501 | 命中的 provider 不支持 chat（能力位关闭） |
| 502 | 上游不可达（连接失败等） |
| 503 | 无已启用的 chat provider；sub2api 分组未配置 |
| 429 | sub2api 分组额度耗尽 / 账号池空 |
| 上游原样 | 上游返回的 4xx/5xx 状态码与响应体原样透传 |

响应体是上游响应的逐字节转发（非流式含 `usage` 字段；流式为 SSE）。

### `POST /v1/messages`

Anthropic Messages 协议转发，鉴权、模型解析、日志、错误码与上完全一致，仅能力位换成 `messages`。provider 不支持时 501（如 trae_sidecar、muse）。

### `GET /v1/models`

| 项 | 说明 |
| --- | --- |
| 鉴权 | 网关密钥（或匿名开关打开） |
| 逻辑 | 遍历已启用且 `capabilities.models` 为真的 provider，合并各自 `list_models()` 结果 |

```json
{"object": "list", "data": [{"id": "gpt-4o-mini", "object": "model", "owned_by": "opencode"}]}
```

某 provider 拉模型失败时静默跳过（不报错），所以空列表可能意味着「没有可用 provider」也可能是「上游都不通」。

---

## 2. 认证与会话（`/api`）

### `POST /api/login`

| 请求体 | 类型 | 说明 |
| --- | --- | --- |
| `username` | string | 用户名 |
| `password` | string | 密码 |

成功：`{"ok": true, "username": "...", "role": "admin|viewer"}`，并种下 HttpOnly Cookie（`u2a_session`，SameSite=Lax，`secure` 取决于 `UNIFIED_COOKIE_SECURE`，有效期 `UNIFIED_SESSION_TTL_SEC`）。失败：401 `用户名或密码错误`（登录失败会记审计日志）。

### `POST /api/logout`

删除当前会话 + 清 Cookie。返回 `{"ok": true}`。无登录态调用也返回成功。

### `GET /api/me`（登录）

返回 `{"username": "...", "role": "..."}`。未登录 401。

### `POST /api/sessions/revoke`（登录）

吊销当前用户的全部会话。返回 `{"ok": true, "relogin_required": true}`。

---

## 3. 服务商（`/api/providers`）

### `GET /api/providers`（登录）

返回 10 家 provider 的公共信息列表，每项字段：

| 字段 | 说明 |
| --- | --- |
| `id` / `name` / `label` | 标识与显示名 |
| `vendor` / `license` / `runtime` | 上游来源 / 许可证 / 运行形态 |
| `status` | `implemented`（主仓内置实现）或 `sidecar`（透明代理到独立进程） |
| `description` | 一句话描述 |
| `base_url` / `default_base_url` | 当前生效地址 / 默认地址 |
| `enabled` | 是否启用 |
| `capabilities` | `{chat, messages, models, oauth, checkin, credits, register, sidecar}` 八个布尔位 |
| `protocols` | 支持的协议（`openai.chat.completions` / `anthropic.messages`） |
| `risk_level` / `risk_note` | `safe` / `warn` / `danger` 及说明 |
| `has_api_key` | 是否已配置上游密钥（不回显明文） |
| `options` | 已保存的高级配置 dict |

### `PATCH /api/providers/{provider_id}`（管理员）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `enabled` | bool | 启用/停用 |
| `base_url` | string | 必须 `http://` 或 `https://` 开头，否则 400；尾部 `/` 自动去除 |
| `label` | string | 显示名，截断 64 字符 |
| `api_key` | string | 约定：`***` = 保持不变；空串 = 清除；其他 = 覆盖 |
| `options` | object | 与已有 options 深合并（部分更新） |

全部字段可省，但至少传一个，否则 400 `没有需要更新的字段`。未知 provider 返回 404。保存立即生效（不等重启）。

各 provider 支持的 `options` 键见 [PROVIDERS.md](PROVIDERS.md)。

---

## 4. 账号（`/api/accounts`）

账号来自各 provider 的上游账号池，列表接口顺带同步（每个 provider 30 秒节流，强制刷新用 `/sync`）。

### `GET /api/accounts?provider=&realm=`（登录）

| 查询参数 | 说明 |
| --- | --- |
| `provider` | 按 provider id 过滤；指定后只同步该 provider |
| `realm` | 按 realm 过滤（目前仅 CodeBuddy 账号有 cn/global 之分） |

```json
{
  "total": 3,
  "accounts": [{
    "file": "uid123.json",
    "uid": "uid123",
    "nickname": "...",
    "enterprise_id": "",
    "provider": "codebuddy",
    "realm": "cn",
    "expires_at": 0,
    "is_expired": false,
    "remain_seconds": 0,
    "credits": 12.5,
    "note": "",
    "disabled": false,
    "source": "file",
    "healthy": true,
    "poolUnknown": false,
    "checkin_today": true,
    "cooling": false,
    "cooldown_remaining": 0,
    "status": "ok"
  }],
  "pool_synced": 3,
  "pool_available": true,
  "sync_note": "muse: 上游不可达"
}
```

`checkin_today` / `cooling` / `cooldown_remaining` / `status` 仅上游返回时出现（meta 平铺）。同步失败不报错，记进 `sync_note`。

### `POST /api/accounts/sync?provider=`（管理员）

绕过节流强制同步。返回 `{provider_id: {synced, pool_available, note}}`；不传 `provider` 则同步全部已启用 provider。

### `POST /api/accounts/refresh-credits?force=false`（管理员）

仅作用于 codebuddy（内部固定取 codebuddy 适配器）。返回其管理端的响应（`{total, succeeded, credits, meta, failed, ...}`）；CodeBuddy 不可达时返回带 `message` 的零值结构而不抛 500。

### `POST /api/accounts/checkin-all`（管理员）

仅 codebuddy：全部账号签到。返回 `{total, succeeded, already, skipped, results, ...}` 或带 `message` 的零值结构。

### `PUT /api/accounts/{file}/note`（管理员）

请求体 `{"note": "..."}`（截断 200 字符）。返回 `{"ok": true, "uid": "...", "note": "..."}`。上游不支持备注时只存控制面本地。

### `POST /api/accounts/{file}/disabled`（管理员）

请求体 `{"disabled": true|false}`。同时下发上游与更新本地。返回 `{ok, file, disabled, reload_triggered, message}`；上游失败时 `ok: false` 且不改本地。

### `DELETE /api/accounts/{file}`（管理员）

上游删除成功才删本地记录，上游删不掉直接抛错。返回 `{"success": true}`。账号不存在 404。

### 账号动作（管理员）：`POST /api/accounts/{file}/{action}`

`action` ∈ `checkin`（签到）/ `test`（测活）/ `refresh`（刷新）/ `clear-cooling`（清冷却）。动作完成后自动强制重同步该 provider。不支持该动作的 provider 返回 `{ok: false, message: ...}`。

### `POST /api/accounts/restart`（管理员）

仅 codebuddy 可重启（其运行时由本控制面联动管理）；否则返回 `{ok: false, message: ...}`。

---

## 5. 状态与模型（`/api`）

### `GET /api/status`（登录）

对所有已启用 provider 调 `health()` 并合并：

```json
{
  "connected": true,
  "total": 3, "healthy": 3, "cooling": 0, "disabled": 0, "in_flight_full": 0,
  "realm_totals": {}, "provider": "codebuddy"
}
```

- 任一 provider 健康即 `connected: true`，健康字段取第一个返回这些字段的 provider。
- 全部不可达时 `connected: false` 且 `error` 为各家的错误拼接。
- CodeBuddy 管理端在线但未配 token 时返回 `connected: true, auth: "missing"` 并附说明。

### `GET /api/models`（登录）

```json
{"models": [{"id": "...", "owned_by": "opencode", "...": "上游原始字段"}],
 "source": "dynamic", "count": 12}
```

`source`：有结果为 `dynamic`，否则 `unknown`。与 `/v1/models` 的区别：登录态而非密钥鉴权，且去重（`/v1/models` 不去重）、附带上游原始元数据字段。

---

## 6. 网关密钥（`/api/keys`）

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /api/keys` | 登录 | 列表，不含明文 |
| `POST /api/keys` | 管理员 | 创建，明文只返回一次 |
| `PATCH /api/keys/{key_id}` | 管理员 | 改名 / 启停 |
| `DELETE /api/keys/{key_id}` | 管理员 | 吊销（物理删除，立即失效） |

### `POST /api/keys`

请求体 `{"name": "..."}`（1–64 字符，超限 400）。响应见 [GETTING-STARTED.md](GETTING-STARTED.md) 第 6 节——`key` 字段仅此一次。

### `PATCH /api/keys/{key_id}`

请求体 `{"name": "...", "enabled": true}`，至少一项。返回更新后的公共行（`id, name, prefix, enabled, created_at, last_used_at`）。

### `DELETE /api/keys/{key_id}`

返回 `{"ok": true}`；不存在 404。

密钥无配额、无模型白名单（属 M3 计费范畴，当前版本有意不收录）。

---

## 7. 统计（`/api/stats`）

### `GET /api/stats/summary?realm=`（登录）

```json
{
  "today_requests": 0, "today_tokens": 0,
  "week_requests": 0, "week_tokens": 0,
  "total_requests": 0, "total_tokens": 0,
  "today_credit": 0, "week_credit": 0, "total_credit": 0,
  "active_keys": 1, "top_model": "gpt-4o-mini",
  "usage_health": {"ok": true, "detail": "...", "logs_today": 0},
  "failures": {"today_4xx": 0, "today_5xx": 0, "week_4xx": 0, "week_5xx": 0}
}
```

- 「周」按最近 7 天（含今天）。
- `*_credit` 三个字段恒为 0：定价与倍率属 M3，当前不计算。
- `realm` 参数当前不参与过滤（保留字段）。

### `GET /api/stats/daily?days=30`（登录）

`days` 夹逼到 1–90。返回逐日桶数组：`[{day: "2026-11-01", requests, prompt_tokens, completion_tokens, credit, failed}]`。`credit` 恒 0；`failed` 只计 4xx/5xx。

---

## 8. 设置（`/api/settings/*`、用户管理、系统）

### `GET /api/settings/upstream`（登录）

返回上游配置（存于 SQLite `settings` 表的 `upstream_config`），保证包含十组空兜底：`schedule / prompt / cooldown / pool / features / session_sticky / server / upstream / global / admin`，另有 `upstash: {url, has_token, token_masked}`（Upstash 未接入，仅占位）。

### `POST /api/settings/upstream`（管理员）

请求体为任意 dict，与存量深合并保存（`available / config_path / reload_hint / push` 四个只读键会被剔除）。若 codebuddy 已启用，会把十组配置真实 POST 到其管理端 `/api/settings/upstream`，结果回显在响应的 `push.codebuddy` 字段（`ok` + `reload_scheduled`/`reload_hint` 或 `message`）；未启用则 `push.codebuddy.ok = false` 且配置仅存本地。

### `GET /api/settings/gateway`（登录）

`{"allow_anonymous": false, "configured": false}`。`allow_anonymous` 显示实际生效值（DB 配置优先，否则环境变量）；`configured` 表示 DB 里是否配过。

### `POST /api/settings/gateway`（管理员）

请求体 `{"allow_anonymous": true|false}`，写入 DB（从此优先于环境变量）。返回 `{"allow_anonymous": ..., "configured": true}`。

### `GET /api/settings/model-map`（登录） / `POST /api/settings/model-map`（管理员）

模型别名表（dict：`别名 → 目标模型`，目标可带 `provider/` 前缀）。POST 会剔除键值为空的项并返回规范化后的表。

### `POST /api/settings/upstash/test`（管理员）

恒返回 `{"ok": false, "message": "M1 未接入 Upstash：持久化由控制面 SQLite 负责"}`。

### 用户管理

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /api/users` | 登录 | `[{"username", "role"}]` |
| `POST /api/users` | 管理员 | 创建。用户名 3–32 字符，密码 ≥6；角色 `admin`/`viewer`（默认 viewer）；重名 409 |
| `PATCH /api/users/{username}` | 管理员 | 改密码和/或角色；改密码会吊销该用户全部会话；不能把最后一个 admin 降级（400） |
| `DELETE /api/users/{username}` | 管理员 | 不能删自己、不能删最后一个 admin；删除会吊销其会话 |

PATCH 响应：`{"username", "role", "sessions_revoked", "relogin_required"}`（自己改自己的密码时 `relogin_required: true`）。

### 系统信息（登录）

- `GET /api/system/check-update`：恒返回「自更新通道未启用」的固定结构。
- `GET /api/system/versions`：`{"manager": "unified-2api M2", "upstream_connected": false, ...}` 固定结构。

---

## 9. sub2api 配额域（`/api/sub2api`）

语义：分组 → 账号池 → 配额扣减 → 耗尽 429。配额按 token 数（prompt+completion）累计。

### 分组

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /api/sub2api/groups` | 登录 | 分组列表（含 `accounts`、`active_accounts` 聚合数） |
| `POST /api/sub2api/groups` | 管理员 | upsert |
| `PATCH /api/sub2api/groups/{group_id}` | 管理员 | 部分更新 |
| `DELETE /api/sub2api/groups/{group_id}` | 管理员 | 删除 |

`POST /api/sub2api/groups` 请求体：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 必填 | 1–64 字符，已存在则覆盖 |
| `name` | string | `""` | 空则用 id |
| `platform` | string | `openai` | 平台标识 |
| `quota_limit` | number | `0` | `0` = 不限额；负值取 0 |

分组对象字段：`id, name, platform, quota_limit, quota_used, quota_remaining（限额>0 时为剩余值，否则 null）, reset_at, enabled, created_at, updated_at, accounts, active_accounts`。PATCH 支持 `name / platform / quota_limit / enabled / reset_quota`（`reset_quota: true` 同时清零已用额度与冷却）。

### 账号池

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /api/sub2api/accounts?group_id=` | 登录 | 列表，可按分组过滤 |
| `POST /api/sub2api/accounts` | 管理员 | 添加 |
| `PATCH /api/sub2api/accounts/{account_id}` | 管理员 | 部分更新 |
| `DELETE /api/sub2api/accounts/{account_id}` | 管理员 | 删除 |
| `POST /api/sub2api/accounts/{account_id}/reset` | 管理员 | 清零该账号额度与冷却 |

`POST /api/sub2api/accounts` 请求体：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `group_id` | string | 必填 | 不存在 404 |
| `name` | string | `""` | |
| `upstream_base` | string | `""` | 该账号的上游地址；为空且 provider 也未设 base_url 时调用会 503 |
| `api_key` | string | `""` | 该账号的上游密钥 |
| `weight` | int | `1` | 轮转权重（≥1） |
| `quota_limit` | number | `0` | `0` = 不限额 |

账号对象字段：`id, group_id, name, upstream_base, weight, quota_limit, quota_used, quota_remaining, enabled, cooldown_until, last_used_at, created_at`（`api_key` 不回显）。PATCH 额外支持 `clear_cooldown: true`。

账号选择：在 `enabled=1`、未超个人额度、未冷却的账号里按 `last_used_at` 升序、`weight` 降序取一。转发遇到 429/5xx（`429,500,502,503,504`）会冷却该账号（默认 60 秒）并换号重试，最多 3 次尝试。

### 流水

`GET /api/sub2api/ledger?group_id=&limit=50`（登录）：返回扣减流水（`limit` 夹逼 1–500，按 id 倒序，逐条含 `ts, group_id, account_id, model, prompt_tokens, completion_tokens, cost, status, note`；`cost` 为 token 计数而非金额）。

---

## 10. Grok 注册任务（`/api/grok/register`）

任务编排为控制面内一等服务的队列；浏览器执行器外置（`GROK_REGISTER_EXECUTOR_URL`），未配置时任务以明确原因失败。

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `POST /api/grok/register/tasks` | 管理员 | 入队 |
| `GET /api/grok/register/tasks?limit=50` | 登录 | 列表（limit 夹逼 1–200，按 id 倒序） |
| `GET /api/grok/register/tasks/{task_id}` | 登录 | 详情，404 不存在 |
| `POST /api/grok/register/tasks/{task_id}/cancel` | 管理员 | 取消；仅 `queued`/`running` 可取消，否则 409 |
| `POST /api/grok/register/tasks/{task_id}/retry` | 管理员 | 重试；仅 `failed`/`cancelled` 可重试（attempts 归零重新入队），否则 409 |
| `GET /api/grok/register/worker` | 登录 | 队列与并发状态 |

`POST .../tasks` 请求体：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `count` | int | 1 | 1–1000 |
| `workers` | int | 1 | 1–32，且被服务端夹逼到 `GROK_REGISTER_MAX_WORKERS` |
| `email_provider` | string | `""` | 邮箱渠道（由外部执行器解释） |
| `proxy` | string | `""` | 代理 URL |
| `max_attempts` | int | 3 | 1–5 |
| `payload` | object | `{}` | 透传给执行器的扩展参数 |

任务对象字段（`grok_tasks` 表原样）：`id, status, count, workers, email_provider, proxy, payload_json, result_json, attempts, max_attempts, error, created_at, started_at, finished_at, next_run_at`。

状态机：`queued → running → succeeded/failed/cancelled`；失败且未到 `max_attempts` 时按指数退避（5s 起、封顶 60s）重新入队。`worker` 返回：`{queued, running, succeeded, failed, cancelled, max_concurrency, max_workers, executor}`（`executor` 为 `http` 或 `unconfigured`）。

---

## 11. 错误码速查

| 状态码 | 典型场景 |
| --- | --- |
| 400 | 请求体非法、缺 `model`、参数越界（用户名/密钥名长度等）、`base_url` 不是 http(s)、无可更新字段 |
| 401 | 未登录/会话过期（`/api/*`）；网关密钥缺失或无效（`/v1/*`）；登录密码错误 |
| 403 | viewer 调管理员接口；sub2api 分组已停用；CodeBuddy 管理端拒绝 `wbt_` token（管理类操作需会话级管理员时透传上游 403） |
| 404 | provider / 密钥 / 账号 / 分组 / 任务不存在 |
| 409 | 用户名已存在；任务状态不允许取消/重试 |
| 429 | sub2api 分组或账号池额度耗尽 |
| 501 | 请求的 provider 不支持该能力（如 trae_sidecar 无 `/v1/messages`；deepseek sidecar 无流式 messages） |
| 502 | 上游不可达（连接失败、DNS 失败等） |
| 503 | 无已启用的 chat provider；sub2api 分组未配置；sub2api 账号缺 upstream_base |
| 上游透传 | 其余 4xx/5xx 原样转发状态码与响应体 |
