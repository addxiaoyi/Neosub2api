# ZCode2api —— 上游参考来源（协议参考，仓内自研直连）

> 主仓**不依赖、不复制**上游进程与源码，只把上游对 ZCode 客户端的逆向结论提炼成协议契约。
> 我们的实现：`apps/control/app/adapters/zcode.py`，
> 协议契约：`docs/providers/zcode2api.md`。

## 上游信息

- 仓库：<https://github.com/addxiaoyi/ZCode2api>
- 许可：**MIT**（清单 `license_source: LICENSE 文件`，据 `upstreams/upstreams.yaml`）
- 分析基线：`analyzed_commit 25c57e6d94c018748f8f226198bf2c919ef7bba0`
  （`analyzed_at 2026-11-02`，`checked_at 2026-11-03`）
- 接入形态：**in-repo-rewrite**——上游为 ZCode 客户端 3.16.3 的逆向实测记录
  （官方未公开契约）；本仓自研直连 `zcode.z.ai` 的 Anthropic Messages 端点，
  **不再需要 zcgw-server sidecar**（清单 `notes`、`docs/providers/zcode2api.md` 开头声明）。

## 我方实现的文件清单

- `apps/control/app/adapters/zcode.py`（`ZCodeAdapter`）——唯一接入文件，主仓内置直连，无 sidecar。
- `docs/providers/zcode2api.md`——协议契约（端点/鉴权头/system 指纹/流式帧/错误码/选项）。
- `ProviderSpec`：status `implemented`、readiness `needs_config`、
  `default_base_url https://zcode.z.ai`、
  requires：「official_system（官方 system 原文，约 10701 字符；缺失时上游回 3012）」。

## 能力位与协议（按 `spec` 实读）

| 能力位 | 值 | 说明 |
| --- | --- | --- |
| `chat` | ✅ | `chat_completion`：Anthropic 事件翻译成 OpenAI chunk，流式 + 非流式 |
| `messages` | ✅ | `messages`：`/v1/messages` 原样透传（保留 `event:` 名、`[DONE]`、错误帧） |
| `models` | ✅ | `declared_models`：默认 `GLM-5.3-Flash`、`GLM-5.3`，可由 `options.models` 追加 |
| `responses` / `oauth` / `checkin` / `credits` / `register` / `sidecar` | ❌ | `stub_capabilities(chat=True, messages=True, models=True)`，其余默认 False |

协议：`openai.chat.completions`、`anthropic.messages`（与清单 `protocols` 一致）。

上游端点（契约文档 §1）：对话只走 `POST /api/v1/zcode-plan/anthropic/v1/messages`；
健康探测用 `GET /api/v1/zcode-plan/billing/balance?app_version=3.16.3`。
鉴权为同一枚账号 JWT 双头（`authorization: Bearer` + `x-api-key`）+ 设备指纹头；
`system` 必须是官方原文，否则上游回 **3012**——本仓不内置该原文，
由 `options.official_system` 注入（契约文档 §3）。

## 默认 base_url

`https://zcode.z.ai`。

## 已知差异 / 限制（契约文档 §6「未验证」项）

- 真实 JWT 与官方 system 原文均未拿到，3012 之外的链路未做真机回归。
- `billing/balance` 只用于健康探测，响应字段按宽松解析，真实字段名（单位/币种）未核对。
- `thinking` / `output_config.effort` 是否对所有套餐生效未验证。
- 设备指纹派生是「同输入稳定派生」，不保证与官方客户端逐字节一致；
  复现 3001 时优先用 `options.uid` 对齐账号。

## 如何同步上游更新

1. 巡检：`scripts/check-upstreams.ps1` 读取 `upstreams/upstreams.yaml` 比对远端 HEAD
   （清单文件头注释）；巡检只更新 `checked_at`，不推进 `analyzed_commit`。
2. 上游有新 commit 时按 `docs/UPSTREAM-SYNC.md` 先看 diff：重点核对端点路径、
   双头鉴权、`system` 指纹校验、SSE 事件名与错误码（3012/1005/3009/3001）；
   本仓为 `in-repo-rewrite`，协议变了才改 `zcode.py`。
3. 结论写回清单 `notes`，读完新 commit 后才推进 `analyzed_commit` / `analyzed_at`。
   本次补 README 只改 `our_files`，未动这些字段。
