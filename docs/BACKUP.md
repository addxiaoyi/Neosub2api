# 备份与恢复

结论：Windows 与 Linux 共用一份实现 `scripts/backup_core.py`，四个封装脚本只做参数翻译。
备份产出单文件 zip 归档，恢复先校验归档、再留存当前库、再替换、最后跑只读自检，失败自动回滚。

**（2026-11-05 已更正）**

「留存当前库 → 替换 → 只读自检 → 失败回滚」现在只描述 **CLI 路径**（`scripts/restore.sh` / `restore.ps1` → `scripts/backup_core.py`），且要求先停服务。管理界面的 `POST /api/system/restore` 走另一条路径：SQLite 在线备份（`apps/control/app/routers/backup.py:341-362`），不停服务也能恢复，失败时回滚到一致性快照而不是字节快照；详见下文「恢复：管理界面（HTTP）」。**这条为什么重要**：把顶部这句当成全部恢复方式的定义，会以为所有恢复都必须停机、都以文件替换结束，从而在管理界面恢复时白停一次服务，或在排障时去找并不存在的 `os.replace`。

## 归档内容

`data/backups/unified-backup-<YYYYmmdd-HHMMSS>.zip`：

| 成员 | 内容 |
| --- | --- |
| `manifest.json` | 版本号、时间戳、每成员 sha256、SQLite 版本/页大小、每表行数、demo 剔除记录 |
| `db/unified.db` | SQLite 在线快照（`VACUUM INTO`，不可用时退化 `Connection.backup`），不是直接拷贝正在写的库 |
| `config/` | `model-aliases.yaml`、`prices.yaml` |
| `providers/` | 各 provider 的 README 与 compose 片段 |
| `upstreams/` | `upstreams.yaml`、`README.md` |

归档旁另有 `<归档>.sha256`（sha256sum 格式），是归档本身的信任锚点。

manifest 关键字段：

| 字段 | 用途 |
| --- | --- |
| `app_version` | 取自 `apps/control/app/__init__.py` |
| `created_at` / `created_at_epoch` | 归档时间 |
| `database.sha256` / `size_bytes` | 数据库快照指纹 |
| `database.integrity_check` | 快照生成时的 `PRAGMA integrity_check` |
| `tables` / `totals` | 每表行数与总行数，恢复后逐表比对 |
| `demo.excluded` / `demo.found_rows` / `demo.removed_rows` | 演示数据处理结果 |

## 备份

```powershell
powershell -File scripts\backup.ps1
powershell -File scripts\backup.ps1 -Keep 7
powershell -File scripts\backup.ps1 -UseContainer     # 数据在命名卷时，容器内做快照
```

```bash
./scripts/backup.sh
./scripts/backup.sh --keep 7
PYTHON=/usr/bin/python3 ./scripts/backup.sh --db /var/lib/unified/unified.db --out /var/backups/unified
```

| 参数（ps1 / sh） | 默认 | 说明 |
| --- | --- | --- |
| `-DbPath` / `--db` | `<repo>/data/unified.db` | 源库 |
| `-OutDir` / `--out` | `<repo>/data/backups` | 归档目录，已被 `.gitignore` 覆盖 |
| `-Keep` / `--keep` | 14 | 保留份数，`pre-restore-*` 快照单独同预算轮转 |
| `-IncludeDemo` / `--include-demo` | 关闭 | 保留演示数据 |
| `-UseContainer` | 关闭 | 仅 ps1：在 control 容器内 `VACUUM INTO`，再 `docker compose cp` 出来打包 |

输出包含归档路径、归档 sha256、数据库 sha256、每表行数、demo 剔除数量、配置成员数与应用版本。

只校验不恢复：

```bash
python3 scripts/backup_core.py verify --archive data/backups/unified-backup-20261103-190037.zip
```

## 恢复：CLI 路径

```powershell
powershell -File scripts\restore.ps1 -Latest
powershell -File scripts\restore.ps1 -Archive data\backups\unified-backup-20261103-190037.zip
powershell -File scripts\restore.ps1 -Latest -WithConfig -ReadyUrl http://127.0.0.1:8010/api/health/ready
```

```bash
./scripts/restore.sh --latest
./scripts/restore.sh --archive data/backups/unified-backup-20261103-190037.zip
./scripts/restore.sh --latest --with-config --ready-url http://127.0.0.1:8010/api/health/ready
```

顺序（任一步失败就停止，不碰目标库）：

1. **校验归档**：若 `.sha256` 存在，先比对归档本身；再逐个成员比对 manifest 里的 sha256、大小；对解压出的库再跑一次 `PRAGMA integrity_check`。
2. **留存现场**：当前库与 `-wal` / `-shm` 原样拷贝到 `data/backups/pre-restore-<时间戳>/unified.db.bak`（含 `pre-restore.json` 记录指纹）。
   这里刻意不用在线快照，因为“库已经坏了”正是最常见的恢复场景。
   目标库本来就不存在时跳过这一步：输出 `rows before restore: <unreadable/absent>`，也不生成 `pre-restore-*` 目录——没有现场可留，属正常分支，不是失败。
3. **原子替换**：解压出的库写到临时文件，`os.replace` 覆盖目标库，再删除旧库残留的 `-wal` / `-shm`。（仅 CLI 路径；管理界面不换文件，见下节「恢复：管理界面（HTTP）」）
4. **只读自检**（`mode=ro`）：

| 检查项 | 判定 |
| --- | --- |
| 可查询 | `SELECT 1` |
| 关键表 | `users / sessions / providers / accounts / api_keys / request_logs / usage_records / settings / audit_logs / provider_credentials / billing_ledger / price_books / quota_policies` 全部存在 |
| 表数量 | 与 manifest `totals.tables` 一致，无缺表/多表 |
| 每表行数 | 与 manifest `tables` 逐表一致 |
| 管理账号 | `users` 里至少 1 个 `role='admin'` |
| 完整性 | `PRAGMA integrity_check = ok` |
| 演示数据 | 归档声明 `demo.excluded=true` 时，`[demo] ` 密钥与 `demo_local` 行必须为 0 |
| 外键 | `PRAGMA foreign_key_check`，仅记录，不作为失败条件 |

**自检的时序前提**：结论只对「刚恢复完、控制面还没启动」的库成立。控制面启动时 `registry._seed()` 会播种内置 provider（实测 `providers` 1 → 14），库文件 sha 随之改变，此后再拿同一份 manifest 跑自检会报 `row_counts` drift（如 `providers: manifest=1 actual=14`）并判 `ok=False`。要复核就趁启动前；启动后再跑属误用，不是恢复出了问题。

5. **失败回滚**：自检不通过时把 `unified.db.bak` 及其 `-wal` / `-shm` 拷回目标路径，打印回滚目录，退出码 2。（仅 CLI 路径；管理界面的回滚是一致性快照的在线写回，不是字节拷贝）
6. **可选就绪探针**：`--ready-url` 轮询 `/api/health/ready`，要求 `checks.database.ok` 为 true。

`-WithConfig` / `--with-config` 才会覆盖仓库的 `config/`、`providers/`、`upstreams/`；默认只恢复数据库，避免用旧配置覆盖当前部署。

## 恢复：管理界面（HTTP）

控制面在跑时用这条路径，不必停服务。端点行为与逐条校验的实现位置见 [API-REFERENCE.md](API-REFERENCE.md) 的「备份与恢复（`/api/system/*`）」一节；本节只写操作面与能力边界，不复制那份锚点。

| 端点 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /api/system/backups` | 管理员 | 列出归档：`name` / `size_bytes` / `created_at` / `has_checksum` |
| `POST /api/system/backup` | 管理员 | 立即备份，返回 `{ok, name, size_bytes, duration_ms, sha256, created_at}` |
| `POST /api/system/restore` | 管理员 | body `{"name": "...", "confirm": true}`；`confirm` 缺省或 false → 400 |

恢复门槛（`apps/control/app/routers/backup.py:227-238`）：`confirm=true`；`name` 必须命中 `ARCHIVE_RE` 白名单，不接受任意路径；归档必须存在；同一时间只允许一个备份/恢复，占锁失败 409。

一次调用里走完的六道关：

1. `core.load_archive`：zip 可读 + manifest 结构 + 成员 sha256/大小 + 解出库的 `PRAGMA integrity_check`（`backup.py:243`）。
2. `_verify_archive`：在静止的归档库上跑 `core.selfcheck`，逐表行数与 manifest 严格相等（`backup.py:315-325`，调用点 260）。行数漂移在这里就被拦下。
3. `_restore_pre_snapshot`：先给当前库出一份完整归档，失败即中止（`backup.py:298-306`，调用点 261）；响应里的 `pre_restore_backup` 就是它。
4. `_snapshot_live`：给活库出一致性回滚快照，放在临时目录（`backup.py:328-338`，调用点 262）。
5. `_backup_into` + `_ensure_wal`：SQLite 在线备份把归档页写进活库，再把 `journal_mode` 钉回 `wal`（`backup.py:341-362`、`:365-379`）。
6. `_structure_check`：表集合、只增表行数下限、admin 账号、`integrity_check`、`foreign_key_check`、demo 残留（`backup.py:410-467`）；不过就 `_rollback_db`（`:400-407`）。

成功响应：`{ok, restored, path, pre_restore_backup, pre_restore_path, duration_ms, selfcheck_ok, rows_after}`，并写 `audit_logs`（action `backup.restore`）。

**能力边界**（和 CLI 的差别）：

- **应用前失败**（第 1–4 步任一步）→ 活库与 `-wal`/`-shm` 逐字节不动，恢复直接中止。
- **应用中途失败**（第 5 步中断，或第 6 步不过）→ 回滚后内容一致（integrity + 结构 + 只增表下限），但**文件字节不再与恢复前逐字节相同**；一致性快照时刻之后的并发写会丢失，窗口约 231–390 ms（实测 `tmp/quiesce-probe/results_load56-fix.json`、`results_idle-fix.json`，`tmp/` 已 gitignore）。
- 要做到应用中途也字节级还原，只能回到「停服务 + 换文件」，也就是 CLI 路径。
- 自检能抓：归档与清单不一致、缺表/多表、表集合层面的 schema 漂移、半截恢复、库结构损坏、外键违规、admin 账号缺失、演示残留；抓不住：行数相同的内容级篡改、非只增表在窗口内的并发增删、归档生成时就已缺失的历史数据、列级与索引级的 schema 漂移。
- API 恢复只写数据库，不带 `config/`、`providers/`、`upstreams/`；要连配置一起恢复，用 CLI 的 `--with-config`。

**两条路径怎么选**：

| 场景 | 用哪条 |
| --- | --- |
| 控制面在跑，只想回到某个归档 | 管理界面：不停服，自动留恢复前归档，写审计 |
| 库已损坏、在线快照都出不来 | CLI：`pre_restore` 刻意用字节拷贝，正是为了应对坏库 |
| 要求中途失败也字节级还原 | CLI |
| 要一并恢复配置文件 | CLI（`--with-config`） |
| 只有管理界面可用（无 shell / 无 Python 脚本环境） | 管理界面 |

## sha256 语义

- 每个成员（数据库、配置）都有独立 sha256，写在 `manifest.json` 里；恢复时逐个重算。
- 归档本身由 `<归档>.sha256` 旁注保护，先于成员校验执行。
- 旁注缺失时默认降级为“只做成员校验”并打印告警；`--require-sidecar` 可要求直接失败。
- 恢复前必须停服务：Windows 上打开中的库文件无法替换，Linux 上会拿到新旧混杂的 WAL 状态。

**（2026-11-05 已更正）**

本句只对 CLI 路径成立：`scripts/restore.sh` / `restore.ps1` → `scripts/backup_core.py` 仍以字节拷贝留存现场、再换文件（`backup_core.py:618-657`、`712-725`），所以必须先停服务。管理界面的 `POST /api/system/restore` 已改为 SQLite 在线备份（`apps/control/app/routers/backup.py:341-362`），不停服务也能恢复，且不受 Windows 打开句柄限制；代价是应用中途失败只能回滚到一致性快照（内容一致，文件字节不同，快照之后的并发写丢失），不是字节级还原。**这条为什么重要**：按本节操作的人若把「必须停服务」当成所有恢复方式的硬前提，会在管理界面恢复时白停一次服务；反过来以为 CLI 也不用停机，会在 Windows 上反复失败。

## 演示数据

`demo_local` provider 与 `[demo] ` 前缀密钥属于演示数据，默认**不进入归档**：

- 备份阶段在快照副本上删除，规则与 `apps/control/app/demo.py` 的 `clear_demo_data` 对齐：
  配额告警/阈值/策略、日志、用量、流水、价格快照、账号、凭据、价格、密钥、provider、`settings` 里的 `demo_seed`。
- 若 `settings.demo_disabled_providers` 记录了演示期间被临时停用的真实 provider，会先恢复 `enabled=1`，再删该清单，避免把停用状态带进生产。
- 删除在快照文件上 `VACUUM` 后复查，仍有残留则备份直接失败。
- `manifest.demo` 记录 `found_rows`、`removed_rows` 与逐表明细，事后可核对删了什么。
  两个计数口径不同：`found_rows` 只数「带演示标记的表内行」，`settings` 里的演示键只计入 `removed_rows`。
  对账公式：`removed_rows = found_rows + settings 演示键（demo_seed、demo_disabled_providers）+ 由 demo 策略级联删除的 quota_alert_thresholds / quota_alerts 行`；实测同一份数据 `found=8 / removed=12`，差额 4 = settings 2 键 + 级联 1 阈值 + 1 告警，属正常。
- 需要保留演示数据用于排查时显式 `-IncludeDemo` / `--include-demo`。
- 演示密钥文件 `data/demo-credentials.txt` 不在归档范围内；恢复不会把它带回来。

## 限制与未覆盖边界

- 不做加密、不上传远端；归档落在本地磁盘，需自行做异地副本。
- 不是 schema 迁移工具：跨版本恢复依赖应用启动时的建表/迁移逻辑，升级前请先备份。
- 配置恢复是显式选项；默认不覆盖 `config/`、`providers/`、`upstreams/`。
- 演示数据识别依赖标记（`demo_local` / `[demo] `）；用户自建的、未打标记的演示数据不会被剔除。
- 目标库被其他进程占用时替换失败，脚本会提示先停服务；不会强删文件。（仅 CLI 路径；管理界面走在线备份，不受打开句柄限制）
- 归档没有 `.sha256` 旁注时，无法发现“manifest 与成员被一起篡改”的情况，严格场景请加 `--require-sidecar`。
- 旧脚本 `backup.ps1 -Compress` 生成的 zip 结构与当前 manifest 不同，新核心会拒绝；请用新脚本重新备份。
- 旧封装参数 `-Compress`、`-SkipVerify` 不再保留：归档始终压缩；校验默认执行，严格模式用 `--require-sidecar`。
- `scripts/backup_lib.py` 是旧实现的遗留文件，四个封装脚本已不再调用它。

## 2026-11-04 覆盖索引变更后复验

`apps/control/app/perf_indexes.py` 把 `idx_billing_ledger_scope` 升级为 `(key_id, provider, model, ts, billed_micro)`、
`idx_billing_ledger_key` 升级为 `(key_id, ts, billed_micro)` 之后，旧的「索引随恢复保留、sha256 逐字节相同」结论按新定义重新演练，
**闭环仍成立**，`scripts/backup_core.py` 与四个封装脚本未发现缺陷、未改动。

演练环境：临时数据目录 `tmp/backup_drill_20261104/`，源库 `request_logs=36`、`billing_ledger=14`、`quota_policies=3`、
`quota_alerts=3`、`api_keys=3`；全程走真实 CLI（`backup` / `verify` / `restore`），完整输出见 `tmp/backup_drill_20261104/drill.log`。

| 核验项 | 实测 |
| --- | --- |
| sha256 | manifest `database.sha256` = 归档内 `db/unified.db` = 恢复后文件，三方逐字节相同（`acc79de5…4c52786c`） |
| 覆盖索引 SQL 原文 | 源库 / 归档内 / 恢复后 / 控制面启动（`ensure_indexes` 跑过一轮）四处 `sqlite_master.sql` 逐字相同，列清单顺序未变 |
| 行数 | 20 张表逐表相等，差集为空 |
| 恢复后控制面 | 端口 8381、`UNIFIED_DEMO_MODE=0`：`/api/health/ready` → `checks.database.ok=true`；`/api/logs` `total=36`，`credit` / `credit_status` 可读（`billed=12/20`，另有 `not_billed`、`unlinked`） |

四处一致的索引原文：

```sql
CREATE INDEX idx_billing_ledger_scope ON billing_ledger(key_id, provider, model, ts, billed_micro);
CREATE INDEX idx_billing_ledger_key ON billing_ledger(key_id, ts, billed_micro);
CREATE INDEX idx_request_logs_failure ON request_logs(key_id, ts) WHERE status >= 400 OR COALESCE(error, '') <> '';
```

三个坏场景（均退出码 2，目标库未被污染；manifest 篡改同时给出两层校验的实测表现）：

| 场景 | 实际表现 |
| --- | --- |
| manifest 的 `database.sha256` 被改，`.sha256` 旁注仍是原始指纹 | 第一层（归档校验）拦下：`error: archive sha256 mismatch: expected=bc05ad54… actual=78e6cd4a…`，目标路径未生成文件 |
| 同样篡改、但去掉旁注（降级为成员校验） | 第二层（成员校验）拦下：`error: database sha256 mismatch: manifest=0000… actual=acc79de5…`，目标路径未生成文件 |
| zip 缺 `db/unified.db` 成员（无旁注） | 同样第二层拦下：`error: database member missing: db/unified.db`，目标路径未生成文件 |
| 归档内的库缺 `price_books`（坏 schema），且目标库已存在 | 替换后自检 `required_tables.missing=["price_books"]`、`table_count.missing=["price_books"]` → `ok=False`，自动回滚 `pre-restore-20261104-124042/unified.db.bak`；目标库 sha 回滚前后均为 `f2338665…3b42c14e`，与 `unified.db.bak` 一致 |

补充两点口径：

- 源库在线文件与 `VACUUM INTO` 快照的 sha 不同（`f2338665…` vs `acc79de5…`）属正常——快照会重排页；
  比对基准始终是 manifest 记录的快照指纹，不是源库在线文件。
- 恢复后启动控制面，`providers` 由 manifest 的 0 变为 14（`registry._seed()` 播种），就是上文「自检的时序前提」说的行数漂移；
  启动前后索引原文均与源库逐字相同，说明 `ensure_indexes` 对新定义是无操作。
