# codar_sidecar（本仓实现）

> 适配器：`apps/control/app/adapters/codar_sidecar.py`。本篇只写**本仓实现**的行为与配置；
> 上游 codar2api 的协议与部署细节见 [codar2api.md](codar2api.md)。
> 能力位：`chat`、`messages`、`models`、`responses`、`sidecar`（扩展位 `count_tokens` 的端点归属见 [PROTOCOL-MATRIX.md](../PROTOCOL-MATRIX.md) §1.1）。（2026-11-05 已更正，原句逐字留档：「能力位：`chat`、`messages`、`models`、`sidecar`。」——原句漏记 `responses`，会让客户端以为该 provider 没有 `/v1/responses` 而绕行；真实声明见 `apps/control/app/adapters/codar_sidecar.py:30-39`，与 PROTOCOL-MATRIX §1 一致。）

## 1. 是什么

主仓对 codar2api sidecar 的 **HTTP 透传**适配器，继承 OpenAI 兼容基类。账号登录、令牌续期、
签到、代理池全部留在 sidecar 进程里，主仓不复制、不内置上游源码或二进制。

## 2. 对外端点（经统一网关）

| 网关路径 | 处理方式 |
| --- | --- |
| `/v1/chat/completions` | 直通 sidecar，JSON + SSE 原样转发 |
| `/v1/messages` | 直通 sidecar `/v1/messages`，流式与非流式都由 sidecar 处理 |
| `/v1/models` | 直通 sidecar |
| `/v1/responses` | 直通 sidecar `/v1/responses`（sidecar 原样转发，控制面不合成）；sidecar 不支持时由 sidecar 自己回 404/501 |

> 2026-11-05 已更正（原句逐字留档）：上表 `/v1/responses` 行原为「**未声明能力位，调用返回 501**——上游虽有此端点，但未经验证，不做「声明了但 501」的假能力」。与代码不符：`apps/control/app/adapters/codar_sidecar.py:30-39` 已声明 `responses=True`（同时声明 `count_tokens=True`），docstring `:7-9` 写明「/v1/responses 经契约交叉验证后声明」，`:56-58` 的 `responses()` 直接 `forward("/v1/responses", payload, stream=stream)`，`:60-62` 的 `count_tokens()` 转 `/v1/messages/count_tokens`；`protocols` 见 `:40`。**两者都是 sidecar 原样转发，不是控制面合成**（与 `gptgrok` / `qoder` 的合成实现本质不同）。误判后果：客户端看到 501 会绕开控制面直连 `http://127.0.0.1:8801`，从而绕过网关的鉴权、配额、失败限流与记账；运维也会去控制面重复加位。依据当前工作树未提交改动（`git status` 显示 M），文档按工作区代码写，未入库。与 [PROTOCOL-MATRIX.md](../PROTOCOL-MATRIX.md) §1 / §3 一致。

## 3. 鉴权

- 控制面「服务商」页的 `api_key` 填 **sidecar 的本地 API Key**（`sk-codar-` 前缀），转发时以
  `Authorization: Bearer` 注入；留空则 sidecar 按其自身鉴权拒绝。
- sidecar 内部的 Codar 账号凭证（登录、续期）由 sidecar 自管，控制面不接触。

## 4. 配置

| 项 | 值 |
| --- | --- |
| 环境变量 | `CODAR_SIDECAR_BASE`，默认 `http://127.0.0.1:8801` |
| `base_url` | 服务商页可改；改为环回/私网地址会被 `guard_upstream_url` 拒绝，内网部署需 `UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1` |
| `options` | 通用项（`timeout` / `proxy` / `extra_headers`） |

## 5. 错误语义

适配器本身不引入新的错误分类：sidecar 返回的状态码与错误体原样透传；sidecar 不可达时由网关层
表现为 502/503（不是 500）。

## 6. 已知限制

- sidecar 未启动时，状态页只反映「上游不可达」，账号池健康度要去 sidecar 自身的管理面板看。
- 签到、积分刷新等账号动作不在控制面（与 codebuddy 不同，没有账号域对接）。
- `/v1/responses` 已声明并由 sidecar 原样转发（`apps/control/app/adapters/codar_sidecar.py:56-58`）；是否真正可用取决于 sidecar 进程是否实现该端点，不是控制面能力位的问题。（2026-11-05 已更正，原句逐字留档：「`/v1/responses` 未接；需要时先验证上游行为再开能力位。」——原句把「控制面未声明」写成了既成事实，误判后果：运维会去适配器里重复加位，客户端则会绕开网关直连 sidecar；真实声明见 `codar_sidecar.py:30-39`。与 [PROTOCOL-MATRIX.md](../PROTOCOL-MATRIX.md) §1.2 一致——该节的「仍 501」清单一栏已不含 `codar_sidecar`。）

## 7. 仓内实现 vs 仍需 sidecar（2026-11-02）

同一上游现在有两个适配器，按能力分界使用：

| 能力 | `codar.py`（仓内直连） | `codar_sidecar.py`（HTTP 代理） |
| --- | --- | --- |
| `GET /v1/models` | 已实现：读 `/v1/codar/models` 目录 | 已实现：转发 sidecar |
| chat/completions（JSON + SSE） | 已实现：直连 Codar 私有 API | 已实现：转发 sidecar |
| messages（JSON + SSE） | 已实现：直连并补 Anthropic 版本头 | 已实现：转发 sidecar |
| responses | 已实现：直连转发；上游不支持时原样回 404/501 | 已声明能力位：直通 sidecar 原样转发（控制面不合成）；sidecar 不支持时按其返回码透传 |
| 多 token 轮换 / 401 续期 | 已实现：`options.tokens` + `/v1/token/refresh` | 由 sidecar 内部账号池负责 |
| 初始 token 获取（扫码/短信/本机 config.json） | 不实现，需用户粘贴 JWT | sidecar 管理面板完成 |
| 新 IP 验证 / 人机校验 | 不实现 | sidecar 完成 |
| 每日签到 / 领免费模型 | 不实现 | sidecar 后台任务完成 |
| 账号池调度 / 冷却 / 代理池 | 不实现（仅 token 轮换） | sidecar 完成 |
| 管理面板 / 本地 Key | 不实现 | sidecar 提供 |

> 2026-11-05 已更正（原单元格值逐字留档）：上表 `responses` 行的 `codar_sidecar.py` 列原为「未声明能力位，调用返回 501」。实际 `apps/control/app/adapters/codar_sidecar.py:30-39` 已声明 `responses=True`（同时声明 `count_tokens=True`），`:56-58` 的 `responses()` 直接 `forward("/v1/responses", payload, stream=stream)`——**sidecar 原样转发，控制面不合成**；`count_tokens` 同理转发 `/v1/messages/count_tokens`（`:60-62`）。误判后果：把该 provider 记成 responses 501，客户端会绕开网关直连 sidecar（丢鉴权、配额、限流、记账），选路时也会误判它不如 `codar.py`。依据当前工作树未提交改动（`git status` 显示 M），文档按工作区代码写，未入库；与 [PROTOCOL-MATRIX.md](../PROTOCOL-MATRIX.md) §1 / §1.1 / §1.2 一致。

选择建议：

- 只要模型转发、且能自己提供 Codar 账号 JWT → 用 `codar.py`，不需要启动任何进程。
- 需要扫码/短信登录、自动续期账号、签到或代理池 → 保留 `codar_sidecar.py` 与 sidecar。
- 两者可以同时注册；按 `provider_id` 分别启用，不会互相覆盖配置。
