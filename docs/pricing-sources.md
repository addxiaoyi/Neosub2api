# 价格表数据来源与维护说明

对应文件：[config/prices.yaml](../config/prices.yaml)。结构依据 [docs/m3-billing-plan.md](m3-billing-plan.md) §3。
本文件记录每条种子价格的出处、抓取日期、没有公开定价的模型，以及价格随时间变化时的处理方式。

## 1. 结论摘要

| 项 | 数量 |
| --- | --- |
| 已定价模型（按 price_books.model 精确字符串计） | 12 |
| 已定价模型（去掉 `glm-5.3` / `GLM-5.3` 大小写重复） | 11 |
| 价格行 | 39 |
| 覆盖的 token_class | input / output / cache_read / cache_write（4 种） |
| 未定价模型 | 4 |
| 别名表覆盖 | 16 / 16 个上游模型名，全部有着落（12 个有价 + 4 个显式未定价） |

> **2026-11-04 已更正（上表原文保留作历史）**：2026-11-04 复核后——已定价模型（按 `price_books.model` 精确字符串计）**13**、去掉 `glm-5.3` / `GLM-5.3` 大小写重复后 **12**、价格行 **42**、未定价 **3**；别名表覆盖仍为 16 / 16（**13 个有价 + 3 个显式未定价**）；token_class 仍为 4 种（`hy3` 无 cache_write 报价）。以 `scripts/check-prices.ps1` 2026-11-04 实跑结果为准（价格 42 条、未定价 3 条、PASS）。

已定价：`claude-sonnet-4`、`gpt-4o`、`gpt-5`、`glm-5.2`、`glm-5.3`、`GLM-5.3`、`qwen3.8-max`、`kimi-k3`、`grok-4.6`、`minimax-m3`、`deepseek-chat`、`deepseek-v4.1-flash`。（原表述，2026-11-04 已更正，见下）

> **2026-11-04 已更正**：已定价 13 个——上列 12 个不变，新增 `hy3`：input **0.132** / output **0.528** / cache_read **0.033** USD/MTok（腾讯 TokenHub 国际站美元价目表，`https://www.tencentcloud.com/document/product/1300/78937`，核实日期 2026-11-04，已补入 `config/prices.yaml`；官方未公布 cache_write 报价）。

未定价：`kimi-k2.7`、`hy3`、`hy3-preview`、`DeepSeek-V4-Flash-Official`。（原表述，2026-11-04 已更正，见下）

> **2026-11-04 已更正**：未定价 3 个——`kimi-k2.7`、`hy3-preview`、`DeepSeek-V4-Flash-Official`；`hy3` 已按上文定价移出。三个残留项各自的查证过程与排除理由见 §4 表下更正块。

## 2. 抓取口径

- 抓取日期：**2026-11-02**（Asia/Shanghai）。
- 只收 **厂商官网明确写出的美元价**（USD / 1M tokens）。人民币价目表只用于交叉核对，不做汇率换算；只有人民币报价的模型进 `unpriced`。
- 不采信第三方博客、比价站、OpenRouter 等市场报价；这类价格不是厂商标价。
- 金额写法：`price_per_mtok` 保留官网原始小数位（字符串），`price_micro_per_1m = price_per_mtok × 1_000_000`，必须是整数。M3 直接写入 `price_books.price_micro_per_1m`。
- `provider` 统一为 `*`：本仓接入的上游（muse、gptgrok、opencode、codebuddy、zcode、trae 等）都是转售/逆向网关，没有公开结算价。种子行记录的是**厂商官网标价**，作为对外计费基准或兜底价，不代表上游实际成本。M3 解析时 `'*'` 按通配行处理（方案 §3.1），后续拿到某个上游的真实结算价时，追加具体 `provider` 行即可，精确行优先于通配行。

## 3. 已收录价格与出处

价格单位均为 USD / 1M tokens。

| 模型 | input | output | cache_read | cache_write | 来源 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| `claude-sonnet-4` | 3.00 | 15.00 | 0.30 | 3.75 | [Anthropic Pricing](https://platform.claude.com/docs/en/about-claude/pricing) | 缓存写入取 5 分钟档（1 小时为 6.00）；页面链接的 PDF 标注 2026-05-12 生效 |
| `gpt-4o` | 2.50 | 10.00 | 1.25 | — | [OpenAI gpt-4o](https://developers.openai.com/api/docs/models/gpt-4o) | 模型页标 Deprecated，但价格仍公开；无缓存写入费 |
| `gpt-5` | 1.25 | 10.00 | 0.125 | — | [OpenAI gpt-5](https://developers.openai.com/api/docs/models/gpt-5) | 同上；无缓存写入费 |
| `glm-5.2` | 1.40 | 4.40 | 0.26 | — | [Z.AI Pricing](https://docs.z.ai/guides/overview/pricing) | 人民币表 8 / 28 元，缓存存储限时免费，无 cache_write 报价 |
| `glm-5.3` / `GLM-5.3` | 1.40 | 4.40 | 0.26 | — | [Z.AI Pricing](https://docs.z.ai/guides/overview/pricing) | 同一价格；大小写两行是因为 `price_books.model` 精确匹配，上游写法不一致 |
| `qwen3.8-max` | 2.00 | 6.00 | 0.25 | 2.50 | [Aliyun Model Studio qwen3.8-max](https://www.alibabacloud.com/help/en/model-studio/qwen3-8-max) | 取 Global 范围美元价；页面标注更新时间 2026-10-10；人民币表 12 / 36 / 1.5 / 15 元 |
| `kimi-k3` | 3.00 | 15.00 | 0.30 | 3.00 | [Kimi Pricing](https://platform.kimi.ai/docs/pricing/chat) | 缓存写入取 5 分钟档（1 小时为 6.00） |
| `grok-4.6` | 2.00 | 6.00 | 0.50 | — | [xAI Pricing](https://docs.x.ai/developers/pricing) | 取 <200k 输入的短上下文档；≥200k 为 4.00 / 12.00 / 1.00 |
| `minimax-m3` | 0.30 | 1.20 | 0.06 | — | [MiniMax Pay-as-you-go](https://platform.minimax.io/docs/guides/pricing-paygo) | 取 ≤512k 输入、标准档、永久五折后的现价；原价 0.60 / 2.40 / 0.12 |
| `deepseek-chat` | 0.15 | 0.60 | 0.003 | — | [DeepSeek Pricing](https://api-docs.deepseek.com/quick_start/pricing) | 官方已弃用该名（2026-07-24），按 `deepseek-v4-flash` 非思考模式计价；取非高峰价 |
| `deepseek-v4.1-flash` | 0.15 | 0.60 | 0.003 | — | [DeepSeek Pricing](https://api-docs.deepseek.com/quick_start/pricing) | 官方现用 id 是 `deepseek-flash`（版本 DeepSeek-V4.1-Flash-1017）；上游 README 的写法需在 M3 路由层归一化；取非高峰价 |

补充说明：

- DeepSeek 高峰价为非高峰的 2 倍（input 0.30 / output 1.20 / cache 0.006）。M3 价格表没有时段维度，种子里先存非高峰价，实际计费会偏低，需在 M3-1 决定是否加时段维度或统一取高峰价。
- Grok、MiniMax 的上下文分档同理：M3 没有“输入长度档位”字段，种子里存基准档，长上下文档位未表达。
- `reasoning` 不单独建行。方案 §2.1 已规定缺省回落到 `output` 价；OpenAI、Anthropic、DeepSeek 的推理 token 都按输出计费。
- 部分厂商的缓存写入价按 TTL 分档（Claude 5m/1h、Kimi 5m/1h）。种子里取 5 分钟档，1 小时价写进 `note`。

## 4. 没有公开定价的模型

| 模型 | 原因 | 已核查来源 |
| --- | --- | --- |
| `kimi-k2.7` | 官方价目表只有 `kimi-k2.7-code`（0.95 / 4.00，缓存 0.19）和 `kimi-k2.6`，没有 `kimi-k2.7` 这一条；无法确认 codebuddy 的 `kimi-k2.7` 是否等同 `kimi-k2.7-code` | [Kimi Pricing](https://platform.kimi.ai/docs/pricing/chat) |
| `hy3` | 腾讯云只公布人民币价（输入 1.2 元、输出 4 元、缓存命中 0.4 元/百万），没有官方美元价目表；OpenRouter 的美元价属第三方市场报价，未采用 | [腾讯云 TokenHub 计费](https://cloud.tencent.com/document/product/1759/127342) |
| `hy3-preview` | 同上，只有人民币报价；且 preview 与正式版是否同价未说明 | 同上 |
| `DeepSeek-V4-Flash-Official` | trae 适配器的内部模型标识（`apps/control/app/adapters/trae.py` 的 `DEFAULT_WORK_MODEL`），不是 DeepSeek 官方价目表里的 id；无法证明与 `deepseek-flash` 同价 | [DeepSeek Pricing](https://api-docs.deepseek.com/quick_start/pricing)、`apps/control/app/adapters/trae.py` |

> **2026-11-04 已更正（上表四行原文逐字保留作历史）**：2026-11-04 复核结论——
>
> 1. **`hy3` 已定价并移出 unpriced**：按腾讯 TokenHub 国际站美元价目表补入 `config/prices.yaml`——input **0.132** / output **0.528** / cache_read **0.033** USD/MTok（核实日期 2026-11-04）。原行「腾讯云只公布人民币价、没有官方美元价目表」的说法作废；原行所引链接 `cloud.tencent.com/document/product/1759/127342` **不是价目表**——该链接实际是 ADP 套餐计费页，不能作为 token 价出处。
> 2. **`kimi-k2.7`**：Kimi 官方价目表无此 id，只有 `kimi-k2.7-code` / `kimi-k2.7-code-highspeed`；且 K2 系列 2026-10-31 起对新用户停售、2026 年底下线，取不到可用结算价。（原行「无法确认是否等同 `kimi-k2.7-code`」的查证保留作历史，本条为最新结论。）
> 3. **`hy3-preview`**：腾讯两个价目表只有 `Hy3` 与 `hy4-preview` 行，预览版无 token 价；新闻稿里的 "approximately USD" 是折算值，不可作结算价。
> 4. **`DeepSeek-V4-Flash-Official`**：Trae 企业版价目表仅有人民币价 3 / 9 / 0.1 元/百万；美元刊例价只列 `DeepSeek-V4-Flash`，未单列该 id，无法证明同一价。
>
> 未定价因此为 3 个，与 §1 更正一致。

处理方式：这些模型只进 `unpriced`，不生成价格行。M3 解析不到价格时按方案 §2.1 的 `usage_missing` 逻辑记 0 并留痕，不会静默套用别的模型价格。

## 5. 价格随时间变化的处理策略

### 5.1 `effective_from` 怎么填

- 种子行的 `effective_from = 1793548800`，即 **2026-11-02 00:00:00 +08:00**，语义是“本仓首次确认该价格的抓取时间”。
- 厂商公告了准确生效日期的，改用公告日期的 epoch 秒，并在 `note` 写明“厂商生效日”。
- 厂商没有公告生效日期、只看到当前页面价格的，用抓取时间，并在 `note` 写明“首次确认时间，厂商生效日期未公布”。不要把抓取时间伪装成厂商生效时间。
- 预公告的未来调价可以直接插 `effective_from` 为未来时间的行；M3 的解析 SQL 只在 `effective_from <= 当前时间` 时命中，到点自动切换。

### 5.2 历史价怎么保留

- **append-only**：改价只插新行，不 UPDATE、不 DELETE 旧行。唯一键是 `(provider, model, token_class, effective_from)`，同一组合允许多个生效版本。
- 旧行就是历史价。任何一笔 `billing_ledger` 通过 `price_snapshot_id → price_book_snapshots.lines_json` 能还原当时的 `price_id` 和 `price_micro_per_1m`，不依赖当前表里的最新价。
- 请求进行中改价也不会出现“两套价格”：准入时解析并冻结快照，流式结束仍按快照结算（方案 §3.2）。
- 定期复核（建议每月一次或厂商发公告时）：确认页面价格，变则追加新行，同时更新本文件的抓取日期和备注；`config/prices.yaml` 里旧行保持不动。

### 5.3 币种与汇率

- 本表只落美元。人民币价目表只用于核对，不按汇率折算成美元后入库。
- 如果后续确实需要按人民币定价，先确定汇率来源与更新策略（例如 `billing.fx_rate` 设置键 + 明确的汇率出处），再新增独立价格行；不要在种子数据里写“估算汇率”。
- MiniMax 的“永久五折”是厂商长期折扣，种子记录折后现价并在 `note` 保留原价；若折扣取消，按 5.2 追加新行。

## 6. 校验

```powershell
pwsh -File scripts/check-prices.ps1
pwsh -File scripts/check-prices.ps1 -Strict
```

校验内容：provider/model/token_class/effective_from 组合重复、token_class 枚举、价格为正、小数位不超过 6 位、`price_per_mtok × 1e6` 是否无损得到整数、`price_micro_per_1m` 是否与换算结果一致、`source`/`note` 是否为空，以及与 `config/model-aliases.yaml` 的覆盖比对（每个上游模型必须“有价”或“显式未定价”）。

- 普通模式：`unpriced`、`source: unknown`、覆盖缺口只报 WARN，退出码 0。
- `-Strict`：上述情况全部算 ERR，退出码 1。用于发布前检查“是否还有模型没定价”。

## 7. M3 接入前的待确认项

1. **provider 维度**：当前全是 `*` 通配行。若要对不同上游定不同售价/成本，需要补具体 provider 行；否则所有上游共用同一套厂商标价。
2. **时段价**：DeepSeek 峰谷价、MiniMax/Claude/Kimi 的 TTL 分档、Grok/Qwen 的上下文分档，M3 表结构都没有对应维度。当前取基准档，计费会有偏差。
3. **模型名归一化**：`deepseek-v4.1-flash`、`deepseek-chat`、`DeepSeek-V4-Flash-Official` 与官方 id 的对应关系需要在路由/计费层显式处理，不能依赖字符串前缀猜测。
4. **未定价模型**：`kimi-k2.7`、`hy3`、`hy3-preview`、`DeepSeek-V4-Flash-Official` 上线前必须补价，或由业务确认“记 0 留痕”。（原表述，2026-11-04 已更正，见下）

> **2026-11-04 已更正**：未定价现为 `kimi-k2.7`、`hy3-preview`、`DeepSeek-V4-Flash-Official` 三个（`hy3` 已于 2026-11-04 按 TokenHub 国际站价补入），处置口径不变。
