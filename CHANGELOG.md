# Neosub2api 更新日志

> 未发布变更按日期分段记录，最新在最上面；`0.1.0` 是已发布版本，保留原样供升级对照。

## 未发布 - 2026-11-06

### 上游与转发

- **`opencode` / `trae` / `qoder` 的 readiness 由 `ready` 降为 `needs_config`**：三家此前只因 `ProviderSpec` 默认值被算作就绪，实际没有任何真实外呼证据；`requires` 分别写明缺少的密钥类型、必需字段与未验证项。影响服务商页/模型页的「就绪」徽标与 `/api/health/ready` 的 `available` 计数。

### 调度

- **账号池轮转接线扩到 `muse` / `zcode` / `opencode` / `codar` / `trae` / `qoder`**：分档冷却、连败升级、熔断、粘性、权重与轮转组走 `services/rotation.py`；四档策略（`sticky` / `priority` / `weighted` / `failover`）仍只接 `codar`（`services/scheduling.py`），其余适配器是池的 `sticky → weighted → round_robin`。影响多账号凭据的换号与冷却，`health()` 会回 `rotation` / `cooling`。`weighted` 无权重降级当前不生效，见 [docs/SCHEDULING.md](docs/SCHEDULING.md) §7。
- **`key_pool` 改租约式占用**：并发请求不再共用同一把 key，`gptgrok` 同步接入。影响高并发下的账号占用正确性。

### 网关与媒体

- **媒体子工程接入统一准入**：`/v1/images/generations`、`/v1/images/edits`、`/v1/images/tasks/{id}`、`/v1/videos`、`/v1/videos/{id}`、`/v1/media/{name}`、`/v1/files`、`/v1/editable-file-tasks` 统一走鉴权、能力选路、配额与失败限流；影响媒体客户端不再需要绕过网关。实现见 `apps/control/app/routers/v1.py:1401-1602`。
- **新增通用任务队列 `/v1/tasks`**：提交、查询、取消；单 worker、五态（`queued` / `running` / `completed` / `failed` / `cancelled`），只存 JSON 元数据，二进制产物留在上游，记录保留 7 天且最多 200 条；影响长任务调用方可以在网关侧查询与取消，不再只能轮询上游。
- **请求级限流（G3）**：`UNIFIED_RATE_LIMIT_RPM`（每密钥每分钟请求数）与 `UNIFIED_RATE_LIMIT_MAX_CONCURRENCY`（进程内在途 `/v1` 请求上限），默认 0 关闭；超限分别返回 429 `key_rate_limited`（带 `Retry-After`）与 503 `gateway_overloaded`。与失败预算限流、账号冷却分开计数，多 worker 每进程各一份；影响默认行为不变，开启后突发请求会更早被拦。

### 安全

- **凭据落库加密 Phase 1**：`provider_credentials.secret` 与 `provider_config.api_key` 写入时 AES-256-GCM 封存（`u2a-enc:v1:`，复用 `UNIFIED_BACKUP_ENCRYPTION_KEY`）；启动时把存量明文就地迁移并回验；缺密钥时读明文直通、写保持明文并计数，`UNIFIED_SECRET_AT_REST_REQUIRED=1` 直接拒绝启动；`GET /api/security/secret-at-rest` 只返回计数与开关。影响备份归档与库内字段共用同一套密文，轮换密钥前需先解密回写。
- **token 配额与密钥白名单**：配额策略新增 `limit_tokens`，与金额、请求数任一命中即拦截，状态接口同时回 `used_tokens` / `remaining_tokens` / `ratio_tokens`；密钥新增 `model_allow`（`*` 通配）与 `ip_allow`（单 IP / CIDR，写入时解析）。影响可按 token 量控额，坏白名单配置在写入时 400，不会拖到请求时才失败。

### 计费

- **计费规则表**：新增 `price_rules`，支持 `per_call` / `per_second` / `long_context` 倍率，按 provider + model + 生效时间取版本；`GET /api/pricing/rules` 开放读，`POST /api/pricing/rules` 仅管理员且只追加。影响图片、视频与长上下文可按非 token 口径计费。
- **视频任务台账**：新增 `video_tasks`，按 `(provider, upstream_task_id)` 登记状态、秒数与金额；状态 `pending` / `running` / `succeeded` / `failed`，同状态重复回调幂等，终态后不能改状态，只有 `succeeded` / `failed` 能 `book()` 回填金额；影响视频计费可对账，不再只依赖上游响应。

## 未发布 - 2026-11-04

### 网关行为

- **模型解析补 `provider_not_found`(404) 与 `conflicting_provider_hints`(400)**：判定顺序固化为「本地存在性 → 头/前缀冲突 → 停用(503 `provider_disabled`)」，`x-provider` 与 `provider/` 前缀同源时按前缀剥离后转发；影响显式指定 provider 的调用方，写错 provider 名或头与前缀互相矛盾时，错误直接指明改哪里，不再被归进 `model_not_found` 或静默取其中一个来源。

### 计费与错误契约

- **断流不再写 `billing_ledger`**：`settle()` 增加 `failed` 参数，按 `request_logs.error` 判定失败，`usage_missing` 语义保留给「正常结束但无 usage」；影响按流水做对账的计费口径，断流请求不再产生计费行。
- **Anthropic 错误壳补 `error.provider`**：与 OpenAI 壳的 `detail.provider` 对称；影响按错误来源分流的 Anthropic 客户端，不再需要自己从别处猜 provider。
- **`POST /api/settings/secret/reveal` 失败也落审计**：action 用后缀区分 `bad_root` / `path_not_found` / `not_a_secret`，明文不入行；影响排查 reveal 误用的管理员行为，失败尝试同样可追溯。
- **Anthropic 错误壳所有 `GatewayError` 统一带 `error.code`**：与 OpenAI 壳同一错误拿到同一个 code；影响按错误码分流的 Anthropic 客户端，不再只能解析 `message` 文案。
- **`billing_ledger` 加 `log_id` 做行级关联，日志页新增「扣费」列**：`credit_status` 五档 `billed` / `unpriced` / `usage_missing` / `not_billed` / `unlinked`，`unpriced` 可点击现查 `/api/pricing/resolve`；影响排查「这请求扣没扣钱」的运维与管理员，逐条日志直接显示扣费状态与金额，未定价模型当场能查到缺价原因。

### 上游与转发

- **`qoder_sidecar` 补 `responses` 能力位与 `/v1/responses` 转发**：影响走该侧车的客户端，`/v1/responses` 现在有对应的转发路径。
- **上游 client 按 `(base_url, key 指纹, proxy, 附加头)` 进程级复用**：裸跑 8 并发 0.7 QPS → 19.2 QPS、p50 11.9s → 344ms；影响所有上游调用的吞吐与延迟，连接不再每请求重建。
- **合规与能力声明补齐**：`opencode` / `sub2api` / `qoder` 直连补 `risk_level` + `risk_note`（GPL-3.0 / LGPL-3.0 隔离要求、clean-room 说明）；`ProviderSpec` 新增 `limitations`，`deepseek_sidecar` 声明 messages 只支持非流式；`trae_sidecar` 的 `oauth` / `credits` 能力修正为 False；影响服务商页与账号页，许可证风险与真实可用能力如实展示，不再对 trae 账号显示做不到的 OAuth / 积分操作。

### 管理面

- **`last_used_at` 命中 0 行不再空提交**：影响每次网关请求的写开销，省掉一次无意义的 UPDATE。
- **`ensure_indexes` 接入启动流程**：影响升级与新装，首次启动会补齐缺的索引。
- **`POST /api/settings/upstash/test` 从桩改为真实只读探测**：`httpx`（`trust_env=False`、10s 超时）只发一条 PING pipeline 不写数据，失败按控制面壳回 `error.code`，共 7 类（`invalid_upstash_url` / `upstash_token_missing` / `upstash_auth_rejected` / `upstash_rejected` / `upstash_unavailable` / `upstash_timeout` / `upstash_unreachable`）；影响配置 Upstash 的管理员，保存前能验证真实连通性与凭据，错误能区分「填错了」和「连不上」。

### 性能

- **三个热路径索引**：`idx_billing_ledger_scope`、`idx_billing_ledger_key`（billing_ledger 覆盖索引）与部分索引 `idx_request_logs_failure`，启动时幂等补齐存量库；实测 20 万行日窗口聚合 2180ms → 38ms、失败限流窗口计数 945ms → 1.75ms；影响配额统计与失败限流在大表下的查询延迟。
- **单请求建连 8 → 4 次**：PRAGMA/chmod 移到建库时执行一次，准入段（网关、配额、失败限流、价格、模型解析）5 次连接合并为 1 次共用；连接税 11.5 → 5.7ms，压测 8/32 并发吞吐 +24% / +35%；影响每个请求的固定开销。

### 前端

- **金额格式化改分段小数位 + 币种**：`0.000125 USD` 不再显示成 `0.0001`；影响价格、配额、用量各页的小额金额展示。
- **账号页能力取「已启用 provider 并集」**：能力接口失败时隐藏操作并给出原因，不再回退硬编码的 CodeBuddy 能力；影响接入多家 provider 的账号页，按钮不再指向未实现的动作。
- **窄屏（390/768）修复**：配额策略下拉、设置页 tab 与字段行、配额页 tab、批量添加弹窗；影响手机与半屏窗口下的操作可达性。
- **表格横向滚动加 44px 边缘渐隐、关键列设最小宽度、时间列表头提到 12px**：影响日志、审计、账号等宽表的可读性。
- **对比度调整**：新增 `--destructive-text` / `--warning-text` 两个 token，暗色 `--chart-1` 提亮，状态色硬编码清零；影响暗色模式下警示文字的辨识度。
- **深色模式 `color-contrast` / `button-name` / `label` 三类 serious+critical 违规修复**：账号页 10px 灰字对比度、审计/安全页图标分页按钮与设置页开关补 `aria-label`、设置页输入框补可访问名称；影响读屏与键盘用户，这三类最重的 axe 告警在源码层已消除。
- **两处页面根节点改 `<main>`**：主布局与登录页各一处；影响读屏的页面地标结构，每页有了唯一的 main 区域。
- **浮动按钮遮挡修复**：主内容容器加 `pb-40 md:pb-24`，桌面端收敛不留多余空白；浮动按钮本身是骨架元素未动；影响手机端最后几行内容不再被浮动按钮盖住。

### 部署

- **`apps/control/Dockerfile` 改为 `WORKDIR /repo/apps/control`**：修掉容器内 `parents[3]` 找不到仓库根的 `IndexError`；影响所有用该镜像的部署，构建上下文保持仓库根。
- **`apps/web/nginx.conf` 补 `root`/`index`**：两处根因修掉后移除三份 compose 里的绕过补丁；影响根 compose、`deploy/` 与 prod 三种起法，不再依赖源码挂载与 command 覆盖。
- **nginx 脱敏别名扩到 18 个、map 链 10+10、`u2a_` 正则补裸参数形态**：影响反代访问日志，更多查询参数与裸 `u2a_` 值不再明文落盘。
- **nginx 查询串脱敏加固（当前链长 20+20）**：map 正则改 `~*` 大小写归一，参数名与 `u2a_` 前缀按逐字符「原字符|%HH」展开认一层 URL 编码，分隔符同时认 `=` 与 `%3d`，任意参数名下的 `u2a_` 值一律脱敏；三份配置（`apps/web/nginx.conf`、`deploy/nginx.conf`、`deploy/web-nginx.conf`）的脱敏块按同一份字节生成、逐字节一致；影响反代访问日志，`?API_KEY=`、`?%74oken=` 这类变体写法同样落 `***`，三种部署起法的脱敏行为完全一致。
- **新增 `apps/web/.dockerignore`**：根目录那份管不到 web 自己的 build context（`./apps/web`），补上后构建上下文文件集 905.7MB → 6.3MB；影响每次 `docker build` web 镜像的上下文上传体积与速度。

### 依赖与安全

- **`fastapi` 0.115.6 → 0.162.0，显式钉 `starlette==1.3.1`**（原钉版 `<0.42.0` 无修复路径，本次直接跨版本升级）：修掉 starlette 三个 HIGH——GHSA-7f5h（Range 请求 DoS）、GHSA-wqp7（Windows 下 UNC 路径导致 NTLM 凭据泄露）、GHSA-82w8（表单解析限制失效）；全量回归 `611 passed / 1 skipped / 0 failed`（另 3 个 subtests 通过），耗时 171.42s，唯一 skip 是 Windows 不支持 POSIX 权限位。

### 测试与文档

- **全量基线 443 passed / 1 skipped / 0 failed / 0 xfailed，125.07s**：影响回归对照的参照值，唯一 skip 是 Windows 不支持 POSIX 权限位的环境性跳过。
- **测试目录 36 个 `httpx` 调用点全部带 `trust_env=False`**：消除本机代理导致的 387s 假慢，带 `HTTP_PROXY` 跑与无代理耗时一致；影响本地与 CI 的跑测时长。
- **新增三个测试文件**：`test_capability_truth.py`（能力声明真实性审计）、`test_concurrency_races.py`（并发竞态）、`test_upstash_probe.py`（Upstash 探测各失败分类）；影响回归覆盖面，能力假声明、竞态与探测错误分类从此有专测盯着。

### 破坏性变更

- **Anthropic 壳 503 `provider_disabled` 的 `error.type` 从 `overloaded_error` 改为 `api_error`**：按 `overloaded_error` 做退避重试的 Anthropic 客户端不再自动重试「provider 已停用」这类配置错误，会直接把错误透出来，需要运维去服务商页启用；上游 503 `provider_unavailable` 仍映射 `overloaded_error`，不受影响。
- **老库升级后存量日志在日志页显示 `unlinked` 且扣费金额为空**：`billing_ledger.log_id` 本次只加列不回填，升级前的成功日志关联不上流水；需要看历史金额的，得另行回填 `log_id`，本版不含。

## 0.1.0 - 2026-11-02

### 安全
- 上游配置（`/api/settings/*`）读写一律要求管理员，返回中的 `api_key` 只给掩码
- 管理员口令不再写死：缺省时首次启动随机生成，写入 `data/admin-password.txt`（0600）
- docker compose 默认只映射 `127.0.0.1:8010`，需要对外访问须显式设置 `UNIFIED_BIND_HOST`
- 网关匿名开关只接受严格布尔值，字符串 `"false"` 不再被当作真值
- 数据目录收紧到 0700、库文件（含 WAL/SHM）收紧到 0600
- 登录失败按 IP + 用户名节流：连续 5 次失败锁定 15 分钟
- 服务端固定安全响应头（nosniff / X-Frame-Options / Referrer-Policy / CSP frame-ancestors）
- CORS 改为精确方法与请求头白名单，不再通配
- 接口文档默认关闭，需显式设置 `UNIFIED_ENABLE_DOCS=1`
- 上游 4xx/5xx 只回显状态码，内部地址与响应体只进日志

### 新增
- `GET/POST/PATCH/DELETE /api/tokens`：把既有的 `u2a_` 网关密钥薄封装成「访问令牌」视图，写操作仅管理员
- `GET /api/system/changelog`：解析本文件，文件缺失时返回空列表而不是报错
- `GET /api/system/update-status`：如实报告本部署不支持在线更新

### 说明
- 本部署不提供在线更新，也不操作宿主机 docker；版本升级需自行拉取镜像与代码

## 未发布 - 2026-11-03

### 网关行为

- **未知模型默认返回 404**：`/v1/*` 遇到本地目录里没有的模型名，不再兜底到第一家 chat provider，而是返回 `404 model_not_found`；模型只存在于未启用 provider 时返回 `404 model_unavailable`；同名多家命中返回 `400 ambiguous_model`（带 `candidates`）。存量客户端可设 `UNIFIED_MODEL_FALLBACK_FIRST=1` 恢复旧兜底，该开关只处理「一家都没命中」，不掩盖歧义。
  影响：所有 `/v1` 客户端；依赖旧兜底的脚本会开始收到 404，需要改成 `<provider>/<model>`、带 `x-provider` 头，或显式打开兜底开关。
- **`GET /api/models` 新契约**：条目新增 `provider` / `providers` / `source`（`upstream` | `declared` | `declared_fallback`）/ `provider_states` / `unavailable_reason` / `fallback_reason`；支持 `?provider=<id>` 只看一家，未知 id 返回 404，且不再按 enabled 过滤。模型页已改调该端点。
  影响：直接消费 `/api/models` 的管理面前端与集成方；只读 `models/source/count` 的旧调用仍兼容，但需要新字段的展示要跟着改。
- **失败请求限流**：同一密钥在滑动窗口内失败次数达到上限后，后续请求返回 `429 failure_rate_limited`，`detail` 含 `used/limit/window_sec/reset_at/reset_after`，并带 `Retry-After`；被拒请求本身也会记一条 429 日志。窗口与阈值由 `UNIFIED_FAILURE_WINDOW_SEC` / `UNIFIED_FAILURE_WINDOW_MAX` 控制，任一为 `0` 即关闭。
  影响：会连续失败的客户端（重试风暴、错配模型、错误凭据）会更早被拦截；监控按 429 与 `failure_rate_limited` 统计时要把这类拒绝和上游 429 区分开。
- **`x-api-key` 兼容**：`/v1/*` 除 `Authorization: Bearer u2a_...` 外，也接受 Anthropic 官方 SDK 默认发送的 `x-api-key: u2a_...`；两个头同时带且值不同返回 `401 conflicting_auth_headers`，两个头同值放行。
  影响：可以直接用 Anthropic SDK 的 `api_key=` 接入，不必再改成 `auth_token`；自己拼头的客户端要保持单头，避免冲突。

### 管理面与权限

- **`/tasks` 页面整体删除**：签到记录、任务日志、上游日志、任务执行、认领计划对应的页面、导航入口、账号页跳转与全部 i18n 文案一并移除；后端从未实现这些端点，因此没有补接口。
  影响：收藏过 `/tasks` 的用户会看到 404；脚本若曾调用 `/api/task-run`、`/api/task-logs` 等路径，本来就会失败，现在前端也不再提供入口。
- **密钥归属 `owner_user_id`**：`api_keys.owner_user_id` 存用户名（`users.username` 是 TEXT 主键）。admin 可查全部、可在创建时指定归属；非 admin 只列自有密钥，配额读接口只能查自有 key，查他人（含历史 `NULL` 归属）返回 403。
  影响：viewer 的密钥列表与配额视图会变短；按密钥归属做统计或自动化的脚本要用 `owner_user_id`，历史无归属密钥只有 admin 可见，需要显式补归属或吊销。
- **审计与登录失败权限收紧**：`GET /api/audit-logs` 非 admin 只能看到 `actor=自己` 的记录，显式传他人 `actor` 返回 403；`GET /api/security/login-failures` 非 admin 只返回 `scope=self` 的聚合计数，不含 IP 与他人信息。
  影响：viewer 的安全页不再显示全站审计与登录失败明细；admin 行为不变，需要跨用户排查时仍用 admin 账号。
- **运行参数进库**：新增 `GET/POST /api/settings/runtime-config` 与 `GET /api/settings/runtime-config/export`、`POST /api/settings/runtime-config/import`（均管理员）。可改 `models_cache_ttl_sec`、`failure_window_sec`、`failure_window_max`、`model_fallback_first`、`sse_max_seconds`、`session_ttl_sec`；数据库值在启动时覆盖 env 默认值并重启后保留，导入会整批校验、任一项非法则不写入，密钥类字段明确拒绝。
  影响：运维不再只靠改环境变量重启；排障时要同时看「数据库值 / env 默认值」，界面会标 `source=db|default`。

### 运维与体验

- **演示模式 `UNIFIED_DEMO_MODE`**：默认关闭；开启后控制面在进程内拉起仓内假上游，注册 `demo_local` provider，并通过真实网关链路播种带 `[demo]` 标记的密钥、账号、日志、用量、配额与价格数据。`scripts/demo.ps1` / `scripts/demo.sh` 可一键启动或 `-Clear` 清除；只允许绑定回环地址，非本机来源请求返回 403。
  影响：第一次接触仓库的人可以十分钟内看到有数据的界面；演示数据与真实数据靠标记隔离，但不能用于生产（假上游、明文演示密钥、演示价格都不是真实报价）。

### 文档

- 重写仓库根 `README.md`，新增 `docs/README.md` 文档索引；测试基线改为「本次提交时实测」，不再把某个固定数字当作长期承诺。
  影响：新读者从 README 进入即可找到上手、契约、运维与合规入口；维护者改行为时需要同步本文件。
