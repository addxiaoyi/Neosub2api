# 风险登记薄（RISK REGISTER）

把散落在各轮报告里的遗留风险收成一份可查的登记薄，每条都带证据和复现方式。**没关掉的风险不要写成已解决**；只有代码里能指到行号、且行为已验证的，才允许标「已收敛」。

- 核验基准：2026-11-05，当前工作区（含未提交改动）。证据均为 `文件:行号` 或可复制命令。
- 状态口径：
  - **未关闭**：风险仍在，或验证不完整；
  - **已收敛**：行为已经在代码里统一，作为正向结论留档，防止后续改回去；
  - **留档**：曾经误判，保留反例，防止后人重复踩坑。
- 本文件只做登记，不替代 `docs/BACKUP.md` 的操作手册、`docs/security-audit.md` 的审计结论与 `docs/dependency-audit.md` 的依赖体检。

## 汇总

| 编号 | 主题 | 状态 | 对外表述限制 |
| --- | --- | --- | --- |
| R-01 | 备份恢复六道关没有真实 CI 记录 | 未关闭 | 不得写成「CI 已验证六道关」 |
| R-02 | 14 行 npm moderate 全部保留，可安全升级数 = 0（2026-11-05 已更正：见 R-02 更正块） | 未关闭（已接受） | 不得写成「已修复」 |
| R-03 | TOTP secret 明文落库（恢复码实为哈希） | 未关闭（已接受） | 必须说明「拿到库文件即可算码」 |
| R-04 | Grok 注册执行器未配置时只有提示，无回退 | 未关闭 | 不得写成「任务会排队等人处理」 |
| R-05 | 日/月界统一走固定 UTC+8 偏移 | 已收敛 | 可作为正向结论引用 |
| R-06 | 日桶时区原判是测试口径错位 | 留档 | 不得再写成「生产按 UTC 切日」 |
| R-07 | 恢复「自检失败后回滚」分支无测试覆盖（2026-11-05 已更正：见 R-07 更正块，已覆盖 / 实测通过） | 已覆盖（实测通过，2026-11-05；原「未关闭」留档见 R-07） | 可写「回滚已演练」，依据是全量 `902 passed` 含三条失败/回滚用例 |
| R-08 | 运行中在线恢复的安全性（原「运行中拷库文件是否安全」待核条目） | 已收敛（仅单副本 + 本地盘） | 可写「带压 56 轮 100%、4850 次压测 0 失败」；不得推广到多副本 / NFS |

---

## R-01 备份恢复六道关没有在真实 CI 跑过

**证据（文件:行号）**

- `.github/workflows/ci.yml:66-131`：CI 的「备份恢复演练」直接调 `scripts/backup_core.py` 的 `backup` / `restore` CLI，再查关键表；**不经过 HTTP 端点**，所以不覆盖 admin 鉴权、`confirm=true`、文件名白名单、恢复前快照这四关。
- `.github/workflows/ci.yml:44-48`：pytest 步骤本可以覆盖 API 六道关，但 `apps/control/tests/test_backup_api.py` 当前是**未跟踪文件**（`git status --short` 显示 `?? apps/control/tests/test_backup_api.py`），不会进入任何已推送提交。
- `.github/workflows/ci.yml` 工作区有 `+98 / -1` 未提交改动（`git diff --stat -- .github/workflows/ci.yml`）。
- `gh run list` 最近一次成功的 CI 是 `2026-11-03T09:10:09Z`（run `41608813707`），早于上述工作区改动。
- 六道关代码位置：`apps/control/app/routers/backup.py:164` / `:191` / `:227`（admin-only）；`:230-231`（`confirm`）；`:232-233`（文件名白名单）；`:242`（调 `core.load_archive`）；`:259` + `:296-304`（恢复前快照）；`:331-362`（原子替换 + 失败回滚）。
- 归档校验的实际实现：`scripts/backup_core.py:567-592`（解压 → manifest 存在 → 创建时 integrity_check → 成员 sha256 → 解出库的 `PRAGMA integrity_check`），成员比对在 `:525-550`。
- API 用例自称真实 sqlite、真实 `backup_core`：`apps/control/tests/test_backup_api.py:1-5`。

**（2026-11-05 复核，原结论不变）**

`git status --porcelain -- apps/control/tests/test_backup_api.py scripts/backup_api_smoke.py .github/workflows/ci.yml` 实测输出 `M .github/workflows/ci.yml` / `?? apps/control/tests/test_backup_api.py` / `?? scripts/backup_api_smoke.py`。结论仍然成立：两个用例文件都未入库，CI 改动未提交，所以「六道关没有真实 CI 记录」不因本地 902 passed 而改变；另新增一个未跟踪文件 `scripts/backup_api_smoke.py`。**这条为什么重要**：本地全量通过容易被误读成 CI 已验证，而这两个 `??` 说明证据尚未进入任何已推送提交。

**（2026-11-05 已更正，针对上面证据第 5 条的 `:331-362`）**

该条写的「原子替换 + 失败回滚」已不成立：`backup.py:341-362` 现在是 `_backup_into`（SQLite 在线备份），回滚快照在 `:328-338`，应用在 `:382-398`，回滚在 `:400-407`；`os.replace` 只出现在注释（`:344-345`）。**误判后果**：按旧指针去找 `os.replace` / 临时文件会一无所获，还会把六道关里的「回滚关」当成文件替换逻辑来验证，验证方向和真实失败面错位。

**复现 / 触发条件**

1. 把 `apps/control/tests/test_backup_api.py` 与 `ci.yml` 的改动提交并推送，开 PR（`ci.yml:3-7` 只对 `push main` / `pull_request` / `workflow_dispatch` 生效）。
2. 看 PR 的 CI 日志里是否出现 `tests/test_backup_api.py` 的用例名；当前分支 `baseline/unified-2api` 尚未包含这些改动。
3. 本地等价命令：`cd apps/control && python -m pytest tests/test_backup_api.py -v`（需 conftest 起进程，注意端口占用）。

**影响**

六道关目前只有本地结论，没有任何一次真实 CI 记录。把「已验证」直接写进发布说明，属于未经验证的表述；首次真实 CI 之前，故障注入、权限与回滚都只能算推理。

**建议动作**

提交测试与 CI 改动后开 PR，并让 CI 日志保留用例名；跑通前本条目保持「未关闭」。若长期不提交，至少在发布说明里写明「备份恢复仅本地验证」。

---

## R-02 14 行 npm moderate 全部保留，可安全升级数 = 0

**（2026-11-05 已更正）**

- 计数：本轮重跑 `npm audit --registry=https://registry.npmjs.org` 实测为 **6 行 moderate（0 high / 0 critical）**，不是 14 行。结构判断仍成立——1 条真实 advisory（`minimatch` / `GHSA-g3hv-75wg-m249`，区间 `>=7.0.0 <11.0.1`，命中 `@typescript-eslint/typescript-estree/node_modules/minimatch@10.2.6`）+ 5 行 via 级联（`@typescript-eslint/typescript-estree`、`parser`、`utils`、`type-utils`、`eslint-plugin`）。标题里的「14 行」及下方「14 行 = 1 + 13」作废，本节其余原文作为 2026-11-04 的历史记录保留。
- 机制表述：「minimatch v11 是纯 ESM」不准确——11.0.4 的 `package.json` 是 `type: module`，但 `exports` 同时给出 `require → ./dist/commonjs/index.js`，自带 CJS 构建。准确表述是：**ESM 入口只有命名导出，而 `@eslint/eslintrc@3.3.7` 的 `lib/config-array/override-tester.js:23` 用 `import minimatch from "minimatch"` 的 default import**，链接期抛 `SyntaxError: The requested module 'minimatch' does not provide an export named 'default'`。补充事实：该阻断**不是 v11 专属**，强制 `10.2.6` 同样崩（同一条报错）。
- 可升级性：「可安全升级数 = 0」**已被推翻并于本次落地**。收窄作用域的 overrides 方案（仅 `@typescript-eslint/typescript-estree` → `minimatch 11.0.4`，不碰根级 minimatch）实测：lock 解析为根 `minimatch 3.1.5` + typescript-estree 下嵌套 `11.0.4`；`npm audit` = 0 vulnerabilities；`npx tsc --noEmit` 0 错；`npm run build:export` 成功导出 18 个静态页；`npx eslint .` 正常启动并跑完、无链接期崩溃，退出码 1 仅来自 `next-env.d.ts`（Next 自动生成、未入库）上那 1 条既有 `@typescript-eslint/triple-slash-reference` error——改动前后输出逐行一致，属既有存量、与实际门禁无关（`npm run lint` 实测 exit 0、0 error、仅 3 条 warning），据此不为它加 ignore。根级 `"minimatch": "11.0.4"` 仍然启动期崩，不可用。
- npm 自身建议：现行 `npm audit fix`（含 `--force`）的方案是把 `@typescript-eslint` 8.70.0 降到 **6.15.0**，且干跑后 audit 仍报 6（修不掉）。「建议版本是降级、不可用」成立，但具体版本不是原文写的 `eslint@4.0.0`，更正为 `@typescript-eslint@6.15.0`。
- 因此下方「建议动作」中「保持 `package.json` / `package-lock.json` 原样」在 2026-11-05 之后不再适用；复查条件中的「`@eslint/eslintrc` 移除 default import」一条仍待上游发生。

**证据（文件:行号）**

- 依赖声明：`apps/web/package.json:64`（`@eslint/eslintrc ^3`）、`:70-71`（`eslint ^9`、`eslint-config-next 15.5.29`）。
- default import 的出处：`apps/web/eslint.config.mjs:3` 的 `FlatCompat` 来自 `@eslint/eslintrc`。
- 锁文件里的实际版本：`apps/web/package-lock.json:6958-6962`（根 `minimatch 3.1.5`）、`:3440-3441`（`@typescript-eslint/typescript-estree` 内嵌 `minimatch 10.2.6`）。
- 审计记录：`docs/dependency-audit.md:59-69`——14 行是 **1 个真实 advisory（minimatch `GHSA-g3hv-75wg-m249`）+ 13 行 via 级联**；三条升级路径均实证阻断，结论为「接受并留档」。
- CI 不拦 moderate：`.github/workflows/ci.yml:171-178` 的 `npm audit` 门是 `--omit=dev --audit-level=high`，只拦 high/critical。

**复现 / 触发条件**

```powershell
cd apps/web
npm audit --registry=https://registry.npmjs.org     # 当前应报 14 行 moderate
```

在临时副本里硬钉 `minimatch@11` 后跑 `npx eslint .`，`@eslint/eslintrc` 的 default import 会因 v11 纯 ESM 无 default 导出而在启动期抛 `SyntaxError`（评估记录见 `docs/dependency-audit.md:67`）。npm 按 advisory range 给出的 `eslint@4.0.0` 是**降级**，不可用——这一条本轮未重跑 `npm audit` 核实，登记为「未核实」。

**影响**

仅 dev/lint/构建链，生产依赖与静态产物零暴露；DoS 需要攻击者控制 glob pattern，而 pattern 来自手写 eslint 配置，无外部输入通路。不会影响运行时，但会让「0 漏洞」的说法不成立。

**建议动作**

保持 `package.json` / `package-lock.json` 原样，不执行 `npm audit fix`。满足 `docs/dependency-audit.md:71-75` 任一复查条件时重新评估。

---

## R-03 TOTP secret 明文存 SQLite；恢复码是哈希，不是明文

**证据（文件:行号）**

- 设计取舍写在模块头：`apps/control/app/totp.py:1-8`——「密钥用 base32 明文存库，理由是控制面库里本来就明文存上游密钥与会话，库文件已 0600」。
- 落库动作：`apps/control/app/routers/auth.py:379-383` 把 `totp_secret = pending` 直接写进 `users`；`pending` 来自 `:337-341` 的明文 `new_secret()`。
- 列定义：`apps/control/app/db.py:301-302`（`totp_secret` / `totp_pending_secret` 均为 `TEXT`）。
- **更正**：恢复码不是明文。`apps/control/app/routers/auth.py:373-378` 存的是 `hash_recovery_code(code)`；`apps/control/app/totp.py:92-94` 走 `hash_password`，`apps/control/app/security.py:10-21` 是 PBKDF2-HMAC-SHA256 / 200,000 轮；表注释也写明「hash 存」：`apps/control/app/db.py:28-35`。
- 同一存储口径的其他明文：`apps/control/app/db.py:265-276` 的 `provider_credentials.secret` 注释为「上游密钥（明文落库，理由见交付说明）」；库文件权限在 `apps/control/app/db.py:350-353`（S-05）创建时设一次；管理员口令文件同类处理：`apps/control/app/config.py:49-52`。

**复现 / 触发条件**

拿到 `data/unified.db`（本机其他用户、备份包、误提交、容器卷泄漏）后：

```powershell
sqlite3 data/unified.db "select username, totp_secret from users where totp_enabled=1"
```

用任意 RFC 6238 工具（30 秒步长 / 6 位 / SHA1）即可离线生成当前验证码。

**影响**

- 拥有库文件的人可以**直接算出 TOTP**，第二因子对这类攻击者不成立；不需要恢复码。
- 恢复码因是 200k 轮 PBKDF2 哈希，不能直接读出使用；但库内还有 `password_hash` 与上游 `secret` 明文，库文件的暴露面远大于 2FA。
- Windows 上 `chmod` 只控只读位，0600 主要靠数据目录 ACL/单用户环境兜底。

**建议动作**

这是有意的存储取舍，不在本轮改成自研加密。若威胁模型包含「库文件外流」，应改为外部 KMS / 信封加密，并对备份包做同等加密与访问控制；在此之前，文档统一口径为「库文件即最高机密」，不要单独宣称「TOTP 已加密保护」。

---

## R-04 Grok 执行器未配置时只有黄条提示，无回退路径

**证据（文件:行号）**

- 前端只判断状态并渲染提示：`apps/web/components/common/settings/GrokRegisterTasks.tsx:191` 读 `worker.executor === 'unconfigured'`；`:232-238` 只是 `TriangleAlert` 提示条。创建按钮在 `:218-229` 仍可用。
- 后端仍然允许入队：`apps/control/app/routers/grok.py:26-35`。
- 执行时才失败并给出原因：`apps/control/app/services/grok_register.py:278-286`（`RuntimeError('未配置注册执行器…')`）；`:174` 暴露 `executor` 状态。
- 适配器健康态与说明：`apps/control/app/adapters/grok_register.py:26-29`、`:43`。
- 可插拔入口已存在但未接默认实现：`apps/control/app/services/grok_register.py:179-180` 的 `set_executor`。

**复现 / 触发条件**

不设 `GROK_REGISTER_EXECUTOR_URL` 启动控制面（默认 `apps/control/app/config.py:137` 为空），在服务商配置区创建任意注册任务。任务会入队、被 claim，然后以「未配置注册执行器」失败。

**影响**

UI 只给提示，不阻止创建；用户会得到一批必然失败的任务。主仓不内置浏览器自动化，也没有本地兜底执行器，因此「有任务面板」容易被误读为「能跑注册」。

**建议动作**

二选一：未配置时禁用创建按钮并给配置指引；或接一个显式的本地/外部执行器（`set_executor` 已有接口），在 `docs/DEPLOYMENT.md` 写明执行器是外部依赖。当前保持「只有提示」时，不要把这个面板当作可用功能对外说明。

---

## R-05 日/月界统一走固定 UTC+8 偏移（已收敛）

**证据（文件:行号）**

- 唯一入口：`apps/control/app/billing.py:22-24` 定义 `WINDOW_TZ_OFFSET = 8 * 3600`，注释写明「Asia/Shanghai 无夏令时；不引入 zoneinfo：Windows 上缺 tzdata 时 IANA 时区会直接查不到」。
- 实现：`apps/control/app/billing.py:376-391` 的 `period_bounds`，日界取本地零点、月界取 1 号零点。
- 复用点：
  - `apps/control/app/routers/stats.py:39-46`（今日/近 7 天 + 日期标签）；
  - `apps/control/app/routers/security.py:34-36`（当日安全统计）；
  - `apps/control/app/demo.py:460` 与 `:502`（演示账本分桶）；
  - `apps/control/app/billing.py:500-502`（配额重置时间的展示）。

**复现 / 触发条件（防止回归）**

任何新增「今日/本月/近 7 天」需求，若自己写 `date.today()`、`datetime.now()` 或 `ts // 86400` 分桶，就会在服务器 `TZ=UTC` 时漂移 8 小时，或在 Windows 缺 `tzdata` 时让 `ZoneInfo('Asia/Shanghai')` 抛 `ZoneInfoNotFoundError`。

**影响**

当前口径已统一：日/月界、日期标签、配额重置展示都走同一个固定偏移。这是正向结论，不是风险。

**建议动作**

保持 `period_bounds` 为唯一天/月界入口；需要新周期时先扩展这里，不要在调用点复制算法。

---

## R-06 日桶时区的原判是测试口径错位（留档）

**证据（文件:行号）**

- 修正后的测试：`apps/control/tests/test_demo_mode.py:168-184` 用 `period_bounds("day", row_ts)` 分桶（`:182`），并把旧的 `row_ts // 86400` 分桶留在 `:183-184` 作为反例：同一批数据按 UTC 日桶只有 6 个桶。
- 生产侧一直按本地日界：`apps/control/app/demo.py:460`、`:502`。
- 时区常量：`apps/control/app/billing.py:22-24`。

**复现 / 触发条件**

`cd apps/control && python -m pytest tests/test_demo_mode.py::test_ledger_day_buckets_hold_at_local_morning -q`。测试取本地 07:30（UTC 前一日 23:30），只有按 UTC+8 日界才能稳定得到 7 个桶。

**影响**

当年的「演示账本少一天」不是生产按 UTC 切日，而是**测试自己用 UTC 日桶**。把这条误判当生产缺陷去改 `demo.py` 或 `billing.py`，会把已经正确的日界改坏。

**建议动作**

遇到「日界不对」的报障，先确认报障代码是否调用 `period_bounds`；只有 `period_bounds` 本身算错，才动生产实现。

---

## R-07 恢复「自检失败后回滚」分支没有测试覆盖

**证据（文件:行号）**

- 回滚实现：`apps/control/app/routers/backup.py:356-362`——`core.selfcheck` 不通过时调 `_restore_bytes` 写回替换前的库，再回 500。
- 回滚取字节：`apps/control/app/routers/backup.py:314-322`。
- 测试现状：在 `apps/control/tests/test_backup_api.py` 检索 `selfcheck|回滚|rollback` 只命中成功路径断言 `:274`；`test_restore_rejects_invalid_targets`（`:185-251`）覆盖的是**替换前**被 `load_archive` 拦下的场景，没有构造「归档校验通过、替换后自检失败」的用例。

**（2026-11-05 已更正，针对上面三条证据）**

- `_restore_bytes` 已不存在：`backup.py:400-407` 的 `_rollback_db` 用反向 SQLite 在线备份（`_backup_into`，`:341-362`）把一致性快照写回活库，再用 `_ensure_wal`（`:365-379`）把 `journal_mode` 钉回 `wal`。
- 「回滚取字节」的 `:314-322` 现为 `_snapshot_live`（`:328-338`），快照走 `VACUUM INTO` / `Connection.backup`（`scripts/backup_core.py:144` 起），不再是字节拷贝。
- 能力边界（别把「回滚」读成「字节级还原」）：应用前失败，活库与 `-wal`/`-shm` 逐字节不动（用例 `apps/control/tests/test_backup_api.py:382-416`）；应用中途失败或应用后自检不过时，回滚后内容一致（integrity / 结构 / 只增表下限），但库文件字节已与恢复前不同，快照之后的并发写会丢失（窗口为单轮恢复时长，实测 231–390 ms）。要中途也字节级还原，只能退回「停服务 + 换文件」。
- **这条为什么重要**：按 `_restore_bytes` 找回滚会找不到代码；把回滚当字节级还原，会在中途失败后拿旧库 sha256 比对，把正常的在线回滚误判成损坏。

**复现 / 触发条件**

需要构造一个能通过 zip/manifest/sha256/integrity_check、但过不了 `core.selfcheck`（关键表/行数/索引等）的归档；当前测试集没有这条路径，只能靠人工注入。

**影响**

原子替换成功之后才发现问题的那条兜底路径（写回字节快照）没有自动化证据；真出问题时，「回滚已演练」不成立。数据库损坏场景恰恰最需要这条路径可靠。

**（2026-11-05 已更正，针对上面「写回字节快照没有自动化证据」）**

- 现状已变：工作树里已有失败与回滚用例——应用前拦截（`apps/control/tests/test_backup_api.py:382-416`）、应用中途失败后的在线回滚（`:419-464`，断言 `_backup_into` 被调两次、`LATE_USER` 仍存在、`integrity_check=ok`、`journal_mode=wal`）、有打开句柄时恢复（`:467` 起）。本轮只做代码阅读、未执行测试，所以「已覆盖」仍需一次真实运行后由维护者裁决。
- **这条为什么重要**：继续沿用「回滚分支零覆盖」会在发布说明里重复一条已不成立的缺口，也会让真正剩余的缺口（回滚自身再失败、NFS 多副本）被这条噪音盖住。

**建议动作**

补一个自检失败用例：用真实归档改掉某个关键表/索引再恢复，断言响应 500、原库仍可读、恢复前快照仍在。跑通后可把本条降级为「已覆盖」。

**（2026-11-05 已更正）**

- 建议动作已完成，并且已真实跑过：`apps/control/tests/test_backup_api.py:382-416` 就是这条用例（只改 manifest 的 `tables` 行数、其余归档校验全过，断言 500、活库 `db/-wal/-shm` 字节未变）。本轮权威验证：`test_backup_api.py` 定向 **8 passed in 21.32s**，后端全量串行 **902 passed, 1 skipped, 2 warnings, 3 subtests passed in 314.34s**，三条失败/回滚用例（应用前拦截、应用中途失败回滚、带打开句柄恢复）都在其中。
- 因此 R-07 由「未关闭」结案为「已覆盖，实测通过」：`test_backup_api.py:382-416`、`:419-464`、`:467` 起均通过，「回滚已演练」现在有真实运行证据。
- **这条为什么重要**：继续把 R-07 挂在未关闭，会让发布说明重复一条已经闭环的缺口；而这里真正的剩余风险只剩「回滚自身再失败」与「NFS 多副本」，不该被这条噪音盖住。

---

## R-08 运行中在线恢复的安全性（原「待核」条目，2026-11-05 实测闭环）

**原结论（2026-11-05 之前，逐字留档）**

- 恢复期间没有请求静默（quiesce）机制。`scripts/backup_core.py:618-623` 的注释明确「前提是服务已停止（Windows 上打开中的文件也拷贝不了）」，而 API 路径在运行中直接 `shutil.copy2` 库与 WAL/SHM（`apps/control/app/routers/backup.py:307-311`），随后 `os.replace`（`:331-345`）。并发请求或 Windows 打开句柄导致的失败未实测；代码上的兜底是替换失败保持原库（`:340-344`）、自检失败回滚（`:356-362`），但回滚自身失败时的最终状态也没有测试证据。

**（2026-11-05 已更正）**

- 这段描述的 API 路径已不存在：`shutil.copy2` 与 `os.replace` 已从 `backup.py` 移除，恢复改为 SQLite 在线备份（`backup.py:341-362`），不需要请求静默，也不再受 Windows 打开句柄限制。
- 实测闭环：同一探针带压 56 轮，成功率由 **2/56（3.6%）** 提升到 **56/56（100%）**；4850 次压测请求 0 失败；恢复后 `journal_mode` 57/57 均为 `wal`。证据：`tmp/quiesce-probe/results_load56-fix.json`（`rounds=56`、`stress_events=4850`、`stress_failed=0`，57 个采样点全为 `wal`）、`tmp/quiesce-probe/results_idle-fix.json`（空载 12 轮同样全部 200 且 `wal`）。`tmp/` 已 gitignore，证据需现场重跑才能复现。
- 修复前的 2/56 基线结果文件没有保留在 `tmp/`（同系列的 `data-load/` 只剩库与备份），引用基线时以本条数字为准，不要引用具体文件名。
- 边界不变：实测只覆盖「单副本 + 同一文件系统 + 本地磁盘」；多副本共享卷 / NFS 下的锁语义仍未验证（见待核下一条）。
- **这条为什么重要**：继续把「运行中拷库文件不安全」当现行结论，会让人在管理界面恢复前白停一次服务，或在 Windows 上误判恢复必失败；反过来把 56/56 推广到 NFS 多副本，会把没测过的部署形态说成已验证。

---

## 待核（不进正文，拿到证据再补条目）

- 原「运行中拷库文件是否安全 / 恢复期间没有静默机制」条目已于 2026-11-05 实测闭环，原文逐字留档见 R-08；下方只留仍待核的条目。
- `backup.lock` 的跨进程互斥只在同一文件系统上可靠；多副本共享卷/NFS 语义下的行为未验证（`apps/control/app/routers/backup.py:67-89`）。
