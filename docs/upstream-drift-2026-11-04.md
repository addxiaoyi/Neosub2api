# 上游读码复核 · 2026-11-04

对两家优先复核上游的新提交做**逐文件读码**，判定我方 sidecar 调用路径是否出现真实行为差异。只读上游、只写结论，未改任何业务代码，未动 `upstreams/upstreams.yaml` 的 `checked_at` / `analyzed_commit` / `analyzed_at`。

## 结论速览

| 上游 | 区间 | 我方路径 | 判定 |
| --- | --- | --- | --- |
| AiXiaoc/codar2api | `d7f5024 → ba05367`（v0.1.4，1 commit / 44 files） | `codar_sidecar.py` HTTP 透传到本地 codar2api 进程 | **影响（需修复·部署侧）**：`dsh_identity_rewrite` 默认开且语义改为「无条件插入 Codar 身份串」，`identity_first_line_fill` 默认 false→true，升级 sidecar 后出站提示词字节会变；鉴权头不受影响。代理重试 / 403 指纹拉黑默认关，**不影响**，且我方请求形态不可能被误触发 |
| NextAgentX/qoder-trae-workbuddy-switch | `93d873c → 5cf62da`（15 commits / 85 files，v2026.11.41007） | `qoder_sidecar.py` HTTP 透传到本地 buddy-switch-gateway | **不影响**：全部网关改动只构造 `RequestMeta` 落 `gateway_logs.json` 日志，`cached_tokens` 不进 `/v1` 响应体；`/api/sessions/export\|import` 是管理面；#165 落在前端地址派生函数 |

---

## 一、AiXiaoc/codar2api（d7f5024 → ba05367）

- compare：<https://github.com/AiXiaoc/codar2api/compare/d7f5024...ba05367>
- 覆盖：44 个文件中，11 个运行时 Go 文件（`compat.go`、`relay.go`、`upstream.go`、`store.go`、`proxy.go`、`proxy_retry.go`、`request_blocklist.go`、`detail_capture.go`、`panel.go`、`app.go`、`account_import.go`）逐 hunk 读完，其中 `store/relay/compat/proxy/upstream/proxy_retry` 按 `ba05367` 全文取回对照旧版；`compat_test.go`、`identity_formats_test.go`、`forwarding_test.go` 逐行读（确认新默认值与新语义）；其余 8 个 Go 测试、9 个 `web/*` 面板前端、7 个 `webtests/*`、CI/README/CHANGELOG 按 hunk 主题核对，均为新特性配套测试与面板 UI，不触及 relay/proxy 出站链路。

### 1.1 变化点与证据

| 文件:行（`ba05367`） | 变化 | 对我方判定 |
| --- | --- | --- |
| `internal/gateway/compat.go:6` | `codarIdentity = "You are an AI agent running on the Codar runtime."` 替换原 `harnessIdentity`/`neutralIdentity` 常量（旧 `compat.go:6-7`） | **影响**（见 Q1） |
| `internal/gateway/compat.go:49,82-83` | `transformIdentity` 从「仅当 prompt 以 `harnessIdentity` 开头才替换」（旧 `compat.go:72-73`）改为「不以 `codarIdentity+"\n"` 开头就无条件 `insert`」 | **影响**（见 Q1） |
| `internal/gateway/compat.go:106` | `transformIdentityPrompts` 作用面不变：`/v1/chat\|messages\|responses\|count_tokens` 的 system / developer / instructions | 不变 |
| `internal/gateway/store.go:171` | `dsh_identity_rewrite` 默认 `true`（旧 `store.go:164` 同为 true，未变） | **影响**（见 Q1） |
| `internal/gateway/store.go:172` | `identity_first_line_fill` 默认 **false→true**（旧 `store.go:165` 为 false） | **影响**（见 Q1） |
| `internal/gateway/store.go:182-183` | 新增 `proxy_connect_retry` / `proxy_connect_switch`，默认均 `false` | 不影响（opt-in） |
| `internal/gateway/store.go:185` | 新增 `restricted_request_block`，默认 `false` | 不影响（opt-in，见 Q2） |
| `internal/gateway/proxy_retry.go:20-21,75,109,113` | 新文件：`proxyPolicy()` 读两键；`doWithPolicy` 中 `policy.retry` 与 `policy.switchProxy` 分别门控同代理重试与换代理，两键皆 false 时单次 `p.Do` 后原样返回 | 不影响（默认关时行为与旧版等价） |
| `internal/gateway/relay.go:108-115` | 每请求读开关：`blockRestrictedRequest`、指纹计算、`transformIdentityPrompts`（身份改写发生在 `forwardedBody` 之前） | 见 Q1/Q2 |
| `internal/gateway/relay.go:218,384-388` | 拉黑命中检查与入库**都**以 `blockRestrictedRequest` 为前提 | 见 Q2 |
| `internal/gateway/relay.go:336`、`upstream.go:179-181` | 出站统一改走 `doWithPolicy`，policy 全 false 时无重试无切代理 | 不影响 |
| `internal/gateway/proxy.go:615-627` | `GotConn` 之前连接失败返回类型化 `proxyConnectError`，错误文案与旧通用错误完全相同 | 不影响 |
| `internal/gateway/upstream.go:46-54` | `restriction()` 判据：**上游响应体** `code/type == "account_restricted"`（响应驱动，非请求形态判定） | 见 Q2 |
| `internal/gateway/upstream.go:158` | `Authorization: Bearer <账号token>` 构造未变（diff 未触及） | 鉴权不受影响 |
| `internal/gateway/store.go:165-167`（旧 `:159-161`） | `use_codar_ua`、`auto_passthrough` 默认仍为 `true`，schema 描述未改 | 不影响 |
| `internal/gateway/store.go:168` | `detailed_debug` 默认 `false`；`detail_capture.go`（diff `@@ -313,8 +316,17`）新增 `request_debug_scope`，默认 `errors` 与旧「仅保留失败」行为一致 | 不影响 |
| `internal/gateway/app.go`（diff `@@ -20,7` / `@@ -86,11` / `@@ -111,8`） | `Version=0.1.4`、新增 `requestBlocks` 启动加载 | 不影响（不开启则文件恒空） |
| `internal/gateway/panel.go`（diff `@@ -32,13`、`@@ -134,6`、`@@ -197,24`、`@@ -228,6`） | 新增 `GET /accounts/catalog`、`POST /catalog/proxy`、`POST /request-blocklist/clear`；账号导入改为 `importStoredToken`（`account_import.go`，不再联上游验 token） | 管理面，不影响 `/v1` |
| `internal/gateway/forwarding_test.go`（`@@ -23,7`、`@@ -68,7`） | 自动透传默认开、`Authorization` 换成账号 token 的断言保留（仅测试内 token 取值方式微调） | 不影响 |
| `internal/gateway/defaults.json` | 与 `d7f5024` **逐字节一致**（仅价格表与验证码配置，不含任何设置键） | 新增设置键只能走 schema 默认（false） |
| `Dockerfile`、`web/*`、`*.md`、CI | 版本注入、面板 UI、发行说明 | 不影响 |

### 1.2 问题一：`dsh_identity_rewrite` 默认值与出站身份串

**默认值 = `true`（新旧相同），但语义已变，出站身份串一定会变：**

- 旧语义：只有 prompt 开头恰好是 `You are an AI agent powered by DeepSeek Harness.` 才被替换成中性文本；普通用户的 system/developer 提示词**原样不动**（旧 `compat.go:72-73`）。且 `identity_first_line_fill` 默认 false（旧 `store.go:165`）。
- 新语义：只要 prompt（含纯 `Keep this.` 这类无身份提示词）不以 `You are an AI agent running on the Codar runtime.` 开头，就**无条件在 system/developer/instructions 开头插入该身份串**；`identity_first_line_fill` 默认翻转为 true，还会在最前加 `.` + U+200B（`.​X`）。上游自己的测试把这一语义钉死：`identity_formats_test.go` 新增用例断言 `"Original rules."` → `".​XYou are an AI agent running on the Codar runtime.\nOriginal rules."`；`compat_test.go` 的 `TestIdentityOnlyChangesExistingInstructions` 断言「无 system/developer 的请求不变」（即用户消息不动）。
- 是否真的生效于存量部署：`store.go:360-368` 用 schema 默认构建，`store.go:500-519` 用 `settings.json` 覆盖，`store.go:743-757` `SaveSettings` 会把**全量键**落盘。因此：
  - 全新数据目录 / 从未保存过面板设置的部署 → 升级后 `fill=true`，且新插入语义生效 → **出站提示词必变**；
  - 保存过设置的部署 → `identity_first_line_fill=false` 被保留，但 `dsh_identity_rewrite=true` 的**插入语义**照样生效 → 出站提示词同样变（除非该部署本来就关了 rewrite）。
- **对上游鉴权 / 风控的影响**：
  - 鉴权：**不影响**。改动只碰请求正文，`upstream.go:158` 的 `Authorization: Bearer <账号token>` 构造未动，`auto_passthrough` / `use_codar_ua` 默认未动（`store.go:165-167`）。
  - 风控：**待观察**。身份串是提示词内容而非头部，是否被 Codar 平台做内容指纹无法从公开代码证实；该身份串是 codar2api 对 Codar 平台的自称文本，属上游自身设计。
- **判定：影响 → 标「需修复」（部署/文档层，我方无代码缺陷）**：我方 `codar_sidecar.py` 是纯透传，无可改代码点；修复动作是部署侧——升级 sidecar 到 v0.1.4 前后显式处置这两个键（保持旧行为需同时 `dsh_identity_rewrite=false`、`identity_first_line_fill=false`），以及文档层同步第 1.5 节列出的文件。按任务要求在回复第一段点名，未自行改代码。

### 1.3 问题二：代理重试与 403 指纹拉黑，我方是否可能被误触发

**默认关闭，且误触发在机制上不可能。**

- 两键默认 false：`store.go:182-183,185`；`defaults.json` 未改（逐字节一致），旧设置文件里不存在这两个新键 → `defaults()` 给 false；`panel.go` 保存时才会落盘新值。`yes()` 对缺失键取 false，`proxyPolicy()`（`proxy_retry.go:20-21`）与 `blockRestrictedRequest`（`relay.go:110`）都按此求值。
- 指纹拉黑的三重门控：
  1. `relay.go:110-113` — 只有 flag 开才计算 `requestFingerprint`（path + 原始 JSON 摘要，计算在身份改写 `relay.go:115` **之前**）；
  2. `relay.go:384-387` — 入库要求 flag 开 **且** 上游真的返回 403 **且** 响应体 `code/type == account_restricted`（`upstream.go:46-54`）——**响应驱动**，不是拿我方请求形态去比对入库；
  3. `relay.go:218` — 命中检查同样要求 flag 开，flag 关时即使文件里有历史指纹也不查。
- 结论：默认关闭时，我方调用面（用户提示词再怎么变）既不会入库也不会被查；即便运维手动开启，入库也必须先拿到真实的 `account_restricted` 403，属上游账号风控触发，不是「形态误判」。届时我方只能透传该 403，清除需进 sidecar 面板 `POST /request-blocklist/clear`（`panel.go` diff `@@ -134,6`）。→ **判定：不影响；待观察项 1 条（运维开启后的处置路径，见第三节）。**
- 代理重试同理：`doWithPolicy`（`proxy_retry.go:75`）在两键皆 false 时只执行一次 `p.Do`，连接失败返回与旧版文案相同的错误（`proxy.go:615-627`）；且切代理仅在 `route.ID > 0`（配置了代理池）时才可能，我方直连部署 `route.ID == 0` 连类型化错误分支都进不去。→ **不影响。**

### 1.4 我方路径对照

- `apps/control/app/adapters/codar_sidecar.py:46-48`（`/v1/messages` 透传）、`openai_compat.py:39,54`（`/v1/chat`、`/v1/models`）：请求体原样发出，改写发生在 sidecar 进程内部 → 我方代码不变，**受影响的是出站字节**（见 Q1）。
- `apps/control/app/adapters/codar.py:46,270,274`：仓内直连自研适配器，**根本不经过 codar2api**，`compat.go` 改动与它无关。
- `/v1` 协议面：路由、错误壳、透传、`count_tokens` 语义均未变；`store.go` 的 `modelUnavailable` 余额检查扩展（diff `@@ -522`）只在 `model==""` 时多拦一种情况，我方调用必带 model，行为不变；`catalog()` 改走 `CatalogRoute`，未配目录代理时 `parseProxyID(nil)=0`（`proxy.go:33-70`）→ `pinLocked(0)` 返回直连（`proxy.go:161-166`），`/v1/models` 行为不变。

### 1.5 将来同步（推进基线）需要动的文件

| 文件 | 动作 |
| --- | --- |
| `apps/control/app/adapters/codar_sidecar.py` | 无需改（`/v1` 转发面与错误壳未变） |
| `docs/providers/codar2api.md:87` | 需更新：「把固定文本**替换为中性文本**」已过时，新语义为「未带 Codar 身份即插入 + 首行填充」；`docs/providers/codar2api.md:11` 的 v0.1.3 基线说明如需推进一并改 |
| `providers/codar2api/README.md:54-60` | 已含 v0.1.4 差异说明（含两键处置），无需再动 |
| `upstreams/upstreams.yaml:121-128` | **仅在人工确认同步时**推进 `analyzed_commit` / `analyzed_at` / `checked_at`（本次未动） |
| 部署侧 | 升级 sidecar 时显式处置 `dsh_identity_rewrite` / `identity_first_line_fill`（见 1.2「需修复」） |

---

## 二、NextAgentX/qoder-trae-workbuddy-switch（93d873c → 5cf62da）

- compare：<https://github.com/NextAgentX/qoder-trae-workbuddy-switch/compare/93d873c...5cf62da>（中间还含 `93d873c → 930d149` 一段）
- 覆盖：8 个 crate / 运行时文件（`logging.rs`、`usage_tap.rs`、`routes/{chat,messages,responses}.rs`、`state.rs`、`buddy-switch-server/api.rs`、`Cargo.toml`）逐 hunk 读完，`src-tauri/commands.rs`、`session_bundle.rs`、`mod.rs` 按 hunk 核对；其余 60+ 文件为前端 `src/**`、locales、docs、npm 打包、脚本与锁文件，逐文件核对归属（其中 #165 相关 `gateway-url.ts`、`gateway.ts` 读了完整 patch），不触及网关路由与出站。

### 2.1 变化点与证据

| 文件:行（`5cf62da`） | 变化 | 对我方判定 |
| --- | --- | --- |
| `crates/buddy-switch-gateway/src/logging.rs:79` | `RequestMeta` 新增 `pub cached_tokens: u64`，注释明写「`0` = 上游没报 / 本入口拿不到（不是没命中）」 | 不影响（日志字段） |
| `logging.rs:119` | 序列化键 `cachedTokens` 只进 `state.log.record(...)` 的日志条目，落 `gateway_logs.json`（测试 `logging.rs:413`） | **不进 `/v1` 响应体** |
| `logging.rs:187-201` | `StreamingLogSink::finish` 把 `UsageReport.cached_tokens` 写进流式请求日志 | 不影响 |
| `crates/buddy-switch-gateway/src/protocol/usage_tap.rs:58,91,117-124` | `UsageReport` 新增 `cached_tokens`；`extract_usage(frame: &Value)` **只读**三家字段（`prompt_cache_hit_tokens` → `prompt_tokens_details.cached_tokens` → `cache_read_input_tokens`），钳制 `[0, prompt]` | 不影响（只读响应帧，不改帧） |
| `routes/chat.rs:225,256,314,401,469`、`messages.rs:210,241,293,355,395-415`、`responses.rs:210,241,293,346,393` | 仅给 `RequestMeta` 补 `cached_tokens` 字段（错误路径补 0；`messages.rs:395-397` 从上游 `usage.cache_read_input_tokens` 读真值） | **确认只进日志**：diff 中无任何响应构造/改帧代码，响应仍为上游字节原样转发 |
| `crates/buddy-switch-server/src/api.rs:259-264` | 新增 `POST /api/sessions/export`、`GET /api/sessions/import/preview`、`POST /api/sessions/import`（issue #87），handler 位于 `api.rs` diff `@@ -2513,6 +2521` 起，入口即 `require_capability(Product::Workbuddy, SessionCopy)` 门控 | 管理面，我方不调用 |
| `crates/buddy-switch-core/src/modules/session_bundle.rs`（+621）、`src-tauri/commands.rs` | 会话打包/导入核心与桌面宿主同契约实现 | 管理面 |
| `crates/buddy-switch-gateway/src/state.rs:162-198` | 仅**注释**澄清 `base_url()` 是根地址不含 `/v1`（#165），`GatewayStatusView.base_url` 形态未变 | 不影响 |
| `src/lib/gateway-url.ts`（新文件）、`src/lib/gateway.ts:319-342` | #165 修复落点：前端地址派生单一入口 `gatewayOpenAiBaseUrlFrom`，统一「带 /v1」形态 | 前端展示修复，非网关路由 |
| `crates/buddy-switch-gateway/Cargo.toml` | 版本 `2026.11.21029 → 2026.11.41007` | 版本号 |
| `core/modules/{process,session,codebuddy_cn_ide}.rs` | 测试 pid 约束、`fn→pub(crate)` 可见性、辅助配置，均在桌面宿主侧 | 不影响网关 |
| 其余 `src/**`、locales、docs、npm、scripts | 前端/文档/打包/CI | 不影响 |

### 2.2 问题三：`cached_tokens` 是否只进日志；将来要用改哪一层

- **只进日志，不进响应体。** 证据链：`routes/*` 中该字段唯一的去处是 `state.log.record(RequestMeta)`（`chat.rs:225` 等、`messages.rs:395-415`）；`extract_usage` 入参 `&Value` 只读（`usage_tap.rs:91,117-124`）；三条路由 diff 均无响应构造改动，SSE/JSON 响应继续原样转发上游字节。日志落点是 `gateway_logs.json`（`logging.rs:413`），属 sidecar 管理面，我方从不读取。
- **将来要用，不需要同步上游**——数据本来就在 `/v1` 响应 `usage` 里原样透传给我方，改动全在我方计费/日志层：
  1. 派生缓存 token：`apps/control/app/billing.py:153,174-180`（`split_usage` 已经在读 `prompt_tokens_details.cached_tokens` / `cache_read_input_tokens` 做 cache 价格档）——已有，无需改；
  2. 若要在请求日志里单列 `cached_tokens`：`apps/control/app/billing.py:130`（`totals`）、`apps/control/app/db.py:58,121`（`request_logs` 列，只允许加列）、`apps/control/app/routers/v1.py:119,208-215`（落库）；
  3. 若要页面展示：`apps/web` 的统计/日志页读上面新增的列。
- 若坚持要读 **sidecar 自己的** `gateway_logs.json`（管理面值）：需为 `qoder_sidecar.py` 新增管理面读取通道——不建议，口径也不如直接解析响应 usage 可控。
- **口径提醒**：上游把「没报」写成 0（`logging.rs:79`、`usage_tap.rs:749` 用例），与「未命中」不可区分；我方若展示需沿用或另行区分。
- **判定：不影响。**

### 2.3 我方路径对照与将来同步要动的文件

- `apps/control/app/adapters/qoder_sidecar.py:52-53,56-57`（`/v1/messages`、`/v1/responses` 透传）+ `openai_compat` 基类（`/v1/chat`、`/v1/models`）：纯 HTTP 转发，上游网关日志字段与管理面新增路由都感知不到。
- `qoder.py`（`qoder.py:378`）是 COSY 直连适配器，与 buddy-switch-gateway 无关。
- 将来同步：`qoder_sidecar.py` / `qoder.py` 无需改；`docs/providers/qoder.md`、`qoder-adapter.md` 无过时描述（我方文档未承诺 sidecar 内部日志与 `/api/sessions` 面）；`providers/qoder-switch/README.md:101-113` 已记录本次区间；如做 cached_tokens 展示按 2.2 第 2 步动 `billing.py` / `db.py` / `routers/v1.py` / `apps/web`；`upstreams/upstreams.yaml:130-137` 字段仅人工确认同步时推进（本次未动）。

---

## 三、待观察清单

1. **Codar 平台侧提示词风控**：升级 sidecar 到 v0.1.4 后，我方用户请求的 system/developer 提示词默认会被插入 Codar 身份串并加 `.​X` 首行填充；平台是否做内容指纹无法从公开代码证实。升级后若出现 403 `account_restricted` 频发，优先回查这两个开关。
2. **`restricted_request_block` 被运维开启的场景**：我方用户相同 prompt 的请求若真实触发上游 403 拉黑，主仓只能透传该 403，清除指纹必须进 sidecar 面板（`POST /request-blocklist/clear`）——运维手册应记一笔。
3. **qoder `cached_tokens=0` 的语义**：0 =「上游没报」而非「未命中」（`usage_tap.rs:749` 用例），且 `messages` 之外的路由在无 usage 时补 0（`chat.rs:401`、`responses.rs:393`）；将来展示需同口径处理。
4. **qoder 会话导入是加法写**：重复导入产生副本（`api.rs` #87 注释），属管理面行为，关注后续安全披露（依赖 `Cargo.lock` 版本跟进）。
5. **codar 账号导入不再联上游验 token**（`account_import.go`）：面板信任本地导入的 JWT，属管理面信任模型变化，不影响 `/v1` 调用链。

## 四、复核方式与约束遵守

- 差异来源：`gh api repos/<owner>/<repo>/compare/<a>...<b>` 完整 diff（保存于工作区 `tmp/upstream-drift/`，复核后删除），关键文件另按 `ref` 取全文对照新旧两版；未 `git clone` 任何上游到仓库内。
- 未修改 `apps/`、`providers/`、`upstreams/upstreams.yaml` 及任何业务代码；未执行 git 写操作；本文档是本次唯一新增文件。
- 行号口径：带行号处均为对应 ref（`ba05367` / `5cf62da` / `d7f5024`）的文件行号；无法取得行号的新文件段落标注 diff hunk 头（如 `panel.go diff @@ -134,6`）。
