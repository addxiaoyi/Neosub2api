# 运维/上线手册（OPS-RUNBOOK）

面向运维与值班同学：起停服务、上线前核对、故障判断，只收运行相关事实；部署细节与接口文档不在此重复，详见 `docs/DEPLOYMENT.md`、`docs/DEPLOYMENT-PROD.md`、`docs/API-REFERENCE.md`。

## 1. 上线前检查清单

- [ ] **构建上下文是仓库根，不是 `apps/control`**。`app/config.py` 按 `parents[3]` 找仓库根，镜像内路径为 `/repo/apps/control`，构建时要 COPY `config/`（价格表、别名表）与 `CHANGELOG.md`（系统页要读）。
  怎么确认：`docker compose config | grep -E "context|dockerfile"`，根编排应输出 `context: .` + `dockerfile: apps/control/Dockerfile`，deploy 下两份为 `context: ..`；`docker compose build control` 无 COPY 失败报错；启动后系统页能读到 CHANGELOG 内容。
- [ ] **根 `.dockerignore` 排掉 `data/`、`.env*`、`.git`、`apps/web`、`docs`**，否则构建上下文上 GB。
  怎么确认：`cat .dockerignore` 含上述条目；build 输出的 context 体积在合理范围而非 GB 级。
- [ ] **三份编排都能起、两个服务健康检查通过**（根 `docker-compose.yml`、`deploy/docker-compose.yml`、`deploy/docker-compose.prod.yml` 均已实测）。
  怎么确认：`docker compose ps`（带 `-f` 的同理）两个服务 STATUS 为 `healthy`；`deploy/healthcheck.sh`（Windows 用 `deploy/healthcheck.ps1`）退出码 0；直连探针 `curl -f http://127.0.0.1:8010/healthz` 返回 200。
- [ ] **`nginx -t` 通过**。
  怎么确认：`docker compose -f deploy/docker-compose.prod.yml exec nginx nginx -t` 输出 `syntax is ok`。
- [ ] **web 容器配置含 `root /usr/share/nginx/html;` + `index index.html;`**（缺这两行曾导致官方镜像首页 500）。
  怎么确认：入口 `curl -I http://127.0.0.1:8080/` 返回 200 且 `content-type: text/html`（prod 入口为 80/443，前置 nginx 后同样测）。
- [ ] **8080 端口空闲**。本机常被 node 进程占用。
  怎么确认：Windows `netstat -ano | findstr :8080`；Linux `ss -ltnp | grep ':8080'`。有输出即被占用，起栈前先让出或换端口。
- [ ] **真实 TLS 证书在目标环境签发**。`deploy/certs/` 当前是空目录，证书未签发；本机验证用的临时自签证书测完即弃，这一步必须在目标环境做。
  怎么确认：`ls deploy/certs/` 有目标环境签发的证书文件（非空）；`docker compose -f deploy/docker-compose.prod.yml exec nginx nginx -t` 通过后，`curl -I https://<域名>/` 无证书错误。
- [ ] **数据目录与所选编排一致**：根 compose 挂 `./data`、`deploy/` 两份挂 `../data`（都是同一份仓库根 `data/`），prod 用命名卷 `unified_data`。
  怎么确认：根/deploy 运行后 `ls data/unified.db` 存在；prod 用 `docker volume ls | grep unified_data` 且 `docker compose -f deploy/docker-compose.prod.yml exec control ls /data` 能看到 `unified.db`。
- [ ] **演示模式闸门关闭**：三份 compose 均强制 `UNIFIED_DEMO_MODE=0`。
  怎么确认：`docker compose config | grep UNIFIED_DEMO_MODE` 输出 `"0"`（各 `-f` 编排分别查一次）。

## 2. 启动与停止

| 编排 | 启动 | 停止 | 环境变量 |
|---|---|---|---|
| 根（本机自托管） | `docker compose up -d --build` | `docker compose down` | 仓库根 `.env`，模板 `.env.example` |
| deploy 本机 | `docker compose -f deploy/docker-compose.yml up -d --build` | `docker compose -f deploy/docker-compose.yml down` | `deploy/.env`，模板 `deploy/.env.example` |
| deploy 生产 | `docker compose -f deploy/docker-compose.prod.yml up -d --build` | `docker compose -f deploy/docker-compose.prod.yml down` | 同上（TLS/域名入口在这份） |

- 启动顺序：web 依赖 control 健康（`service_healthy`），起栈时先看 `docker compose ps` 里 control 是否转 healthy，再看 web；卡住通常是 control 探针没过。
- 取管理员口令：根编排口令留空时容器首次启动随机生成，写到 `data/admin-password.txt` 并打印在日志里。怎么确认：`cat data/admin-password.txt` 或 `docker compose logs control` 中搜 password。
- 停止不要 `docker kill`：编排设了 `stop_grace_period: 30s` 给 SQLite WAL 收尾，强杀有落盘风险。怎么确认：`docker compose ps` 显示正常退出（Exited 0），而非被 OOM/Kill。
- 日志：`docker compose logs -f control`；容器侧日志轮转为 json-file 10m×3，排障窗口有限，超时前先落盘。详见 `docs/TROUBLESHOOTING.md`。

## 3. 常见故障与判断方法

| 现象 | 怎么确认（判断依据） | 原因 | 处置 |
|---|---|---|---|
| `docker compose up` 报 8080 端口已占用 | `netstat -ano \| findstr :8080` / `ss -ltnp \| grep ':8080'`，看 PID 与进程名（常见是 node） | 本机 node 进程占了 8080 | 让出端口或改 web 容器宿主端口映射后重启 |
| 构建极慢、context 上传到 GB 级 | build 输出的 context 体积；`cat .dockerignore` 缺条目 | 根 `.dockerignore` 没排 `data/`、`.env*`、`.git`、`apps/web`、`docs` | 补齐上述条目，重新 build |
| control 镜像构建失败，报找不到仓库根文件 / COPY `config/`、`CHANGELOG.md` 失败 | `docker compose config` 看 `build.context` 与 `dockerfile` | 构建上下文传成了 `apps/control`，而 `app/config.py` 按 `parents[3]` 找仓库根 | context 改回仓库根（`context: .` / `..`，`dockerfile: apps/control/Dockerfile`） |
| 官方镜像首页 500 | `curl -I http://127.0.0.1:8080/` 非 200 或非 `text/html` | `apps/web/nginx.conf` 缺 `root /usr/share/nginx/html;` 与 `index index.html;` | 补齐两行、`nginx -t` 通过后 reload |
| 容器报「数据库不可读」 | `docker compose logs control` 搜「数据库」；`lsof data/unified.db`（Linux）或资源监视器（Windows）看是否有第二个进程在用同一文件 | 同机同时跑宿主机版和容器版，互抢 `data/unified.db` 文件锁，Windows 上更明显 | 二选一运行，或给两套实例各自独立数据目录 |
| 上游巡检 11 家全报 `error`（`rate_limited`） | `python scripts/check_upstreams.py` 输出含 `rate_limited`，或出现「未配置令牌，可能被限流」提示 | 匿名访问撞 GitHub 限流（额度 60 次/小时） | 配 `GH_TOKEN` 或 `GITHUB_TOKEN` 后重跑；巡检只读 HEAD 不读码，`--update` 只写 `checked_at` 且跳过 error 记录 |
| 测试单条用例从 0.5 秒涨到几分钟（曾出现 387s vs 55.90s） | `echo $env:HTTP_PROXY`（Linux：`env \| grep HTTP_PROXY`）非空；pytest 计时异常 | 本机有 `HTTP_PROXY`，测试里 httpx 没关环境代理 | 测试中 httpx 必须 `trust_env=False`（36 个调用点已全部加上，新增调用点保持一致） |
| 并发上到 64 后 p95 明显抬头 | 压测各并发档位 p95 对比，详见 `docs/PERFORMANCE.md` | 拐点是 SQLite 单写者，不是网络 | 属预期，无需处置；要提吞吐需另评估存储方案 |
| demo 实例放在同机 nginx 反代后，请求级 403 闸门不生效 | `docker compose config \| grep UNIFIED_DEMO_MODE` | demo 闸门只允许回环，判断依据是 `request.client.host`；反代后来源全是 127.0.0.1，闸门形同虚设 | demo 实例不要放同机反代后面；编排已强制 `UNIFIED_DEMO_MODE=0`，确认输出为 `"0"` |
| prod 前置 nginx 报 502，连不上 control | `docker compose -f deploy/docker-compose.prod.yml logs control` 看进程是否起来；`docker compose -f deploy/docker-compose.prod.yml exec control env \| grep UNIFIED_HOST` 应输出 `UNIFIED_HOST=0.0.0.0` | control 未启动 / 健康检查失败；prod compose 的 `UNIFIED_HOST` 已硬编码 `0.0.0.0`（`deploy/docker-compose.prod.yml:21`），`.env` 改不动它 | 按 logs 定位退出原因（数据卷、端口、口令），修完重启 control；`env` 输出确认为 `0.0.0.0` 后再查 nginx 上游 |
| https 打开报证书错误，或 nginx 起不来 | `ls deploy/certs/` 为空 | 真实 TLS 证书未签发（本地临时自签已弃用） | 在目标环境签发证书后再起 nginx，并重跑 `nginx -t` |

## 4. 运行前提（不写会误判）

- **演示模式闸门**：`UNIFIED_DEMO_MODE=1` 只允许回环访问，但它看的是 `request.client.host`——不要把 demo 实例放在同机 nginx 反代后面，代理后端 IP 是 127.0.0.1，这道闸形同虚设。三份 compose 都已强制 `UNIFIED_DEMO_MODE=0`。
  怎么确认：`docker compose config | grep UNIFIED_DEMO_MODE` 输出 `"0"`。
- **SQLite 文件锁**：同一台机器同时跑「宿主机版」和「容器版」会互抢 `data/unified.db` 的锁（Windows 上更明显，容器里报「数据库不可读」）。
  怎么确认：`docker compose logs control` 搜「数据库」；`lsof data/unified.db` 看持锁进程是否不止一个；`netstat -ano | findstr :8010` 查是否有第二个实例在监听。
- **上游巡检需要令牌**：`scripts/check_upstreams.py` 匿名跑会撞 GitHub 限流，11 家全报 `error`（`rate_limited`），需配 `GH_TOKEN`/`GITHUB_TOKEN`。巡检只读 HEAD、不读码；`--update` 只写 `checked_at` 且跳过 error 记录。
  怎么确认：脚本启动时打印的令牌提示（无令牌会提示「未配置令牌，可能被限流」）；跑完看输出里还有没有 `rate_limited`。巡检细节详见 `docs/UPSTREAM-CHECK-RUNBOOK.md`。
- **测试环境**：本机有 `HTTP_PROXY` 时，测试里的 httpx 必须 `trust_env=False`，否则单条用例能从 0.5 秒变成几分钟（曾出现 387s vs 55.90s）；测试目录 36 个调用点现已全部加上。
  怎么确认：`env | grep HTTP_PROXY`（Windows：`echo $env:HTTP_PROXY`）是否非空；pytest 单条计时是否在秒级；`grep -r "trust_env" apps/control/tests` 覆盖新增调用点。
- **一键启动脚本的用例按平台分流，别把 CI 绿当成 Windows 和 Linux 两个脚本都被本机验证过**：`scripts/tests/test_start_scripts.py` 的 4 条用例挂在 `.github/workflows/ci.yml` 的 `start-scripts` job（不并入 backend job，后者没有 Node 与 PowerShell）。`windows-latest` 只覆盖 `start.ps1` 分支，`ubuntu-latest` 只覆盖 `start.sh` 分支；macOS 未单独挂，POSIX 行为与 Linux 同源。
  怎么确认：PR 或 push main 的 Checks 里 `start-scripts (windows-latest)` 与 `start-scripts (ubuntu-latest)` 均绿，日志为 `4 passed` 且无 SKIPPED。
- **压测**：拐点是 SQLite 单写者，不是网络；并发上到 64 后 p95 明显抬头，属预期。
  怎么确认：压测各并发档位 p95 对比，详见 `docs/PERFORMANCE.md`。
- **老库升级（本轮 schema 改动）**：对存量库幂等、无损，已验证 44/44 断言——补列幂等（老行 `key_prefix` 为 NULL、不回填假值）、两条新索引自动重建且部分索引 `WHERE` 谓词逐字保真、七张业务表行数与内容指纹零损失、第二次启动零错误且索引数不变。**限制**：`billing_ledger.log_id` 只加列不回填，存量成功日志升级后在 `/api/logs` 统一显示 `credit_status=unlinked`、金额为空；若要让历史金额可见需单独立项做回填。
  怎么确认：升级前后对七张业务表做行数与内容指纹对比；第二次启动日志零错误，`SELECT count(*) FROM sqlite_master WHERE type='index'` 前后不变；抽一条存量成功日志看 `/api/logs` 的 `credit_status` 为 `unlinked`。

## 5. 已知边界

- **nginx query 脱敏**：access log 走 `u2a_redacted` 格式（18 个别名逐级 map + `u2a_` 兜底，最终落 `$log_uri_20` 字段），`error_log crit`。针对大小写与 URL 编码形态的绕过：**已加固/待验证**（另一会话在做，复测结论未回），在结论回来前不要对外宣称已彻底解决。
  怎么确认：`docker compose exec nginx tail /var/log/nginx/access.log`，query 中的敏感参数应以掩码形式落盘；加固复测结论以对应会话产出为准。
- **密钥处理**：完整密钥只在创建时一次性明文返回；列表只显示前缀；`reveal` 端点 admin-only 且写审计（成功与失败都写）；完整密钥不接受出现在 query 参数中。
  怎么确认：创建时保存一次性返回值；拉一次列表确认响应只含前缀；用非 admin 会话调 `reveal` 应 403；`/api/audit-logs` 能查到该次调用（成功与失败均有记录）。端点细节详见 `docs/API-REFERENCE.md`。
- **日志与统计可见性**：非 admin 只能看到自己名下密钥的日志与统计；`NULL` 归属的历史 key 对非 admin 不可见（宁可少算，不串算）。
  怎么确认：分别用 admin 与非 admin 会话拉 `/api/logs` 与统计接口，对比条目归属——非 admin 视图中不应出现他人密钥与 NULL 归属记录。
