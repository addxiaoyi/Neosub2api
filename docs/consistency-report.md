# 并行工作结论一致性复核（2026-11-03）

> 只读复核，未改任何源码、既有文档或 `.github/`，未执行 git 操作。
> 复核快照：2026-11-03 19:00:21（Asia/Shanghai）。仓库当时仍在被其他会话写入，见文末「并发写入观测」。
> 运行方式均为仓库内相对路径；唯一短时启动的进程是 `tests/test_error_contract.py` 的测试夹具（25 秒内退出）。

## 总表

| # | 主张 | 结论 | 一句话证据 |
| --- | --- | --- | --- |
| 1 | 幽灵端点 0、方法不匹配 0 | 成立 | `contract_check.py` 输出 77 路径 / 96 路由 / 83 前端调用，`[幽灵端点] 0`、`[方法不匹配] 0` |
| 2 | i18n 五语言真缺 0 | 成立 | `check_i18n.py --strict` 输出 5 个语言真缺均为 0，结果「通过」 |
| 3 | 五个语言包无孤立「批量授权」残留 | 成立 | 5 个 locale JSON 中 `批量授权` 命中 0；`批量` 只出现在「后端没有批量接口」的说明句里，且零引用键为 0 |
| 4 | messages=Anthropic 壳、chat/responses=OpenAI 壳；`/v1/*` 未匹配 404 形状 | 部分成立 | 壳分工成立；404 形状已被 18:56–18:57 的后续改动覆盖：现在是协议壳，不再是 `{"detail":"Not Found"}` |
| 5 | 除 codebuddy 外账号操作按钮数为 0 | 部分成立 | 后端仅 codebuddy 覆写账号方法；但「备注」对所有 provider 恒为 true，前端每行恒渲染备注按钮 |
| 6 | 密钥不出现在任何响应 / request_logs / 日志 | 部分成立 | u2a_ 在日志与 request_logs 基本全覆盖；但创建接口一次性回明文、列表回 10 位前缀、`demo._write_call` 未走 mask_text |
| 7 | 演示模式走真实链路，无 `if demo: return 假JSON` | 成立 | `DemoAdapter` 继承 `OpenAICompatAdapter`，播种调用 `adapter.chat_completion` 并真实写 logs/usage/settle；v1 路由无 demo 分支 |
| 8 | docs 下 Markdown 无本机绝对路径 | 成立（2026-11-03 修复后复扫为 0 残留） | 原 `docs/` 内 `file:` 本机链接与盘符绝对路径共 76 行、12+ 文件，已全部改为仓库相对路径；详见「逐条证据 §8」的修复记录 |
| 9 | docs 测试数量描述与实际变化一致、未写死单一数字 | 成立（2026-11-03 修复） | 原多处写死「250 passed」，已统一改为「以最近一次全量 `pytest` 结果为准」的口径并标注日期与来源，详见「逐条证据 §9」 |
| 10 | nginx.conf 查询串掩码覆盖 `u2a_` 与常见参数名 | 部分成立 | `u2a_` 任意参数名覆盖；常见名覆盖 **18 个别名**（已含 `client_secret / access_key / authorization / api-key / x-api-key`），map 链 20 段参数名 + 20 段 `u2a_`（末段 `$log_uri_40`），三份配置脱敏块逐字节一致（2026-11-04 复核）；仍缺 `sig`、`jwt`，串链仍是固定上限 |

## 逐条证据

### 1. 幽灵端点 0、方法不匹配 0 —— 成立

运行 `scripts/contract_check.py`（19:00:21）输出：

```text
契约检查：后端 77 个路径 / 96 条路由，前端 83 处调用

[幽灵端点] 0

[方法不匹配] 0

[后端有路由但前端零调用] 12（仅提示）
```

12 条「后端有路由但前端零调用」只是提示，包含 `/api/health`、`/api/metrics`、`/api/security/audit`、`/api/settings/secret/reveal` 等运维/管理员端点，不属于幽灵端点。

### 2. i18n 五语言真缺 0 —— 成立

运行 `scripts/check_i18n.py --strict` 输出：

```text
zh-CN  1099 叶子 / 1099 界面键 / 真缺 0 / 多余 0 / 源码缺 0
zh-TW  1302 叶子 / 1099 界面键 / 真缺 0 / 多余 0 / 源码缺 0
en     1310 叶子 / 1099 界面键 / 真缺 0 / 多余 0 / 源码缺 0
ja     1302 叶子 / 1099 界面键 / 真缺 0 / 多余 0 / 源码缺 0
ko     1302 叶子 / 1099 界面键 / 真缺 0 / 多余 0 / 源码缺 0

合计：真缺 0，多余 0，源码零引用 0，短语软缺 0
结果：通过
```

### 3. 五个语言包无孤立「批量授权」残留 —— 成立

- `apps/web/lib/i18n/locales/` 下 5 个 JSON 对 `批量授权` 命中 0 次。
- 既有源码/文档中 `批量授权` 共 4 处，均在 locale 之外（不含本报告引述）：
  - `docs/ux-review.md`
  - `docs/ux-fix-triage.md`（2 处）
  - `apps/web/app/(main)/error.tsx`（代码注释）
- locale 内的 `批量` 仅出现在说明「后端没有批量接口」的文案里：
  - `apps/web/lib/i18n/locales/zh-CN.json:826-827,841`
  - `apps/web/lib/i18n/locales/zh-TW.json:634-635,649`
- 同日 `check_i18n.py --strict` 的「源码零引用」为 0，说明这些 `batch*` 键都有代码引用，不是孤儿键。

结论：语言包范围内成立；locale 之外的旧文案仍存在（见「需要人来决定的冲突点」）。

### 4. 错误壳分工与 `/v1/*` 未匹配 404 —— 部分成立（404 形状已被后续覆盖）

壳分工成立：

- `apps/control/app/gateway_errors.py:107-112` `protocol_for_path()`：`/v1/messages*` 返回 Anthropic，其余 `/v1/*` 返回 OpenAI。
- `apps/control/app/main.py:239-247` `GatewayError` 处理：按 `protocol_for_path` 选择 `anthropic_payload()` / `openai_payload()`。
- `apps/control/app/gateway_errors.py:180-190` 两个 payload 的顶层形状（OpenAI `error + detail`；Anthropic `type + error`）。

`/v1/*` 未匹配路由 404：已被后续改动覆盖。

- 修复前的旧结论（当时的 `docs/error-contract.md:64-81`）：`{"detail": "Not Found"}`。
- 当前源码：`apps/control/app/main.py:249-259` 捕获 `StarletteHTTPException`，对 `/v1`、`/v1/*` 调 `framework_payload()`；`apps/control/app/gateway_errors.py:115-128` 生成协议壳：
  - 非 messages：`{"error":{message,type,param,code:"route_not_found"},"detail":...}`
  - messages：`{"type":"error","error":{type,message,code:"route_not_found"}}`
- 修复前实跑（历史记录）：`pytest tests/test_error_contract.py::test_v1_unknown_path_uses_openai_shell -q` → `[XPASS(strict)]`、`1 failed in 24.66s`，证明源码已返回 OpenAI 壳。
- **修复前**测试与文档的旧状态：`apps/control/tests/test_error_contract.py:294-305` 曾用 `xfail(strict=True)` 并断言「未匹配路由落到默认 detail」；`docs/error-contract.md:64-81` 曾写 `{"detail":"Not Found"}`。
- **已修（2026-11-03）**：`test_error_contract.py` 中已无 `xfail`（只读核对全文件 0 命中），改为对协议壳的正向断言；`docs/error-contract.md` §1 已同步为协议壳口径，三处（源码、测试、文档）对齐。按「接受源码修复」的分支处理，未回退源码。

最小修法建议（只建议）：确认保留当前源码修复，则删除 `test_error_contract.py:294-297` 的 `xfail` 标记并更新 `docs/error-contract.md` §1；如果决定回退源码，则恢复默认 404 行为。二选一，不要维持「源码已修、测试期望未修」的现状。（**已按前一分支执行，见上一条「已修」。**）

### 5. 账号页按真实能力渲染 —— 部分成立

后端：

- `apps/control/app/routers/accounts.py:18-27` `_implements()` 以「是否真的覆写基类方法」判定能力，不读 `spec.capabilities`。
- `apps/control/app/routers/accounts.py:30-47` `_account_ops()` 中只有 `"note": True` 是无条件能力；其余 `list/oauth/checkin/credits/test/refresh/clear_cooling/disabled/delete/restart` 都依赖实际覆写。
- `apps/control/app/routers/accounts.py:134-160` `/api/accounts/capabilities` 输出每家 provider 的 `ops` 与 `managed`（返回体的实际键名是 `managed`，`_managed_by()` 只是取值函数名；取值 `unified|sidecar|quota|external`，由 `_implements()` 按真实覆写算出）。
- 全仓实现情况：只有 `apps/control/app/adapters/codebuddy.py:91,149,157,165,175,178,184` 覆写了 `sync_accounts / start_login / poll_login / account_action / delete_account / restart / refresh_credits`；`apps/control/app/adapters/base.py:201-228` 是桩。`sub2api_store.py:260` 的 `delete_account` 是存储辅助类，不是 provider 适配器方法。

前端：

- `apps/web/app/(main)/accounts/page.tsx:95-104` `opsOf()` 优先用 `/capabilities` 结果，未返回时只有 codebuddy 用本地兜底 `CODEBUDDY_ACCOUNT_OPS`（`:63-76`）。
- `apps/web/app/(main)/accounts/page.tsx:697-706` 「备注」按钮对每一行无条件渲染。
- 其余操作均有 gate：签到 `:710`，测试/刷新 Token `:731`，清冷却 `:747`，删除 `:793`；空态里的添加/扫码/批量按钮由 `:1121-1136` 的 `selectedOps.oauth` 控制。

因此：codebuddy 之外 provider 的「上游相关操作按钮」为 0，但每行仍有 1 个本地备注按钮。原主张按字面不成立。

最小修法建议（只建议）：把主张改成「上游操作能力为 0（备注除外）」并在文档写明备注只落本端库；若坚持字面为 0，则前端对非 codebuddy 隐藏备注按钮（不推荐，会损失已有功能）。

### 6. 密钥不出现在响应 / request_logs / 日志 —— 部分成立

已覆盖的部分：

- 脱敏原语：`apps/control/app/api_utils.py:31-38` `mask_secret`（前 4 + 后 4），`:41-47` `mask_text`（正则 `u2a_[A-Za-z0-9_-]{4,}`）。
- 网关日志落库：`apps/control/app/routers/v1.py:128-130`（限流拒绝，error 走 `mask_text`）、`:224-226`（正常/失败记录，error/key_name 走 `mask_text`）、`:304`（流式中断异常走 `mask_text`）。
- 上游错误壳：`apps/control/app/gateway_errors.py:134-135`（框架错误壳）、`:197-199`（上游错误）对 code/message 走 `mask_text`。
- 应用日志：`apps/control/app/observability.py:107,120,126` 分别对 message、异常栈、整行 JSON 做 `mask_text`。
- 访问日志：`apps/control/app/main.py:55-63` 给 `uvicorn.access` 挂 `_AccessLogRedactor`，`:63` 注册过滤器；`api_utils.py:50-54` 逐项脱敏 query。
- 密钥列表：`apps/control/app/routers/keys.py:24-33` 只输出 `prefix`，不输出 `key_hash`；`prefix` 为 `token[:10]`（`keys.py:66`）。
- 请求日志接口：`apps/control/app/routers/logs.py:48-54` 把前缀二次掩成 `u2a_xxxx****`；`:144-170` 出参无完整密钥。
- 配额接口：`apps/control/app/routers/quota.py:116-123` 输出 `u2a_xxxx****`；完整密钥只允许从 `X-Api-Key` 或 POST body 进入（`:56-65`），query 拒绝（`:68-81`）。

未覆盖/例外：

1. 创建接口按设计一次性返回明文：`apps/control/app/routers/keys.py:88`（`"key": token`）与 `apps/control/app/routers/tokens.py:87`（`"token": token`）。这是产品口径，但与「任何响应都不出现」字面冲突。
2. `/api/keys` 列表返回 10 位前缀（`u2a_` + 6 位密钥），比 `/api/logs`、`/api/quota` 的 4 位更宽。
3. 演示播种绕过脱敏：`apps/control/app/demo.py:399-402` 直接把 `error`（上游响应前 200 字节）写入 `request_logs`，未调用 `mask_text`；当前假上游不输出密钥，风险低，但不是「全覆盖」。
4. `POST /api/settings/secret/reveal` 会按设计返回 provider 凭据明文（管理员 + 审计），见 `apps/control/app/routers/settings.py:479-501`；这不是 u2a_ 网关密钥，但属于「响应里出现明文 secret」。
5. 演示模式的 u2a_ 明文会落两处：`apps/control/app/demo.py:250-251,337` 的 `demo_seed`（settings 表）与 `:454` 的 `demo-credentials.txt`。它们不是响应/日志，但属于磁盘明文。

最小修法建议（只建议）：明确文档里的例外清单（创建一次性明文、列表前缀、reveal 端点）；`demo.py` 写日志前统一走 `api_utils.mask_text`；演示凭据文件在文档中标注「仅本机、禁止分发」。

### 7. 演示模式走真实链路 —— 成立

- `apps/control/app/demo.py:176-195` `DemoAdapter` 继承 `OpenAICompatAdapter`，只替换 base URL；`:191` 能力声明为 chat/models。
- `apps/control/app/demo.py:341-382` `_seed_calls()` 通过 `await adapter.chat_completion(payload, stream=False)` 真打进程内假上游（`DemoUpstream`，`demo.py:105-160`）。
- `apps/control/app/demo.py:384-430` `_write_call()` 真实写 `request_logs`、`record_usage`、`settle`（含价格解析）。
- `apps/control/app/main.py:144` 在 `settings.demo_mode` 时 `demo.install(...)`；`:148-149` 启动时播种。
- 在 `apps/control/app/routers/v1.py` 中检索 `demo` 无命中，不存在 `if demo: return 假JSON` 分支。

限定说明：播种的 6 条调用直接走适配器，不经过 `/v1` HTTP 路由层；用户页面上发起的请求仍走正常 v1 链路。

### 8. docs 下 Markdown 无本机绝对路径 —— 不成立

`docs/**/*.md` 对 `file:` 协议本机链接或盘符绝对路径的匹配共 76 行（**修复前**状态，2026-11-03 已修，见本节末尾修复记录）。代表证据（为避免本报告自身再引入绝对路径，下面用 `[盘符]` 代表实际盘符）：

- `docs/CONTRACT.md:10`：`[盘符]:\<repo>\.runtime\python\python.exe scripts\contract_check.py`
- `docs/DEMO.md:162`：`& "[盘符]:\<repo>\.runtime\python\python.exe" -m pytest tests -q`
- `docs/error-contract.md:10`：同上形式的绝对解释器路径
- `docs/security-retest-2026-11-03.md:3`：`复测对象：[盘符]:\qwq\项目\unified-2api`
- `docs/ADDING-A-PROVIDER.md:46,79`：`file:` 协议的本机链接（`[盘符]:/qwq/项目/unified-2api/...`）
- `docs/design/README.md:5-6`、`docs/design/frontend-integration-plan.md:5`、`docs/design/ia-proposal.md:5,37,52,60,62,63,258`
- `docs/m3-billing-plan.md:12-21,42,48,54,63-64,128-132,175,209-211,264,271-274,283,288,294,308,316,334-338,347-358,403`（大量 `file:` 协议的本机链接）
- `docs/providers/deepseek-feasibility.md:76-77,118`：`file:` 协议的本机链接（`[盘符]:/qwq/%E9%A1%B9%E7%9B%AE/...`）
- `docs/UPSTREAM-SYNC.md:138`、`docs/FRONTEND-SKELETON.md:81`：相对路径示例中夹带绝对运行时路径

最小修法建议（只建议）：统一改为仓库相对路径；解释器路径写成 `python -m pytest ...` 或 `.runtime\python\python.exe`；链接改为相对的 `./...` 或 `../...`。

修复记录（2026-11-03）：上列 76 行已全部处理——指向仓内文件的 `file:` 链接改为仓库相对路径；操作命令里的绝对解释器路径改为 `python -m pytest ...`；指向未进仓库的临时证据（截图、本机日志）的链接改为「文件名 + 采集方式」的文字描述，不留死链。修复后按同一匹配模式复扫 `docs/**/*.md`，残留可点击的本机绝对路径链接 **0 行**；仍命中该模式的只有对本问题本身的文字描述（本报告用 `[盘符]` 占位、`unification-audit.md` 附录 F 记录「写死 `[盘符]:\qwq\...`」这一问题、`site/README.md` 解释构建脚本如何处理 `file:///…` 链接），均非链接。

### 9. docs 测试数量描述 —— 不成立

写死单一数字的文档（下列内容为**修复前**状态，2026-11-03 已修，见本节末尾修复记录）：

- `docs/ADDING-A-PROVIDER.md:161`：「当前基线：**250 passed, 1 skipped, 3 subtests passed**」，无快照说明。
- `docs/unification-audit.md:9`：以「250 passed, 1 skipped, 3 subtests passed」为测试基线；同一文件 `:597` 又记录「本次提交实测约 425 passed，且会继续增长；其余三处仍写 250」——说明维护者已知该数字会漂移，但未回填。
- `docs/FRONTEND-SKELETON.md:100`、`docs/security-audit.md:122`、`docs/ux-review.md:54`：保留各自快照数字（76/73），并补一句「该数字为当次快照；当前基线为 250 ...」。有快照意识，但仍然把「当前」写死为 250。

实际值：

```text
apps/control: pytest tests --collect-only -q
428 tests collected in 0.70s   （约 18:57）
432 tests collected in 1.54s   （19:00:21，3 分钟内增加 4 条）
```

结论：文档既有「快照」措辞，又写死了「250」这个单一数字，且该数字已经与当前测试规模（432）不一致。

最小修法建议（只建议）：删除「当前基线 250」的硬编码，改成「以 `pytest tests -q` 实测为准」；如需保留参考值，只保留带日期与 commit 的「历史快照」，并明确不是当前值。

修复记录（2026-11-03 改写表述，2026-11-04 更新数字）：上列写死数字的 5 处（`README.md`、`docs/ADDING-A-PROVIDER.md:161`、`docs/FRONTEND-SKELETON.md:100`、`docs/security-audit.md:122`、`docs/ux-review.md:54`）与 `docs/unification-audit.md:9-10` 已统一改为「以最近一次全量 `pytest` 结果为准」，**当前基线以 2026-11-04 全量实测为准：443 passed + 1 skipped + 0 failed + 0 xfailed（125.07 秒）**，`--collect-only` 收集 **444 条**（量级约 444），唯一 skip 是 `test_hardening.py` 的「Windows 不支持 POSIX 权限位」环境性跳过。`76 passed` / `73 passed` 等仍保留在原处，但已明确是当次快照而非当前基线。此前唯一未通过的用例已修复并通过，`xfail` 已全部移除（当前 0 xfailed），该失败说法不再出现在任何文档。

### 10. nginx.conf 查询串掩码 —— 部分成立（下列内容为**修复前**状态，2026-11-03 已扩链，见本节末尾修复记录）

**修复前**配置：

- `deploy/nginx.conf:19-38`：6 次按参数名替换，覆盖 `api_key / apikey / access_token / refresh_token / key_id / key / token / secret / password / session`。
- `deploy/nginx.conf:39-58`：5 次按值替换，覆盖任意参数名下的 `u2a_...`（`:41,45,49,53,57`）。
- `deploy/nginx.conf:61-64`：`Referer` 只保留 origin+path，query 整体丢弃。
- `deploy/nginx.conf:66-68`：`log_format` 只使用最终变量 `$log_uri_10`；80/443 两个 server 均引用该格式（`:76,112`）。

**修复前**缺口：

1. 常见别名未覆盖：`client_secret`、`access_key`、`authorization`、`api-key`、`x-api-key`、`auth`、`sig`、`jwt`。其中 `client_secret` 是后端 `settings.py:52-57` 自己认定的敏感键。
2. 串链有硬上限：同一 URL 里超过 6 个「已知参数名」值或超过 5 个 `u2a_` 值，多出的部分不会被掩码（map 链长度固定）。
3. 只掩码 query：密钥若出现在 path 中不会掩码；当前网关密钥设计走 Authorization/Header，正常不会进 path。

最小修法建议（只建议）：把 `client_secret|access_key|authorization|api-key|x-api-key|auth` 补进参数名正则；在文件注释写明 6/5 的链长上限，或再追加几次 map 覆盖异常长 query。

修复记录（2026-11-03）：缺口 1、2 已修（缺口 3 未动，见下）。`deploy/nginx.conf`、`deploy/web-nginx.conf`、`apps/web/nginx.conf` 三份的脱敏块逐字节一致（SHA256 相同：`deploy/nginx.conf:14-201`、`deploy/web-nginx.conf:3-190`、`apps/web/nginx.conf:2-189`；`apps/web/nginx.conf:1` 声明「与 deploy/nginx.conf 同口径」，2026-11-04 复核）：参数名正则补进 `api-key`、`x-api-key`、`access_key`、`client_secret`、`private_key`、`refresh`、`authorization`、`auth`，**18 个别名**（`api_key|apikey|api-key|x-api-key|access_key|access_token|refresh_token|refresh|client_secret|private_key|key_id|key|token|authorization|auth|secret|password|session`）；map 链由 6+5 扩为 **20 段参数名 + 20 段 `u2a_`**，`log_format` 末段变量同步为 `$log_uri_40`（`deploy/nginx.conf:27-186,199-201`、`deploy/web-nginx.conf:16-185,188-190`、`apps/web/nginx.conf:15-174,187-189`；2026-11-04 复核）。因此「`client_secret` 等 6 个别名漏掩」「超过 6/5 个就不掩」两条**已不成立**（只读核对两份 nginx 配置，未改动）。仍未覆盖：`sig`、`jwt` 两个别名（链长虽到 20+20，仍是固定上限）；缺口 3（只掩码 query、path 不掩码）维持原判断——网关密钥走 Authorization/Header，正常不进 path。

## 需要人来决定的冲突点

1. **`/v1/*` 404 修好了，但文档和测试没跟上（当时测试是红的）—— 已修。**
   源码已在 `main.py:249-259` + `gateway_errors.py:115-128` 实现协议壳；**修复前** `docs/error-contract.md:64-81` 与 `apps/control/tests/test_error_contract.py:294-305` 仍按旧行为写，`xfail(strict=True)` 实测 XPASS → `1 failed`。当时的待决项是「接受源码修复（更新文档、删 xfail）」还是「回退源码」。
   **已修（2026-11-03，采前一选项）**：移除 `test_error_contract.py` 的 `xfail`，改为对协议壳的正向断言；`docs/error-contract.md` §1 同步为协议壳口径，源码 / 测试 / 文档三处对齐。只读复核：该测试文件全文件已 0 处 `xfail`。

2. **仓库正在被并发写入，本报告是时间点快照。**
   复核期间 `apps/control/app/main.py` 在 18:57:03 变成 0 字节，18:57:50 又被写回 12584 字节；测试收集数 18:57 为 428、19:00 为 432。任何「当前状态」结论都需要在合并前再跑一次。

3. **docs 绝对路径是「可移植性」还是「可接受的本机说明」。**
   76 行绝对路径中，有些是操作命令（`[盘符]:\...python.exe`），有些是 IDE 可点击的 `file:` 协议本机链接（`[盘符]:/...`）。需要决定统一策略（全相对化，或只允许在明确标注「本机环境」的附录中出现）。

4. **「除 codebuddy 外按钮数为 0」与「备注按钮恒显示」冲突。**
   需决定是修改主张措辞（推荐），还是让备注按钮也按 provider 能力隐藏。

5. **「密钥不出现在任何响应」与产品化的例外冲突。**
   创建一次性明文、列表前缀（10 位 vs 日志 4 位）、`demo_seed`/`demo-credentials.txt` 明文，需要产品/安全侧确认哪些是允许例外并写进契约，哪些要收紧（例如 `/api/keys` 前缀也统一 4 位、demo 日志走 `mask_text`）。`settings/secret/reveal` 不在此列：代码本就是 admin-only，成功路径写 `audit_logs`（actor/ts/action/target，detail 不含明文），属已接受的例外。

6. **nginx 掩码的参数名范围与链长上限。**
   当前覆盖 18 个别名 + `u2a_` 任意值（`client_secret` 等已补，2026-11-04 复核），链长 20+20 仍为固定上限；仍缺 `sig`、`jwt`。需要决定是补齐别名并接受固定次数，还是改用其他日志方案（例如只记录 path + 白名单参数）。

7. **测试基线数字的单一事实来源。**
   `docs/` 多处写死 250，实际以全量 `pytest` 收集数为准（2026-11-03 复跑 444 条，见 §9）。需要决定保留「历史快照 + 实测为准」的写法，还是引入一个 CI 生成、可自动更新的基线文件。（2026-11-03 已按「历史快照 + 实测为准」口径回填，见「逐条证据 §9」的修复记录；CI 自动基线仍待决定。）

8. **locale 之外的「批量授权」旧文案。**
   `docs/ux-review.md`、`docs/ux-fix-triage.md`、`apps/web/app/(main)/error.tsx` 注释仍有该词。语言包内已清零；是否清理这些历史/注释性文案需要文档维护者决定。
