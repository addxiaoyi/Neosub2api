# 错误契约矩阵

结论先行：三套错误壳在本次覆盖范围内一致——`chat/completions` 与 `responses` 全程 OpenAI 壳，`messages` 全程 Anthropic 壳，`/api/*` 用 `detail` 壳，配额/失败限流的结构化拒因只在 OpenAI 壳可见。此前记录的三处偏差（框架级 404/405 落默认 `detail`、无启用 provider 的状态码与文档冲突、显式 provider 不校验 enabled）已在源码、文档与用例同步修复，`xfail` 标记全部移除，用例改为正向断言。

验证入口（`apps/control` 下）：

```powershell
$env:UNIFIED_TEST_CTRL_PORT="18133"
$env:UNIFIED_TEST_FAST_PORT="18134"
# 在 apps/control 目录下执行（python 解释器按本机环境选择，不写死盘符）
python -m pytest tests/test_error_contract.py -q
```

## 契约矩阵

`N/A` 表示该端点族不会产生这个状态码（例如上游 403 会合并成 401，`/api/*` 不代理上游 5xx）。

| 端点族 | 状态码 | 典型触发 | 外壳 | 关键字段 | Retry-After |
| --- | --- | --- | --- | --- | --- |
| `/v1/chat/completions` | 401 | 无密钥 / 无效密钥 / 两个鉴权头冲突 | OpenAI | `error.message/type/code/param`，`detail` 兼容字段；`code=invalid_api_key｜conflicting_auth_headers` | 否 |
| `/v1/chat/completions` | 403 | 上游 403 合并 | OpenAI | 对外 401，`error.upstream_status=403`、`upstream_code="403"` | 否 |
| `/v1/chat/completions` | 404 | 未知模型 / 没有任何已启用 provider 提供该模型 / 别名循环 | OpenAI | `error.code=model_not_found｜model_unavailable`，`detail.model`、`detail.candidates`（无候选时为空数组）；别名循环时 `detail.model`=请求入口、`detail.cycle`=入环成员数组 | 否 |
| `/v1/chat/completions` | 404 | `x-provider` 指向本地注册表没有的 provider | OpenAI | `error.type/code=provider_not_found`、`detail.provider`；message「未知的 provider：xxx」；真正的上游 404 仍是 `upstream_not_found` | 否 |
| `/v1/chat/completions` | 404 | `/v1/*` 路径不存在 | OpenAI | `error.code=route_not_found`，`detail` 与 `error.message` 同文 | 否 |
| `/v1/chat/completions` | 405 | 路径存在但方法不允许 | OpenAI | `error.code=method_not_allowed`，保留 `Allow` 头 | 否 |
| `/v1/chat/completions` | 400 | 缺 `model` / JSON 非法 / 模型歧义 | OpenAI | `error.code=missing_model｜invalid_json｜ambiguous_model`；歧义时 `detail.candidates[{provider,readiness}]` | 否 |
| `/v1/chat/completions` | 400 | `x-provider` 与 `provider/model` 前缀指向不同 provider | OpenAI | `error.type/code=conflicting_provider_hints`（不静默取头），`detail{model, provider=头值, header_provider, prefix_provider}` | 否 |
| `/v1/chat/completions` | 429 | 上游 429 | OpenAI | `error.type=rate_limit_error`、`code=upstream_rate_limited`、`upstream_status=429` | 是，默认 60 |
| `/v1/chat/completions` | 429 | 密钥配额超限 | OpenAI | `error.type=insufficient_quota`、`code=quota_exceeded`；`detail{type,layer,window,metric,used,limit,unit,reset_at,reset_after}` | 是，= `reset_after` |
| `/v1/chat/completions` | 429 | 同密钥失败率触发 | OpenAI | `error.type=rate_limit_error`、`code=failure_rate_limited`；`detail{type=failure_rate_limited,layer=key,metric=failures,window=sliding,unit=requests,used,limit,reset_after}` | 是，= `reset_after` |
| `/v1/chat/completions` | 502 | 上游 5xx 透传 | OpenAI | `error.type=api_error`、`code=upstream_unavailable`、`upstream_status=500`、`upstream_code="500"` | 否 |
| `/v1/chat/completions` | 503 | 上游 503 / 显式指定已停用 provider | OpenAI | 上游 503：`code=provider_unavailable`、`upstream_status=503`；停用 provider：`type=api_error`、`code=provider_disabled`、`detail.provider` | 否 |
| `/v1/chat/completions` | 501 | 能力位不支持 | OpenAI | `error.type=invalid_request_error`、`code=capability_unsupported`、`provider` | 否 |
| `/v1/responses` | 401 | 无密钥 / 无效密钥 / 冲突 | OpenAI | 同 chat | 否 |
| `/v1/responses` | 404 | 未知模型 | OpenAI | 同 chat | 否 |
| `/v1/responses` | 400 | 缺 `model` / JSON 非法 | OpenAI | 同 chat（歧义在 responses 上先撞 501 能力门槛，见备注） | 否 |
| `/v1/responses` | 429 | 上游 429 | OpenAI | 同 chat | 是，默认 60 |
| `/v1/responses` | 501 | provider 无 responses 能力 | OpenAI | `code=capability_unsupported`、`provider` | 否 |
| `/v1/responses` | 502 | 上游 5xx | OpenAI | 同 chat | 否 |
| `/v1/responses` | 503 | 上游 503 | OpenAI | `code=provider_unavailable`、`upstream_status=503` | 否 |
| `/v1/messages` | 401 | 无密钥 / 无效密钥 / 冲突 | Anthropic | `{type:"error", error{type,message,code}}`；`error.type=authentication_error` | 否 |
| `/v1/messages` | 403 | 上游 403 合并 | Anthropic | 对外 401；`error.upstream_status=403`、`error.upstream_code="403"` | 否 |
| `/v1/messages` | 404 | 未知模型 / 无启用 provider / `x-provider` 指向未知 provider / 路径不存在 | Anthropic | `error.type=not_found_error`、`error.code=model_not_found｜route_not_found｜provider_not_found`、`error.candidates` | 否 |
| `/v1/messages` | 405 | 路径存在但方法不允许 | Anthropic | `error.type=invalid_request_error`、`error.code=method_not_allowed`，保留 `Allow` 头 | 否 |
| `/v1/messages` | 400 | 缺 `model` / JSON 非法 | Anthropic | `error.type=invalid_request_error` | 否 |
| `/v1/messages` | 400 | 模型歧义 | Anthropic | `error.type=invalid_request_error`、`error.code=ambiguous_model`、`error.candidates[{provider,readiness}]` | 否 |
| `/v1/messages` | 429 | 上游 429 | Anthropic | `error.type=rate_limit_error`、`error.upstream_status=429` | 是，默认 60 |
| `/v1/messages` | 429 | 配额超限 / 失败限流 | Anthropic | `error.type=rate_limit_error` + `error.code`；结构化拒因不可见 | 是，= `reset_after` |
| `/v1/messages` | 501 | 能力位不支持（如 `trae_sidecar` 无 messages） | Anthropic | `error.type=invalid_request_error`、`error.provider` | 否 |
| `/v1/messages` | 502 | 上游 5xx | Anthropic | `error.type=api_error`、`upstream_status=500` | 否 |
| `/v1/messages` | 503 | 上游 503 / provider_unavailable | Anthropic | `error.type=overloaded_error`、`upstream_status=503`；**可重试** | 否 |
| `/v1/messages` | 503 | 显式指定已停用 provider（`code=provider_disabled`） | Anthropic | `error.type=api_error`、`error.code=provider_disabled`、`detail.provider`；**配置问题，重试无用** | 否 |
| `/api/*` | 401 | 未登录 | detail | `{"detail": "未登录或会话已过期"}` | 否 |
| `/api/*` | 403 | viewer 调管理员接口 | detail + 结构化 error | `detail="需要管理员权限"`、`error{type=permission_denied,code=insufficient_role,required_role=admin,role=viewer,message}` | 否 |
| `/api/*` | 404 | 未知路径 / 已删除 `/api/tasks` / 未知 provider | detail | `{"detail": "..."}`；未知 provider 文案带 id | 否 |
| `/api/*` | 400 | 参数校验（如 `status=bogus`） | detail | `detail` 为字符串并点名非法参数 | 否 |
| `/api/*` | 429 | 登录失败节流 | detail | `detail` 为字符串；无 `error` 对象 | 否 |
| `/api/*` | 503 | 无 oauth provider 时 `/api/auth/start` | detail | `detail="扫码登录不可用..."` | 否 |
| `/api/health/ready` | 503 | 没有任何可服务 provider | 自有 health JSON | `ok=false`、`status=error`、`checks.providers{ok,enabled,available,items}`、`unhealthy[]`、`request_id` | 否 |

备注：
- OpenAI 壳的 `param` 永远存在（当前实现固定 `null`），`detail` 永远保留以兼容老客户端。
- Anthropic 壳只允许 `type` + `error` 两个顶层键；`error.code` 现在**所有** `GatewayError` 都带（`anthropic_payload()` 统一输出，与 OpenAI 壳同值，两协议字段对称），`candidates` 仍只在模型解析失败时由 `ModelResolutionError` 附加。
- 上游 429 若不带 `Retry-After`，网关补 60 秒；配额/失败限流用拒因里的 `reset_after`（同一秒内可能差 1，测试按 ≤1 秒容差）。
- `responses` 端点当前没有已启用且声明该能力位的 provider，歧义用例会先被 501 能力门槛拦下；这是路由顺序的既定行为，不是壳异常。

显式 provider 指定的判定顺序（`model_resolve.resolve_model()`，三条 `/v1/*` 共用，已固化）：

1. **本地存在性**：`x-provider` 指向本地注册表没有的 provider → 404 `provider_not_found`（message「未知的 provider：xxx」；真正的上游 404 仍是 `upstream_not_found`，`from_provider_error()` 未动）；
2. **冲突**：`x-provider` 与 `provider/model` 前缀指向不同 provider → 400 `conflicting_provider_hints`，不静默取头，message 点名两个 provider id，`detail` 带 `header_provider`/`prefix_provider`，`error.provider` 取头值；
3. **停用**：→ 503 `provider_disabled`（`provider_disabled` 仍映射 `api_error`，上游 `provider_unavailable` 仍 `overloaded_error`）。

头与前缀同源时不整串透传，按前缀剥离后只把裸模型名给上游。以上新码与既有码一样：客户端重试决策**按 `error.code` 判断，不按 `error.type`**（400/404 是请求侧问题，重试无用），§3 的结论仍成立。别名循环 A→B→A **已修**（2026-11-04）：解析沿别名链逐跳走到尽头，撞环时 404 `model_not_found` 报**请求入口名**而不是链末端，`error.message` 写明「别名循环」并用 `A → B → A` 列出入环成员；OpenAI 壳 `detail` 同步给出 `detail.model`=请求入口与 `detail.cycle`=入环成员数组（从首次回环处截取），Anthropic 壳没有 detail，入口名与环成员都在 `error.message` 里。配套口径：非循环长链正常走到终点（A→B→C 报 C），别名目标不存在仍报 404 `model_not_found` + `detail.model`=链末端，自指 A→A 沿用「当没有别名」的旧口径（报 A，不贴循环标签）。本次未新增 `error.code`，`conflicting_provider_hints` 的 `detail.header_provider`/`prefix_provider` 仍是头/前缀语义——冲突判定发生在显式分支，别名链根本不参与，两条路径的 detail 不会混淆。

## 已修复的契约偏差

三处偏差均已对齐源码、文档与用例，`xfail(strict=True)` 全部移除，改为正向断言。

### 1. `/v1/*` 未匹配路由的 404/405 不走协议壳

处理：`app/main.py` 注册 `StarletteHTTPException` 处理器，路径属于 `/v1/*` 时由 `gateway_errors.framework_payload()` 按协议出壳：

- chat/responses：OpenAI 壳，`error.code = route_not_found`（404）/ `method_not_allowed`（405），`detail` 与 `error.message` 同文；
- messages：Anthropic 壳 `{type:"error", error{type,message,code}}`，404 为 `not_found_error`、405 为 `invalid_request_error`；
- 405 保留 `Allow` 头；`/api/*` 的 404/405 仍是 `{"detail": ...}`，未被改动。

### 2. 无已启用 chat provider：保留 404 `model_not_found`，改文档

判定保留 404，理由：

- 失败在「模型维度」：网关本身可用，鉴权、配额、日志链路都正常，只是没有任何已启用 provider 提供这个模型；
- `503` 在本项目已占用「provider 侧不可用」（上游 503 → `provider_unavailable`）与 sub2api 分组未配置。把「模型不存在」混进 503 会让监控把客户端写错模型名当成服务故障；
- `detail.candidates` 为空数组正好是「无候选」的证据，客户端可据此提示模型名错误。

因此删除 `docs/API-REFERENCE.md` 原第 840 行「无已启用的 chat provider」的描述，`test_v1_no_enabled_provider_reports_model_not_found` 改为正向断言 404（chat 与 messages 两种壳都覆盖）。

### 3. 显式 `x-provider` / 前缀不校验 enabled

处理：`model_resolve.resolve_model()` 在显式路径与前缀路径解析出 adapter 后统一校验启用集合（`_require_enabled()`）。停用即返回 **503 `provider_disabled`**：OpenAI 壳 `error.type=api_error`，Anthropic 壳 `error.type=api_error`（`gateway_errors.py:184-190` 对 503 + `code=provider_disabled` 特判，不再落进 `_ANTHROPIC_TYPES[503]=overloaded_error`），`detail.provider` 回显 provider id，message 明确「已停用，请先在服务商页启用」。上游真过载的 503（`provider_unavailable`）仍是 `overloaded_error`，保留可重试语义——**同为 503，客户端按 `error.code` 判断是否重试，不要按 `error.type`**。

选 503 而不是 404 的理由：与既有设计文档 `docs/model-routing.md`（「provider 存在但未启用 → 503」）以及 `registry.resolve()` 的既有取舍一致；404 保留给「模型/路径不存在」。停用属于运维可恢复的服务侧状态，语义上更接近 503。

注意：能力位检查（501）发生在启用校验之后，所以「停用且能力位不支持」的 provider 现在先报 503 `provider_disabled`；`test_v1_messages_501_is_anthropic_shell` 通过临时启用 `trae_sidecar` 保留 501 覆盖。

## 仍缺的契约面

- **错误壳的 402 / 408 / 504**：上游计费拒绝、超时归并尚未纳入矩阵；当前只覆盖到 5xx。
- **`/api/*` 的 422**：Pydantic 校验仍走 FastAPI 默认 `{"detail": [...]}`，未按管理面统一形状；`/v1/*` 的 404/405 已在本轮修复。
- **Anthropic 结构化拒因**：配额/失败限流只在 `error.message` 里带中文说明，客户端拿不到 `window/used/limit/reset_after`。若需要程序化处理，应在 `error` 内补结构化键（同时保持协议兼容）。
- **合法密钥的 401 衍生场景**：`x-api-key` 合法、Bearer 非法等组合只测了冲突与无效，未测单头合法 / 双头同值。
- **流式断流后的计费/额度一致性**：已验证 `stats` 与 `/api/logs` 口径一致（不计成功、计入 5xx 失败），未验证失败是否会错误扣配额流水。
