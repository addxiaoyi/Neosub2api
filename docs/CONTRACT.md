# 前后端契约检查

`scripts/contract_check.py` 用后端真实路由表去核对前端调用点，专治「前端调用了后端不存在的路径」。
历史上的 27 条幽灵端点就是靠人肉审计漏掉的，这个脚本把这件事变成一条命令。

## 用法

```powershell
# 仓库根目录；用项目自带的运行时解释器（路径按本机环境，不写死盘符）
.runtime\python\python.exe scripts\contract_check.py

# CI / 脚本消费
... scripts\contract_check.py --json

# 有 error 级问题（幽灵端点 / 方法不匹配 / 字段不存在）时以退出码 1 结束
... scripts\contract_check.py --fail-on-ghost
```

退出码：`1` = 存在 **error 级**问题（三类：幽灵端点、方法不匹配、明确字段不存在）；`0` = 无 error 级问题——warn 与「后端零调用」提示都不影响退出码。
`--fail-on-ghost` 旗标语义**变严**：此前该旗标下只拦幽灵端点，现在旗标下三类 error 全部导致失败，不再只是「仅幽灵端点失败」。

## 提取方式

### 后端

导入 `app.main:app` 后直接读 `app.routes`，每条 `APIRoute` 自带 `path` 与 `methods`，因此方法是一手信息，
不依赖源码解析。以下路由不入表：

- `@app.mount("/", StaticFiles(...))` 这类 `Mount`（没有 `methods`）；
- 非 `/api`、`/v1` 前缀的路径，以及 `/healthz` 之外的非接口路径；
- 自动追加的 `HEAD` / `OPTIONS`（只比较业务方法）。

导入会把 `create_app()` 的副作用（建临时库、写管理员口令文件）引到系统临时目录，
跑完即删，不碰仓库里的 `data/`，也不会挂载 `apps/web/out`。

### 前端

只扫 `apps/web` 下的 `app/`、`lib/`、`components/`、`hooks/` 四个目录（`ts/tsx/js/jsx/mjs`），
识别三类调用点：

- `fetch(...)`：未写 `method` 时按 `GET`；
- `http.get/post/put/patch/delete(...)`：直接映射方法；
- 文件内自己声明的 `get/post/patch/put/del` 封装（如 `lib/api.ts`、`lib/quota-api.ts`）：按封装名映射。

扫描前先把注释替换成等长空格，因此注释里的 `fetch('/api/...')` 不会被误抓，行号仍然准确。
模块级 `const ENDPOINT = '/api/...'` 会参与解析，`` `${ENDPOINT}/export` `` 能还原成真实路径。

另外还会收集没有方法信息的路径字面量（例如协议表里的 `path: '/v1/responses'`）。它们只用于给
「后端零调用」提示降噪，**不参与**幽灵端点和方法不匹配的判定——因为拿不到方法，也不确定运行时是否真会请求。

### 字段对账（query / body）

- 后端 query 契约从 FastAPI 路由对象的 `dependant.query_params` 加源码里的 `query_params['x']` 提取。
- body 只对 Pydantic 模型做字段对账；`extra="allow"`、裸 dict 体、`Request` 裸体、`/v1/*` 裸体按约定跳过（这些形态无法静态枚举字段）。
- 前端侧能识别对象字面量、同文件就近声明、跨文件 `type`/`interface` 别名与 `Omit<...>`，识别不了的按 warn 处理（见下节）。

## 路径归一化

| 前端 / 后端写法 | 归一化后 |
| --- | --- |
| `/api/keys/{key_id}` | `/api/keys/{}` |
| `/api/keys/12` | `/api/keys/{}` |
| `` `/api/keys/${id}` `` | `/api/keys/{}` |
| `/api/pricing/import?dry_run=true` | `/api/pricing/import` |
| `/api/accounts/` | `/api/accounts` |

规则：去掉 query/fragment 与重复斜杠；把 `{...}`、`${...}`、纯数字段都折叠成 `{}`；
再去掉末尾斜杠。这样参数名不同、实参是字面量还是模板，都能对上同一条后端路由。

## 三类结果怎么判定

脚本输出分三级，分歧点是「解析不确定时宁可漏报还是制造假阳性」：

| 级别 | 判定 | 典型例子 | 对退出码 |
| --- | --- | --- | --- |
| **error** | 可静态确证的不一致 | 幽灵端点（归一化路径不在后端路由表）；方法不匹配（前端 `POST`、后端只有 `GET`）；明确字段不存在（前端 query 参数名或 POST body 字段在后端契约里没有） | 退出码 1 |
| **warn** | 走显式白名单、或前后端无法静态枚举 | 前端把参数声明成 `Record<string, unknown>` 之类无法展开的类型；解析不出后端契约（此时一律降为 warn，不硬报 error） | 不影响退出码 |
| **提示** | 后端有路由但前端零调用 | 后端路径既没被任何调用点命中，也没被路径字面量引用 | 不影响退出码 |

warn 的设计意图：解析不稳时**宁可漏报，让人事后补注解，也不制造假阳性**——如果告警里混进猜出来的错，人被训练成忽略告警，工具就废了。解析不出后端契约一律降 warn，同一个道理。

`--fail-on-ghost` 旗标语义**变严**：此前只拦幽灵端点，现在该旗标下三类 error 全部失败。

## 当前基线

最近一次实跑（2026-11-04，数字会随前后端改动变化）：

```
契约检查：error 0 / warn 1
[后端有路由但前端零调用] 12（仅提示）
```

- 唯一 1 条 warn：`apps/web/lib/api.ts:242` 的 `list: (params: Record<string, unknown>)` 无法静态枚举参数名，属**待前端补注解**，不是缺陷。

12 条提示都是前端本来就不该调的：健康/指标探针（`/api/health`、`/api/health/ready`、`/api/metrics`、`/healthz`）、
grok 注册任务（外部执行器调用）、`/api/accounts/sync`、`/api/security/audit`（`/api/audit-logs` 的别名）、
`/api/settings/secret/reveal`。playground 的三条 `/v1/*` 是通过 `path` 字段动态调用的，由字面量参考清单兜住，
不会误报成「零调用」。

## 误报风险与收敛

- **动态拼接**：路径来自变量、函数返回值或跨文件常量时无法静态解析，脚本会跳过（宁可漏报，不误报）。
  这类页面通常会把路径放在配置表里，字面量参考清单能兜住「零调用」提示，但幽灵/方法判定仍需静态字面量。
  新增调用尽量把路径写成字面量，或收敛成模块级常量。
- **封装识别**：裸 `get/post/...` 只在该文件里声明了同名封装时才当调用点，避免把 `map.get(...)` 当接口。
- **注释与静态资源**：注释先被抹掉；只保留 `/api`、`/v1` 开头的路径，favicon、页面跳转不会被误判。
- **方法缺省**：`fetch` 没写 `method` 时按 `GET`，若实际运行时才拼 options，方法比对可能失真。
- **不要白名单**：真出现无法归一的路径，优先改前端把路径集中到 `lib/api.ts`，而不是往工具里加例外，
  否则工具会慢慢退化成噪音源。

## 接入 CI

`apps/control` 的依赖已经由现有 backend job 安装，在 `.github/workflows/ci.yml` 的 backend job 末尾加一步即可：

```yaml
      - name: Contract check
        working-directory: apps/control
        run: python ../../scripts/contract_check.py --fail-on-ghost
```

`tests/test_contract_openapi.py` 已经随 `pytest tests/ -v` 跑，负责守住 OpenAPI 生成、三条 `/v1/*` 网关路由，
以及 `/api/health`、`/api/health/ready`、`/api/metrics`、`/api/accounts/capabilities`、
`/api/settings/runtime-config/*`、`/api/models` 这些近期新增端点不被重构弄丢。

建议节奏：

- PR：`--fail-on-ghost` 必跑，拦住三类 error（幽灵端点、方法不匹配、字段不存在）；
- 主干：额外用 `--json` 落一份报告，观察「后端零调用」清单是否持续膨胀；
- 本地改前端页面后先跑一次，比等 CI 报错快。
