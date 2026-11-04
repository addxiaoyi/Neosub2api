# deploy：自托管编排与上线检查

本文档属于 **Neosub2api（仓库目录仍为 unified-2api）**。展示名用 Neosub2api；机器标识保持 `unified-2api` 不变——镜像名（`unified-2api-control` 等）、compose 服务名、`unified_data` 卷名、`data/` 等目录名都按仓库标识走，不随展示名改。

本目录放容器化部署文件。仓库根 `docker-compose.yml` 是「本机一键起」，这里是有 TLS / 反代 / 资源限制的生产形态。

## 三套编排怎么选

| 文件 | 形态 | 暴露方式 | 适用 |
| --- | --- | --- | --- |
| 根 `docker-compose.yml` | control + web 两服务 | 默认只绑 127.0.0.1:8010 / 8080 | 本机试跑、内网临时使用（无 TLS） |
| `deploy/docker-compose.yml` | control + web 两服务 | 只绑本机，带健康检查 | 本机/内网自托管，前面自行接反代 |
| `deploy/docker-compose.prod.yml` | control + web + nginx | 80/443 | 公网/正式环境，nginx 终止 TLS |

启动前先复制变量模板：

```bash
cp deploy/.env.example deploy/.env
# 至少修改 UNIFIED_ADMIN_PASSWORD、UNIFIED_CORS_ORIGINS；HTTPS 环境保持 UNIFIED_COOKIE_SECURE=true
```

## ⚠️ 反代 + 演示模式：必须在反代层禁止

`UNIFIED_DEMO_MODE=1` 有三层保护，其中**请求级保护依赖 `request.client.host`**：

1. 启动校验：只允许 `UNIFIED_HOST` 是 `localhost` / `127.0.0.0/8` / `::1`，否则进程直接退出。
2. 请求级兜底：非回环来源一律 403（`apps/control/app/main.py` 的 `demo_loopback_only`）。
3. 假上游只绑 `127.0.0.1` 的随机端口。

**风险**：如果演示实例前面挂了**同机反向代理**（nginx 反代到 `127.0.0.1:8010`），控制面看到的
`request.client.host` 会变成 `127.0.0.1`，第 2 层保护直接失效；此时任何能从外部访问反代的人
都能拿到演示数据，等于把假上游、假账单当生产用。

因此：

- **演示实例永远不要接任何反向代理**，也不要放进 `deploy/` 的 compose 或 k8s Service。
- 演示只在本机用 `scripts/demo.ps1` / `scripts/demo.sh` 启动，绑 `127.0.0.1`，用完 `--clear`。
- 如果反代层必须存在（例如统一入口），在反代上**硬拒绝**演示域名/路径，而不是依赖后端判断：

```nginx
# 反代层兜底：演示域名直接 403，绝不转发到后端
server {
  listen 443 ssl;
  server_name demo.example.com;
  ssl_certificate     /etc/nginx/certs/fullchain.pem;
  ssl_certificate_key /etc/nginx/certs/privkey.pem;
  return 403;
}
```

- `allow 127.0.0.1; deny all;` 在反代场景**无效**：来源地址就是反代自己。
- 上线前从**另一台机器**验证：`curl -i https://<你的域名>/healthz` 必须被拒；如果返回 200，
  立刻下线该实例并检查 `UNIFIED_DEMO_MODE`。

容器场景的额外说明：控制面在容器内固定监听 `0.0.0.0:8000`（否则端口映射不可用），
所以把 `UNIFIED_DEMO_MODE=1` 塞进 `deploy/` 的 compose 会在启动校验阶段直接失败——这是**有意的**。
不要为了让它起来把容器内 `UNIFIED_HOST` 改成 `127.0.0.1`：那只是容器内的回环，并不等于对外安全。

## 2026-11-04 第四轮（最新）：Docker 构建与 compose 冒烟复验

这轮改过 `apps/control/Dockerfile`、`.dockerignore`、三份 compose 与 `apps/web/nginx.conf`，
重新证明镜像能起。全部在仓库根执行，未做 git 操作：

| 项目 | 结果 |
| --- | --- |
| `docker compose build` | 通过，`unified-2api-control`、`unified-2api-web` 均 `Built` |
| `docker compose up -d` + `ps` | 两服务 `healthy` |
| 首页 `/` | 容器内经 nginx `HTTP/1.1 200 OK` |
| `nginx -t`（web 镜像内） | `syntax is ok` / `test is successful` |
| `UNIFIED_DEMO_MODE` | 实测 `0`（`docker inspect` 读容器 env，不是看 compose config 推断） |
| `/api/health`、`/api/health/ready` | 均 200（容器内直连 control 与经 nginx 反代各打一遍，`ready` 返回 `unhealthy: []`） |
| 容器日志敏感值 | `u2a_`、`Authorization`、`token` 各 **0 命中**（control 37 行 / web 57 行日志） |
| `docker compose down` | 通过，两容器与 `unified-2api_default` 网络移除，镜像保留 |

### 宿主回环发布限制（Windows + Docker Desktop，本机实测）

Docker Desktop 在 Windows 上只为 `127.0.0.1` 建宿主端口监听。根 compose 的
`UNIFIED_BIND_HOST` 换成别的回环（实测 `127.0.0.2`）时，容器照样 `healthy`、
Docker 日志也打出 `adding … tcp4 forward from 127.0.0.2:8010 → 容器` 且无 `bind` 报错，
但宿主侧没有 listener，`GET` 一律 connection refused。对照实验用同一个 `nginx:alpine` 镜像钉死：

| 发布地址 | 宿主探测 |
| --- | --- |
| `127.0.0.1:18081` | 200 |
| `127.0.0.2:18082` | connection refused，`netstat` 无对应监听 |

结论：**要换宿主地址只能用 `0.0.0.0` 或局域网 IP，而那会把控制面暴露到局域网且没有 TLS，
生产必须走反代**（见下面的「三套编排怎么选」与上线清单第 4 条），别为了绕开端口占用去改它。
本轮宿主 8010 被 `scripts/start.ps1 -Port 8010` 的演示服务占用，故验收落在容器内口径 +
上面的对照实验，演示服务全程未停（收尾复采仍 200）。

### 本地环境说明（本机 Docker 构建）

- **Docker Hub 被 DNS 污染**：`auth.docker.io` 解析到封禁 IP，直连拉取超时。本机已在
  `%USERPROFILE%\.docker\daemon.json` 加 `registry-mirrors: ["https://docker.m.daocloud.io"]`，
  原文件备份在同目录 `daemon.json.bak-u2a-smoke`；撤销方式 = 删掉该键并重启 Docker Desktop。
  这台机器的 `HTTP_PROXY=127.0.0.1:10808` 在 WSL 侧访问返回 502，救不了镜像拉取与构建，别依赖它。
- **`apps/web/.dockerignore` 的效果分两个口径，别相减**：文件集大小 **905.7MB → 6.3MB**
  （排掉 `node_modules` 540.7MB + `.next` 350.4MB，另排 `out`/`coverage`/`.turbo`/`*.tsbuildinfo`，
  源码与 `public/` 保留，镜像内 `next build` 照常跑通）；buildkit 打印的 `transferring context`
  是**增量口径**（加 ignore 后实测 14–17kB，只同步变更文件），改前那个 367MB 也是 buildkit 数字，
  两者不同口径，**不能拿 367MB 减 17kB**，真实体积看 905.7MB → 6.3MB。

## 本次验证状态（2026-11-04，第三轮：大小写与 URL 编码加固）

本轮只动 nginx 配置与本文件：`deploy/nginx.conf`、`apps/web/nginx.conf`、`deploy/web-nginx.conf`
三份的脱敏块按同一份字节重新生成，逐字节一致（各 45357 字节，sha256 前 16 位 `71a9559c73e0df0e`）。

| 改动点 | 改前 | 改后（实测） |
| --- | --- | --- |
| 大小写 | map 正则 `~`（PCRE 区分大小写），`?API_KEY=`、`?Token=`、`?Authorization=` 明文落盘 | 全部改 `~*`；`?API_KEY=`、`?AUTHORIZATION=`、`?Client_Secret=`、`?REFRESH_TOKEN=` 均落 `***` |
| URL 编码 | 只认字面参数名，`?%74oken=`、`?api%5Fkey=` 明文落盘 | 参数名与 `u2a_` 前缀逐字符展开为 `(?:字符\|%HH\|%HH)`（字母给小写/大写两种编码形态，`~*` 再覆盖 `%6b`/`%6B` 这类十六进制字母）；`?%74oken=`、`?api%5Fkey=`、`?x-api%2Dkey=`、`?%75%32%61_xxx=` 均落 `***`；分隔符同时认 `=` 与 `%3d`（`?token%3d***` 已验） |
| `u2a_` 值匹配位置 | 必须紧跟 `[?&]` 或 `参数名=` | 改为「出现在 `?` 之后的 query 内任意位置」，`?a=1&b=u2a_x`、`?u2a_x=y` 与编码形态一并覆盖 |
| 链深 | 10 段参数名 + 10 段 `u2a_` | 20 段 + 20 段 |

链深成本实测（WSL Ubuntu 本机 nginx 1.31.8，keepalive，20000 请求 × 4 连接取 4 次最优，
URI 约 500 字节含 3 个敏感参数，客户端自身开销约 100µs/请求）：

| 链深 | 每请求耗时 | 相对不脱敏的增量 |
| --- | --- | --- |
| 0（无 map） | 100µs | — |
| 10+10（改前） | 243µs | +143µs |
| 16+16 | 418µs | +318µs |
| **20+20（采用）** | **540µs** | **+440µs** |
| 30+30 | 813µs | +713µs |

每加一段约 14µs（同一份大正则在 500 字节 URI 上替换一次）。20+20 把可掩码个数翻倍、代价约
0.44ms/请求，相对 LLM 转发延迟可忽略；再往下成本线性涨、收益递减，故定在 20+20，并写进配置注释。

参数个数边界（链深 20+20 实测，`N` 个敏感参数名挤一条 URL，重复别名也算）：

| N（敏感参数名个数） | 结果 | `u2a_` 值个数 M | 结果 |
| --- | --- | --- | --- |
| 1 / 3 / 5 / 7 / 10 / 15 / 18 / 20 | 全部落 `***` | 1 / 5 / 10 / 15 / 20 | 全部落 `***` |
| 21 | 第 21 个起漏网 | 21 | 第 21 个起漏网 |
| 22 / 25 | 第 21 个起全部漏网 | 25 | 第 21 个起全部漏网 |

真实请求 → 落盘日志对照（`deploy/nginx.conf`，WSL 内 80 端口，`/` 与 `/api/` 各打一遍）：

| 输入 URL（值已换成占位） | 落盘 request 行 |
| --- | --- |
| `/?api_key=SEC_g1_lower` | `GET /?api_key=*** HTTP/1.1` |
| `/?API_KEY=…&Token=…&AUTHORIZATION=…` | `GET /?API_KEY=***&Token=***&AUTHORIZATION=***` |
| `/?%74oken=…&api%5Fkey=…&x-api%2Dkey=…` | `GET /?%74oken=***&api%5Fkey=***&x-api%2Dkey=***` |
| 9 参数混杂（`Client_Secret`、`u2a_g`、`%75%32%61_h`、`Session`、`password` 等） | `GET /?foo=1&API_KEY=***&%74oken=***&Client_Secret=***&***&***&Session=***&x-api-key=***&password=***` |
| `/?token%3dSEC_g5_eq` | `GET /?token%3d***` |
| `/?x=SEC_g7_control_plain&safe=1`（非敏感对照） | 原样保留，未被误掩 |

验证口径：20 个敏感值对全量 `access.log` 逐个 `grep`，**0 命中**；`error.log` 同样 0 命中；
`apps/web/nginx.conf` 单独起一遍重复同样 7 组 URL，也是 0 命中；
`nginx -t` 三份配置分别 `syntax is ok`。测试实例已全部停止，未执行 git 操作。

仍存在的边界（都不构成可登录的绕过，但日志会保留原文，属已知取舍）：

- 只解一层编码：`%2574`（双重编码）不覆盖——上游也只解一层，解出来不是参数名，登录用不上；
- `&` 被编码成 `%26` 后在值内「伪造」的 `token=…` 不掩码——后端 `parse_qsl` 语义下它不是独立参数；
- `;` 仍不作参数分隔符（整段按值掩码，偏保守）；重复参数上限 20，超出按上表追加同款 map 段即可加深；
- `error_log` 必须保持 `crit`，排障期间不要用带密钥的 URL 打请求。

评估过但**未采用**：只记 path + 白名单参数。改法只有一行 `log_format`，能一次性消掉全部正则绕过面；
代价是排障损失——上游 4xx/5xx 时看不到请求实际带的 query（`?model=`、`?limit=`、`?format=`、
`?debug=` 这类非敏感参数，对照组实测确实仍原样保留且有用），投诉对账与复现请求都失去依据。
结论：不切换。当前正则口径已覆盖真实绕过面（大小写 + 单层编码），剩余边界均有已知语义解释；
若日后出现新的参数名绕过再重新评估，属产品取舍。

环境说明：本轮在 WSL Ubuntu 的本机 nginx（nginx.org 官方源 1.31.8——系统自带 1.24 不认
`http2 on;`）上完成，端口用 WSL 内 80/443 与 18401（宿主机 8080 段未碰，Docker Desktop
引擎本机未就绪，故未起容器）。

## 上一轮验证状态（2026-11-03，第二轮：镜像裸跑 + 脱敏补全；2026-11-04 第三轮已复核并加深深度）

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| `docker compose config`（根 / deploy / prod） | 通过 | 三份文件均可解析，变量插值无报错 |
| 镜像裸跑 `unified-2api-control` | 通过 | 不挂任何源码、不覆盖 `working_dir`/`command`，`docker run` 直接起来，无 `IndexError`，`/healthz` 200 |
| `docker compose up -d --build` | 通过 | `docker ps`：control、web 均 `healthy`；根 compose 只剩 `./data:/data` 一条挂载 |
| 控制面 `/api/health` | 200 | 直连 8010 与经 web 反代都是 200 |
| 控制面 `/api/health/ready` | 200 | `unhealthy: []`（`opencode` enabled+ready）。中途出现过一次 503，原因是宿主机其他进程正占着 `data/unified.db`（并行会话的 uvicorn 在跑同一份数据），锁一放开即恢复，与镜像改动无关 |
| web 首页 `/` | 200 | 返回真实页面（`WorkBuddy Manager` 标题），不是默认欢迎页 |
| `nginx -t`（web 容器内） | 通过 | `docker exec unified-2api-web-1 nginx -t` |
| `nginx -t`（三份配置分别跑） | 通过 | `deploy/nginx.conf` 用临时自签证书 + `--add-host control/web`；`apps/web/nginx.conf`、`deploy/web-nginx.conf` 同法（`--add-host control`） |
| 访问日志脱敏（实流） | 通过 | `?api_key=***&foo=***&token=***&safe=keep`；18 个参数名别名与裸 `?u2a_xxx` 全部落 `***`，Referer 只留 origin+path |
| 本机端口 | 8010 空闲；8080 被其他服务占用 | 本轮把 web 临时映射到 18080（override 文件放 `%TEMP%`，未落仓库） |
| `scripts/start.sh`（Linux） | 通过 | `python:3.12-slim` 容器内实跑，`/healthz` 返回 200（首轮） |
| `scripts/start.sh`（Git Bash） | **不适用** | Windows 版 Python 不认 POSIX 路径；Windows 请用 `start.ps1` |
| `scripts/start.ps1`（Windows） | 通过 | 使用仓库内 `.runtime\python\python.exe`，`/healthz` 返回 200（首轮） |
| 真实 TLS 证书 / 真实上游 / 公网暴露 | **未验证** | `deploy/certs/` 目前是空目录，真实证书**尚未签发**（`nginx -t` 用的是临时自签证书，测试完即弃）；本机也没有域名与真实上游，需在目标环境执行上线清单 |

### 两个根因已修（2026-11-03）

1. `apps/control/Dockerfile` 原来是 `WORKDIR /app` + `COPY app ./app`，应用落在 `/app/app/config.py`，
   而 `app/config.py` 的 `Path(__file__).resolve().parents[3]` 要的是 `<仓库根>/apps/control/app/config.py`
   → 启动即 `IndexError`。现改为 `WORKDIR /repo/apps/control` + `COPY apps/control/app ./app`，
   并补 `COPY config /repo/config`、`COPY CHANGELOG.md /repo/CHANGELOG.md`（`pricing.py` 的
   `config/prices.yaml`、`system.py` 的 `CHANGELOG.md` 都按仓库根路径读，缺了会 404 / 显示 unavailable）。
   三份 compose 的 control 构建 context 随之改成仓库根（`dockerfile: apps/control/Dockerfile`），
   根目录新增 `.dockerignore` 挡住 `node_modules`/`.git`/`docs`/`apps/web` 等大目录。
2. `apps/web/nginx.conf` 的 server 块补 `root /usr/share/nginx/html;` 与 `index index.html;`，
   并按 `deploy/nginx.conf` 的口径补齐查询串脱敏（map 链 + `log_format u2a_redacted` + `error_log crit`）。

三份 compose 里的绕过项已全部删除：源码挂载 `apps/control`、`config`，`working_dir`，`command` 覆盖，
以及 `web-nginx.conf` 挂载。控制面只剩数据目录挂载（根 compose 为 `./data`、`deploy/docker-compose.yml`
为 `../data`——同一份仓库根 `data/`；prod 为 `unified_data` 卷）。
`deploy/web-nginx.conf` 保留但不再被任何 compose 挂载，只作 `nginx -t` 冒烟对象。

### 复跑 `nginx -t`（无证书环境）

Windows PowerShell（Git Bash 的 openssl）：

```powershell
$tmp = Join-Path $env:TEMP "u2a-nginx-test"
New-Item -ItemType Directory -Force "$tmp\certs" | Out-Null
& "C:\Program Files\Git\usr\bin\openssl.exe" req -x509 -newkey rsa:2048 -nodes `
  -keyout "$tmp\certs\privkey.pem" -out "$tmp\certs\fullchain.pem" -days 1 -subj "/CN=localhost"
docker run --rm --add-host control:127.0.0.1 --add-host web:127.0.0.1 `
  -v "${PWD}\deploy\nginx.conf:/etc/nginx/conf.d/default.conf:ro" `
  -v "${tmp}\certs:/etc/nginx/certs:ro" nginx:stable-alpine nginx -t
```

Linux/macOS 把 `openssl` 换成系统自带即可。证书就位后，在 compose 网络内复跑：

```bash
docker compose -f deploy/docker-compose.prod.yml run --rm nginx nginx -t
```

## 上线前必做清单

1. `deploy/.env` 按 `deploy/.env.example` 配好：强口令、`UNIFIED_COOKIE_SECURE=true`、
   `UNIFIED_CORS_ORIGINS` 只列真实前端域名。
2. 证书放到 `deploy/certs/fullchain.pem` 与 `privkey.pem`（权限收紧），再跑上面的 `nginx -t`。
3. 确认 `UNIFIED_DEMO_MODE` 未设置（或为 0），且演示实例没有接任何反代。
4. 确认端口只暴露给反代：control 的 `127.0.0.1:8010` 不要对公网开放；防火墙只放 80/443。
5. 用真实密钥发一次 `/v1` 请求，检查 `docker compose logs control` 与 nginx `access.log` 中
   `api_key` / `u2a_` 已变成 `***`（`deploy/nginx.conf` 与 `apps/web/nginx.conf` 的 map 链
   只作用于 nginx 日志，不改转发行为）。
6. 确认 `docker compose ps` 两个服务都是 `healthy`；`curl http://127.0.0.1:8010/healthz` 返回 200。
7. 验证重启策略：`docker compose restart` 后服务能自动恢复；`data/`（prod 为 `unified_data` 卷）不丢。
8. 容器内静态站反代（`apps/web/nginx.conf`，`deploy/web-nginx.conf` 与其内容一致）
   已按 `deploy/nginx.conf` 的口径脱敏：18 个参数名别名（含 `client_secret`、`access_key`、
   `authorization`、`api-key`、`x-api-key`、`auth`、`private_key`、`refresh`）+ 任意 `u2a_`
   值与裸参数，正则大小写不敏感（`~*`）且覆盖单层百分号编码（`%5F`/`%2D`/字母、分隔符含 `%3d`），
   map 链深度 20+20（2026-11-04 由 10+10 加深，成本与个数边界见上一节），
   Referer 只留 origin+path，`error_log` 同样压到 `crit`。
   生产入口仍以本目录的 `nginx.conf` 为准（它才是 TLS 终结那一跳）。
9. `deploy/nginx.conf` 把 `error_log` 压到 `crit`：nginx 的错误日志不受 `log_format` 控制，
   `error` 级会原样打印 request line（含 query），只能靠提级屏蔽。排障时临时改回 `error`/`warn`，
   排完改回 `crit`，且排障期间别用带密钥的 URL 打请求。

## 常用命令

```bash
# 本机一键起（仓库根）
docker compose up -d --build
docker compose ps
docker compose logs -f control

# 本机两服务（deploy）
docker compose -f deploy/docker-compose.yml up -d --build

# 生产（nginx 入口）
docker compose -f deploy/docker-compose.prod.yml up -d --build

# 健康检查脚本（按实际入口传 base URL）
bash deploy/healthcheck.sh http://127.0.0.1:8010
powershell -File deploy\healthcheck.ps1 -BaseUrl http://127.0.0.1:8010
```
