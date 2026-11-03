# codar_sidecar（本仓实现）

> 适配器：`apps/control/app/adapters/codar_sidecar.py`。本篇只写**本仓实现**的行为与配置；
> 上游 codar2api 的协议与部署细节见 [codar2api.md](codar2api.md)。
> 能力位：`chat`、`messages`、`models`、`sidecar`。

## 1. 是什么

主仓对 codar2api sidecar 的 **HTTP 透传**适配器，继承 OpenAI 兼容基类。账号登录、令牌续期、
签到、代理池全部留在 sidecar 进程里，主仓不复制、不内置上游源码或二进制。

## 2. 对外端点（经统一网关）

| 网关路径 | 处理方式 |
| --- | --- |
| `/v1/chat/completions` | 直通 sidecar，JSON + SSE 原样转发 |
| `/v1/messages` | 直通 sidecar `/v1/messages`，流式与非流式都由 sidecar 处理 |
| `/v1/models` | 直通 sidecar |
| `/v1/responses` | **未声明能力位，调用返回 501**——上游虽有此端点，但未经验证，不做「声明了但 501」的假能力 |

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
- `/v1/responses` 未接；需要时先验证上游行为再开能力位。

## 7. 仓内实现 vs 仍需 sidecar（2026-11-02）

同一上游现在有两个适配器，按能力分界使用：

| 能力 | `codar.py`（仓内直连） | `codar_sidecar.py`（HTTP 代理） |
| --- | --- | --- |
| `GET /v1/models` | 已实现：读 `/v1/codar/models` 目录 | 已实现：转发 sidecar |
| chat/completions（JSON + SSE） | 已实现：直连 Codar 私有 API | 已实现：转发 sidecar |
| messages（JSON + SSE） | 已实现：直连并补 Anthropic 版本头 | 已实现：转发 sidecar |
| responses | 已实现：直连转发；上游不支持时原样回 404/501 | 未声明能力位，调用返回 501 |
| 多 token 轮换 / 401 续期 | 已实现：`options.tokens` + `/v1/token/refresh` | 由 sidecar 内部账号池负责 |
| 初始 token 获取（扫码/短信/本机 config.json） | 不实现，需用户粘贴 JWT | sidecar 管理面板完成 |
| 新 IP 验证 / 人机校验 | 不实现 | sidecar 完成 |
| 每日签到 / 领免费模型 | 不实现 | sidecar 后台任务完成 |
| 账号池调度 / 冷却 / 代理池 | 不实现（仅 token 轮换） | sidecar 完成 |
| 管理面板 / 本地 Key | 不实现 | sidecar 提供 |

选择建议：

- 只要模型转发、且能自己提供 Codar 账号 JWT → 用 `codar.py`，不需要启动任何进程。
- 需要扫码/短信登录、自动续期账号、签到或代理池 → 保留 `codar_sidecar.py` 与 sidecar。
- 两者可以同时注册；按 `provider_id` 分别启用，不会互相覆盖配置。
