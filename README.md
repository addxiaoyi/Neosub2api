# unified-2api

把 10 家 2api/注册上游的能力收敛成**一个控制面 + 多家 provider 适配器**。
自 F1 起采用 clean-room 重写：主仓只依据公开协议契约实现，不复制上游源码，
**不再依赖外部 sidecar 进程**；`providers/` 仅保留上游参考来源与可选对照编排。
控制面负责账号、服务商、密钥、用量、配额分发与注册任务；推理协议由各 provider
适配器直接对接上游（或由用户自建的上游服务提供），主仓保持 MIT 可分发、可商用。

当前进度：**F1（清掉三个占位桩）** —— opencode2api / sub2api / grok-register
三个 stub 已换成主仓内置实现：OpenCode 直连 Zen/Go 转发、sub2api 订阅配额分发、
Grok 注册任务编排。M2 的鉴权、密钥体系与 usage 旁路行为保持不变。

## 目录结构

```
unified-2api/
├─ apps/
│  ├─ web/                 # Next.js 15 前端（基于 codebuddy2api 的 web/ 迁移）
│  │  ├─ app/(main)/       # dashboard / accounts / providers / settings …
│  │  ├─ components/       # 沿用原项目的设计语言与组件
│  │  └─ lib/              # api.ts、providers.ts、use-providers.ts、i18n…
│  └─ control/             # FastAPI 控制面
│     ├─ app/
│     │  ├─ adapters/      # provider 适配器（opencode/sub2api 等为主仓自研实现）
│     │  ├─ services/      # 控制面内一等服务（grok 注册任务编排）
│     │  ├─ routers/       # auth / providers / accounts / keys / stats / settings / v1 / sub2api / grok
│     │  ├─ registry.py    # provider 注册表（SQLite 持久化）
│     │  ├─ db.py          # 表结构 + 连接（sqlite3，WAL）
│     │  └─ main.py        # 装配 + 静态页面托管 + 任务服务生命周期
│     ├─ tests/            # F1 关键单测（配额扣减、429、任务状态流转）
│     └─ scripts/          # 假上游、验证脚本
├─ docs/providers/         # clean-room 协议契约（opencode2api/sub2api/grok-register）
├─ providers/              # 上游参考来源（不是依赖）：只放说明与可选对照编排
│  ├─ opencode2api/        # GPL-3.0 上游参考（主仓已内置实现）
│  ├─ sub2api/             # LGPL-3.0 上游参考（主仓已内置实现）
│  ├─ grok-register/       # MIT 上游参考（主仓已内置任务编排）
│  ├─ trae2api/            # MIT sidecar（Trae Solo+Work 双通道，F2 待重写）
│  ├─ deepseek2go/         # ⚠️ 无 LICENSE sidecar（仅评估，商用前移除）
│  └─ qoder-switch/        # ⚠️ PolyForm-NC sidecar（禁止商用）
├─ THIRD-PARTY-NOTICES.md  # 完整第三方许可证清单与合规 checklist
├─ data/                   # SQLite 数据（运行时生成，已 gitignore）
├─ docker-compose.yml
└─ .env.example
```

## 启动方式

### 方式一：docker compose（推荐）

```bash
cp .env.example .env          # 至少改 UNIFIED_ADMIN_PASSWORD
docker compose up -d --build
```

- 控制面 API：<http://127.0.0.1:8010>
- 前端页面（nginx 静态 + 反代）：<http://127.0.0.1:8080>
- 默认账号：`admin` / `unified-admin`（**生产环境必须改**）

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
（登录 → 仪表盘 / 账号 / 服务商 / 设置）。

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
| `grok-register`：**不作为对外 provider**，改为一等任务服务 + `grok_tasks` 表 + API | ✅ |
| Grok 状态机：`queued → running → succeeded/failed/cancelled`，并发上限、指数退避重试、取消/重试 | ✅ |
| `providers/opencode2api`、`sub2api`、`grok-register` 说明改为「上游参考来源」，主仓不再依赖外部进程 | ✅ |
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

## 下一步建议（F2 入口）

1. `trae2api`：Solo + Work 双通道，Work 积分通道降级链（余量不足时切 Solo）。
2. `Deepseek2Go`：PoW 挑战求解，需要逆协议 + WASM 调用，风险最高，先做可行性验证。
3. `qoder`：多产品线 × 多地域路由与重试链，契约清晰度居中。
4. 三家均按 F1 同一流程：先写 `docs/providers/*.md` 契约，再实现、再用假上游验证。
