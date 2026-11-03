# 设计落地说明

> **已判定不采用，仅作存档。** 本文与其描述的落地方案、原型均未采纳；前端基准与红线见 [docs/FRONTEND-BASELINE.md](../FRONTEND-BASELINE.md)。正文保持原样，不再维护。

- 方案：[ia-proposal.md](file:///d:/qwq/项目/unified-2api/docs/design/ia-proposal.md)
- 原型：[prototype.html](file:///d:/qwq/项目/unified-2api/docs/design/prototype.html)
- 本文回答一件事：这些结论怎么落到 `apps/web/` 的现有代码里，按改动成本排先后。

## 0. 边界

- 不改 API 契约、不动 `realm` 行为、不新增依赖、不重写技术栈（仍是 Next 15 + shadcn + Tailwind）。
- 不改 `localStorage` 键前缀（`workbuddy-manager:*`）。品牌改名只动可见文案，避免用户丢掉 dock 位置、引导关闭状态与版本偏好。
- 原型里的样式是「目标态参考」，不是可直接拷贝的文件；落地时优先复用现有 token 与组件，不新建一套 CSS。

## 1. 改动分三类

### A. 纯样式 / 文案（不改结构，可独立合入）

| # | 改动 | 文件 | 成本 |
| --- | --- | --- | --- |
| A1 | 术语替换：`上游提供方`→`服务商`、`上游地址`→`服务地址`、`Sidecar`→`独立进程`、`上游配置`→`运行配置`、`反代上游`→`上游服务` | `lib/i18n/locales/{zh-CN,zh-TW,en,ja,ko}.json` 共 6 处键（清单见 ia-proposal §3.2） | 0.5 天，纯文本 |
| A2 | 风险说明字号 10px→12px、去掉 `text-destructive/80`、对比度提到 ≥4.5:1 | `components/common/providers/ProviderPanel.tsx` L189-L209 | 0.5 天 |
| A3 | `safe` 也渲染「可商用」，不再留空白 | 同上，徽章条件分支 | 0.5 天 |
| A4 | 品牌：`title.template` / `default` / `description`、个人信息面板产品名、`site.webmanifest` | `app/layout.tsx` L23-L33、`ManagementBar.tsx` L488 附近、`app/favicon/site.webmanifest` | 0.5 天 |
| A5 | 删除 `/settings` 的「服务商接入」tab（与 `/providers` 同一 `ProviderPanel`） | `app/(main)/settings/page.tsx` L1088-L1102 | 0.5 天，删代码为主 |
| A6 | 顶部计数在失败态显示 `—`，加载态不显示 `0` | `ProviderPanel.tsx` 统计区 | 0.5 天 |

这一类不需要设计评审，可直接进 sprint；A1 与 A4 需要四份 locale 同步，别只改中文。

### B. 组件结构（需要拆组件 / 改布局）

| # | 改动 | 涉及 | 成本与注意 |
| --- | --- | --- | --- |
| B1 | 新增 `SidebarNav`：4 组 11 项，`aria-current="page"`，分组标题不可聚焦；`(main)/layout.tsx` 从「单列 + pb-24」改为「左侧栏 + 主区」网格 | 新建 `components/common/layout/SidebarNav.tsx`；改 `app/(main)/layout.tsx` L40-L53 | 2–3 天。保留 `ManagementBar` 的移动 dock 分支，仅在 ≥1024px 隐藏 dock |
| B2 | 服务商表格从 6 列改 5 列（服务商 / 许可证风险 / 状态 / 服务地址 / 启用），配置项移入行展开详情 | `ProviderPanel.tsx` L237-L352 拆成 `ProviderList` + `ProviderDetail` | 3 天。这是 U1 溢出的根治：去掉固定 `w-[210px]` 地址输入框，地址列改只读文本 + 截断 |
| B3 | `EmptyState` 增加 `error` 变体：`role="alert"`、标题/说明/重试/诊断折叠；失败与空态共享容器尺寸 | 现有空态组件 + `ProviderPanel.tsx` L350-L352 | 1–2 天。`catch` 里要保留 error 状态，不能只 toast 后置空 |
| B4 | 统一确认：停用服务商、关闭网关鉴权走二次确认；danger 启用走勾选门控；普通启停保留即时 + toast | `ProviderPanel.tsx` 的 `patch` 调用点、`ConfirmDialog` | 2 天。确认口径表见 ia-proposal §5.5，别再按操作类型各写一套 |
| B5 | 移动端 「更多」：dock 5 项改 4 项 + `Sheet`，里面放日志/用量/模型/测试台/任务/安全/设置 | `ManagementBar.tsx` L416-L445、L690-L726 | 1–2 天。顺手删掉贴边引导浮层，改成侧栏顶部提示条 |
| B6 | `FloatingDock` 增加 `active` 参数与当前页高亮 | `components/ui/floating-dock.tsx` | 0.5 天，移动端才需要 |

B1 与 B2 是唯一两处「必须动结构」的地方；其余都可以在现有组件内完成。

### C. 页面 / 路由（新增或接入口）

| # | 改动 | 依赖 | 成本 |
| --- | --- | --- | --- |
| C1 | 导航接入 6 个已有页面：`/logs`、`/stats`、`/models`、`/playground`、`/tasks`、`/security` | 无，页面已实现，i18n 已有 `nav.*` 文案 | 0.5 天，只有 href 与分组 |
| C2 | 模型路由页 `/models/routing`，迁入设置里的「模型映射」 | `GET/POST /api/settings/model-map`、`/api/providers` 已存在 | 3 天，纯前端组合 |
| C3 | `/tasks` 增加「注册任务」分区 | `POST/GET /api/grok/tasks` 已存在 | 2 天 |
| C4 | 配额页 `/quota`（sub2api 分组/账户/流水） | `/api/sub2api/{groups,accounts,ledger}` 已存在 | 4 天，前端 0 实现 |
| C5 | 用量与账单 | **M3 未落地**（`billing_ledger` / `quota_policies`） | M3 后再排，现在不做 |

C1 是性价比最高的一项：六个页面已经能跑，只差入口。

## 2. 推进顺序（按成本从低到高）

| 阶段 | 内容 | 目的 |
| --- | --- | --- |
| **P0**（约 2–3 天） | A1–A6 + C1 + B6 | 不冒结构风险，先把「找不到页面 / 术语混用 / 风险看不出 / 品牌还是 WorkBuddy」解决 |
| **P1**（约 1 周） | B1 + B2 + B3 | 桌面导航换侧栏；服务商页三态与溢出根治。这两项一起做，因为表格重构要复用侧栏后的宽度预算 |
| **P2**（约 1–2 周） | B4 + B5 + C2 + C3 | 确认口径统一、移动 dock 收敛、模型路由与注册任务 |
| **P3** | C4，等 C5 | 配额页先做；账单等 M3 接口 |

不建议先做 B1 再做 A1：侧栏菜单里如果还写着「上游配置」「Sidecar」，等于把术语问题放大到全局。

## 3. Token 映射

原型里的变量对应到现有 Tailwind/shadcn token，不新增主题层：

| 原型 | 值 | 落到现有 token |
| --- | --- | --- |
| `--paper` | `#f7f7f5` | `--background`（浅色主题） |
| `--surface` | `#ffffff` | `--card` |
| `--ink` | `#22252a` | `--foreground` |
| `--muted` | `#5f646b` | `--muted-foreground` |
| `--line` / `--line-strong` | `#e2e2dd` / `#cfcfc9` | `--border` / `--input` |
| `--accent` | `#1f6f5c` | `--primary`（替换现有高饱和蓝/紫） |
| `--accent-soft` | `#eef4f1` | `--accent`（选中态底色） |
| 风险三档 | `#8c2f2f` / `#6f4a12` / `#4a5560` | 新增 `--risk-danger` / `--risk-warn` / `--risk-safe`，只在徽章与合规区块使用 |

强调色只用于：主按钮、当前导航项、开关开启态、链接。不要再给表头、卡片描边、图标加彩色。

## 4. 验证记录

原型用本机 Edge headless 跑过（Playwright，1440/1280/1024/768/390 五个视口）：

- 控制台 `errors: []`、无失败请求（favicon 已用 `data:` 空图标消除 404）。
- 五个视口 `document.scrollWidth === clientWidth`，表格无横向溢出；≤760px 转卡片布局。
- 三种状态可切换：正常 5 行；空态显示「空数据：注册表返回 0 条」；失败态计数为 `—`，并提供重试。
- danger 行展开后，未勾选「仅内部评估」时保存按钮 `disabled`，勾选后变「已确认，可保存」。
- Tab 顺序：侧栏 12 项 → 版本/语言 → 刷新 → 状态切换 → 行展开 → 启用开关 → 详情表单，无焦点陷阱。

落地后需要回归：

- 1440 / 1024 / 768 三档宽度，表格与侧栏都不出现横向滚动。
- 深色主题下风险三档的对比度（原型只做了浅色）。
- 五份 locale 的导航与风险文案不缺键。
- `workbuddy-manager:*` 的既有 localStorage 数据仍能读到。

## 5. 非目标

- 不做深色主题重设计、不做登录页改版（原型未覆盖）。
- 不引入图表库或新的 UI 依赖；`/stats`、`/dashboard` 的图表沿用现有实现。
- 不把 `provider`、`risk_level`、`realm` 等代码/接口字段改成中文。
