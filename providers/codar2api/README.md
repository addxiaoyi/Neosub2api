# codar2api —— 上游参考来源（非依赖）

> 主仓运行时**不强依赖**该进程（扫码 / 短信登录与签到仍需它）。这里只保留上游信息与本地启动方式。
> 我方实现：`apps/control/app/adapters/codar.py`（仓内直连）与 `codar_sidecar.py`（HTTP 代理）；
> 协议契约：`docs/providers/codar2api.md`。

## 上游信息

- 仓库：<https://github.com/AiXiaoc/codar2api>
- 许可：**MIT（许可证分层：可入主仓）**（`LICENSE:1-3`，Copyright (c) 2026 AiXiaoc）
- 形态：Go 1.26 单进程网关（仅标准库），管理面板编译进二进制
- 默认监听：`127.0.0.1:8801`（`compose.yaml`）
- 对外端点：`/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/messages`、
  `/v1/messages/count_tokens`、`/ping`、`/health`

## 启动方式（上游源码不进主仓）

在**主仓之外**的任意目录执行：

```bash
git clone --depth 1 https://github.com/AiXiaoc/codar2api.git
cd codar2api
go build -o codar2api ./cmd/codar
./codar2api            # 或按上游 compose.yaml 用 Docker 启动
```

首次启动后打开 `http://127.0.0.1:8801/` 的管理面板：设置管理员口令 → 导入 Codar 账号
（扫码 / 短信 / 粘贴 JWT）→ 创建 API Key（模型范围选「全部」或包含要用的模型）。

## 主仓如何对接

**路径 A：仓内直连（一等 provider `codar`）**

- `apps/control/app/adapters/codar.py`，默认 base_url `https://api.claw.speedai.com`；
- 能力位 chat/messages/models/responses，协议 `openai.chat.completions`、
  `anthropic.messages`、`openai.responses`；需账号 JWT（readiness needs_config）；
- 扫码 / 短信登录与签到仍需下述 sidecar。

**路径 B：sidecar（`codar_sidecar`）**

1. `.env` 或环境变量：`CODAR_SIDECAR_BASE=http://127.0.0.1:8801`；
2. 控制面「服务商」页启用 **Codar (codar2api)**，`api_key` 填面板里创建的 `sk-codar-…`；
3. 请求时用 `codar_sidecar/<model>` 指定该 provider，或带 `x-provider: codar_sidecar`。

sidecar 路径主仓只做 HTTP 透传：`/v1/chat/completions`（JSON + SSE）、`/v1/messages`、
`/v1/models`。不复制、不内置上游源码或二进制；`/v1/responses` 在 sidecar 路径未声明
能力位（仓内直连 `codar` 有）。

## 注意

- 账号/签到/令牌续期/代理池都在 sidecar 内，主仓不重复实现。
- API Key 的模型白名单没包含实际模型时，上游会以 403 拒绝，表现为网关 403 原样回传。
- Codar 平台限「仅管理本人名下账号」，转售/共享风险由部署方承担，MIT 不豁免这一点。
- **v0.1.4 升级差异（仅提示词层面，`/v1` 契约、鉴权、SSE 不受影响，无需改适配器）**：旧版只在命中原有
  DeepSeek Harness 文案时替换，新版只要 system/developer 提示词开头没有 `You are an AI agent running on the
  Codar runtime.` 就插入该身份串；`identity_first_line_fill` 缺省由 `false` 翻为 `true`。裸 data 目录升级即生效；
  面板存过 `settings.json` 且该键为 `false` 的部署不受影响。要保持旧行为：升级前在 sidecar 面板关闭该键。
- v0.1.4 另增 `proxy_connect_retry`、`proxy_connect_switch`、`restricted_request_block` 三开关，缺省全关，勿随手开：
  开启 403 拉黑后，相同接口 + 完整 JSON 指纹的请求被挡会在 sidecar 内直接回 403，主仓只能透传成上游错误；
  清除黑名单必须进 sidecar 面板，主仓无对应接口。
