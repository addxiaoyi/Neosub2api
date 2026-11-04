# 演示模式（UNIFIED_DEMO_MODE）

## 结论

一个开关就能让空库变成「有东西可看」：控制面启动时在本进程内拉起仓内的假上游
`apps/control/scripts/fake_upstream.py`，注册成 id 为 `demo_local` 的 provider，并通过**真实网关链路**
播种一批带 `[demo]` 标记的数据。

- 默认关闭，**关掉时行为和改动前完全一致**（不注册 provider、不写库、不启动任何额外进程）。
- 演示数据与真实数据靠显式标记隔离，一条命令清除。
- **只能绑 127.0.0.1**：检测到非回环地址直接启动失败；另有请求级兜底拒绝非本机来源。

## 怎么开

```powershell
# Windows（默认 http://127.0.0.1:8010）
powershell -File scripts\demo.ps1

# 换端口
powershell -File scripts\demo.ps1 -Port 18150
```

```bash
# Linux / macOS / Git Bash
bash scripts/demo.sh
PORT=18150 bash scripts/demo.sh
```

脚本会：建库（幂等）、强制 `UNIFIED_DEMO_MODE=1` 与 `UNIFIED_HOST=127.0.0.1`、打印页面地址、
健康探针、admin 口令、演示密钥文件位置，并醒目提示「这是演示数据，不要用于生产」。

也可以手动开：

```powershell
$env:UNIFIED_DEMO_MODE = "1"
$env:UNIFIED_HOST = "127.0.0.1"     # 必须是回环地址，否则启动失败
$env:UNIFIED_DATA_DIR = "data\demo" # 建议单独目录，避免和真实库混住
python -m uvicorn --app-dir apps\control app.main:app --host 127.0.0.1 --port 8010
```

> 注意：开启演示模式会**临时停用**「默认启用但零配置」的 provider（首次建库的 codebuddy），
> 否则 `/v1/models` 和首次对话要先等它的连接超时。被停用的 id 记在 `demo_disabled_providers`
> 设置项里，清除演示数据时自动恢复。

## 能看到什么

| 位置 | 内容 |
| --- | --- |
| 库表 providers | 多出一行 `demo_local`，label 为「演示假上游（demo，仅本机）」，`readiness=ready` |
| `/api/health/ready` | 200，`checks.providers.items` 里 `demo_local.available=true`；假上游挂了会变 `needs_config` |
| `/v1/models` | `fake-chat-1`、`fake-chat-2`（从假上游实拉，`owned_by=demo_local`） |
| `/v1/chat/completions` | 真实经过网关，上游返回 `[fake] ...` 固定内容，`usage` 由假上游给出 |
| `/api/logs` | 6 条带演示密钥归属的请求日志（5 条 200 + 1 条真实 429） |
| `/api/keys` | 2 把密钥：`[demo] 演示密钥 A`（启用）、`[demo] 演示密钥 B（已停用）` |
| `/api/accounts` | 2 个 `demo_account_*.json` 账号，`credits` 留空（不编造余额） |
| `/api/stats/summary` | 真实的请求数/token 数/金额（金额由演示价格算出） |
| 配额 | 2 条策略挂在演示密钥 A 上（日 200 次 / $5、月 $20），含 80%/100% 阈值 |
| 文件 | `data/demo-credentials.txt`：演示密钥明文，权限 0600，文件头写明禁止生产使用 |

演示密钥也会以明文存在 settings 表的 `demo_seed` 里，方便脚本重复打印；该行随清除一起删除。

### 为什么说「数据是真的」

- 日志、用量、流水不是手填的：启动时由 `DemoAdapter` 通过 `UnifiedApiClient` 真的发 HTTP 到假上游，
  延迟、`usage`、状态码都是这次调用实际测到的；其中 1 条 429 是假上游对 `fake-chat-err-429`
  真实返回的错误，用来展示失败也会入库。
- provider 走的是与真实上游同一个适配器基类和网关路由，没有「demo 就直接返回假 JSON」的分支。
  假上游的协议实现只有一份：`scripts/fake_upstream.py`，演示模式用 importlib 加载它的 FastAPI app，
  在独立线程里用 uvicorn 起在 `127.0.0.1` 的随机端口上。

## 演示数据清单与标记

| 表 | 标记 |
| --- | --- |
| `providers` | `id = demo_local` |
| `request_logs` / `usage_records` / `billing_ledger` / `price_book_snapshots` | `provider = demo_local` |
| `provider_credentials` / `price_books` | `provider = demo_local`，note 以 `[demo]` 开头 |
| `accounts` | `provider = demo_local`（文件名 `demo_*.json`） |
| `api_keys` | `name` 以 `[demo] ` 开头 |
| `quota_policies` / `quota_alert_thresholds` | 归属于 `[demo]` 密钥的策略 |
| `settings` | `demo_seed`、`demo_disabled_providers` |

**唯一会触及非 demo 行的动作**是「临时停用零配置 provider」及其恢复，id 清单落在
`demo_disabled_providers` 里，清除时恢复为启用。

## 怎么清

```powershell
powershell -File scripts\demo.ps1 -Clear
```

```bash
bash scripts/demo.sh --clear
```

```powershell
# 等价的手工方式（apps/control 目录下）
python -c "import sys; sys.path.insert(0, '.'); from app.config import load_settings; from app.db import Database; from app.demo import clear_demo_data; s = load_settings(); print(clear_demo_data(Database(s.db_path), s))"
```

清除动作：删 `demo_local` 的 provider/凭据/价格/账号/日志/用量/流水/快照，删 `[demo]` 密钥与其配额策略，
删两个 demo 设置项，恢复被停用的 provider，删除 `demo-credentials.txt`。返回的是各表删除行数（0 行的不列）。

可以重复执行；关掉开关后执行同样有效。

## 回环地址保护怎么实现

三层，前两层防「把 demo 开到公网」，第三层是防误用的兜底：

1. **启动即校验**（`app/demo.py: ensure_loopback_bind`）
   解析 `UNIFIED_HOST`：只有 `localhost`、`127.0.0.0/8`、`::1` 放行。空串、`0.0.0.0`、`::`、局域网 IP、
   域名一律抛 `DemoModeError` 并中止进程，报错里写明「只允许绑定回环地址」和实际检测到的值。
   由 `create_app()` 在启动时执行，所以 `uvicorn` 导入失败即退出（非 0 退出码）。
2. **请求级兜底**（`app/main.py` 里的 `demo_loopback_only` 中间件）
   即使有人用 CLI 参数把 host 改成 `0.0.0.0`（绕过环境变量的校验），只要来源不在回环网段就返回 403。
   该中间件只在演示模式注册，关闭开关时不加。
3. **假上游也只绑回环**
   `DemoUpstream` 在 `127.0.0.1` 上 `bind(0)` 取随机端口，监听地址不可配置，不存在对外暴露路径。

## 验证（实测）

开启（全新库，`UNIFIED_DEMO_MODE=1`）：

```
GET /api/health/ready -> 200
  checks.providers: enabled=1 available=1 needs_config=0
  items[demo_local]: enabled=True readiness=ready available=True
GET /v1/models (Bearer [demo] 密钥) -> 200
  {"data":[{"id":"fake-chat-1","owned_by":"demo_local"},{"id":"fake-chat-2","owned_by":"demo_local"}]}
POST /v1/chat/completions {"model":"fake-chat-1",...} -> 200
  choices[0].message.content = "[fake] model=fake-chat-1 system=''"
  usage = {"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}
GET /api/logs -> total=7（6 条播种 + 1 条刚发的请求），provider 全部 demo_local
GET /api/stats/summary -> total_requests=7, today_credit>0
```

关闭（另一个全新库，未设置 `UNIFIED_DEMO_MODE`）：

```
GET /api/health/ready -> 503（仅有 needs_config 的默认 provider，没有 demo_local 条目）
GET /v1/models -> 401（库里没有任何密钥）
GET /api/logs -> total=0；GET /api/keys -> []；GET /api/stats/summary -> total_requests=0
无 demo-credentials.txt；providers 表无 demo_ 开头的行
```

清除（在开启过演示的库上执行）：

```
{'quota_alert_thresholds': 4, 'quota_policies': 2, 'request_logs': 7, 'usage_records': 7,
 'billing_ledger': 6, 'price_book_snapshots': 6, 'accounts': 2, 'provider_credentials': 1,
 'price_books': 4, 'api_keys': 2, 'providers': 1, 'settings': 2, 'providers_restored': 1,
 'credentials_file': 1}
清除后各表 demo 行数均为 0，codebuddy 的 enabled 恢复为 1
```

测试：

```powershell
cd apps\control
$env:UNIFIED_TEST_CTRL_PORT = "18121"
$env:UNIFIED_TEST_FAST_PORT = "18122"
# 解释器用仓库根 `.runtime\python\python.exe`（按本机环境选择，不写死盘符）
python -m pytest tests -q
```

演示相关用例在 `apps/control/tests/test_demo_mode.py`：覆盖开启态有数据、关闭态无痕迹、清除只删 demo 行、
非回环地址拒绝启动、回环判定函数。

## 为什么不能用于生产

- 上游是假服务：响应内容固定，`fake-chat-*` 不代表任何真实模型能力；把它当生产上游等于对外说谎。
- 演示密钥会以明文落在 `data/demo-credentials.txt` 与 settings 表；真实环境不该有这种文件。
- 演示价格（$1/M input、$2/M output）只是为了让流水有金额，**不是任何厂商的报价**，据此计费会算错账。
- 演示模式会临时停用零配置 provider，并把启用状态记在设置项里；生产环境的开关状态不应被工具改写。
- 演示日志/用量会与真实账单混在同一张表（虽然带 `demo_local` 标记），统计口径需要人工剔除。

## 风险与防护边界

| 风险 | 现有防护 | 边界 / 做不到的事 |
| --- | --- | --- |
| 有人把 demo 开到公网 | 启动校验非回环即失败；请求级 403 兜底；假上游只绑 127.0.0.1 | 若前面挂了**同机反向代理**，远端请求的来源地址会显示为 127.0.0.1，请求级兜底失效；只能靠启动校验和不要这么做 |
| CLI 覆盖 host 绕过环境变量 | 请求级中间件按来源地址拒绝 | 同上，同机代理场景无法区分 |
| 演示数据混进真实库 | `provider=demo_local`、`[demo]` 前缀、`demo_seed` 清单，清除按标记删 | 如果真实请求也打了 `demo_local`（演示期间调用网关会这样），清除时一并删除——这是有意的，演示 provider 的流量都算演示数据 |
| 演示密钥泄漏 | 文件 0600、只在回环监听、明文仅存在于本机 | 本机其他用户/进程仍可读；演示密钥没有轮换机制，用完即清 |
| 假上游占用资源 | 独立守护线程、随机端口、退出时 `should_exit` + join | 进程被强杀时线程随进程结束，无残留端口占用 |
| 播种失败导致页面空 | 播种异常只告警，不影响启动；密钥/账号仍可见 | 日志/流水可能为空，需重跑（先 `-Clear` 再启动） |

最后一条建议：演示环境请用独立数据目录（`UNIFIED_DATA_DIR=data\demo`），演示完 `-Clear`，
不要把演示库直接当生产库继续用。
