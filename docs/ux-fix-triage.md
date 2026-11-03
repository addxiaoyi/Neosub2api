# UX 问题修复分级（A / B / C）

- 依据：`docs/ux-review.md` 与 `docs/ux-shots/`（截图拍摄于 2026-11-02 18:00 左右）
- 复核方式：只读比对当前代码与基准仓库 `addxiaoyi/codebuddy2api@main`（2026-09-25）
- 约束：本文只做分级与修法建议，未修改 `apps/web`、`apps/control` 任何文件，未执行 git 提交
- 重要时间差：截图之后（18:21–20:01）并行会话已改动 `auth.py`、`ProviderPanel.tsx`、`dashboard/page.tsx`、`settings/page.tsx`、`accounts/page.tsx`。下文每项都标注“当前代码状态”，但未重新 build 截图，UI 效果仍需复测

## 结论

**三类分布：A 类 9 条，B 类 3 条，C 类 2 条。**

| 类别 | 含义 | 编号 |
| --- | --- | --- |
| A | 不改外观即可修（文案、状态判断、确认框、对比度、内联校验） | U2、U3、U5、U7、U8、U9、U11、U12、U14 |
| B | 必须改结构或外观，或属于 IA；基准版恢复后可能自然消失 | U1、U10、U13 |
| C | 功能/数据缺陷，修法不涉及视觉 | U4、U6 |

**A 类里最该先修的 3 条（按当前代码尚未修复的部分排序）**

1. **U8 密钥首屏空白**：加载中和“没有密钥”在视觉上完全一样，每次进页面都会遇到。
2. **U12 仪表盘透传上游底层错误**：直接显示内网地址和英文 `All connection attempts failed`，既是可读性问题也泄露内部拓扑。
3. **U14 密钥名称无内联校验**：提交后才用 toast 报错，用户要回头找字段，是唯一还没有“提交前提示”的表单。

补充：U2、U3、U5、U7、U9 在当前代码里已被并行会话修复，应转为回归确认，不应重复开发。

**U4 根因判断**

不是前端调错接口。`apps/web/lib/api.ts` 的 `accountApi.start` 调用 `POST /api/auth/start`，与基准版 `web/lib/api.ts` 逐字一致；基准后端 `server/routers/accounts.py` 也实现了 `@router.post('/auth/start')`。问题出在控制面重写时漏掉了该路由：当时 `apps/control/app/routers/accounts.py` 只有 `GET ""`、`/sync`、`/refresh-credits`、`/checkin-all`、`/{file}/...`、`/restart`，POST 请求落到 `main.py` 末尾的 `StaticFiles` 挂载，StaticFiles 不支持 POST，于是返回 `405 Method Not Allowed`。属于后端迁移遗漏（regression），修它不需要改任何视觉。

**基准版差异归属**

- **原仓库自带（7 条）**：U2、U8、U10、U11、U12、U13、U14
- **我们引入（5 条）**：U1、U3、U6、U7、U9
- **混合（1 条）**：U5（服务商侧为本地新增，密钥侧来自基准）
- **迁移遗漏（1 条）**：U4

## 分类总表

| 编号 | 页面 | 类别 | 基准归属 | 当前代码状态（只读复核） | 优先级 |
| --- | --- | --- | --- | --- | --- |
| U1 | 服务商 / 设置 | B | 我们引入 | 已加“启用”列 sticky、输入框 210→150px、<lg 滚动提示；是否仍截列需复测 | P1（若保留该页） |
| U2 | 仪表盘 | A | 原仓库自带 | **已修**：`pending` + “加载中 / 数据不可用”，不再渲染 0 | P1（回归确认） |
| U3 | 服务商 | A | 我们引入 | **已修**：中文 `providers.loadFailed` + 错误面板 + 重新加载 | P1（回归确认） |
| U4 | 账号 | C | 迁移遗漏 | 18:21 已在 `auth.py` 补 `POST /auth/start`；依赖已启用 oauth 适配器 | **P0** |
| U5 | 服务商 / 密钥 | A | 混合 | 服务商启停、网关匿名已加确认；密钥启停仍即时生效，口径未统一 | P2 |
| U6 | 服务商 | C | 我们引入 | 仍无 `warn` 级数据；琥珀样式存在但从未触发 | P3 |
| U7 | 服务商 | A | 我们引入 | **已修**：风险说明 12px + `text-red-700`，对比度约 6:1 | P1（回归确认） |
| U8 | 密钥 | A | 原仓库自带 | 未修：`loading` 时不渲染任何占位，只剩表头 | P1 |
| U9 | 服务商 | A | 我们引入 | **已修**：空态 + 说明 + 重新加载 | P2（回归确认） |
| U10 | 全站 | B | 原仓库自带 | 未修：Dock 引导浮层仍可能遮挡表格 | P3 |
| U11 | 登录 | A | 原仓库自带 | 未修：空提交仍提示自动填充场景 | P2 |
| U12 | 仪表盘 | A | 原仓库自带 | 未修：`upstream.error` 原样红字输出 | P1 |
| U13 | 账号 | B | 原仓库自带 | 未修：空态动作与页头重复 | P3 |
| U14 | 密钥 | A | 原仓库自带 | 未修：仅 `notify.err('请填写密钥名称')` | P2 |

## A 类：不改外观即可修（9 条）

| 编号 | 现状证据 | 修法（不新增组件、不改布局） | 影响文件 | 优先级 |
| --- | --- | --- | --- | --- |
| U2 | `dashboard-loading.png`：请求未返回时显示 `0`、`全部正常` | 引入“请求是否 settled”的状态；未 settled 时数值用 `—`、提示用“加载中”，禁止在此之前计算“全部正常”；失败时用“数据加载失败”文案 | `apps/web/app/(main)/dashboard/page.tsx` | P1（已修） |
| U3 | `providers-error.png`：失败显示“没有登记任何服务商”+ 英文 `Network Error` | `catch` 分支写入独立 `error` 状态，先渲染错误面板（中文说明 + 重新加载），再判断空态；不把 axios 原文交给 toast | `apps/web/components/common/providers/ProviderPanel.tsx`、`apps/web/lib/i18n/locales/zh-CN.json` | P1（已修） |
| U5 | `providers-toggle-feedback.png`、`keys-delete-confirm.png`：删除有确认，启停直接生效 | 统一“启停”口径：常规启停即时生效 + toast；安全敏感开关（网关匿名）保留二次确认；密钥启停与服务商启停选择同一种做法 | `apps/web/components/common/providers/ProviderPanel.tsx`、`apps/web/app/(main)/keys/page.tsx` | P2 |
| U7 | `providers-1440.png`：风险说明 10px、`destructive/80`，实测 3.84:1 | 字号提到 12px，前景色改为不透明 `red-700`（实测约 6:1）；若未来出现 `warn` 徽章，字号同样提到 12px | `apps/web/components/common/providers/ProviderPanel.tsx` | P1（已修） |
| U8 | `keys-loading.png`：首屏只有表头，表格区域空白 | `loading && !keys.length` 时渲染“加载中…”一行（复用账号/服务商页已有文案模式），不要新增骨架组件 | `apps/web/app/(main)/keys/page.tsx` | P1 |
| U9 | `providers-error.png`：空数据只有一行提示 | 空态补一句原因说明 + “重新加载”按钮，规格与账号/密钥空态一致 | `apps/web/components/common/providers/ProviderPanel.tsx`、`apps/web/lib/i18n/locales/zh-CN.json` | P2（已修） |
| U11 | `login-empty-submit.png`：空提交提示把自动填充写进通用文案 | 通用提示改为“请输入用户名和密码”；自动填充专项提示只在检测到 DOM 有值而 state 为空时出现 | `apps/web/app/(auth)/login/page.tsx`、`apps/web/lib/i18n/locales/zh-CN.json` 及其余 4 个语言包 | P2 |
| U12 | `dashboard-1440.png`：显示 `CodeBuddy: 上游不可达（http://127.0.0.1:7864）：All connection attempts failed` | 面向用户只显示“上游服务不可达”，原始地址与英文错误折叠到“详情”或仅控制台输出；不改卡片布局 | `apps/web/app/(main)/dashboard/page.tsx`、`apps/web/lib/i18n/locales/zh-CN.json` | P1 |
| U14 | `keys-empty-name-validation.png`：空名称提交后才弹 toast | 提交前判断名称，输入框下方显示内联错误并聚焦；toast 仅作辅助 | `apps/web/app/(main)/keys/page.tsx` | P2 |

## B 类：必须改结构或外观（3 条）

| 编号 | 现状证据 | 为什么不能“只改文案” | 修法 / 处理建议 | 影响文件 | 优先级 |
| --- | --- | --- | --- | --- | --- |
| U1 | `providers-1440.png`、`providers-768.png`、`settings-1440.png`：表格 1621px，1440 下 `上游地址`、`启用` 在可视区外；768 只剩 1–2 列 | 列宽、列序或容器必须变；sticky 与滚动提示只能缓解，不能在不改布局的前提下让 6 列在 1440 全部可读 | 二选一：① 按“前端保持基准原样”，该页删除，问题自然关闭；② 若必须保留服务商管理，把 `上游地址` 编辑移入展开行，或 1280 以下改卡片布局。当前代码已加 sticky/滚动提示，需先复测再决定是否还改 | `apps/web/components/common/settings/ProvidersSection.tsx`、`apps/web/app/(main)/settings/page.tsx` | P1（若保留） |
| U10 | `providers-768.png`、`settings-1440.png`：引导浮层固定底部，遮挡表格与开关 | 位置/层级/出现时机必须改，属于布局，不是文案 | 若保持基准原样：不修（基准版 `ManagementBar` 也有 `showDockTip`）；若允许偏离：改为不遮挡内容的顶部提示，或进入页面后延时出现并允许一键忽略 | `apps/web/components/common/layout/ManagementBar.tsx`、`apps/web/components/ui/floating-dock.tsx` | P3 |
| U13 | `accounts-768.png`：页头已有 OAuth / 批量授权 / 添加账号，空态又重复一组按钮 | 需要删减或重排空态动作区，属于内容结构 | 基准版空态本来就重复 OAuth + 批量两个按钮；按“保持原样”不修。若允许偏离，空态只留一个主 CTA | `apps/web/app/(main)/accounts/page.tsx` | P3 |

导航入口问题不在 U1–U14 内，按你的指示归入 IA 并作废。只记录两个事实：基准版 `ManagementBar` 声明 11 个入口，本地曾多出 `/providers`（该页后来并入设置页）；`/red-packets` 页面与入口已于 2026-11-03 按业务决定整条链路删除，不再作为死链恢复。

## C 类：功能/数据缺陷，修法不涉及视觉（2 条）

### U4 添加账号 405 / 二维码空白

| 项目 | 内容 |
| --- | --- |
| 现状证据 | `accounts-add-dialog.png`：二维码区域空白，错误只有 `Method Not Allowed`；网关日志 `POST /api/auth/start → 405` |
| 判定 | **C 类，后端任务**；不是前端调错接口 |
| 根因 | 前端 `accountApi.start` 与基准版一致，调用 `POST /api/auth/start`；基准后端 `server/routers/accounts.py` 有该路由，本地控制面重写时漏掉。POST 落到 `main.py` 的 `StaticFiles` 挂载，StaticFiles 不支持 POST，返回 405 |
| 当前状态 | 18:21 已在 `apps/control/app/routers/auth.py` 补 `@router.post("/auth/start")`；但它要求管理员身份且存在已启用的 oauth 适配器，否则返回 503 中文提示。需用真实启用服务商验证二维码能出图 |
| 修法 | 补齐/保留 `/api/auth/start` 与 `/api/auth/poll`；控制面无 oauth 服务商时返回可执行的中文提示（已具备）。前端无需改视觉；`AddAccountDialog` 已用 `errText` 展示后端 detail，后端给中文即可 |
| 影响文件 | `apps/control/app/routers/auth.py`、`apps/control/app/routers/accounts.py`、`apps/control/app/main.py`、`apps/web/lib/api.ts`（仅确认，无需改）、`apps/web/components/common/accounts/AddAccountDialog.tsx`（仅确认，无需改） |
| 优先级 | **P0** |

### U6 warn 徽章从未出现

| 项目 | 内容 |
| --- | --- |
| 现状证据 | `providers-1440.png`：10 个服务商中 2 个 `danger`、0 个 `warn` |
| 判定 | **C 类，数据/后端**；不是 UI 缺陷。琥珀样式本身可用（amber-100/amber-900，理论对比度约 10:1），只是没有数据触发 |
| 修法 | 若确实存在 GPL/LGPL 来源，在适配器元数据里设 `risk_level="warn"`；若产品不打算使用该级别，作为 won't-fix 关闭（删除样式属于 B 类外观变更，不在本轮） |
| 影响文件 | `apps/control/app/adapters/base.py`、`apps/control/app/adapters/*_sidecar.py` |
| 优先级 | P3 |

## 基准版差异归属（逐项依据）

| 编号 | 归属 | 依据（基准仓库 `addxiaoyi/codebuddy2api@main`） |
| --- | --- | --- |
| U1 | 我们引入 | 基准 `web/app` 无 `providers` 路由，`web/components` 无 provider 组件，`web/lib/api.ts` 无 `providerApi`；该页为本地新增 |
| U2 | 原仓库自带 | 基准 `dashboard/page.tsx` 同样用空数组初始化，无 `pending` / “加载中”保护，加载期同样会渲染 `0` 与“全部正常”；本地已在 19:15 修复 |
| U3 | 我们引入 | 基准无服务商列表页；本地 `ProviderPanel` 原先 `catch` 后清空 `items` 且 toast 透传 axios 原文 |
| U4 | 迁移遗漏 | 基准 `server/routers/accounts.py` 有 `@router.post('/auth/start')`；本地 `apps/control/app/routers/accounts.py` 当时没有，属于重写时漏路由 |
| U5 | 混合 | 服务商与网关开关是本地新增页面带出的问题；密钥删除确认在基准版已存在（`ConfirmDialog`），所以“删除有确认、启停无确认”的不一致一半来自基准 |
| U6 | 我们引入 | 基准无 provider 概念，`risk_level` 与琥珀徽章均为本地新增 |
| U7 | 我们引入 | 基准无 `risk_note`；10px / `destructive/80` 是本地样式 |
| U8 | 原仓库自带 | 基准 `keys/page.tsx` 有 `loading` 但只在 `!keys.length && !loading` 时渲染空态，加载期无占位 |
| U9 | 我们引入 | 基准无服务商空态 |
| U10 | 原仓库自带 | 基准 `ManagementBar.tsx` 同样含 `showDockTip` / `dockTipStep` 引导浮层 |
| U11 | 原仓库自带 | 基准 `zh-CN.json` 的 `login.emptySubmit` 与本地逐字相同 |
| U12 | 原仓库自带 | 基准 `dashboard/page.tsx` 同样直接红字渲染 `upstream.error` |
| U13 | 原仓库自带 | 基准 `accounts/page.tsx` 的空态已重复 OAuth + 批量授权按钮 |
| U14 | 原仓库自带 | 基准 `keys/page.tsx` 同样只 `notify.err(t('keys.nameRequired'))`，无内联错误 |

### 「设置默认 tab」与「导航缺入口」的澄清

- **服务商接入默认 tab**：基准 `settings/page.tsx` 是 `defaultValue="upstream"`，且根本没有 `providers` tab（基准 tab 为 upstream / models / users / tokens / system / changelog / about）。本地把 `providers` 插到第一位并设为默认，属于新增 IA，不是“默认 tab 选错”。按“前端保持原样”应删除该 tab，问题随之消失；不要只把默认值改回 `upstream` 而留下多余 tab。证据：`settings-default-tab.png`、`settings-1440.png`。
- **导航缺 6 个入口**：按你的指示作废。代码层事实是本地导航入口已与基准对齐，`/providers` 并入设置页、`/red-packets` 按业务决定删除，两处都不是待修复项。

## 建议执行顺序

1. **先定边界**：确认 `/providers` 页与设置页“服务商接入”tab 是否随“保持基准原样”删除。若删除，U1、U3、U5（服务商侧）、U6、U7、U9 直接关闭，不投入修复。
2. **P0**：验证 U4 的 `/api/auth/start` 在新路由下能返回二维码；补一个已启用 oauth 服务商的实测。
3. **A 类未修项**：U8 → U12 → U14 → U11（U5 统一启停口径可并入 U14 同一次表单/交互整理）。
4. **回归确认**：U2、U3、U5、U7、U9 已由并行会话修改，重新 build 后按原截图状态复测，避免“代码改了但 UI 没生效”。
5. **B 类原仓库自带项**（U10、U13）：在“保持原样”前提下不修；若后续允许偏离，再单独立项。

## 未做与限制

- 本轮未重新 build、未重新截图；U1 的 sticky 列与收窄输入框是否已彻底消除 1440 截列，需要复测才能关闭。
- “当前代码状态”依据只读阅读与文件修改时间（`auth.py` 18:21、`ProviderPanel.tsx` 18:49、`dashboard/page.tsx` 19:15、`settings/page.tsx` 19:59、`accounts/page.tsx` 20:01），不代表已在浏览器验证。
- 深色主题下的对比度未复核；U7 的结论只针对浅色主题。
