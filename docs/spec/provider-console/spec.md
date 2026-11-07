# 服务商管理端：从只读快照到可用管理面

## 目标

7 家（codebuddy / muse / gptgrok / zcode / trae / qoder / codar）的 `provider_console` 在现有只读快照之上，补齐两类能力：

1. **查看类**：账号清单、请求记录均支持分页、条件筛选、CSV 导出。
2. **运维类**：显式触发 + 二次确认的动作（探活、重连/重置会话、清冷却、任务重跑、取消任务），每次落审计。

## 硬边界（不可越过）

- **不碰** `apps/control/app/adapters/**`（含 `qoder.py`、`key_pool.py`）。适配器只读调用，不改一行。
- 无后台自动动作：所有上游外呼只发生在用户点击按钮后；打开面板仍然只读本地。
- 骨架不动：导航 11 项、设置页 8 个 tab 顺序、品牌、浮动按钮不动。
- 五语文案同步（zh-CN / zh-TW / en / ja / ko），`python scripts/check_i18n.py --strict` 必须过。
- 改动留工作区，不 `git add` / `commit` / `push`。

## 现状基线

- 后端：`apps/control/app/routers/provider_console.py`（快照、动作、媒体清单、codebuddy 任务台账）、`apps/control/app/services/provider_console.py`（`CONSOLE_PROVIDERS`、`ACTIONS`、`usage`、`recent_logs`、`account_stats`、`rotation_groups`）。
- 前端：`apps/web/components/common/settings/ProviderConsolePanel.tsx`（5 个子 tab：overview / connections / runtime / tasks / usage）、`apps/web/lib/provider-console.ts`、`apps/web/lib/api.ts` 的 `providerConsoleApi`。
- 鉴权口径：`owner_scope(user)` → admin 看全站，其他角色只看自己名下 `api_keys` 产生的日志 / 自己名下账号。

## 后端设计

### 1. `apps/control/app/services/provider_console.py`

新增：

- `MAX_PAGE_SIZE = 100`、`DEFAULT_PAGE_SIZE = 20`。
- `def accounts_page(conn, pid, owner, *, page=1, size=20, status="all", q="") -> dict`
  - 列：`file, uid, nickname, enterprise_id, provider, realm, expires_at, credits, note, disabled, created_at`。
  - 过滤：`status ∈ {all, active, expired, disabled}`，`q` 匹配 `uid / nickname / file`（`LIKE`，trim + 截断 64 字符）。
  - 排序 `created_at DESC, id DESC`；返回 `{items, total, page, size, pages}`。
  - `owner` 非空时强制 `owner_user_id = ?`。
  - 每行复用 `api_utils.public_account(row)` 后只保留白名单字段，并附加 `is_expired`；`file` 只取 `basename`（不泄露绝对路径）。
- `def logs_page(conn, pid, owner, *, page=1, size=20, status="all", model="", q="", window="7d") -> dict`
  - 过滤：`status ∈ {all, ok, failed}`，`model` 精确匹配，`q` 匹配 `model / error`，`window` 复用 `WINDOW_SECONDS`。
  - 返回结构同 `accounts_page`，行结构同 `recent_logs` 的单条（`error` 走 `clean`）。
- `def csv_text(rows: list[dict], columns: list[tuple[str, str]]) -> str`
  - `csv.writer` + `io.StringIO`，行首补 `\ufeff`（Excel 中文兼容），列标题由调用方提供。
- `async def clear_cooldown(adapter, credential: str = "") -> dict`
  - 用 `rotation_groups(adapter)` 取 pool/keys；`credential` 空则全部，否则按 `rotation.fingerprint(key)` 精确匹配（也接受前缀）。
  - 重置方法按序取 `report_success` → `note_success`；都没有则抛 `ProviderError`（400）。
  - 返回 `{cleared: n, keys: [fingerprint...], pool: "..."}`；不返回明文 key。
- `ACTIONS` 调整为：
  - `codebuddy: ("probe", "run_task")`
  - `muse: ("probe", "keepalive", "clear_cooldown")`
  - `gptgrok: ("probe", "cancel_task")`
  - `zcode: ("probe", "claim", "clear_cooldown")`
  - `trae: ("probe", "clear_cooldown")`
  - `qoder: ("probe", "clear_cooldown", "reset_sessions")`
  - `qoder_sidecar / codar_sidecar: ("probe",)`
  - `codar: ("probe", "clear_cooldown")`
  - gptgrok（`KeyPool` 无 reset 方法）与 codebuddy（无凭据池）不挂 `clear_cooldown`。

### 2. `apps/control/app/routers/provider_console.py`

`ActionIn` 增加 `credential: str = ""`。

新增路由（全部复用 `_public` / `console.clean` / `owner_scope` 口径）：

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/providers/{pid}/console/accounts` | 登录用户（非 admin 仅自己名下） | `page,size,status,q` |
| GET | `/api/providers/{pid}/console/accounts/export` | 同上 | `status,q`，CSV |
| GET | `/api/providers/{pid}/console/logs` | 同上 | `page,size,status,model,q,window` |
| GET | `/api/providers/{pid}/console/logs/export` | 同上 | 同上，CSV |
| GET | `/api/providers/codebuddy/tasks/export` | admin | 任务运行台账 CSV |

CSV 响应：`text/csv; charset=utf-8`，`Content-Disposition: attachment; filename="{pid}-{kind}-{yyyymmdd}.csv"`，内容为 `csv_text`，不含凭据、不含 URL/内网地址（沿用 `console.clean`）。

动作扩展（`POST /{pid}/console/actions`，admin）：

- `clear_cooldown`：读 `body.credential`，调用 `console.clear_cooldown(adapter, credential)`；审计 detail 记 `cleared=n`。
- `reset_sessions`（仅 qoder）：遍历 `adapter._sessions` 的 key，逐个调用 `adapter._invalidate_session(key)`（方法不存在则 400）；返回 `{cleared: n}`。不触碰上游账号状态，只清本地缓存。
- `retry_task` 不做：媒体任务 runner 是进程内的，跨请求已失效，重跑必然 `runner_unavailable`；codebuddy 用既有 `run_task` 重跑（UI 二次确认）。

### 3. 校验细节

- `page < 1` → 归一到 1；`size` 限制在 `1..100`。
- 未知 `status` 值 → 400（`该筛选值不支持`）。
- 未知 provider / 未知动作沿用现有 404 / 400 文案。

## 前端设计

### 1. `apps/web/lib/provider-console.ts`

新增类型：
- `ConsolePage<T> = {items: T[]; total: number; page: number; size: number; pages: number}`
- `ConsoleAccountRow`、`ConsoleLogRow`、`ConsoleQuery = {page?, size?, status?, model?, q?, window?}`

### 2. `apps/web/lib/api.ts`

`providerConsoleApi` 增加：
- `accounts(id, query)` / `logs(id, query)`
- `exportCsv(id, kind: 'accounts'|'logs'|'task-runs', query)` → `http.get(url, {params, responseType: 'blob'})` 返回 `Blob`

### 3. `apps/web/components/common/settings/ProviderConsolePanel.tsx`

- 子 tab 新增 `accounts`（顺序：overview → connections → accounts → runtime → tasks → usage），其余 tab 内容不动。
- `accounts` tab：状态筛选（全部/有效/过期/停用）、关键字搜索、分页（上一页/下一页 + 页码）、导出按钮、空态。
- `usage` tab：`recent logs` 改为分页 + 状态/模型/关键字筛选 + 导出；顶部统计与柱状图保持原样。
- `connections` tab：凭据轮转表每行加「清冷却」（仅 `actions.has('clear_cooldown')`），走二次确认。
- 所有运维动作（`probe` / `keepalive` / `claim` / `run_task` / `cancel_task` / `clear_cooldown` / `reset_sessions`）统一经 `confirmAction()` 弹确认；取消则不请求。
- 确认框必须**写清对象与后果**，不能只写「确定要执行吗」：
  - `clear_cooldown`：写明所属 provider、凭据指纹（如 `a1b2c3d4`）、当前状态（冷却剩余 12s / 熔断 / 连续失败 3 次）；后果写明「清零冷却与连续失败计数，{models} 的下一次请求会重新选中该凭据并打向 {provider} 上游」。
  - `reset_sessions`：写明清空的会话条数与作用域（本进程 qoder 会话缓存），后果写明「下次请求需重新握手换取会话，不影响账号本身」。
  - `probe`：写明目标 provider 与超时（45s），后果「只做一次健康探测，不改变路由与启用状态」。
  - `keepalive` / `claim` / `run_task`：写明目标账号范围（会话池条数 / 本轮可领取账号 / 全量账号），后果写明「会实际调用上游接口」。
  - `cancel_task`：写明任务 id、kind、当前状态，后果「仅停本地队列任务，不回滚已产生计费」。
- 二次确认文案走 i18n 插值（`{target}` / `{effect}` / `{provider}`），五语同步。
- 导出走 `Blob` + 临时 `<a download>`，文件名取后端 `Content-Disposition`。
- 非 admin（`data.readonly`）不渲染任何动作按钮与导出按钮；分页/筛选照常。

### 4. i18n（5 份 locale 同步）

新增 `providerConsole` 键（值按语种翻译，结构一致）：
- `tabs.accounts`
- `accounts.title / empty / status.all / status.active / status.expired / status.disabled / uid / nickname / realm / expiresAt / credits / searchPlaceholder`
- `common.export / common.page / common.prev / common.next / common.total / common.confirmTitle / common.confirm / common.cancel / common.noData`
- `actions.confirm.probe / keepalive / claim / runTask / cancelTask / clearCooldown / resetSessions`
- `actions.clearCooldown / actions.resetSessions / actions.clearedCount`
- `usage.filterStatus / filterModel / keyword / logsTotal`

### 5. 测试

新增 `apps/control/tests/test_provider_console_management.py`：

- 用 `db_path` fixture 直接插 `accounts` 与 `request_logs`，验证：分页边界、`status` 过滤、`q` 搜索、`owner` 隔离（viewer 只看自己）。
- 导出：`GET .../accounts/export`、`.../logs/export` 返回 200 + `text/csv` + `attachment`；正文无 `Bearer`、无 `http://`、无绝对路径。
- 动作：`clear_cooldown` 对未挂载 provider（codebuddy / gptgrok）返回 400；对 codar 的 scheduler 打桩 `note_success` 断言被调用（monkeypatch/注册表注入 `_scheduler` 可用 mock）。
- `reset_sessions` 仅 qoder；非 qoder 返回 400。
- viewer 对动作 403、对 accounts/logs 200 且只含本人数据。

参数化 7 家 provider，按 `-k <pid>` 可单跑，满足逐家验证。

## 验收

每家跑：
```bash
python -m pytest apps/control/tests/test_provider_console_management.py -q -k <pid>
```
收尾跑：
```bash
python -m pytest apps/control/tests/test_provider_console.py apps/control/tests/test_provider_console_management.py -q
cd apps/web && npx tsc --noEmit
python scripts/check_i18n.py --strict
```

## 明确不做

- 不改 `adapters/**`、`middleware/rate_limit.py`、`routers/health.py`。
- 不做后台定时清理/自动重连/自动重试。
- 不新增导航项、不改设置页 8 tab 顺序、不改品牌与浮动按钮。
