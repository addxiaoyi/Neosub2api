# 协议差异表（Provider Protocol Matrix）

本表是 14 家已注册 provider 的协议事实清单，**以 `apps/control/app/adapters/*.py` 与 `ProviderSpec` 为准**，不引用旧文档口径。与 `PROVIDERS.md` 冲突时改本表；与代码冲突时改本表。

复核方式（在仓库根执行；改代码后输出应与下面三张表逐行一致）：

```powershell
python -c "import sys,inspect; sys.path.insert(0,'apps/control'); import app.adapters as p; from app.adapters.base import ProviderAdapter as B; mods=[m for m in vars(p).values() if inspect.ismodule(m)]; cls=[c for m in mods for c in vars(m).values() if isinstance(c,type) and issubclass(c,B) and c is not B]; [print(c.spec.id,[k for k,v in c.spec.capabilities.items() if v],c.spec.protocols,c.spec.risk_level) for c in cls if getattr(c,'spec',None)]"
```

## 1. 能力位

`✓` = `capabilities[...] is True`，`—` = `False`，调用返回 `501 capability_unsupported`（`base.py` 的桩）。`register` 不是推理能力位。

| provider | chat | messages | responses | register | runtime | readiness |
| --- | :-: | :-: | :-: | :-: | --- | --- |
| `codebuddy` | ✓ | ✓ | — | — | 管理端直连（7864 → 7863） | needs_config |
| `muse` | ✓ | — | ✓ | — | 内置直连 | needs_config |
| `grok-register` | — | — | — | ✓ | 控制面任务服务 | needs_config |
| `gptgrok` | ✓ | ✓ | ✓ | — | 内置直连 | needs_config |
| `zcode` | ✓ | ✓ | — | — | 内置直连 | needs_config |
| `opencode` | ✓ | ✓ | — | — | 内置直连 | needs_config |
| `sub2api` | ✓ | ✓ | ✓ | — | 内置直连（账号池分发） | needs_config |
| `codar` | ✓ | ✓ | ✓ | — | 内置直连 | needs_config |
| `codar_sidecar` | ✓ | ✓ | ✓ | — | sidecar 直通（8801） | needs_config |
| `trae_sidecar` | ✓ | — | — | — | sidecar 直通（7871） | needs_config |
| `deepseek_sidecar` | ✓ | ✓（仅非流式） | — | — | sidecar 直通（5001） | needs_config |
| `qoder_sidecar` | ✓ | ✓ | ✓ | — | sidecar 直通（7873） | needs_config |
| `qoder` | ✓ | ✓ | ✓ | — | 内置直连（COSY） | needs_config |
| `trae` | ✓ | — | — | — | 内置直连（Solo/Work） | needs_config |

> 2026-11-06 已更正（原单元格值逐字留档）：`opencode` / `qoder` / `trae` 的 readiness 列原均为「ready」。三家的 `ProviderSpec` 实为 `needs_config`（`apps/control/app/adapters/opencode.py:47`、`qoder.py:532`、`trae.py:157`），当前 14 家全部 `needs_config`（其余见 `codebuddy.py:129` 等同字段）。`ready` 只表示「填好 key / base_url 就能调用」，**`ready` ≠ 真机跑通**；没有真实外呼证据的 provider 不按 `ready` 展示（同 `docs/PROVIDERS.md` §「就绪状态」）。
> 误判后果：照旧表会把这三家当成开箱即用，遇到 401 / 429 / 503 时误判为网关故障；排期也会跳过它们的真实链路验证。`PROVIDERS.md`、`README.md`、`API-REFERENCE.md` 中的旧「3 家 ready」口径同批作废。

`responses` 有 `codar`、`qoder`、`qoder_sidecar`、`codar_sidecar`、`muse`、`sub2api`、`gptgrok` 七家声明（2026-11-05 更正，原句与误判后果见下方更正块）。

> 2026-11-04 更正（原句保留作历史）：上一行原记「`responses` 只有 `codar`、`qoder` 两家声明」，`qoder_sidecar` 一行 responses 列原为「—」、§2 上游对话入口原为「sidecar `/v1/chat/completions`、`/v1/messages`」，三处同因与代码不符——`apps/control/app/adapters/qoder_sidecar.py:32` 声明 `"responses": True`、`:55-57` 实现转发、`apps/control/app/routers/v1.py:479-481` 挂载 `/v1/responses`，故一并补正（`codar_sidecar` 不声明该能力位，见 `codar_sidecar.py:8`，不受影响）。（2026-11-05 复核更正：本块末句已失效——`codar_sidecar.py:7-9` 的 docstring 现写明「/v1/responses 经契约交叉验证后声明」，`:30-39` 声明 `responses=True`、`:56-58` 转发；§1 该行已改为「✓」，详见下方 2026-11-05 更正块。）

> 2026-11-04 补记：本行上一段（句中交叉引用）原写作「（该节正文仍记『只有 codar 与 qoder』，待其对应任务更正）」，`docs/API-REFERENCE.md` §「/v1/responses」已于同日按同一口径更正（原句同样在该节下方留档），故交叉引用改为已完成状态。同日一并对齐：`docs/examples.md:8,206,504`、`docs/API-REFERENCE.md:237`、`docs/model-routing.md:191`。

> 2026-11-05 已更正（原句逐字留档）：上一段原记「`responses` 有 `codar`、`qoder`、`qoder_sidecar` 三家声明，与 `API-REFERENCE.md` §「/v1/responses」一致（该节已于 2026-11-04 同步更正，不再记「只有 codar 与 qoder」）。」；§1 表中 `muse` 的 responses 列原为「—」、`sub2api` 的 responses 列原为「—」、`gptgrok` 的 messages 与 responses 两列原均为「—」。四处同因与代码不符：
> - `muse`：`apps/control/app/adapters/muse.py:63` 声明 `responses=True`、`:225-241` 实现（上游 `RESPONSES_PATH` 原样转发，流式逐帧直通）；`muse` 仍**不声明** `messages`，理由见 §1.2。
> - `sub2api`：`apps/control/app/adapters/sub2api.py:67` 声明 `responses=True`、`:181` 转发 `_FORWARD_PATHS["responses"]`（路径表 `sub2api.py:23` → `/v1/responses`）。
> - `gptgrok`：`apps/control/app/adapters/gptgrok.py:400` 声明 `responses=True, messages=True`、`:1323` 与 `:1353` 分别把 ChatGPT 帧合成为 Responses / Anthropic 帧。
> 误判后果：旧表把这三家记成「responses 必 501」、把 `gptgrok` 记成「没有 messages」，客户端会因此绕开本网关直连上游或降级到 chat 兼容层——muse/sub2api 的 responses 与 gptgrok 的 messages 实际都能从 `/v1/responses`（`apps/control/app/routers/v1.py:649`）、`/v1/messages`（`:644`）真实命中。
> 能力位真值：`responses` 的方法体断言在 `apps/control/tests/test_capability_truth.py:78`（`CAP_RULES["responses"] = ("any", ("responses",))`），参数化遍历见 `:86-92`、用例 `:151-158`；三家当前均为真实覆写，无需 xfail。

> 2026-11-05 已更正（原单元格值逐字留档）：`codar_sidecar` 的 responses 列原为「—」，上一段原记「`responses` 有 `codar`、`qoder`、`qoder_sidecar`、`muse`、`sub2api`、`gptgrok` 六家声明」；§2 上游对话入口原为「sidecar `/v1/chat/completions`、`/v1/messages`」、§3 responses 列原为「✗ 501」、§6 第 4 条原记「仍 501 的只剩 `codar_sidecar`」，四处同因与代码不符：
> - `apps/control/app/adapters/codar_sidecar.py:30-39` 声明 `responses=True`（同时声明扩展位 `count_tokens=True`），docstring `:7-9` 写明「/v1/responses 经契约交叉验证后声明」；`protocols` 见 `:40`，已含 `openai.responses`。
> - `:56-58` 的 `responses()` 直接 `forward("/v1/responses", payload, stream=stream)`，`:60-62` 的 `count_tokens()` 转 `/v1/messages/count_tokens`——两者都是 **sidecar 原样转发**，控制面不合成（选路时与 `gptgrok` / `qoder` 的「本进程合成」是两回事）。
> 误判后果：把该 provider 的 responses 记成 501，客户端会绕开 sidecar 直连 codar2api（绕过控制面鉴权/配额/记账），或降级到 chat；把 501 归因到控制面声明还会让维护者去改适配器做无用功。**依据当前工作树改动（`git status` 显示 M，未提交），文档按工作区代码写，未入库。**

> 2026-11-05 补充（能力位覆盖范围）：`apps/control/app/adapters/base.py:9-19` 的 `CAPABILITY_KEYS` 只含 chat / messages / models / responses / oauth / checkin / credits / register / sidecar，**不含** `embeddings` / `images` / `videos` / `media` / `rerank` / `usage` / `count_tokens`。这批位是在 `sub2api.py:66-74`、`muse.py:62-67`、`codar_sidecar.py:30-39` 用字典合并额外声明的（不新增跨会话共享常量），因此 `test_capability_truth.py` 的 `CAP_RULES`（`:75-83`）**覆盖不到**它们；它们的「声明 ⟺ 真实覆写」双向断言放在 `apps/control/tests/test_v1_extension_endpoints.py`。读本表时不要把扩展位当成 `test_capability_truth.py` 已验证过的位。

## 1.1 `/v1` 对外端点总表（2026-11-05 新增）

当前挂载 **14 条路由**（`apps/control/app/routers/v1.py:639-720`）：4 条对话/模型路由 + 10 条能力端点。全部复用同一套准入段（`_gateway_key` 鉴权 → `_json_payload` → `_resolve` / `_pick_capable` → `_quota_guard` → `_failure_guard` → 价格快照 → 记账），**不绕过鉴权与配额**；`_pick_capable`（`:551-582`）在多候选且未指定 `x-provider` 时回 400 `ambiguous_provider`，无候选回 501 `capability_unsupported`。

「原样转发」= 只把 JSON 或字节搬到上游协议路径；「本仓实现」= 由适配器聚合或合成。

| 端点 | provider | 实现方式 | 能力位 | token 用量 | 计价落点 |
| --- | --- | --- | :-: | :-: | --- |
| `POST /v1/chat/completions`（`:639`） | 全部声明 chat 的家 | 转发 / gptgrok、qoder、zcode、trae 翻译 | `chat` | 有 | 按 `config/prices.yaml` token 计费 |
| `POST /v1/messages`（`:644`） | codebuddy、opencode、zcode、sub2api、codar、codar_sidecar、deepseek_sidecar（仅非流式）、qoder、qoder_sidecar、gptgrok | 转发 / gptgrok、qoder 合成 | `messages` | 有 | 同上 |
| `POST /v1/responses`（`:649`） | codar、codar_sidecar（`codar_sidecar.py:56-58`，sidecar 转发）、qoder、qoder_sidecar、muse（`muse.py:225`）、sub2api（`sub2api.py:181`）、gptgrok（`gptgrok.py:1323`） | 转发 / gptgrok、qoder 合成 | `responses` | 有 | 同上 |
| `GET /v1/models`（`:654`） | 全部声明 `models` 的家 | 本仓实现（`declared_models` / `fetch_models` 聚合，`:655-669`） | `models` | 无 | 不记账 |
| `POST /v1/embeddings`（`:672`） | sub2api（`sub2api.py:184` → `/v1/embeddings`） | 原样转发 | `embeddings` | 有（`prompt_tokens`） | 有 usage 无价目 → `unpriced:<model>`；空/全零 → `usage_missing` |
| `POST /v1/images/generations`（`:677`） | sub2api（`sub2api.py:187`）、muse（`muse.py:243`） | 原样转发（上游对非 OpenAI 平台分组回 404，`gateway.go:81-92`） | `images` | 无（按张） | `usage_missing` |
| `POST /v1/images/edits`（`:682`） | sub2api（`sub2api.py:190`）、muse（`muse.py:246`） | 原样转发（仅 JSON；`multipart/form-data` → 415 `unsupported_media_type`，`:281-287`） | `images` | 无（按张） | `usage_missing` |
| `GET /v1/images/tasks/{task_id}`（`:687`） | 仅 muse（`muse.py:249`）；sub2api 未实现 → 501 | 原样转发（`_proxy_get`，`:584-636`） | `images` | 无 | `usage_missing` |
| `POST /v1/videos`（`:692`） | sub2api（`sub2api.py:199`）、muse（`muse.py:252`，超时 660s） | 原样转发 | `videos` | 无（按秒） | `usage_missing` |
| `GET /v1/videos/{task_id}`（`:697`） | 仅 muse（`muse.py:255`）；sub2api 未实现 → 501（`:594-597`） | 原样转发 | `videos` | 无 | `usage_missing` |
| `GET /v1/media/{name}`（`:702`） | 仅 muse（`muse.py:258`） | 原样转发（按上游 `content-type` 回字节） | `media` | 无 | `usage_missing` |
| `POST /v1/messages/count_tokens`（`:707`） | sub2api（`sub2api.py:193`）、codar_sidecar（`codar_sidecar.py:60-62`）；`codar` 直连跳过 | 原样转发（上游对 OpenAI 平台分组回 404，`gateway.go:58-67`） | `count_tokens` | 无（顶层 `input_tokens` 只计数，非 `usage`） | `usage_missing` |
| `POST /v1/rerank`（`:712`） | sub2api（`sub2api.py:196`） | 原样转发 | `rerank` | 上游仅 `usage.total_tokens` | `usage_missing`（`totals` 只认 `prompt/completion`） |
| `GET /v1/usage`（`:717`） | sub2api（`sub2api.py:202`） | 原样转发（查询串透传，`:719`） | `usage` | 无（只有聚合数字） | `usage_missing` |

口径说明：`EXTENSION_CAPABILITIES`（`v1.py:47`）内的端点在 `_usage_for_ledger`（`v1.py:273-277`）把「usage 缺失或全零」一律折成 `None`；`billing.settle()` 随之走 `billing.py:322-323` 的 `note=usage_missing`，`routers/logs.py:157-158` 归类为 `usage_missing`。**任何路径都不会**把全零 usage 写成 `usage_zero` 再记成 `billed(0)`（该分支只服务既有 chat/responses/messages，见 `billing.py:318-320`）。有 token 但无价目时 `billing.py:315-317` 写 `unpriced:<name>`。失败或上游 ≥400 时 `billing.py:308-310` 提前返回不写流水，前端按 `logs.py:184` 归为 `not_billed`。images / videos 没有按张/按秒的 price schema，本表不虚构计价。

> 2026-11-05 已更正（原记录留档）：旧版本文件与 `PROVIDERS.md` 均未记录 sub2api 的 `rerank` / `videos` / `usage`，上一轮盘点把它们标为「上游不存在或证据未确认」而跳过。一手核对上游源码副本 `D:\qwq\项目\sub2api-codex-gnet-sub2api\backend\internal\server\routes\gateway.go` 后确认三条全部存在：`rerank` 在 `:80`、`videos` 建任务在 `:108`（包在 `h.SoraClient != nil` 内，是构造注入而非 feature flag）、`usage` 在 `:72`。鉴权不存在「另一套凭证」：`/v1/usage` 与其它 `/v1` 路由同挂 `gateway.go:41-47` 的组中间件（`apiKeyAuth` 在 `:46`），`routes/auth.go:36-55` 注册的是**无鉴权**的 `/api/v1/public/models|status`，与 usage 无关。另有两条上游门控需知，本仓只透传其结果：images 两条在非 OpenAI 平台分组直接回 404（`gateway.go:81-104`），count_tokens 在 OpenAI 平台分组回 404（`:58-67`）。误判后果：客户端会以为这三条不可用而直连上游，`/v1/usage` 的聚合用量也读不到；`/v1/videos` 被误当成「能力不存在」会让 Cherry Studio 类客户端直接报 501。本仓落地：`sub2api.py:28-29`、`:196`、`:199`、`:202`，路由 `v1.py:712-720`。

## 1.2 仍不支持 / 仍跳过（保留理由，勿当漏项回填）

- **`muse` 的 `/v1/messages` 仍为 501**：`apps/control/app/adapters/muse.py:62-67` 未声明 `messages` 位、也未覆写该方法，故 `_pick_capable` 在能力门回 501 `capability_unsupported`（`apps/control/app/routers/v1.py:394-401`），不是 400、不假装成功；与 `docs/TROUBLESHOOTING.md:101` 同口径。旧盘点表引「`plan:56`」说 muse 有 messages，该引用是错的，已按上述两处代码反证更正。
- **`codar` 直连的 `count_tokens` 仍跳过**：证据只覆盖 sidecar（`docs/providers/codar2api.md:57`、`providers/codar2api/README.md:14`），`codar.py:57` 的 `stub_capabilities` 也没有该位；需要时就近改走 `codar_sidecar`（`codar_sidecar.py:60-62`）。误把 `codar` 也标成支持会让请求打到不存在的上游路径并被 4xx 掩成 502。
- **sub2api 的 `images/tasks/{id}`、`videos/{id}` 读取未实现**：上游源码副本的 `/v1` 组（`gateway.go:41-110`）只注册了 `POST /v1/videos`（`:107-109`，包在 `h.SoraClient != nil` 内），没有 `GET /v1/videos/:id`，也没有 `GET /v1/images/tasks/:id`；`sub2api.py` 因此未覆写 `image_task` / `video_task`，`_proxy_get` 在 `v1.py:810-815` 明确回 501 `capability_unsupported`，不退化成 500。
- **`codebuddy` 的 `/v1/responses` 与 `count_tokens` 未接（缺口，待补；与上面三条「保留取舍」不同）**：上游（`addxiaoyi/codebuddy2api`，分析基线 `92b8b91`）的 README 写明同时提供 Responses 协议（`/v1/responses`）与 `/v1/messages/count_tokens`，接口表也列出 `POST /v1/responses`；本地 `apps/control/app/adapters/codebuddy.py:119-127` 的 `stub_capabilities` 没有 `responses` / `count_tokens`，也没有覆写 `responses()` / `count_tokens()`，`_pick_capable` 在能力门回 501（`apps/control/app/routers/v1.py:394-401`）。当前只接 chat/messages；`apps/control/tests/test_v1_extension_endpoints.py:336-343` 已锁定 codebuddy 这批扩展端点返回 501，补接时需同步改判并补转发用例。

## 2. 鉴权与上行入口

| provider | 鉴权头 | 上游对话入口 |
| --- | --- | --- |
| `codebuddy` | `Authorization: Bearer wbt_…`（管理端作用域 token） | 管理端 `/v1/chat/completions`、`/v1/messages`（转发到 workbuddy2api 7863） |
| `muse` | `Authorization: Bearer <api_key>`；可附 `cookie: hatch_sess=…` | OpenAI 兼容入口的 `chat_path`（默认 `/v1/chat/completions`） |
| `grok-register` | 无；控制面不持上游凭据 | 外部执行器 `GROK_REGISTER_EXECUTOR_URL`（env，非 provider 地址） |
| `gptgrok` | ChatGPT：`Authorization: Bearer <accessToken>` + `oai-device-id` / `oai-session-id` + OAI 指纹头；Grok：`Cookie: sso=…; sso-rw=…` | ChatGPT `POST /backend-api/conversation`；Grok `POST /rest/app-chat/conversations/new` |
| `zcode` | `authorization: Bearer <JWT>` 与 `x-api-key: <JWT>` 双写 + `anthropic-version` / `anthropic-beta` + 必需 `x-device-mid` | `POST /api/v1/zcode-plan/anthropic/v1/messages` |
| `opencode` | OpenAI：`Authorization: Bearer`；Anthropic：`x-api-key` + `anthropic-version: 2023-06-01` | 上游 `/v1/chat/completions`、`/v1/messages`（`tier=go` 加 `/go` 前缀） |
| `sub2api` | `Authorization: Bearer <账号 api_key>` | 账号自带 `upstream_base` 下的 `/v1/chat/completions`、`/v1/messages`、`/v1/responses`（及其余扩展端点） |
| `codar` | `Authorization: Bearer <Codar JWT>`（多 token 轮询）+ `user-agent: codar-desktop/…`；Anthropic 额外 `anthropic-version`（可配 `anthropic-beta`） | `/v1/chat/completions`、`/v1/messages`、`/v1/responses` |
| `codar_sidecar` | `Authorization: Bearer sk-codar-…`（sidecar 本地 key） | sidecar `/v1/chat/completions`、`/v1/messages`、`/v1/responses`、`/v1/messages/count_tokens`（均由 sidecar 原样转发） |
| `trae_sidecar` | 控制面不填（账号池与 OAuth 在 sidecar 内） | sidecar `/v1/chat/completions` |
| `deepseek_sidecar` | 控制面 api_key 可空（登录与刷新在 sidecar 内） | `/v1/chat/completions`；Anthropic **非标** `/anthropic/v1/messages` |
| `qoder_sidecar` | `Authorization: Bearer <sidecar key>` | sidecar `/v1/chat/completions`、`/v1/messages`、`/v1/responses` |
| `qoder` | `authorization: Bearer COSY.<payload>.<sig>` + `cosy-key` / `cosy-date` / `cosy-user` / `cosy-machineid` / `cosy-version` / `cosy-clientip` | `POST /algo/api/v2/service/pro/sse/agent_chat_generation?Encode=1`；workbuddy 走 `/v2/chat/completions` |
| `trae` | `authorization: Cloud-IDE-JWT <jwt>` + `x-cloudide-token` + `x-ide-token` + `x-uid` / `x-app-id`（Solo / Work 两套头） | Solo `POST /api/agent/v3/llm_utils_chat`；Work `create_agent_task` 或 `workflow/start` + 轮询 |

## 3. 流式与 SSE

「透传」= 控制面只搬字节，不改帧；「翻译」= 适配器逐帧改写成对外协议的帧。

| provider | chat 流式 | messages 流式 | responses 流式 | 上游帧形态 |
| --- | :-: | :-: | :-: | --- |
| `codebuddy` | ✓ 透传 | ✓ 透传 | ✗ 501 | OpenAI `data:` / Anthropic `event:`+`data:` |
| `muse` | ✓ 透传 | ✗ 501 | ✓ 透传 | OpenAI `data:` + `[DONE]`；responses 是上游 `response.*` 事件原样 |
| `grok-register` | — | — | — | 无 SSE（任务状态机） |
| `gptgrok` | ✓ 翻译 | ✓ 合成 | ✓ 合成 | ChatGPT：`data:` 帧，增量语义 `{"o":"append","v":…}`；Grok：逐行 JSON `result.response.token`（`isThinking` 区分思维链），`isSoftStop` / `finalMetadata` 收尾；对外 messages / responses 由适配器合成 |
| `zcode` | ✓ 翻译 | ✓ 透传 | ✗ 501 | Anthropic 事件：`message_start` → `content_block_start` → `content_block_delta` → `content_block_stop` → `message_delta` → `message_stop` |
| `opencode` | ✓ 透传 | ✓ 透传 | ✗ 501 | OpenAI `data:` / Anthropic `event:`+`data:` |
| `sub2api` | ✓ 透传（边转发边攒 usage） | ✓ 透传 | ✓ 透传 | 由账号上游决定，OpenAI / Anthropic / Responses 原样 |
| `codar` | ✓ 透传 | ✓ 透传 | ✓ 透传 | OpenAI `data:` / Anthropic `event:` / Responses `event:` |
| `codar_sidecar` | ✓ 透传 | ✓ 透传 | ✓ 透传（sidecar 转发） | OpenAI `data:` / Anthropic `event:` / Responses `event:`，均由 sidecar 原样 |
| `trae_sidecar` | ✓ 透传 | ✗ 501 | ✗ 501 | OpenAI `data:` + `[DONE]` |
| `deepseek_sidecar` | ✓ 透传 | **✗ 恒 501** | ✗ 501 | OpenAI `data:` + `[DONE]`；Anthropic 端点无流式实现 |
| `qoder_sidecar` | ✓ 透传 | ✓ 透传 | ✓ 透传 | OpenAI / Anthropic |
| `qoder` | ✓ 翻译 | ✓ 合成 | ✓ 合成 | 上游是**信封行** `{"body":"<内层 JSON 或 [DONE]>","statusCodeValue":200}`，内层才是 OpenAI chunk；对外 messages 合成 `message_start…message_stop`，responses 合成 `response.created` → `response.output_item.added` → `response.output_text.delta` → `response.output_text.done` → `response.completed` |
| `trae` | ✓ 翻译 | ✗ 501 | ✗ 501 | 上游内部事件名 `output` / `token_usage` / `done`（异常走 error 帧）；Solo 上行强制 `stream=true` |

> 2026-11-04 更正（原单元格值留档）：`qoder_sidecar` 的 responses 列原记「✗ 501（sidecar 有 `/v1/responses`，控制面未声明）」，与代码不符——`apps/control/app/adapters/qoder_sidecar.py:32` 已声明 `"responses": True`、`:55-57` 已实现转发，`apps/control/app/routers/v1.py:479-481` 已挂 `/v1/responses` 路由，带 `responses` 能力位的请求正常透传，故改为「✓ 透传」。（`codar_sidecar` 一行不变（`codar_sidecar.py:8` 明确不声明该能力位，仍 501）——2026-11-05 复核更正：该括注已失效，`codar_sidecar.py:7-9` 的 docstring 现写明 responses 经契约交叉验证后声明、`:30-39` 声明 `responses=True`、`:56-58` 转发，本行已改为「✓ 透传（sidecar 转发）」，详见 §1 的 2026-11-05 更正块。）

> 2026-11-05 更正（原单元格值逐字留档）：`muse` 行原记「`✓ 透传 | ✗ 501 | ✗ 501 | OpenAI \`data:\` + \`[DONE]\`」、`gptgrok` 行原记「`✓ 翻译 | ✗ 501 | ✗ 501`」、`sub2api` 行的 responses 列原记「✗ 501」、上游帧形态原为「由账号上游决定，OpenAI / Anthropic 原样」。
> - `muse`：`muse.py:225-241` 的 `responses()` 用 `self._send(RESPONSES_PATH, payload, stream=stream)`，流式分支直接 `return Reply(..., stream=reply.iter_bytes())`，是**原样透传**，不是 501；`messages` 仍 501（见 §1.2）。
> - `gptgrok`：`gptgrok.py:1323` 的 `responses()` 与 `:1353` 的 `messages()` 均由 `_render_responses` / `_render_anthropic` 把 ChatGPT 帧逐帧**合成**，不是 501。原表把这两列记成 ✗，会让 Anthropic / Responses 客户端误以为 gptgrok 只能走 chat 兼容层。
> - `sub2api`：`sub2api.py:181` 转发 `/v1/responses`（路径表 `:23`），`_forward(..., stream=stream)` 拨的是透传通道；原记 501 会让客户端放弃 responses 而降到 chat。
> 误判后果同上：三处都会让客户端以为端点不可用而绕行直连，"流式不支持"尤其致命——客户端拿到 501 后会退回非流式并可能直接报错。

## 4. 错误码风格

| provider | 错误载体 | 关键映射 |
| --- | --- | --- |
| `codebuddy` | HTTP 状态 + 管理端 body 原样 | 会话级管理员操作被拒是 **403**，不掩盖 |
| `muse` | HTTP 状态 + 厂商 body | 401/403/408/429/5xx 可重试并换 key；其余 4xx 直接返回（`retryable=False`） |
| `grok-register` | 任务状态 + `failure_reason` | 未配执行器 → 入队后 `failed`，不伪造成功 |
| `gptgrok` | 流内 `{"error":{code,message}}` | ChatGPT 流内错误 → 502；Grok `code=8` / rate limit → 429 |
| `zcode` | Anthropic `error` 帧 + 业务码 | 3012 → 403（system 指纹缺失，不可重试）；1005 / 3009 → 429（可换 key）；3001 → 400（x-device-mid 缺失）；401 → 401；429 → 429；404 → 404；5xx → 502 |
| `opencode` | HTTP 状态 | 400/413/422 立即返回；401/403/408/429/5xx 换 key 重试（0.5s→2s） |
| `sub2api` | HTTP 状态 + 配额语义 | 未建分组/无账号 → 503；账号池全冷却或额度耗尽 → 429；账号级 429/5xx → 冷却 60s 后换号 |
| `codar` | HTTP 状态 | 401 → `POST {pay_base}/v1/token/refresh` 续期后重试，全失败才 401 |
| `codar_sidecar` | sidecar 原样 HTTP | OpenAI / Anthropic / Responses 外壳照搬 |
| `trae_sidecar` / `deepseek_sidecar` / `qoder_sidecar` | sidecar 原样 HTTP | 控制面不改写，连不上 → 502/503 |
| `qoder` | 信封 `statusCodeValue` + 内层 `code` | HTTP 401 或 code `12153` / 文案 `SessionDead` / `AuthRejected` / `HardCredit` → **429** 并带 `session_dead` 标记（需重新登录）；HTTP 429 → 429（带 `Retry-After`）；404 → 404；5xx → 502；`trae` 产品线显式 501 |
| `trae` | 业务 `code` | `{1005,4008,4001,4000105}` → 429（Work 首帧即积分错误时自动降级 Solo）；`4011` / 429 → 429；401 → 401；404 → 404；5xx → 502 |

## 5. 许可证风险

| 级别 | provider | 依据 |
| --- | --- | --- |
| safe | `codebuddy`、`gptgrok`、`muse`、`trae_sidecar`、`codar_sidecar`、`opencode`、`sub2api`、`codar`、`qoder`、`trae`、`zcode`、`grok-register` | 上游 MIT，或本仓 clean-room 重写（MIT） |
| danger | `deepseek_sidecar` | 上游 Deepseek2Go **无 LICENSE**（All Rights Reserved），商用前必须移除 |
| danger | `qoder_sidecar` | 上游 **PolyForm Noncommercial 1.0.0**，禁止任何商用 |

风险级取自 `ProviderSpec.risk_level`；证据与完整清单见 `THIRD-PARTY-NOTICES.md`。

## 6. 已知坑（按踩坑概率排序）

1. **`deepseek_sidecar` 的 messages 流式恒 501**：`messages(stream=True)` 直接 `raise ProviderError(501)`，因为 sidecar 的 `/anthropic/v1/messages` 只实现非流式。别把它写成「支持流式」，客户端会拿不到任何帧。
2. **`deepseek_sidecar` 的 Anthropic 路径非标**：`/anthropic/v1/messages`，不是 `/v1/messages`，改成标准路径会 404。
3. **`muse` / `trae` / `trae_sidecar` 没有 messages**：调用 `/v1/messages` 是 501，不是 400；模型路由命中后由网关统一拒绝。（2026-11-05 更正，原句「3. **`muse` / `trae` / `trae_sidecar` / `gptgrok` 没有 messages**：调用 `/v1/messages` 是 501，不是 400；模型路由命中后由网关统一拒绝。」保留作历史：`gptgrok` 已不适用——`gptgrok.py:400` 声明 `messages=True`、`:1353` 用 `_render_anthropic` 把 ChatGPT 帧合成 Anthropic 事件，`/v1/messages` 不再 501。`muse` 仍 501 的依据是 `apps/control/app/adapters/muse.py:62-67` 未声明 `messages` 位、未覆写方法，能力门 `apps/control/app/routers/v1.py:394-401` 回 501；旧盘点表引「`plan:56`」说 muse 有 messages 是错的，该引用已按上述两处代码反证更正，勿按旧表回填。）
4. **`codar_sidecar` 的 responses 已声明（由 sidecar 转发，非合成）**：`apps/control/app/adapters/codar_sidecar.py:30-39` 声明 `responses=True`（同时声明扩展位 `count_tokens=True`），`:56-58` 的 `responses()` 直接 `forward("/v1/responses", payload, stream=stream)`、`:60-62` 的 `count_tokens()` 转 `/v1/messages/count_tokens`，控制面只搬字节；`protocols` 见 `:40`。要排查 501 应看 sidecar 进程是否在线、是否支持该端点，而不是控制面能力位。（2026-11-05 已更正，原句逐字留档：「4. **`codar_sidecar` 的 responses 未声明**：上游 sidecar 可能有该端点，但控制面声明为 False，调用 501；要开就得先补适配器与测试。」；2026-11-04 更正块末句「仍 501 的只剩 `codar_sidecar`」同步失效——误判后果：把真实 501 归因到控制面声明，会去改适配器做无用功；客户端则会绕开 sidecar 直连 codar2api，绕过控制面鉴权/配额/记账。依据当前工作树未提交改动，文档按工作区代码写，未入库。）
5. **`qoder` 的 `trae` 产品线 501**：COSY 网关不接 Trae 模型，改用 provider `trae`。
6. **`zcode` 缺 `official_system` 必回 3012**：上游做 system 内容指纹校验，适配器不内置官方原文。
7. **`gptgrok` 缺 sentinel token 可能 403**：ChatGPT 通道无法本地生成 sentinel，只能 `options.sentinel_token` 注入。
8. **`trae` 的 Work 通道只在首帧积分错误时降级 Solo**：流到一半出错不换通道，避免重复计费。
9. **`sub2api` 池空必 503**：没建分组/账号时不是 401，是 503。
10. **`codebuddy` 的 403 是权限而非鉴权失败**：`wbt_` token 对删除账号 / 重启 / 设置下发可能被拒，属会话级管理员限制。

## 7. 与既有文档的核对结论

逐条比对 `PROVIDERS.md`、`API-REFERENCE.md`、`docs/providers/*.md` 后，**未发现与代码冲突的硬性表述**，因此本表没有「纠正旧结论」的条目。旧文档只有三处给了结论、没给协议细节，本表补齐：

1. `deepseek_sidecar`：旧文只写「仅非流式」，本表把「恒 501」写进能力位与流式两列，避免被理解成「参数不支持时降级」。
2. `qoder`：旧文只写「信封 SSE」，本表给出信封字段与外层/内层关系，以及 messages / responses 是适配器合成而非透传。
3. `gptgrok`：旧文只写「OpenAI 协议」，本表区分 ChatGPT 的 `data:` 增量帧与 Grok 的逐行 JSON 两种上游形态。

（2026-11-05 更正，原句「逐条比对 `PROVIDERS.md`、`API-REFERENCE.md`、`docs/providers/*.md` 后，**未发现与代码冲突的硬性表述**，因此本表没有「纠正旧结论」的条目。」保留作历史：该结论已失效。本次核对至少发现三处硬冲突，均已在本表以「2026-11-05 已更正」块留档——§1 把 `muse`/`sub2api` 的 responses、`gptgrok` 的 messages/responses 记成不支持（实际 `muse.py:225`、`sub2api.py:181`、`gptgrok.py:1323/1353` 已实现）；§6 第 3 条把 `gptgrok` 列入「没有 messages」；§1 完全缺 `rerank`/`videos`/`usage`（上游 `gateway.go:80/108/72` 实际存在）。这次失效的根因是只核了仓库内 `providers/sub2api/` 与 `docs/providers/sub2api.md`，没读仓库外的上游源码副本——**后续核对 sub2api 时必须把源码副本当一手证据**。`docs/TROUBLESHOOTING.md:101` 的 `gptgrok` 条目同轮已同步更正（该文件 §8 的 2026-11-05 更正块）。）

若后续发现旧文档与本表冲突，以 `adapters/*.py` 为唯一事实来源，改文档不改代码语义。
