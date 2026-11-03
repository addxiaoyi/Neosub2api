# 前端骨架：可扩展但不改观

> **已判定不采用，仅作存档。** 本文的 tokens、AppShell、states、provider-kit 等自造抽象未采纳，不得作为实现依据；前端基准与红线见 [docs/FRONTEND-BASELINE.md](FRONTEND-BASELINE.md)。正文保持原样，不再维护。

本仓前端（`apps/web`）保持 codebuddy2api 迁移时的原样：底部 dock、右上角语言/版本切换、页面结构、组件与视觉语言都不变。骨架只做一件事——把写死在页面里的 provider 元数据收进配置，让新增第 12 家 provider 时只加 descriptor 和适配器。

> `docs/design/` 下的信息架构提案（`ia-proposal.md`、`prototype.html`、`README.md`）仅存档，未采纳。

## 令牌

`app/tokens.css` 新增颜色、间距、圆角、字号、层级变量，`app/globals.css` 只加一行 `@import "./tokens.css";`。

| 类别 | 变量 | Tailwind 类 |
| --- | --- | --- |
| 表面 | `--surface` / `--surface-raised` / `--surface-sunken` / `--line` | `bg-surface` `bg-surface-raised` `bg-surface-sunken` `border-line` |
| 风险 | `--risk-safe` / `--risk-warn` / `--risk-danger`（含 `-bg` / `-border`） | `text-risk-danger` `bg-risk-warn-bg` 等 |
| 间距 | `--space-gutter` / `--space-section` / `--space-stack` | `px-gutter` `gap-section` `gap-stack` |
| 圆角 | `--radius-card` / `--radius-pill` | `rounded-card` `rounded-pill` |
| 字号 | `--text-caption`(12px) / `--text-body`(14px) / `--text-title`(18px) / `--text-display`(24px) | `text-caption` `text-body` `text-title` `text-display` |
| 层级 | `--z-nav` / `--z-overlay` | — |

风险三档在浅色下的对比度：danger `#b91c1c` on `#f4f4f5` = 5.84:1；warn `#78350f` ≈ 9.9:1；safe `#166534` ≈ 8.0:1。

## 组件清单

### 外壳 `components/shell/`

- `AppShell`：与 `app/(main)/layout.tsx` 同一结构（底部 `ManagementBar`、`max-w-7xl` 居中容器、右上角语言/版本切换），页面迁移时直接 `<AppShell>{children}</AppShell>`。
- `PageHeader`：re-export 现有 `components/common/layout/PageHeader`，不引入第二套标题规格。
- `ContentSection`：沿用「20px 圆角 + `bg-muted`」卡片语言，提供 title / description / actions。

### 状态原语 `components/states/`

- `LoadingSkeleton`：`list` / `table` / `detail` 三种形态，`role="status"`，未返回前不渲染 0。
- `EmptyState`：真空数据 + 引导动作。
- `ErrorState`：中文原因 + 重试按钮 + 折叠诊断，`role="alert"`。
- `ConfirmDialog`：re-export 现有实现，危险操作口径与密钥删除一致。
- `RiskBadge`：`safe` / `warn` / `danger` 三档，图标 + 文字 + 颜色三重编码，不依赖颜色单独区分。

### descriptor 渲染 `components/provider-kit/`

- `ProviderDescriptorTable`：服务商表格，列结构对齐 `/providers`。
- `ProviderConfigForm`：按 `configFields` 渲染 text / password / number / switch / select / json。
- `ProviderDetailDrawer`：右侧抽屉展示元数据、风险说明与字段清单。

## descriptor 机制

`lib/provider-schema.ts` 是唯一登记处：

```ts
export interface ProviderDescriptor {
  id: string;
  displayName: string;
  vendor: string;
  license: string;
  runtime: string;
  status: 'implemented' | 'sidecar';
  description: string;
  defaultBaseUrl: string;
  protocols: string[];
  capabilities: ProviderCapabilities;
  riskLevel: 'safe' | 'warn' | 'danger';
  riskNote: string;
  configFields: ProviderConfigField[];
}
```

`configFields` 的字段值必须从 `apps/control/app/adapters/<id>.py` 的 `ProviderSpec` 与 `options.get(...)` 读取，不编造。`scope: 'connection'` 对应 `base_url` / `api_key`，`scope: 'option'` 对应 `settings` 表 `provider_config.<id>` 的 options。

`exposed: true` 表示该字段进入 `/providers` 高级区。现有页面只渲染这些字段（muse 的 `timeout`；gptgrok 的 `proxy` / `fingerprint` / `extra_headers`；zcode 的 `system_prompt_override`），保证迁移时的观感不变。

## 接入第 12 家 2api

1. **后端适配器**：在 `apps/control/app/adapters/` 新增 `<id>.py`，实现 `ProviderSpec`、`configure(base_url, api_key, options)` 与能力位；在 `adapters/__init__.py` 注册。
2. **descriptor**：在 `apps/web/lib/provider-schema.ts` 的 `PROVIDER_DESCRIPTORS` 追加一条，字段照抄适配器里真实读取的 `options`。
3. **高级区字段**：需要出现在 `/providers` 表单里的 option 字段标 `exposed: true`；不需要就只登记不暴露。
4. **文案**：复用 `providers.status*` / `providers.cap*` 与 `risk.safe|warn|danger`；新字段的 label 用现有 i18n 键或中文原文（`t()` 对未知键原样返回）。`zh-CN` 与 `en` 需要同步，其他语言回落中文。
5. **校验**：
   ```powershell
   # 后端
   cd apps/control; ..\..\.runtime\python\python.exe -m pytest tests -q
   # 前端
   cd apps/web; npm run build:export
   ```
6. **界面验收**：打开 `/design`，确认新 provider 出现在表格与详情抽屉；需要配置表单时点 `ProviderConfigForm` 的样例切换。页面不应出现横向滚动，控制台无报错。

## 本轮改动到的现有文件

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `app/globals.css` | +1 行 `@import "./tokens.css";` | 引入令牌层 |
| `components/common/providers/ProviderPanel.tsx` | `OPTION_FIELDS` / `STATUS_KEY` / `CAP_LABELS` 改为读取 `provider-schema.ts` | 去掉按 provider 写死的字段表；渲染输出不变 |

未改动：导航结构、底部 dock、品牌 metadata、`workbuddy-manager:*` 存储键、设置页「服务商接入」tab。

## 验收记录

- `npm run build:export`：通过，`/design` 进入静态产物。
- `/design`：1440 与 768 下 `document.documentElement.scrollWidth` 分别等于视口宽度（无横向滚动）；控制台 0 条错误；Tab 连续 16 次覆盖 10 个可聚焦控件。
- `apps/control/tests`：`76 passed, 3 subtests passed`（该数字为当次快照；当前基线为 `250 passed, 1 skipped, 3 subtests passed`）。
- 截图：`docs/ux-shots/skeleton/design-1440.png`、`design-768.png`、`design-drawer-1440.png`。
