# Neosub2api（仓库目录仍为 unified-2api）

Neosub2api 把多家 2api / 注册上游收敛成**一个控制面 + 14 家 provider 适配器**，对外提供 OpenAI 与 Anthropic 兼容的模型网关。

- 控制面：FastAPI + SQLite，负责账号、服务商、网关密钥、配额、定价、用量、日志、安全与运行参数。
- 网关：`/v1/chat/completions`、`/v1/messages`、`/v1/responses`、`/v1/models`，流式与非流式都支持。
- 前端：Next.js 15 静态导出，由控制面直接托管，不需要单独跑 Node 服务。
- 许可证：主仓 MIT；可 clean-room 重写的上游只依据公开 README / 协议 / 实测 HTTP 行为实现，不复制源码；需要独立进程的上游保留 sidecar 边界。逐家许可证与商用限制见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) 与 [docs/compliance.md](docs/compliance.md)。

## 十分钟上手

### 0. 先看哪几份文档

1. 本文件：是什么、怎么起、有哪些能力、边界在哪。
2. [docs/README.md](docs/README.md)：全部文档的分组索引，按问题找文档。
3. [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md)：从环境变量到第一个 `/v1` 请求的完整步骤。
4. [docs/API-REFERENCE.md](docs/API-REFERENCE.md)：接口、鉴权、错误码与权限矩阵。

### 1. 演示模式：一键看到有数据的界面（推荐第一次用）

不配任何真实上游，直接在仓库根执行：

```powershell
# Windows
powershell -File scripts\demo.ps1
```

```bash
# Linux / macOS / Git Bash
bash scripts/demo.sh
```

脚本会建库、强制 `UNIFIED_DEMO_MODE=1` 且只绑 `127.0.0.1`，打印页面地址、健康探针、admin 口令与演示密钥位置。打开 <http://127.0.0.1:8010> 即可看到密钥、账号、日志、用量、配额与价格数据。演示数据带 `[demo]` 标记，可一键清除：

```powershell
powershell -File scripts\demo.ps1 -Clear
```

演示模式只用于本机评估，不能用于生产：上游是仓内假服务、演示密钥明文落盘、演示价格不是任何厂商报价。细节与防护边界见 [docs/DEMO.md](docs/DEMO.md)。

### 2. docker compose（正式部署的起点）

```bash
cp .env.example .env          # 至少设置 UNIFIED_ADMIN_PASSWORD
docker compose up -d --build
```

- 控制面 API：<http://127.0.0.1:8010>
- 前端页面（nginx 静态 + 反代）：<http://127.0.0.1:8080>

宿主机 8000 端口常被占用，因此 compose 把控制面映射到 8010；容器内部仍是 8000。管理员用户名默认 `admin`，可经 `UNIFIED_ADMIN_USER` 修改（仅首次播种生效），初始口令来自 `UNIFIED_ADMIN_PASSWORD`；未设置且库里还没有口令时，首次启动会随机生成并写入 `data/admin-password.txt`，启动日志会说明来源。历史固定口令 `unified-admin` 只用于识别并触发轮换，不会被当作实际口令。

### 3. 本地直接跑

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
# 开发模式：npm run dev；把 NEXT_PUBLIC_BACKEND_BASE_URL 指到控制面地址
```

构建完成后打开 <http://127.0.0.1:8010>，登录后进入仪表盘。要直接用客户端调通 `/v1/*`，照 [docs/examples.md](docs/examples.md) 的 curl / Python / Node 示例抄；网关密钥在「密钥」页创建，明文只显示一次。

### 4. 第一次调网关

```bash
curl http://127.0.0.1:8010/v1/chat/completions \
  -H "Authorization: Bearer u2a_..." \
  -H "Content-Type: application/json" \
  -d '{"model":"opencode/<model>","messages":[{"role":"user","content":"hi"}]}'
```

也可以把 `Authorization` 换成 `x-api-key`（Anthropic SDK 的默认头）。模型解析默认严格：未知模型返回 404，详见下方「关键约定」。

## 能力清单

| 能力 | 现状 | 入口 |
| --- | --- | --- |
| 多协议网关 | OpenAI `chat/completions`、`responses`，Anthropic `messages`，`/v1/models`；SSE 直通 | `/v1/*` |
| 14 家 provider | 当前注册表下 `opencode` / `qoder` / `trae` 为 `ready`，其余 `needs_config`；能力位与协议差异见 [docs/PROTOCOL-MATRIX.md](docs/PROTOCOL-MATRIX.md) | `/api/providers`、设置页「服务商」 |
| 多密钥与凭据中心 | 一个 provider 可配多把 key，按顺序轮换、401/403/429 冷却 | 设置页「服务商 / 凭据」 |
| 网关密钥 | `u2a_` 前缀，sha256 入库，明文仅创建时返回一次；支持启停、改名、吊销、归属 | 「密钥」页、`/api/keys` |
| 配额与告警 | 按密钥/分组/窗口的配额策略、当前周期用量、阈值告警历史 | 「配额」页、`/api/quota/*` |
| 定价与账单 | 价格簿、按 provider + model 解析价格、请求级账单流水与 usage 快照 | 「定价」页、`/api/pricing/*` |
| 模型目录 | 上游实拉 + 声明回退，返回 `provider/providers/source/provider_states` 等字段 | 「模型」页、`GET /api/models` |
| 用量与日志 | 请求日志、用量记录、日统计；失败请求按密钥滑动窗口限流 | 「统计」页、「日志」页 |
| 安全 | 角色隔离、审计日志、登录失败节流与统计、上游地址 SSRF 校验、安全响应头 | 「安全」页、「设置」页 |
| 运行参数 | 可在界面修改并落库，数据库值覆盖 env 默认值，支持导出/导入 | 设置页「运行参数」、`/api/settings/runtime-config` |
| 多语言 | `zh-CN` / `zh-TW` / `en` / `ja` / `ko`，缺键按设计回退 | 顶栏语言切换 |
| 演示模式 | 进程内假上游 + 带标记的演示数据，一条命令开关与清除 | `scripts/demo.ps1` / `scripts/demo.sh` |
| 注册任务（Grok） | 控制面内任务编排，执行器外置；未配置执行器时任务明确失败 | `/api/grok/register/*`（暂无界面） |

## 页面清单

导航顺序为仪表盘、账号、配额、定价、密钥、模型、测试台、统计、日志、安全、设置。

| 路由 | 作用 |
| --- | --- |
| `/login` | 登录；admin 与 viewer 共用，登录后按角色进入 |
| `/dashboard` | 运行状态、请求与用量概览 |
| `/accounts` | 上游账号列表、签到/测活/刷新/启停/删除/重启 |
| `/quota` | sub2api 分组、账号池、额度与流水 |
| `/quota/policies` | 配额策略、当前周期用量、告警历史（非管理员只显示权限说明） |
| `/pricing` | 价格簿、导入、按 provider + model 试算 |
| `/keys` | 网关密钥创建、启停、改名、吊销 |
| `/models` | 模型目录、来源与可用性 |
| `/playground` | 页面内直接调 `/v1`，验证密钥与上游链路 |
| `/stats` | 请求数、token、金额的汇总与趋势 |
| `/logs` | 请求日志与错误详情 |
| `/security` | 审计日志、登录失败统计、防护状态 |
| `/settings` | 服务商、凭据、运行参数、模型映射、令牌、用户 |

`/tasks` 页面已整体删除；后端从未提供任务、签到记录或上游日志端点，不要再按旧文档找它。

## 关键约定（新人最容易踩）

- **模型解析默认严格**：`<provider>/<model>` 或 `x-provider` 头最稳；未知模型 `404 model_not_found`，未启用 provider 里的模型 `404 model_unavailable`，同名多家 `400 ambiguous_model`。存量客户端可设 `UNIFIED_MODEL_FALLBACK_FIRST=1` 临时恢复旧兜底。
- **失败限流**：同一密钥在窗口内失败次数达到 `UNIFIED_FAILURE_WINDOW_MAX` 后返回 `429 failure_rate_limited`，带 `Retry-After`；被拒请求也会入库，所以看日志时会看到 429。
- **密钥归属**：`api_keys.owner_user_id` 存用户名。admin 可见全部；viewer 只可见自有密钥，配额读接口只能查自有 key；历史 `NULL` 归属只有 admin 可见。
- **审计与登录失败按角色收敛**：viewer 只看到与自己相关的审计记录和 `scope=self` 的登录失败聚合，显式查他人返回 403。
- **运行参数优先读库**：设置页保存后立即生效并重启保留；数据库值优先于 `.env` 默认值。排障时先看设置页标的 `source=db|default`。
- **匿名网关默认关闭**：`/v1` 不带密钥默认 401；需要放开时用设置页「网关鉴权」开关或 `UNIFIED_ALLOW_ANONYMOUS_GATEWAY`，数据库配置优先。

## 测试

```bash
cd apps/control
python -m pytest tests -q
```

测试基线以 2026-11-04 全量实测为准：**443 passed、1 skipped、0 failed、0 xfailed**（125.07 秒；`--collect-only` 收集 444 条、量级约 444。Windows 下唯一的 skip 是 `test_hardening.py` 的「Windows 不支持 POSIX 权限位」环境性跳过，按设计跳过）。这个数字会随用例增加而变化，只作为当次快照，不是长期承诺。耗时前提：`apps/control/tests` 的 36 个 `httpx` 调用点已全部带 `trust_env=False`，本机存在 `HTTP_PROXY` 时耗时与无代理一致（同批 16 条 28.95s vs 28.92s；修复前为 387s vs 55.90s）。

前端与契约检查：

```bash
cd apps/web && npx tsc --noEmit && npm run build:export
python scripts/contract_check.py          # 前后端契约检查，error/warn/提示三分类，见 docs/CONTRACT.md
python scripts/check_i18n.py              # 五语言键集
```

## 目录结构

```text
unified-2api/
├─ apps/
│  ├─ web/                 # Next.js 15 前端（静态导出，由控制面托管）
│  └─ control/             # FastAPI 控制面
│     ├─ app/adapters/     # 14 家 provider 适配器 + key_pool.py
│     ├─ app/routers/      # auth / providers / accounts / keys / stats / settings /
│     │                    # pricing / quota / logs / security / v1 / sub2api / grok
│     ├─ app/services/     # Grok 注册任务编排
│     ├─ app/db.py         # SQLite 表结构与连接（WAL）
│     ├─ app/demo.py       # 演示模式：进程内假上游 + 演示数据
│     └─ tests/            # 控制面测试
├─ docs/                   # 文档，入口见 docs/README.md
├─ docs/providers/         # 逐家上游的 clean-room 协议契约
├─ providers/              # 上游参考来源说明，不是运行依赖
├─ upstreams/              # 上游清单与巡检脚本
├─ scripts/                # demo / backup / restore / 契约与 i18n 检查 / 压测
├─ data/                   # SQLite 数据（运行时生成，已 gitignore）
├─ docker-compose.yml
└─ .env.example
```

## 边界与已知限制

- **真实上游未全覆盖验证**：opencode、sub2api、gptgrok、muse、zcode 等多家链路来自公开协议与假上游实测，真实鉴权细节仍需逐家用真实凭证确认；CodeBuddy 链路需启动管理端（7864）并配置 `wbt_` token。
- **sidecar 许可证**：`deepseek_sidecar` 无 LICENSE，`qoder_sidecar` 为 PolyForm-NC，商用前必须移除或取得书面授权；`codar_sidecar` / `trae_sidecar` 在仓内直连稳定后可只保留一种接入。
- **Grok 注册执行器不随仓交付**：未配置 `GROK_REGISTER_EXECUTOR_URL` 时任务明确失败，不会假装成功。
- **没有在线更新**：本部署不提供在线升级，也不操作宿主机 docker；升级需自行拉取代码与镜像。
- **管理面错误文案仍有中文硬编码**：网关已带稳定错误码，管理面部分路径（令牌、设置、内网地址校验）仍直接返回中文，非中文用户会看到中文；见 [docs/unification-audit.md](docs/unification-audit.md) P2-10。
- **viewer 仍可读全站请求日志**：`GET /api/logs` 只校验登录、不按 owner 过滤，不含密钥明文但能看到他人请求元信息；见 [docs/security-retest-2026-11-03.md](docs/security-retest-2026-11-03.md)。
- **SSRF 残留窗口**：`base_url` 保存时校验一次，请求时不复查；需要 admin 权限且能控制 DNS 才能利用，见安全复测报告。
- **演示模式不是生产能力**：假上游、明文演示密钥、演示价格、演示日志都会与真实库混存（带标记），生产禁用。

## 文档索引

完整索引在 [docs/README.md](docs/README.md)。常用直达：

- 上手：[GETTING-STARTED](docs/GETTING-STARTED.md)、[examples](docs/examples.md)、[DEMO](docs/DEMO.md)、[TROUBLESHOOTING](docs/TROUBLESHOOTING.md)
- 契约：[API-REFERENCE](docs/API-REFERENCE.md)、[CONTRACT](docs/CONTRACT.md)、[error-contract](docs/error-contract.md)、[PROTOCOL-MATRIX](docs/PROTOCOL-MATRIX.md)
- 运维：[DEPLOYMENT](docs/DEPLOYMENT.md)、[DEPLOYMENT-PROD](docs/DEPLOYMENT-PROD.md)、[PERFORMANCE](docs/PERFORMANCE.md)、[UPSTREAM-SYNC](docs/UPSTREAM-SYNC.md)
- 安全合规：[compliance](docs/compliance.md)、[security-audit](docs/security-audit.md)、[security-retest-2026-11-03](docs/security-retest-2026-11-03.md)、[THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES.md)
- 设计与历史：[unification-audit](docs/unification-audit.md)、[m3-billing-plan](docs/m3-billing-plan.md)、[FRONTEND-BASELINE](docs/FRONTEND-BASELINE.md)

行为变化按日期记录在 [CHANGELOG.md](CHANGELOG.md)。
