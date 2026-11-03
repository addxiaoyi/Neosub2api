# codar2api —— 上游参考来源（非依赖）

> 主仓**不依赖**该进程。这里只保留上游信息与本地启动方式。
> 我方实现：`apps/control/app/adapters/codar_sidecar.py`；协议契约：`docs/providers/codar2api.md`。

## 上游信息

- 仓库：<https://github.com/AiXiaoc/codar2api>
- 许可：**MIT**（`LICENSE:1-3`，Copyright (c) 2026 AiXiaoc）
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

1. `.env` 或环境变量：`CODAR_SIDECAR_BASE=http://127.0.0.1:8801`；
2. 控制面「服务商」页启用 **Codar (codar2api)**，`api_key` 填面板里创建的 `sk-codar-…`；
3. 请求时用 `codar_sidecar/<model>` 指定该 provider，或带 `x-provider: codar_sidecar`。

主仓只做 HTTP 透传：`/v1/chat/completions`（JSON + SSE）、`/v1/messages`、`/v1/models`。
不复制、不内置上游源码或二进制；`/v1/responses` 未声明能力位。

## 注意

- 账号/签到/令牌续期/代理池都在 sidecar 内，主仓不重复实现。
- API Key 的模型白名单没包含实际模型时，上游会以 403 拒绝，表现为网关 403 原样回传。
- Codar 平台限「仅管理本人名下账号」，转售/共享风险由部署方承担，MIT 不豁免这一点。
