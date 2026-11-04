# 模型别名与路由矩阵

> 对象：[config/model-aliases.yaml](../config/model-aliases.yaml)。本文定义这套结构的语义，是**目标契约**；控制面当前尚未接入（本轮不改 `apps/control/app/` 与 `apps/web/`），现状差异见第 9 节。
> 上游复合路由的参考口径：[docs/providers/sub2api.md](providers/sub2api.md) 第 3.1 节。

## 1. 口径

一个别名 = 一组「provider × upstream_model」候选 + 能力承诺 + 兜底链。别名是对外契约，上游模型改名时只换候选，别名不动。

与 sub2api 复合路由的对应关系：

| sub2api 字段 | 本结构 | 说明 |
| --- | --- | --- |
| `public_model` | `aliases` 的键 | 客户端请求的模型名 |
| `target_platform` | `provider` | 这里用本仓注册表 id，不是平台大类 |
| `upstream_model` | `upstream_model` | 实际转发给上游的标识符 |
| `priority` | `priority` | 数值小者优先 |
| `match_type` | 无 | 首版只做精确匹配，不做 prefix |
| `enabled` | 文件里增删候选 | 首版不做运行时开关 |
| 无 | `weight` | 同优先级层内的流量份额 |
| 无 | `fallbacks.on` | 失败触发条件，见第 5 节 |

与 sub2api 一致的关键点是 **fail closed**：解析不到就报错，不猜 provider、不静默换模型。当前默认行为是模型目录唯一命中 + 404；「第一个已启用且支持该能力位的 provider」兜底只在 `UNIFIED_MODEL_FALLBACK_FIRST=1` 时恢复，且只作用于「一家都没命中」，歧义仍是 400（见第 9 节）。

## 2. 文件结构与硬约束

```yaml
version: 1
aliases:
  glm:
    - provider: codebuddy
      upstream_model: glm-5.2
      weight: 0.7
      priority: 10
fallbacks:
  glm:
    - provider: zcode
      upstream_model: GLM-5.3
      priority: 20
      "on": ["429", "5xx", "timeout", "capability_unsupported"]
capabilities:
  glm: {stream: true, tools: true, vision: false, long_context: true, reasoning: false}
notes:
  glm: "自造对外名：家族级别名，上游真实标识是 glm-5.2 / glm-5.3。"
```

| 字段 | 约束 |
| --- | --- |
| 别名键 | 小写，`[a-z0-9._-]`，不含 `/`；命中时对请求 `model` 做 `lower()` |
| `provider` | 必须存在于适配器注册表（由 `adapters/__init__.py` 的 `BUILDERS` 推导，当前 14 个） |
| `upstream_model` | 原样转发、大小写敏感；必须能追溯到上游 README 或本仓代码 |
| `weight` | 0–1；同一别名内合计为 1.0；只决定同优先级层内的份额 |
| `priority` | 非负整数；小者先选 |
| `fallbacks.on` | 只能是 `429` / `5xx` / `timeout` / `capability_unsupported` |
| `capabilities` | 恰好五个布尔位：`stream` / `tools` / `vision` / `long_context` / `reasoning` |
| `notes` | 每个别名一行，必须写明自造对外名还是沿用上游名 |

两个容易踩的坑：

1. **`on` 必须加引号**。YAML 1.1 会把裸 `on` 解析成布尔键 `True`，后续按 `item["on"]` 读会拿不到。校验脚本会提示这个警告，但配置本身应写成 `"on"`。
2. **别名内不要写 `/`**。带 `/` 的模型名会先被 `<provider_id>/<model>` 规则截走。

## 3. 解析顺序

请求进入 `/v1/chat/completions`、`/v1/messages`、`/v1/responses` 后按下列顺序解析：

1. `x-provider` 请求头显式指定 → 用该 provider，**完全跳过别名表**。现网行为保留，用于排障和强制走某家。`model` 同时带 `provider/model` 前缀时：同源（前缀与头指向同一 provider）按前缀剥离、只把裸模型名给上游；异源 → 400 `conflicting_provider_hints`（不静默取头）；头指向本地没有的 provider → 404 `provider_not_found`。判定顺序：本地存在性(404) → 冲突(400) → 停用(503)。
2. `model` 形如 `<provider_id>/<rest>` 且前缀是已注册 provider → 用该 provider，取 `rest`，**跳过别名表**。前缀不认识时不做二次猜测。
3. 别名精确匹配：`model.strip().lower()` 命中 `aliases` 键 → 进入候选选择。`upstream_model` 保持文件里的原始大小写（`GLM-5.3` 与 `glm-5.3` 各自按上游要求发送）。
4. 迁移期兼容：未命中别名时读旧 `model_map`（见 [model-map-migration.md](model-map-migration.md)）。新别名优先于旧 map；同名冲突记 warning，新表胜出。
5. 仍未命中 → **404 `model_not_found`**，不再回落到第一个已启用 provider。

命中别名后还要做能力与协议过滤：

```text
候选可用 = provider 已启用
        且 provider.capabilities[endpoint] == true   # chat / messages / responses
        且 候选满足本次请求声明的能力（stream / tools / vision / long_context / reasoning）
```

请求能力的推导：

| 能力 | 触发条件 |
| --- | --- |
| `stream` | 请求体 `stream: true` |
| `tools` | `tools` 非空，或 `tool_choice` 非 `none` |
| `vision` | 消息内容里出现 `image_url` / `image` 类型的分片 |
| `reasoning` | 出现 `reasoning_effort` / `thinking` |
| `long_context` | 首版不做 token 估算；仅在请求头 `x-require-capabilities: long_context` 时作为硬性要求 |

`capabilities` 是我方对外承诺，不是上游实测结论。声明为 `true` 的能力如果上游实际不支持，属于配置错误，应由联调阶段修正，而不是在路由层兜底。

过滤后没有候选 → 400 `capability_not_supported`；别名或 provider 不存在 → 404；provider 存在但未启用 → 503。

## 4. 权重算法与并发一致性

### 4.1 选路

1. 按 `priority` 升序分组，只取**最小的非空可用层**。高优先级层有可用候选时，低优先级层完全不参与。
2. 层内按 `weight` 归一化（若该层权重和不是 1，用层内和重新归一化）。
3. 用平滑加权轮询（smooth weighted round-robin）选出本次候选：

```text
每次选择：
  for c in tier: c.current += c.weight
  pick = argmax(c.current, tie -> priority, provider, upstream_model)
  pick.current -= sum(tier.weight)
```

选 SWRR 而不是随机：同样权重下分布精确、可复现、不会出现「权重 0.1 的候选连续被抽中」的长尾，测试也更容易断言。

### 4.2 并发

- 单进程内每个 `(alias, priority, endpoint)` 维护一份计数器；更新包在 `asyncio.Lock` 里，临界区只有 O(候选数) 的加减，没有 `await`、没有 IO。
- 因此在同一事件循环里并发请求不会丢更新，N 个请求后的比例严格贴近权重。
- 多 worker / 多副本时，每个进程各有一份计数器：单进程内精确，跨进程是「每个进程都按权重走」，大样本下聚合收敛，不保证全局逐请求精确。首版不引入 Redis/DB 共享计数器，避免把每个请求变成一次写。
- 不做会话粘性：同一 API Key 的连续请求可能落到不同候选。若后续需要，按 `hash(api_key, alias)` 做一致性哈希，作为显式开关加入，不默认开启。
- **流式响应一旦向下游写出第一个字节就不能换候选**，否则会在同一条流里拼接两个模型的输出。兜底只发生在「还没向客户端写任何字节」之前。

## 5. 兜底（fallbacks）

| 触发条件 | 是否换候选 | 说明 |
| --- | --- | --- |
| `429` | 是 | 上游限流 / 配额耗尽 |
| `5xx` | 是 | 上游 500/502/503/504 |
| `timeout` | 是 | 连接或首字节超时；已开始的流不重放 |
| `capability_unsupported` | 是 | 候选不满足本次请求的能力或端点协议 |
| 400 / 413 / 422 | 否 | 请求级错误，换谁都一样 |
| 401 / 403 | 否 | 凭据/权限问题，交给 provider 自己的换 key 逻辑 |

执行规则：

1. 主候选层按第 4 节选路；失败且状态命中 `on` → 进入 `fallbacks`。
2. `fallbacks` 按 `priority` 升序逐条尝试；每条可以写具体 `provider + upstream_model`，也可以写 `alias: <另一个别名>` 做整链委托。
3. 别名链深度上限 2；校验脚本拒绝环（例如 `a -> b -> a`）。深度超限或链中别名缺失按配置错误处理，返回 502 并告警，不继续猜。
4. 全部失败 → 返回最后一次上游状态；一个可用候选都没有 → 503。不返回空成功。
5. 每次尝试记应用日志（alias、候选、触发条件）；`request_logs` 现有 schema 只记录**最终生效**的 provider/model。首版不新增 `routing_json` 列，避免为路由矩阵改表；需要完整链路时看应用日志。

## 6. 上游改名后怎么改

原则：**别名是契约，候选是实现细节**。

1. 只改 `upstream_model` 与上方的「出处」注释，别名键不动。客户端无感知。
2. 上游改名但旧名仍可用时，走三步灰度：
   - 新增候选，`weight: 0`，同 `priority`；部署并确认 `/v1/models` 的上游列表里出现新名；
   - 分两次把权重从 `0 → 0.1 → 0.5 → 1.0` 迁移，每次保持同别名权重合计 1.0；
   - 删除旧候选，跑 `scripts/check-model-aliases.ps1 -Strict`。
3. 别名本身就是上游名（如 `glm-5.3`）且上游升版时：
   - 新增 `glm-5.4` 别名指向新模型；
   - 旧别名 `glm-5.3` 保留一个弃用窗口，候选改指新模型，并在 `notes` 标注「已弃用，等价于 glm-5.4」；
   - 窗口结束后删除旧别名。**不要**把旧别名复用到另一个模型家族。
4. 文件改动后必须重跑校验；`-Strict` 会把 `unknown` 模型名、权重和不等于 1、provider 拼错、兜底环全部拦下。
5. 热加载契约（待实现）：按文件 mtime 原子替换内存表；解析/校验失败保留上一份可用表并记 error；启动时校验失败直接拒绝启动，避免带着坏表上线。

## 7. 与 `/v1/models` 的关系

现状（[v1.py](../apps/control/app/routers/v1.py#L227-L238)）：

- 遍历已启用且 `capabilities.models` 为真的 provider，合并各自 `list_models()`；
- 返回的是**上游模型 id**，不含别名；`/v1/models` 没有去重，跨 provider 同名会出现重复条目；
- 单个 provider 拉取失败时静默跳过，空列表既可能是「没 provider」也可能是「都不通」。

目标契约：

| 场景 | 返回 |
| --- | --- |
| `GET /v1/models` | 只返回别名（去重），`object: "model"`，`owned_by: "unified-2api"` |
| 别名元数据 | 非标准字段 `x_capabilities`、`x_candidate_count`、`x_deprecated`（客户端应忽略未知字段） |
| 调试上游真实清单 | `GET /v1/models?include_upstream=1`，gateway key 鉴权；保留现有聚合结果 |
| 别名与上游 id 同名 | 公开列表以别名为准；上游 id 仍可通过 `<provider>/<id>` 或 `x-provider` 调用 |
| 迁移期旧 `model_map` 键 | 出现在公开列表，标 `x_legacy: true` |
| 管理面 `/api/models` | 保持现状（实时上游聚合），用于服务商排障，不混入别名 |

空表 / 配置无效时不要返回陈旧别名：启动期失败即拒绝启动；热加载失败保留 last-good 并告警。

## 8. 14 家 provider × 协议 × 模型命名特征

协议取自各适配器 `ProviderSpec.protocols`；模型名只写上游 README / 示例配置里真实出现的标识符。

| provider id | 协议 | 模型命名特征 | 证据样本 | 出处 |
| --- | --- | --- | --- | --- |
| `codebuddy` | OpenAI Chat + Anthropic Messages | 以 GLM 为主，混编 kimi/minimax/hy3；全小写、连字符分版本；`glm-5v-turbo` 用 `v` 标视觉 | `glm-5.2`、`glm-5.1`、`glm-5v-turbo`、`kimi-k2.7`、`minimax-m3`、`hy3`、`hy3-preview` | <https://github.com/addxiaoyi/codebuddy2api> README「可用模型」 |
| `muse` | OpenAI Chat | 不按自家名暴露，内置生态通用别名映射（GPT/Claude/DeepSeek 名混用） | `gpt-4o`、`gpt-5`、`claude-sonnet-4`、`deepseek-chat` | <https://github.com/czg86389-hub/muse2api> README |
| `grok-register` | 无推理协议（`register` 任务） | 无模型 | — | [providers/grok-register.md](providers/grok-register.md) |
| `gptgrok` | OpenAI Chat | OpenAI 短名；图像模型带 `image` 段；Grok 侧 README 未给 ID | `gpt-5`、`gpt-image-2` | <https://github.com/addxiaoyi/gptGrok2api> README |
| `zcode` | OpenAI Chat + Anthropic Messages | 大写 `GLM` + 版本，`-Flash` 后缀；大小写敏感 | `GLM-5.3`、`GLM-5.3-Flash` | <https://github.com/addxiaoyi/ZCode2api> README `models` 数组 |
| `opencode` | OpenAI Chat + Anthropic Messages | 全小写，点号分版本（`qwen3.8-max`）、家族-代际（`kimi-k3`） | `deepseek-v4.1-flash`、`glm-5.3`、`grok-4.6`、`kimi-k3`、`qwen3.8-max` | <https://github.com/jasonxu114514/opencode2api/blob/main/config.example.json> `rotation.sota.order` |
| `sub2api` | OpenAI Chat + Anthropic Messages（上游还支持 Responses/Gemini，本仓未登记） | 不维护固定模型清单；`upstream_model` 由复合路由表逐条决定，平台枚举 `anthropic/openai/gemini/grok/kimi/glm/deepseek/minimax/antigravity` | 无固定样本（`unknown`） | [providers/sub2api.md](providers/sub2api.md) 第 3.1 节 |
| `trae_sidecar` | OpenAI Chat | 小写 GLM，`config_name` 即模型名；README 未给出 32 个清单 | `glm-5.2`、`glm-5.3` | <https://github.com/JeffHu0912/trae2api> README |
| `deepseek_sidecar` | OpenAI Chat + Anthropic（非标路径、仅非流式） | 原生名 `deepseek-chat`；Anthropic 侧用带日期的 Claude 映射名 | `deepseek-chat`、`claude-sonnet-4-20250514` | <https://github.com/ColudAI/Deepseek2Go> README `config.json` |
| `qoder_sidecar` | OpenAI Chat + Anthropic Messages（上游还暴露 Responses，本仓未登记） | README 未列模型；模型名由产品线 / 地域前缀决定（`workbuddy/`、`global:`、`cn:`） | 无 README 样本（`unknown`） | <https://github.com/NextAgentX/qoder-trae-workbuddy-switch> README；[qoder.py](../apps/control/app/adapters/qoder.py#L400-L413) |

补充：代码注册表实际有 **14** 个 provider，比上表的 10 家多出四个主仓内置直连适配器：

| provider id | 协议 | 命名特征 | 证据 |
| --- | --- | --- | --- |
| `codar` | OpenAI Chat + Anthropic Messages + Responses | 不维护固定清单，模型目录由上游 `/v1/codar/models` 返回 | [codar.py](../apps/control/app/adapters/codar.py) |
| `codar_sidecar` | OpenAI Chat + Anthropic Messages | 与 `codar` 同一上游目录 | [codar_sidecar.py](../apps/control/app/adapters/codar_sidecar.py) |
| `trae` | OpenAI Chat | `config_name` 小写；Solo 默认 `glm-5.2`，Work 默认 `DeepSeek-V4-Flash-Official` | [trae.py](../apps/control/app/adapters/trae.py#L42-L43) |
| `qoder` | OpenAI Chat + Anthropic Messages + Responses | 与 `qoder_sidecar` 相同的产品线/地域前缀语义 | [qoder.py](../apps/control/app/adapters/qoder.py#L400-L413) |

## 9. 与现状的差异

| # | 维度 | 现状 | 新结构 | 影响 |
| --- | --- | --- | --- | --- |
| 1 | 别名形态 | `model_map` 是 `{别名: "目标"}` 的扁平字符串 map，只能 1:1 | 别名 → 候选列表（1:N）+ 权重 + 优先级 | 旧 map 无法表达权重与兜底，必须迁移 |
| 2 | 未知模型 | 回落到「第一个已启用且支持 chat 的 provider」 | 默认 404 fail closed；可用 `UNIFIED_MODEL_FALLBACK_FIRST=1` 临时恢复旧兜底 | **行为破坏**：以前能跑通的未知模型默认会报 404；开关只给存量客户端留窗口，不掩盖歧义 |
| 3 | 别名匹配 | `model in alias_map`，大小写敏感 | 别名键小写、请求侧 lower() | `GLM-5.3` / `glm-5.3` 这类上游差异可在别名层吸收 |
| 4 | 别名目标 | 目标串按 `provider/model` 二次解析，靠 `/` 猜 provider | 候选显式带 `provider` + 原样 `upstream_model` | 不再需要「目标串里必须带前缀」的约定 |
| 5 | 能力过滤 | 选完 provider 才检查端点能力，失败直接 501 | 选路前按端点协议 + 请求能力过滤候选 | 候选不支持时可兜底，而不是把 501 抛给客户端 |
| 6 | `/v1/models` | 只聚合上游 id，不含别名，不去重；失败静默跳过 | 公开别名（去重）；上游清单走 `include_upstream=1` | 客户端看到的模型面变了，需同步文档 |
| 7 | 配置读取 | 每个请求都读一次 SQLite `settings.model_map` | 进程内表 + 文件 mtime 热加载 | 去掉每请求 DB 读，但需要处理热加载失败 |
| 8 | 校验 | 保存接口只做 `strip()`，无 schema 校验 | YAML 文件 + 脚本校验（唯一/provider/权重/成环/能力） | 配置错误从「运行时静默」前移到「发布前拦截」 |
| 9 | provider 清单 | 文档写 10 家；注册表实际 14 家（多 `codar`、`codar_sidecar`、`trae`、`qoder`） | 校验脚本按 `BUILDERS` 推导 14 家 | 文档与服务商页统一为 14 家口径 |
| 10 | 兜底 | 无 | 429/5xx/timeout/能力不支持四级触发 | 新增一次请求内多候选重试；流式已开始后不换 |
| 11 | 审计 | `model_map` 保存无审计事件（`gateway` 有） | 建议文件变更走 git + 校验脚本 | 迁移后变更留痕方式改变 |

另外澄清一个命名：任务里提到的 `settings.py` 的 `model_map`，实际代码在 [routers/settings.py](../apps/control/app/routers/settings.py#L267-L293)（读写接口）和 [registry.py](../apps/control/app/registry.py#L164-L193)（解析），仓库里没有 `app/settings.py`。

## 10. 自检

```powershell
pwsh -File scripts/check-model-aliases.ps1 -Strict
```

当前结果：12 个别名、18 条候选、6 条兜底，退出码 0。负例（权重越界、provider 拼错、兜底成环、重复键、`unknown` + Strict）均已验证能拦截。
