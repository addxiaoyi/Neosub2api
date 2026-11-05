# 部署（Deployment）

docker compose 部署步骤、端口规划、反向代理注意事项、数据持久化与备份。本地开发启动见 [GETTING-STARTED.md](GETTING-STARTED.md)，故障排查见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。

## 1. docker compose 部署

```bash
cd unified-2api
cp .env.example .env        # 至少改 UNIFIED_ADMIN_PASSWORD
docker compose up -d --build
```

`docker-compose.yml` 定义两个服务：

| 服务 | 构建上下文 | 端口映射 | 说明 |
| --- | --- | --- | --- |
| `control` | `./apps/control`（python:3.11-slim + uvicorn） | 宿主机 **8010** → 容器 8000 | FastAPI 控制面；容器内 `UNIFIED_DATA_DIR=/data`，挂载 `./data:/data` |
| `web` | `./apps/web`（node:22 构建静态导出 + nginx:alpine 托管） | 宿主机 **8080** → 容器 80 | 多阶段构建：`npm ci && npm run build:export` → nginx 托管 `out/` 并反代控制面 |

- 访问入口二选一：
  - `http://127.0.0.1:8080` —— nginx 前端（静态页面 + 反代 `/api`、`/v1`、`/healthz` 到 `control:8000`）；
  - `http://127.0.0.1:8010` —— 控制面直接托管前端静态产物（控制面会把 `apps/web/out` 挂在 `/`；容器里该目录不存在时，控制面 `/` 返回提示 JSON，此时用 8080 入口）。
- 登录 `admin` / `UNIFIED_ADMIN_PASSWORD` 的值；该变量为空时首次启动随机生成口令并写入 `data/admin-password.txt`（容器内 `/data/admin-password.txt`），历史固定值 `unified-admin` 会被轮换。
- compose 已把控制面的上游地址默认值指向宿主机（`CODEBUDDY_BASE=http://host.docker.internal:7864` 等），sidecar 跑在宿主机时无需改。Linux 宿主机如 `host.docker.internal` 不可达，在 compose 里为 control 服务加 `extra_hosts: ["host.docker.internal:host-gateway"]`（需自行编辑 compose，本文不代改）。

常用运维命令：

```bash
docker compose up -d --build   # 改代码后重建
docker compose logs -f control # 看控制面日志
docker compose restart control # 只重启控制面
docker compose down            # 停止（./data 保留）
```

## 2. 端口规划

| 端口 | 归属 | 备注 |
| --- | --- | --- |
| **8010** | 控制面（宿主机） | 容器内 8000；宿主机 8000 常被占，刻意错开（见 TROUBLESHOOTING） |
| **8080** | nginx 前端（宿主机） | 容器内 80 |
| 7864 | codebuddy2api 管理端（上游，需自部署） | 7863 为其 Go 数据面 |
| 7865 / 7867 / 8787 | muse / gptgrok / zcode（上游，需自部署） | provider 默认地址，可在管理页改 |
| 7871 / 5001 / 7873 | trae / deepseek / qoder sidecar（上游，需自部署） | 同上；deepseek 上游默认 5001 |

控制面本身只占 8010 和数据目录；各家上游是独立进程/容器，自行决定部署位置，用「服务商」页的 base_url 或 `.env` 指过去即可。

## 3. 反向代理注意事项

在 compose 之外再套一层 nginx（或别的网关）时，以下三条都来自 sub2api 项目实际踩过的坑，仓库自带的 `apps/web/nginx.conf` 已按此处理，自建代理照抄即可：

**① 透传 Authorization 头。**
`/v1/*` 的网关鉴权依赖 `Authorization: Bearer u2a_...`。nginx 的 `proxy_pass` 默认会透传请求头，但如果你写了 `proxy_set_header Authorization "";`（有些老模板用来防伪造）或用了会剥头的中间件，网关就会一直 401。自建配置里不要动这个头。

**② SSE/流式响应要关缓冲。**
`/v1/*` 是流式转发，缓冲会让 SSE 攒成一坨最后一次性吐出，客户端表现为「卡住然后突然全出来」。参考仓库 nginx.conf：

```nginx
location /v1/ {
  proxy_pass http://control:8000;
  proxy_buffering off;
  proxy_read_timeout 300s;
  proxy_set_header Host $host;
}
```

`/api/` 与 `/healthz` 是普通 JSON，正常代理即可。

**③ SPA 路由要回退。**
前端是 Next.js 静态导出（`trailingSlash: true`），刷新 `/accounts/` 这类路径时必须回退到页面文件，否则 404。参考：

```nginx
location / {
  try_files $uri $uri/ /index.html;
}
```

**④ 静态资源缓存策略。**
前端产物带内容哈希（`/_next/static/*`），可以对它们设长缓存；但 `index.html` 与页面级 HTML **不要缓存**（`no-cache`），否则发版后用户拿到的还是旧页面、加载不到新 chunk。控制面托管的场景同理，给 HTML 响应加 `Cache-Control: no-cache`。

**⑤ HTTPS 场景。**
反代终止 TLS 后，把控制面的 `UNIFIED_COOKIE_SECURE=true` 打开，保证会话 Cookie 只走 HTTPS；同时 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` 保留真实 IP（审计日志记录的是 `request.client.host`，即直连对端，套了代理后就是代理地址——如需真实 IP 落审计，需要 uvicorn 侧配置 `--proxy-headers`，当前镜像未开启，属已知限制）。

## 4. 数据持久化

- 所有状态都在一个 SQLite 文件：`./data/unified.db`（容器内 `/data/unified.db`），WAL 模式。
- 库里有什么：用户与会话、provider 启停与地址、每 provider 的 api_key/options（`settings` 表）、账号池缓存、网关密钥（SHA-256）、请求日志与 usage 旁路、sub2api 分组/账号/流水、grok 注册任务、审计日志。
- `docker compose down` / 重建容器不丢数据；删掉 `./data` 目录等于完全重置（管理员账号会重新播种，密钥全部作废）。

## 5. 备份与恢复

WAL 模式下直接拷贝单文件可能拿到不一致快照，用 sqlite3 的备份命令（或先停服务再拷）：

```bash
# 在线备份（推荐，宿主机装了 sqlite3 的情况下）
sqlite3 ./data/unified.db ".backup './backup/unified-$(date +%F).db'"

# 或停机冷备
docker compose stop control
cp ./data/unified.db backup/unified.db
docker compose start control
```

恢复 = 停 control，用备份文件覆盖 `./data/unified.db`（如目录里有 `unified.db-wal`、`unified.db-shm` 一并删除），再启动。会话、密钥、日志都在库文件里，恢复即全量还原。

**（2026-11-05 已更正）**

这条手工停机覆盖路径仍然可用，没有被废掉：容器里没有可用恢复 API，或库已经坏到在线快照都出不来时，停 control → 覆盖 `data/unified.db`（连同 `-wal` / `-shm`）→ 启 control，仍是最稳的兜底。但控制面正常运行时的日常恢复请改用 `POST /api/system/restore`（SQLite 在线备份，不停服，自动留恢复前归档，能力边界见 [BACKUP.md](BACKUP.md) 的「恢复：管理界面（HTTP）」）。**这条为什么重要**：以为新实现把手工路径废了，会在没有 API 的环境里卡死；反过来在服务运行中照这条覆盖文件，Windows 上会被打开句柄挡住，Linux 上会留下新旧混杂的 WAL。

## 6. 升级

1. 备份 `./data/unified.db`（见上）。
2. `git pull` 后 `docker compose up -d --build`。
3. 库结构由控制面启动时的 `CREATE TABLE IF NOT EXISTS` + 轻量补列迁移自动处理（`apps/control/app/db.py`），无需手工跑迁移脚本。

## 7. 常见部署故障

| 症状 | 处理 |
| --- | --- |
| 8080 打不开 | `docker compose ps` 看 web 是否起来；`docker compose logs web` 看构建是否失败 |
| 8010 打不开 | 控制面启动失败多为 `./data` 不可写或 8010 被占；详见 TROUBLESHOOTING |
| 页面能开、接口全 502 | nginx 容器到 `control:8000` 不通，control 没起来；`docker compose logs control` |
| 网关一直 401 | 密钥写错/已吊销，或反代剥了 Authorization（见上文①） |
| 上游都「不可达」 | sidecar/上游没启动，或容器内访问宿主机地址不对（`host.docker.internal`），用 `/api/status` 看每家的错误信息 |
