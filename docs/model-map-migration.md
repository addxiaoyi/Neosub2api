# `model_map` 迁移方案

> 结论：**需要迁移**，但不改现有代码。本轮只交付新结构与迁移步骤；[config/model-aliases.yaml](../config/model-aliases.yaml) 是目标配置，`apps/control/app/` 与 `apps/web/` 保持原样。
> 目标语义见 [model-routing.md](model-routing.md)。

## 1. 为什么需要迁移

现状（[routers/settings.py](../apps/control/app/routers/settings.py#L267-L293)、[registry.py](../apps/control/app/registry.py#L164-L193)）：

- 存储是 SQLite `settings` 表里的一个 JSON 对象：`{"别名": "目标串"}`；
- 一个别名只能指向一个目标，无法表达「同优先级多候选 + 权重」；
- 没有兜底链、没有能力标记、没有配置校验；
- 目标串要靠 `/` 猜 provider，不带前缀时进入模型目录自动匹配（唯一命中才走）；
- 每个请求读一次 DB。

新结构要解决的是 1:N 选路、权重、兜底和能力过滤，这些都是扁平 map 无法表达的，所以迁移不是「换个存储位置」，而是换语义。**必须做**，但可以分阶段兼容，不必一次性切换。

## 2. 目标结构

### 2.1 源文件

`config/model-aliases.yaml` 作为唯一写入口，进 git，走代码评审和校验脚本。运行时只读，不提供在线编辑（首版）。

### 2.2 可选落库（提案，不落地）

如果后续管理页需要在线编辑或审计，可把 YAML 编译成下面三张表；表只是 YAML 的投影，不反向成为权威来源。

```sql
CREATE TABLE model_aliases (
    alias            TEXT PRIMARY KEY,
    capabilities_json TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    deprecated       INTEGER NOT NULL DEFAULT 0,
    updated_at       INTEGER NOT NULL
);

CREATE TABLE model_alias_candidates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    alias          TEXT NOT NULL REFERENCES model_aliases(alias) ON DELETE CASCADE,
    provider       TEXT NOT NULL,
    upstream_model TEXT NOT NULL,
    weight         REAL NOT NULL CHECK (weight >= 0 AND weight <= 1),
    priority       INTEGER NOT NULL CHECK (priority >= 0),
    enabled        INTEGER NOT NULL DEFAULT 1,
    UNIQUE (alias, provider, upstream_model)
);

CREATE TABLE model_alias_fallbacks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    alias          TEXT NOT NULL REFERENCES model_aliases(alias) ON DELETE CASCADE,
    priority       INTEGER NOT NULL,
    provider       TEXT,
    upstream_model TEXT,
    target_alias   TEXT,
    triggers_json  TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    CHECK (
        (provider IS NOT NULL AND upstream_model IS NOT NULL AND target_alias IS NULL)
        OR (provider IS NULL AND upstream_model IS NULL AND target_alias IS NOT NULL)
    )
);
```

约束与校验脚本一致：别名唯一、provider 在注册表、权重 0–1、兜底二选一、触发器白名单、别名链无环。落库时用一次性编译脚本写入，不要在两个地方手改。

## 3. 兼容读法

目标读路径按以下优先级，**旧 `model_map` 原样保留、不迁移、不删除**：

```text
1. 显式 x-provider / <provider>/<model>          （与现状一致，最高优先级）
2. 新别名表精确匹配（请求 model 先 lower()）
3. 旧 model_map 精确匹配（保持大小写敏感，避免改变现网行为）
     目标串含 "/" 且前缀已注册 -> provider=前缀, upstream=后半段
     目标串不含 "/"           -> 进入模型目录自动匹配（唯一命中才走）
4. 都没有 -> 404 fail closed（model_not_found / model_unavailable）
```

兼容期的关键约定：

| 约定 | 说明 |
| --- | --- |
| 新旧同名 | 新别名胜出，记 warning；不静默合并 |
| 旧别名能力 | 旧 map 没有能力信息，**不做能力过滤**，保持现有行为 |
| 旧别名权重 | 固定 `1.0`、`priority: 0`，等价于单候选 |
| 旧别名大小写 | 先按原始字符串精确匹配，未命中再尝试 lower() 并记 warning |
| 读失败 | 新表解析失败时保留上一次可用表；启动期失败直接拒绝启动 |

### 3.1 切换开关（提案）

建议加一个环境变量，不碰业务代码逻辑之外的分支：

```text
UNIFIED_MODEL_ROUTING_SOURCE=legacy | shadow | yaml
```

| 取值 | 行为 |
| --- | --- |
| `legacy` | 完全走旧 `model_map`，新表只加载不使用（默认，保证可回滚） |
| `shadow` | 两套都算，**按旧表返回**，记录新表会选谁、是否 404，用于对比 |
| `yaml` | 新表优先，未命中再读旧 map；未知模型按配置决定是否 fail closed |

本次不实现该开关，仅作为迁移契约；实现时建议默认 `legacy`，避免部署即改变线上行为。

## 4. 切换步骤

### 阶段 0：冻结与盘点

1. 暂停通过 `POST /api/settings/model-map` 改别名，改为提 YAML PR。
2. 导出当前表：

```bash
curl -b cookie.txt http://127.0.0.1:8010/api/settings/model-map
```

3. 为每个旧条目记录：别名、目标串、是否带 provider 前缀、线上 30 天调用量（`request_logs` 按 `model` 聚合）。

### 阶段 1：生成新表（双写前）

1. 能映射到具体 provider 的旧条目 → 写成新别名 + 单候选，`weight: 1.0`；
2. 目标串不带前缀、依赖「第一个启用 provider」的旧条目 → 单独列一份清单，**不要猜 provider**；迁移期继续由旧 map 承接；
3. 新别名按 [model-routing.md](model-routing.md) 第 2 节补齐 `capabilities` / `notes`；
4. 每个 `upstream_model` 补「出处」注释；
5. 校验：

```powershell
pwsh -File scripts/check-model-aliases.ps1 -Strict
```

### 阶段 2：影子运行

1. 开 `shadow`，观察 24–72 小时；
2. 对比三组指标：新表命中的请求、旧表命中但新表未命中的请求、新表会 404 的请求；
3. 对「新表会 404」的模型，逐个决定：补别名，或保留旧兜底一个窗口。

### 阶段 3：切主

1. 开 `yaml`，保留旧 map 作为未命中兜底；
2. 观察 404 率、501 率、`request_logs.model` 分布、`/v1/models` 客户端兼容性；
3. 稳定一个发布周期后，再把「未知模型」从旧兜底切成 404 fail closed；
4. 下线旧 map 读路径（DB 键保留，代码不再读）。

### 阶段 4：清理

1. 删除管理页的旧 map 编辑入口（若已由 YAML 取代）；
2. 把 `model_map` 键标记只读或归档；
3. 文档、示例 curl、前端提示统一改成别名口径。

## 5. 回滚

回滚成本要压到「一次重启」，因此：

| 动作 | 做法 |
| --- | --- |
| 快速回滚 | `UNIFIED_MODEL_ROUTING_SOURCE=legacy`，重启；旧 map 从未删除，立即恢复 |
| 配置回滚 | YAML 在 git 里，`git revert` 对应提交即可；不涉及数据迁移 |
| 数据回滚 | 不写旧表、不做 DDL 变更，无需回滚 SQL；可选三张新表可整表删除 |
| 部分回滚 | 只把某个别名标 `deprecated: true` 或清空 `fallbacks`，不影响其他别名 |

前提：**不要**在切换时顺手删除或改写 `settings.model_map`。它是回滚锚点。

## 6. 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 未知模型从「能跑」变 404 | 客户端报错 | 阶段 3 最后再切 fail closed；先补别名或保留旧兜底窗口 |
| 别名与上游 id 同名 | `/v1/models` 出现歧义 | 别名优先；上游 id 用 `<provider>/<id>` 调用 |
| 权重合计不为 1 | 选路比例失真 | 校验脚本硬性拦截 |
| 兜底成环 | 请求递归/超时 | 校验脚本 DFS 拦截；运行时深度上限 2 |
| `on` 未加引号 | YAML 布尔键，兜底条件丢失 | 配置写 `"on"`；脚本给 warning |
| 大小写差异（`GLM-5.3` / `glm-5.3`） | 上游 404 | 别名键小写；`upstream_model` 原样保留 |
| 热加载读到半写文件 | 路由表短暂不一致 | 写临时文件 + 原子 rename；解析失败保留 last-good |
| 多副本权重漂移 | 单副本精确、全局近似 | 首版接受；需要精确时再加共享计数器 |

## 7. 验收清单

- [ ] `pwsh -File scripts/check-model-aliases.ps1 -Strict` 退出码 0；
- [ ] 旧 `model_map` 导出的每个键都有归属：已迁到新别名，或明确保留旧路径；
- [ ] `shadow` 期「新表 404」清单已清零或有明确接受理由；
- [ ] `/v1/models` 返回别名去重后的列表，客户端无解析错误；
- [ ] `request_logs` 里不再出现「未知模型被第一个 provider 接走」的隐性路由；
- [ ] 回滚演练：设 `legacy` + 重启后，旧别名行为与迁移前一致。
