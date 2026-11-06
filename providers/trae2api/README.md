# trae2api —— 上游参考来源（双路径）

**上游仓库**：`https://github.com/JeffHu0912/trae2api`
**许可证**：MIT ✅（许可证分层：可入主仓，可自由合入）

## 形态

本仓注册了两个 provider，**上游源码均不入本仓**：

1. **仓内直连 `trae`（一等 provider）**：`apps/control/app/adapters/trae.py`，
   自研 MIT 直连 Solo / Work（默认 `https://trae-api-cn.mchost.guru`），
   能力位 chat/models，协议 `openai.chat.completions`，status implemented、readiness ready。
2. **sidecar `trae_sidecar`（可选替代）**：`apps/control/app/adapters/trae_sidecar.py`
   纯 HTTP 代理，默认 `http://127.0.0.1:7871`，readiness needs_config（缺 sidecar 进程不通）。

上游 sidecar 自行管理账号池、OAuth 登录、Work 积分探测、SOLO→Work 自动降级；
控制面不提供 OAuth / 积分操作（`oauth`、`credits` 能力位为 False）。

> **2026-11-06 更正（原句逐字留档）**：第 1 条「readiness ready」与代码不符——`apps/control/app/adapters/trae.py:157` 的 readiness 为 `needs_config`。`ready` 只表示配置齐备，不等于真机跑通；当前 14 家全部 `needs_config`。误判后果：按旧句会把直连 `trae` 当成开箱即用，跳过真实凭证验证。

## 支持的协议

| 端点 | 说明 |
| --- | --- |
| `POST /v1/chat/completions` | OpenAI 兼容 SSE 流式 + 非流式 |
| `GET /v1/models` | trae2api 内置 32 个 config_name 静态模型表 |
| `/status` / `/healthz` | sidecar 自身健康检查 |

**不支持**：`/v1/messages`（两条路径均无 Anthropic 协议，`messages` 能力位为 False）。

## sidecar 快速启动（本地）

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
