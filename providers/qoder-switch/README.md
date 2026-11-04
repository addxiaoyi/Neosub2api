# qoder-trae-workbuddy-switch 上游参考（双路径）⚠️ 许可证风险

**上游仓库**：`https://github.com/NextAgentX/qoder-trae-workbuddy-switch`
**许可证**：**PolyForm Noncommercial 1.0.0**（许可证分层：PolyForm-NC，商用禁用）🔴

## 重要声明

> 本仓**不拷贝、不修改、不分发** qoder 的任何源码，共注册两个 provider：
>
> 1. **`qoder_sidecar`**（`apps/control/app/adapters/qoder_sidecar.py`）：HTTP 代理，
>    让用户自行 clone 并启动 qoder 的 buddy-switch-gateway sidecar 进程后，通过标准 HTTP 调用。
>    **属禁止商用部分：商用前必须移除本 provider 或取得 NextAgentX 的书面商用授权。**
> 2. **`qoder`**（`apps/control/app/adapters/qoder.py`）：clean-room 自研 MIT 直连 COSY 网关，
>    一等 provider（status implemented，chat/messages/models/responses 能力位，readiness ready），
>    不含上游代码，**不受 PolyForm-NC 限制**。

## 形态

独立 sidecar 进程（Rust `buddy-switch-gateway` crate，Axum HTTP 网关）。
sidecar 自行管理：
- 多产品线 × 多地域账号池（Trae / WorkBuddy / Qoder × 国内 / 国际）
- 账号一键切换工具（Tauri 桌面壳）
- 降级链（fallback hop，失败自动换下一个上游）
- Retry-After 智能退避

unified-2api 在 sidecar 路径只做 HTTP 代理。

## 支持的协议

`qoder`（仓内直连）与 `qoder_sidecar`（代理）两个 provider 对外暴露同一组端点：

| 端点 | 说明 |
| --- | --- |
| `POST /v1/chat/completions` | OpenAI 兼容 |
| `POST /v1/messages` | Anthropic 兼容 |
| `GET /v1/models` | 模型列表 |
| `POST /v1/responses` | OpenAI 新协议（两个适配器均声明 responses 能力位并做流式/非流式转发；sidecar 网关路由 `lib.rs:135-140` 已暴露该端点） |

## 快速启动（本地）

```bash
git clone --depth 1 https://github.com/NextAgentX/qoder-trae-workbuddy-switch.git ../qoder
cd ../qoder
# 需 Rust 1.75+ + Cargo；用 Docker 模式更简单：docker compose up buddy-switch-gateway
# 网关默认 127.0.0.1:7873，在 unified-2api 服务商页把 qoder_sidecar 的 Base URL 设为 http://127.0.0.1:7873
```

## 合规建议

以下限制针对**上游代码 / sidecar 路径**（`qoder_sidecar`，7873）：

- **PolyForm Noncommercial 1.0.0 禁止任何商业使用**——包括：
  - 内部商业项目（即使不收费）
  - 托管/代理服务
  - 收费分发
  - SaaS 集成
- 个人非商业自用：理论上允许，但边界模糊，仍可能被作者主张
- 商用：**必须移除 `qoder_sidecar`**（含 sidecar 进程），或联系作者 NextAgentX 取得书面商用授权；
  clean-room 直连的 `qoder.py` 不含上游代码，可保留

## 上游跟进记录

### 2026-11-02：`802e510` → `93d873c`（含 `0f972a5`）

判定：**协议未变（本仓接入面）**，适配器零改动。

- **LICENSE 未变**：仍是 PolyForm Noncommercial 1.0.0（`LICENSE` 首行 Required Notice）。
  上面「许可风险」全部继续有效。
- **路由表未变**：`crates/buddy-switch-gateway/src/lib.rs:135-140`
  （`/v1/models`、`/v1/chat/completions`、`/v1/messages`、`/v1/responses`）。
- **chat / messages / 错误 / 鉴权 / 启动参数全部不在 diff 中**：`routes/chat.rs`、
  `routes/messages.rs`、`routes/models.rs`、`protocol/openai.rs`、`protocol/anthropic.rs`、
  `error.rs`、`apikey.rs`、`unified/`、`Dockerfile`、`docker-compose.yml`、`package.json`。
- 实际变化只有两处：
  1. `/v1/responses` 的请求改写（`protocol/responses.rs:52-235`、`294-380`）：
     `type:"namespace"` 工具扁平化成 `ns__name` 发给上游、出站还原 `namespace` + 原名；
     自定义工具降级。**当时**属本仓未接入的协议面（此后已补 responses 能力位与
     `/v1/responses` 转发，现为已接入）。
  2. 图片能力字段（`model_capability.rs:60-77`、`311-337`）：上游不声明时，
     对 `deepseek-v4.1-flash` 下发 `supports_images=true`、`image_input_source="known"`。
     `/v1/models` 外层结构不变（`routes/models.rs:206-217`），只是新增可选字段值。
- 账号/会话侧改动（`session.rs`、`trae/profile.rs`、`qoder/profile.rs`、`vscode_cn_inject.rs`）
  属于 sidecar 内部实现，不影响 HTTP 契约。

复现方式：

```bash
git diff --stat 802e510 93d873c                     # 17 个文件
git diff --stat 802e510 93d873c -- crates/buddy-switch-gateway/src/routes \
  crates/buddy-switch-gateway/src/protocol/openai.rs \
  crates/buddy-switch-gateway/src/protocol/anthropic.rs \
  crates/buddy-switch-gateway/src/error.rs \
  crates/buddy-switch-gateway/src/apikey.rs \
  crates/buddy-switch-gateway/src/unified Dockerfile docker-compose.yml package.json
# 上一条输出为空
```

注意：评估期间上游 HEAD 从 `0f972a5` 前进到 `93d873c`，两段提交都已纳入核对，
清单基线记为 `93d873c`。

### 2026-11-04：`93d873c` → `5cf62da`（远端 HEAD，02:11Z 前移）

判定：**网关协议面未变**，`qoder.py` / `qoder_sidecar.py` 零改动，清单 `analyzed_commit`
仍记 `93d873c`（读码基线不推进）。

- `routes/{chat,messages,responses}.rs` 只给内部日志补 `cached_tokens` 字段
  （从 usage 读 `prompt_cache_hit_tokens` / `cached_tokens` / `cache_read_input_tokens`，
  只入日志不进响应体），端点、鉴权、请求/响应结构、SSE 均未变。
- `buddy-switch-server` 新增 `/api/sessions/export` 与 `/api/sessions/import{,/preview}`
  管理端点，属加法，不在 `/v1` 协议面。
- 末段仅 `state.rs` 注释澄清 base_url 不含 `/v1`；接入地址补 `/v1` 的修复在上游前端
  `src/lib/gateway-url.ts` 与文档，网关路由未动。
- LICENSE 未变，PolyForm-NC 商用限制继续有效。
