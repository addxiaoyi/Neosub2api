# Neosub2api 安全与合规审计

> 审计方式：只读代码审查 + 临时数据库运行时验证（未改动仓库业务代码，未触碰 `data/unified.db`）。
> 快照时间：2026-11-02 17:40–17:55（Asia/Shanghai）。
> 说明：`apps/control/app` 与 `apps/web` 当时有并行会话在改动（settings.py / v1.py / sub2api.py 17:36，`adapters/trae.py` 17:44，`adapters/__init__.py` 17:46 已引用尚不存在的 `qoder.py`，导致该快照下应用无法导入启动）。并行中间态不计入缺陷，但会影响复现，见「待确认」第 3 条。

## 结论

整体风险等级：**高（High）**；若按仓库自带的 `docker-compose.yml` 默认值直接暴露到公网，则为 **critical**。

最严重的三个问题：

1. **任意 viewer 账号可读取全部上游 API Key 明文**（`GET /api/settings/upstream` 只要求登录，`_read_config` 把 `provider_config` 原样合并返回）。已用临时库实测：管理员写入 `SECRET-UPSTREAM-KEY-42` 后，viewer 请求 200 且响应体包含该明文。
2. **匿名网关开关存在类型强制错误**：`bool(body.get("allow_anonymous"))` 把字符串 `"false"` 判为真，管理员只要从 API 传入字符串就会静默开放 `/v1` 匿名调用；实测随后无 `Authorization` 的 `/v1/chat/completions` 通过鉴权（返回 503 而非 401）。
3. **默认管理员口令 + 默认网络暴露**：`UNIFIED_ADMIN_PASSWORD` 缺省为 `unified-admin`，`docker-compose.yml` 直接透传该缺省值并把 8010 绑到 `0.0.0.0`；且 `_seed_admin` 只在 users 表为空时插入，事后改环境变量对已有库无效。

紧随其后的是 `base_url` / `upstream_base` 只校验 `http(s)://` 前缀造成的后鉴权 SSRF（可指向 `127.0.0.1`、`169.254.169.254` 等）。

## 审计面覆盖

| 面 | 覆盖情况 |
| --- | --- |
| 认证与会话 | Cookie 属性、session 生命周期、注销、`u2a_` 签发/校验/吊销、匿名开关 |
| 授权 | 逐路由核对 `/api/*` 的 `current_user` / `admin_user`，provider 配置写权限 |
| 注入与输入校验 | SQL 参数化、JSON body 校验、`base_url` SSRF、路径参数 |
| 凭据管理 | 上游 key 落库形态、日志、错误响应、`.env` / `.gitignore` |
| 流式与资源 | SSE 生命周期、超时、断开释放、并发限流 |
| 供应链与合规 | `THIRD-PARTY-NOTICES.md` 与仓库内容、`npm audit --omit=dev` |
| 部署面 | CORS、OpenAPI 暴露、默认口令、错误堆栈、容器与反代 |

## 问题清单

### Critical

| 编号 | 严重度 | 位置 | 可利用条件 | 影响 | 修复建议 |
| --- | --- | --- | --- | --- | --- |
| S-01 | critical | `apps/control/app/routers/settings.py:43-69`、`:72-74`；写入侧 `apps/control/app/registry.py:121-133` | 任意已登录账号（含 `role=viewer`）调用 `GET /api/settings/upstream` | 返回 `provider_config` 中每家上游的 `api_key` 明文，以及 `sub2api` 之外的 `upstream_config`；低权账号可批量窃取上游凭证并直接消耗额度 | 该 GET 改为 `admin_user`；对 `api_key` 只返回 `has_api_key` / 掩码；`_read_config` 增加按字段脱敏，禁止把 `provider_config` 原样回显 |
| S-02 | critical | `apps/control/app/routers/settings.py:129`；消费侧 `apps/control/app/routers/v1.py:18-27`、`:30-35` | 管理员调用 `POST /api/settings/gateway` 时 `allow_anonymous` 传字符串 `"false"`（任何脚本/第三方客户端都可能这样传） | `bool("false") == True`，网关被静默改为匿名可用；`/v1/chat/completions`、`/v1/models` 等无需 `u2a_` 即可调用，且请求记录 `key_id=None`，无法归因/限额 | 用严格布尔解析（仅接受 `true/false/1/0`，其余 400），并加回归测试；保存后校验返回值与实际鉴权行为一致 |

> **2026-11-05 修复状态（原 S-01 判据保留作历史；时点：当前工作树未提交）**：S-01 的两条触发条件均已消失——① `GET /settings/upstream` 现为 `Depends(admin_user)`（settings.py:479-481），viewer 直接 403；② `_read_config` 默认 `reveal=False`、返回 `_mask_tree` 脱敏结果（settings.py:443、472-476、73-87），密钥字段换成 `*_masked` / `has_*` 并移除原字段，不再原样回显 `provider_config`。明文路径只剩 `POST /settings/secret/reveal`，该端点同样 `Depends(admin_user)`、限定 `provider_config` / `upstream` 根、成功与失败都写审计且审计行不含明文（settings.py:505-522）。这条为什么重要：照旧判读会让人以为配置接口在裸奔，从而在别处重复加一层无用的脱敏；真正需要盯的是 reveal 这个显式出口——每调一次就是一次明文外发，该靠审计与最小化调用去管，而不是再补一层读侧掩码。

### High

| 编号 | 严重度 | 位置 | 可利用条件 | 影响 | 修复建议 |
| --- | --- | --- | --- | --- | --- |
| S-03 | high | `apps/control/app/config.py:43-44`；`apps/control/app/main.py:18-29`；`docker-compose.yml:8`、`:17-18`；`.env.example:4-5` | 直接 `docker compose up`，未覆盖 `UNIFIED_ADMIN_PASSWORD`；或历史库已创建后仅改环境变量 | 默认口令 `unified-admin` 可直接登录管理面；8010 绑 `0.0.0.0` 使其可被局域网/公网访问；`_seed_admin` 不更新已有用户，轮换环境变量无效，运维会误以为已改密 | 启动时若口令等于默认值或为空则 fail-fast 拒绝启动；compose 不提供默认值（`${UNIFIED_ADMIN_PASSWORD:?}`）；已有库需提供显式改密/轮换流程；端口默认绑 `127.0.0.1` 或由反代统一入口 |
| S-04 | high | `apps/control/app/registry.py:107-112`；`apps/control/app/adapters/sub2api_store.py:185-193`、`:221-223` | 任一 admin 账号（或被攻陷的管理员会话）把 provider `base_url` 或 sub2 账号 `upstream_base` 设为内网/环回/链路本地地址 | 网关进程带凭证向任意地址发起请求，可探测内网、访问云元数据（`169.254.169.254`）、命中本机 sidecar/管理端口；错误响应还会回显目标地址 | 解析后校验 IP 段，拒绝 loopback/私有/链路本地/元数据地址与非常规端口；必要时改为域名白名单；sub2 的 `upstream_base` 复用同一校验函数 |
| S-05 | high | `apps/control/app/registry.py:121-133`；`apps/control/app/adapters/sub2api_store.py:185-193`；`apps/control/app/security.py:37-45` | 获得数据库文件、备份或任意 SQL 读能力（明文落盘） | provider `api_key`、`sub2_accounts.api_key`、session token 均以明文入库；一次备份泄露即可接管全部上游额度与在线管理会话 | 上游凭证与 session token 加密存储（应用级密钥/KMS），或至少对上游 key 做信封加密；备份流程按敏感数据对待 |
| S-06 | high | `apps/control/app/routers/v1.py:40-45`、`:146-171`；`apps/control/app/adapters/client.py:61-70` | 持有任一有效 `u2a_` key（或 S-02 触发后匿名）发起大量/长时流式请求 | 无每 key 并发、QPS、日配额；SSE 无总时长上限（`httpx.Timeout` 只限制单次读，不限制总时长），上游持续出块即可长期占用连接与上游费用；每请求还同步 `UPDATE last_used_at` 并 commit，SQLite 高并发下写锁放大 | 增加每 key 并发/速率/日配额与 SSE 最大时长；`last_used_at` 改节流或异步批量写；对异常长的流做熔断 |
| S-07 | high | `apps/web/package.json`（`next` 固定 `15.5.25`，`axios ^1.9.0`）；`package-lock` 解析结果 | 运行 `next dev`/开发服务器，或未来重新启用 Next 图像优化与 Server Actions | `npm audit --omit=dev` 报告 1 critical + 4 high + 14 moderate；critical 为 Next `GHSA-2464-fhr4-w9xw`（Image Optimization API 未授权 RCE，影响 `>=10.0.0 <=15.5.27`），另有 SSG/ISR 缓存投毒与 Server Actions DoS；high 含 `sharp <0.35.5`、`follow-redirects 1.16.0-1.16.1`、`source-map-js <1.2.2`、`brace-expansion <1.11.21` | 上线前升级 `next >= 15.5.28`（本次审计按要求未升级）；生产保持 `output: export` + 不暴露 `next dev`；CI 恢复审计门禁（见 S-22） |
| S-08 | high | `THIRD-PARTY-NOTICES.md`（danger 清单段）；`.env.example:31-33` | 商业部署并启用 `qoder` / `deepseek` provider | `qoder-trae-workbuddy-switch` 为 PolyForm Noncommercial 1.0.0，禁止商用；`Deepseek2Go` 标注无 LICENSE（All Rights Reserved）。这不是代码漏洞，但会直接阻断商用合规 | 商用前移除或取得书面授权；在启动/文档中明确禁用这两个 provider；保留授权凭证到 notices |

### Medium

| 编号 | 严重度 | 位置 | 可利用条件 | 影响 | 修复建议 |
| --- | --- | --- | --- | --- | --- |
| S-09 | medium | `apps/control/app/main.py:47`、`:68-74` | 未认证访问 `/docs`、`/openapi.json`、`/redoc`、`/healthz` | 完整管理 API 结构与参数对外可见，降低攻击成本；`/healthz` 返回数据库绝对路径，泄露部署目录 | 生产环境关闭 docs/openapi/redoc（`docs_url=None` 等）；`/healthz` 只返回 `{"ok":true}` 或经鉴权 |
| S-10 | medium | `apps/control/app/routers/auth.py:46-54`；`apps/control/app/config.py:46`；`docker-compose.yml`（未传 `UNIFIED_COOKIE_SECURE`）；`apps/web/nginx.conf:1-29` | 通过 HTTP（非 TLS）访问管理面，或网络中被嗅探 | Cookie 默认 `secure=False`，session token 明文经 HTTP 传输；nginx 未配 TLS/HSTS；配合 S-03 的 0.0.0.0 暴露可被会话劫持 | 生产强制 `UNIFIED_COOKIE_SECURE=true`，前置 TLS 并加 HSTS；compose 显式传该变量并在文档中标注必须为 true |
| S-11 | medium | `apps/control/app/main.py:53-59`；`apps/control/app/config.py:38` | 运维把 `UNIFIED_CORS_ORIGINS` 放宽到任意/过多来源 | `allow_credentials=True` 叠加 `allow_methods=["*"]`、`allow_headers=["*"]`，宽来源配置下可跨站携带管理 Cookie 发起请求 | 保持来源精确白名单；只放行实际使用的方法/头；同源部署时可不启用 CORS |
| S-12 | medium | `apps/control/app/routers/auth.py:29-55` | 对 `/api/login` 高频撞库/口令喷洒 | 无失败计数、无锁定、无退避；仅有失败审计写入，攻击者可无限尝试；默认弱口令（S-03）下尤甚 | 按 IP + 用户名做滑动窗口限流与临时锁定；失败响应统一，避免用户名枚举差异 |
| S-13 | medium | `apps/control/app/security.py:37-45`、`:53-69`；`apps/control/app/routers/auth.py:58-69` | 数据库泄露，或用户只点了注销 | session token 明文入库；过期行只在被再次使用时删除，无定期清理；注销只删当前 token，其他设备会话继续有效；无绝对/空闲双失效与登录后轮换 | token 存哈希（与 `u2a_` 一致）；增加定期清理任务；提供「全部下线」；敏感操作后可强制重签 |
| S-14 | medium | `apps/control/app/adapters/client.py:76-81`、`:98-102`；`apps/control/app/main.py:61-63`；`apps/control/app/routers/v1.py:137-144` | 调用 `/v1/*` 触发上游错误或不可达 | `ProviderError` 消息包含内部 `base_url` 与上游响应体前 200 字符，并经统一异常处理器原样回给调用方；泄露内网拓扑与上游实现细节 | 对外错误只给通用文案与错误码；`base_url`、上游响应体仅写服务端日志（并对 key 做屏蔽） |
| S-15 | medium | `apps/control/app/routers/keys.py:35-46`；`apps/control/app/routers/settings.py:958-964`；`apps/control/app/routers/grok.py:38-48`；`apps/control/app/services/grok_register.py:105-123` | viewer 登录后读取 | viewer 可读全部 key 元数据（名称/前缀/最后使用）、用户列表、Grok 注册任务详情；任务查询为 `SELECT *` 整行返回，含 `proxy`、`payload` 等执行参数（原表述，2026-11-04 已更正，见下） | 按最小权限收紧：key 列表、用户列表、注册任务详情改 `admin_user`；任务列表只返回状态字段并脱敏 payload/proxy |
| S-16 | medium | `apps/web/nginx.conf:1-29` | 管理台被第三方页面 iframe 嵌入，或浏览器缺少防护 | 无反代层安全响应头：无 `Content-Security-Policy`、`X-Frame-Options`/`frame-ancestors`、`X-Content-Type-Options`、`Referrer-Policy`、HSTS；存在点击劫持与内容嗅探风险 | 在 nginx `server`/`location` 增加上述响应头；管理面设置 `frame-ancestors 'none'` |
| S-17 | medium | `apps/control/app/registry.py:128-133`；适配器使用面如 `apps/control/app/adapters/client.py:44-50`、`:68-69` | 任一 admin 写入 provider `options` | `options` 为任意 dict 深合并、无 schema：可设置 `proxy`（把带凭证的请求导向任意代理）、`extra_headers`（覆盖/注入请求头）、`timeout`；`zcode` 等还会把配置拼进 system prompt | 为每个 provider 定义 options schema（类型/枚举/范围），`proxy` 与 `extra_headers` 走白名单/地址校验，禁止任意键 |

> **2026-11-04 已更正（原表格 S-15 行保留作历史）**：S-15 的三项中两项已落地——「用户列表」：`GET /api/users` 现为 `Depends(admin_user)`（`apps/control/app/routers/settings.py:958-964`，def 行 959）；「Grok 注册任务详情/列表」：现为 `Depends(admin_user)`（`apps/control/app/routers/grok.py:39-41`、`:44-47`，def 行 40、45，`:38` 注释「读侧与写侧同一口径：仅 admin」）。仍开放的一项：`GET /api/keys` 仍 `current_user`（`apps/control/app/routers/keys.py:36-46`），但函数体按 `owner_user_id` 收窄、NULL 归属行对非 admin 不可见，属体内权限而非裸暴露。修复建议中「用户列表、注册任务详情改 `admin_user`」已完成；「key 列表改 `admin`」未做（现以体内归属收窄替代）；「任务列表脱敏 payload/proxy」未做且不再必要（端点已整收 admin，viewer 拿不到任何行）。这条为什么重要：S-15 若继续按原行当「三项均对 viewer 开放」跟踪，会重复修复已完成的两项，也会误判 keys 列表的真实现状（体内收窄，不是全量裸读）。

### Low

| 编号 | 严重度 | 位置 | 可利用条件 | 影响 | 修复建议 |
| --- | --- | --- | --- | --- | --- |
| S-18 | low | `apps/control/Dockerfile`（`uvicorn --host 0.0.0.0`，未设 `USER`） | 容器内进程被利用 | 以 root 运行，容器逃逸/横向时权限更大 | 增加非 root 用户并以该用户运行；只读根文件系统（除 `/data`） |
| S-19 | low | `apps/control/app/routers/auth.py:77-84` | viewer 调用 `/api/sessions/revoke` | 只能吊销自己；管理员无法强制下线其他用户（改密除外），应急响应能力不足 | 增加管理员按用户吊销接口，并写审计 |
| S-20 | low | `apps/control/app/routers/settings.py:282-296` | viewer 反复调用系统检查接口 | `check-update` / `versions` 对 viewer 开放，可能触发外部请求与信息暴露（具体外呼目标待确认） | 改为 `admin_user` 或加缓存与限流 |
| S-21 | low | `apps/control/app/routers/auth.py:21-26`；`apps/control/app/routers/settings.py:77-135`、`:199-218` | 管理员改 provider key、开匿名、加用户等 | 仅登录/登录失败写 `audit_logs`；配置类写操作无审计，事后无法追溯谁改了上游凭证或开放了匿名 | 在管理面写操作统一记录 actor/action/target/ip（不记录密钥值） |
| S-22 | low | `.github/workflows/ci.yml`（`npm ci --no-audit`） | 依赖引入已知高危版本 | CI 明确关闭 npm 审计，S-07 这类公告不会在流水线暴露 | 恢复 `npm audit --omit=dev`（或等价 SCA）作为非阻断报告 + 高危阻断 |

> **（2026-11-05 已更正，原 S-20 行保留作历史）** S-20 里的 `check-update` 已于 2026-11-05 下线（`apps/control/app/routers/system.py` 不再注册该路由），viewer 无法再调用它，也就没有「反复调用触发外部请求」这条路径；仍对 viewer 开放的是 `GET /api/system/versions`，它只读库内 `accounts` 计数、不发外网请求。
>
> **这条为什么重要**：按旧行会继续按「可能出现外呼」的严重度给 S-20 排期，或去 `check-update` 的 handler 找限流缺口；实际剩余风险只是版本页暴露已启用上游账号数，处置方式（是否改 `admin_user`）与外部请求无关。

## 重点问题证据

- **S-01**：`_read_config` 在 `provider_config` 分支里把数据库 JSON 原样并入返回（`settings.py:57-64`），`GET /settings/upstream` 仅 `current_user`（`:72-74`）。临时库实测响应中出现 `SECRET-UPSTREAM-KEY-42`。前端 settings 页却按 `api_key_masked` / `token_masked` 渲染，说明前后端契约本身已错位。
- **S-02**：`save_gateway_config` 用 `bool(body.get("allow_anonymous"))`（`settings.py:129`），`v1._anonymous_allowed` 再 `bool(cfg["allow_anonymous"])`（`v1.py:25-26`）。实测传 `"false"` 后返回 `allow_anonymous: true`，无 `Authorization` 请求由 401 变为通过（503 上游不可达）。
- **S-04**：`registry.update` 只做 `startswith(("http://","https://"))`（`registry.py:109-110`），未解析主机；`sub2api_store` 的 `create/update` 对 `upstream_base` 连前缀都不校验（`:191`、`:221-223`）。
- **S-06**：`_gateway_key` 每次成功鉴权都同步写 `api_keys.last_used_at` 并 commit（`v1.py:45-46`）；流式分支把上游迭代器直接交给 `StreamingResponse`（`:146-171`），没有并发信号量、没有总时长上限。

## 已确认无问题（避免误报）

- SQL 全部参数化：`db.py` 与各 router 的查询均为 `?` 占位；动态 SET 的列名来自固定分支/白名单，未见字符串拼接用户输入。
- 密码校验使用 PBKDF2-HMAC-SHA256 200k + `hmac.compare_digest`（`security.py:14-34`）；会话 token 用 `secrets.token_urlsafe(32)`。
- `u2a_` key 仅存 sha256，明文只在创建响应返回一次（`keys.py:53-73`）；吊销是删除行，下一请求立即 401，无需等待缓存过期。
- 网关 key 查询为 sha256 摘要等值查找而非明文比较；虽然 SQL 比较非恒定时间，但攻击者需先构造 sha256 前像，实际不构成可行时序攻击。
- 客户端断开时 SSE 经 `iter_bytes` 的 `finally` 调用 `aclose` 释放上游连接（`client.py:25-32`）；`_record_safe` 用 `asyncio.shield` 保证断开后仍落库。
- 前端不在浏览器持久化密钥/token：`sessionStorage('wb-me')` 只有 username/role；axios 仅 `withCredentials`。
- `.gitignore` 覆盖 `.env`、`.env.local`、`data/`、`.runtime/`、`node_modules/`、`out/`；仓库内无真实 `.env`（仅 `.env.example`）。
- `providers/` 只有 `README.md` 与 `docker-compose.fragment.yml`，未把上游源码或 LICENSE 文件带入主仓；仓库内未发现 GPL/LGPL 源文件。
- 错误堆栈未外泄：uvicorn 默认非 debug，FastAPI 未开 debug 时返回通用 500；`ProviderError` 只回业务消息（但含 base_url，见 S-14）。
- 最后一个管理员不可删除/降级（`settings.py:237-240`、`:270-273`）；改密/删用户会 `revoke_user_sessions`。

## 待确认

1. **clean-room 与上游实质性相似性无法本地验证**：`THIRD-PARTY-NOTICES.md` 的 License URL 指向仓库外的 `research/` clone，当前仓库无对应源码，无法比对。
2. **`Deepseek2Go`「无 LICENSE / All Rights Reserved」声明**为离线项目内声明，未能在本机核实其分发页；商用前需法务复核。
3. **并行开发中间态**：`adapters/trae.py`（17:44 新增）硬编码 Trae 主机名、APP_ID 与版本号，来源与真机行为未验证；`adapters/__init__.py`（17:46）已 import 尚不存在的 `qoder.py`，该快照下应用不可导入。属并行会话进行中状态，不计为本次缺陷。
4. **生产 TLS 终止位置**：`apps/web/nginx.conf` 无 TLS 配置，若 TLS 由 compose 之外的反代/网关终止，S-10 的实际暴露面需结合真实部署确认。
5. **`accounts.py:148-190` 的 `{file}` 路径参数**：当前先经 `require_file` 精确查库再拼进上游 URL；若上游返回的 `file` 含 `../`，可能在上游侧形成路径穿越，未实测。
6. **前端 settings 页是否以字符串发送 `allow_anonymous`**：后端缺陷已由直调 API 证实；Web 端实际 payload 类型未逐一核对。
7. **`options.proxy` 的可用协议与解析面**：由 httpx 处理，是否允许非 http(s) 代理、是否读取环境变量，未实测。

## 上线前必须处理（最小清单）

1. 修复 S-02：`allow_anonymous` 严格布尔解析 + 回归测试；确认保存后实际鉴权行为一致。
2. 修复 S-01：`GET /api/settings/upstream` 改 `admin_user`；`api_key`/token 一律脱敏，前端按掩码字段渲染。
3. 修复 S-03：默认口令时拒绝启动；compose 去掉默认值；提供已有库的改密/轮换步骤；端口默认仅本机。
4. 修复 S-04：`base_url` 与 `upstream_base` 统一做解析后地址校验（拒 loopback/私网/链路本地/元数据/异常端口）。
5. 处理 S-05：上游凭证与 session token 不落明文（至少加密存储），备份按敏感数据管理。
6. 关闭 S-09：生产禁用 `/docs`、`/openapi.json`、`/redoc`；`/healthz` 去掉 DB 路径。
7. 处理 S-06：`/v1` 增加每 key 并发 + 速率 + 日配额；SSE 设最大时长；`last_used_at` 降频写入。
8. 处理 S-10 / S-16：生产强制 Secure Cookie、TLS + HSTS；nginx 补 CSP/frame-ancestors/nosniff/Referrer-Policy。
9. 处理 S-07 / S-22：升级 `next >= 15.5.28`（本次不动），CI 恢复 `npm audit --omit=dev` 门禁。
10. 处理 S-08：商用前移除或取得 `qoder`（PolyForm-NC）与 `Deepseek2Go`（无 LICENSE）授权，并更新 notices。
11. 处理 S-12：登录失败限流 + 锁定。
12. 处理 S-21：管理面写操作落 `audit_logs`（不记录密钥值）。

## 修复状态（逐条验收，2026-11-02）

> 回归基线：`apps/control` 下 `python -m pytest tests -q` → **73 passed, 3 subtests passed**（含本次新增 `tests/test_security.py` 9 项）。该数字为当次审查快照；当前基线以 2026-11-04 全量实测为准（443 passed, 1 skipped, 0 failed, 0 xfailed，125.07 秒，收集 444 条，量级约 444）。
> 说明：每条只写「状态 / 改了什么 / 怎么验」，未修复的一律标明原因，无静默跳过。

| 编号 | 状态 | 处理内容与验证方式 |
| --- | --- | --- |
| S-01 | 已修复 | 全部 `/api/settings/*` 读写改 `admin_user`；`_read_config` 把密钥字段换成 `*_masked`（前 4 + 后 4）与 `has_*`，`provider_config`/`upstream_config`/`upstash` 递归脱敏；新增 `POST /api/settings/secret/reveal`（管理员 + `audit_logs` 记 actor/路径/IP，不记明文）；保存时丢弃回传的 `*_masked`，`push_config` 才用明文。验证：`test_security.py::test_viewer_cannot_read_settings`、`test_admin_settings_masks_upstream_key`、`test_reveal_returns_plaintext_and_writes_audit`、`test_non_admin_cannot_reveal`；契约同步更新在 `test_debt.py::test_detached_groups_fall_back_to_real_settings`。 |
| S-02 | 已修复 | 新增 `api_utils.parse_bool`：布尔直通，字符串仅接受 `true/false`（大小写不敏感），其他 400；`save_gateway_config` 与 `get_gateway_config` 同源校验；`v1._anonymous_allowed` 复用解析，库里脏值按「关闭」处理。验证：`test_string_false_is_rejected_and_gateway_stays_closed`（含 `"false"`/`"FALSE"` 解析为 False、匿名 401、`1/0/null/"yes"/""/[]` 全部 400）、`test_bool_and_mask_helpers`。 |
| S-03 | 已修复 | `config._admin_password`：env（且非历史默认值）> `data/admin-password.txt` > 首次 `secrets.token_urlsafe(24)` 生成并落盘 0600；`main._seed_admin` 对环境变量显式轮换、对已有库识别历史默认口令 `unified-admin` 并更新+吊销全部会话；启动日志打印口令来源与文件路径；默认监听改 `127.0.0.1:8010`（`UNIFIED_HOST`/`UNIFIED_PORT`），Dockerfile 容器内 0.0.0.0、compose 端口默认只映射 `127.0.0.1`、`.env.example` 去掉固定口令。验证：`test_default_admin_password_is_generated`（历史默认值被忽略、随机且落盘、二次读为 `file` 来源）；测试控制面启动即使用随机/文件口令路径。 |
| S-04 | 已修复 | 新增 `app/net_guard.guard_upstream_url`：解析主机后拒绝 loopback/私网/链路本地/保留/组播/未指定与 `169.254.169.254`、`fd00:ec2::254`；接入 `registry.update(base_url)`、凭据库写入与 `QuotaStore.add_account/update_account(upstream_base)`。本地 loopback 按 `spec.default_base_url` 推导放行（2026-11-03 补充，边界见下）；其余内网地址仍用 `UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1` 显式放开，云元数据地址无条件拒绝。验证：`test_private_upstreams_are_blocked`、`test_sub2_store_rejects_private_upstream`，以及 2026-11-03 新增的 loopback / 私网 / 元数据 / 公网 / 对外字段 7 项。 |
| S-05 | 延期（需决策） | 原因：上游 key / `sub2_accounts.api_key` / session token 明文落库，改应用级加密要先定密钥托管（env 密钥、KMS 还是信封加密）、既有库迁移与备份流程，属产品/运维决策，单靠代码改动会留下「密钥和密文同机」的假安全。现状已在 notices 与部署文档标注「备份按敏感数据管理」。 |
| S-06 | 部分修复（配额延期） | 已加：SSE 单请求最大时长（默认 600s，`UNIFIED_SSE_MAX_SECONDS`，超时中断并留下 `error` 记录，显式 `aclose` 释放上游）；`last_used_at` 60s 节流写入，减少 SQLite 写锁。延期：每 key 并发 / QPS / 日配额——需要产品定义额度口径与超限行为（拒绝码、是否计数、是否按模型区分）。 |
| S-07 | 延期（需决策/升级窗口） | 原因：升级 `next >= 15.5.28` 需改 `package-lock` 并做全量前端回归，属独立依赖升级窗口；当前生产用 `output: export` 静态产物，不暴露 Next Image Optimization / Server Actions / `next dev`。已在 `THIRD-PARTY-NOTICES.md` 记录受影响版本。 |
| S-08 | 延期（需决策/法务） | 原因：`qoder-trae-workbuddy-switch`（PolyForm NC）与 `Deepseek2Go`（无 LICENSE）的商用授权是法务/商务决策；仓库侧已把两条风险写进 `adapters` 的 `risk_level/risk_note`、`THIRD-PARTY-NOTICES.md`、`.env.example` 警示段落，未引入其源码或二进制。 |
| S-09 | 延期（需决策） | 原因：`/docs`、`/openapi.json` 是否保留调试入口需产品确认；`/healthz` 的 `db` 字段被测试基建与运维探针使用，去掉要同步改 `conftest.py` 与部署脚本，需一并排期。 |
| S-10 | 延期（部署面决策） | 原因：TLS 终止位置（反代 / nginx / 网关）未定；`UNIFIED_COOKIE_SECURE` 已可配置，生产需显式置 true，属部署清单项。 |
| S-11 | 延期（部署面决策） | 原因：CORS 收窄到精确来源需先确认同源部署还是跨域管理台形态，否则会直接打断现有前端。 |
| S-12 | 延期（需决策） | 原因：登录失败限流需产品定义阈值、窗口与锁定策略（按 IP 还是 IP+用户名、锁多久、是否解锁工单），阈值拍定前贸然上线会锁死运维。 |
| S-13 | 延期 | session token 改存哈希、全部下线、双失效与登录后轮换，属会话模型重构，需与 S-05 的凭据治理一起做。 |
| S-14 | 延期 | 对外错误文案统一（错误码 + 通用描述，`base_url`/上游响应体只进服务端日志）需逐个适配器确认错误语义，现有 `ProviderError` 契约被 F1/F2 适配器依赖，改动要连带回归。 |
| S-15 | 延期（部分收紧） | 已收紧 `/api/settings/*` 全部管理员；`/api/keys` 列表、`/api/users` 列表、Grok 任务详情仍对 viewer 开放，需产品确认 viewer 的职责边界后再改权限矩阵。（原表述，2026-11-04 已更正，见下） |
| S-16 | 延期（部署面） | 原因：CSP / frame-ancestors / nosniff / Referrer-Policy / HSTS 属 `apps/web/nginx.conf` 部署配置，需与 TLS 方案（S-10）一起落。 |
| S-17 | 延期（需决策） | 原因：逐 provider 的 `options` schema（字段白名单、类型、范围）要先定各家支持面；当前 `options.proxy`/`extra_headers` 仍为任意键，属管理面高权限操作。 |
| S-18 | 延期 | 容器非 root 与只读根文件系统需同步调整 `/data` 权限与 compose 挂载，属镜像改造项。 |
| S-19 | 延期（需决策） | 管理员按用户强制下线需产品确认入口与审计文案，已可通过改密达到同等效果。 |
| S-20 | 延期 | `check-update`/`versions` 对 viewer 开放；改为管理员或加缓存需确认前端调用方。 |
| S-21 | 部分修复（延期） | 已落审计：`settings.gateway`（匿名开关）、`settings.reveal_secret`（查看明文）。延期：provider key 变更、用户增删改、model-map 保存的统一审计，需先定 `audit_logs` 的 action 命名与保留期。 |
| S-22 | 延期（CI 决策） | 原因：恢复 `npm audit --omit=dev` 门禁要处理现有 1 critical + 4 high 的存量告警（与 S-07 的升级窗口绑定），否则流水线会长期红。 |

> **（2026-11-05 已更正，原 S-20 状态行保留作历史）** `check-update` 已下线（`apps/control/app/routers/system.py` 不再注册该路由），S-20 的延期理由只剩 `versions` 对 viewer 开放这一项；后者不发外网请求，「加缓存与限流」对它不适用，待决的只有是否收紧为 `admin_user`。
>
> **这条为什么重要**：按旧状态行会以为 S-20 还压着一个外部请求限流项，验收时找不到可测的外呼路径；也会把已删除端点算进未修复清单，虚增延期项。

> **2026-11-04 已更正（原表格 S-15 行保留作历史）**：S-15 于 2026-11-04 进一步收紧——「`/api/users` 列表」现为 `admin_user`（`apps/control/app/routers/settings.py:958-964`，def 行 959）、「Grok 任务详情」现为 `admin_user`（`apps/control/app/routers/grok.py:39-41`、`:44-47`，def 行 40、45），两项均不再对 viewer 开放；仍对 viewer 开放的只剩「`/api/keys` 列表」，且已带 `owner_user_id` 体内收窄（`apps/control/app/routers/keys.py:36-46`）。原行「`/api/keys` 列表、`/api/users` 列表、Grok 任务详情仍对 viewer 开放」仅前半句仍成立。这条为什么重要：修复状态表是验收依据，旧状态会让人以为三项都没动而重复排期，也会让 keys 列表的体内收窄改造被漏看。

### S-04 边界补充（2026-11-03）

本地 sidecar 地址（`trae_sidecar` / `deepseek_sidecar` / `qoder_sidecar` / `codar_sidecar`，以及默认地址同为 `127.0.0.1` 的 `codebuddy` 管理端）不再要求手动开启全局开关：

- **放行依据**：`spec.default_base_url` 本身是否指向 loopback（`127.0.0.0/8`、`::1`、`localhost`）。名单由 spec 推导，新增 sidecar 不用改守卫代码。
- **放行范围**：只有 loopback。`127.0.0.0/8` 整段放行（RFC 6890 保留给环回，无法路由出本机，只认 `127.0.0.1` 并不能多挡攻击面）。
- **继续拦截**：`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`、链路本地 `169.254.0.0/16`、组播/保留/未指定地址。
- **绝对红线**：`169.254.169.254` 与 `fd00:ec2::254` 云元数据地址无条件拒绝，包括 `UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1` 时。
- **全局开关**：`UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1` 语义未变（跳过 IP 段检查并优先于 per-provider 判断），唯一收紧是元数据地址仍拒绝。
- **对外契约**：`GET /api/providers` 新增 `allows_local_base_url`；`PATCH /api/providers/{id}` 与 `/api/provider-credentials` 写 `base_url` 复用同一守卫，400 文案说明「这家是否允许本地地址」及正确做法。
- **远端备份 endpoint**：`/api/system/backup-remote` 保存 `endpoint` 时与 provider `base_url` 共用 `guard_upstream_url`（`apps/control/app/services/backup_remote.py:229-244`）：默认拒绝环回/私网/链路本地，云元数据地址即使 `UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1` 也拒绝；本机或内网 MinIO 场景需显式打开全局开关。
- **残余风险**：守卫在保存时解析 DNS，保存到实际请求之间仍有 DNS 重绑定窗口（与修复前同性质，本次未引入连接期二次校验）；白名单信任 spec 里的静态默认地址，若默认值写错会让该 provider 获得 loopback 权限。
