# 鉴权面审计报告（/api/ 与 /v1/ 全端点）

- 审计日期：2026-11-04
- 范围：`apps/control/app/main.py` + `apps/control/app/routers/*.py`（18 个路由文件）+ 前端调用面 `apps/web/lib/api.ts`、`apps/web/lib/quota-api.ts` 及各页面调用点
- 方式：只读代码审计。未修改任何 `.py`，未跑测试、未起 HTTP 服务、未做 git 操作
- 鉴权原语（全部定义在 `apps/control/app/security.py`）：
  - `current_user` — 登录即可（security.py:86）
  - `admin_user` — 必须 admin（security.py:102）
  - `quota_admin` — 配额域只读闸门，等价 admin（security.py:111）
  - 无依赖 = 匿名可访问（本应用没有全局鉴权中间件，鉴权完全靠每个路由的 `Depends`）

## 结论速览

1. **「应当 admin 但只要求登录」共 4 个端点**（其中 grok 注册任务、账号列表两处会把带凭据/资产性质的数据吐给 viewer），另有 7 个低敏运维读端点同样只收 `current_user`，列为观察项。
2. **「应当登录但完全无鉴权」为 0**：所有返回业务数据的 `/api/` 端点都挂了 `current_user` 或 `admin_user`；完全匿名的只有登录、登出、健康探针、`/healthz`、静态资源与默认关闭的文档页。
3. **前端必 403 的调用为 0**：`lib/api.ts` / `lib/quota-api.ts` 中每个 admin 端点的调用点都有 `isAdmin` 门禁；仅发现 1 处潜在口子（`ManagementBar` 的自定义事件监听不判角色，当前无触发方）。
4. `/v1` 侧 4 个端点全部经过 `_gateway_key` API Key 校验，**无绕过路径**；唯一旁路是 `allow_anonymous` 配置开关（DB/env，**默认关**）。

---

## 一、总表：端点 / 方法 / 当前鉴权 / 判定 / 证据

判定口径：
- **合规** = 鉴权级别与数据敏感度匹配
- **应 admin 只要登录** = 读操作把全量数据暴露给 viewer
- **观察项** = 只要登录可读，但内容属运维/全局数据，建议复核
- **有意公开** = 匿名可访问且合理
- **体内权限** = 依赖只查登录，角色/归属判断写在函数体里（单独见第三节）

### health.py（prefix `/api`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/health | 无 | 有意公开 | health.py:94 |
| GET | /api/health/ready | 无 | 有意公开 | health.py:99 |
| GET | /api/metrics | admin_user | 合规 | health.py:120 |

### auth.py（prefix `/api`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| POST | /api/login | 无 | 有意公开 | auth.py:85 |
| POST | /api/logout | 无 | 有意公开（幂等登出，仅操作自身 cookie） | auth.py:121 |
| GET | /api/me | current_user | 合规 | auth.py:135 |
| POST | /api/auth/start | admin_user | 合规 | auth.py:140 |
| GET | /api/auth/poll | admin_user | 合规 | auth.py:151 |
| POST | /api/sessions/revoke | current_user（只吊销自己） | 合规 | auth.py:166-171 |

### providers.py（prefix `/api/providers`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/providers | current_user | 合规（`list_public()` 不含密钥） | providers.py:21-23 |
| PATCH | /api/providers/{provider_id} | admin_user | 合规 | providers.py:26-31 |

### provider_credentials.py（prefix `/api/provider-credentials`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/provider-credentials | current_user | 观察项（secret 只回掩码 `secret_masked`，见 provider_credentials.py:22-36） | provider_credentials.py:63-67 |
| POST | /api/provider-credentials | admin_user | 合规 | provider_credentials.py:84-88 |
| PATCH | /api/provider-credentials/{credential_id} | admin_user | 合规 | provider_credentials.py:122-127 |
| DELETE | /api/provider-credentials/{credential_id} | admin_user | 合规 | provider_credentials.py:151-155 |

### accounts.py（prefix `/api/accounts`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/accounts/capabilities | current_user | 观察项（全量 provider 能力矩阵） | accounts.py:134-135 |
| GET | /api/accounts | current_user | **应 admin 只要登录**（全量账号资产无归属收窄） | accounts.py:162-167 |
| POST | /api/accounts/sync | admin_user | 合规 | accounts.py:199-200 |
| POST | /api/accounts/refresh-credits | admin_user | 合规 | accounts.py:210-215 |
| POST | /api/accounts/checkin-all | admin_user | 合规 | accounts.py:225-229 |
| PUT | /api/accounts/{file}/note | admin_user | 合规 | accounts.py:246-247 |
| POST | /api/accounts/{file}/disabled | admin_user | 合规 | accounts.py:264-265 |
| DELETE | /api/accounts/{file} | admin_user | 合规 | accounts.py:288-289 |
| POST | /api/accounts/{file}/checkin | admin_user | 合规 | accounts.py:315-316 |
| POST | /api/accounts/{file}/test | admin_user | 合规 | accounts.py:320-321 |
| POST | /api/accounts/{file}/refresh | admin_user | 合规 | accounts.py:325-326 |
| POST | /api/accounts/{file}/clear-cooling | admin_user | 合规 | accounts.py:330-331 |
| POST | /api/accounts/restart | admin_user | 合规 | accounts.py:335-336 |

### keys.py（prefix `/api/keys`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/keys | current_user + 体内归属收窄 | 合规（体内权限，见第三节） | keys.py:35-46 |
| POST | /api/keys | admin_user | 合规 | keys.py:71-72 |
| PATCH | /api/keys/{key_id} | admin_user | 合规 | keys.py:95-96 |
| DELETE | /api/keys/{key_id} | admin_user | 合规 | keys.py:118-119 |

### tokens.py（prefix `/api/tokens`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/tokens | current_user + 体内归属收窄 | 合规（体内权限） | tokens.py:58-72 |
| POST | /api/tokens | admin_user | 合规 | tokens.py:75-76 |
| PATCH | /api/tokens/{token_id} | admin_user | 合规 | tokens.py:90-91 |
| DELETE | /api/tokens/{token_id} | admin_user | 合规 | tokens.py:114-115 |

### models.py（prefix `/api`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/status | current_user | 合规（上游状态汇总，dashboard 在用） | models.py:10-11 |
| GET | /api/models | current_user | 合规 | models.py:30-35 |

### stats.py（prefix `/api/stats`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/stats/summary | current_user + 体内归属收窄 | 合规（体内权限） | stats.py:58-63 |
| GET | /api/stats/daily | current_user + 体内归属收窄 | 合规（体内权限） | stats.py:137-142 |

### settings.py（prefix `/api`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/settings/runtime-config | admin_user | 合规 | settings.py:383-384 |
| POST | /api/settings/runtime-config | admin_user | 合规 | settings.py:388-392 |
| GET | /api/settings/runtime-config/export | admin_user | 合规 | settings.py:398-399 |
| POST | /api/settings/runtime-config/import | admin_user | 合规 | settings.py:411-415 |
| GET | /api/settings/upstream | admin_user | 合规 | settings.py:477-478 |
| POST | /api/settings/secret/reveal | admin_user | 合规 | settings.py:503-504 |
| POST | /api/settings/upstream | admin_user | 合规 | settings.py:523-527 |
| GET | /api/settings/gateway | admin_user | 合规 | settings.py:558-559 |
| POST | /api/settings/gateway | admin_user | 合规 | settings.py:579-583 |
| GET | /api/settings/model-map | admin_user | 合规 | settings.py:604-605 |
| POST | /api/settings/model-map | admin_user | 合规 | settings.py:614-618 |
| POST | /api/settings/upstash/test | admin_user | 合规 | settings.py:919-920 |
| GET | /api/users | current_user | **应 admin 只要登录**（全量用户名+角色） | settings.py:956-963 |
| POST | /api/users | admin_user | 合规 | settings.py:966-967 |
| PATCH | /api/users/{username} | admin_user | 合规 | settings.py:988-993 |
| DELETE | /api/users/{username} | admin_user | 合规 | settings.py:1028-1029 |

### sub2api.py（prefix `/api/sub2api`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/sub2api/groups | quota_admin | 合规 | sub2api.py:57-58 |
| POST | /api/sub2api/groups | admin_user | 合规 | sub2api.py:62-63 |
| PATCH | /api/sub2api/groups/{group_id} | admin_user | 合规 | sub2api.py:72-73 |
| DELETE | /api/sub2api/groups/{group_id} | admin_user | 合规 | sub2api.py:87-88 |
| GET | /api/sub2api/accounts | quota_admin | 合规 | sub2api.py:96-97 |
| POST | /api/sub2api/accounts | admin_user | 合规 | sub2api.py:101-102 |
| PATCH | /api/sub2api/accounts/{account_id} | admin_user | 合规 | sub2api.py:116-117 |
| DELETE | /api/sub2api/accounts/{account_id} | admin_user | 合规 | sub2api.py:133-134 |
| POST | /api/sub2api/accounts/{account_id}/reset | admin_user | 合规 | sub2api.py:142-143 |
| GET | /api/sub2api/ledger | quota_admin | 合规 | sub2api.py:150-155 |

### pricing.py（prefix `/api/pricing`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/pricing/books | current_user | 观察项（全局价格表） | pricing.py:38-45 |
| POST | /api/pricing/books | admin_user | 合规 | pricing.py:78-82 |
| GET | /api/pricing/resolve | current_user | 观察项 | pricing.py:127-133 |
| POST | /api/pricing/import | admin_user | 合规 | pricing.py:160-164 |

### quota.py（prefix `/api/quota`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/quota/policies | current_user + 体内归属收窄 | 合规（体内权限；前端对 viewer 直接不请求） | quota.py:170-193 |
| POST | /api/quota/policies | admin_user | 合规 | quota.py:217-218 |
| DELETE | /api/quota/policies/{policy_id} | admin_user | 合规 | quota.py:281-282 |
| GET | /api/quota/status | current_user + 体内归属收窄 | 合规（体内权限） | quota.py:329-358 |
| GET | /api/quota/alerts | current_user + 体内归属收窄 | 合规（体内权限） | quota.py:363-410 |

### grok.py（prefix `/api/grok/register`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| POST | /api/grok/register/tasks | admin_user | 合规 | grok.py:26-27 |
| GET | /api/grok/register/tasks | current_user | **应 admin 只要登录（最严重）** | grok.py:38-40 |
| GET | /api/grok/register/tasks/{task_id} | current_user | **应 admin 只要登录（最严重）** | grok.py:43-46 |
| POST | /api/grok/register/tasks/{task_id}/cancel | admin_user | 合规 | grok.py:51-52 |
| POST | /api/grok/register/tasks/{task_id}/retry | admin_user | 合规 | grok.py:61-62 |
| GET | /api/grok/register/worker | current_user | 观察项（worker 状态） | grok.py:71-72 |

### system.py（prefix `/api/system`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/system/changelog | current_user | 合规 | system.py:79-80 |
| GET | /api/system/versions | current_user | 观察项（含上游账号计数，system.py:92-113） | system.py:105-106 |
| GET | /api/system/check-update | current_user | 合规（无远端查询，system.py:116-143） | system.py:116-117 |
| GET | /api/system/update-status | current_user | 合规 | system.py:146-147 |

### logs.py（prefix `/api/logs`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/logs | current_user + 体内归属收窄 | 合规（体内权限） | logs.py:221-253 |
| POST | /api/logs/clear | admin_user | 合规 | logs.py:265-266 |

### security.py（prefix `/api`）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /api/security/guard | current_user | 观察项（全局网络守卫配置） | security.py:93-94 |
| GET | /api/security/login-failures | current_user + 体内收窄 | 合规（体内权限） | security.py:107-168 |
| GET | /api/audit-logs | current_user + 体内收窄 | 合规（体内权限） | security.py:173-212 |
| GET | /api/security/audit | 同上（同一函数的别名路由） | 合规（体内权限） | security.py:174 |

### v1.py（无 prefix）

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| POST | /v1/chat/completions | API Key（`_gateway_key`） | 合规 | v1.py:468-470 → v1.py:356 |
| POST | /v1/messages | API Key（`_gateway_key`） | 合规 | v1.py:473-475 → v1.py:356 |
| POST | /v1/responses | API Key（`_gateway_key`） | 合规 | v1.py:478-480 → v1.py:356 |
| GET | /v1/models | API Key（`_gateway_key`） | 合规 | v1.py:483-487 |

### main.py 注册的裸端点

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /healthz | 无 | 有意公开（编排器探针） | main.py:282-285 |
| GET | /（静态资源挂载） | 无 | 有意公开（SPA 产物） | main.py:287-288 |
| GET | /（无前端产物时的 JSON 兜底） | 无 | 有意公开（不含敏感信息） | main.py:291-296 |
| GET | /docs、/redoc、/openapi.json | 无 | 有意公开但**默认关闭**（`UNIFIED_ENABLE_DOCS` 默认 0） | main.py:163-165，config.py:89、config.py:137 |

合计 97 个可路由端点（含别名），其中完全匿名 9 项（login/logout/health×2/healthz/静态/兜底/docs 组），admin 38 个，quota_admin 3 个，current_user 46 个，API Key 4 个，其余为多路由同函数。

---

## 二、分类判定

### 2.1 应当 admin 但只要求登录（读操作暴露全量数据给 viewer）

**4 个端点（按严重度）：**

1. **GET /api/grok/register/tasks、GET /api/grok/register/tasks/{task_id}** — grok.py:38、grok.py:43
   - 依赖只有 `current_user`，返回 `SELECT * FROM grok_tasks` 的整行（grok_register.py:115-121、grok_register.py:105-111，`_row()` 直接 `dict(row)`，grok_register.py:23-24）
   - `grok_tasks` 表含 `proxy`（代理地址，常见形态带 `user:pass@`）、`email_provider`、`payload_json`（注册载荷）、`result_json`（注册产物）——db.py:224-238
   - 写侧全部要求 admin（grok.py:26、51、61），唯独读侧放给 viewer，且**前端没有任何页面调用它**（`apps/web` 全局 grep 无调用点），属于「无业务理由的暴露面」

2. **GET /api/accounts** — accounts.py:162-196
   - `current_user`，SQL 只按 provider/realm 过滤，**无归属收窄**，返回全部上游账号：`file`、`uid`、`nickname`、`enterprise_id`、`credits`、`note`、`expires_at` 等（api_utils.py:76-101，token 明文未回传）
   - 写操作全为 admin（accounts.py:199 起），读写口径不一致
   - 前端 dashboard 对 viewer 也调用（dashboard/page.tsx:73），即 viewer 必然拿到全量账号资产清单

3. **GET /api/users** — settings.py:956-963
   - `current_user`，返回 `SELECT username, role FROM users` 全量行
   - 用户名+角色是账号枚举信息（配合撞库/定向登录），而同一文件的增删改都要求 admin（settings.py:966、988、1028）
   - 前端 settings 页对 viewer 也调用（settings/page.tsx:926）

**观察项 7 个（只要登录，内容为运维/全局数据，建议复核是否收紧）：**

| 端点 | 证据 | 内容 |
|---|---|---|
| GET /api/accounts/capabilities | accounts.py:134-135 | 各 provider 账号管理能力矩阵 |
| GET /api/security/guard | security.py:93-104 | 网络守卫开关与封禁清单 |
| GET /api/grok/register/worker | grok.py:71-72 | 注册 worker 状态 |
| GET /api/system/versions | system.py:105-113 | 版本 + 上游账号计数 |
| GET /api/pricing/books | pricing.py:38-45 | 全局价格表 |
| GET /api/pricing/resolve | pricing.py:127-133 | 单模型价格解析 |
| GET /api/provider-credentials | provider_credentials.py:63-67 | 凭据条目（secret 掩码，provider_credentials.py:29） |

### 2.2 应当登录但完全无鉴权

**0 处。** 所有返回业务数据的 `/api/` 端点都挂了 `current_user`/`admin_user`/`quota_admin`；完全匿名的 9 项全部归入 2.3 有意公开。路由注册处（main.py:278-280）未在 `include_router` 层加任何全局依赖，也就没有「加了依赖却被 include 绕过」的风险面。

### 2.3 有意公开清单

| 端点 | 为什么公开是合理的 | 证据 |
|---|---|---|
| POST /api/login | 登录入口本身必须匿名；带 IP+用户名失败节流与弱口令告警 | auth.py:85-89、main.py:66-73 |
| POST /api/logout | 幂等，只删请求携带的自身 cookie，无会话也返回 ok，不泄露数据 | auth.py:121-132 |
| GET /api/health、GET /api/health/ready | 容器/编排探针不带会话；返回体只含子系统状态与 readiness 枚举，不回传 base_url/凭据/库路径 | health.py:1-11、health.py:42-45、health.py:94-117 |
| GET /healthz | 同上，且已刻意不返回数据库路径 | main.py:282-285 |
| GET /（静态资源挂载、JSON 兜底） | 前端 SPA 产物必须可匿名下载；登录由页面自身处理（前端 401 拦截器跳 /login） | main.py:287-296、lib/api.ts:56-78 |
| /docs、/redoc、/openapi.json | 生产默认关闭（`enable_docs=False`），本地调试显式开 | main.py:162-165、config.py:89、config.py:137 |
| POST /v1/*（4 个端点） | 网关面向下游程序，用 API Key 而非会话鉴权是协议设计（OpenAI/Anthropic 客户端只发 `Authorization`/`x-api-key`） | v1.py:57-98 |
| （配置项）`allow_anonymous` 旁路 | DB `gateway.allow_anonymous` 或 `UNIFIED_ALLOW_ANONYMOUS_GATEWAY` 打开时 /v1 免 key；**默认关**，脏值按拒绝处理 | v1.py:40-49、config.py:131 |

### 2.4 权限判断写在函数体里而不是依赖里（最容易漏，单独列）

| 端点 | 依赖 | 函数体内的权限逻辑 | 证据 |
|---|---|---|---|
| GET /api/keys | current_user | `if role == admin` 查全表，否则按 `owner_user_id` 收窄；NULL 归属对非 admin 不可见 | keys.py:36-46 |
| GET /api/tokens | current_user | 同上双分支 | tokens.py:59-72 |
| GET /api/logs | current_user | `_owner(user)` 决定是否加归属条件；带 `key_id` 时 `_assert_key_owned` 校验归属 | logs.py:235、logs.py:238-246、logs.py:43-53 |
| GET /api/stats/summary、/api/stats/daily | current_user | `_owner()` + `_owned()` 收窄 request_logs/billing_ledger/api_keys | stats.py:21-32、stats.py:60-63、stats.py:140-142 |
| GET /api/quota/policies | current_user | `_scope_key()` 校验 key 归属，`_owner()` 收窄策略集 | quota.py:175、quota.py:179-192、quota.py:106-114、quota.py:46-53 |
| GET /api/quota/status | current_user | 同上 | quota.py:335、quota.py:341-354 |
| GET /api/quota/alerts | current_user | 同上；非 admin 只看自有 key 告警 | quota.py:371、quota.py:382-392 |
| GET /api/security/login-failures | current_user | `is_admin` 分支：admin 看全站行含 IP，非 admin 只回聚合数字 | security.py:108-168 |
| GET /api/audit-logs、/api/security/audit | current_user | 非 admin 强制 `actor = 自己`，跨 actor 查询直接 403 | security.py:175-193 |

这 9 个端点的共同风险：**收窄逻辑靠人肉记得写**，新增过滤参数（如未来加 `actor=`、`provider=`、跨 key 查询）时容易漏掉归属条件。目前逐个核对未发现可越权读取他人数据的路径（quota 的 `X-Api-Key` 头走 `_key_by_token`+`_assert_owned`，quota.py:56-65、quota.py:46-53；日志 `key_id` 走 `_assert_key_owned`，logs.py:43-53）。

---

## 三、前端实际调用面交叉核对

映射基准：`apps/web/lib/api.ts`（355 行）、`apps/web/lib/quota-api.ts`、`app/(main)/settings/RuntimeConfigSection.tsx:40`。

**结论：没有「前端调了、后端要求 admin、viewer 必得 403」的调用（0 处）。** 每个 admin 端点的调用点都做了角色门禁：

| admin 端点 | 前端调用点 | 门禁证据 |
|---|---|---|
| POST /api/keys、PATCH、DELETE | keys/page.tsx:92、218、220 | 渲染门禁 keys/page.tsx:164、211（`isAdmin &&`） |
| POST /api/tokens 等 | TokensPanel.tsx:95 | 组件级早退 TokensPanel.tsx:58（`if (!isAdmin)`） |
| POST /api/logs/clear | logs/page.tsx:258 | logs/page.tsx:251（`isAdmin &&`） |
| POST /api/accounts/* 写操作、/api/auth/start、/api/auth/poll | accounts/page.tsx:760-833、OAuthDialog.tsx:81/94、AddAccountDialog.tsx:105、BatchOAuthDialog.tsx:178 | 操作列只在 `isAdmin` 下渲染（accounts/page.tsx:1072、1142、1162-1177）；顶栏入口 accounts 页与 ManagementBar.tsx:506（`if (isAdmin)`） |
| PATCH /api/providers/{id} | ProvidersSection.tsx:128 | 全部按钮 `disabled={!isAdmin}`（ProvidersSection.tsx:302-552、427-552） |
| GET/POST /api/settings/gateway | ProvidersSection.tsx:116-119、144 | 读取用 `if (!isAdmin) return`（ProvidersSection.tsx:112-120，注释明说「viewer 不该为此拿一次 403」）；开关 `disabled={!isAdmin}`（ProvidersSection.tsx:777） |
| GET/POST /api/settings/upstream、model-map | settings/page.tsx:878-879 | 加载包在 `if (isAdmin)` 内（settings/page.tsx:876-928）；保存按钮 `disabled={!isAdmin}`（settings/page.tsx:1304-1398） |
| POST /api/settings/upstash/test | settings/page.tsx:338 | `disabled={!isAdmin}`（settings/page.tsx:1464） |
| GET/POST /api/settings/runtime-config* | settings/RuntimeConfigSection.tsx:40 | `if (!isAdmin) return` 加载门禁 + `return null` 渲染门禁（RuntimeConfigSection.tsx:88-92） |
| POST /api/users、PATCH、DELETE | settings/page.tsx:344-349 | `isAdmin &&` 渲染（settings/page.tsx:1106） |
| POST/DELETE /api/provider-credentials | ProvidersSection.tsx:216 | `disabled={!isAdmin}` / `isAdmin &&`（ProvidersSection.tsx:613-701、766） |
| POST /api/pricing/books、/api/pricing/import | pricing/page.tsx:131、154、165 | 操作列三元门禁 pricing/page.tsx:182-184；导入按钮在 isAdmin 预览分支内 |
| /api/sub2api/* 全部读写 | quota/page.tsx:161-371 | 页面级 `if (!isAdmin)` 早退，viewer 直接看说明（quota/page.tsx:95-113） |
| /api/quota/policies、status、alerts | lib/quota-api.ts:88-102 | /quota/policies 页 `if (!isAdmin)` 早退（quota/policies/page.tsx:90-104，注释「避免三块 403 报错」） |
| gatewayApi.get | ProvidersSection.tsx:116 | `if (!isAdmin) return`（ProvidersSection.tsx:115） |

**反向问题（宽后端 + 严前端）：** 上面 quota 读接口后端其实允许 viewer 自查（按归属收窄），但前端一律 admin-only 不请求——属于有意收紧，不构成 bug。

**仅有的 1 处潜在口子：** `ManagementBar.tsx:317-326` 监听 `workbuddy-manager:open-add-account` 自定义事件时**不判 `isAdmin`** 直接 `setAddOpen(true)`，viewer 打开添加账号对话框后发起 `POST /api/auth/start` 会得 403。当前 `apps/web` 内没有该事件的 `dispatch` 调用方（grep 仅命中监听器本身），所以现实中触发不了；一旦将来有入口 dispatch 就会变成「点了就报错」。

**前端未调用但后端暴露的端点：** `/api/grok/register/*`（全部 6 个）、`/api/metrics`、`/api/grok` 外的其余无——其中 grok 的 3 个读端点是纯暴露面，没有任何前端消费者。

---

## 四、/v1 侧核对

| 检查项 | 结果 | 证据 |
|---|---|---|
| 路由总数 | 4 个：`POST /v1/chat/completions`、`POST /v1/messages`、`POST /v1/responses`、`GET /v1/models`；**本服务不提供 /v1/images、/v1/videos、/v1/embeddings 等**（图片/视频由各 adapter 在上游侧转发，不是本服务路由） | v1.py:468-498，全仓 grep `@router\.` 仅这 4 条 `/v1` 路由 |
| 是否都过 API Key | **是。** 3 个 POST 全走 `_proxy()`，第一行准入即 `_gateway_key()`；GET /v1/models 在返回模型列表前显式调 `_gateway_key()` | v1.py:356（`key = _gateway_key(request, conn)`）、v1.py:483-489 |
| 是否有绕过鉴权中间件的路径 | 无中间件可绕——本应用**没有全局鉴权中间件**，/v1 鉴权是每端点内联调用。4 个端点逐一核对均已调用；未匹配的 /v1 路径进框架异常处理器只回协议壳 404/405，不泄露数据 | main.py:249-259 |
| 鉴权头口径 | 支持 `Authorization: Bearer`、裸 token、`x-api-key`；两头同带必须同值，冲突直接 401 `conflicting_auth_headers`；无头时查匿名开关，关闭则 401 | v1.py:52-78 |
| key 校验 | sha256 哈希查 `api_keys`，`enabled=1` 才通过，不匹配 401 `invalid_api_key` | v1.py:79-86 |
| 配置型旁路 | `_anonymous_allowed()`：DB `gateway.allow_anonymous` 优先，回落 `UNIFIED_ALLOW_ANONYMOUS_GATEWAY`，**默认 False**；库内脏值按关闭处理 | v1.py:40-49、config.py:131 |
| 免 key 后的影响面 | 匿名请求 `key.id = None`，配额/失败限流直接跳过（无归属维度），但仍会写 request_logs | v1.py:132-134、v1.py:164-166 |

---

## 五、建议修复（按严重度排序，仅建议，未动手）

### 高

1. **`GET /api/grok/register/tasks` 与 `GET /api/grok/register/tasks/{task_id}` 改为 `admin_user`**（grok.py:38-48）。理由：返回整行含 `proxy`、`payload_json`、`result_json`（db.py:224-238），可能携带代理账号口令与注册产物，而写侧本就全 admin，且前端零调用。若要保留给非 admin 查看，至少对这三列做掩码。
2. **`GET /api/accounts` 收紧为 `admin_user` 或按需脱敏**（accounts.py:162-196）。理由：写操作全 admin，读侧无归属收窄，全量 uid/昵称/积分/备注对 viewer 可见。**注意联动**：dashboard 对 viewer 也调用它（dashboard/page.tsx:73），收紧时需同步给前端加 `isAdmin` 门或提供降级数据（只回聚合数字），否则会新造「点了就报错」。
3. **`GET /api/users` 收紧为 `admin_user`**（settings.py:956-963）。理由：全量用户名+角色枚举，而同文件写操作全 admin。**联动**：settings/page.tsx:926 对 viewer 也调用，需一并加门。

### 中

4. **把第 2.4 节的 9 个「体内权限」端点逐步收敛到依赖层**（如 `Depends(scoped_user)` 统一返回归属条件），避免后续新增查询参数时漏写归属过滤——这是当前架构里最可能「悄悄漏」的位置。
5. **复核 7 个观察项读端点**是否真需要对 viewer 开放：优先 `GET /api/security/guard`（封禁清单）、`GET /api/system/versions`（账号计数）、`GET /api/accounts/capabilities`；`/api/pricing/*` 与 `/api/provider-credentials`（已掩码）可维持现状但应留下判定依据。

### 低

6. **`ManagementBar.tsx:317-326` 事件监听加 `isAdmin` 判断**（或打开后由对话框自身拦截），消除这个无消费方的 403 引信。
7. **考虑给 `/api/grok/register/worker`、`/api/metrics` 之外的运维读接口统一标注权限语义**，并在契约检查里加「路由依赖 = current_user 但返回全局表」的静态告警，防止同类问题复发。
8. **匿名端点清单纳入回归**：9 个有意公开端点建议在测试中固化「确实无鉴权 + 不泄露部署信息」（当前 `/api/health/ready` 与 `/healthz` 已刻意不回传库路径，main.py:284、health.py:42-45），避免未来加字段时无意泄露。

### 未评估项

- CORS `allow_origins` 的具体取值（main.py:175）依赖部署环境变量，本次未核对其是否可能配成宽松来源，建议部署侧核对。
- 本报告未做运行时验证（按约束不跑服务/测试），所有结论均为静态代码证据。
