# 任务清单：服务商管理端可用化

> 依 `spec.md` 执行。逐家推进、逐家验证、逐家报进度；改动留工作区。

## 阶段 A：后端地基（一次成型，覆盖 7 家）

- [x] A1 `services/provider_console.py`：新增 `MAX_PAGE_SIZE` / `DEFAULT_PAGE_SIZE` / `accounts_page` / `logs_page` / `csv_text`
  - 验收：`python -c "from app.services import provider_console"` 无误；`accounts_page` 对空表返回 `{items: [], total: 0, page: 1, size: 20, pages: 0}`
- [x] A2 `services/provider_console.py`：新增 `async clear_cooldown(adapter, credential="")`，按 `fingerprint` 匹配
- [x] A3 `services/provider_console.py`：更新 `ACTIONS` 矩阵（muse/zcode/trae/qoder/codar 挂 `clear_cooldown`，qoder 增 `reset_sessions`）
- [x] A4 `routers/provider_console.py`：`ActionIn.credential`；`clear_cooldown` / `reset_sessions` 分支 + 审计
- [x] A5 `routers/provider_console.py`：`GET accounts` / `GET accounts/export` / `GET logs` / `GET logs/export` / `GET codebuddy/tasks/export`
- [x] A6 `tests/test_provider_console_management.py`：分页/筛选/导出/权限/动作 6 组断言（参数化 7 家）

## 阶段 B：逐家验证与报进度

每家：跑 `pytest ... -k <pid>`，确认动作集与快照不外泄，然后报一条进度。

- [x] B1 codebuddy（probe / run_task + 任务台账导出）
- [x] B2 muse（probe / keepalive / clear_cooldown）
- [x] B3 gptgrok（probe / cancel_task）
- [x] B4 zcode（probe / claim / clear_cooldown）
- [x] B5 trae（probe / clear_cooldown）
- [x] B6 qoder（probe / clear_cooldown / reset_sessions）
- [x] B7 codar（probe / clear_cooldown）

## 阶段 C：前端

- [x] C1 `lib/provider-console.ts` 新增分页与行类型
- [x] C2 `lib/api.ts` 新增 `accounts` / `logs` / `exportCsv`
- [x] C3 `ProviderConsolePanel.tsx` 新增 `accounts` 子 tab（筛选/分页/导出/空态）
- [x] C4 `ProviderConsolePanel.tsx` usage tab 日志分页/筛选/导出
- [x] C5 `ProviderConsolePanel.tsx` 全部动作接入二次确认；connections 行内「清冷却」
- [x] C6 五语 locale 同步新增键（zh-CN / zh-TW / en / ja / ko）

## 阶段 D：收尾校验

- [x] D1 `python -m pytest apps/control/tests/test_provider_console.py apps/control/tests/test_provider_console_management.py -q`
- [x] D2 `cd apps/web && npx tsc --noEmit`
- [x] D3 `python scripts/check_i18n.py --strict`
- [x] D4 汇总：动作矩阵、接口清单、测试结果、未做项（重试任务的取舍理由）

## 约束复述

- 不碰 `apps/control/app/adapters/**`
- 不做后台自动动作；动作必须显式点击 + 二次确认
- 导航 11 项 / 设置页 8 tab / 品牌 / 浮动按钮不动
- 不 `git add` / `commit` / `push`
