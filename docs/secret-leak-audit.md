# 密钥/凭据泄露面审计（只读）

- 日期：2026-11-04
- 方式：只读代码审阅，未改代码、未跑测试、未起服务
- 范围：`apps/control/app/`（错误响应、日志、数据库、审计导出）+ `apps/web/`（前端存储与渲染）+ `scripts/backup_core.py`（备份包）

## 结论

**存在真实的、可执行的透传路径，但没有发现「无条件泄露 u2a_ 密钥」的路径**：`u2a_` 网关密钥在错误响应、日志、数据库、前端四处均被掩码或哈希。最严重的问题是 `api_utils.mask_text()` 只掩 `u2a_` 前缀这一**系统性掩码盲区**——每条上游错误都会经过它流向下游客户端与 `/api/logs`，若上游错误体/base_url 中携带非 `u2a_` 形态的凭据（`sk-`、JWT、`?key=` 等），将原样透出；其次是适配器把 `base_url` 拼进对外错误文案（base_url 允许带 query，未做校验）。明文落盘类（上游 secret、备份含凭据表）属本地文件面，有既定权限约束但无加密。

## 总表

| 面 | 风险点 | 是否可控 | 证据 | 建议 |
|---|---|---|---|---|
| 错误回显 | `mask_text` 只掩 `u2a_` 前缀，上游错误原文（`upstream_message`/`message`/`detail`）经它清洗后直接进下游响应 | 部分可控：有掩码+200 字符截断，但掩码模式单一 | `apps/control/app/api_utils.py:43-49`（`_KEY_IN_TEXT = u2a_ + [A-Za-z0-9_-]{4,}`）；`apps/control/app/gateway_errors.py:131-136`、`206-221`、`224-249`（均只调 `mask_text` + `_MAX_DETAIL=200`） | 上游文本改走更强 redact（如复用 `model_catalog.py:26-30` 的 `_SENSITIVE_KEYS`/`redact()` 口径，覆盖 `sk-`/`Bearer`/JWT/URL query） |
| 错误回显 | 适配器把 `self.base_url` 与 `exc!r` 拼进 `ProviderError.message` → 经 `from_provider_error` 到下游；`base_url` 无 query 禁止校验（仅 upstash 有） | 不可控 | `apps/control/app/adapters/opencode.py:99`、`muse.py:159`、`zcode.py:250`、`gptgrok.py:309`；`apps/control/app/gateway_errors.py:209`；`apps/control/app/registry.py` 无 urlparse/query 校验（grep 无命中）；对照 `routers/settings.py:724` 仅 upstash 禁 query | 连接失败文案去掉 `base_url`/`exc!r`，或入库/出错前清洗 URL query |
| 错误回显 | upstash 探测错误体：URL 禁 query/userinfo、snippet 取 `error/message/detail` 后 `mask_text`+截断、admin 依赖 | 可控 | `apps/control/app/routers/settings.py:720-726`、`817-826`、`860-861`、`911-913`、`919-926`（`Depends(admin_user)`） | 无需改；若要防上游回显其自身 token，随第 1 条统一 redact |
| 错误回显 | 管理面 `HTTPException(detail=str(exc))` 回显本地异常文本/服务器路径 | 可控（admin-only、内容为本地错误） | `apps/control/app/routers/grok.py:48,56,58,66,68`；`pricing.py:168,174`（404 含服务器绝对路径）；`main.py:235-237`（`ProviderError` → `{"detail": exc.message}` 不过掩码） | 低优先：`pricing.py:168` 去掉绝对路径；`main.py:237` 统一过 `mask_text` |
| 错误回显 | 鉴权头冲突/缺失错误不回显头值；框架 404/405 固定文案 | 可控 | `apps/control/app/routers/v1.py:59-80`（66/75 只给提示语）；`gateway_errors.py:62-70,115-128` | 无需改 |
| 日志面 | `log_access` 只记 method/path/status/duration/request_id，不含 query/头/体；uvicorn 访问日志过 `_AccessLogRedactor` | 可控 | `apps/control/app/observability.py:9-10,66-77`（口径同源说明）；`main.py:55-63`；`api_utils.py:52-56`（`mask_access_args`） | 无需改 |
| 日志面 | JSON formatter 对 message/exc/整行统一 `mask_text`——但同样只掩 `u2a_`，异常里 httpx request repr 的 URL（非 u2a_ query token）不脱敏 | 部分可控 | `apps/control/app/observability.py:26,107,120,126` | 随第 1 条扩展 redact 模式 |
| 日志面 | 上游响应片段落本地日志：`resp.text[:200]` 可能含上游返回的 token，仅过 `mask_text` | 不可控（本地文件面） | `apps/control/app/adapters/client.py:211`（`上游 %s%s 返回 %s：%s`，含 `resp.text[:200]`） | 写日志前对响应片段套 `model_catalog.redact()` 同级清洗 |
| 日志面 | 全仓未发现把 `Authorization`/`api_key`/`u2a_` 完整值打进日志的 `logger.*` 调用；客户端池 key 用指纹 | 可控 | grep `logger.*(header\|Authorization\|token\|secret)` 仅 `adapters/codar.py:149`、`routers/settings.py:248`（均为错误文案，非凭据）；`adapters/client.py:89-92`（sha256 指纹） | 无需改 |
| 数据库 | `provider_credentials.secret` 明文落库（有注释与 S-05 文件权限约束，无加密） | 本地面可控（权限），加密无 | `apps/control/app/db.py:244-249`（`secret TEXT ... 明文落库`）、`db.py:312`（S-05 权限说明） | 中期：可选应用层加密；至少保持 `data/` 权限与备份保管纪律 |
| 数据库 | `sub2_accounts.api_key` 明文落库；对外只出 `has_api_key` | 本地面可控 | `db.py:100-105`；`adapters/sub2api_store.py:36-51`（`_account_public` 只给 `has_api_key`）、`301-338`（明文仅内部方法） | 同上 |
| 数据库 | `sessions.token` 明文存主键，可被拿库者劫持会话（密码是 pbkdf2） | 本地面可控 | `db.py:14-19`；`security.py:46-56`（原样插入）；对照 `security.py:14-21`（密码哈希） | 低：session token 存 sha256 |
| 数据库 | `request_logs.error` 写入前过 `mask_text`（含上游 body `[:200]` 与 `str(exc)`），但盲区同第 1 条；`/api/logs` 原样返回该列 | 部分可控 | `routers/v1.py:212-217`（`mask_text(error)`）、`376-383,393,447,419`（写入点）；`routers/logs.py:188-218`（`error` 直出） | 随第 1 条统一 |
| 数据库 | `billing_ledger` 无 error 原文列，`note` 为固定枚举；`accounts.meta_json` 对外经白名单过滤 | 可控 | `db.py:156-175`（`note` 枚举说明）；`api_utils.py:76-107`（`public_account` 白名单）；`routers/accounts.py:187` | 无需改 |
| 数据库 | demo 模式写 `request_logs.error` 不过掩码 | 可控（localhost-only 演示） | `demo.py:786-815`（`error=body[:200]` 未 mask）；`main.py:229-232`（演示模式只收本机） | 低：补 `mask_text` 保持口径一致 |
| 审计导出 | 审计端点非 admin 只见自己；写审计只记字段名/路径，不记值 | 可控 | `routers/security.py:173-212`；`security.py:37-43`；`routers/provider_credentials.py:18-19,58-60`；`routers/settings.py:486-519`（reveal 只记 path） | 无需改 |
| 审计导出 | `/api/logs` 只出 `key_prefix` 掩码（`u2a_abcd****`），不出完整密钥 | 可控 | `routers/logs.py:56,188-218`；`key_prefix.py`（前缀+掩码规则） | 无需改 |
| 审计导出 | 密钥列表只存 sha256、明文仅创建时返回一次；配额密钥必须走头、禁 URL | 可控 | `routers/keys.py:60-63,87`（`只此一次`）；`routers/quota.py:74-81,106-111` | 无需改 |
| 审计导出 | 备份 zip = 全库 `VACUUM INTO` 快照，**含 `provider_credentials`/`sub2_accounts`/`sessions` 凭据表**；manifest 只含表名与行数统计，无值 | 本地面可控：离线脚本、无 HTTP 端点、`data/backups` 已 gitignore | `scripts/backup_core.py:147-169`（VACUUM INTO）、`331-362`（manifest 仅统计）、`365-369`（打 zip）；`scripts/backup.ps1:8-19`；`apps/control/app` grep `backup\|VACUUM` 无命中 | 备份文件按机密保管/加密存储；文档明确其含凭据 |
| 前端 | 无任何 localStorage/sessionStorage 存密钥：存储项仅为显示偏好、语言、dock、`wb-me` 用户缓存 | 可控 | `apps/web/lib/display-prefs.ts:24-32`、`lib/i18n/config.ts:58`、`lib/auth-context.tsx:14-25`（仅用户元数据） | 无需改 |
| 前端 | playground 密钥输入 `type="password"`，保存进 `sessionStorage`（`u2a_playground_key`，会话级、非持久）；仅经请求头发给网关，不进 URL | 可控（用户手填、可清除） | `apps/web/app/(main)/playground/page.tsx:40,410-435,683-694` | 可选：加「清除密钥」提示或不落存储 |
| 前端 | 凭据列表渲染 `secret_masked`；settings 出参经 `_mask_tree`；无 `?key=` 拼 URL、无凭据 `console.log` | 可控 | `apps/web/lib/providers.ts:26-38`；`components/common/settings/ProvidersSection.tsx:632-647`；`routers/settings.py:61-80,441-474`；grep `router.push(...?` 仅 nginx 配置命中 | 无需改 |
| 前端/边缘 | nginx 查询串对敏感参数名与 `u2a_` 值双重脱敏，`error_log crit` 防原始请求行进 error.log | 可控 | `apps/web/nginx.conf:6-18,183-200` | 无需改 |

## 已确认可控、无需改

1. **u2a_ 密钥四条链路均闭合**：错误响应（`gateway_errors.py:134,209-211` 掩码+200 截断）、日志（`observability.py:107,120,126` + `main.py:55-63`）、数据库（`keys.py:61` 只存 sha256，明文仅创建返回一次 `keys.py:87`）、前端（type=password、无持久化）。
2. **鉴权信息不进日志**：`log_access` 字段白名单（`observability.py:66-77`）、访问日志过滤器（`main.py:55-63`）、客户端池 key 指纹化（`client.py:89-92`）；全仓无打印 header/完整 key 的 `logger.*`。
3. **upstash 探测**：禁 query/userinfo/非 https（`settings.py:720-726`）、snippet 掩码+截断（`817-826`）、admin 权限（`919-926`）、secret reveal 仅 admin 且审计只记路径（`503-519`）。
4. **出参掩码口径统一**：`provider_credentials` 出 `secret_masked`/`has_secret`（`provider_credentials.py:22-36`）、`sub2` 出 `has_api_key`（`sub2api_store.py:51`）、settings 出 `_mask_tree`（`settings.py:470-474`）、`/api/logs` 出掩码前缀（`logs.py:56`）。
5. **审计写入不记值**：凭据审计只记字段名（`provider_credentials.py:18-19`），审计端点非 admin 自见（`security.py:187-193`）。
6. **`billing_ledger` 无上游原文列**（`db.py:156-175`）、`accounts.meta_json` 白名单过滤（`api_utils.py:76-107`）、`/healthz` 不暴露库路径（`main.py:282-285`）。
7. **备份无远程暴露面**：备份仅离线脚本，后端代码无备份/下载路由（`apps/control/app` grep `backup|VACUUM INTO|zipfile` 零命中）。

## 建议修复（按严重度）

1. **【高】扩展 `mask_text` 的脱敏模式**（`api_utils.py:43-49`）：当前只匹配 `u2a_` 前缀，导致上游错误原文、日志、`request_logs.error` 对 `sk-`/`Bearer`/JWT/URL query 形态凭据零防护。建议复用 `model_catalog.py:26-75` 已有的 `_SENSITIVE_KEYS` + `redact()` 口径，让 `gateway_errors.py`、`observability.py`、`v1.py` 写库三处同步受益。这是唯一贯穿「错误响应 + 日志 + 数据库 + /api/logs」四个面的根因。
2. **【中】适配器连接失败文案去掉 `base_url` 与 `exc!r`**（`opencode.py:99`、`muse.py:159`、`zcode.py:250`、`gptgrok.py:309`）：`base_url` 允许携带 query（无校验），httpx 异常文本可含完整 URL，二者会经 `from_provider_error` 直接到达下游客户端。
3. **【中】上游响应片段落日志前清洗**（`client.py:211`）：`resp.text[:200]` 是上游原文，仅过 `u2a_` 掩码，本地日志文件可能残留上游 token。
4. **【中低】`main.py:237` 的 `ProviderError` 全局处理器不过掩码**：管理面各测试端点的 `exc.message` 直出，建议统一 `mask_text(detail)`。
5. **【低】demo 路径写库补掩码**（`demo.py:793`），与 `v1.py:216` 口径对齐（当前仅 localhost 演示可达）。
6. **【低】会话 token 哈希化**（`db.py:14-19`、`security.py:52`），避免拿库即可劫持在线会话。
7. **【低】凭证明文面**：`provider_credentials.secret`、`sub2_accounts.api_key` 明文落库 + 备份 zip 含凭据表——短期靠既定的 `data/` 权限与备份保管纪律（S-05），中期可评估应用层加密；`pricing.py:168` 的 404 建议不回显服务器绝对路径。
8. **【可选】playground 密钥存储**（`playground/page.tsx:435`）：会话级 `sessionStorage` 可接受，可补「清除已存密钥」入口。
