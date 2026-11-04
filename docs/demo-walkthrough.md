# 演示模式有数据走查（2026-11-03）

## 结论

九个业务页面 + 设置页 8 个 tab 均在演示数据下完成 1440 / 768 两轮截图。**有数据时界面本身没有崩溃或空白页**，字段、列表、图表、分页、状态徽标都能正常渲染；但暴露了 1 个中等演示覆盖缺口、3 个界面/数据口径问题，以及 1 个演示模式特有的后台 503。

| 页面 | 有数据内容 | 结论 |
| --- | --- | --- |
| `/dashboard` | 2 账号 / 90 Token / 5 次请求 / 14 天趋势图 | 通过；账号健康显示「未加载 2」见问题 5 |
| `/keys` | 2 把演示密钥，前缀已掩码，一把停用 | 通过；「最近使用」与日志不一致，见问题 4 |
| `/accounts` | 2 个演示账号、到期时间、状态/积分列 | 通过；顶部 CodeBuddy 操作按钮可见，见问题 3 |
| `/models` | `fake-chat-1`、`fake-chat-2`，状态「就绪」，provider `demo_local` | 通过 |
| `/playground` | 载入演示密钥后拉出 2 个模型，provider 正确 | 通过（需手动填入演示密钥） |
| `/logs` | 6 条记录，含 1 条 429；时间/密钥/服务商/模型/状态/耗时/Token 列为表格文本 | 通过，不是原始 JSON |
| `/stats` | 5 请求 / 90 Token / 1 活跃密钥 / Top 模型 + 柱状图 | 金额展示有问题 2 |
| `/quota` | 分组 / 账号池 / 流水三个 tab 均为空 | **未验证到有数据**，见问题 1 |
| `/quota/policies` | 2 条策略（日 $5 / 200 次、月 $20 / 不限次）；用量 5/200、3 次，剩余 $4.9999 / $19.9999，重置时间与倒计时 | 通过；金额精度见问题 2 |
| `/settings` 8 tabs | 上游服务信息、运行参数、模型别名、用户、访问令牌、系统更新、更新日志、关于 | 全部通过 |

控制台与网络：除 `/dashboard` 每次加载固定一条 `503 /api/accounts/refresh-credits?force=false` 外，其余 33 次页面截图 **0 条 console error、0 个 pageerror、0 个 4xx/5xx**（含设置页全部 tab）。768 宽度下表格、卡片、策略用量区均无横向溢出。

## 环境与复现

```powershell
$env:UNIFIED_DEMO_MODE="1"
$env:UNIFIED_HOST="127.0.0.1"; $env:UNIFIED_PORT="18250"
$env:UNIFIED_DATA_DIR="$env:TEMP\unified-demo-walkthrough"
$env:UNIFIED_DB_PATH="$env:TEMP\unified-demo-walkthrough\unified.db"
$env:UNIFIED_WEB_DIR="<仓库>\apps\web\out"
$env:UNIFIED_ADMIN_PASSWORD="<临时密码>"
.runtime\python\python.exe -m uvicorn --app-dir apps\control app.main:app --host 127.0.0.1 --port 18250
```

- 数据目录放在系统临时目录，未写入仓库 `data/`；结束后实例已停止，临时目录与其中的演示凭据文件已删除。
- Playwright 使用仓库 `.runtime` 内置浏览器，Chromium headless；登录态通过 `POST /api/login`（admin）注入 cookie。
- 每页等待 `networkidle` 后再等 2.5 秒截图；首次加载的菜单栏引导通过 localStorage 预先关闭，避免遮挡内容。
- `/playground` 额外填入演示密钥并点击「保存并加载模型」，确保截图是加载后的有数据状态。

接口真值对照：`/api/stats/summary` 今日 5 请求 / 90 Token / 0.000125 USD；`/api/quota/status` 已用 125 micro-USD、5 次；`/api/logs` 6 条；`/api/models` 2 条；`/api/accounts` 2 条；`/api/keys` 2 条。截图数值与接口一致（金额精度除外）。

## 问题清单

### 1. 中：`/quota` 在演示模式下仍为空，无法验证有数据排版

- 复现：`/quota` → 三个 tab（分组、账号池、流水）均为空状态。
- 实际：演示播种只覆盖反代网关侧（密钥、模型、日志、配额策略），没有 sub2api 分组/账号池/流水数据；`/api/sub2api/*` 返回空集合。
- 定性：**演示数据覆盖缺口**，不是界面 bug。界面空状态本身正常。
- 影响：本轮仍无法验证该页面「有数据时」的分组卡片、账号池表格、流水分页排版。

### 2. 低：统计页金额被截断到 4 位小数，且缺少币种标识

- 复现：`/stats` 今日实付 / 本周实付显示 `0.0001`；接口真值 `0.000125` USD。
- 原因：`apps/web/lib/format.ts:291` 的 `fmtCredit` 用 `toFixed(4)`；`/quota/policies` 走 `fmtMicroUsd`（`format.ts:270`），`maximumFractionDigits: 4`，125 micro-USD 同样显示为 `US$0.0001`。
- 同时统计页金额没有 `US$` 符号，与配额页的 `US$` 展示口径不一致。
- 定性：**界面真实问题**（micro-USD 换算本身正确，是展示精度与单位问题；真实小额账单同样会被四舍五入）。0.000125 → 0.0001，偏差约 20%。

### 3. 低：账号页顶部操作按钮未按「已启用服务商」收敛

- 复现：`/accounts`（筛选为「全部」，演示环境仅启用 `demo_local`，CodeBuddy 停用）顶部仍显示「强制重启 / 刷新积分 / 今日已签」。
- 原因：`apps/web/app/(main)/accounts/page.tsx:103-104` 在 `providerFilter === 'all'` 时固定按 `codebuddy` 能力渲染按钮；`opsMeta` 中存在 CodeBuddy 的声明能力，即使该 provider 已停用也会显示。
- 点击「刷新积分」会以 `provider=codebuddy` 调用 `/api/accounts/refresh-credits`，在演示环境失败。
- 定性：**界面问题**（演示环境放大，生产启用 CodeBuddy 时不一定触发）。

### 4. 低：密钥页「最近使用：从未使用」与日志页 6 条使用记录矛盾

- 复现：先看 `/keys`（两把密钥最近使用均为「从未使用」），再看 `/logs`（演示密钥 A 已有 6 条 200/429 记录，时间在播种时刻）。
- 原因：`apps/control/app/demo.py:341` 的播种直接调用适配器并 `_write_call` 写日志/用量，绕过了 `apps/control/app/routers/v1.py:92` 的 `api_keys.last_used_at` 更新。
- 定性：**演示数据造得不对**（种子数据口径不一致），不是密钥页展示 bug。通过 Playground 真实走一次网关后，该字段会正常更新。

### 5. 信息：`/dashboard` 固定产生一条 503 控制台错误

- 复现：打开 `/dashboard`，DevTools Network 出现 `POST /api/accounts/refresh-credits?force=false → 503`，Console 一条对应 error。
- 原因：管理员首屏聚合加载时会刷新积分；演示环境只有 `demo_local` 且无 credits 能力、CodeBuddy 停用，后端没有可用的积分适配器，返回 503。页面用 `Promise.allSettled` 兜底，无可见错误块，积分显示「— 等待上游同步」。
- 定性：**演示能力缺口**，不是界面故障；但会在每轮走查里制造噪声。

### 6. 信息：演示账号状态「未知 / 未加载」属于假上游能力缺口

- `/accounts` 两行状态为「未知」、积分为「—」；`/dashboard` 显示「有效期内 2 · 2 个不可用」「账号健康快照 · 未加载 2」。
- 原因：`fake_upstream` 不提供账号池健康/积分接口；演示种子按设计不编造余额。
- 定性：演示数据缺口，账号名称、到期时间、启停状态、密钥前缀等真实字段均正常显示。

## 截图与验证方式

截图目录：`docs/ux-shots/demo/`（已被 `.gitignore` 覆盖，不入库），命名 `页面-宽度.png`，共 34 张。

- 页面：`dashboard`、`keys`、`accounts`、`models`、`playground`、`logs`、`stats`、`quota`、`quota-policies`，各 `-1440.png` / `-768.png`。
- 设置页 tab：`settings-upstream`、`settings-runtime`、`settings-models`、`settings-users`、`settings-tokens`、`settings-system`、`settings-changelog`、`settings-about`，各 `-1440.png` / `-768.png`。
- 每轮截图同时采集 console error、pageerror、同源 4xx/5xx 与请求失败；采集结果：仅 `dashboard-1440` 与 `dashboard-768` 各 1 条 503，其余全部干净。
- 重点核对：`stats` 金额换算与接口真值、`logs` 表格列可读性、`quota-policies` 用量/剩余/重置时间对比、`accounts` 演示账号字段、`models` 演示模型与「就绪」状态。

![统计页 1440](ux-shots/demo/stats-1440.png)

![配额策略页 1440](ux-shots/demo/quota-policies-1440.png)

![账号页 1440](ux-shots/demo/accounts-1440.png)

本轮仅新增本文件与截图，未修改任何源码、`apps/` 或既有文档，未执行 git 操作。
