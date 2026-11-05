# API 参考（API Reference）

依据 `apps/control/app/routers/` 下的实际路由整理（代码事实，非文档推导）。

- `/api/*`：管理面，登录态鉴权（HttpOnly Cookie `u2a_session`），部分仅管理员。
- `/v1/*`：模型网关，`Authorization: Bearer u2a_...` 或 `x-api-key: u2a_...` 密钥鉴权。
- `GET /healthz`：无鉴权。
- `GET /`：前端静态入口，不是 JSON——前端静态目录（默认 `apps/web/out`，`UNIFIED_WEB_DIR` 可改）存在时挂载 StaticFiles 返回 HTML（`npm run build:export` 产物）；不存在时返回 `{"service": "...", "hint": "未找到前端静态产物…"}` 提示（`main.py`）。

通用约定：

- `/api/*` 错误响应为 `{"detail": "..."}`（FastAPI 默认结构 + ProviderError 处理器）；权限失败（403）额外带 `error` 对象，见下。
- `/v1/*` 错误响应为 `{"error": {...}, "detail": "..."}`，`error` 是 OpenAI 兼容对象，映射规则见「1. 网关」。
- 权限失败示例：`{"detail": "需要管理员权限", "error": {"type": "permission_denied", "code": "insufficient_role", "message": "需要管理员权限", "required_role": "admin", "role": "viewer"}}`（`role` 仅在后端能拿到当前角色时出现）。
- 时间戳均为 Unix 秒（整数）。
- 角色两种：`admin` / `viewer`。标注「管理员」的接口 viewer 调用返回 403。
- 请求体均为 JSON；`Content-Type: application/json`。

## 0. 健康检查与探针

### `GET /healthz`（根路径，非 `/api` 前缀，免鉴权）

```json
{"ok": true, "providers": 14}
```

`providers` 为注册表内 provider 数量（不是已启用数）。**不回传库路径**（S-09；`tests/test_hardening.py::test_docs_disabled_and_healthz_hides_db` 断言 `db` 键不存在）。

### `GET /api/health`（免鉴权，liveness）

```json
{"ok": true, "status": "ok", "probe": "liveness", "service": "unified-2api control plane", "version": "..."}
```

进程活着即 200，不查任何依赖（不碰库、不碰 provider 注册表）。

### `GET /api/health/ready`（免鉴权，readiness，200 / 503）

```json
{
  "ok": true, "status": "ok", "service": "...", "version": "...",
  "checks": {
    "database": {"ok": true, "latency_ms": 0.31},
    "providers": {"ok": true, "enabled": 2, "available": 2, "needs_config": 0, "items": [{"id": "...", "enabled": true, "readiness": "ready", "available": true, "requires": []}]}
  },
  "unhealthy": [], "request_id": "..."
}
```

- `database`：`SELECT 1` 耗时；失败只回 `detail: "数据库不可读"`，不回异常原文（sqlite 报错可能带库路径）。
- `providers`：readiness 口径是「已启用且 `readiness=ready`」才算 `available`，`needs_config` 即使启用也不计入。
- 任一子系统不健康 → **503**，`unhealthy` 里点名（`database` / `providers`）。
- 两个探针都只给子系统状态，不回传 `base_url`、凭据、库路径等部署信息。

### `GET /api/metrics?window_seconds=3600`（管理员）

按 `request_logs` 聚合的运维指标，不新建累加器，口径与 `/api/logs`、`/api/stats` 一致。`window_seconds` 夹逼 60–2592000（30 天）。返回 JSON：

```json
{
  "generated_at": 0, "window_seconds": 3600,
  "requests": {"total": 0, "success": 0, "failed": 0},
  "providers": [{"provider": "openai", "requests": 0, "success": 0, "failed": 0}],
  "failure_rate_limited": 0
}
```

`failure_rate_limited` 是窗口内 `status = 429` 且 `error` 含 `failure_rate_limited` 的条数。未登录 401，viewer 403（它暴露按 provider 的失败分布，与 `/api/stats` 同一权限面；免鉴权只留给两个探针）。

> **2026-11-05 已更正（原句保留作历史）**
>
> 本段原记「返回 JSON」并把 JSON 示例挂在 `GET /api/metrics` 上；当前该路径返回 **Prometheus 文本**（`text/plain; version=0.0.4`），原 JSON 已迁到 `GET /api/metrics/json`。
>
> | 项 | 当前事实 | 代码 |
> | --- | --- | --- |
> | 响应类型 | `Response(content=..., media_type=PROMETHEUS_MEDIA_TYPE)`；媒体类型常量 `text/plain; version=0.0.4` 在 `health.py:45`，窗口上下限在 `health.py:43-44` | `health.py:410-427` |
> | 鉴权 | `metrics_access`：默认仅 `admin`；配置 `UNIFIED_METRICS_TOKEN` 后额外接受 `Authorization: Bearer <token>`（`secrets.compare_digest` 比对，命中即放行）；Bearer 未命中不会跳过登录态校验 | `health.py:143-157` |
> | 旧 JSON 路径 | `GET /api/metrics/json`，`Depends(admin_user)`，字段与迁移前一致 | `health.py:430-447` |
> | `window_seconds` | 两个端点共用 `_clamp_window`，夹逼 60–2592000 | `health.py:406-407` |
> | 抓取副作用 | 渲染前调用 `evaluate_alerts`，评估失败只记日志、不阻断抓取 | `health.py:420-423`、`440-443` |
>
> **这条为什么重要**：照旧按 JSON 解析的抓取器/面板会解析失败；以为「除 admin 登录外还有匿名面」或反过来「配了 token 也必须带 Cookie」都会让抓取被 401/403 拦住，最后被误判成服务不可用。

### `GET /api/metrics/json?window_seconds=3600`（管理员）

旧版自定义 JSON 指标，路径从 `/api/metrics` 迁来、字段不变（`health.py:430-447`）。`window_seconds` 同 `/api/metrics`，夹逼 60–2592000（`health.py:406-407`）。

```json
{
  "generated_at": 0, "window_seconds": 3600,
  "requests": {"total": 0, "success": 0, "failed": 0},
  "providers": [{"provider": "openai", "requests": 0, "success": 0, "failed": 0}],
  "failure_rate_limited": 0
}
```

`failure_rate_limited` 口径不变：窗口内 `status = 429` 且 `error` 含 `failure_rate_limited` 的条数。指标按 `request_logs` 聚合，不新建累加器。鉴权只走 `admin_user`，没有 `UNIFIED_METRICS_TOKEN` 旁路；未登录 401，viewer 403。

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

1. `x-provider` 头 → 用该 provider，跳过别名表；`model` 带**同源** `<provider>/` 前缀时按前缀剥离、只把裸模型名交给上游（不再整串透传），带**其他** provider 的前缀时 400 `conflicting_provider_hints`（不静默取头，`detail` 带 `header_provider`/`prefix_provider`，`error.provider` 取头值），头指向本地注册表没有的 provider 时 404 `provider_not_found`。判定顺序固化：本地存在性(404) → 冲突(400) → 停用(503)；
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

**行为变化**：旧实现查不到模型名时会兜底到第一家 chat provider，现在默认**没有兜底**——名字不在聚合目录里就直接 404。依赖「随便写个模型名也能跑」的客户端必须改成 `<provider>/<model>`、带 `x-provider`，或临时设置 `UNIFIED_MODEL_FALLBACK_FIRST=1`。显式指定（`x-provider` 或前缀）的 provider 必须先启用，停用时返回 503 `provider_disabled`，不会静默改走其它 provider。

#### 跨 provider 故障转移（`UNIFIED_FAILOVER_ENABLED`，默认关）

| 项 | 事实 | 代码 |
| --- | --- | --- |
| 开关 | `UNIFIED_FAILOVER_ENABLED`，每个请求读环境变量；`1/true/yes/on` 视为开，缺省为关 | `model_resolve.py:54, 67-70` |
| 尝试次数 | `UNIFIED_FAILOVER_MAX_ATTEMPTS`，默认 2，钳制 1–10；非整数回默认 2 | `model_resolve.py:55-57, 73-79` |
| 实际取几个候选 | `targets[:failover_max_attempts()]`；关闭时只取 `targets[:1]` | `v1.py:424-426` |
| 作用范围 | 只作用于「本来就有多个候选」的两条路径：模型目录多命中、`UNIFIED_MODEL_FALLBACK_FIRST` 的兜底候选；显式 `x-provider` 与 `provider/model` 前缀恒单目标、不换家 | `model_resolve.py:14-18, 356-372` |
| 成功响应头 | 流式与非流式都带 `x-upstream-provider: <最终承接的 provider id>`；失败响应不带 | `v1.py:532, 547` |
| 每次尝试 | 每次尝试独立写一条 `request_logs`；开关打开且有备选时 `error` 前缀 `[尝试N/M]`；转移另打一条 WARNING 日志（从谁到谁、原因状态码与文案） | `v1.py:376-380, 387-392, 445-479` |

转移判据（`retryable_status()`，`model_resolve.py:82-84`）：

| 上游/网关结果 | 是否换下一个 provider | 依据 |
| --- | --- | --- |
| 5xx（501 除外） | 转移 | `model_resolve.py:64, 82-84` |
| 408 | 转移 | `model_resolve.py:62, 82-84` |
| 429（上游限流） | 转移 | `model_resolve.py:62, 82-84` |
| 连接失败 / 读超时 | 转移（客户端层统一归一成 `ProviderError(502)`） | `client.py:206-208, 238-240, 277-279` |
| 400 / 404 | 不转移：请求本身的问题，换家结果相同 | `model_resolve.py:59-62` |
| 上游 401 / 403 | 不转移：凭证/权限问题，换家等于拿别人的额度试错 | `model_resolve.py:59-61` |
| 501 能力位不支持 | 不转移：换家同样不支持 | `model_resolve.py:63-64` |
| 配额 `429 quota_exceeded`、失败限流 `429 failure_rate_limited`、密钥缺失/无效 `401`、鉴权头冲突 | 不转移：都在准入段（尝试循环之前）返回，根本没进入尝试 | `v1.py:400-409` |

- 只有「已解析出多个目标」时才有备选；单候选时即使开关打开也无事发生（`_transferred` 要求 `index + 1 < total`，`v1.py:383-384`）。
- 转移后由**最终承接**的 provider 决定 `request_logs.provider` 与 `x-upstream-provider`；按 provider 对账/排障以这两处为准，不要只看请求里写的模型。

**这条为什么重要**：把 401/403/400/404/501 也当成「可转移」，会让客户端误以为重试能绕过上游凭证或能力问题（实际原样失败），还会把配置错误记成上游故障；以为显式 `x-provider` 也会漂移，会让按 provider 记账/排障的人错认承接方。

#### 错误契约

所有 provider 失败先归一成内部错误（实现在 `app/gateway_errors.py`），再**按端点协议**输出不同外壳；状态码映射三端点共用，不受外壳影响。

| 端点 | 错误外壳 |
| --- | --- |
| `/v1/chat/completions` | OpenAI：`{"error": {message, type, param, code, ...}}`，另带兼容字段 `detail` |
| `/v1/responses` | 同 Chat Completions（OpenAI 形状） |
| `/v1/messages` | Anthropic：`{"type": "error", "error": {type, message, ...}}`，**没有** `detail` |

- 框架级错误（未匹配的 `/v1/*` 路径、方法不允许）不落 FastAPI 默认 `{"detail": "Not Found"}`，同样按所属协议出壳：chat/responses 用 OpenAI 壳（`error.code = route_not_found / method_not_allowed`），messages 用 Anthropic 壳；`405` 保留 `Allow` 头。`/api/*` 的 404/405 仍是 `{"detail": ...}`，不受影响。

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
    "code": "upstream_auth_failed",
    "provider": "zcode",
    "upstream_status": 401,
    "upstream_code": "401",
    "upstream_message": "token expired"
  }
}
```

- Anthropic 的错误对象只认 `type` + `message` 两个标准键，`code`、`provider / upstream_status / upstream_code / upstream_message` 作为 `error` 的**附加键**保留；这是「按协议出形状」与「保留排查信息」之间的取舍。
- `provider`、`upstream_status`、`upstream_code`、`upstream_message` 是排查用的子字段：
  - `upstream_status` 是上游原始 HTTP 状态（含被合并的 403）；
  - `upstream_code` / `upstream_message` 由适配器从厂商错误体解析（兼容 OpenAI `error.code/message`、Anthropic `error.type/message`、`code` / `message` / `detail` 等常见键），拿不到时留空（键可能不出现），不会编造；
  - `upstream_message` 已掩码并截断到 200 字符。
- 上游状态码映射（`public_status`）：

| 上游状态 | 对外状态 | `error.type`（OpenAI / Anthropic） | `error.code`（两壳同值） |
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

网关本地拒绝同样按协议出外壳：缺密钥/密钥无效 `401 invalid_api_key`；`Authorization` 与 `x-api-key` 同时带上且值不同 `401 conflicting_auth_headers`；请求体非法 `400 invalid_json`；缺 `model` `400 missing_model`；**`x-provider` 与前缀异源** `400 conflicting_provider_hints`、**`x-provider` 指向本地没有的 provider** `404 provider_not_found`（判定顺序：本地存在性 → 冲突 → 停用）；配额耗尽 `429 quota_exceeded`（`detail` 里仍是结构化拒因，含 `window/metric/used/limit/reset_after`）；**显式指定已停用的 provider**（`x-provider` 或 `provider/model` 前缀）`503 provider_disabled`，message 直接点明该 provider 已停用——停用状态不能靠显式指定绕过（详见「模型解析失败」表）。

> 上表 `503` 行只描述**上游**返回 503（`code=provider_unavailable`）的映射；网关本地的 `provider_disabled` 503 在 Anthropic 壳下是 `type=api_error` 而非 `overloaded_error`，两种 503 的区分见「模型解析失败」表下方的说明。

模型解析失败（发生在转发之前，不写 `request_logs`、不产生流水）：

| 场景 | 状态 | OpenAI 外壳 `type` / `code` | Anthropic 外壳 `type` / `code` | 关键字段 |
| --- | --- | --- | --- | --- |
| 同名模型命中多家 | 400 | `ambiguous_model` / `ambiguous_model` | `invalid_request_error` / `ambiguous_model` | OpenAI 在 `detail.candidates`、Anthropic 在 `error.candidates` 给 `[{provider, readiness}]` |
| 没有已启用 provider 提供 | 404 | `model_not_found` / `model_not_found` | `not_found_error` / `model_not_found` | `detail.model` 回显，`candidates` 为空 |
| 只存在于未启用 provider | 404 | `model_unavailable` / `model_unavailable` | `not_found_error` / `model_unavailable` | `candidates` 含 `provider/readiness/enabled/requires/reason`，不含凭据与 `base_url` |
| 显式指定已停用的 provider | 503 | `api_error` / `provider_disabled` | `api_error` / `provider_disabled` | `detail.provider` 回显 provider id；message 说明「已停用，请先在服务商页启用」 |

`code` 是 Anthropic 错误对象的固定附加键——**所有** `GatewayError` 都带（与 OpenAI 壳同值，两协议字段对称）；`candidates` 只在模型解析失败时由 `ModelResolutionError` 附加；其余 Anthropic 错误还有 `provider/upstream_*` 等排查键。

**503 要按 `code` 分两种处理**（`gateway_errors.py:184-190` 的 `anthropic_payload()`）：

- **`code=provider_disabled`**（管理员停用了 provider）：Anthropic 壳 `error.type=api_error`。这是配置问题，重试无用，客户端应提示用户/管理员换模型或启用 provider；
- **上游真过载 / `provider_unavailable`**：Anthropic 壳 `error.type=overloaded_error`，保留可重试语义。

同一状态码（503）下两种 `type` 并存，**客户端判断是否重试要看 `error.code`（`provider_disabled` 不重试、`provider_unavailable` 可重试），不要按 `error.type` 判断**。OpenAI 壳（`/v1/chat/completions`、`/v1/responses`）不受影响，两者 `type` 均为 `api_error`，靠 `code` 区分。

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

OpenAI Responses 协议转发，鉴权、模型解析、日志、错误码与 `/v1/chat/completions` 一致（含 OpenAI `error` 外壳），能力位换成 `responses`（代码里同走 `_proxy`）。目前 `codar`、`qoder`、`qoder_sidecar` 三家声明了该能力位，其余 provider 返回 501（`codar_sidecar` 不声明，仍 501）。

> 2026-11-04 更正（原句保留作历史）：本段原记「目前只有 `codar` 与 `qoder` 声明了该能力位，其余 provider 返回 501」，与代码不符——`apps/control/app/adapters/qoder_sidecar.py:32` 声明 `"responses": True`、`:55-57` 实现转发、`apps/control/app/routers/v1.py:479-481` 已挂 `/v1/responses`，故补上 `qoder_sidecar`。本节其余内容（鉴权、模型解析、错误外壳、501 语义）不变；`codar_sidecar` 仍不声明该能力位（`codar_sidecar.py:8`），返回 501 属实。

### `GET /v1/models`

| 项 | 说明 |
| --- | --- |
| 鉴权 | 网关密钥（或匿名开关打开） |
| 逻辑 | 遍历已启用且 `capabilities.models` 为真的 provider，合并各自 `list_models()` 结果 |

```json
{"object": "list", "data": [{"id": "gpt-4o-mini", "object": "model", "owned_by": "opencode"}]}
```

某 provider 拉模型失败时静默跳过（不报错），所以空列表可能意味着「没有可用 provider」也可能是「上游都不通」。

#### `/v1` 扩展端点（10 条，`/v1` 路由总数 14）

除上文 4 条（`chat/completions`、`messages`、`responses`、`models`）外，另有 10 条扩展路由（装饰器位置 `v1.py:672-720`），鉴权、配额、失败限流、日志与主端点同一套。扩展能力位集合为 `EXTENSION_CAPABILITIES = {embeddings, images, videos, media, count_tokens, rerank}`（`v1.py:44-47`）。

| 方法与路径 | 能力位 | 转发方法 | 说明 |
| --- | --- | --- | --- |
| `POST /v1/embeddings` | `embeddings` | `embeddings` | 请求体原样转发 |
| `POST /v1/images/generations` | `images` | `image_generation` | 请求体原样转发 |
| `POST /v1/images/edits` | `images` | `image_edit` | 只收 JSON；`multipart/*` → 415 `unsupported_media_type` |
| `GET /v1/images/tasks/{task_id}` | `images` | `image_task` | 走 `_proxy_get` |
| `POST /v1/videos` | `videos` | `video_create` | 请求体原样转发 |
| `GET /v1/videos/{task_id}` | `videos` | `video_task` | 走 `_proxy_get` |
| `GET /v1/media/{name}` | `media` | `media` | 走 `_proxy_get` |
| `POST /v1/messages/count_tokens` | `count_tokens` | `count_tokens` | Anthropic 计数端点；错误外壳按路径前缀走 `messages` 协议（`gateway_errors.py:108-112`） |
| `POST /v1/rerank` | `rerank` | `rerank` | 请求体原样转发 |
| `GET /v1/usage` | `usage` | `usage` | 查询串原样透传（`v1.py:717-720`） |

行为口径：

- **强制非流式**：扩展 POST 端点丢弃请求体里的 `stream` 并置 `stream=False`（`v1.py:410-412`），既不透传给上游，也不返回 SSE。
- **请求体**：只接受 JSON；`multipart/*` 返回 415 `unsupported_media_type`，非法 JSON 或非对象返回 400 `invalid_json`（`v1.py:280-296`）。
- **计费口径**：扩展能力位的空/全零 usage 经 `_usage_for_ledger` 归成 `None`（`v1.py:273-277`），结算仍会写一条 `note=usage_missing`、0 金额的流水（`billing.py:321-323`）；日志侧因此是 `credit_status=usage_missing`、`credit=null`（`logs.py:155-162`），**不是** `billed(0)`。`usage` 不在 `EXTENSION_CAPABILITIES` 内，但 `/v1/usage` 走 GET 代理，同样不采用量。
- **`_proxy_get`（4 条 GET，`v1.py:584-636`）**：没有 `model` 可解析，按能力位挑 provider（`_pick_capable`，`v1.py:551-581`）：零家支持 501 `capability_unsupported`；多家支持且未带 `x-provider` 400 `ambiguous_provider`；`x-provider` 指向未知/停用 provider 404 `provider_not_found`，指向不支持该能力位的已启用 provider 501。家级能力位为真但适配器没实现读取方法（如只有建视频任务、没有查询）同样 501 `capability_unsupported`（`v1.py:590-597`）。
- **配额与限流维度**：GET 端点没有 model，`_quota_guard` / `_failure_guard` 按 provider 维度判（model 传空串），model 级配额策略不适用（`v1.py:598-600`）。
- **落账**：GET 端点无论成功失败都写 `request_logs`（`model=""`、`usage=None`），成功时同样落 `usage_missing` 零金额流水（`v1.py:607-631`）；成功响应只透传上游 `content-type`，**不**带 `x-upstream-provider`（`v1.py:632-636`）。
- **上游错误**：GET 端点的上游 4xx/5xx 先落日志，再按统一映射出错误壳（`v1.py:616-626`），与 POST 端点一致。

**这条为什么重要**：漏记会让人以为这些能力只能直连上游，从而绕开密钥、配额与请求日志；把扩展端点的空/全零 usage 当成 `billed(0)` 会让账少记一笔「有请求但无法计价」，也让 usage_missing 告警口径失效；把 GET 读取端点当可流式或可计费，会误设客户端超时与对账口径。

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

### `POST /api/auth/register`（公开，受开关控制）

| 项 | 说明 |
| --- | --- |
| 开关 | `allow_registration` 关闭 → 403 `注册未开启，请联系管理员`（`auth.py:245-247`） |
| 用户名 | 3–32 位字母、数字、点、下划线、连字符（`auth.py:249-250`） |
| 密码 | ≥ 8 字符（`auth.py:251-252`） |
| 邮箱 | 可选，给了就校验格式（`auth.py:253-255`） |
| 角色 | 落库固定 `user`（`auth.py:263`；非 admin，管理员权限面不放行） |
| 冲突 | 用户名或邮箱已存在 → 409，`detail` 区分「邮箱已存在 / 用户名已存在」（`auth.py:266-274`） |
| 响应 | `{"ok": true, "username": "...", "role": "user"}`，写审计 `register`（`auth.py:279-282`） |

> **2026-11-05 已更正（原句保留作历史；依据当前工作树，未提交）**
>
> 上表「角色」与响应示例里的 `role` 原记 `user`；当前注册落库与响应都改为 **`viewer`**（`auth.py:263`、`auth.py:282`），权限行为不变：`admin_user` 只判 `role != "admin"`（`security.py:102-104`），`viewer` 与原先的 `user` 完全等价。历史 `role='user'` 行在启动时幂等映射为 `viewer`，只匹配 `user`、不碰 `admin`（`db.py:317-318`）。
>
> **这条为什么重要**：照旧断言 `role == "user"` 的客户端或测试会开始失败；把 `user` 当成第三种权限等级，会误判它与 `viewer` 有差别——实际分界只有 admin 与非 admin。

### `POST /api/auth/refresh`（登录）

续期当前会话：`Depends(current_user)`（`auth.py:286`），Cookie 缺失或无效 401（`auth.py:288-290`）。

- **同表轮换**：先 `DELETE` 当前 token，再在**同一张 `sessions` 表**签发新 token（`auth.py:294-295`）——不引入第二套 token 体系。
- 旧 token 立即失效，客户端收到响应后必须改用新 Cookie（响应会重种 `u2a_session`，`auth.py:299`）。
- 写审计 `session_refresh`（`auth.py:296`）；响应 `{ok, username, role, expires_in}`（`auth.py:300-305`）。

### 两步验证（TOTP，`/api/auth/totp/*`，全部登录）

五个端点都 `Depends(current_user)`，未登录 401：

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/auth/totp/status` | `{enabled, recovery_codes_left, period}`；`recovery_codes_left` 只数 `used_at IS NULL` 的恢复码（`auth.py:308-325`） |
| `POST /api/auth/totp/setup` | 生成待确认密钥；返回 `{secret, otpauth_uri, algorithm: "SHA1", digits: 6, period}`；已开启返回 400「请先关闭」，重复调用覆盖 pending；写审计 `totp_setup`（`auth.py:328-351`） |
| `POST /api/auth/totp/enable` | body `{code}`；必须先 setup 否则 400；用 pending 密钥校验（`auth.py:364-372`）；成功后写 `totp_secret`、`totp_enabled=1`、`totp_last_counter`，签发 8 枚恢复码并**只在此次返回明文**（`auth.py:373-392`） |
| `POST /api/auth/totp/disable` | body `{password, code}`；必须密码正确 + 验证码有效（`auth.py:406-418`）；密码错给 **400** 而非 401（避免前端 401 拦截器把人踢回登录页，`auth.py:408-410`）；成功后清零 `totp_last_counter` 并删恢复码（`auth.py:419-424`） |
| `POST /api/auth/totp/verify` | body `{challenge, code?, recovery_code?}`；登录第二因子，成功后删 challenge、签发会话并种 Cookie（`auth.py:449-510`） |

登录侧行为（`auth.py:197-239`）：

- 账号已开 2FA 时，**密码正确也不发 token/Cookie**，只返回 `{"ok": false, "need_totp": true, "challenge": "...", "expires_in": 300}`（`auth.py:223-232`；`CHALLENGE_TTL_SEC = 300` 在 `auth.py:46`）。
- `POST /api/auth/totp/verify` 用 `challenge` 换会话：不存在或过期 401 并删过期行（`auth.py:454-463`）；账号未开 2FA 400（`auth.py:466-467`）。**失败累计 5 次作废**：第 5 次失败直接删 challenge，之后只能重新登录（`auth.py:469-479`；`CHALLENGE_MAX_ATTEMPTS = 5` 在 `auth.py:47`）。
- **同窗重放拒绝**：`users.totp_last_counter` 单调递增，命中的 counter `<=` 已记录值即判 `replay`，返回「验证码已使用，请等待下一个时间窗」（`totp.py:49-71`；启用时写初值 `auth.py:379-383`，验证成功推进 `auth.py:500-503`）。校验接受当前窗及前/后各一格（`totp.py:20-23, 60-66`），容差只用于时钟漂移，不放宽重放判定。
- **恢复码**：一次性、共 8 枚、12 位十六进制（48 bit 熵）、沿用口令哈希参数（PBKDF2），用后置 `used_at` 且并发只允许一条更新成功（`totp.py:24, 83-94`；`auth.py:432-446`）。恢复码登录同样把 `totp_last_counter` 推进到当前窗，避免同窗 TOTP 码再用（`auth.py:481-489`）。

**这条为什么重要**：漏记 `need_totp` 分支，对接方会以为登录成功却拿不到 Cookie，误判成会话 bug；漏记 `totp_last_counter` 与 5 次作废，会以为同一验证码可重复使用、也会低估爆破防护；漏记恢复码一次性，会把「同一枚恢复码反复登录」当成设计行为。

### `POST /api/auth/start`（管理员）

发起扫码登录：走第一个「已启用且 `capabilities.oauth` 为真」的适配器（当前是 CodeBuddy 管理端），路由只做参数与错误翻译，不直连上游。

| 请求体 | 类型 | 说明 |
| --- | --- | --- |
| `realm` | string? | `global` 或其他值；只有精确的 `global`（忽略大小写/首尾空格）走全球区，其余归一为 `cn` |

- 成功：`{"state": "...", "authUrl": "...", "realm": "cn|global"}`。`state` 留给 `/api/auth/poll`，`authUrl` 用来生成二维码。
- 没有已启用的扫码服务商 → **503** `扫码登录不可用：没有已启用的扫码服务商，请先在「服务商」页启用 CodeBuddy 并配置上游地址`。
- 上游没回 `state` 或 `authUrl` → **502** `上游未返回二维码链接，请确认 CodeBuddy 管理端（7864）已就绪`。
- viewer 调用 403（`admin_user`）。

### `GET /api/auth/poll?state=&realm=&region=`（管理员）

轮询扫码结果。`state` 缺省空串：直接返回 `{"status": "invalid"}`（与上游口径一致，不算错误）。给了 `state` 则把 `state`/`realm`/`region` 透传给适配器，原样返回其轮询 JSON（`status` 取值由上游定义，如待扫/已扫/成功/过期）。`realm` 归一规则同上，`region` 原样透传。同样：无扫码服务商 503、viewer 403。

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
| `risk_level` / `risk_note` | `safe` / `warn` / `danger` 及说明。分级按**具体接入路径**标注，不是整家禁用：`opencode`（GPL-3.0）、`sub2api`（LGPL-3.0）为 `warn`；`qoder` 直连为 `safe`（`risk_note` 明说 PolyForm-NC 的非商用限制针对的是 `qoder_sidecar` 而非直连路径），`qoder_sidecar` 为 `danger`（PolyForm-NC 禁商用，作者书面授权前不得商用） |
| `limitations` | `dict<能力位, 说明>`：能力受限声明，同一句与网关对应场景的 501 报错文案同源（共用常量）。现仅 `deepseek_sidecar` 有值：`messages_stream` → 「Deepseek2Go sidecar 只支持非流式 messages」 |
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

### 凭据库（`/api/provider-credentials`，读登录 / 写管理员）

同一 provider 可存多行凭据（多账号 / 多 base_url）。密钥只落库、只在服务端流转：响应一律 `secret_masked`（掩码）+ `has_secret`（布尔），审计行只记改动过的**字段名**，绝不记值。

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /api/provider-credentials?provider=` | 登录 | 掩码列表，可按 provider 过滤；每项 `{id, provider, label, base_url, secret_masked, has_secret, enabled, sort_order, note, created_at, updated_at}` |
| `POST /api/provider-credentials` | 管理员 | 新建；body `{provider, label?, base_url?, secret?, enabled?, sort_order?, note?}`；`secret` 空串 = 暂不填（如无需鉴权的 sidecar）。`base_url` 走与 `PATCH /api/providers/{provider_id}` 同一守卫（非 http(s)、私网/链路本地/云元数据 → 400，文案含「这家是否允许本地地址」） |
| `PATCH /api/provider-credentials/{credential_id}` | 管理员 | 部分更新。`secret`：`"***"` = 保持不变，空串 = 清除，其他 = 覆盖；`base_url` 同守卫 |
| `DELETE /api/provider-credentials/{credential_id}` | 管理员 | 返回 `{"ok": true, "id": N}`；不存在 404 |

写操作成功写审计（`provider_credentials.create/update/delete`，`target` 形如 `codebuddy#3`）；上游拒绝（ProviderError）按其状态码与中文文案透传，例如 400「…元数据…」。不存在的 `credential_id` → 404。

---

## 4. 账号（`/api/accounts`）

账号来自各 provider 的上游账号池，列表接口顺带同步（每个 provider 30 秒节流，强制刷新用 `/sync`）。

### `GET /api/accounts?provider=&realm=`（登录，按归属收窄）

权限口径（2026-11-04 收紧）：`admin` 返回全量账号；非 `admin` 只返回 `owner_user_id` 等于自己用户名的行。`accounts` 表同日补 `owner_user_id` 列，`NULL` 表示无主/共享池行，对非 admin 不可见；因此 dashboard 等既有调用方仍是 200，只是数据面收窄，不产生新的 403。

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

### `GET /api/accounts/capabilities`（登录）

每家 provider 的账号管理能力清单，前端据此决定画哪些按钮、哪些家只给「账号在哪里管理」的说明。能力以**适配器实际覆写**为准而不是 spec 声明位（`trae_sidecar` 声明了 oauth/credits 但方法未覆写，按声明画按钮点下去就是 501）。

返回 `{"providers": [{id, label, enabled, status, readiness, requires, managed, ops}]}`：

- `ops`：`{list, oauth, checkin, credits, test, refresh, clear_cooling, disabled, delete, note, restart}` 十一个布尔位；`note` 恒为 `true`（备注只落本端库，不依赖上游）。
- `managed`：`unified`（本页管理）/ `sidecar`（在自身控制台管理）/ `quota`（sub2api，配额域）/ `external`（未接入）。

### `POST /api/accounts/sync?provider=`（管理员）

绕过节流强制同步。返回 `{provider_id: {synced, pool_available, note}}`；不传 `provider` 则同步全部已启用 provider。

### `POST /api/accounts/refresh-credits?force=false`（管理员）

仅作用于 codebuddy（内部固定取 codebuddy 适配器）。返回其管理端的响应（`{total, succeeded, credits, meta, failed, ...}`）；CodeBuddy 不可达时返回带 `message` 的零值结构而不抛 500。

### `POST /api/accounts/checkin-all`（管理员）

仅 codebuddy：全部账号签到。返回 `{total, succeeded, already, skipped, results, ...}` 或带 `message` 的零值结构。

### `POST /api/accounts/import`（管理员）

贴凭证式登记一条账号；`Depends(admin_user)`（`accounts.py:359-360`）。

请求体（`accounts.py:75-82`）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `provider` | string | 必填，必须已注册（`accounts.py:131-136`） |
| `label` / `name` | string | 二选一，截 64 字符（`accounts.py:165`） |
| `credential_type` | string | `api_key` / `bearer` / `cookie` / `json` / `env` 五选一（`accounts.py:68, 146-152`） |
| `credential` | string 或 object | `json` 类型可传对象/数组，其余必须字符串；空值 400；上限 4096 字符（`accounts.py:100-126`） |
| `base_url` | string | 可选，走与统一凭据库同一道地址守卫（`accounts.py:153-158`） |
| `note` | string | 截 200 字符（`accounts.py:166`） |

- `json` 凭证先 `json.loads` 语法校验，错误定位到行/列（`accounts.py:111-117`）。
- `sidecar` / `demo` 账号拒收（前者由 sidecar 自管，后者是种子数据），400 并说明原因（`accounts.py:139-145`）。
- 凭据写入统一凭据库（`registry.create_credential`，参与转发轮换），账号行进本端 `accounts` 表并标 `imported` + `credential_id`（`accounts.py:369-403`），`owner_user_id` 写当前用户名（`accounts.py:405`）。
- 响应与 `GET /api/accounts` 单条同构，**凭据不回显明文**（`accounts.py:93-97, 417-420`）；审计 `accounts.import`，detail 只记 `credential_type`（`accounts.py:408-416`）。

### `POST /api/accounts/import/probe`（管理员）

保存前试连通，请求体同上（`accounts.py:423-429`）。临时适配器 = 浅拷贝 + 重建 options，**探测不落库、不改进程内共享实例**（`accounts.py:430-435`）。

- 优先调 `health()`；适配器没有则调 `fetch_models()`；两者都没有则 `status=unsupported`（`accounts.py:436-453`）。
- 返回 `{status: "ok"|"fail"|"unsupported", connected, message, upstream_status}`；`message` 过 `mask_text` 并截 300 字符（`accounts.py:460-465`）。
- 探测失败**只报告、不阻断保存**；响应不含凭据明文（`accounts.py:455-459, 463`）。

**这条为什么重要**：漏记会让人以为只能扫码/OAuth 加账号；把 probe 当强校验会误以为探测失败就不能保存（实际只是报告）；以为 probe 会回显凭据会误判泄露面。

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
| `provider_states` | 每个归属 provider 的 `{source, readiness, requires, limitations, enabled, available, unavailable_reason, fallback_reason}`；`limitations` 同第 3 节（`dict<能力位, 说明>`，现仅 `deepseek_sidecar` 的 `messages_stream`） |
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

### 访问令牌（`/api/tokens`）：同一张 `api_keys` 表的面板视图

前端 TokensPanel 要的是 `ApiToken` 结构，这里对 `api_keys` 行做字段映射，**不另建存储**：创建/改名/启停/删除都与 `/api/keys` 作用于同一批密钥，两边改动互见。

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /api/tokens` | 登录 | 列表；admin 全量（`created_at` 倒序），非 admin 只看 `owner_user_id = 自己` 的行，NULL 归属不可见 |
| `POST /api/tokens` | 管理员 | 创建，明文只返回一次 |
| `PATCH /api/tokens/{token_id}` | 管理员 | 改名 / 启停；不存在 404 |
| `DELETE /api/tokens/{token_id}` | 管理员 | 物理删除即吊销（哈希没了，该 `u2a_` 立刻过不了 `/v1` 鉴权）；不存在 404 |

行结构（缺失字段如实置空，不编造）：`{id, name, prefix, scope: "readonly", enabled, expires_at: null, created_at, created_by: "", last_used_at, last_used_ip: null}`。

- `scope` 恒为 `readonly`：`u2a_` 是下游调模型的网关凭据，不具备管理接口权限，不能标成 `admin`。
- `POST` body：`{name, scope?, expires_at?}`；`name` 1–64 字符否则 400；传了非 `readonly` 的 `scope` → 400 `网关密钥不支持 admin 作用域，这里只提供只读令牌`；传非空 `expires_at` → 400 `网关密钥不支持到期时间，创建后长期有效，需停用请直接禁用或删除`（不静默降级成「永不过期」）。响应 = 行结构 + `"token"`（明文，仅此一次）。
- `PATCH` body：`{name?, enabled?, scope?, expires_at?}`，`name`/`enabled` 都缺 → 400 `没有需要更新的字段`；`scope`/`expires_at` 同样按上面两条 400 拒绝。

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

### `GET /api/settings/upstream`（管理员）

返回上游配置（存于 SQLite `settings` 表的 `upstream_config`），保证包含十组空兜底：`schedule / prompt / cooldown / pool / features / session_sticky / server / upstream / global / admin`，另有 `upstash: {url, has_token, token_masked}`（Upstash 未接入，仅占位）。

### `POST /api/settings/upstream`（管理员）

请求体为任意 dict，与存量深合并保存（`available / config_path / reload_hint / push` 四个只读键会被剔除）。若 codebuddy 已启用，会把十组配置真实 POST 到其管理端 `/api/settings/upstream`，结果回显在响应的 `push.codebuddy` 字段（`ok` + `reload_scheduled`/`reload_hint` 或 `message`）；未启用则 `push.codebuddy.ok = false` 且配置仅存本地。

### `GET /api/settings/gateway`（管理员）

`{"allow_anonymous": false, "configured": false}`。`allow_anonymous` 显示实际生效值（DB 配置优先，否则环境变量）；`configured` 表示 DB 里是否配过。

### `POST /api/settings/gateway`（管理员）

请求体 `{"allow_anonymous": true|false}`，写入 DB（从此优先于环境变量）。返回 `{"allow_anonymous": ..., "configured": true}`。

### `GET /api/settings/model-map`（管理员） / `POST /api/settings/model-map`（管理员）

模型别名表（dict：`别名 → 目标模型`，目标可带 `provider/` 前缀）。POST 会剔除键值为空的项并返回规范化后的表。

### `POST /api/settings/upstash/test`（管理员）

真实连通性探测（只读）：向 `{url}/pipeline` 下发 `[["PING"]]`，带 `Authorization: Bearer {token}`；不写数据、不落库、不改配置。出站 `httpx` `trust_env=False`、超时 10s；输入校验在网络请求之前完成，非法输入零出站。请求体 `{url, token}` 与前端保持兼容，`token` 留空回退已保存的值。

SSRF 加固：建连前先做主机名/字面量红线（内网、环回、链路本地、保留地址、云元数据）校验，域名还会先 DNS 解析、任一解析结果落入红线即整体拒绝——两条路径都**不产生任何出站连接**，报 `upstash_target_forbidden`；公网不可达（含 DNS 解析失败）报 `upstash_unreachable`。上游 3xx 明确不跟随（`follow_redirects=False`），响应体按解码后字节计数、超 64KB 中止读取（防 gzip 放大）。

| 结果 | HTTP | `error.type` | `error.code` |
| --- | --- | --- | --- |
| 成功 | 200 | —（`{ok, verified, latency_ms, detail, message}`） | — |
| 凭据被拒（上游 401/403） | 400 | `authentication_error` | `upstash_auth_rejected` |
| 超时（10s） | 504 | `upstream_error` | `upstash_timeout` |
| 连不上（连接失败或 DNS 解析失败） | 502 | `upstream_error` | `upstash_unreachable` |
| 地址非法 | 400 | `invalid_config` | `invalid_upstash_url` |
| token 缺失 | 400 | `invalid_config` | `upstash_token_missing` |
| 目标是内网/环回/链路本地/保留地址/云元数据（字面量，或 DNS 解析后任一结果命中） | 400 | `invalid_config` | `upstash_target_forbidden` |
| 上游 3xx 重定向（不跟随） | 502 | `upstream_error` | `upstash_redirect_blocked` |
| 响应体超 64KB（解码后字节） | 502 | `upstream_error` | `upstash_response_too_large` |
| 其余上游 4xx | 400 | `invalid_config` | `upstash_rejected` |
| 上游 5xx | 502 | `upstream_error` | `upstash_unavailable` |

错误体是控制面风格 `{ok:false, detail, error:{type, code, message}}`，**不是** `/v1` 协议壳。表内共 10 个业务错误码（上表 = 1 行成功 + 10 行错误码）；403 `insufficient_role` 是 `admin_user` 守卫在进入探测逻辑前由统一 403 处理器（`main.py`）产生的权限错误，**不是** upstash 业务错误码，不列入本表。

### 运行参数（runtime-config，四个端点均管理员）

四个端点全部 `admin_user`（`apps/control/app/routers/settings.py:376-431`，viewer 返回 403 `insufficient_role`）。可写项以代码里的 `RUNTIME_ITEMS` 白名单为准：`models_cache_ttl_sec`、`failure_window_sec`、`failure_window_max`（整数，0–86400 / 0–86400 / 0–100000）、`model_fallback_first`（布尔）、`sse_max_seconds`（1–86400）、`session_ttl_sec`（60–2592000）、`allow_anonymous_gateway`（布尔，存网关鉴权键）。

#### `GET /api/settings/runtime-config`（管理员）

```json
{
  "ok": true,
  "items": [
    {"key": "models_cache_ttl_sec", "type": "int", "value": 300, "default": 300,
     "source": "default", "min": 0, "max": 86400, "ui": true}
  ],
  "excluded_secrets": [
    "provider_credentials.secret",
    "providers 表与 provider_config 中的 api_key / token / secret / password",
    "upstream_config 中的密钥字段",
    "环境变量里的密钥（CODEBUDDY_API_KEY 等）"
  ]
}
```

`source` 取值 `default`（未落库）/ `db`（已落库且合法）/ `invalid`（库里值不合法，回退默认值）。`excluded_secrets` 是 `SECRET_EXCLUSIONS`（`settings.py:138-143`）——刻意不允许从本接口读写的密钥类配置说明。

#### `POST /api/settings/runtime-config`（管理员）

请求体为 `{"items": {key: value, ...}}`（也接受直接传扁平 `{key: value, ...}`）。行为（`_write_runtime`，`settings.py:287-373`）：

- **先全量校验**：任一项不合法即整体拒绝，返回 400 `{"ok": false, "error": {"type": "invalid_config", "message", "errors": [{key, value, reason, message, min?, max?}]}}`，`reason` 可能是 `empty_payload`（空 body）、`unknown_key`（白名单外）、`secret_not_allowed`（密钥类，提示改走凭据体系）、`invalid_boolean` / `not_an_integer` / `out_of_range`。
- 校验全过后**单事务写入**（`put_settings` + `commit`），写库异常 `rollback` 并返回 500 `{"ok": false, "error": {"type": "write_failed", "message": "写入数据库失败，本次改动已整体回滚", "errors": []}}`——不会留下半套配置。
- 落库成功后才更新内存 settings（下一次请求即生效，无需重启），并写 `audit_logs`（action `settings.runtime_config`）。
- 成功响应 = `GET` 的完整 payload + `"saved": ["已写入的键"]`。

#### `GET /api/settings/runtime-config/export`（管理员）

```json
{
  "kind": "unified-2api-runtime-config",
  "version": 1,
  "exported_at": 1793700000,
  "items": {"models_cache_ttl_sec": 300},
  "excluded_secrets": ["..."],
  "note": "只含非密钥运行参数；凭据请在「服务商」页按 provider 管理。"
}
```

只导出非密钥运行参数；`kind` / `version` 为导出文件的机器标识（字符串原样，勿改）。

#### `POST /api/settings/runtime-config/import`（管理员）

请求体接受两种形态：完整导出文件（含 `kind`/`version`/`items`），或扁平 `{key: value, ...}`。校验顺序：

1. body 必须是 JSON 对象，否则 400 `invalid_shape`；
2. `kind` 存在且 ≠ `unified-2api-runtime-config` → 400 `kind_mismatch`；
3. `version` 存在且 ≠ 1 → 400 `unsupported_version`；
4. 其余字段剥离元数据键（`kind/version/exported_at/excluded_secrets/note`）后按 POST 同一套 `_write_runtime` **先全量校验、再单事务写入、异常 rollback**（400 `invalid_config` / 500 `write_failed` 语义同上）。

成功响应 = 完整 payload + `"saved"` + `"imported": ["参与本次导入的键"]`。导入同样写 `audit_logs`（action `settings.runtime_config.import`）。

> 注：导出/导入路径是 `/api/settings/runtime-config/export|import`（挂在 runtime-config 子路径下），不是 `/api/settings/export|import`。

### 明文密钥查看：`POST /api/settings/secret/reveal`（管理员）

唯一会回明文 secret 的控制面端点，刻意不给前端入口（明文不下发浏览器，安全取向，见 `unification-audit` 附录）。请求体 `{"path": "..."}`，点号分隔的配置路径。

- 根段必须是 `provider_config` 或 `upstream` 组内的密钥，否则 400 `只能查看 provider_config / upstream 组内的密钥`；路径不存在 404；目标字段不是非空字符串 400 `目标字段不是密钥字符串`。
- 成功：`{"path": "...", "value": "明文"}`。
- 成功与失败都写 `audit_logs`（action `settings.reveal_secret` / `settings.reveal_secret.{bad_root|path_not_found|not_a_secret}`，`target` 记配置路径），审计行不含明文值。
- viewer 调用 403。

### 用户管理

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /api/users` | 管理员 | `[{"username", "role"}]`；与本组写操作同一口径（2026-11-04 收紧，此前为登录可读），非 admin 403 `insufficient_role` |
| `POST /api/users` | 管理员 | 创建。用户名 3–32 字符，密码 ≥6；角色 `admin`/`viewer`（默认 viewer）；重名 409 |
| `PATCH /api/users/{username}` | 管理员 | 改密码和/或角色；改密码会吊销该用户全部会话；不能把最后一个 admin 降级（400） |
| `DELETE /api/users/{username}` | 管理员 | 不能删自己、不能删最后一个 admin；删除会吊销其会话 |

PATCH 响应：`{"username", "role", "sessions_revoked", "relogin_required"}`（自己改自己的密码时 `relogin_required: true`）。

### 系统信息（登录）

- `GET /api/system/check-update`：恒返回「自更新通道未启用」的固定结构。
- `GET /api/system/versions`：`{"manager": "unified-2api M2", "upstream_connected": false, ...}` 固定结构。
- `GET /api/system/changelog`：解析根目录 `CHANGELOG.md`，返回 `{available: true, path: "CHANGELOG.md", total, versions: [...], truncated, current}`；`versions` 每项 `{version, date, unreleased, sections: [{title, items: [{level, text}]}]}`（`## 版本` / `### 分类` / `- 条目`）。文件缺失或不可读返回 `available: false` + `error` + 空 `versions`，不 500。
- `GET /api/system/update-status`：与 `check-update` 同源的固定「无更新」结构 + 面板所需字段：`{available: false, current_version, reason: "本部署不提供在线更新", version, running: false, ok: null, step: "", logs: [], updater_found: false, upstream_dir: "", can_update_upstream: false, in_container, upstream_ref: ""}`。

> **（2026-11-05 已更正）** `GET /api/system/check-update` 已于 2026-11-05 下线：`apps/control/app/routers/system.py` 不再注册该路由，旧投影函数 `check_update_legacy` 也从 `apps/control/app/routers/update.py` 删除，请求它现在直接 404。`/api/system/update-status` 不再「与 `check-update` 同源」，它由 `apps/control/app/routers/update.py` 的 `status_payload` 取 `app.state.update_feed.summary()`，与已删除的旧壳没有共享结构。
>
> **读旧响应会得到的误判**：按原文继续请求 `check-update`，会把 404 当成鉴权失效或发布源临时故障去排查，实际是端点已不存在；按「两套外形共用一份探测逻辑」去找旧路径，会误以为 `update_feed` 之外还有一条外呼链路，而默认 `UNIFIED_UPDATE_ENABLED=false` 时 `update-status` 一个外网请求都不发。原文列出的固定字段（`version`、`running`、`step`、`logs`、`upstream_ref` 等）也不是当前返回，照旧键取值只会得到缺键，而不是「无更新」。

### 备份与恢复（`/api/system/*`，三个端点均管理员）

路由前缀 `/api/system`（`backup.py:36`）；保留份数 `BACKUP_KEEP = 14`（`backup.py:38`）。

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/system/backups` | `{backups: [{name, size_bytes, created_at, path, has_checksum}], count, dir}`；`path` 过 `mask_text`（`backup.py:164-188`） |
| `POST /api/system/backup` | 立即备份；跨进程锁；返回 `{ok, name, size_bytes, duration_ms, path, sha256, created_at}`（`backup.py:191-219`） |
| `POST /api/system/restore` | body `{name, confirm}`；`confirm` 默认 `false`，缺省 400；`name` 必须命中 `ARCHIVE_RE` 白名单（`backup.py:222-234`） |

恢复流程（`backup.py:240-287`）：

1. 取跨进程锁，键 `restore:<name>`；占锁失败 409（`backup.py:241, 69-90`，409 在 83 行）。
2. `core.load_archive` 校验归档：zip + manifest + 成员 sha256 + `PRAGMA integrity_check`（`backup.py:243`）。
3. 恢复前**自动给当前库出一份完整快照**，名字随响应返回（`backup.py:261, 298-306`）。
4. `_replace_with` 用临时文件 + `os.replace` 原子替换（`backup.py:331-345`，`os.replace` 在 339 行）。
5. `core.selfcheck`；不过则写回字节快照回滚，库不处于半写状态（`backup.py:350-362`）。
6. 写审计（`backup.py:270-276`）；返回 `{ok, restored, path, pre_restore_backup, pre_restore_path, duration_ms, selfcheck_ok, rows_after}`（`backup.py:278-287`）。

> **2026-11-05 已更正（原句保留作历史；`backup.py` 已不是原句描述的实现，以当前工作树为准）**
>
> 第 4、5 步原记「`_replace_with` 用临时文件 + `os.replace` 原子替换（339 行）、`core.selfcheck` 失败写回字节快照」；当前恢复改为 **SQLite 在线备份**：`_apply_archive` 调 `_backup_into` 把归档库逐页备份进活库（`backup.py:382-390`、`341-362`），全程不换文件；`os.replace` 只出现在注释里，用来说明「目标文件被占用时换文件必失败」（`backup.py:344-345`）。写入失败由 `_rollback_db` 反向在线备份回滚（`backup.py:400-407`）。
>
> 自检位置也换了：应用**前**先 `_verify_archive` 调 `core.selfcheck` 比对归档库行数（`backup.py:315-325`，调用点在 260）；应用**后**再 `_structure_check`（`backup.py:410-467`：`PRAGMA integrity_check` 434、`foreign_key_check` 435、只增不减行数下限 424-428、admin 至少 1 个 429-433），不过同样回滚。
>
> **这条为什么重要**：以为恢复是「换文件」会去查 `os.replace`/临时文件路径，排障时找不到实际代码；以为自检只在事后做，会漏掉应用前的 `selfcheck` 已经把半截归档拦在写入之前。
>
> **2026-11-05 已更正（补充：在线恢复的能力边界；上一块保留）**
>
> - **应用前失败 → 字节不动**：`load_archive` 归档校验（`backup.py:243`）、`_verify_archive` 里的 `core.selfcheck`（`315-325`，调用点 260）、`_restore_pre_snapshot` 的恢复前归档（`298-306`，调用点 261）、`_snapshot_live` 的回滚快照（`328-338`，调用点 262）都排在 `_apply_archive`（263）之前；任一步失败即中止，活库与它的 `-wal`/`-shm` 一个字节都不动。用例按字节断言：`apps/control/tests/test_backup_api.py:382-416`。
> - **应用中途失败 → 内容一致，字节不再相同**：`_backup_into`（`341-362`，`src.backup` 在 359，30s 超时在 347）中断，或应用后 `_structure_check`（`410-467`）不过，都走 `_rollback_db` 的反向在线备份 + `_ensure_wal`（`400-407`、`365-379`）。回滚后 integrity / 结构 / 只增表下限都对得上，但库文件字节已不是恢复前的样子；一致性快照（262）之后、回滚之前的并发写会丢失，窗口等于单轮恢复时长（实测 231–390 ms，`tmp/quiesce-probe/results_load56-fix.json`、`results_idle-fix.json`，`tmp/` 已 gitignore）。
> - **要做到中途也字节级还原，只能回到「停服务 + 换文件」**：CLI 路径 `scripts/restore.sh` → `scripts/backup_core.py` 仍是这一套（`backup_core.py:618-657` 留存现场、`712-725` 回滚），脚本头也仍写「恢复前必须停掉 control 服务」（`restore.sh:4`）。这是在线备份的固有取舍，不是缺陷。
> - **自检口径能抓**：归档与清单不一致（行数漂移在应用前的 `selfcheck` 就拦）、缺表/多表（`441-450`）、表集合层面的 schema 漂移、半截恢复（`APPEND_ONLY_TABLES` 行数低于 manifest，`424-428` 与 `312`）、库结构损坏（`PRAGMA integrity_check`，434）、外键违规（435）、admin 账号缺失（`429-433`）、演示残留（`436-437`、`453`）。
> - **自检口径抓不住**：行数相同的内容级篡改（`_structure_check` 只比行数下限，不比内容）、非只增表在恢复窗口内的并发增删（只有 `APPEND_ONLY_TABLES` 有下限）、归档生成时就已缺失的历史数据、列级与索引级的 schema 漂移（`_structure_check` 不比对 `sqlite_master.sql`）。
> - **这条为什么重要**：把「回滚」读成「恢复原状」会让人以为回滚后文件字节与恢复前一致，从而跳过恢复后备份；把「应用前失败」与「应用中途失败」混为一谈，会在中途失败后拿旧库指纹做字节比对，把正常的在线回滚判成数据损坏。

- 锁文件 `backup.lock`、`LOCK_STALE_SEC = 600`（`backup.py:39-40`）：备份/恢复是秒级操作，占锁超 10 分钟视为进程已崩溃可回收。
- 归档名不是任意路径：只接受 `ARCHIVE_RE` 命中的文件名，路径穿越/自定义目录直接 400（`backup.py:233-234`）。

**校验实现位置（2026-11-05 补记）**

上表第 2 步的四道校验全部实现在 `scripts/backup_core.py`；API 层只调 `load_archive` 并采用其返回值，没有第二套平行校验。逐项落点：

| 校验 | 实现 | 行为 |
| --- | --- | --- |
| zip 可读 / 成员损坏 | `extract_zip`（`backup_core.py:505-514`） | `testzip()` 命中损坏成员直接拒（507-509） |
| 成员路径穿越 | `extract_zip`（`backup_core.py:505-514`）、`member_path`（`backup_core.py:517-522`） | 解压目标必须落在 staging 目录内，越界即拒（513、521-522） |
| `manifest.json` 缺失 | `load_archive`（`backup_core.py:576-578`） | 直接拒，不猜测、不降级 |
| manifest 结构 / 版本 | `validate_manifest`（`backup_core.py:552-564`） | 字段类型与 `manifest_version` 校验，高于本工具版本即拒 |
| 成员尺寸 + sha256 | `verify_members`（`backup_core.py:525-549`） | 库成员与 files 列表逐项比对 size/sha256（537-539、546-547） |
| 备份创建时的库完整性 | `load_archive`（`backup_core.py:580-583`） | manifest 记录的 `integrity_check` 必须是 `ok` |
| 解出库的完整性 | `load_archive`（`backup_core.py:585-588`）、`database_stats`（`backup_core.py:127-131`） | 对解出的 `unified.db` 再跑一次 `PRAGMA integrity_check` |
| 归档 `.sha256` 旁注 | `archive_digest`（`backup_core.py:491-502`） | 旁注存在则强校验，缺失返回 `None`：API 调 `load_archive` 未传 `require_sidecar=True`（`backup.py:243`），所以缺失旁注不阻断恢复；CLI 需显式 `--require-sidecar` 才强制（`backup_core.py:804-806`、`restore.sh:48,87`） |

**这条为什么重要**：以为 API 层只做「文件名白名单」就敢恢复任意归档，会漏掉成员篡改、路径穿越、库损坏这三类拒绝；以为 `.sha256` 旁注是硬门槛，会把没有旁注的合法归档当成必坏件。

**这条为什么重要**：漏记 `confirm` 会让人以为误调一次就覆盖生产库；漏记「恢复前快照 + selfcheck 回滚」会把恢复当成不可逆操作而不敢用；漏记文件名白名单会以为能指定任意 zip/路径；漏记 409 会把并发触发的锁误报成 500 故障。

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

### 已知边界：告警评估逐阈值隔离，失败阈值下一轮重试

`evaluate_alerts()` 内部**逐阈值（并逐策略）隔离**：某条阈值评估抛异常时只记一条 ERROR 日志（带 `policy_id` / `threshold` / `key_id` / `provider` / `model` / `window`），跳过**本轮该阈值**，同一请求中的后续阈值与后续策略照常评估；`settle()` 外层另有一道整体 try/except 兜底策略匹配这类整段失败。评估失败只记日志，不影响已写入的流水与用量。

- **失败阈值的语义是「跳过、下一轮重试」**：不写失败状态（`quota_alerts` 无状态列，也不新增），下一次请求评估时该阈值若仍跨线就直接补发；同一周期内若再无请求则不补，与改动前一致。
- **不产生重复行**靠 `quota_alerts` 的 `UNIQUE(policy_id, threshold_percent, period_start)` 库层约束（不是 upsert）：先查后插，重试时查到已有行即跳过；并发下后到者的 INSERT 撞 UNIQUE，被该条阈值的隔离吞掉并记日志，库里仍只有一行。

---

## 12. Grok 注册任务（`/api/grok/register`）

任务编排为控制面内一等服务的队列；浏览器执行器外置（`GROK_REGISTER_EXECUTOR_URL`），未配置时任务以明确原因失败。

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `POST /api/grok/register/tasks` | 管理员 | 入队 |
| `GET /api/grok/register/tasks?limit=50` | 管理员 | 列表（limit 夹逼 1–200，按 id 倒序） |
| `GET /api/grok/register/tasks/{task_id}` | 管理员 | 详情，404 不存在；非 admin 403 `insufficient_role` |
| `POST /api/grok/register/tasks/{task_id}/cancel` | 管理员 | 取消；仅 `queued`/`running` 可取消，否则 409 |
| `POST /api/grok/register/tasks/{task_id}/retry` | 管理员 | 重试；仅 `failed`/`cancelled` 可重试（attempts 归零重新入队），否则 409 |
| `GET /api/grok/register/worker` | 登录 | 队列与并发状态 |

> **2026-11-05 已更正（原句保留作历史）**
>
> 上表 `GET /api/grok/register/worker` 原记「登录」，当前事实是 **admin-only**：路由注释写明队列状态只给 GrokRegisterTasks 用、仅 admin，处理函数为 `Depends(admin_user)`（`grok.py:72-76`）；viewer 在进入处理逻辑前 403 `insufficient_role`。前端消费方本身也只在管理员下渲染：`ProvidersSection.tsx:25` import、`:66` `isAdmin`、`:721` `p.id === 'grok-register' && isAdmin && <GrokRegisterTasks />`。
>
> **这条为什么重要**：按旧文档给 viewer 放行，对接会直接吃 403 并误判成接口故障；反过来若照旧口径保持登录可读，会把队列与执行器状态暴露给非管理员。

读侧权限口径（2026-11-04 收紧）：两个 `GET /api/grok/register/tasks*` 与写侧同为 `admin`，非 admin 在查库前 403 `insufficient_role`。原因是任务对象按 `grok_tasks` 表原样回显，含 `proxy`（常带代理口令）、`payload_json`、`result_json`；前端对这两个读端点零调用，收紧不影响既有页面。

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
| 400 | `x-provider` 头与 `provider/model` 前缀指向不同 provider（`conflicting_provider_hints`：message 点名两个 provider id，`detail` 带 `header_provider`/`prefix_provider`，`error.provider` 取头值） |
| 401 | 未登录/会话过期（`/api/*`）；网关密钥缺失或无效（`/v1/*`）；登录密码错误 |
| 403 | viewer 调管理员接口或读取非自有密钥的配额数据（响应带 `error.type=permission_denied`、`required_role`）；sub2api 分组已停用；CodeBuddy 管理端拒绝 `wbt_` token |
| 404 | provider / 密钥 / 账号 / 分组 / 任务不存在；配额 `key_id` 引用的密钥或策略不存在；模型解析失败（`model_not_found` / `model_unavailable`，含**没有任何已启用 provider** 能服务该模型的情况）；`/v1/*` 路径不存在（`route_not_found`） |
| 404 | `x-provider` 指向本地注册表没有的 provider（`provider_not_found`，message「未知的 provider：xxx」）；与真正上游 404 的 `upstream_not_found` 区分 |
| 409 | 用户名已存在；任务状态不允许取消/重试 |
| 429 | sub2api 分组或账号池额度耗尽；网关密钥触发配额上限；同一密钥失败请求达到窗口上限（`failure_rate_limited`）；上游 429（保留 `Retry-After`） |
| 501 | 请求的 provider 不支持该能力（如 trae_sidecar 无 `/v1/messages`；deepseek sidecar 无流式 messages） |
| 502 | 上游不可达（连接失败、DNS 失败等）；上游 5xx 归并到 502（原始状态码见 `error.upstream_status`） |
| 503 | 显式指定已停用的 provider（`x-provider` 或 `provider/model` 前缀，`provider_disabled`）；sub2api 分组未配置；sub2api 账号缺 upstream_base |
| 504 | 上游超时（408/504 归并）；流式响应超过时长上限 |
| 405 | `/v1/*` 路径存在但方法不允许（`method_not_allowed`，保留 `Allow` 头，按协议出壳） |

---

## 14. 请求日志（`/api/logs`）

`request_logs` 表只有 `ts / provider / model / status / tokens / latency / stream / error / key_id / key_name`，没有 IP、UA、首字延迟、缓存三段、扣费这些列。响应按 `apps/web/lib/types.ts::RequestLog` 的字段名输出，除 `credit` / `credit_status`（按 `log_id` 关联 `billing_ledger` 算出，见下）外，缺的列一律给 `null`（该类型的 `null` = 未采集），不编造数值；`key_prefix` 只回显掩码（`u2a_` + 后 4 位 + `****`）。未登录 401。

### `GET /api/logs`（登录）

| 查询参数 | 说明 |
| --- | --- |
| `page` / `size` | 前端分页：`size` 夹逼 1–200（默认 20），`page` 从 1 起 |
| `limit` / `offset` | 与上一组二选一；**同时传时 `limit` 优先**，`limit` 同样夹逼 1–200 |
| `key_id` | 按密钥过滤。非 admin 显式传他人 / 不存在 / NULL 归属的 `key_id` → 403 `无权查看他人密钥的请求日志`（不退化成空集合） |
| `provider` | 精确匹配 |
| `model` | `LIKE %…%` 模糊匹配 |
| `status` | `ok` / `error` / 纯数字状态码 / `all`（或不传 = 不过滤）；其他值 400 `status 需为 ok / error 或数字状态码`。`ok`/`error` 口径与第 7 节 stats 一致：2xx 但带 `error` 的断流请求算失败 |
| `days` | 最近 N 天，夹逼 1–365 |
| `start` / `end` | Unix 秒时间窗，可与 `days` 叠加（AND） |

归属：admin 读全量；非 admin 只读 `owner_user_id = 自己` 名下 key 的日志（NULL 归属的老 key、`key_id` 为空的匿名日志一律不可见）。

返回 `{"total": 过滤后真实 COUNT, "items": [...]}`，按 `ts, id` 倒序。item 字段：`id, ts, key_id, key_name, key_prefix, provider, ip: "", model, mapped_model: null, status, prompt_tokens, completion_tokens, latency_ms, first_token_ms: null, cache_hit_tokens: null, cache_miss_tokens: null, cache_write_tokens: null, ua: null, error, stream, credit, credit_status, account: null`。

`credit` 为整数 micro-USD（关联到流水时），关联不到为 `null`，配合 `credit_status` 五档区分口径：

| `credit_status` | 含义 |
| --- | --- |
| `billed` | 已计费且金额确定，`credit` 给 micro-USD 整数（上游回了 usage 但用量为 0 时也落此档，金额为 0） |
| `unpriced` | 流水 `note` 为 `unpriced:{类别,…}`：部分类别缺价，流水金额只是估值，不冒充确定金额，`credit` 为 `null` |
| `usage_missing` | 上游没回 usage，无法计价，`credit` 为 `null` |
| `not_billed` | 失败/断流请求本就不写流水（`status >= 400` 或带 `error` 且无关联流水） |
| `unlinked` | 成功但查不到关联流水：存量日志 `log_id` 未回填，或写流水异常 |

流水的 `note` 字段**不下发**到本接口；缺价时想拿确定金额，用 `GET /api/pricing/resolve` 对该行现查（见第 10 节）。

### `POST /api/logs/clear`（管理员）

清空 `request_logs`，返回 `{"ok": true, "deleted": N}` 并写审计 `logs.clear`（detail `deleted=N`）。viewer 403。只清日志表：`billing_ledger` 流水与 `billing_*` 设置不动（金额只来自流水，见第 10 节）；清空后 stats 的请求计数也随之归零（它读的就是这张表）。

---

## 15. 审计与安全（`/api/audit-logs`、`/api/security/*`）

安全面**只读**，四个端点全部 `Depends(current_user)`（登录即可，未登录 401），viewer 能读但被强制收敛到自己的数据。**不存在** `/api/security/rules` 与 `/api/security/logs`：库里没有对应存储，也没有执行逻辑（`net_guard.py` 只在写入上游地址时做环回/私网/链路本地/云元数据校验）。

### `GET /api/audit-logs`（= `GET /api/security/audit`，登录）

同一 handler 挂两个路径，行为完全一致；前端只用前者，后者是兼容别名（`unification-audit` 建议标 `deprecated` 或删除）。

| 查询参数 | 说明 |
| --- | --- |
| `limit` | 默认 **200**，夹逼 1–500（`MAX_AUDIT_LIMIT`） |
| `offset` | 默认 0，负数归 0 |
| `action` | 精确匹配，如 `login_failed`、`logs.clear`、`settings.reveal_secret` |
| `actor` | 用户名；非 admin 传他人 → 403 `无权查看他人审计日志`，否则强制 `actor = 自己` |

返回 `{"items": [{id, ts, actor, action, target, detail, ip}], "total": 过滤后 COUNT}`，按 `ts, id` 倒序。`actor` 存执行者用户名（登录失败那类记录存的是「被尝试的用户名」）。

### `GET /api/security/guard`（登录）

地址守卫的**静态策略**，不是真实封禁名单，也不是访问日志：

```json
{
  "allow_private_upstreams": false,
  "blocked_hostnames": ["localhost", "localhost.localdomain", "metadata.google.internal"],
  "blocked_addresses": ["169.254.169.254", "fd00:ec2::254"],
  "blocked_categories": ["loopback", "private", "link_local", "multicast", "reserved", "unspecified"],
  "allowed_schemes": ["http", "https"]
}
```

### `GET /api/security/login-failures?limit=50`（登录）

登录失败节流视图（窗口 900s / 阈值 5 次，与 `POST /api/login` 同一套计数）。`limit` 夹逼 1–200，只作用于 admin 的 `recent`。按角色分两个 scope：

| 字段 | admin（`scope: "all"`） | 非 admin（`scope: "self"`） |
| --- | --- | --- |
| `window_sec` / `max_failures` | 900 / 5 | 900 / 5 |
| `active` | 窗口内仍在计数的 `{ip, username, failures, first_ts, last_ts, locked, retry_after}`（`locked` = 达到阈值） | 恒 `[]` |
| `stats` | 全站 `login_failed` 审计的 `{total, today}` | 针对**自己账号**的 `{total, today}` |
| `recent` | 最近 `limit` 条审计行 | 恒 `[]` |
| `window_failures` / `last_failed_ts` | 无 | 自己账号的窗口内失败数 / 最后一次失败时间 |

非 admin 不返回 IP、不返回他人用户名，也不做撞库推测；内存桶只保留窗口内的记录，`active` 同样按窗口过滤。
