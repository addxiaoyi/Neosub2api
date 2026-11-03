# unified-2api

把 11 家 2api/注册上游收敛成**一个控制面 + 14 家 provider 适配器**（部分上游同时提供「仓内直连」和「sidecar 直通」两种接入，所以 provider 数比上游数多 3）。

主仓对可 clean-room 重写的上游只依据公开 README / 协议说明 / 实测 HTTP 行为实现，不复制上游源码；需要独立进程的保留 sidecar 边界：CodeBuddy 管理端（7864）加 codar / trae / deepseek / qoder 四个 sidecar。`providers/` 只放上游参考来源说明，不是运行依赖。
控制面负责账号、服务商、密钥、用量、配额分发与注册任务；推理协议由各 provider 适配器直接对接上游（或用户自建的上游服务），主仓保持 MIT 可分发、可商用。

当前状态：14 家 provider（3 家 `ready` / 11 家 `needs_config`，逐家见 [docs/PROVIDERS.md](docs/PROVIDERS.md)）；前端导航为仪表盘、账号、配额、定价、密钥、模型、测试台、统计、日志、安全、设置（服务商配置已并入设置页）。红包/抽奖链路已全部删除，后端与前端都不再保留；`/tasks` 任务页（含签到记录、任务日志、上游日志、任务执行、认领计划）也已整体删除，后端从无对应端点。

## 目录结构

```
unified-2api/
├─ apps/
│  ├─ web/                 # Next.js 15 前端（基于 codebuddy2api 的 web/ 迁移）
│  │  ├─ app/(main)/       # dashboard / accounts / quota / pricing / keys / models /
│  │  │                    # playground / stats / logs / security / settings
│  │  ├─ components/       # 沿用原项目的设计语言与组件
│  │  └─ lib/              # api.ts、providers.ts、use-providers.ts、i18n…
│  └─ control/             # FastAPI 控制面
│     ├─ app/
│     │  ├─ adapters/      # 14 家 provider 适配器 + key_pool.py（多密钥轮换/冷却）
│     │  ├─ services/      # 控制面内一等服务（grok 注册任务编排）
│     │  ├─ routers/       # auth / providers / accounts / keys / stats / settings / pricing /
│     │  │                 # quota / logs / security / v1 / sub2api / grok
│     │  ├─ registry.py    # provider 注册表（SQLite 持久化）
│     │  ├─ db.py          # 表结构 + 连接（sqlite3，WAL）
│     │  └─ main.py        # 装配 + 静态页面托管 + 任务服务生命周期
│     ├─ tests/            # 控制面测试（当前 250 passed, 1 skipped, 3 subtests passed）
│     └─ scripts/          # 假上游、验证脚本
├─ docs/providers/         # clean-room 协议契约与逐家实现说明
├─ providers/              # 上游参考来源（不是依赖）：只放说明与可选对照编排
│  ├─ opencode2api/        # GPL-3.0 上游参考（主仓已内置实现）
│  ├─ sub2api/             # LGPL-3.0 上游参考（主仓已内置实现）
│  ├─ grok-register/       # MIT 上游参考（主仓已内置任务编排）
│  ├─ trae2api/            # MIT sidecar（另有仓内直连 trae 适配器）
│  ├─ codar2api/           # MIT sidecar（另有仓内直连 codar 适配器）
│  ├─ deepseek2go/         # ⚠️ 无 LICENSE sidecar（仅评估，商用前移除）
│  └─ qoder-switch/        # ⚠️ PolyForm-NC sidecar（禁止商用）
├─ upstreams/              # 上游清单（11 家）+ 巡检脚本
├─ THIRD-PARTY-NOTICES.md  # 完整第三方许可证清单与合规 checklist
├─ data/                   # SQLite 数据（运行时生成，已 gitignore）
├─ docker-compose.yml
└─ .env.example
```

## 测试

```bash
cd apps/control
python -m pytest tests -q
```

当前基线：**250 passed, 1 skipped, 3 subtests passed**。skip 的那条是 POSIX 权限位用例（`test_hardening.py`），在 Windows 上按设计跳过。

## 启动方式

### 方式一：docker compose（推荐）

```bash
cp .env.example .env          # 至少改 UNIFIED_ADMIN_PASSWORD
docker compose up -d --build
```

- 控制面 API：<http://127.0.0.1:8010>
- 前端页面（nginx 静态 + 反代）：<http://127.0.0.1:8080>
- 管理员账号固定为 `admin`；初始口令来自 `UNIFIED_ADMIN_PASSWORD`。未设置且库里还没有口令时，首次启动会随机生成并写入 `data/admin-password.txt`，启动日志会说明来源。历史固定口令 `unified-admin` 只用于识别并触发轮换，绝不会作为实际口令回退。

宿主机 8000 端口经常被占用（本机就被别的进程占着），因此 compose 把控制面映射到
**8010**；容器内部仍是 8000，`apps/web/nginx.conf` 的 upstream 不受影响。

### 方式二：本地直接跑

控制面（Python 3.11+）：

```bash
cd apps/control
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

前端（Node 20+）：

```bash
cd apps/web
npm install
npm run build:export        # 产出 apps/web/out，由控制面直接托管
# 或开发模式：npm run dev（rewrites 把 /api、/v1 转到 http://127.0.0.1:8000/8010）
```

构建后控制面会把 `apps/web/out` 挂到 `/`，浏览器打开 <http://127.0.0.1:8010> 即可
（登录 → 仪表盘 / 账号 / 服务商 / 设置）。要直接拿客户端调通 `/v1/*`，照
[docs/examples.md](docs/examples.md) 的三个协议示例抄（curl / Python / Node，流式与非流式齐全）。

> 本机实测注意：这台机器的系统 Python 3.10 标准库已损坏（缺 `encodings`），
> 因此验证时用的是仓库内 `.runtime/python`（3.12 embed 包，已 gitignore）。
> 换机器请用正常安装的 Python，不需要这个目录。

## F1 已完成（clean-room 重写）

| 项 | 状态 |
| --- | --- |
| 契约文档：`docs/providers/opencode2api.md`、`sub2api.md`、`grok-register.md` | ✅ |
| `opencode` 适配器：直连 Zen/Go，`/v1/models`、`/v1/chat/completions`、`/v1/messages`，SSE 直通 | ✅ |
| OpenCode 重试：仅 401/403/408/429/5xx 换 key，400/413/422 立即返回；`tier` 支持 zen / go | ✅ |
| `sub2api` 配额分发：分组、账号池、token 扣减、账号冷却、额度耗尽 429，全量落库 | ✅ |
| `sub2api` 管理 API：`/api/sub2api/groups`、`/accounts`、`/ledger`、`/accounts/{id}/reset` | ✅ |
| `grok-register`：保留在 provider 列表但只声明 `register` 能力位（不参与 `/v1` 推理路由），任务编排升为一等服务 + `grok_tasks` 表 + API | ✅ |
| Grok 状态机：`queued → running → succeeded/failed/cancelled`，并发上限、指数退避重试、取消/重试 | ✅ |
| `providers/opencode2api`、`sub2api`、`grok-register` 说明改为「上游参考来源」；需要独立进程的上游改走显式 sidecar（codebuddy / codar / trae / deepseek / qoder），不再是隐式依赖 | ✅ |
| 单测：配额扣减、429、账号冷却轮转、任务成功/重试/取消/并发上限（6 项全过） | ✅ |

新增配置项（`.env.example` 已同步）：

- `OPENCODE_UPSTREAM_BASE`（默认 `https://opencode.ai/zen`）
- `GROK_REGISTER_EXECUTOR_URL`（外部注册运行时；留空则任务明确失败）
- `GROK_REGISTER_MAX_CONCURRENCY` / `GROK_REGISTER_MAX_WORKERS`

> 三家实现的共同约束：只依据公开 README / 协议说明 / 实测 HTTP 行为提炼契约；
> 函数体、变量命名、注释均为新写，未复制上游源码。

## M2 已完成

| 项 | 状态 |
| --- | --- |
| CodeBuddy 适配器改指管理端（默认 7864）：账号同步、签到/测活/刷新/清冷却/启停/删除/重启、checkin-all、refresh-credits | ✅ |
| 设置下发：upstream_config 保存后真实 POST 到管理端 `/api/settings/upstream`，结果回显在响应 `push` 字段 | ✅ |
| muse / gptGrok / ZCode 适配器：`/v1/chat/completions` + `/v1/models` 真实转发，SSE 逐 chunk 直通 | ✅ |
| 风控开关（存 `settings.provider_config`，保存即生效）：zcode `system_prompt_override`、gptgrok `fingerprint`/`proxy`/`extra_headers`、muse `timeout` | ✅ |
| 网关密钥：`u2a_` 前缀，sha256 入库，明文仅创建时返回一次；创建/改名/启停/吊销（`/api/keys`） | ✅ |
| `/v1` 默认拒绝匿名（`UNIFIED_ALLOW_ANONYMOUS_GATEWAY` 默认 False）；`/api/settings/gateway` 可下发覆盖 | ✅ |
| 计费旁路：`usage_records` 表原样存上游 usage JSON（流式从含 usage 的 SSE chunk 提取），`request_logs` 带 key 归因 | ✅ |
| 密钥页重写（创建/吊销/启停/改名 + 一次性明文弹窗）；导航挂回「密钥」 | ✅ |
| 服务商页：每行高级配置（api_key + 各家风控开关）、底部网关鉴权开关 | ✅ |
| 删除孤儿页面（red-packets / claim）：后端本就无端点，属于 M3 配额域 | ✅ |

## M3 / 近期已完成

| 项 | 状态 |
| --- | --- |
| 服务商扩到 14 家：新增 `codar`（仓内直连）与 `codar_sidecar`，`qoder`、`trae` 改为仓内直连，保留对应 sidecar | ✅ |
| 多密钥轮换：新增 `adapters/key_pool.py`，`zcode` / `gptgrok` 接入；401/403/429 冷却（默认 30s，`options.key_cooldown`），5xx 只重试不冷却 | ✅ |
| `GET /api/providers` 增强：`readiness` / `requires`、`base_url_source`、凭据列表与掩码；`base_url_warning` / `credential_base_warnings` 提示历史默认地址残留（当前只认 `deepseek_sidecar` 的 7872） | ✅ |
| 凭据中心：`/api/provider-credentials` 与设置页「服务商」tab，支持一个 provider 多把 key、逐条 base_url 与启停 | ✅ |
| 定价页：价格簿、导入、`/api/pricing/resolve` 按 provider+model 命中价；配额页：分组/额度/流水 | ✅ |
| 日志页、测试台、安全页重写；红包/抽奖链路从前端到类型定义全部移除，不再保留空壳页 | ✅ |
| 测试基线：`250 passed, 1 skipped, 3 subtests passed`（Windows；skip 为 POSIX 权限位用例） | ✅ |

### 2026-11-03 行为变化

以下改动对外有影响，升级前请先核对客户端用法，细节见 [docs/API-REFERENCE.md](docs/API-REFERENCE.md)。

- **未知模型名不再兜底**：以前 `/v1/*` 遇到本地目录里没有的模型名会兜底到第一家 chat provider，现在直接 **404 `model_not_found`**。客户端要么用 `<provider>/<model>` 写法，要么带 `x-provider` 头；模型只存在于未启用 provider 时返回 404 `model_unavailable`，同名多家命中返回 400 `ambiguous_model`（带 `candidates`）。存量客户端可临时设置 `UNIFIED_MODEL_FALLBACK_FIRST=1` 恢复旧兜底；该开关只解决「一家都没命中」，不掩盖歧义。详见 API-REFERENCE §1「模型解析顺序」与错误契约。
- **`/tasks` 页面整体删除**：签到记录、任务日志、上游日志、任务执行、认领计划对应的页面、导航入口、账号页跳转与全部 i18n 文案一并移除；这些端点后端从未实现。账号行内的「活动任务」按钮同步消失，不再保留死按钮。
- **`GET /api/models` 新契约**：条目新增 `provider` / `providers` / `source`（`upstream` | `declared` | `declared_fallback`）/ `provider_states` / `unavailable_reason` / `fallback_reason`；支持 `?provider=<id>` 只看一家（未知 id 404，且不再按 enabled 过滤）。模型页已改调该端点。
- **密钥归属**：`api_keys.owner_user_id` 存用户名（`users.username` 是 TEXT 主键）。admin 可查全部、可指定归属；非 admin 只列自有密钥，配额读接口只能查自有 key，查他人（含历史 NULL 归属）返回 403。
- **失败请求限流**：同一密钥在滑动窗口内失败次数达到上限，后续请求返回 **429 `failure_rate_limited`**，`detail` 含 `used/limit/window_sec/reset_at/reset_after`，并带 `Retry-After`。计数直接来自 `request_logs`（`status>=400` 或 `error` 非空），被拒请求本身也会落一条 429 日志。
- **新增配置项**：`UNIFIED_MODELS_CACHE_TTL_SEC`（默认 60，`0` 关闭模型目录缓存）、`UNIFIED_MODEL_FALLBACK_FIRST`（默认 `0`，未知模型 404；设 `1` 兜底到首家启用的、支持该端点能力位的 provider）、`UNIFIED_FAILURE_WINDOW_SEC`（默认 60）、`UNIFIED_FAILURE_WINDOW_MAX`（默认 20，后两项任一为 `0` 即关闭失败限流）。

### 本地假上游（无真实凭证时的链路验证）

```bash
cd apps/control
python -m uvicorn scripts.fake_upstream:app --port 7899
```

然后在「服务商」页把 muse/gptgrok/zcode 的 Base URL 指到 `http://127.0.0.1:7899`
（API Key 随便填）。假上游提供 `/v1/models`、OpenAI 与 Anthropic 两种 SSE、
末 chunk 带 usage，回声里带 `system='...'` 便于验证 zcode 的改写。

## M1 已完成

| 项 | 状态 |
| --- | --- |
| 仓库骨架（web / control / compose / env / README） | ✅ |
| 前端以 codebuddy2api `web/` 为基准迁移，保留设计语言 | ✅ |
| accounts 页：服务商筛选、realm 只对 CodeBuddy 生效、按钮/列按能力位门控 | ✅ |
| settings 页：新增「服务商接入」tab（默认选中），移除 M1 无关的 3 个 tab | ✅ |
| 控制面 provider 注册表（SQLite 持久化 + 启停 + base_url 可改） | ✅ |
| 统一 API 客户端层（`adapters/client.py`，httpx + 流式透传） | ✅ |
| CodeBuddy 适配器（chat / messages / models / health） | ✅ |
| 其余 6 家占位适配器（能力位齐、可注册可启用、调用返回 501/未接入） | ✅ |
| sidecar 目录（GPL/LGPL 许可证隔离，源码不进主仓） | ✅ |
| 网关 `/v1/chat/completions`、`/v1/messages`、`/v1/models` | ✅ |
| 账号、用量、设置、用户管理 API 的空壳实现 | ✅ |

## 已采用的默认决策 + 待确认

以下 8 项在用户逐条确认前按推荐值先做，后续可随时推翻（改动点都标在括号里）。

1. **存储：SQLite（M1）+ repository 抽象**
   控制面自带 sqlite3（WAL），表已在 `db.py` 定型。待确认：是否需要 Postgres 版本。
2. **部署形态：分层，但 M1 compose 只起 web + control**
   各上游仍按外部进程/容器处理（`CODEBUDDY_BASE` 等）。待确认：是否把 6 家上游也编进同一份 compose。
3. **前端改造方式：原生改造 codebuddy2api `web/`，不重写**
   只抽 provider 能力位（`lib/providers.ts`），页面按能力位显示/门控。待确认：是否保留 `WorkBuddy Manager` 品牌名。
4. **红包/抽奖页已删除（M2）**
   后端从无对应端点（属 M3 配额域），孤儿页面与类型/接口定义一并移除，避免假承诺。
5. **计费推迟到 M3**
   M1 只记 `request_logs` 的 token/次数，不做价格表与倍率。
6. **许可证隔离（详见 THIRD-PARTY-NOTICES.md）**
   - 已 clean-room 自研（MIT，主仓可直接分发）：opencode2api / sub2api / grok-register
     —— 仅依据公开协议契约重写，未复制上游源码，上游仓库只作为「上游参考来源」记录
   - safe（MIT/Apache/BSD）：trae2api、muse、gptGrok、ZCode、内部 CodeBuddy——代码可自由合入主仓
   - danger（无 LICENSE / PolyForm-NC）：Deepseek2Go、qoder-sidecar——仍是 sidecar，
     **商用前必须移除或取得书面授权**
   - 仍为 sidecar 的两家只通过进程间 HTTP 调用，不链接、不拷贝、不修改上游源码
7. **注册机（grok-register）控制面内任务服务**
   仅登记 `register` 能力位；编排（队列/并发/状态机/重试）在主仓内实现，
   浏览器执行器通过 `GROK_REGISTER_EXECUTOR_URL` 外置，未配置时任务明确失败而不假装成功。
8. **模型别名沿用 sub2api 式语义**
   M1 只有简单 alias map（`/api/settings/model-map`），倍率/分组留到 M3。

## 已知限制（F1 边界）

- CodeBuddy 链路（账号同步/签到/重启/设置下发）已对接真实管理端 API，但**本机未跑
  codebuddy2api**，只验证到「未配置时如实报错（502 + 原因），不假装成功」；
  真实账号行为需启动管理端（7864）并配置 `wbt_` token 后确认。
- muse/gptgrok/zcode 的转发已在**假上游**（`scripts/fake_upstream.py`）全链路验证；
  真实上游的鉴权细节（如 MUSE2API_KEY 头格式、Grok 指纹头字段）需真实凭证确认。
- OpenCode Zen/Go 直连**未用真实凭证验证**：契约来自公开 README / 协议说明，
  链路验证走假上游；真实 Zen 的模型清单与 `tier=go` 路径前缀需实际账号确认。
- sub2api 的配额分发在假上游验证了扣减/冷却/429；真实上游的 `upstream_base`
  与账号级鉴权需逐个账号配置后确认。
- grok-register 的浏览器执行器**未随仓交付**：未配置 `GROK_REGISTER_EXECUTOR_URL`
  时任务会明确失败（不假成功），执行器协议见 `docs/providers/grok-register.md`。
- 配额、模型白名单、价格表、倍率属 M3，`usage_records` 只存原始 usage 不重算。
- `ja` / `ko` / `zh-TW` 未补新增的 `providers.*` / `keys.*` 文案，按 zh-CN 回退。
- `/api/status` 在 CodeBuddy 上游未启动时返回 `connected:false`（属预期）。

## 下一步建议

1. 真实凭证回归：gptgrok（sentinel 403）、muse（官方入口与鉴权）、zcode（3012 之外的链路）目前只走过假上游，需要用真实账号确认。
2. sidecar 收敛：`deepseek_sidecar`（无 LICENSE）与 `qoder_sidecar`（PolyForm-NC）商用前必须移除或取得书面授权；`codar_sidecar` / `trae_sidecar` 在仓内直连稳定后可只保留一种接入。
3. 配额与价格策略按 [docs/m3-billing-plan.md](docs/m3-billing-plan.md) 继续推进；`usage_records` 目前只存原始 usage，不重算。
