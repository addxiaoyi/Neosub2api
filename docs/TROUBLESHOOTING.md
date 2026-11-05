# 故障排查（Troubleshooting）

按症状索引。先确认服务本身活着：`curl http://127.0.0.1:8010/healthz` 返回 `{"ok":true,...}` 说明控制面正常，问题在上游或客户端；连不上说明是启动/端口/环境问题。

## 症状速查表

| 症状 | 直接原因 | 处理 |
| --- | --- | --- |
| 启动报地址被占 / compose 起不来 | 8000 端口被占 | 用 8010（见 §1） |
| `docker compose` 连不上 Docker | Docker Desktop 未启动 | 见 §2 |
| Python 启动即崩，报缺 `encodings` 等 | 系统 Python 标准库损坏 | 见 §3 |
| CodeBuddy 账号操作返回 403 | `wbt_` token 权限不足 | 见 §4 |
| 网关返回 503 | 上游限流 / 无可用 provider / sub2api 未配置 | 见 §5 |
| 网关返回 401 | 密钥问题 | 见 §6 |
| 网关返回 502 | 上游不可达 | 见 §7 |
| 网关返回 501 | provider 能力位不支持 | 见 §8 |
| `/api/status` 显示 `connected: false` | 上游未启动或地址不对 | 见 §9 |
| 页面 404 / 刷新后 404 | 静态托管或反代回退缺失 | 见 §10 |

---

## 1. 启动报「address already in use」/ 8000 端口被占

控制面容器内固定监听 8000，宿主机 8000 经常被其他进程占用，所以 **compose 已把控制面映射到 8010**（`docker-compose.yml` 里 `8010:8000`），容器内部不动。

- 容器部署：直接用 8010，不要去改容器内端口；`apps/web/nginx.conf` 的 upstream 写的是容器内 `control:8000`，改了内部端口反而会断开反代。
- 本地直跑：`python -m uvicorn app.main:app --port 8010`；如果连 8010 也被占，查占用者并换端口：

```powershell
# Windows：查 8010 被谁占
netstat -ano | findstr :8010
# Linux/macOS
lsof -i :8010
```

- 换用其他宿主机端口（如 8011）时：本地直跑改 `--port`；compose 改映射 `"8011:8000"`；前端 dev 模式记得同步 `NEXT_PUBLIC_BACKEND_BASE_URL`。
- 注意 8010 只是「控制面对外端口」；若你还在 8010 上跑过别的服务（另一份 codebuddy2api 等），先停掉或换端口。

## 2. `docker compose` 报连不上 Docker / daemon

典型报错：`error during connect: ... docker daemon is not running`（Windows）或 `Cannot connect to the Docker daemon`（Linux）。

1. Docker Desktop（Windows/macOS）没启动：先启动它，等托盘图标就绪再执行 compose。
2. Linux：`sudo systemctl start docker`，并把当前用户加进 docker 组（`sudo usermod -aG docker $USER`，重新登录生效）。
3. 确认 `docker info` 能正常输出后再 `docker compose up -d --build`。

## 3. Python 启动即崩：`ModuleNotFoundError: encodings` / 标准库异常

症状：任何 Python 命令都崩，报缺 `encodings`、`codecs` 等标准库模块。这是**系统 Python 安装本身损坏**（标准库目录缺失/损坏），不是本项目的问题。

处理（任选其一）：

1. 重装官方 Python 3.11+（推荐，治本），重开终端后确认 `python -c "print('ok')"` 能跑。
2. 临时绕过：用一个完好的 Python embed 包。本仓库作者验证时用的是仓库内 `.runtime/python`（3.12 embed 包，已 gitignore）——换机器用正常安装的 Python 即可，**不需要**这个目录。
3. 排查环境劫持：检查 `PYTHONHOME` / `PYTHONPATH` 是否指向了错误的目录，虚拟环境是否基于已删除的解释器创建（删掉 `.venv` 重建）。

## 4. CodeBuddy 账号操作返回 403（`wbt_` token 权限不足）

背景：控制面对接 codebuddy2api 的管理端（7864）用的是作用域化 API Token（`wbt_...`）。管理端的**删除账号 / 重启 / 设置下发**要求**会话级管理员**，token 可能被拒。

- 表现：账号页的删除/重启、设置页的保存下发返回 403，detail 是管理端原样透传的错误（控制面不掩盖也不伪造成功）。
- 处理：
  1. 确认 token 在 codebuddy2api 管理端侧的授权范围包含你要做的操作（授权模型以 codebuddy2api 文档为准，待确认——控制面侧只透传，不决定权限）；
  2. 需要会话级管理员权限的操作，登录 codebuddy2api 自己的管理界面（7864）执行；
  3. 管理端在线但完全没配 token 时，`/api/status` 会返回 `connected: true, auth: "missing"` 并提示「需配置 API Token」——这属于配置缺失，不是故障；去「服务商」页给 codebuddy 填 api_key 或设 `CODEBUDDY_API_KEY`。

## 5. 网关返回 503

503 有三种来源，看响应 `detail` 区分：

| detail 示例 | 原因 | 处理 |
| --- | --- | --- |
| `OpenCode 未配置上游密钥...` | opencode 没填 api_key / extra_keys | 服务商页补密钥 |
| `sub2api 配额分组未配置...` | sub2api 还没建分组/账号 | 先建分组和账号（`/api/sub2api/groups`、`/accounts`） |
| `sub2api 分组订阅额度已耗尽` / `账号池无可用账号` | 配额或池子耗尽 | 重置额度（`/api/sub2api/accounts/{id}/reset`）或加账号 |
| `上游 <path> 返回 503：<原文>` | **上游限流或暂时不可用** | 控制面原样透传状态码；opencode 适配器对 503 已做换 key 重试（最多 `max_attempts` 次，退避封顶 2s），重试耗尽后透传。处理：换 key、降低并发、稍后重试，或给该 provider 配 `options.proxy` 换出口 |

上游 429 同理透传；sub2api 适配器还会把 429/5xx 的账号冷却 60 秒并自动换号（最多 3 次尝试）。

模型解析失败不再产生 503，而是 400/404 结构化错误：无命中 `404 model_not_found`；只存在于未启用 provider `404 model_unavailable`（`candidates` 带 `requires` 与原因）；多家命中 `400 ambiguous_model`（`candidates` 带 `provider/readiness`）。先看 `/api/models` 里该 `id` 归谁，再写成 `provider/model` 即可。详见 `API-REFERENCE.md`「模型解析顺序」。

## 6. 网关返回 401

- `缺少 Authorization: Bearer <key>`：请求没带头。检查客户端是否真的发了 `Authorization`；套了反代的看是不是代理把头剥了（见 [DEPLOYMENT.md](DEPLOYMENT.md) §3①）。
- `无效的 API Key`：key 写错、被吊销（删除即失效）或被停用（`enabled: false`）。密钥库只存哈希，**明文丢了没法找回**，只能吊销重建。
- 匿名开关：`/api/settings/gateway` 的 `allow_anonymous` 打开后才能不带密钥调 `/v1`；默认（环境变量 `UNIFIED_ALLOW_ANONYMOUS_GATEWAY=false`）拒绝匿名。注意 DB 配置一旦保存就优先于环境变量，改环境变量不再生效。

## 7. 网关返回 502

detail 形如 `上游不可达（http://...）：...`，是控制面连不上上游的连接层错误：

1. 上游进程/容器没起：按 [PROVIDERS.md](PROVIDERS.md) 的端口表核对；
2. 地址不对：容器里的控制面访问宿主机要用 `host.docker.internal`（compose 默认已这么配），Linux 宿主机需要 `extra_hosts: host-gateway`；
3. 系统代理劫持：控制面的 HTTP 客户端刻意不读系统代理（`trust_env=False`），上游在内网但被代理规则拦截时，给 provider 配 `options.proxy` 显式指定出口即可，不需要动系统代理。
4. **deepseek_sidecar 连不上先核对端口**：Deepseek2Go 上游默认监听 **5001**（`PORT` 可覆盖），仓内 `DEEPSEEK_SIDECAR_BASE` 默认也是 5001；仓内**早期版本**默认 7872，从旧版升级后 `.env` 里残留的 `7872` 会导致连接失败（报「连接失败」而非「配置错误」，容易误判成 sidecar 没启动）。处理：把 `DEEPSEEK_SIDECAR_BASE` 改成 `http://127.0.0.1:5001`（compose 部署为 `http://host.docker.internal:5001`），或反过来给 sidecar 设 `PORT=7872`——两边对齐即可。「服务商」页里给 deepseek_sidecar 单独配过 base_url 的同理核对。

## 8. 网关返回 501

detail 形如 `<provider> 不支持 <capability>` 或 `<provider> 适配器未实现 /v1/messages`——请求打到了不支持该协议的 provider：

- trae_sidecar / muse 没有 Anthropic `/v1/messages`（muse 的实证：`apps/control/app/adapters/muse.py:62-67` 未声明 `messages`、也未覆写该方法（`:225` 起只有 `responses`），能力门在 `apps/control/app/routers/v1.py:366-372` 回 501 `capability_unsupported`；原句把 `gptgrok` 一并列入已过期，见本节末更正块）；
- deepseek_sidecar 不支持流式 `/v1/messages`（仅非流式）；
- 处理：换用声明 `messages` 的 provider（codebuddy / opencode / zcode / sub2api / codar / codar_sidecar / qoder / qoder_sidecar / gptgrok / deepseek_sidecar（仅非流式）；完整清单见 [PROTOCOL-MATRIX.md](PROTOCOL-MATRIX.md) §1），或在请求里用 `provider前缀/模型` 明确路由。

> 2026-11-05 已更正（原句逐字留档）：上一行原为「- trae_sidecar / muse / gptgrok 没有 Anthropic `/v1/messages`；」，其下原为「- 处理：换用双协议 provider（codebuddy / opencode / zcode / sub2api / qoder_sidecar），或在请求里用 `provider前缀/模型` 明确路由。」。两处同因与代码不符：`apps/control/app/adapters/gptgrok.py:400` 声明 `messages=True, responses=True`，`:1353` 的 `messages()` 用 `_render_anthropic` 把 ChatGPT 帧**合成** Anthropic SSE（流式是合成，非透传），路由挂载在 `apps/control/app/routers/v1.py:644`；同时旧清单漏列了 `codar` / `codar_sidecar` / `qoder` / `gptgrok` 等同样声明 `messages` 的家（`PROTOCOL-MATRIX.md` §1）。误判后果：客户端看到「gptgrok 没有 messages」会绕开网关直连 ChatGPT，或被迫降级到 chat 兼容层；按旧清单选路又会误以为只有 5 家可用。另：盘点表曾引 `plan:56` 说 muse 有 `/v1/messages`，该引用是错的；muse 的真实反证是 `muse.py:62-67` 未声明 `messages` 位、未覆写方法，`v1.py:366-372` 能力门统一回 501。核验口径与 [PROTOCOL-MATRIX.md](PROTOCOL-MATRIX.md) §1 / §3 一致。

## 9. `/api/status` 显示某家 `connected: false`

- 这是预期行为而非故障：上游没启动时状态页如实显示不可达，控制面「不假装成功」。
- 每家的 `error` 字段说明原因（不可达 / 401 需配 token / 在线但模型列表为空等），逐条处理。
- OpenAI 兼容系（muse/gptgrok/zcode）的健康检查探测 `/v1/models`：返回「上游在线但模型列表为空」说明服务在、账号池可能空。

## 10. 前端页面 404 / 刷新后 404

- 控制面直接托管（8010）：确认 `apps/web/out` 存在（先 `npm run build:export`）；目录不存在时控制面 `/` 返回 JSON 提示。重新 build 后无需重启控制面（StaticFiles 每次请求读磁盘），但新增路由文件首次访问即可生效。
- nginx 托管（8080）：镜像里的 `nginx.conf` 已配 `try_files $uri $uri/ /index.html;`；自建反代报 404 通常是缺这条 SPA 回退或路径前缀不匹配（子路径部署需构建时设 `NEXT_PUBLIC_BASE_PATH`）。

## 11. 其他

- **统计页 credit 全是 0**：先确认请求是否落进了 `billing_ledger`（`status >= 400` 且没有 usage 的请求不写流水，属于设计行为），再查 `price_books` 是否命中该模型（未定价时金额为 0 且 `estimated=1`，可用 `GET /api/pricing/resolve` 干跑确认）。缺价格就补价，见 [API-REFERENCE.md](API-REFERENCE.md) 第 10 节与 [pricing-sources.md](pricing-sources.md)。
- **忘记管理员密码**：停控制面，删除 `./data/unified.db` 会全量重置（密钥、日志全丢）。只想改密码且还有另一个 admin 账号登录：用户管理页直接改。当前版本没有 CLI 改密工具（待确认后续版本是否提供）。
- **Upstash 测试失败**：`/api/settings/upstash/test` 是真实只读 PING 探测，按 `error.code` 分流——`upstash_auth_rejected` 凭据被拒、`upstash_timeout` 超时、`upstash_unreachable` 连不上、`invalid_upstash_url`/`upstash_token_missing` 输入问题；SSRF 加固新增三码——`upstash_target_forbidden` 目标是内网/环回/保留地址/云元数据（字面量或 DNS 解析后任一结果命中，未发出请求，改填公网 Upstash 地址）、`upstash_redirect_blocked` 上游返回 3xx（探测不跟随跳转，直接填目标地址）、`upstash_response_too_large` 响应超 64KB 被中止（多半是地址填错打到了别的服务）。端点细节见 [API-REFERENCE.md](API-REFERENCE.md) 设置章节。
- **grok 注册任务全部失败**：先看 `/api/grok/register/worker` 的 `executor` 字段——`unconfigured` 说明没配 `GROK_REGISTER_EXECUTOR_URL`，任务会以明确原因失败，这是设计行为。
