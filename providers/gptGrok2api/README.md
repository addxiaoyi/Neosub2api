# gptGrok2api —— 上游参考来源（协议参考，仓内自研直连）

> 主仓**不依赖、不复制**上游进程与源码，只把上游的实测结论提炼成协议契约。
> 我们的实现：`apps/control/app/adapters/gptgrok.py`，
> 协议契约：`docs/providers/gptgrok2api.md`。

## 上游信息

- 仓库：<https://github.com/addxiaoyi/gptGrok2api>
- 许可：**MIT**（清单 `license_source: LICENSE 文件`，据 `upstreams/upstreams.yaml`）
- 分析基线：`analyzed_commit 0335d6279083da46ffc14ac0c8165d6acce3c1d1`
  （`analyzed_at 2026-11-02`，`checked_at 2026-11-03`）
- 接入形态：**in-repo-rewrite**——上游作为协议参考（逆向 ChatGPT Web / Grok Web 的实测记录），
  本仓按契约文档 clean-room 重写，不引入上游代码（清单 `notes`、`docs/providers/gptgrok2api.md` 开头声明）。

## 我方实现的文件清单

- `apps/control/app/adapters/gptgrok.py`（`GptGrokAdapter`）——唯一接入文件，主仓内置直连，
  无 sidecar。
- `docs/providers/gptgrok2api.md`——协议契约（通道/鉴权/请求体/流式帧/错误码/选项）。
- `ProviderSpec`：status `implemented`、readiness `needs_config`、
  `default_base_url https://chatgpt.com`、
  requires：「sentinel_token（ChatGPT sentinel token，本地无法生成；缺失时上游可能 403）」。

## 能力位与协议（按 `spec` 实读）

| 能力位 | 值 | 说明 |
| --- | --- | --- |
| `chat` | ✅ | `chat_completion`：双通道翻译成 OpenAI chunk，流式 + 非流式 |
| `models` | ✅ | `declared_models`：内置 `gpt-5*` / `grok-*` 静态表，可由 `options.models` 覆盖 |
| `messages` / `responses` / `oauth` / `checkin` / `credits` / `register` / `sidecar` | ❌ | `stub_capabilities(chat=True, models=True)`，其余默认 False |

协议：`openai.chat.completions` 单协议（与清单 `protocols` 一致；上游另有
responses/messages/images，本仓未接——清单 `notes`）。

双通道（契约文档 §1）：按模型名前缀分流（`grok-*` → Grok），`options.channel` 可强制指定；
ChatGPT 走 `POST /backend-api/conversation`（Bearer JWT + OAI 指纹头，SSE `data:` 帧、`[DONE]` 收尾），
Grok 走 `POST /rest/app-chat/conversations/new`（`sso` Cookie，逐行 JSON，
`result.response.token` 增量、`isSoftStop`/`finalMetadata` 收尾）。
多凭证轮换（`api_key` + `options.extra_keys`）、401/403/429 冷却换 key 按契约文档 §5。

## 默认 base_url

`https://chatgpt.com`。两通道默认各回官方域（Grok 为 `https://grok.com`），
`options.chatgpt_base` / `options.grok_base` 单通道覆盖；把 `base_url` 改成非默认值时
两通道统一走该入口（契约文档 §1）。

## 已知差异 / 限制（契约文档 §6「未验证」项）

- sentinel token 本地无法生成，只支持 `options.sentinel_token` 手工注入；未用真实凭证跑通。
- 健康探测端点 `GET /backend-api/me`、`GET /rest/models` 是推断，未实测；
  失败时 `health()` 返回 `connected=false`，可用 `options.health_path` 改探针。
- `oai-device-id`/`oai-session-id` 派生只保证同凭证稳定，不保证与官方客户端一致。
- 两家上游都没有原生 function calling：`tools` 不注入，tool 消息降级为 `[tool result]` 文本。
- 两通道都不返回 `usage`，记账侧按 `usage_missing` 处理。

## 如何同步上游更新

1. 巡检：`scripts/check-upstreams.ps1` 读取 `upstreams/upstreams.yaml` 比对远端 HEAD
   （清单文件头注释）；巡检只更新 `checked_at`，不推进 `analyzed_commit`。
2. 上游有新 commit 时按 `docs/UPSTREAM-SYNC.md` 先看 diff 再判性质：
   本仓为 `in-repo-rewrite`，协议变了才改 `gptgrok.py`，只影响文档描述就改
   `docs/providers/gptgrok2api.md` 的对应表格。
3. 结论写回清单 `notes`，读完新 commit 后才推进 `analyzed_commit` / `analyzed_at`。
   本次补 README 只改 `our_files`，未动这些字段。
