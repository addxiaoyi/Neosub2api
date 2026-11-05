# 安全复测报告（2026-11-03）

复测对象：本仓 control 面（仓库目录 `unified-2api`）
复测范围：权限矩阵、`u2a_` 密钥泄露面、provider `base_url` 的 SSRF 边界
复测方式：只新增 `apps/control/tests/test_security_matrix.py`，全部通过真实 HTTP + 临时库直读验证；未改源码、未改既有测试、未执行 git 操作。

**总评：未发现未登录/viewer 可绕过角色边界的越权；未发现完整 `u2a_` 密钥出现在响应、日志表或全库任何文本列。确认 1 个需要 admin 权限才能利用的 SSRF 残留窗口（DNS 重绑定），以及 2 个低危权限口径问题。**

> **2026-11-04 已更正（原句保留作历史）**：「2 个低危」是 2026-11-03 当日实测口径，数字不改，但其中 1 项现已失效——**发现 2（`GET /api/logs` viewer 可读全站）已于 2026-11-04 修复**：`_owner()`（`apps/control/app/routers/logs.py:36-40`）对非 admin 收窄、`_assert_key_owned`（`logs.py:43-52`）显式查他人 `key_id` 直接 403、WHERE 注入见 `logs.py:238-246`，详见 §1 发现清单后的更正块。**发现 3（`/api/security/guard` 静态策略暴露）仍成立**：`security.py:93-104` 仍为 `Depends(current_user)` 且返回内容未变。当前真实状态是 2 个低危中 1 个已修复、1 个仍存。这条为什么重要：引用总评时若照读「2 个低危」会重复立项已修复的日志问题；若把数字改成 1 则伪造了当日实测结果。

## 1. 发现清单（按严重度）

| 严重度 | 发现 | 可利用性判断 | 证据 |
| --- | --- | --- | --- |
| 中 | **DNS 重绑定窗口仍存在**：`base_url` 只在保存时解析并校验一次，请求时不再校验 | 需要 admin 权限写入 provider/credential/sub2api 地址，且攻击者能控制域名解析。未登录与 viewer 不能利用。无重定向跟随，不能靠 302 绕过。成功时可让控制面代发请求命中 `169.254.169.254` 或私网 | 保存时 `guard_upstream_url` 解析 1 次；`app/adapters/base.py`、`client.py`、`codebuddy.py`、`opencode.py` 均无第二次调用；`client.py` 未开启 `follow_redirects` |
| 低 | **viewer 可读取全站请求日志**：`GET /api/logs` 只校验登录，不按 owner 过滤，可显式传他人 `key_id` | 任意 viewer 可直接利用，读到他人 `key_name/provider/model/status/error`；**不含密钥明文** | viewer 传 admin 的 `key_id` 得到 200 且响应含 admin 的 key_name（原结论，2026-11-04 已更正，见下） |
| 低 | **`/api/security/guard` 向所有登录用户暴露部署配置**：返回 `allow_private_upstreams` 与固定黑名单 | 任意 viewer 可读，但内容是静态策略，不是真实封禁名单；仅帮助判断私网 SSRF 是否被放开。单独不可利用 | viewer 与 admin 响应完全一致；`blocked_hostnames` 仅 localhost/metadata.google.internal，`blocked_addresses` 仅 `169.254.169.254`/`fd00:ec2::254` |
| 信息 | viewer 可读 `/api/providers`、`/api/provider-credentials` 的 `base_url`（内部地址/拓扑）与掩码密钥 | 设计口径下允许；无明文密钥；单独不可利用 | 三身份矩阵中 viewer 均 200，响应无完整 `u2a_` |
| 信息 | viewer 可枚举 `/api/users` 全部用户名与角色 | 低敏信息，可与上面的日志泄露组合做内部定位；不可直接提权 | viewer 200，返回全量用户（原结论，2026-11-04 已更正，见下） |

> **2026-11-04 已更正（原表格两行保留作历史）**：两行均为 2026-11-03 实测结果，2026-11-04 起已过期——
>
> 1. **`GET /api/logs`（发现 2，低）**：现按 owner 收窄——`_owner()`（`apps/control/app/routers/logs.py:36-40`）对非 admin 返回本人用户名并注入 WHERE（`logs.py:238-246`），显式传他人 `key_id` 由 `_assert_key_owned`（`logs.py:43-52`）直接 403「无权查看他人密钥的请求日志」；viewer 只见本人行，「可读全站、传他人 key_id 得 200」不成立。
> 2. **`GET /api/users`（发现 5，信息）**：现为 `Depends(admin_user)`（`apps/control/app/routers/settings.py:958-964`，def 行 959），viewer 请求 403，「可枚举全部用户名与角色」不成立；前端 `settings/page.tsx:926` 的调用也已入 `isAdmin` 门禁（`settings/page.tsx:877-928`）。
>
> 这条为什么重要：复测报告常被当作「当前暴露面」直接引用，这两行会让读者以为日志与用户列表仍对 viewer 全量可读，据此重复立项修复，或在别处照抄旧口径。

无高危、无致命项。

## 2. 权限矩阵

环境：临时控制面 `127.0.0.1:18129`，假上游 `18130`；admin 为初始账号，viewer 由 admin 通过 `/api/users` 创建后独立登录。下表中每个方法+路径都对三种身份实跑。

### 2.1 读端点

| 端点 | 未登录 | viewer | admin | 结论 |
| --- | --- | --- | --- | --- |
| `/healthz`、`/api/health` | 200 | 200 | 200 | 公共探针 |
| `/api/health/ready` | 200/503 | 200/503 | 200/503 | 公共探针，503 是就绪语义 |
| `/api/me` | 401 | 200 | 200 | 一致 |
| `/api/keys`、`/api/tokens` | 401 | 200 | 200 | viewer 仅见自身，见 §3 |
| `/api/logs` | 401 | 200 | 200 | ⚠ viewer 可读全站，见发现 2（原结论，2026-11-04 已更正，见下） |
| `/api/quota/policies`、`/api/quota/status`、`/api/quota/alerts` | 401 | 200 | 200 | viewer 仅见自身；显式查他人 403 |
| `/api/audit-logs`、`/api/security/audit` | 401 | 200 | 200 | viewer 仅见 `actor=自己`；查他人 403 |
| `/api/security/guard` | 401 | 200 | 200 | 静态策略，见发现 3 |
| `/api/security/login-failures` | 401 | 200 | 200 | viewer `scope=self` 聚合，无 IP/他人信息 |
| `/api/accounts`、`/api/accounts/capabilities` | 401 | 200 | 200 | 一致（原值，2026-11-04 已更正，见下） |
| `/api/providers`、`/api/provider-credentials` | 401 | 200 | 200 | ⚠ 内部地址对 viewer 可见（设计口径） |
| `/api/models`、`/api/status` | 401 | 200 | 200 | 一致 |
| `/api/stats/summary`、`/api/stats/daily` | 401 | 200 | 200 | 一致 |
| `/api/system/changelog`、`/api/system/versions` | 401 | 200 | 200 | 一致 |
| `/api/grok/register/tasks`、`/api/grok/register/worker` | 401 | 200 | 200 | 一致（原结论，2026-11-04 已更正，见下） |
| `/api/users` | 401 | 200 | 200 | ⚠ viewer 可枚举用户（原结论，2026-11-04 已更正，见下） |
| `/api/metrics` | 401 | **403** | 200 | 角色边界正确，403 结构化 |
| `/api/sub2api/groups`、`/api/sub2api/accounts`、`/api/sub2api/ledger` | 401 | **403** | 200 | 角色边界正确 |
| `/api/settings/upstream`、`/api/settings/gateway`、`/api/settings/model-map`、`/api/settings/runtime-config`、`/api/settings/runtime-config/export` | 401 | **403** | 200 | 角色边界正确 |

> **2026-11-04 已更正（原表格四行保留作历史）**：状态码为 2026-11-03 实测，2026-11-04 起以下四行的 viewer 列已变——
>
> 1. **`/api/grok/register/tasks`（与 worker 同行）**：两个任务读端点现为 `Depends(admin_user)`（`apps/control/app/routers/grok.py:39-41`、`:44-47`，def 行 40、45），viewer 应为 **403** 而非 200；同格的 `/api/grok/register/worker` 仍 `current_user`（`grok.py:72-73`），viewer 200 不变。
> 2. **`/api/users`**：现为 `Depends(admin_user)`（`settings.py:958-964`，def 行 959），viewer 应为 **403**，「⚠ viewer 可枚举用户」不成立。
> 3. **`/api/accounts`（与 capabilities 同行）**：三列状态码结论不变（401/200/200），但 viewer 的数据面已收窄——`accounts.py:294-297` 对非 admin 追加 `WHERE owner_user_id = <username>`，viewer 只见自己名下账号（无主 NULL 行不可见），「一致」在数据内容上不再等同 admin 视图；同格 `/api/accounts/capabilities` 未变。
> 4. **`/api/logs`**：viewer 现只见本人行，显式传他人 `key_id` 由 `_assert_key_owned`（`logs.py:43-52`）403（`_owner` 收窄见 `logs.py:36-40`、`:238-246`），「⚠ viewer 可读全站」不成立。
>
> 这条为什么重要：权限矩阵按行引用，旧行会让人以为 viewer 现在仍能 200 拿到 grok 任务与全量用户列表、以为 accounts 返回与 admin 相同的全量数据，或以为日志仍可跨用户读——据此会误判暴露面、重复立项。

### 2.2 写端点

| 端点 | 未登录 | viewer | admin（实测） | 结论 |
| --- | --- | --- | --- | --- |
| `POST/PATCH/DELETE /api/keys` | 401 | **403** | 200 / 404 / 404 | 正确 |
| `POST/PATCH/DELETE /api/tokens` | 401 | **403** | 200 / 404 / 404 | 正确 |
| `POST/DELETE /api/quota/policies` | 401 | **403** | 404 / 404（不存在的 key/策略） | 正确 |
| `POST /api/sub2api/groups`、`POST/DELETE /api/sub2api/accounts`、`DELETE /api/sub2api/groups/{id}` | 401 | **403** | 200 / 404 / 404 / 200 | 正确 |
| `POST /api/accounts/sync`、`PUT /api/accounts/{file}/note`、`POST /api/accounts/{file}/checkin`、`DELETE /api/accounts/{file}` | 401 | **403** | 200 / 404 / 404 / 404 | 正确 |
| `PATCH /api/providers/{id}` | 401 | **403** | 200 | 正确 |
| `POST/PATCH/DELETE /api/provider-credentials` | 401 | **403** | 404 / 404 / 404 | 正确 |
| `POST /api/settings/runtime-config`、`/import`、`/upstream`、`/secret/reveal`、`/gateway`、`/model-map`、`/upstash/test` | 401 | **403** | 400（空 payload） / 400 / 200 / 404 / 200 / 200 / 200 | 正确 |
| `POST/PATCH/DELETE /api/users` | 401 | **403** | 400 / 404 / 404 | 正确 |
| `POST /api/pricing/books` | 401 | **403** | 422（缺字段） | 正确 |
| `POST /api/grok/register/tasks`、`POST /api/grok/register/tasks/{id}/cancel` | 401 | **403** | 422（count=0） / 404 | 正确 |
| `POST /api/logs/clear` | 401 | **403** | 200 | 正确 |

**越权结论**：没有任何端点允许 viewer 或未登录执行 admin 动作；所有拒绝均为结构化 403（`error.type=permission_denied`、`code=insufficient_role`、`required_role=admin`、`role=viewer`）。标注 ⚠ 的 4 行属于“登录用户可读范围过宽”的数据暴露，不是角色绕过。

> **2026-11-04 已更正（原句保留作历史）**：原句称「标注 ⚠ 的 4 行」，但 2026-11-03 提交版的 §2.1 表内实际只有 3 处 ⚠（`/api/logs` 行、`/api/providers`+`/api/provider-credentials` 行、`/api/users` 行）。按裁决，⚠ 与「2 个低危」都是当日实测口径，数字不改，这里只补记实际标记数，免得复核者按 4 行去数、把某个无 ⚠ 的行当成漏看的暴露面。这条为什么重要：⚠ 行是「登录用户可读范围过宽」的唯一清单，行数对不上会让复核结论失去锚点。

## 3. 密钥归属与审计自限权（重点改动复测）

### `api_keys.owner_user_id`

- viewer `GET /api/keys` 全部行 `owner_user_id=viewer`，不含 admin 的 key id；`GET /api/tokens` 不含 admin token id。
- viewer 对 `policies/status/alerts` 显式传 admin `key_id`：均 403，detail=`无权查看该密钥的配额数据`，携带 `required_role=admin` / `role=viewer`。
- viewer 用 `X-Api-Key: <admin 明文>` 访问配额：同样 403；用自己明文：200。
- viewer 在请求体里建他人 key 的策略：403。
- admin 建 key 时指定不存在的 `owner_user_id`：400，不落无主记录。
- admin `GET /api/quota/policies` 可见两条（viewer 与 admin 各一条）；viewer 永远只看到自己那条。

### 审计日志

- viewer 默认 `GET /api/audit-logs` 的 `total` 与自己 `actor` 的行数完全一致，`items` 全是自己；他人 username 不在响应文本中。
- viewer 传 `actor=<他人>`：403，detail=`无权查看他人审计日志`（`/api/audit-logs` 与 `/api/security/audit` 一致）。
- admin 按 `action` 过滤可见 viewer 与他人的全部行。
- 登录失败：viewer 视图 `scope=self`，`active=[]`、`recent=[]`，只返回 `stats.total`，响应中无任何 IP、他人用户名、`anonymous`；admin 视图 `scope=all`，`recent` 含他人记录，`active` 含真实的 `127.0.0.1` 失败条目（真实失败一次后 `window_failures>=1`）。

## 4. 密钥泄露面（逐条证据）

| 通道 | 实测结果 | 结论 |
| --- | --- | --- |
| 网关响应体 | `POST /v1/chat/completions` 带 `x-api-key: <明文>` 返回 200，响应体不含完整 key | 不泄露 |
| 响应头 / `X-Request-Id` | 所有响应头不含 key；`X-Request-Id` 仅回显客户端传入值，换 id 不影响鉴权 | 不泄露，无关 |
| `request_logs` 表 | 最新行 `key_id/key_name` 正确，逐列检查无完整 key | 不泄露 |
| 全库扫描 | 所有表、所有文本列搜完整 key：0 命中 | 不泄露（`api_keys` 仅存 hash） |
| `GET /api/logs` | admin 与 viewer 响应均无完整 key；但 viewer 能读到 admin 的 `key_name` | 无密钥泄露，有跨用户元数据暴露（原实测，2026-11-04 已更正，见下） |
| 无效 key 错误 | `x-api-key: u2a_<随机>` → 401，响应不回显该值 | 不泄露 |
| 双头冲突 | `Authorization` 与 `x-api-key` 同时给不同 key → 401，两者都不回显 | 不泄露 |
| URL 传 key | `?key_id=<完整 key>` → 400，响应不回显；`X-Api-Key` 与 `key_id` 同传 → 400 | 不泄露 |
| uvicorn access log | `mask_access_args` 将查询串中的 `u2a_` 主体掩码，保留前缀；`JsonFormatter` 只输出无查询串的 `path`，原消息中的 key 也被 `mask_text` 抹掉；`app/main.py` 仍复用同一函数 | 脱敏链路存在且有效（见 §7 限制：未拿到真实 access log 行） |
| 建 key 响应 | `POST /api/keys` 一次性返回明文 key | 设计行为，不计泄露 |

> **2026-11-04 已更正（原实测保留作历史）**：`GET /api/logs` 那行「viewer 能读到 admin 的 `key_name`」已不成立——`_owner()`（`logs.py:36-40`）对非 admin 返回本人用户名、admin 才返回 `None`（不收窄），列表查询据此注入收窄条件（`_where` 见 `logs.py:101-111`，过滤子句 `logs.py:116`）；显式传他人 `key_id` 由 `_assert_key_owned`（`logs.py:43-52`）直接 403「无权查看他人密钥的请求日志」。故该通道现为「不泄露（viewer 仅见本人行）」。这条为什么重要：§4 是密钥泄露面的逐通道证据表，照旧读会以为仍存在跨用户元数据通道，要么重复立项，要么误判修复未生效。

## 5. SSRF 实测

### 5.1 守卫函数矩阵（生产默认 `allow_private_upstreams=False`）

| 输入 | 条件 | 结果 |
| --- | --- | --- |
| `http://127.0.0.1:7864` | 该 provider 允许环回 | 通过 |
| `http://127.1.2.3:7864` | 允许环回 | 通过（整个 `127.0.0.0/8` 都识别为环回） |
| `http://127.1.2.3:7864` | 不允许环回 | 拒绝 |
| `http://[::ffff:127.0.0.1]:7864` | 允许/不允许环回 | 通过 / 拒绝（IPv4-mapped 被折算回 IPv4 后判定） |
| `http://10.0.0.5`、`http://172.16.0.9`、`http://192.168.1.10` | 允许环回 | 拒绝（私网） |
| `http://169.254.1.1` | 允许环回 | 拒绝（链路本地） |
| `http://[::ffff:10.0.0.5]` | 允许环回 | 拒绝（映射后私网） |
| `http://8.8.8.8` | 任意 | 通过 |
| `ftp://example.com` | 任意 | 拒绝（非 http/https） |
| `http://169.254.169.254`、`http://[::ffff:169.254.169.254]`、`http://metadata.google.internal` | 即使 `allow_private=True` | 拒绝（云元数据硬红线） |

### 5.2 真实接口

- `POST /api/provider-credentials` 依次传元数据字面量、IPv4-mapped 写法、`metadata.google.internal`：全部 400，detail 含“元数据”。
- `PATCH /api/providers/codebuddy {"base_url":"http://169.254.169.254"}`：400，且 provider 的 `base_url` 保持原值（未落库）。
- `POST /api/sub2api/accounts` 的 `upstream_base` 传元数据：400，detail 含“元数据”。
- 环回：为 codebuddy 创建 `enabled=false`、`base_url=<假上游 127.0.0.1>` 的凭据：200，随后删除，证明“允许环回的 provider”在保存期可通过。
- **私网拒绝无法在本次真实接口上验证**：测试基建设置了 `UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1` 才能指向假上游；`GET /api/security/guard` 如实返回 `allow_private_upstreams=true`。生产默认见 `app/config.py` 为 `False`，私网拒绝已由 §5.1 的函数级实测覆盖。

### 5.3 保存时校验 vs 请求时解析

- 保存时：`guard_upstream_url` 会做一次 DNS 解析并把结果与私网/元数据黑名单比对。
- 请求时：`app/adapters/base.py`、`client.py`、`codebuddy.py`、`opencode.py` 均未调用守卫；`sub2api_store.py` 只在保存侧调用。httpx 在发送时自行重新解析域名。
- 重定向：`client.py` 未设置 `follow_redirects`，httpx 默认不跟随，302 跳元数据不成立。
- **结论：DNS 重绑定窗口仍存在**（保存时公网、请求时解析到 `169.254.169.254`/私网即可）。需要 admin 写地址权限 + 域名解析控制，未登录/viewer 不可利用，因此定为“中”而非高危；sub2api 账号地址同理。

## 6. 测试运行

```powershell
cd apps\control   # 相对仓库根
$env:UNIFIED_TEST_CTRL_PORT="18129"; $env:UNIFIED_TEST_FAST_PORT="18130"
# 解释器用仓库根 `.runtime\python\python.exe`（按本机环境选择，不写死盘符）
python -m pytest tests/test_security_matrix.py -q
```

输出：

```text
29 passed in 34.13s
```

新增文件：`apps/control/tests/test_security_matrix.py`（29 个用例，覆盖权限矩阵 40+ 方法+路径 × 3 身份、归属、审计、guard、密钥泄露、SSRF、重绑定）。

## 7. 未能验证 / 环境限制

- **真实 uvicorn access log 行**：测试基建以 `--log-level warning` 启动，access log 不产生；只在函数/格式化器级验证脱敏，未拿到端到端日志文件证据。
- **DNS 重绑定端到端利用**：无法控制 DNS；结论基于“保存时解析一次 + 请求路径无守卫 + 不跟随重定向”的可证结构。
- **生产态私网拒绝的真实 HTTP 验证**：受 `UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1` 限制，只能做函数级验证；生产默认值已从 `app/config.py` 确认。
- **`/api/settings/upstream` 里的 URL 字段**：代码上该接口不做地址守卫，但这些值只作为配置内容下发给上游，不作为控制面出站目标；未做写库实测，因此未列为可利用发现。
- **网络依赖端点**：`/api/system/check-update` 未实测（可能访问外网）；其余矩阵端点均已实跑。
- **并发/竞态**：未做多进程、并发登录失败与配额并发的压力验证。

> **（2026-11-05 已更正）** 上文端点已于 2026-11-05 下线（`apps/control/app/routers/system.py` 不再注册该路由），该条的「未实测」对象已不存在；当前矩阵里与更新相关的只剩 `GET /api/system/update-status`，默认 `UNIFIED_UPDATE_ENABLED=false` 时不发外网请求。
>
> **这条为什么重要**：把一个 404 端点继续记成「漏测的网络攻击面」，会让复测报告一直挂一条无对象的遗留项，也会让「外呼目标待确认」被反复引用而实际已无外呼。
