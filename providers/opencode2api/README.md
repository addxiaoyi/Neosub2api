# opencode2api —— 上游参考来源（非依赖）

> 主仓**不再依赖**该进程。这里只保留上游参考信息与可选的对照编排。
> 我们的实现：`apps/control/app/adapters/opencode.py`，协议契约：`docs/providers/opencode2api.md`。

## 上游信息

- 仓库：<https://github.com/jasonxu114514/opencode2api>
- 许可：**GPL-3.0（许可证分层：只能进程隔离）**——上游代码只允许独立进程方式接入，
  本仓不包含其源码、也不链接/修改它；主仓实现为 clean-room 自研 MIT
- 能力：OpenCode Zen / Zen Go 的 OpenAI 与 Anthropic 兼容网关

## 主仓如何实现同等能力

- 直接对接 Zen/Go 上游（`OPENCODE_UPSTREAM_BASE`，默认 `https://opencode.ai/zen`）；
- 自行实现 `/v1/models`、`/v1/chat/completions`、`/v1/messages` 转发与 SSE 直通；
- 自行实现可重试状态码（401/403/408/429/5xx）换 key 与 `tier=zen|go` 路径选择；
- 只提炼协议契约，不复用上游函数体、变量命名或注释。

## 可选对照实现

`docker-compose.fragment.yml` 仅用于「想对比上游行为」时手工启动，不作为主仓运行依赖：

```bash
docker compose -f providers/opencode2api/docker-compose.fragment.yml up -d
```

启动后可在服务商页把 `opencode` 的 base_url 临时指到 `http://127.0.0.1:7869` 做 A/B 对照。
