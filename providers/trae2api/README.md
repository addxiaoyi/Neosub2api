# trae2api sidecar 说明

**上游仓库**：`https://github.com/JeffHu0912/trae2api`
**许可证**：MIT ✅（可自由合入）

## 形态

**独立 sidecar 进程**（Go 编译成单个二进制），与 unified-2api 通过 HTTP 通信。
本仓只包含：

- 控制面适配器：`apps/control/app/adapters/trae_sidecar.py`（纯 HTTP 代理）
- 本 README

**源码不入本仓**——trae2api 自行管理账号池、OAuth 登录、Work 积分探测、SOLO→Work 自动降级。

## 支持的协议

| 端点 | 说明 |
| --- | --- |
| `POST /v1/chat/completions` | OpenAI 兼容 SSE 流式 + 非流式 |
| `GET /v1/models` | trae2api 内置 32 个 config_name 静态模型表 |
| `/status` / `/healthz` | sidecar 自身健康检查 |

**不支持**：`/v1/messages`（无 Anthropic 协议）。

## 快速启动（本地）

```bash
# 1. clone（不在本仓目录）
git clone --depth 1 https://github.com/JeffHu0912/trae2api.git ../trae2api

# 2. 创建空 auths 目录（无账号时 sidecar 仍会返回 503，但链路通了）
cd ../trae2api && mkdir -p auths && go run ./cmd/server -config config.example.json

# 3. unified-2api 把 trae_sidecar provider 的 Base URL 指向 http://127.0.0.1:7871
#    （或设环境变量 TRAE_SIDECAR_BASE=http://127.0.0.1:7871）
```

## 无真实凭证时的预期

- `GET /v1/models` → 正常返回 32 个模型 ✅
- `POST /v1/chat/completions` → `503 all accounts unavailable`（预期，链路通）

## 有真实凭证后

把 `auths/trae-{uid}.json` 放进 trae2api 的 `auths/` 目录，sidecar 自动加载。
