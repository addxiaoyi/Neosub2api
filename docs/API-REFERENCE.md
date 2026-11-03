# API 参考（API Reference）

依据 `apps/control/app/routers/` 下的实际路由整理（代码事实，非文档推导）。

- `/api/*`：管理面，登录态鉴权（HttpOnly Cookie `u2a_session`），部分仅管理员。
- `/v1/*`：模型网关，`Authorization: Bearer u2a_...` 或 `x-api-key: u2a_...` 密钥鉴权。
- `GET /healthz`：无鉴权。

通用约定：

- `/api/*` 错误响应为 `{"detail": "..."}`（FastAPI 默认结构 + ProviderError 处理器）；权限失败（403）额外带 `error` 对象，见下。
- `/v1/*` 错误响应为 `{"error": {...}, "detail": "..."}`，`error` 是 OpenAI 兼容对象，映射规则见「1. 网关」。
- 权限失败示例：`{"detail": "需要管理员权限", "error": {"type": "permission_denied", "code": "insufficient_role", "message": "需要管理员权限", "required_role": "admin", "role": "viewer"}}`（`role` 仅在后端能拿到当前角色时出现）。
- 时间戳均为 Unix 秒（整数）。
- 角色两种：`admin` / `viewer`。标注「管理员」的接口 viewer 调用返回 403。
- 请求体均为 JSON；`Content-Type: application/json`。

## 0. 健康检查

### `GET /healthz`

无鉴权。

```json
{"ok": true, "providers": 14, "db": "/data/unified.db"}
```

`providers` 为注册表内 provider 数量（不是已启用数）。

---

## 1. 网关（`/v1/*`，密钥鉴权）

鉴权规则（`routers/v1.py`）：

- 请求头 `Authorization: Bearer <u2a_密钥>`（`Bearer ` 前缀可省，裸 token 也接受），优先级最高。
- 请求头 `x-api-key: <u2a_密钥>`（Anthropic 官方 SDK 的默认鉴权头），与 `Authorization` 等价。
- 两个头同时带且值不同：`401 conflicting_auth_headers`（鉴权头冲突），不会静默取其一。
- 密钥按 SHA-256 比对 `api_keys` 表，仅 `enabled = 1` 的通过；命中即更新 `last_used_at`。
- 两个鉴权头都不带时：DB 里「网关鉴权」开关（`/api/settings/gateway`）优先；未配置过则回落到环境变量 `UNIFIED_ALLOW_ANONYMOUS_GATEWAY`（默认 false）。允许匿名时以 `anonymous` 身份记录，密钥归因为空。

### `POST /v1/chat/completions`

OpenAI Chat Completions 协议转发。

| 项 | 说明 |
| --- | --- |
| 鉴权 | 网关密钥 |
| 请求体 | 上游要求的 OpenAI 请求体原样转发；`model` 必填，缺失返回 400 |
| 请求头 | 可选 `x-provider: <provider_id>` 显式指定服务商 |
| 流式 | 请求体 `"stream": true` 时 SSE 直通（`text/event-stream`） |
| 日志 | 旁路写 `request_logs` + `usage_records`（流式从含 usage 的 SSE chunk 提取） |

模型解析顺序（`app/model_resolve.py`，三条 `/v1` 协议共用同一套）：

1. `x-provider` 头 → 用该 provider，模型名不变；
2. `model` 形如 `<provider_id>/<model>` 且前缀是已知 provider → 用该 provider，取后半段；显式指定优先级高于自动匹配；
3. 「设置 → 模型别名」表命中 → 目标值再按规则 2 解析；目标不带前缀时继续走规则 4；
4. 自动匹配：在**已启用** provider 的模型目录（与 `/v1/models` 同一份聚合数据）里找同名 `id`：
   - 恰好一家支持当前端点能力位 → 直接走该 provider，请求体、流式、计费、日志与带前缀写法完全一致；
   - 两家及以上支持 → 400 `ambiguous_model`，`candidates` 给 `[{provider, readiness}]` 供消歧；
   - 一家都没有 → 404；若该 `id` 只出现在**未启用** provider 的静态声明里，返回 404 `model_unavailable`，`candidates` 带 `readiness / requires / reason`，说明「存在但不可用」；
   - 命中的 provider 不支持该端点能力位 → 501 `capability_unsupported`（与带前缀写法一致）。
5. 解析结果直接落进 `request_logs` 既有字段 `provider` / `model`，不新增表；自动解析命中时另写一行 `unified.model_resolve` 日志便于对照。
6. 兜底开关 `UNIFIED_MODEL_FALLBACK_FIRST`（默认 `0`）只作用在规则 4 的「一家都没有」：
   - `0`：按上面返回 404 `model_not_found` / `model_unavailable`；
   - `1`：改用**首家启用的、支持当前端点能力位的 provider**，模型名原样透传。此时 `request_logs.provider` 写实际承接的 provider，`model` 写原模型名；另写一行 `unified.model_fallback` 日志（含请求 id、模型名、选中的 provider、候选 provider 数）；
   - 多家命中（400 `ambiguous_model`）不受开关影响；若没有任何启用的 provider 支持该能力位，仍返回原有 404 / 501，不会凭空造路由。

**行为变化**：旧实现查不到模型名时会兜底到第一家 chat provider，现在默认**没有兜底**——名字不在聚合目录里就直接 404。依赖「随便写个模型名也能跑」的客户端必须改成 `<provider>/<model>`、带 `x-provider`，或临时设置 `UNIFIED_MODEL_FALLBACK_FIRST=1`。

#### 错误契约

所有 provider 失败先归一成内部错误（实现在 `app/gateway_errors.py`），再**按端点协议**输出不同外壳；状态码映射三端点共用，不受外壳影响。

| 端点 | 错误外壳 |
| --- | --- |
| `/v1/chat/completions` | OpenAI：`{"error": {message, type, param, code, ...}}`，另带兼容字段 `detail` |
| `/v1/responses` | 同 Chat Completions（OpenAI 形状） |
| `/v1/messages` | Anthropic：`{"type": "error", "error": {type, message, ...}}`，**没有** `detail` |

OpenAI 外壳示例：

```json
{
  "error": {
    "message": "上游返回 401：token expired",
    "type": "authentication_error",
    "param": null,
    "code": "upstream_auth_failed",
    "provider": "codebuddy",
    "upstream_status": 401,
    "upstream_code": "401",
    "upstream_message": "token expired"
  },
  "detail": "上游返回 401：token expired"
}
```

Anthropic 外壳示例（`/v1/messages`）：

```json
{
  "type": "error",
  "error": {
    "type": "authentication_error",
    "message": "上游返回 401：token expired",
    "provider": "zcode",
    "upstream_status": 401,
    "upstream_code": "401",
    "upstream_message": "token expired"
  }
}
```

- Anthropic 的错误对象只认 `type` + `message`，`provider / upstream_status / upstream_code / upstream_message` 作为 `error` 的**附加键**保留；这是「按协议出形状」与「保留排查信息」之间的取舍。
- `provider`、`upstream_status`、`upstream_code`、`upstream_message` 是排查用的子字段：
  - `upstream_status` 是上游原始 HTTP 状态（含被合并的 403）；
  - `upstream_code` / `upstream_message` 由适配器从厂商错误体解析（兼容 OpenAI `error.code/message`、Anthropic `error.type/message`、`code` / `message` / `detail` 等常见键），拿不到时留空（键可能不出现），不会编造；
  - `upstream_message` 已掩码并截断到 200 字符。
- 上游状态码映射（`public_status`）：

| 上游状态 | 对外状态 | `error.type`（OpenAI / Anthropic） | `error.code`（仅 OpenAI 外壳） |
| --- | --- | --- | --- |
| 400 / 409 / 413 / 415 / 422，其余未列出的 4xx | 400 | `invalid_request_error` / `invalid_request_error` | `upstream_bad_request` |
| 401、403（合并） | 401 | `authentication_error` / `authentication_error` | `upstream_auth_failed` |
| 402 | 402 | `insufficient_quota` / `invalid_request_error` | `upstream_billing` |
| 404 | 404 | `invalid_request_error` / `not_found_error` | `upstream_not_found` |
| 408 | 504 | `api_error` / `timeout_error` | `upstream_timeout` |
| 429 | 429 | `rate_limit_error` / `rate_limit_error` | `upstream_rate_limited`（保留上游 `Retry-After`，缺失时默认 60） |
| 500 / 502 / 其余 5xx（合并） | 502 | `api_error` / `api_error` | `upstream_unavailable` |
| 503 | 503 | `api_error` / `overloaded_error` | `provider_unavailable` |
| 504 | 504 | `api_error` / `timeout_error` | `upstream_timeout` |
| 501（能力位未实现） | 501 | `invalid_request_error` / `invalid_request_error` | `capability_unsupported` |

网关本地拒绝同样按协议出外壳：缺密钥/密钥无效 `401 invalid_api_key`；`Authorization` 与 `x-api-key` 同时带上且值不同 `401 conflicting_auth_headers`；请求体非法 `400 invalid_json`；缺 `model` `400 missing_model`；配额耗尽 `429 quota_exceeded`（`detail` 里仍是结构化拒因，含 `window/metric/used/limit/reset_after`）。

模型解析失败（发生在转发之前，不写 `request_logs`、不产生流水）：

| 场景 | 状态 | OpenAI 外壳 `type` / `code` | Anthropic 外壳 `type` / `code` | 关键字段 |
| --- | --- | --- | --- | --- |
| 同名模型命中多家 | 400 | `ambiguous_model` / `ambiguous_model` | `invalid_request_error` / `ambiguous_model` | OpenAI 在 `detail.candidates`、Anthropic 在 `error.candidates` 给 `[{provider, readiness}]` |
| 没有已启用 provider 提供 | 404 | `model_not_found` / `model_not_found` | `not_found_error` / `model_not_found` | `detail.model` 回显，`candidates` 为空 |
| 只存在于未启用 provider | 404 | `model_unavailable` / `model_unavailable` | `not_found_error` / `model_unavailable` | `candidates` 含 `provider/readiness/enabled/requires/reason`，不含凭据与 `base_url` |

`code` 与 `candidates` 是 Anthropic 错误对象的附加键，只在解析类错误上出现；其余 Anthropic 错误仍是 `type` + `message` + `provider/upstream_*`。

#### 失败请求限流（429 `failure_rate_limited`）

按**密钥**做滑动窗口计数，计数直接聚合 `request_logs`（`status >= 400` 或 `error` 非空），不另建累加器：

- 窗口 `UNIFIED_FAILURE_WINDOW_SEC`（默认 60 秒）、上限 `UNIFIED_FAILURE_WINDOW_MAX`（默认 20 次），任一为 `0` 即关闭；匿名请求没有密钥维度，不参与计数。
- 达到上限后，后续请求在转发前返回 **429**，`error.type = rate_limit_error`、`error.code = failure_rate_limited`，响应头带 `Retry-After: <reset_after>`；`detail` 形状：

```json
{
  "type": "failure_rate_limited", "layer": "key", "metric": "failures",
  "used": 20, "limit": 20, "unit": "requests",
  "window": "sliding", "window_sec": 60,
  "reset_at": 1762000060, "reset_after": 42,
  "message": "60 秒内失败请求已达 20 次，上限 20 次；请在 42 秒后重试"
}
```

- `reset_after` 按「最早一条失败记录 + 窗长 - 当前时刻」计算，不是整窗重开。
- 被拒请求本身只写 `request_logs(status=429)`，不写 `usage_records` / `billing_ledger`。
- 注意：这条 429 日志的 `error` 非空，因此**也计入失败窗口**。客户端在限流期间持续重试，会把窗口一直堆满、让限制一直续命；请按 `Retry-After` 退避，别原地轮询。
- 限流查询读库失败时按旁路处理放过请求（不因统计组件故障阻断转发）。

**流式响应**：上游直接以 4xx/5xx 拒绝时返回普通 JSON 错误（不是 SSE）；若**已经出帧后中途断开**，先前的分片照常转发，随后补一个错误事件再正常结束：

- Chat Completions：`data: {"error": {...}}` 后跟 `data: [DONE]`；
- Messages（Anthropic）：`event: error` + `data: {"type": "error", "error": {type, message, ...}}`（Anthropic 无 `[DONE]`）；
- Responses：`event: error` + `data: {"type": "error", "code", "message", "param", ...}`（OpenAI Responses 的错误事件形状）。

计费侧不变：被拒请求（配额 429）只写 `request_logs`，不写 `billing_ledger`；中途失败的流式请求仍按已采集 usage 结算。

### `POST /v1/messages`

Anthropic Messages 协议转发，鉴权、模型解析、日志、状态码映射与 `/v1/chat/completions` 一致，仅能力位换成 `messages`；**错误外壳是 Anthropic 形状**（`{"type": "error", "error": {...}}`，详见「错误契约」），不是 OpenAI 的 `{"error": {...}}`。provider 不支持时 501（如 trae_sidecar、muse）。

### `POST /v1/responses`

OpenAI Responses 协议转发，鉴权、模型解析、日志、错误码与 `/v1/chat/completions` 一致（含 OpenAI `error` 外壳），能力位换成 `responses`（代码里同走 `_proxy`）。目前只有 `codar` 与 `qoder` 声明了该能力位，其余 provider 返回 501。

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

返回 14 家 provider 的公共信息列表（其中 3 家 `ready`、11 家 `needs_config`，逐家卡点见 [PROVIDERS.md](PROVIDERS.md#就绪状态)），每项字段：

| 字段 | 说明 |
| --- | --- |
| `id` / `name` / `label` | 标识与显示名 |
| `vendor` / `license` / `runtime` | 上游来源 / 许可证 / 运行形态 |
| `status` | `implemented`（主仓内置实现）或 `sidecar`（透明代理到独立进程） |
| `description` | 一句话描述 |
| `base_url` / `default_base_url` | 当前生效地址 / 默认地址 |
| `base_url_source` | 生效地址来源：`db`（provider 行或凭据行）/ `env` / `default` |
| `base_url_warning` | 历史默认值残留提示，空串表示正常。当前只对 `deepseek_sidecar` 的 `http://127.0.0.1:7872` 生效（真实默认是 5001）；启动时扫 `providers.base_url` 与启用中的凭据行并打日志，接口返回时按当前库值重算；只告警不改值 |
| `credential_base_warnings` | 同上检查作用于 `enabled=1` 的凭据行，数组项 `{provider, id, label, base_url, suggested_base_url, message}`；为空数组表示无残留，停用的凭据不提示 |
| `enabled` | 是否启用 |
| `capabilities` | `{chat, messages, models, responses, oauth, checkin, credits, register, sidecar}` 九个布尔位 |
| `protocols` | 支持的协议（`openai.chat.completions` / `anthropic.messages` / `openai.responses`） |
| `readiness` / `requires` | `ready` / `needs_config`，以及缺什么（给人看的短句；`ready` 时为空数组） |
| `allows_local_base_url` | 是否允许把 `base_url` / 凭据 `base_url` 填成本地 loopback 地址；由 `default_base_url` 是否指向 loopback 推导（四家 sidecar 与 `codebuddy` 管理端为 `true`） |
| `risk_level` / `risk_note` | `safe` / `warn` / `danger` 及说明 |
| `has_api_key` | 是否已配置上游密钥（不回显明文） |
| `credential_count` / `credentials` | 凭据条数 / 掩码列表（`id`、`label`、`base_url`、`secret_masked`、`sort_order`） |
| `options` | 已保存的高级配置 dict |

### `PATCH /api/providers/{provider_id}`（管理员）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `enabled` | bool | 启用/停用 |
| `base_url` | string | 必须 `http://` 或 `https://` 开头，否则 400；尾部 `/` 自动去除。本地 loopback 地址只对 `allows_local_base_url=true` 的 provider 放行，其余返回 400 并在 `detail` 说明原因；私网、链路本地与云元数据地址一律 400（元数据地址即使 `UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1` 也拒绝） |
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

跨所有 provider 聚合真实并集：同一 `id` 被多家支持时合并为一条，`providers` 列出全部归属，`provider_states` 保留每家的来源与就绪状态。默认只列 `enabled=1` 的 provider；`?provider=<id>` 可显式只看一家（此时不再按 enabled 过滤，未知 id 返回 404）。

```json
{
  "models": [
    {
      "id": "shared-model",
      "object": "model",
      "owned_by": "codebuddy",
      "provider": "codebuddy",
      "providers": ["codebuddy", "muse"],
      "source": "upstream",
      "provider_states": {
        "codebuddy": {"source": "upstream", "readiness": "needs_config", "requires": ["..."], "enabled": true, "available": false, "unavailable_reason": "...", "fallback_reason": null},
        "muse": {"source": "declared", "readiness": "needs_config", "requires": ["..."], "enabled": true, "available": false, "unavailable_reason": "...", "fallback_reason": null}
      },
      "readiness": "needs_config",
      "requires": ["..."],
      "enabled": true,
      "available": false,
      "unavailable_reason": "汇总的不可用原因",
      "fallback_reason": null
    }
  ],
  "source": "dynamic",
  "count": 1
}
```

条目字段：

| 字段 | 说明 |
| --- | --- |
| `provider` / `providers` | 主归属 provider（首个返回该 id 的家）与全部归属列表 |
| `source` | 主归属的来源：`upstream` / `declared` / `declared_fallback` |
| `provider_states` | 每个归属 provider 的 `{source, readiness, requires, enabled, available, unavailable_reason, fallback_reason}` |
| `readiness` / `requires` | 沿用 provider 清单的语义；`needs_config` 的模型不会被隐藏 |
| `available` | 任一归属 provider 既已启用又 `ready` 时为 `true` |
| `unavailable_reason` | 不可用原因；`available=true` 时为 `null` |
| `fallback_reason` | 主归属处于 `declared_fallback` 时的上游失败原因（已脱敏） |

来源判定只看实际行为，不做推测：

- `declared`：适配器只有静态声明（没有覆盖 `fetch_models`）；
- `upstream`：覆盖了 `fetch_models` 且本次真实拉取成功（缓存命中沿用上游结果）；
- `declared_fallback`：覆盖了 `fetch_models` 但拉取失败，回落到该适配器的静态声明，并在条目上带脱敏后的失败原因。拿不到上游又没有声明时返回空，不补条目。

顶层 `source`（有结果 `dynamic`，否则 `unknown`）与 `count` 保持不变以兼容旧前端。上游实拉结果按 `UNIFIED_MODELS_CACHE_TTL_SEC`（默认 60 秒）做进程内缓存，键含 provider、`base_url`、凭据指纹与 `models_path`/`tier` 选项，切地址或换凭据后不会读到旧缓存。

与 `/v1/models` 的区别：登录态而非密钥鉴权、去重合并，并附带来源与就绪状态。


---

## 6. 网关密钥（`/api/keys`）

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /api/keys` | 登录 | 列表，不含明文；admin 返回全部，非 admin 只返回 `owner_user_id = 自己用户名` 的密钥（历史 NULL 归属对非 admin 不可见） |
| `POST /api/keys` | 管理员 | 创建，明文只返回一次；`owner_user_id` 可空，缺省归当前登录用户 |
| `PATCH /api/keys/{key_id}` | 管理员 | 改名 / 启停 |
| `DELETE /api/keys/{key_id}` | 管理员 | 吊销（物理删除，立即失效） |

### `POST /api/keys`

请求体 `{"name": "...", "owner_user_id": "..."}`（`name` 1–64 字符，超限 400；`owner_user_id` 可省略或留空，此时归当前登录用户；指定不存在的用户返回 400「归属用户不存在」）。响应见 [GETTING-STARTED.md](GETTING-STARTED.md) 第 6 节——`key` 字段仅此一次。

密钥行的公共字段为 `id, name, prefix, enabled, created_at, last_used_at, owner_user_id`。归属存的是用户名而不是数字 id：`users` 表主键是 TEXT 的 `username`，与 `owner_user_id` 一一对应；删除用户不会自动回收其密钥，这些密钥会变成只有 admin 可见的孤儿记录，需要管理员显式吊销。

### `PATCH /api/keys/{key_id}`

请求体 `{"name": "...", "enabled": true}`，至少一项。返回更新后的公共行（`id, name, prefix, enabled, created_at, last_used_at, owner_user_id`）。归属不随 PATCH 变更。

### `DELETE /api/keys/{key_id}`

返回 `{"ok": true}`；不存在 404。

密钥的额度策略与告警见第 11 节（`/api/quota`）；密钥表本身仍不限制模型白名单。

---

## 7. 统计（`/api/stats`）

### `GET /api/stats/summary?realm=`（登录）

```json
{
  "today_requests": 0, "today_tokens": 0,
  "week_requests": 0, "week_tokens": 0,
  "total_requests": 0, "total_tokens": 0,
  "today_credit": 0.000098, "week_credit": 1.204311, "total_credit": 12.50842,
  "active_keys": 1, "top_model": "gpt-4o-mini",
  "usage_health": {"ok": true, "detail": "...", "logs_today": 0},
  "failures": {"today_4xx": 0, "today_5xx": 0, "week_4xx": 0, "week_5xx": 0}
}
```

- 「周」按最近 7 天（含今天）。
- 成功口径：`status < 400` **且** `error` 列为空。流式请求已经出帧后中途断开时，HTTP 层只能保持 200，这类请求靠 `request_logs.error` 判为失败——计入 `failures.today_5xx / week_5xx`，不计入 `today_requests / week_requests`；`top_model` 与 `usage_health.logs_today` 用同一口径。`/api/logs?status=ok|error` 与之一致（`ok` 排除带 error 的 2xx）。
- `*_credit` 是历史字段名，值现在是**美元金额**：分别对 `billing_ledger.billed_micro` 求和（全量 / 最近 7 天 / 今天），再在展示层用 `to_usd()` 换算并保留 6 位小数。落库始终是整数 micro-USD。
- 金额只来自已落库流水：`status >= 400` 且没有 usage 的请求不写流水，因此不产生金额（详见第 10 节）。
- `realm` 参数当前不参与过滤（保留字段）。

### `GET /api/stats/daily?days=30`（登录）

`days` 夹逼到 1–90。返回逐日桶数组：`[{day: "2026-11-01", requests, prompt_tokens, completion_tokens, credit, failed}]`。`credit` 是当天 `billing_ledger.billed_micro` 求和后换算的美元；`requests` 与 `failed` 使用与 summary 相同的成功口径（`status < 400` 且 `error` 为空才算成功，带 error 的 2xx 计入 `failed`）。金额与 success 分桶无关：失败请求通常没有金额，但带 usage 的失败请求仍会记账。

统计域**只有以上两个端点**：没有 `/api/stats/keys` 与 `/api/stats/providers`，按密钥 / 按 provider 的拆分明细暂无接口，需要时直接在 `request_logs` / `billing_ledger` 上按 `key_id` / `provider` 列自行统计。

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

鉴权口径：这里的 GET 端点读的是**全局**管理数据（`sub2api_*` 表没有归属列，分组/账号池/流水属于整个实例而不是某个登录用户），因此与 `/api/quota/*` 读接口一致，要求 `admin`；viewer 一律在查库前返回 `403 无权查看配额数据`，不会因为传入不存在的 `group_id` 变成 404 而暴露存在性。

### 分组

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /api/sub2api/groups` | 管理员 | 分组列表（含 `accounts`、`active_accounts` 聚合数） |
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
| `GET /api/sub2api/accounts?group_id=` | 管理员 | 列表，可按分组过滤 |
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

`GET /api/sub2api/ledger?group_id=&limit=50`（管理员）：返回扣减流水（`limit` 夹逼 1–500，按 id 倒序，逐条含 `ts, group_id, account_id, model, prompt_tokens, completion_tokens, cost, status, note`；`cost` 为 token 计数而非金额）。

---

## 10. 计费（`/api/pricing`）

M3-1 的消费侧计费只算钱、不拦人：网关转发链路不会因为价格缺失或计费异常而拒绝请求；价格解析失败只记日志，结算按「未定价」处理。金额一律以整数 micro-USD 落库（1 micro = 1e-6 美元），美元只出现在展示层。

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /api/pricing/books` | 登录 | 价格行列表；可按 `provider` / `model` / `token_class` 过滤，`limit` 夹逼 1–1000（默认 200），按 provider、model、token_class、effective_from 倒序 |
| `POST /api/pricing/books` | 管理员 | 追加一行价格；同一唯一键已存在返回 409 |
| `GET /api/pricing/resolve` | 登录 | 干跑解析：某 provider + model 在 `at` 时刻命中的价格行 |
| `POST /api/pricing/import` | 管理员 | 从 `config/prices.yaml` 幂等导入；`dry_run=true` 只统计不落库 |

**没有 `PATCH` / `DELETE`。** 价格表 append-only：历史行是已发生流水的定价依据，原地改价会让 `billing_ledger` 的价格快照对不上账。改价 = 插入一条 `effective_from` 更晚的新行，旧行保留。

### `POST /api/pricing/books`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `model` | string | 必填 | 模型名；`*` 表示通配 |
| `token_class` | string | 必填 | `input` / `output` / `cache_read` / `cache_write` / `reasoning` |
| `price_micro_per_1m` | int | 必填 | 每 1M token 的 micro-USD，非负 |
| `provider` | string | `*` | 适配器 id；`*` 表示对所有 provider 生效 |
| `effective_from` | int | 当前秒 | 生效 epoch 秒；填未来时间即定时生效 |
| `note` | string | `""` | 价格来源与口径说明 |

响应（列表项同构）：

```json
{
  "id": 42, "provider": "*", "model": "gpt-4o", "token_class": "input",
  "price_micro_per_1m": 2500000, "price_usd_per_1m": 2.5,
  "currency": "USD", "effective_from": 1793548800,
  "created_by": "admin", "note": "..."
}
```

唯一键为 `(provider, model, token_class, effective_from)`；`currency` 当前固定 `USD`。解析优先级：精确 provider 行优先于 `*`，精确 model 行优先于 `*`，同精度取 `effective_from` 最新且不晚于请求准入时刻的行。

### `GET /api/pricing/resolve?provider=&model=&at=`

`provider` / `model` 必填，`at` 默认当前秒。返回每类 token 命中的行，以及没有独立价格行的类别：

```json
{
  "provider": "opencode", "model": "gpt-4o", "at": 1793548800,
  "lines": {
    "input": {"price_id": 42, "provider": "*", "model": "gpt-4o",
              "price_micro_per_1m": 2500000, "price_usd_per_1m": 2.5},
    "output": {"price_id": 43, "provider": "*", "model": "gpt-4o",
               "price_micro_per_1m": 10000000, "price_usd_per_1m": 10}
  },
  "missing": ["cache_write", "reasoning"]
}
```

`missing` 只表示没有独立价格行。`reasoning` 缺失不是问题——结算时回落到 `output` 价；其它类别缺失则该类 token 不计费，并让这笔流水 `estimated=1`。

### `POST /api/pricing/import?dry_run=false`

读取仓库内 `config/prices.yaml`（文件不存在 404，YAML 非法 400）。逐条插入缺失的行，已存在的唯一键跳过、不覆盖；`prices` 条目既可写 `price_micro_per_1m`，也可写 `price_per_mtok`（字符串小数，服务端乘 1e6 后四舍五入）。`unpriced` 段只回显给调用方，不落库——没有公开价就不编价。

```json
{"inserted": 24, "skipped": 0, "total": 24, "dry_run": false,
 "currency": "USD", "unpriced": ["..."], "path": ".../config/prices.yaml"}
```

### 数据表

`price_books`：价格版本行，只追加。

| 字段 | 说明 |
| --- | --- |
| `id` | 行 id；流水通过快照间接引用 |
| `provider` / `model` | 适配器 id / 实际模型名；`*` 为通配 |
| `token_class` | 五类之一，见「计费口径」 |
| `price_micro_per_1m` | 每 1M token 的 micro-USD，非负整数 |
| `currency` | 当前固定 `USD`，不做多币种 |
| `effective_from` | 生效 epoch 秒 |
| `created_by` / `note` | 操作者与价格来源（审计） |

唯一键 `UNIQUE(provider, model, token_class, effective_from)`；查询索引 `(token_class, provider, model, effective_from)`。

`price_book_snapshots`：每个请求准入时冻结一份解析结果。

| 字段 | 说明 |
| --- | --- |
| `id` | 快照 id，被流水的 `price_snapshot_id` 引用 |
| `resolved_at` | 解析时间（= 请求准入时刻，不是落库时刻） |
| `provider` / `model` | 本次请求的 provider 与模型 |
| `lines_json` | `{"input": {"price_id": 42, "provider": "*", "model": "gpt-4o", "price_micro_per_1m": 2500000}, ...}` |

没有任何价格行命中时不写空快照，`price_snapshot_id` 为 `NULL`。

`billing_ledger`：每请求一行，金额已定格，可重算可审计。

| 字段 | 说明 |
| --- | --- |
| `ts` | 落库时间 epoch 秒 |
| `key_id` / `key_name` | 网关密钥归因；匿名或失效密钥 `key_id` 为 `NULL` |
| `billing_account_id` | 冗余自 `api_keys`；M3-1 恒 `NULL`（M3-2 回填） |
| `provider` / `model` / `status` | 实际路由结果与上游状态码 |
| `input_tokens` | OpenAI：`prompt_tokens` 减去缓存命中；Anthropic：`input_tokens` 原值 |
| `output_tokens` | 已扣除 reasoning 的可见输出 |
| `cache_read_tokens` / `cache_write_tokens` / `reasoning_tokens` | 其余三类，未出现即 0 |
| `billed_micro` | 定格金额，整数 micro-USD；只增，退款走负行 |
| `price_snapshot_id` | 指向 `price_book_snapshots.id`，无价格行时 `NULL` |
| `estimated` | `1` = 金额不完整（命中未定价档） |
| `note` | `usage_missing` / `usage_zero:<provider>` / `unpriced:input,output` 等 |

### 计费口径

五类 token 的拆分不靠 provider 名猜，按 usage 里「字段是否存在」判定：

| 类别 | OpenAI 来源 | Anthropic 来源 |
| --- | --- | --- |
| `input` | `prompt_tokens` 减去 `cached_tokens` | `input_tokens`（本身不含缓存） |
| `output` | `completion_tokens` 减去 `reasoning_tokens` | `output_tokens` 减去 reasoning 明细 |
| `cache_read` | `prompt_tokens_details.cached_tokens` | `cache_read_input_tokens` |
| `cache_write` | 0（OpenAI 不单收缓存写入费） | `cache_creation_input_tokens` |
| `reasoning` | `completion_tokens_details.reasoning_tokens` | 输出明细里的 reasoning |

单类金额公式，四舍五入到 1 micro：

```text
cost_micro = (tokens × price_micro_per_1m + 500000) // 1000000
```

每类各自取整后再相加，所以单请求最大误差 ≤ 2.5 micro；两套协议行为一致。

结算规则：

- `reasoning` 没有独立价格行时回落到 `output` 价；`output` 也缺价才算未定价。
- 未定价：只计有价部分，`billed_micro` 可能小于真实成本，`estimated=1`，`note` 记 `unpriced:<类别>`。
- 没有 usage（上游没回 usage 或流式里没捞到）：金额记 0，`note=usage_missing`，`estimated=0`（没有 token 需要定价）。
- 有 usage 但五类 token 全为 0（如 muse）：金额记 0，`note=usage_zero:<provider>`，`estimated=0`；与"完全没给数"刻意分开，便于统计该通道的免费调用量。
- `status >= 400` 且没有 usage：不写流水，痕迹只留在 `request_logs`。
- `status >= 400` 但有 usage：照常结算（按实际 token 计费）。
- 计费异常不影响网关：`settle` 抛错时保留已写的日志与原始 usage，只记异常。

### 可复算示例

`gpt-4o` 的种子价：input `2.50` 美元/1M（`2500000` micro）、output `10.00` 美元/1M（`10000000` micro）。一次请求 usage 为 11 prompt + 7 completion，无缓存、无 reasoning：

| 类别 | 计算 | micro |
| --- | --- | --- |
| `input` | `(11 × 2500000 + 500000) // 1000000` | 28 |
| `output` | `(7 × 10000000 + 500000) // 1000000` | 70 |
| 合计 `billed_micro` | 28 + 70 | **98** |

98 micro = `$0.000098`（`to_usd()` 展示为 `0.000098`）。注意不是 97.5 micro：每类先取整，input 的 27.5 进到 28。

### 价格快照与在途请求

价格在**准入时**解析并冻结：请求进入网关、路由到 provider 后立刻 `resolve_prices(at=准入秒)`，把结果放进本次请求的内存上下文；流式响应结束、非流式响应返回后结算时，用的还是这份结果（同一份 `price_book_snapshots`）。

因此：

- 请求进行中插入更晚生效的价格行，不会改变这笔请求的金额；新价只影响之后准入的请求。
- 同一请求的流式与非流式路径金额口径一致，不因结算时刻不同而取到不同价格。
- 事后改价是不可能的（没有 PATCH/DELETE）；对账用流水上的 `price_snapshot_id` 回查当时的价。

---

## 11. 配额策略与告警（`/api/quota`）

按网关密钥设日/月额度，可再按 provider / 模型收窄；金额以整数 micro-USD 落库，`*_usd` 只是展示换算。

### 密钥标识（`key_id`）

配额端点按「入参通道」区分，指向同一条 `api_keys` 记录：

- **Query 参数 `key_id`**：只接受数字 `api_keys.id`，与改动前行为完全一致。完整 `u2a_` 密钥**不允许**出现在 URL 中（会进浏览器历史、Referer、反代/access log），出现即 **400**，文案明确提示改用 `X-Api-Key` 请求头。
- **请求头 `X-Api-Key`**：GET `/api/quota/policies`、`/status`、`/alerts` 可选用它按完整密钥定位；服务端按 SHA-256 哈希精确匹配，无需前端把 `u2a_` 映射成数字。与 `key_id` 同时出现返回 **400**。
- **POST body `key_id`**：可传数字 id 或完整 `u2a_` 密钥（body 不经过 URL）；服务端解析到同一条记录。
- 形态非法（空串、非数字、`X-Api-Key` 不是完整 `u2a_` 密钥）返回 **400**；形态合法但解析不到（不存在或已吊销）返回 **404**，不会静默落到另一条密钥。

出参统一带数字 `key_id`、密钥名称 `key_name`，以及掩码后的 `key_prefix`（形如 `u2a_abcd****`）。`key_prefix` 仅供展示，任何端点都不输出完整密钥。

日志侧：`request_logs` 落库前对 `provider` / `model` / `error` / `key_name` 里疑似 `u2a_` 的片段统一掩码；服务自身写出的 `uvicorn.access` 访问日志同样在落盘前替换为 `u2a_…末4位`。反向代理（nginx 等）的 access log 不在应用内，需在代理层另行配置脱敏。

### 端点

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /api/quota/policies?key_id=` | 登录（admin 全部 / 归属者自有） | 策略列表；`key_id` 只收数字 id，按密钥定位用 `X-Api-Key` 请求头 |
| `POST /api/quota/policies` | 管理员 | 按 `(key_id, provider, model, window)` 幂等 upsert，阈值整体替换 |
| `DELETE /api/quota/policies/{policy_id}` | 管理员 | 删除策略与阈值；告警历史保留 |
| `GET /api/quota/status?key_id=&at=` | 管理员 / 归属者 | 当前周期用量与告警；`at` 缺省为当前秒 |
| `GET /api/quota/alerts?key_id=&policy_id=&limit=&offset=` | 管理员 / 归属者 | 告警历史，支持过滤与分页 |

配额读取权限按 `api_keys.owner_user_id` 判定（该列存 `users.username`）：

- `admin`：可查全部密钥，`key_id` / `X-Api-Key` 只作过滤条件。
- 非 admin：不带过滤条件时只返回自己名下密钥的策略/状态/告警；用 `X-Api-Key` 或数字 `key_id` 查他人的密钥返回 **403 `无权查看该密钥的配额数据`**；历史 NULL 归属的密钥对非 admin 不可见。
- 鉴权与归属判断都在解析密钥之前完成，不区分密钥是否存在，避免 403/404 差异被用于存在性探测。出参里的 `key_id` / `key_name` / `key_prefix` 只用于展示，不改变可见范围。

`POST /api/quota/policies` 请求体：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `key_id` | int / string | 必填 | 数字 id 或 `u2a_` 明文密钥 |
| `window` | string | 必填 | `day` / `month` |
| `provider` | string | `""` | 空或 `*` = 不限 provider |
| `model` | string | `""` | 空或 `*` = 不限模型 |
| `limit_micro` | int | `0` | 金额上限 micro-USD，`0` = 不设 |
| `limit_requests` | int | `0` | 请求数上限，`0` = 不设 |
| `thresholds` | int[] | `[]` | 告警阈值百分比，1–1000，去重后升序 |
| `enabled` | bool | `true` | 是否生效 |

策略对象字段：`id, key_id, key_name, key_prefix, provider, model, window, limit_micro, limit_usd, limit_requests, enabled, thresholds, created_at, updated_at`。

### 告警分页

`GET /api/quota/alerts` 使用 **`limit` + `offset`**：

- `limit` 夹逼到 1–1000，默认 100；`offset` 默认 0，负数按 0。
- `total` 是过滤条件下的**真实总数**（独立 `COUNT(*)`），与当页 `items` 条数无关。
- 不传分页参数时响应仍是 `{"total": ..., "items": [...]}`，形状与改动前一致；`total` 语义修正为真实总数。
- 选择 `limit` + `offset` 而不是 `page` / `size`：既有接口已经暴露 `limit`，新增 `offset` 是最小兼容增量，也不会出现两套互相重叠的页大小参数。

---

## 12. Grok 注册任务（`/api/grok/register`）

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

## 13. 错误码速查

| 状态码 | 典型场景 |
| --- | --- |
| 400 | 请求体非法、缺 `model`、参数越界（用户名/密钥名长度等）、`base_url` 不是 http(s)、无可更新字段；配额 `key_id` 形态非法、`window`/阈值越界 |
| 401 | 未登录/会话过期（`/api/*`）；网关密钥缺失或无效（`/v1/*`）；登录密码错误 |
| 403 | viewer 调管理员接口或读取非自有密钥的配额数据（响应带 `error.type=permission_denied`、`required_role`）；sub2api 分组已停用；CodeBuddy 管理端拒绝 `wbt_` token |
| 404 | provider / 密钥 / 账号 / 分组 / 任务不存在；配额 `key_id` 引用的密钥或策略不存在；模型解析失败（`model_not_found` / `model_unavailable`） |
| 409 | 用户名已存在；任务状态不允许取消/重试 |
| 429 | sub2api 分组或账号池额度耗尽；网关密钥触发配额上限；同一密钥失败请求达到窗口上限（`failure_rate_limited`）；上游 429（保留 `Retry-After`） |
| 501 | 请求的 provider 不支持该能力（如 trae_sidecar 无 `/v1/messages`；deepseek sidecar 无流式 messages） |
| 502 | 上游不可达（连接失败、DNS 失败等）；上游 5xx 归并到 502（原始状态码见 `error.upstream_status`） |
| 503 | 无已启用的 chat provider；sub2api 分组未配置；sub2api 账号缺 upstream_base |
| 504 | 上游超时（408/504 归并）；流式响应超过时长上限 |
