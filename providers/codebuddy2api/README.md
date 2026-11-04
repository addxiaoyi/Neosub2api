# codebuddy2api —— 上游参考来源（sidecar）

> 主仓**不复制**上游源码，只经管理端 HTTP 代理。这里记录上游信息、我方实现与同步方式。
> 我们的实现：`apps/control/app/adapters/codebuddy.py`。
> 协议契约文档：**未读到**——`docs/providers/` 下没有 codebuddy 对应文件，
> 本仓暂无协议级契约文档，以下协议事实直接取自适配器代码。

## 上游信息

- 仓库：<https://github.com/addxiaoyi/codebuddy2api>
- 许可：**MIT**（清单 `license_source: LICENSE 文件`，据 `upstreams/upstreams.yaml`）
- 分析基线：`analyzed_commit 92b8b910417c8bb24412ad62b99cef9adf96adb6`
  （`analyzed_at 2026-11-02`，`checked_at 2026-11-03`）
- 接入形态：**sidecar**——Python 管理端（默认 `7864`）+ Go 数据面 workbuddy2api（`7863`），
  主仓由 `CodeBuddyAdapter` 代理 `127.0.0.1:7864`，不落上游代码（清单 `notes`）。

## 我方实现的文件清单

- `apps/control/app/adapters/codebuddy.py`（`CodeBuddyAdapter`）——唯一的接入文件，
  无独立 sidecar 适配器、无 `docs/providers/` 契约文档。
- `ProviderSpec`：status `implemented`、readiness `needs_config`、
  `default_base_url http://127.0.0.1:7864`、
  requires：「7864 管理端进程 + wbt_ token（缺了账号同步、签到与设置下发全部失败）」。
- 鉴权：管理端作用域化 API Token（`Authorization: Bearer wbt_...`）；
  注意删除账号 / 重启 / 设置下发在管理端要求会话级管理员，wbt token 可能被拒，
  此时如实透传管理端错误（适配器 docstring）。

## 能力位与协议（按 `spec` 实读）

| 能力位 | 值 | 代码依据（`codebuddy.py`） |
| --- | --- | --- |
| `chat` | ✅ | `chat_completion` → 转发 `/v1/chat/completions` |
| `messages` | ✅ | `messages` → 转发 `/v1/messages` |
| `models` | ✅ | `fetch_models` → `/v1/models`，失败退管理端 `/api/models` |
| `oauth` | ✅ | `start_login` → `POST /api/auth/start`，`poll_login` → `GET /api/auth/poll` |
| `checkin` | ✅ | `checkin_all` → `POST /api/accounts/checkin-all` |
| `credits` | ✅ | `refresh_credits` → `POST /api/accounts/refresh-credits?force=` |
| `responses` / `register` / `sidecar` | ❌ | `stub_capabilities` 未覆盖，默认 False |

协议：`openai.chat.completions`、`anthropic.messages`（双协议，与清单 `protocols` 一致）。

账号管理与设置面走管理端 `/api/*`：`sync_accounts` → `GET /api/accounts`、
`health` → `GET /api/status`、账号启停/备注/删除 → `/api/accounts/{file}/...`、
设置下发与拉取 → `POST|GET /api/settings/upstream`（只透传 `PUSH_GROUPS` 分组）、
`restart` → `POST /api/restart`。

## 默认 base_url

`http://127.0.0.1:7864`（管理端；chat/models 也经管理端网关代理到 Go 数据面 7863）。

## 已知差异 / 限制

- 无 `docs/providers/codebuddy2api.md` 契约文档，协议细节只有适配器代码一层依据。
- `fetch_models` 的 `/api/models` 回退是上游静态表，属管理端兜底而非数据面真实 `/v1/models`。
- 管理端会话级管理员限制（见上）会在账号删除、重启、设置下发时暴露为透传错误。
- 上游进程实际启动命令：**未读到**（三处允许来源中均无记录），本仓只按默认端口接入。

## 如何同步上游更新

1. 巡检：`scripts/check-upstreams.ps1` 读取 `upstreams/upstreams.yaml` 比对远端 HEAD
   （清单文件头注释）；`checked_at` 只记巡检日期，**巡检不推进 `analyzed_commit`**。
2. 有更新时先按 `docs/UPSTREAM-SYNC.md` 的顺序评估 diff：LICENSE → 路由/handler →
   请求响应结构与 SSE → 错误码 → 启动参数，再决定是否改适配器。
3. 只有读完新 commit 才手工推进 `analyzed_commit` / `analyzed_at`，并同步
   `notes`；本次补 README 只改 `our_files`，未动这些字段。
