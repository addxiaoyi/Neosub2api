# 从零启动（Getting Started）

本文覆盖：环境要求 → 配置 → 构建前端 → 启动控制面 → 首次登录 → 创建第一个网关密钥 → 跑通一次 `/v1/chat/completions`。

部署形态差异见 [DEPLOYMENT.md](DEPLOYMENT.md)，接口细节见 [API-REFERENCE.md](API-REFERENCE.md)，服务商逐家说明见 [PROVIDERS.md](PROVIDERS.md)。

## 1. 环境要求

| 组件 | 版本要求 | 用途 |
| --- | --- | --- |
| Python | 3.11+ | 控制面（FastAPI + uvicorn，依赖仅 fastapi / uvicorn / httpx） |
| Node.js | 20+（容器镜像用 22） | 前端构建（Next.js 15，构建一次即可，运行期不需要 Node） |
| Docker | 可选 | 用 docker compose 一键起两个容器 |

控制面是纯 Python（SQLite 内置），不需要额外数据库。

## 2. 配置 `.env`

```bash
cp .env.example .env
```

`.env` 里的每个变量（解析逻辑在 `apps/control/app/config.py`，启动时从进程环境读一次）。仓库里没有 dotenv 加载：`.env` 只被 `scripts/start.*`、`scripts/demo.*` 与 docker compose 消费，第 4 节直接跑 `python -m uvicorn` 时它不生效——想用 `.env` 就改用 `scripts\start.ps1` / `bash scripts/start.sh`：

| 变量 | 默认值 | 说明 | 什么时候必须改 |
| --- | --- | --- | --- |
| `UNIFIED_DATA_DIR` | `./data` | 数据目录（SQLite 落这里） | 一般不用改；compose 内固定为 `/data` |
| `UNIFIED_DB_PATH` | `./data/unified.db` | SQLite 文件路径 | 想把库放别处时改 |
| `UNIFIED_ADMIN_USER` | `admin` | 首个管理员用户名 | **只在 users 表为空时生效**（首次启动播种），之后改它没影响 |
| `UNIFIED_ADMIN_PASSWORD` | 空 | 首个管理员密码 | 不设时首次启动随机生成并写入 `data/admin-password.txt`；历史固定值 `unified-admin` 会被识别并轮换 |
| `UNIFIED_SESSION_TTL_SEC` | `86400` | 登录会话有效期（秒） | 按需 |
| `UNIFIED_COOKIE_SECURE` | `false` | 会话 Cookie 是否仅 HTTPS | 部署在 HTTPS 反代后面时改 `true` |
| `UNIFIED_CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | 允许跨域的来源 | 前端和控制面不同源部署时改 |
| `UNIFIED_WEB_DIR` | `./apps/web/out` | 控制面托管的静态前端目录 | 前端产物不在默认位置时改；目录不存在时 `/` 返回提示 JSON |
| `UNIFIED_ALLOW_ANONYMOUS_GATEWAY` | `false` | `/v1` 是否允许匿名调用 | 保持 `false`；管理页可覆盖此值（DB 配置优先于环境变量） |
| `CODEBUDDY_BASE` | `http://127.0.0.1:7864` | CodeBuddy 管理端地址 | docker compose 内必须用 `http://host.docker.internal:7864`（compose 已内置该默认值） |
| `CODEBUDDY_API_KEY` | 空 | CodeBuddy 管理端 `wbt_` token 的兜底值 | 也可在管理页「服务商」里按 provider 配置，页面配置优先 |
| `OPENCODE_UPSTREAM_BASE` | `https://opencode.ai/zen` | OpenCode 直连上游 | 用自建网关时改 |
| `GROK_REGISTER_EXECUTOR_URL` | 空 | Grok 注册浏览器执行器 HTTP 入口 | 留空则注册任务以明确原因失败（不假装成功） |
| `GROK_REGISTER_MAX_CONCURRENCY` | `2` | 注册任务全局并发上限 | 按执行器能力调整 |
| `GROK_REGISTER_MAX_WORKERS` | `4` | 单任务 workers 上限 | 按执行器能力调整 |
| `UNIFIED_MODELS_CACHE_TTL_SEC` | `60` | 上游实拉模型目录的进程内缓存时长（秒） | 想让模型页每次都实拉时设 `0`（关闭缓存）；缓存键含 provider、`base_url`、凭据指纹与 `models_path`/`tier` |
| `UNIFIED_MODEL_FALLBACK_FIRST` | `0` | 未知模型名是否兜底到首家启用的、支持该端点能力位的 provider | 默认 `0`：404 `model_not_found`（严格模式）；设 `1` 恢复旧行为，只影响「一家都没命中」，多家命中仍是 400 `ambiguous_model` |
| `UNIFIED_FAILURE_WINDOW_SEC` | `60` | 失败请求滑动窗口长度（秒） | 与下面一项同时 `> 0` 才生效；任一项为 `0` 即关闭失败限流 |
| `UNIFIED_FAILURE_WINDOW_MAX` | `20` | 窗口内失败请求上限（按密钥计数） | 同上；调高可减少误伤，调低更早拦截刷量 |
| `UNIFIED_LOGIN_MAX_FAILURES` | `5` | `/api/login` 窗口内失败上限（按来源 IP + 用户名计数） | 调大可减少共享出口 IP 的误伤，调小更早拦截撞库 |
| `UNIFIED_LOGIN_WINDOW_SEC` | `900` | 登录失败滑动窗口长度（秒） | 与上一项配合；超过上限后的冷却从最后一次失败起算 |
| `UNIFIED_LOGIN_COOLDOWN_SEC` | `60` | 首次冷却基数（秒） | 超限后按失败次数翻倍退避，这是第一档时长 |
| `UNIFIED_LOGIN_COOLDOWN_MAX_SEC` | `3600` | 冷却上限（秒） | 退避不会超过这个值；管理员账号始终豁免 |
| `UNIFIED_HOST` | `127.0.0.1` | 控制面监听地址 | 本机保持回环；容器内由 Dockerfile/compose 固定为 `0.0.0.0` |
| `UNIFIED_PORT` | `8010` | 控制面监听端口 | 宿主机约定 8010；容器内固定 `8000` |
| `UNIFIED_ALLOW_PRIVATE_UPSTREAMS` | `false` | 是否放行上游指向环回/私网/链路本地 | 内网 sidecar 场景置 `1`；云元数据地址始终拒绝 |
| `UNIFIED_SSE_MAX_SECONDS` | `600` | 单请求流式响应最大时长（秒），`0` 不限 | 上游长流被截断时调大 |
| `UNIFIED_GATEWAY_MAX_BODY_BYTES` | `8388608` | `/v1` JSON 入口单请求体字节上限（下限 1024） | 超限 413 `request_too_large` 且不读进内存；长上下文或大图请求可调大 |
| `UNIFIED_ENABLE_DOCS` | `false` | 是否开放 `/docs`、`/openapi.json`、`/redoc` | 生产保持 `false` |
| `UNIFIED_LOG_LEVEL` | `info` | uvicorn 启动日志级别 | 仅 `python -m app.main` 入口读取，`uvicorn` CLI 启动时忽略 |
| `UNIFIED_LOG_FORMAT` | `text` | 访问日志格式 | 采集系统要 JSON 时设 `json` |
| `UNIFIED_UPSTREAM_MAX_CONNECTIONS` | `100` | 每个上游 client 的连接上限 | 高并发时调大 |
| `UNIFIED_UPSTREAM_MAX_KEEPALIVE` | `50` | 每个上游 client 的 keepalive 连接数 | 按需 |
| `TRAE_SIDECAR_BASE` | `http://127.0.0.1:7871` | Trae sidecar 地址 | sidecar 未启动时该 provider 显示不可达 |
| `DEEPSEEK_SIDECAR_BASE` | `http://127.0.0.1:5001` | Deepseek2Go sidecar 地址（上游默认端口 5001） | 自行改 sidecar 端口时同步 |
| `QODER_SIDECAR_BASE` | `http://127.0.0.1:7873` | qoder sidecar 地址 | 同上 |
| `CODAR_SIDECAR_BASE` | `http://127.0.0.1:8801` | codar2api sidecar 地址（上游不在主仓，需自行 clone 启动） | 同上 |
| `UNIFIED_DEMO_MODE` | `false` | 演示模式（假上游 + 演示数据） | 只用 `scripts/demo.*` 启动；三份 compose 强制 `0`，见 [DEMO.md](DEMO.md) |

根 `docker-compose.yml` 与 `deploy/` 编排都会透传 `UNIFIED_LOG_LEVEL`、`UNIFIED_LOG_FORMAT`、`UNIFIED_ENABLE_DOCS`、`UNIFIED_UPSTREAM_MAX_CONNECTIONS`、`UNIFIED_UPSTREAM_MAX_KEEPALIVE` 这 5 个键（根 compose 走 `${VAR:-默认值}` 插值读根 `.env`，deploy/ 走 `env_file` 读 `deploy/.env`），改完重启 control 生效。`UNIFIED_BIND_HOST` 只被根 compose 用于端口映射（默认 `127.0.0.1`），不是控制面进程变量。

前端还有两个构建期变量（放 `apps/web/.env`，模板见 `apps/web/.env.example`——next 只读自己目录的 `.env`，根 `.env` 里的 `NEXT_PUBLIC_*` 不生效；不设置也能跑，见 `apps/web/next.config.ts`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NEXT_PUBLIC_BACKEND_BASE_URL` | `http://127.0.0.1:8000` | **仅 `npm run dev` 时用**：dev 服务器把 `/api`、`/v1` 反代到这个地址。本地控制面跑在 8010 就设成 `http://127.0.0.1:8010` |
| `NEXT_PUBLIC_BASE_PATH` | 空 | 子路径部署时设（如 `/workbuddy-manager`），构建期注入 |

注意：服务商的 api_key、风控开关等保存在 SQLite（`settings` 表），在管理页保存后立即生效，不需要重启；环境变量只是初始默认值。

## 3. 构建前端

```bash
cd apps/web
npm install
npm run build:export
```

- `build:export` = `NEXT_OUTPUT_EXPORT=1 next build`，产出静态目录 `apps/web/out`（`output: 'export'` + `trailingSlash: true`）。
- 本地预览方式一（推荐）：直接由控制面托管——构建完成后控制面把 `out/` 挂在 `/`，打开控制面地址即可，无需单独起前端服务。
- 本地预览方式二（开发模式）：`npm run dev`，此时 `next.config.ts` 的 rewrites 把 `/api`、`/v1`、`/v2`、`/healthz` 转发到 `NEXT_PUBLIC_BACKEND_BASE_URL`（默认 `http://127.0.0.1:8000`）。

## 4. 启动控制面

```bash
cd apps/control
python -m venv .venv
. .venv/Scripts/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

- 宿主机约定用 **8010**（8000 端口经常被占，见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)）。
- 容器内监听 8000，compose 映射 `8010:8000`。
- 启动即完成：建库建表（`data/unified.db`，WAL 模式）+ 播种管理员账号 + 注册 14 家 provider（默认仅 codebuddy 启用）。
- 健康检查：`curl http://127.0.0.1:8010/healthz`（无需鉴权）。

浏览器打开 `http://127.0.0.1:8010` 即是管理界面（仪表盘 / 账号 / 配额 / 定价 / 密钥 / 模型 / 测试台 / 统计 / 日志 / 安全 / 设置）。

## 5. 首次登录

- 账号：`UNIFIED_ADMIN_USER`（默认 `admin`）；口令取 `UNIFIED_ADMIN_PASSWORD`，未设置且库里没有口令时首次启动随机生成并写入 `data/admin-password.txt`（历史固定值 `unified-admin` 会被识别并轮换，不再作为默认口令）。
- 登录后会种下 HttpOnly 会话 Cookie（`u2a_session`），有效期 `UNIFIED_SESSION_TTL_SEC` 秒。
- 密码用 PBKDF2-SHA256（20 万轮）哈希存储；改密码会吊销该用户全部会话。

curl 登录（保存 Cookie 供后续管理接口使用，口令换成启动日志或 `data/admin-password.txt` 里的值）：

```bash
curl -c cookies.txt -X POST http://127.0.0.1:8010/api/login \
  -H "content-type: application/json" \
  -d '{"username":"admin","password":"<初始口令>"}'
# {"ok":true,"username":"admin","role":"admin"}
```

## 6. 创建第一个网关密钥

`/v1` 网关用 `u2a_` 前缀密钥鉴权（默认拒绝匿名）。密钥明文只在创建时返回一次，库里只存 SHA-256。

方式一：管理页「密钥」→ 创建，弹窗里复制明文。

方式二：curl（需要登录态）：

```bash
curl -b cookies.txt -X POST http://127.0.0.1:8010/api/keys \
  -H "content-type: application/json" \
  -d '{"name":"first-key"}'
```

响应（`key` 字段只出现这一次，丢了只能吊销重建）：

```json
{
  "id": 1,
  "name": "first-key",
  "prefix": "u2a_6Tszhi2P",
  "enabled": true,
  "created_at": 1762000000,
  "last_used_at": null,
  "key": "u2a_xxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

## 7. 跑通一次 `/v1/chat/completions`

密钥鉴权、模型解析规则、错误码详见 [API-REFERENCE.md](API-REFERENCE.md)。这里给最小可用链路。

先启用至少一个 provider（默认只有 codebuddy 启用，且需要它的上游真实可达）：

```bash
# 启用 opencode（主仓内置实现，直连 Zen/Go，无需本地 sidecar）
curl -b cookies.txt -X PATCH http://127.0.0.1:8010/api/providers/opencode \
  -H "content-type: application/json" -d '{"enabled":true}'
```

在「服务商」页给 opencode 填 api_key（或 curl）：

```bash
curl -b cookies.txt -X PATCH http://127.0.0.1:8010/api/providers/opencode \
  -H "content-type: application/json" \
  -d '{"api_key":"你的上游密钥"}'
```

发一次对话（OpenAI 协议）：

```bash
curl http://127.0.0.1:8010/v1/chat/completions \
  -H "Authorization: Bearer u2a_你的密钥" \
  -H "content-type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

- `model` 的解析顺序：`x-provider` 请求头显式指定 → `provider前缀/模型名`（如 `opencode/gpt-4o-mini`）→ 「设置」页的模型别名表 → 在已启用 provider 的模型目录里按同名 `id` 自动匹配（唯一命中直接走，多家命中 400 `ambiguous_model`，无命中 404）。只写模型名也能路由，前提是该 `id` 在 `/api/models` 里唯一。
- 流式请求把 `"stream": true` 打开即可，SSE 原样直通。
- 三个协议的完整示例（curl / Python / Node，流式与非流式）见 [examples.md](examples.md)。

验证结果落在哪：仪表盘的统计来自 `request_logs`（次数/token/延迟/状态码），`usage_records` 另存上游返回的原始 usage JSON。日志是旁路记录，不影响响应。

## 8. 本地快速自检（无真实上游凭证）

没有真实上游密钥时，用仓库自带的假上游走通链路：

```bash
cd apps/control
python -m uvicorn scripts.fake_upstream:app --port 7899
```

然后在「服务商」页把 muse / gptgrok / zcode 的 Base URL 改成 `http://127.0.0.1:7899`（API Key 随便填），即可验证转发、SSE、usage 提取与 zcode 的 system 改写。
