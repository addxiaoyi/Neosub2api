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

1. **「应当 admin 但只要求登录」共 4 个端点**（其中 grok 注册任务、账号列表两处会把带凭据/资产性质的数据吐给 viewer），另有 7 个低敏运维读端点同样只收 `current_user`，列为观察项。（原表述，2026-11-04 已更正，见下）

> **2026-11-04 已更正（原句保留作历史）**：截至 2026-11-04，「应当 admin 但只要求登录」**0 个**——原列 4 个已全部处理：`GET /api/grok/register/tasks`、`GET /api/grok/register/tasks/{task_id}` 改挂 `Depends(admin_user)`（grok.py:39-41、grok.py:44-47，def 行分别为 40、45，grok.py:38 注释「读侧与写侧同一口径：仅 admin」）；`GET /api/users` 改挂 `Depends(admin_user)`（settings.py:958-964，def list_users 行 959）；`GET /api/accounts` 依赖仍为 `current_user` 但函数体已按角色收窄（accounts.py:294-297，非 admin 追加 `WHERE owner_user_id = ?`，仍 HTTP 200 不是 403），按本文判定口径改归「体内权限」。原句括号内「grok 注册任务、账号列表两处会把带凭据/资产性质的数据吐给 viewer」随之作废；「7 个观察项」不受影响，仍为 7 个（`GET /api/grok/register/worker` 仍 current_user，grok.py:72-73）。这条为什么重要：旧的「4 个未收口」会让人误以为这四处仍对 viewer 全量开放，按错误暴露面做二次处置，或在别处照抄旧口径继续放权。

2. **「应当登录但完全无鉴权」为 0**：所有返回业务数据的 `/api/` 端点都挂了 `current_user` 或 `admin_user`；完全匿名的只有登录、登出、健康探针、`/healthz`、静态资源与默认关闭的文档页。
3. **前端必 403 的调用为 0**：`lib/api.ts` / `lib/quota-api.ts` 中每个 admin 端点的调用点都有 `isAdmin` 门禁；仅发现 1 处潜在口子（`ManagementBar` 的自定义事件监听不判角色，当前无触发方）。（2026-11-04 复核仍成立：新收为 admin 的 `GET /api/grok/register/tasks` 其唯一消费者是 `GrokRegisterTasks.tsx:85`，渲染点在 `ProvidersSection.tsx:721` 的 `isAdmin` 条件内。2026-11-05 再核：该口子的事件仍无派发方；同一组件的个人信息弹窗已挂 `TotpSection`，与本口子无关，见 §三）
4. `/v1` 侧 4 个端点全部经过 `_gateway_key` API Key 校验，**无绕过路径**；唯一旁路是 `allow_anonymous` 配置开关（DB/env，**默认关**）。（原表述，2026-11-04 已更正：现为 14 条，结论不变，见 §四）

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
| GET | /api/accounts/capabilities | current_user | 观察项（全量 provider 能力矩阵） | accounts.py:247-248 |
| GET | /api/accounts | current_user | **应 admin 只要登录**（全量账号资产无归属收窄）（原判定，2026-11-04 已更正，见下） | accounts.py:275-280 |
| POST | /api/accounts/sync | admin_user | 合规 | accounts.py:316-317 |
| POST | /api/accounts/refresh-credits | admin_user | 合规 | accounts.py:331-336 |
| POST | /api/accounts/checkin-all | admin_user | 合规 | accounts.py:346-350 |
| PUT | /api/accounts/{file}/note | admin_user | 合规 | accounts.py:476-477 |
| POST | /api/accounts/{file}/disabled | admin_user | 合规 | accounts.py:494-495 |
| DELETE | /api/accounts/{file} | admin_user | 合规 | accounts.py:518-519 |
| POST | /api/accounts/{file}/checkin | admin_user | 合规 | accounts.py:557-558 |
| POST | /api/accounts/{file}/test | admin_user | 合规 | accounts.py:562-563 |
| POST | /api/accounts/{file}/refresh | admin_user | 合规 | accounts.py:567-568 |
| POST | /api/accounts/{file}/clear-cooling | admin_user | 合规 | accounts.py:572-573 |
| POST | /api/accounts/restart | admin_user | 合规 | accounts.py:577-578 |

> **2026-11-04 已更正（原表格行保留作历史）**：`GET /api/accounts`（accounts.py:275-313，def list_accounts 行 276，依赖 `Depends(current_user)` 在 accounts.py:280）读侧已按角色收窄——accounts.py:294-297：`user.get("role") != "admin"` 时追加 `WHERE owner_user_id = <当前用户名>`，admin 看全量，非 admin 只看自己名下（无主 NULL 行不可见），**HTTP 200 正常返回、不是 403**。原判定「应 admin 只要登录（全量账号资产无归属收窄）」不成立，正确判定为「合规（体内权限）」（参照第 2.4 节 keys.py 的同类形态）。这条为什么重要：旧判定会让人以为 viewer 拉 `GET /api/accounts` 仍能拿到全量 uid/昵称/积分/备注，从而在 dashboard 等调用点误判出并不存在的越权暴露，甚至给 viewer 关掉本可正常展示的数据页。

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
| GET | /api/users | current_user | **应 admin 只要登录**（全量用户名+角色）（原判定，2026-11-04 已更正，见下） | settings.py:958-964 |
| POST | /api/users | admin_user | 合规 | settings.py:968-969 |
| PATCH | /api/users/{username} | admin_user | 合规 | settings.py:990-996 |
| DELETE | /api/users/{username} | admin_user | 合规 | settings.py:1030-1031 |

> **2026-11-04 已更正（原表格行保留作历史）**：`GET /api/users` 已改为 `Depends(admin_user)`（settings.py:958-964，def list_users 行 959），当前鉴权应记为 `admin_user`、判定「合规」；viewer 请求为 403，不再返回全量用户名+角色。这条为什么重要：旧行写着 `current_user` + 「应 admin 只要登录（全量用户名+角色）」，照此会以为该端点对普通用户可读、用户名枚举面仍在，实际已收紧——据此再做「前端必须隐藏该调用」的重复处置会误伤本已闭环的 admin 流程。

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
| GET | /api/grok/register/tasks | current_user | **应 admin 只要登录（最严重）**（原判定，2026-11-04 已更正，见下） | grok.py:39-41 |
| GET | /api/grok/register/tasks/{task_id} | current_user | **应 admin 只要登录（最严重）**（原判定，2026-11-04 已更正，见下） | grok.py:44-47 |
| POST | /api/grok/register/tasks/{task_id}/cancel | admin_user | 合规 | grok.py:52-53 |
| POST | /api/grok/register/tasks/{task_id}/retry | admin_user | 合规 | grok.py:62-63 |
| GET | /api/grok/register/worker | current_user | 观察项（worker 状态） | grok.py:72-73 |

> **2026-11-04 已更正（原表格两行保留作历史）**：`GET /api/grok/register/tasks` 与 `GET /api/grok/register/tasks/{task_id}` 均已改为 `Depends(admin_user)`——grok.py:39-41（def list_tasks 行 40）、grok.py:44-47（def get_task 行 45），grok.py:38 注释「任务行含 proxy / payload_json / result_json，读侧与写侧同一口径：仅 admin」；两行的「当前鉴权」应为 `admin_user`、判定「合规」，「最严重」标记随之撤销。`GET /api/grok/register/worker`（grok.py:72-73，def 行 73）仍为 `current_user` 观察项，本表第 3 行不变。这条为什么重要：这两行原是全报告标为「最严重」的暴露面（整行返回含 `proxy`、`payload_json`、`result_json`），旧判定会让人以为 viewer 仍能读到注册任务整行数据，误判暴露面或重复立项修复。

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
| POST | /v1/chat/completions | API Key（`_gateway_key`） | 合规 | v1.py:639-641 → v1.py:402 |
| POST | /v1/messages | API Key（`_gateway_key`） | 合规 | v1.py:644-646 → v1.py:402 |
| POST | /v1/responses | API Key（`_gateway_key`） | 合规 | v1.py:649-651 → v1.py:402 |
| GET | /v1/models | API Key（`_gateway_key`） | 合规 | v1.py:654-659（校验 :658） |

### main.py 注册的裸端点

| 方法 | 路径 | 当前鉴权 | 判定 | 证据 |
|---|---|---|---|---|
| GET | /healthz | 无 | 有意公开（编排器探针） | main.py:289-292 |
| GET | /（静态资源挂载） | 无 | 有意公开（SPA 产物） | main.py:294-295 |
| GET | /（无前端产物时的 JSON 兜底） | 无 | 有意公开（不含敏感信息） | main.py:294-303 |
| GET | /docs、/redoc、/openapi.json | 无 | 有意公开但**默认关闭**（`UNIFIED_ENABLE_DOCS` 默认 0） | main.py:164-167，config.py:89、config.py:137 |

合计 97 个可路由端点（含别名），其中完全匿名 9 项（login/logout/health×2/healthz/静态/兜底/docs 组），admin 38 个，quota_admin 3 个，current_user 46 个，API Key 4 个，其余为多路由同函数。（原表述，2026-11-04 已更正，见下）

> **2026-11-04 已更正（原句保留作历史）**：本合计是收紧前的基线。2026-11-04 有 3 个端点从 `current_user` 迁到 `admin_user`（`GET /api/grok/register/tasks`、`GET /api/grok/register/tasks/{task_id}` 见 grok.py:39-41、44-47（def 行 40、45）；`GET /api/users` 见 settings.py:958-964（def 行 959））；`GET /api/accounts` 依赖未变，仍计 `current_user`（accounts.py:275-281）。故现口径为 **admin 41 个、current_user 43 个**；总数 97、完全匿名 9、quota_admin 3、API Key 4 不变。这条为什么重要：鉴权依赖计数是暴露面基线，旧数会让人以为 `current_user` 面里还包含 grok 注册任务读接口与用户列表，按旧基线复核会对不上账。

> **2026-11-04 已更正（补充：分母已变，旧总数不可沿用）**：上条之后工作区又扩了路由——`v1.py` 新增 10 条（清单与行号见 §四），`backup.py` 新增 3 条且全为 admin：`GET /api/system/backups`（backup.py:163-164）、`POST /api/system/backup`（backup.py:190-191）、`POST /api/system/restore`（backup.py:226-227），路由已在 main.py 注册。按 `@router` 装饰器实测（2026-11-05 工作区当前状态）：19 个 router 共 **118 条**，其中 admin 62、current_user 32、quota_admin 3、无显式依赖 21（v1 14 条走 `_gateway_key`、auth 4 条公开（login/register/logout/totp_verify）、health 3 条探针）。原文 97 条注明「含别名」，与本处按装饰器计数无法逐条对账，故不推算一个看似能相加的合计数。**可确认的关键事实**：新增的 v1 10 条与 backup 3 条没有一条落到 `current_user` 或完全匿名，故 §2.2「0 处完全无鉴权」不变；但 §2.1/§2.3 的分项数已随工作区变化而不再等于原值。这条为什么重要：端点数是全篇「0 处」「无绕过」结论的分母，分母错了，按旧表复核就会漏掉新增暴露面——其中 restore 是能整库覆盖的高危操作，必须出现在基线里。

---

## 二、分类判定

### 2.1 应当 admin 但只要求登录（读操作暴露全量数据给 viewer）

**4 个端点（按严重度）：**（原表述，2026-11-04 已更正，见下）

> **2026-11-04 已更正（原句保留作历史）**：本节数出的 4 个端点截至 2026-11-04 **全部已处理，现为 0 个**——grok 两个读端点与 `GET /api/users` 收为 `admin_user`（grok.py:40、45，settings.py:959；写侧 settings.py:968、990、1030），`GET /api/accounts` 改为体内按归属收窄（accounts.py:294-297，仍 200 不变）。以下三条明细原样保留作历史，各自紧随其后的更正块给出当前口径与确切行号。这条为什么重要：「4 个未收口」是全篇严重度排序的依据，不更正会让读者把本节继续当成待修清单。

1. **GET /api/grok/register/tasks、GET /api/grok/register/tasks/{task_id}** — grok.py:39、grok.py:44
   - 依赖只有 `current_user`，返回 `SELECT * FROM grok_tasks` 的整行（grok_register.py:115-121、grok_register.py:105-111，`_row()` 直接 `dict(row)`，grok_register.py:23-24）
   - `grok_tasks` 表含 `proxy`（代理地址，常见形态带 `user:pass@`）、`email_provider`、`payload_json`（注册载荷）、`result_json`（注册产物）——db.py:227-243
   - 写侧全部要求 admin（grok.py:26、52、62），唯独读侧放给 viewer，且**前端没有任何页面调用它**（`apps/web` 全局 grep 无调用点），属于「无业务理由的暴露面」（原表述，2026-11-04 已更正，见下）

> **2026-11-04 已更正（原句保留作历史）**：两处过期——其一，上文第 1 条子项「依赖只有 `current_user`」与本句「唯独读侧放给 viewer」：两个 GET 现均为 `Depends(admin_user)`（grok.py:39-41、grok.py:44-47，def 行 40、45，grok.py:38 注释「读侧与写侧同一口径：仅 admin」）；其二，「前端没有任何页面调用」：现有一处消费者 `apps/web/components/common/settings/GrokRegisterTasks.tsx:85`（`grokRegisterApi.list` 调 `GET /api/grok/register/tasks`，4 秒轮询见 GrokRegisterTasks.tsx:98-105），渲染门禁在 `apps/web/components/common/settings/ProvidersSection.tsx:721`（`p.id === 'grok-register' && isAdmin && <GrokRegisterTasks />`），viewer 不挂载组件、不发请求；`GET /api/grok/register/tasks/{task_id}` 仍无前端消费者。这条为什么重要：旧句双向误导——既夸大了当前暴露面（已收 admin），又会让后来者把列表端点当「无消费者可随意删改」，漏掉 admin 页面对它的轮询依赖。

2. **GET /api/accounts** — accounts.py:275-313
   - `current_user`，SQL 只按 provider/realm 过滤，**无归属收窄**，返回全部上游账号：`file`、`uid`、`nickname`、`enterprise_id`、`credits`、`note`、`expires_at` 等（api_utils.py:129-160，token 明文未回传）
   - 写操作全为 admin（accounts.py:316 起），读写口径不一致
   - 前端 dashboard 对 viewer 也调用（dashboard/page.tsx:73），即 viewer 必然拿到全量账号资产清单（原表述，2026-11-04 已更正，见下）

> **2026-11-04 已更正（原句保留作历史）**：`GET /api/accounts` 依赖仍是 `current_user`（accounts.py:275-281，def 行 276），但上文子项「SQL 只按 provider/realm 过滤，**无归属收窄**，返回全部上游账号」与本句「viewer 必然拿到全量账号资产清单」均不成立——accounts.py:294-297 对 `role != "admin"` 追加 `WHERE owner_user_id = <username>`，viewer 经 `dashboard/page.tsx:73` 的调用照常 200，返回面却只有自己名下账号（无主 NULL 行不可见）。写侧全 admin（accounts.py:316 起）不变，读写口径已通过体内归属对齐（HTTP 200 而非 403）。这条为什么重要：旧结论会让人以为 dashboard 的 viewer 视图在吐全量资产清单，据此可能错误地给 viewer 关页，或再做一轮「加 isAdmin 门」的重复改动。

3. **GET /api/users** — settings.py:956-963
   - `current_user`，返回 `SELECT username, role FROM users` 全量行
   - 用户名+角色是账号枚举信息（配合撞库/定向登录），而同一文件的增删改都要求 admin（settings.py:966、988、1028）
   - 前端 settings 页对 viewer 也调用（settings/page.tsx:925）（原表述，2026-11-04 已更正，见下）

> **2026-11-04 已更正（原句保留作历史）**：`GET /api/users` 已为 `Depends(admin_user)`（settings.py:958-964，def 行 959），上文子项「`current_user`，返回 `SELECT username, role FROM users` 全量行」仅 admin 请求时成立；本句「前端 settings 页对 viewer 也调用（settings/page.tsx:925）」也已过期——该调用现位于 `if (isAdmin)` 块内（settings/page.tsx:877-928，调用行 926，:925 注释「用户列表是 admin-only：viewer 不请求，否则开一次设置页就多吃一次 403」）。这条为什么重要：旧句会让读者以为 viewer 打开设置页就会吃到 403 并据此排查前端，实际后端已收 admin、前端已有门禁，双向都已闭环，无需再动。

**观察项 7 个（只要登录，内容为运维/全局数据，建议复核是否收紧）：**

| 端点 | 证据 | 内容 |
|---|---|---|
| GET /api/accounts/capabilities | accounts.py:247-248 | 各 provider 账号管理能力矩阵 |
| GET /api/security/guard | security.py:93-104 | 网络守卫开关与封禁清单 |
| GET /api/grok/register/worker | grok.py:72-73 | 注册 worker 状态；当前仍 `current_user`（grok.py:72-73），建议改 `admin_user`，依据：唯一消费方 `GrokRegisterTasks.tsx:86` 已 admin-only。**待落地**（2026-11-05） |
| GET /api/system/versions | system.py:105-113 | 版本 + 上游账号计数 |
| GET /api/pricing/books | pricing.py:38-45 | 全局价格表 |
| GET /api/pricing/resolve | pricing.py:127-133 | 单模型价格解析 |
| GET /api/provider-credentials | provider_credentials.py:63-67 | 凭据条目（secret 掩码，provider_credentials.py:29） |

### 2.2 应当登录但完全无鉴权

**0 处。** 所有返回业务数据的 `/api/` 端点都挂了 `current_user`/`admin_user`/`quota_admin`；完全匿名的 9 项全部归入 2.3 有意公开。路由注册处（main.py:285-287）未在 `include_router` 层加任何全局依赖，也就没有「加了依赖却被 include 绕过」的风险面。

### 2.3 有意公开清单

| 端点 | 为什么公开是合理的 | 证据 |
|---|---|---|
| POST /api/login | 登录入口本身必须匿名；带 IP+用户名失败节流与弱口令告警 | auth.py:85-89、main.py:66-73 |
| POST /api/logout | 幂等，只删请求携带的自身 cookie，无会话也返回 ok，不泄露数据 | auth.py:121-132 |
| GET /api/health、GET /api/health/ready | 容器/编排探针不带会话；返回体只含子系统状态与 readiness 枚举，不回传 base_url/凭据/库路径 | health.py:1-11、health.py:42-45、health.py:94-117 |
| GET /healthz | 同上，且已刻意不返回数据库路径 | main.py:289-292 |
| GET /（静态资源挂载、JSON 兜底） | 前端 SPA 产物必须可匿名下载；登录由页面自身处理（前端 401 拦截器跳 /login） | main.py:294-303、lib/api.ts:67-85 |
| /docs、/redoc、/openapi.json | 生产默认关闭（`enable_docs=False`），本地调试显式开 | main.py:164-167、config.py:89、config.py:137 |
| POST /v1/*（原 4 个端点，2026-11-04 已更正：现 14 条，见 §四） | 网关面向下游程序，用 API Key 而非会话鉴权是协议设计（OpenAI/Anthropic 客户端只发 `Authorization`/`x-api-key`） | v1.py:67-96 |
| （配置项）`allow_anonymous` 旁路 | DB `gateway.allow_anonymous` 或 `UNIFIED_ALLOW_ANONYMOUS_GATEWAY` 打开时 /v1 免 key；**默认关**，脏值按拒绝处理 | v1.py:50-59、config.py:131 |

### 2.4 权限判断写在函数体里而不是依赖里（最容易漏，单独列）

| 端点 | 依赖 | 函数体内的权限逻辑 | 证据 |
|---|---|---|---|
| GET /api/keys | current_user | `if role == admin` 查全表，否则按 `owner_user_id` 收窄；NULL 归属对非 admin 不可见 | keys.py:36-46 |
| GET /api/tokens | current_user | 同上双分支 | tokens.py:59-72 |
| GET /api/logs | current_user | `_owner(user)` 决定是否加归属条件；带 `key_id` 时 `_assert_key_owned` 校验归属 | logs.py:235、logs.py:238-246、logs.py:43-52 |
| GET /api/stats/summary、/api/stats/daily | current_user | `_owner()` + `_owned()` 收窄 request_logs/billing_ledger/api_keys | stats.py:21-32、stats.py:60-63、stats.py:140-142 |
| GET /api/quota/policies | current_user | `_scope_key()` 校验 key 归属，`_owner()` 收窄策略集 | quota.py:175、quota.py:179-192、quota.py:106-114、quota.py:46-53 |
| GET /api/quota/status | current_user | 同上 | quota.py:335、quota.py:341-354 |
| GET /api/quota/alerts | current_user | 同上；非 admin 只看自有 key 告警 | quota.py:371、quota.py:382-392 |
| GET /api/security/login-failures | current_user | `is_admin` 分支：admin 看全站行含 IP，非 admin 只回聚合数字 | security.py:108-168 |
| GET /api/audit-logs、/api/security/audit | current_user | 非 admin 强制 `actor = 自己`，跨 actor 查询直接 403 | security.py:175-193 |

这 9 个端点的共同风险：**收窄逻辑靠人肉记得写**，新增过滤参数（如未来加 `actor=`、`provider=`、跨 key 查询）时容易漏掉归属条件。目前逐个核对未发现可越权读取他人数据的路径（quota 的 `X-Api-Key` 头走 `_key_by_token`+`_assert_owned`，quota.py:56-65、quota.py:46-53；日志 `key_id` 走 `_assert_key_owned`，logs.py:43-52）。

---

## 三、前端实际调用面交叉核对

映射基准：`apps/web/lib/api.ts`（388 行）、`apps/web/lib/quota-api.ts`、`app/(main)/settings/RuntimeConfigSection.tsx:40`。

**结论：没有「前端调了、后端要求 admin、viewer 必得 403」的调用（0 处）。** 每个 admin 端点的调用点都做了角色门禁：

| admin 端点 | 前端调用点 | 门禁证据 |
|---|---|---|
| POST /api/keys、PATCH、DELETE | keys/page.tsx:92、110、186 | 渲染门禁 keys/page.tsx:164、211（`isAdmin &&`） |
| POST /api/tokens 等 | TokensPanel.tsx:95 | 组件级早退 TokensPanel.tsx:58（`if (!isAdmin)`） |
| POST /api/logs/clear | logs/page.tsx:258 | logs/page.tsx:251（`isAdmin &&`） |
| POST /api/accounts/* 写操作、/api/auth/start、/api/auth/poll | accounts/page.tsx:831-905、OAuthDialog.tsx:81/94、AddAccountDialog.tsx:105、BatchOAuthDialog.tsx:178 | 操作列只在 `isAdmin` 下渲染（accounts/page.tsx:1172、1242、1273-1298）；顶栏入口 accounts 页与 ManagementBar.tsx:506（`if (isAdmin)`） |
| PATCH /api/providers/{id} | ProvidersSection.tsx:129 | 全部按钮 `disabled={!isAdmin}`（ProvidersSection.tsx:303-553、428-553） |
| GET/POST /api/settings/gateway | ProvidersSection.tsx:117-120、145 | 读取用 `if (!isAdmin) return`（ProvidersSection.tsx:113-121，注释明说「viewer 不该为此拿一次 403」）；开关 `disabled={!isAdmin}`（ProvidersSection.tsx:781） |
| GET/POST /api/settings/upstream、model-map | settings/page.tsx:878-879 | 加载包在 `if (isAdmin)` 内（settings/page.tsx:877-928）；保存按钮 `disabled={!isAdmin}`（settings/page.tsx:1304-1398） |
| POST /api/settings/upstash/test | settings/page.tsx:1468 | `disabled={!isAdmin}`（settings/page.tsx:1464） |
| GET/POST /api/settings/runtime-config* | settings/RuntimeConfigSection.tsx:40 | `if (!isAdmin) return` 加载门禁 + `return null` 渲染门禁（RuntimeConfigSection.tsx:88-92） |
| POST /api/users、PATCH、DELETE | settings/page.tsx:1874-1949 | `isAdmin &&` 渲染（settings/page.tsx:1106） |
| POST/DELETE /api/provider-credentials | ProvidersSection.tsx:217 | `disabled={!isAdmin}` / `isAdmin &&`（ProvidersSection.tsx:614-702、770） |
| POST /api/pricing/books、/api/pricing/import | pricing/page.tsx:131、154、165 | 操作列三元门禁 pricing/page.tsx:182-184；导入按钮在 isAdmin 预览分支内 |
| /api/sub2api/* 全部读写 | quota/page.tsx:161-371 | 页面级 `if (!isAdmin)` 早退，viewer 直接看说明（quota/page.tsx:95-113） |
| /api/quota/policies、status、alerts | lib/quota-api.ts:88-102 | /quota/policies 页 `if (!isAdmin)` 早退（quota/policies/page.tsx:90-104，注释「避免三块 403 报错」） |
| gatewayApi.get | ProvidersSection.tsx:117 | `if (!isAdmin) return`（ProvidersSection.tsx:116） |

**反向问题（宽后端 + 严前端）：** 上面 quota 读接口后端其实允许 viewer 自查（按归属收窄），但前端一律 admin-only 不请求——属于有意收紧，不构成 bug。

**仅有的 1 处潜在口子：** `ManagementBar.tsx:317-326` 监听 `workbuddy-manager:open-add-account` 自定义事件时**不判 `isAdmin`** 直接 `setAddOpen(true)`，viewer 打开添加账号对话框后发起 `POST /api/auth/start` 会得 403。当前 `apps/web` 内没有该事件的 `dispatch` 调用方（grep 仅命中监听器本身），所以现实中触发不了；一旦将来有入口 dispatch 就会变成「点了就报错」。

> **2026-11-05 复核（时点：当前工作树未提交，原句保留作历史）**：`ManagementBar` 不再是纯展示层——个人信息弹窗已挂载 2FA 面板 `TotpSection`（import 见 `ManagementBar.tsx:47`，真实 JSX 渲染点 `:628`，嵌在弹窗内容里、非条件包死）。但这**不改变**本条判定：`workbuddy-manager:open-add-account` 仍无任何派发方——全 `apps/web` grep `dispatchEvent` 只命中另一事件 `workbuddy-manager:accounts-changed`（accounts/page.tsx:439、AddAccountDialog.tsx:142、OAuthDialog.tsx:113、:201、BatchOAuthDialog.tsx:271）；该事件的监听与移除仍在 `ManagementBar.tsx:323`、`:325`，没有派发调用。另核：`TotpSection` 调用的 4 个端点均为 `Depends(current_user)`（auth.py:309、329、355、397），viewer 不会因此吃 403，「前端必 403 = 0」不变。这条为什么重要：旧句若被读成「整个 ManagementBar 没有功能挂载、可当静态层忽略」，会在别处重复实现入口或判定它无业务价值；反过来，若把 `TotpSection` 挂载当成「该事件已有派发方」，会误报这处 403 引信已拆，或给监听器加一道多余的角色门。

**前端未调用但后端暴露的端点：** `/api/grok/register/*`（全部 6 个）、`/api/metrics`、`/api/grok` 外的其余无——其中 grok 的 3 个读端点是纯暴露面，没有任何前端消费者。（原表述，2026-11-04 已更正，见下）

> **2026-11-04 已更正（原句保留作历史）**：grok 读端点已有前端消费者——`apps/web/components/common/settings/GrokRegisterTasks.tsx:85` 调 `GET /api/grok/register/tasks`、`:86` 调 `GET /api/grok/register/worker`（组件渲染受 `ProvidersSection.tsx:721` 的 `isAdmin` 门禁约束，viewer 不挂载、不发请求）；仍无消费者的只有 `GET /api/grok/register/tasks/{task_id}`。同时后端读侧已收为 `admin_user`（grok.py:39-41、44-47，def 行 40、45；`worker` 除外，grok.py:72-73 仍 `current_user`）。`/api/metrics` 无消费者部分不变。这条为什么重要：「3 个读端点零消费者」若被当真，会把有 admin 消费方（4 秒轮询）的列表接口当成可随意删改的纯暴露面，或误判 viewer 会因它产生 403。

---

## 四、/v1 侧核对

| 检查项 | 结果 | 证据 |
|---|---|---|
| 路由总数 | 4 个：`POST /v1/chat/completions`、`POST /v1/messages`、`POST /v1/responses`、`GET /v1/models`；**本服务不提供 /v1/images、/v1/videos、/v1/embeddings 等**（图片/视频由各 adapter 在上游侧转发，不是本服务路由） | v1.py:639-717，全仓 grep `@router\.` 仅这 4 条 `/v1` 路由（原表述，2026-11-04 已更正，见下） |
| 是否都过 API Key | **是。** 3 个 POST 全走 `_proxy()`，第一行准入即 `_gateway_key()`；GET /v1/models 在返回模型列表前显式调 `_gateway_key()`（原数量，2026-11-04 已更正：现 9 个 POST，见下） | v1.py:402（`key = _gateway_key(request, conn)`）、v1.py:654-659（models 校验 :658，见下） |
| 是否有绕过鉴权中间件的路径 | 无中间件可绕——本应用**没有全局鉴权中间件**，/v1 鉴权是每端点内联调用。4 个端点逐一核对均已调用（原数量，2026-11-04 已更正：现 14 条，见下）；未匹配的 /v1 路径进框架异常处理器只回协议壳 404/405，不泄露数据 | main.py:256-266（原引用 249-259，2026-11-04 已更正） |
| 鉴权头口径 | 支持 `Authorization: Bearer`、裸 token、`x-api-key`；两头同带必须同值，冲突直接 401 `conflicting_auth_headers`；无头时查匿名开关，关闭则 401 | v1.py:71-88 |
| key 校验 | sha256 哈希查 `api_keys`，`enabled=1` 才通过，不匹配 401 `invalid_api_key` | v1.py:89-96 |
| 配置型旁路 | `_anonymous_allowed()`：DB `gateway.allow_anonymous` 优先，回落 `UNIFIED_ALLOW_ANONYMOUS_GATEWAY`，**默认 False**；库内脏值按关闭处理 | v1.py:50-59、config.py:131 |
| 免 key 后的影响面 | 匿名请求 `key.id = None`，配额/失败限流直接跳过（无归属维度），但仍会写 request_logs | v1.py:143-144、v1.py:175-176 |

> **2026-11-04 已更正（原表述保留作历史）**：`/v1` 路由已从 4 条扩到 **14 条**——基线 4 条在 `v1.py:639`、`:644`、`:649`、`:654`；新增 10 条为 `POST /v1/embeddings`（:672）、`POST /v1/images/generations`（:677）、`POST /v1/images/edits`（:682）、`GET /v1/images/tasks/{task_id}`（:687）、`POST /v1/videos`（:692）、`GET /v1/videos/{task_id}`（:697）、`GET /v1/media/{name}`（:702）、`POST /v1/messages/count_tokens`（:707）、`POST /v1/rerank`（:712）、`GET /v1/usage`（:717）。故「本服务不提供 /v1/images、/v1/videos、/v1/embeddings」与「全仓只有 4 条 /v1 路由」都不成立。**结论「全部经过 API Key 校验、无绕过路径」仍成立**：9 条 POST 走 `_proxy()`（准入第一行 `v1.py:402`），4 条 GET 走 `_proxy_get()`（`v1.py:588`），`GET /v1/models` 在 `v1.py:658` 直调 `_gateway_key()`。这条为什么重要：原结论是从「只有 4 个端点」逐一核对推出来的，端点扩到 14 条后照旧引用会以为 images/videos/embeddings 根本不经过本服务，从而漏掉这 10 条路径的鉴权与配额复核。

---

## 五、建议修复（按严重度排序，仅建议，未动手）

### 高

- **`POST /api/system/restore`：整库覆盖型端点，新增于本轮，基调为「高危但有护栏」**（`apps/control/app/routers/backup.py:226-227`）。理由：一次恢复会把整个控制面回退到某个旧库状态，误触发或选错备份的后果不是「某个页面数据不对」，而是全库回退。**已具备的四层缓解**：① 仅 `admin_user`（backup.py:227）；② `confirm=true` 必填，缺省 `False` 直接 400（backup.py:230-231）；③ 恢复前自动给当前库出快照，快照失败即中止、当前库一个字节不动（backup.py:259、296-304）；④ 归档校验（zip + manifest + 成员 sha256 + `PRAGMA integrity_check`，backup.py:4-6、242）→ 原子替换 `os.replace`（backup.py:331-345，`os.replace` 在 :339）→ 恢复后 `core.selfcheck` 不通过就把字节快照写回（backup.py:356-362；WAL 清理失败也回滚，:350-354）。另有跨进程互斥锁（backup.py:240、71-89，冲突 409 在 :82）与成功审计（backup.py:268-274）。结论：**不是欠修项，也无需重新发明防护**；若要再加强，只剩产品取舍（如二次确认串输入备份名、限恢复时间窗）。这条为什么重要：只写「高危入口」而不写护栏，会让后来人把它当漏洞重做一遍，或在已有保护之上再叠一层互斥/备份逻辑。

1. **`GET /api/grok/register/tasks` 与 `GET /api/grok/register/tasks/{task_id}` 改为 `admin_user`**（grok.py:39-49）。理由：返回整行含 `proxy`、`payload_json`、`result_json`（db.py:227-243），可能携带代理账号口令与注册产物，而写侧本就全 admin，且前端零调用。若要保留给非 admin 查看，至少对这三列做掩码。（2026-11-04 已更正：已落地，见下）

> **2026-11-04 已更正（原建议保留作历史）**：已落地——两个 GET 现为 `Depends(admin_user)`（grok.py:39-41、grok.py:44-47，def 行 40、45，grok.py:38 注释「读侧与写侧同一口径：仅 admin」）。掩码方案未采用（直接整端点收 admin，`proxy`/`payload_json`/`result_json` 对 viewer 全部不可见）；原理由里「前端零调用」已过期——消费方 `GrokRegisterTasks.tsx:85` 已随 `ProvidersSection.tsx:721` 的 `isAdmin` 门禁对齐，无 viewer 裸调用。这条为什么重要：该建议已不再是待办，继续按未修项跟踪会重复排期，也会让复核者误以为 grok 读侧仍未收口。

2. **`GET /api/accounts` 收紧为 `admin_user` 或按需脱敏**（accounts.py:275-313）。理由：写操作全 admin，读侧无归属收窄，全量 uid/昵称/积分/备注对 viewer 可见。**注意联动**：dashboard 对 viewer 也调用它（dashboard/page.tsx:73），收紧时需同步给前端加 `isAdmin` 门或提供降级数据（只回聚合数字），否则会新造「点了就报错」。（2026-11-04 已更正：已落地，见下）

> **2026-11-04 已更正（原建议保留作历史）**：已落地，但方式不是改 `admin_user`，而是体内按归属收窄——accounts.py:294-297 对 `role != "admin"` 追加 `WHERE owner_user_id = <当前用户名>`（admin 全量、非 admin 只看自己名下，无主 NULL 行不可见），HTTP 仍 200 不是 403。因此**原「注意联动」的处置不再需要**：`dashboard/page.tsx:73` 对 viewer 的调用不用加 `isAdmin` 门、也不用降级数据，viewer 照常 200、只看到自己名下账号，不会新造「点了就报错」。这条为什么重要：若照旧联动句给 dashboard 加门，会把 viewer 本可正常展示的账号数据页错误地关掉。

3. **`GET /api/users` 收紧为 `admin_user`**（settings.py:956-963）。理由：全量用户名+角色枚举，而同文件写操作全 admin。**联动**：settings/page.tsx:925 对 viewer 也调用，需一并加门。（2026-11-04 已更正：已落地，见下）

> **2026-11-04 已更正（原建议保留作历史）**：已落地——`GET /api/users` 现为 `Depends(admin_user)`（settings.py:958-964，def 行 959）；联动也已落地：`settings/page.tsx:926` 的 users 调用在 `if (isAdmin)`（settings/page.tsx:877-928）内，:925 注释「用户列表是 admin-only：viewer 不请求，否则开一次设置页就多吃一次 403」。这条为什么重要：「需一并加门」已是完成态，按待办跟踪会重复排期。

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
