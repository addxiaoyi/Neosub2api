# 生产部署（硬化版）

面向公网或内网正式环境的部署方式：nginx 统一入口（HTTPS、SSE、缓存策略）、容器健康检查、资源限制、日志轮转、在线备份。
本地开发与基础 compose 用法见 [DEPLOYMENT.md](DEPLOYMENT.md)，这里只写它没覆盖的部分，末尾统一引用。

## 1. 文件构成

| 文件 | 作用 |
| --- | --- |
| `deploy/nginx.conf` | 入口反向代理，挂载为 `/etc/nginx/conf.d/default.conf` |
| `deploy/docker-compose.prod.yml` | 生产编排（不替换原 `docker-compose.yml`） |
| `deploy/.env.example` | 生产环境变量模板，复制为 `deploy/.env` |
| `deploy/healthcheck.ps1` / `deploy/healthcheck.sh` | 探 `/healthz` 与静态首页，非 200 退出码非 0 |
| `scripts/backup.ps1` | SQLite 在线备份、压缩、保留最近 N 份（恢复步骤写在脚本头部） |

`deploy/.env`（含管理员密码）与 `deploy/certs/`（含私钥）不要提交到仓库。

## 2. 端口与拓扑（已按 Dockerfile 核实）

```
浏览器 → nginx(宿主 80/443) → web:80（Next 静态导出，nginx:alpine）
                          └→ control:8000（FastAPI，宿主 8010）
```

| 宿主端口 | 容器 | 说明 |
| --- | --- | --- |
| 80 / 443 | nginx | 对外唯一入口；80 只做 ACME 与 `/healthz`，其余 301 到 HTTPS |
| 3000 | web:80 | `apps/web/Dockerfile` 的运行阶段是 `nginx:alpine` 监听 **80**，3000 是 `next dev` 的开发端口，不是生产端口 |
| 8010 | control:8000 | 只绑 `127.0.0.1`，仅排障用，不对外 |

- 3000 与 8010 都绑定 `127.0.0.1`，公网只暴露 80/443。
- 数据库落在命名卷 `unified-2api-prod_unified_data`（卷名前缀来自 compose 的 `name:`）。

## 3. 首次部署

前置条件：域名 A 记录已指向服务器；安全组/防火墙放行 80、443；Docker Compose ≥ 2.24（`env_file` 的 `required` 语法）。老版本把 compose 里 `required: false` 那两行删掉即可。

```powershell
# 1. 变量：至少改密码与域名；文件与 compose 同目录，compose 会读取它做插值
Copy-Item deploy\.env.example deploy\.env
notepad deploy\.env

# 2. 证书目录（没有证书时 nginx 会因 cannot load certificate 反复重启）
New-Item -ItemType Directory -Force deploy\certs, deploy\certbot-webroot

# 3. 先拿证书（见第 4 节），再启动
docker compose -f deploy\docker-compose.prod.yml build
docker compose -f deploy\docker-compose.prod.yml up -d

# 4. 自检
docker compose -f deploy\docker-compose.prod.yml ps
powershell -File deploy\healthcheck.ps1 -BaseUrl https://你的域名
```

首次登录用 `deploy\.env` 里的 `UNIFIED_ADMIN_USER` / `UNIFIED_ADMIN_PASSWORD`，然后到「密钥」页创建 `u2a_...` 网关密钥。创建后立即验证流式链路（见第 7 节），这是最容易在代理层被悄悄破坏的一环。

## 4. 证书获取

### 4.1 Let's Encrypt 首次签发（此时 nginx 还没起，用 standalone）

```bash
docker run --rm -p 80:80 \
  -v "$PWD/deploy/certs:/etc/letsencrypt" \
  certbot/certbot certonly --standalone \
  -d api.example.com --email you@example.com --agree-tos --no-eff-email
```

签发完把 LE 的 `live/` 目录软链目标复制成 nginx 配置里固定的文件名：

```bash
cp deploy/certs/live/api.example.com/fullchain.pem deploy/certs/fullchain.pem
cp deploy/certs/live/api.example.com/privkey.pem  deploy/certs/privkey.pem
```

### 4.2 续期（nginx 在跑，用 webroot，不需要停机）

`deploy/nginx.conf` 已放行 `/.well-known/acme-challenge/` 并挂载 `deploy/certbot-webroot`：

```bash
docker run --rm \
  -v "$PWD/deploy/certs:/etc/letsencrypt" \
  -v "$PWD/deploy/certbot-webroot:/var/www/certbot" \
  certbot/certbot renew --webroot -w /var/www/certbot

cp deploy/certs/live/api.example.com/fullchain.pem deploy/certs/fullchain.pem
cp deploy/certs/live/api.example.com/privkey.pem  deploy/certs/privkey.pem
docker compose -f deploy/docker-compose.prod.yml exec nginx nginx -s reload
```

放到 cron（Linux）或计划任务（Windows），每天跑一次；`renew` 未到期会自动跳过。复制与 reload 不能省——nginx 只在启动/重载时读证书文件。

### 4.3 自签名证书（仅内网/联调）

宿主机有 openssl 就直接生成；没有则用临时容器（只影响这一次操作，不引入项目依赖）：

```bash
docker run --rm -v "$PWD/deploy/certs:/certs" alpine:3.20 \
  sh -c "apk add --no-cache openssl >/dev/null && \
  openssl req -x509 -nodes -days 30 -newkey rsa:2048 \
  -keyout /certs/privkey.pem -out /certs/fullchain.pem -subj '/CN=localhost'"
```

同时把 `deploy/nginx.conf` 里的 `Strict-Transport-Security` 那行注释掉，否则浏览器会记住 HSTS，之后连自签名都进不去。

## 5. 迁移已有数据（`./data` → 命名卷）

```powershell
# 0. 先做一份在线备份，出问题可退回
powershell -File scripts\backup.ps1

# 1. 停掉整套（保证没有写入）
docker compose -f deploy\docker-compose.prod.yml down

# 2. 用 control 镜像把旧目录拷进命名卷；顺便清掉可能残留的 -wal/-shm
docker compose -f deploy\docker-compose.prod.yml run --rm --no-deps `
  -v "${PWD}/data:/src:ro" control `
  sh -c "rm -f /data/unified.db*; cp -a /src/. /data/; ls -l /data"

# 3. 启动并验证
docker compose -f deploy\docker-compose.prod.yml up -d
powershell -File deploy\healthcheck.ps1 -BaseUrl https://你的域名
```

迁移后先保留旧 `data/` 目录至少一个发布周期。登录后台核对密钥、账号、请求日志条数与迁移前一致，再删旧目录。

## 6. 更新与回滚

### 更新（单实例，秒级中断）

```powershell
powershell -File scripts\backup.ps1 -UseContainer -Compress
git pull
docker compose -f deploy\docker-compose.prod.yml build
docker compose -f deploy\docker-compose.prod.yml up -d
powershell -File deploy\healthcheck.ps1 -BaseUrl https://你的域名
```

`up -d` 只会重建有变化的容器，期间网关入口有数秒 502 窗口；健康检查会让编排等待 control 就绪再起 web/nginx。需要真正零中断就做蓝绿：复制一份 compose 改 `name:` 与端口，新栈验证通过后切 DNS/负载均衡，而不是原地重启。

### 回滚

```powershell
git checkout <上一个可用提交>
docker compose -f deploy\docker-compose.prod.yml build
docker compose -f deploy\docker-compose.prod.yml up -d
```

库结构只有 `CREATE TABLE IF NOT EXISTS` 与补列（`apps/control/app/db.py`），旧代码能容忍多出来的列；但**不要**把新版本写过的库直接给旧版本做破坏性回滚，涉及字段语义变化时按 `scripts/backup.ps1` 头部的恢复步骤还原备份。

## 7. 监控要点

| 项目 | 检查方式 | 告警阈值建议 |
| --- | --- | --- |
| 入口与控制面存活 | `powershell -File deploy\healthcheck.ps1 -BaseUrl https://域名` | 连续 2 次失败 |
| 容器健康 | `docker compose -f deploy/docker-compose.prod.yml ps` | 任一 `unhealthy` |
| 502/504/413 | `docker compose ... logs --tail=100 nginx` | 5 分钟内出现即查 |
| SSE 是否真的流式 | 下面 curl 命令，观察是否逐块到达 | 出现"攒完一次性返回" |
| 磁盘 | `docker compose ... exec control ls -lh /data` | 可用空间 < 20% |
| WAL 膨胀 | 同上，看 `unified.db-wal` 是否长期大于主库 | 持续增长说明长事务/写入异常 |
| 证书到期 | `docker run --rm -v "$PWD/deploy/certs:/etc/letsencrypt" certbot/certbot certificates` | 剩余 < 14 天 |
| 资源 | `docker stats --no-stream` | 接近 compose 里 cpus/memory 上限 |

流式验证（关键，能同时验证 Authorization 透传、SSE 不缓冲、HTTPS 链路）：

```bash
curl -N -H "Authorization: Bearer $U2A_KEY" -H "Content-Type: application/json" \
  -d '{"model":"your-model","stream":true,"messages":[{"role":"user","content":"hi"}]}' \
  https://api.example.com/v1/chat/completions
```

预期是 token 逐步打印；如果先静默数秒再一次性吐出，说明链路上还有一层缓冲（CDN、云负载均衡或别的代理），要按同样规则关掉 buffering 与压缩。

## 8. 常见错误对照

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| nginx 反复重启，日志 `cannot load certificate` | `deploy/certs/fullchain.pem` 或 `privkey.pem` 缺失 | 按第 4 节签发并复制到固定文件名，再 `up -d` |
| 页面能开、接口 502 | control 未 healthy，或 nginx 网络里解析不到 `control` | `docker compose ps` 看 health；`logs control` 看启动错误 |
| 接口 504、流式卡住 | 中间代理仍在缓冲，或 `/v1` 超时被调小 | 确认请求真的经过本套 nginx；对比直连 `127.0.0.1:8010` |
| 所有网关请求 401 | `Authorization` 被外层代理剥掉 | 本套配置已显式 `proxy_set_header Authorization $http_authorization`；检查 CDN/其他网关有没有 `Authorization ""` |
| 登录成功但刷新掉登录 | 用 `http://` 访问，而容器里 `UNIFIED_COOKIE_SECURE=true` | 一律走 `https://`；仅本地调试时临时改 compose 里该值为 `false` |
| 上传/长上下文返回 413 | 请求体超过 `client_max_body_size 32m` | 按需调大该值，同时留意内存 |
| 发版后仍是旧页面 | 外层 CDN 缓存了 HTML | 源站已 `no-cache`；CDN 侧也要对 HTML 关闭缓存、只缓存 `/_next/static/` |
| 续期后浏览器仍报证书过期 | 忘了复制到固定文件名或没 reload | 重跑第 4.2 节最后三条命令 |
| Linux 上上游全部"不可达" | 容器访问不到宿主机 sidecar | compose 已加 `host.docker.internal:host-gateway`；确认 `.env` 里上游地址用的是该主机名 |
| `docker compose config` 报 `env_file` 不认识 | Compose < 2.24 | 删除 compose 中 `required: false` 两行，或升级 Compose |

## 9. 上线前自检

```bash
docker compose -f deploy/docker-compose.prod.yml config --quiet
docker compose -f deploy/docker-compose.prod.yml run --rm --no-deps nginx nginx -t
```

第二条需要 `deploy/certs` 里已有证书文件，否则会因证书缺失失败——这是预期行为，不是配置写错。

本次交付已实际执行 `docker compose config`、脚本语法检查、备份/恢复演练；`nginx -t` 未在交付机器执行（本机无 nginx、Docker daemon 未启动），上线前请按上面命令自行验证。

开发环境启动、基础 compose 命令、数据目录说明与更多故障排查见 [DEPLOYMENT.md](DEPLOYMENT.md)，本文不重复。
