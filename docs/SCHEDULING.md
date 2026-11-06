# 账号池轮转与四档调度

本文写两块运行时能力：**账号池健康与选号**（`apps/control/app/services/rotation.py`）和**四档调度策略**（`apps/control/app/services/scheduling.py`）。行号以 2026-11-06 当前工作区为准；代码与本文冲突时以代码为准。

先说接线现状，避免把“能力存在”误读成“每家都在用”：

| provider | 接的是什么 | 选号入口 | 健康快照 | 证据 |
| --- | --- | --- | --- | --- |
| `zcode` | `CredentialPool`（无策略层） | `_send` 内 `acquire()` | `health()` 返回 `rotation` + `cooling` | `apps/control/app/adapters/zcode.py:98,286,640-647` |
| `opencode` | `CredentialPool` + `RotationStore`（轮转组） | `pick_in_group()` | `health()` 返回 `rotation` + `cooling` | `apps/control/app/adapters/opencode.py:59-60,148-154,238-240` |
| `codar` | `Scheduler`（四档策略） | `_rotation()` → `plan()` | `health()` 返回 `rotation` + `cooling` | `apps/control/app/adapters/codar.py:170-171,242-248,477-478` |
| `trae` | `CredentialPool`（无策略层，2026-11-06 接入） | `chat_completion` 内 `acquire()` | `health()` 返回 `rotation` + `cooling` | `apps/control/app/adapters/trae.py:171,177-207,924-958,980-1019` |

> **（2026-11-06 接线后更正，原句逐字留档）** 原句：「`trae` 这一行是“指不到接线行号”的实情，按待复核保留：`_jwt()` 只读一个 `api_key`（`trae.py:170-174`），`health()` 只回 `connected/channel/work_mode/models/uid`（`:913-927`），仓内没有任何 `CredentialPool` / `Scheduler` 调用点。若后续要接，接线位置应落在 `_request`，目前不存在这段代码，本文不替它补。」
> 现 `trae` 已接 `CredentialPool`：`_keys()` 合并 `api_keys` / `api_key` / `options.extra_keys`（`trae.py:177-186`），`chat_completion` 每次 `acquire()` 选号并在成功/失败后登记（`:924-958`），`health()` 返回 `rotation` + `cooling`（`:980-1019`）。未引入 `Scheduler`，选号规则仍是池的 `sticky → weighted → round_robin`。
> 误判后果：照旧句会把已接池的 `trae` 当单 JWT，验收时误判“多账号换号没有证据”，也会漏测 401/402/429/5xx 的冷却路径。

## 1. 冷却分档、升级与熔断

基础分档是 `services/rotation.py:30-42` 的常量表，升级与熔断在 `report_failure`（`:260-280`）：

| 上游状态 | 基础冷却 | 证据 |
| --- | --- | --- |
| 401 / 403 | 600s | `rotation.py:33-34` |
| 402 | 900s | `rotation.py:35` |
| 429 | 120s | `rotation.py:37` |
| 408 | 60s | `rotation.py:36` |
| 500 / 502 / 503 / 504 | 60s | `rotation.py:38-41` |
| 其余状态（含 400 / 404） | 30s（`DEFAULT_COOLDOWN`） | `rotation.py:30,267` |

三条规则要连起来看：

1. **基础值取大**：`base = max(分档值, cooldown_sec 或 30)`（`rotation.py:267`）。`cooldown_sec` 是调用方传的下限，只能把冷却调长、不能调短；`zcode` 把 `options.key_cooldown` 传进来（`zcode.py:103-104,325-327`），默认 30 秒对 401 不产生效果（600 > 30）。
2. **连续失败按倍数升级**：`span = min(base × 2^(consecutive-1), 3600)`（`rotation.py:265-268`）。同一个凭据指纹（`cred-` + sha256 前 12 位）连续失败第 1 次是基础值，第 2 次翻倍，第 3 次再翻倍。
3. **连续 3 次熔断**：`consecutive >= BREAKER_THRESHOLD(3)` 时把窗口抬到 `BREAKER_COOLDOWN(3600s)`（`rotation.py:45-46,269-271`）。所以 401 的第 3 次是 3600s 而不是 2400s；429 的第 3 次也是 3600s 而不是 480s。

成功一次就清零：`report_success` 删除冷却、弹出连败计数，并在有连败时打一条“账号恢复”日志（`rotation.py:282-290`）。旧接口 `cool()` 按给定秒数冷却，**不做分档与熔断升级**（`rotation.py:298-303`），接旧 KeyPool 语义的调用点不要拿它当分档入口。

**现行接线（2026-11-06 接线后）覆盖 401 / 402 / 408 / 429 / 5xx，但仍非每家全档。** `zcode` 把上游 5xx 归一成 502（`zcode.py:255-256`），402 / 408 只在未命中专门分支的透传路径出现（`:260`），三者现在都按 `POOL_COOLDOWN_STATUSES` 登记（`:321-326`）；`codar` 的 402 走 `stop`，但会先 `note_failure` 再透传（`codar.py:364-368`），408 归 `cool`、5xx 归 `rotate`，两条路径都登记（`:41,370-383`）；`trae` 的 402 来自积分/套餐码（`trae.py:337-339`），5xx 归一成 502（`:345-346`），408 没有产生路径，前两者都交给池（`:202-207,924-958`）；`opencode` 本轮未改，仍只登记 401/403/429（`opencode.py:198`），5xx 只换号不冷却（`:125-132,177-179`）。

> **（2026-11-06 接线后更正，原句逐字留档）** 原句：「**现行接线只登记了 401 / 403 / 429。** `zcode` 与 `opencode` 只在状态码属于 `COOLDOWN_STATUSES` 时调 `report_failure`（`key_pool.py:12`、`zcode.py:320-327`、`opencode.py:185-186`）；`codar` 的 402 走 `stop`、5xx 走 `rotate`，都不调 `note_failure`（`codar.py:41,44-48,360-379`）。因此 **402=900s、408=60s、5xx=60s、默认 30s 是池的能力，不是当前每家都走得到的路径**：402 在 codar 被当作“付费受限、原样透传”（`:364-367`），5xx 在 zcode/opencode 明确“不占用 key”（`zcode.py:319`、`opencode.py:183`）。这一点按待复核登记，不写成“已生效”。」
> 误判后果：按旧句会把本轮已接的 402/408/5xx 当“无调用点”重复排期；验收时也会把真实的 402 冷却（codar/trae）与 5xx 冷却（zcode/codar/trae）误判成“池能力未生效”。

另有一条单号保护：`zcode` / `opencode` / `trae` 在只剩这一把可用时不登记冷却（`zcode.py:321-324`、`opencode.py:196-199`、`trae.py:202-207`），避免唯一账号被关进冷却后所有请求直接 429；`codar` 没有这层保护，401 / 402 / 408 / 429 / 5xx 即使只有一把 token 也会 `note_failure`（`codar.py:364-385`）。

## 2. 选号顺序与全冷却

`CredentialPool.acquire()`（`rotation.py:211-244`）严格按四步走，每一步命中都会把 `rule` 与 `note` 带回调用方（`Pick` 定义 `:83-94`）：

1. **冷却跳过**：`_available()` 先 `_expire()` 清过期冷却，再过滤仍在冷却的账号（`rotation.py:176-183,216`）。
2. **粘性命中**：有 `sticky_key` 且上次绑定的账号仍健康、未超过 TTL，直接复用（`rotation.py:229-234`；TTL 1800s 见 `:47`，校验见 `:198-209`）。
3. **加权**：配了非均权（`_weights` 非空且可用账号多于 1 个）时按权重随机（`rotation.py:235-239`；权重解析 `:141-165`）。
4. **轮转**：默认均权游标，`available[cursor % len]` 并推进游标（`rotation.py:240-244`）。不配权重时行为可复现，测试锁死为 `tok-a, tok-b, tok-a, tok-b`（`tests/test_rotation_pool.py:38-43`）。

全冷却有两种行为，由 `allow_degraded` 决定（`rotation.py:217-228`）：

- **strict（默认）**：返回 `None`。调用方自己转成错误：`zcode` 抛“N 把密钥全部不可用”（`zcode.py:287-288,106-113`），`opencode` 抛 429（`opencode.py:152-153`），轮转组在全冷却时也返回 `None`（`rotation.py:443-444`，测试 `tests/test_rotation_pool.py:171-177`）。
- **`allow_degraded=True`**：取 `min(冷却截止时间)` 的那个账号试一次，`rule="degraded"`、`note="全部账号冷却中，按最早恢复降级"`，并打 WARNING（`rotation.py:220-228`）。测试同时锁了 strict 与 degraded（`tests/test_rotation_pool.py:103-112`）。

注意 `Scheduler.plan()` 永远传 `allow_degraded=True`（`scheduling.py:132`），所以经 `codar` 走全冷却时不会拿到 `None`，而是 `Plan.degraded=True` 加一条降级说明（`scheduling.py:97-101`）。

## 3. 粘性、失败解绑与健康快照

### 3.1 粘性键从哪来

网关只把请求体传给适配器，读不到 HTTP 头（`rotation.py:63-65`）。`sticky_key_from_payload` 按顺序取：

- 顶层 `session_id`、`conversation_id`、`prompt_cache_key`（`rotation.py:50,69-72`）
- `metadata.session_id`、`metadata.conversation_id`、`metadata.user_id`（`:51,73-78`）
- 顶层 `user` 字符串（`:79-80`）

取不到就返回空串，调度退回轮转，不伪造粘性；单测锁了四种来源与空值（`tests/test_rotation_pool.py:180-186`）。

绑定发生在调用方拿到成功响应之后：`zcode.py:309-311`、`opencode.py:173-176`；`Scheduler.plan` 只在 `session_key` 存在且**非降级**时绑定（`scheduling.py:117-118`）。

### 3.2 失败即解绑

`report_failure` 把“绑在该失败账号上的所有会话”一次性删除（`rotation.py:273-277`），下一次请求重新选号，不会继续撞坏号。测试 `tests/test_rotation_pool.py:69-75` 锁的就是这条：绑定 `tok-a` 后让 `tok-a` 401，下一次同会话必须落到 `tok-b`。

### 3.3 健康快照字段

`snapshot()`（`rotation.py:307-344`）返回：

| 层级 | 字段 | 含义 |
| --- | --- | --- |
| 顶层 | `rule` | `sticky>weighted>round_robin`（配了非均权）或 `sticky>round_robin`（`:336`） |
| 顶层 | `available` / `cooling` | 可用数 / 冷却中数量（`:337-338`） |
| 顶层 | `credentials[]` | 每个凭据一条（`:339`） |
| 顶层 | `group` / `group_active` | 仅传了 `group` 时出现（`:341-343`） |
| 条目 | `id` | `cred-` + sha256 前 12 位（`:54-58,328`） |
| 条目 | `state` | `ready` / `cooling` / `breaker` / `degraded`（判定见 `:321-326`） |
| 条目 | `cooldown_remaining` | 剩余秒数，保留 1 位小数（`:330`） |
| 条目 | `consecutive_failures` | 连续失败次数（`:331`） |
| 条目 | `last_used_sec_ago` | 距上次使用秒数，从未用过为 `null`（`:332`） |
| 条目 | `weight` | 权重，默认 1.0（`:333`） |

两个 `degraded` 不是一回事，别混：**快照里的 `degraded`** 指“冷却已过期、但仍记着连败”，账号可用（`:323-324`）；**`Plan.degraded`** 指“全部账号冷却、本次忽略冷却降级试号”（`scheduling.py:45-46,97-101`）。测试 `tests/test_rotation_pool.py:115-131` 同时断言了快照字段与“快照不含明文”。

## 4. 轮转组：`group_init` / `group_rotate` / 组粘滞

轮转组目前只接在 `opencode`（`opencode.py:59-60,148-154`）。组名来自 `options.group` 或 `options.rotation_group`，缺省 `default`，硬截 64 字符（`opencode.py:63-65`、`rotation.py:48,410-411`）。

`pick_in_group()`（`rotation.py:414-451`）的顺序：

1. 读持久化的 `active` 指纹（`:430-431`），若该账号仍在 `keys` 且当前可用 → 直接命中，`rule="group_sticky"`，只 `touch` 不重写库（`:433-441`）。
2. 否则走 `pool.acquire(keys, sticky_key=...)`（`:442`）；返回 `None` 则组内全冷却，调用方转 429（`opencode.py:152-153`）。
3. 选到新号后落库，`rule` 按“此前有没有 active”分两种：没有 → `group_init`，有但不可用 → `group_rotate`（`:445-446`）。

落库只写指纹和规则名，不复制明文：`save()` 的 JSON 是 `{"active": cred_id, "rule": rule}`（`rotation.py:393-405`），表 `rotation_state(provider, group_name, state_json, updated_at)`（`apps/control/app/db.py:335-343`）。测试同时验证“新池 + 新 store 读同一个库仍命中同号”（`tests/test_rotation_pool.py:134-149`）与“库里只能是 `cred-`、不含 `tok-a/tok-b`”（`:151-156`）。

读写失败一律降级为内存态并留 WARNING，不让调度把请求打死（`rotation.py:347-352,376-391,393-408`）。

## 5. 四档调度策略（`services/scheduling.py`）

四档集合是 `sticky` / `priority` / `weighted` / `failover`（`scheduling.py:34`），缺省 `sticky`（`:35`）。四档**只在第一个号怎么选上不同**，选完一律按配置顺序向后轮转（`:3-5,110-116`）。

| 策略 | 第一个号怎么选 | 降级规则 | 证据 |
| --- | --- | --- | --- |
| `priority` | 健康号里取配置顺序第一个，并 `touch` | 主号冷却后自动落到下一个健康号 | `scheduling.py:128-131`；测试 `tests/test_schedule_policy.py:52-59` |
| `sticky` | 同 `session_key` 上次绑定的号仍健康就复用；首次则按顺序选并绑定 | **无 `session_key` → 降为 `failover`**，`note="请求未带会话标识，粘滞退化为故障转移"` | `scheduling.py:105-107,117-118`；测试 `:62-76` |
| `weighted` | 健康号之间按权重随机 | **设计上“无有效权重 → 降为 `failover`”，但当前不生效**：`scheduling.py:102` 写的是 `not self._pool.has_weights`，而 `has_weights` 是方法不是属性（`rotation.py:167-169`），取到的是绑定方法对象，取反恒为 `False`；无权重时 `acquire` 落到轮转（`rotation.py:235-244`），实际效果是“规则仍叫 weighted、选择已是轮转”，`Plan.rule` 也不会变成 `failover` | `scheduling.py:102-104,132`；权重 `rotation.py:141-169`；测试只覆盖“配了权重会偏向”，未覆盖“无权重降级”（`tests/test_schedule_policy.py:79-87`） |
| `failover` | 均权轮转游标，失败前不重复命中同一个号 | 无额外降级 | `scheduling.py:132`、`rotation.py:240-244`；测试 `:90-98` |

其余规则：

- **未知策略**：回落 `DEFAULT_POLICY`（`sticky`）并打 WARNING，`plan.rule` 仍可能因无会话标识继续落到 `failover`（`scheduling.py:69-75`）。测试只断言“给出 sticky/failover 之一 + 有 WARNING”（`tests/test_schedule_policy.py:121-126`）。
- **每次决策一条 INFO**：`调度决定 policy=... first=... 顺序=... 降级=... 说明=...`（`scheduling.py:119-122`），`first` 与顺序都只打指纹。
- **去重与空池**：入参先去重保序（`:86-89`），空池返回 `Plan([], "none", False, "账号池为空")`（`:90-91`）。
- **尝试顺序与上限**：`indices = [first, 其余可用号按配置顺序环形][:limit]`，`limit` 默认 3（`:36,110-116`）。测试 `tests/test_schedule_policy.py:114-118` 锁了截断。
- **全冷却**：`usable` 为空时置 `degraded=True`、`usable=creds`，说明写“全部账号冷却，忽略冷却按最早恢复顺序降级”（`:97-101`）。测试 `tests/test_schedule_policy.py:101-111` 断言 `plan.degraded=True`、`indices` 仍保留可尝试顺序、日志含“降级/全冷却”。
- **策略只接在 `codar`**：`zcode` / `opencode` / `trae` 没有 `Scheduler`，它们直接用池的 `sticky → weighted → round_robin`（`zcode.py:286`、`opencode.py:148-151`、`trae.py:924-958`）；`priority` / `failover` 这两个名字只在 codar 的 `options.schedule_policy`（`codar.py:244-247`）下生效。`codar` 的 `_rotation()` 还负责 token 列表与配置签名同步（`codar.py:234-241`）。

## 6. 口径澄清：`readiness` 是配置契约，不是运行时就绪

三处容易混：

1. **`ProviderSpec.readiness` 是静态配置契约**：`ready` 只表示“填好 `api_key` / `base_url` 就能调用”，`needs_config` 表示“还要补额外配置或外部进程”；标了 `needs_config` 必须写 `requires`，否则构造时直接抛错（`apps/control/app/adapters/base.py:139-153`）。它不随请求成功/失败变化。
2. **运行时就绪在 `health()` 的 `rotation` / `cooling`**：`zcode` 在探活成功后返回 `rotation` 快照与 `cooling` 计数（`zcode.py:640-647`），`opencode` 同（`opencode.py:238-240`），`codar` 也同（`codar.py:477-478`），`trae` 现在也同（`trae.py:980-1019`）。
3. **`/api/status` 只合并 `cooling`，不转发 `rotation` 明细**：合并白名单是 `total/healthy/cooling/disabled/in_flight_full/realm_totals`（`apps/control/app/routers/models.py:43-45`），循环里后一个连通的 provider 会覆盖前一个的值（`:39-47`）；`rotation` 明细留在 `health()` 的原始返回里，当前没有独立 HTTP 面。排查时要看适配器 `health()`，不能只看 `/api/status`。

另一个同名不同源：`codebuddy` 的 `health()` 里的 `cooling` 来自上游管理端 `/api/status`（`apps/control/app/adapters/codebuddy.py:220,229-233`），不是本地 `CredentialPool` 的冷却；`apps/web/lib/account-status.ts:103-106` 还记录了上游把“降权”并进 `cooling`。不要拿它和本文的分档冷却互相验证。

## 7. 已知边界与待复核

- **`weighted` 的无权重降级当前不生效（缺陷，待修）**：`scheduling.py:102` 的 `not self._pool.has_weights` 取到的是绑定方法对象，恒为 `False`，所以“未配置有效权重，weighted 退化为故障转移”这条永远不会执行。无权重时实际是 `acquire` 的均权轮转（`rotation.py:235-244`），`Plan.rule` 仍报 `weighted`。本文按代码实际行为写；修法是把 `has_weights` 变成 `@property` 或在调用处加 `()`，本次不改代码。
- **`trae` 已接账号池（2026-11-06 接线后更正，原句逐字留档）**：原句「`trae` 未接账号池：证据见文首表；如果要补，先定它是否支持多 JWT 与如何取会话标识，再按 `zcode` 的接法落地。」现 `trae` 按 `zcode` 的接法用 `CredentialPool` 选号并在 `chat_completion` / `health` 登记（`trae.py:171,177-207,924-958,980-1019`）；上游 trae2api 本身是多账号池（`internal/pool/pool.go:199` `RecordError`、`:276` `Pick`、`:209` `Add`，`internal/server/accounts.go:40,91,231,259,297` 账号增删改查与刷新），所以“先定是否支持多 JWT”这一步已有肯定答案。误判后果：按旧句会把已落地的换号与冷却当成待排期，重复实现。未接 `Scheduler`，`priority` / `failover` 策略对 `trae` 不生效。
- **402 / 408 / 5xx 分档（2026-11-06 接线后更正，原句逐字留档）**：原句「402 / 408 / 5xx 分档当前无调用点：见 §1 末段；写测试可以直接调 `report_failure` 验证，但不能据此说“线上某次 402 冷却了 900 秒”。」现状：`codar` 的 402 / 408 / 5xx、`trae` 的 402 / 5xx、`zcode` 的 402 / 408 / 5xx（后两者仅透传路径）均已进入池的 `report_failure`（`codar.py:364-383`、`trae.py:202-207,924-958`、`zcode.py:260,321-326`）；`trae` 与 `opencode` 不产生 408，`opencode` 的 402/408/5xx 仍未接线。误判后果：按旧句会把已生效的冷却路径当测试专用能力，验收时反而把真实冷却日志判成造假。
- **未接池的适配器不适用分档**：`gptgrok` 仍走 `KeyPool` 单档冷却，408/5xx 只换 key 不冷却（`gptgrok.py:43`）；`muse` 仍在自身重试循环（`muse.py:38`），仓内没有池调用点。两者的 408/5xx 属于**池能力，未接线**；把它们算进“402/408/5xx 已接线”会虚报覆盖。
- **权重会影响非 `weighted` 策略的首选**：`Scheduler.plan` 每次都会 `set_weights`（`scheduling.py:94-95`），而 `_first` 对所有非 `priority` 策略都走 `pool.acquire`（`:132`），池在粘性未命中后只要 `_weights` 非空就会加权（`rotation.py:235-239`）。这与文件头“四档只在第一个号怎么选上不同”的设计表述存在口径差，是否要隔离为“只有 weighted 才设权重”待复核；本文按代码实际路径写，不按注释写。
- **连败计数不随时间衰减**：只有成功会清零（`rotation.py:265-266,282-290`），跨天的一次旧失败再叠加一次新失败仍算第 2 次。是否要按时间窗口衰减待复核。
- **全冷却的 `note` 与后续顺序不完全一致**：`scheduling.py:101` 写“按最早恢复顺序降级”，第一个号确实是 `pool.acquire(allow_degraded=True)` 取的最早恢复号（`:132`、`rotation.py:220-228`），但第二个及以后是“从该号起按配置顺序”排出来的（`scheduling.py:110-116`），不是按剩余冷却时间排序。排障时不要把 `indices` 后半段理解成“次早恢复”。
- **`rotation` 明细无 HTTP 面**：`/api/status` 白名单只带 `cooling`（`models.py:43-45`），目前要靠适配器 `health()` 或日志看单号状态。需要运维面板时再单开接口，不在本文擅自承诺。

## 8. 测试与证据

| 覆盖点 | 用例 |
| --- | --- |
| 均权轮转可复现、权重、粘性命中、失败解绑、分档升级与熔断、全冷却 strict/degraded、快照字段与不泄密、轮转组持久化与换号 | `apps/control/tests/test_rotation_pool.py:38-186` |
| 四档策略、sticky 无会话降级、未知策略回落、全冷却降级、截断、会话键来源、codar 失败动作表、codar 粘性接线；**`weighted` 无权重降级未覆盖** | `apps/control/tests/test_schedule_policy.py:52-188` |
| `trae` 多账号选号、401 换号与冷却、402 / 502 进入池分档、`health()` 的 `rotation` / `cooling` | `apps/control/tests/test_f2.py:301-349` |
| 上游 402 / 408 / 5xx 登记：`codar` 的 `note_failure`、`zcode` 的 5xx 冷却与单号保护 | `apps/control/tests/test_codar.py:174-184`、`apps/control/tests/test_vendor_adapters.py:227-233` |

测试只锁选号规则与健康登记，不覆盖网络路径；网络层的换号由 provider / smoke 用例另行覆盖（`test_rotation_pool.py:1-5`）。
