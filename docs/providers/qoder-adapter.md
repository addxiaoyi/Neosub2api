# qoder（内置直连，本仓实现）

> 适配器：`apps/control/app/adapters/qoder.py`（clean-room，标准库自写加密原语，无三方依赖）。
> 本篇只写**本仓实现**的行为与配置；COSY 协议事实与上游字段形状见 [qoder.md](qoder.md)。
> 能力位：`chat`、`messages`、`models`、`responses`；
> 协议：`openai.chat.completions`、`anthropic.messages`、`openai.responses`。

## 1. 是什么

主仓内置的 Qoder COSY 网关客户端，**不经过任何 sidecar**。自定义 base64、AES-128-CBC、
RSA PKCS#1 v1.5、签名全部用标准库实现。对外同时提供 OpenAI Chat、Anthropic Messages、
OpenAI Responses 三套协议——后两套由适配器在内部转成 OpenAI 形状再走同一条 COSY 通道。

## 2. 鉴权与请求头（本仓自写）

- `api_key` 填 Qoder access token（带不带 `Bearer ` 前缀都行，会剥掉）。
- 每个请求头由适配器构造：`authorization: Bearer COSY.<payload>.<md5 签名>`；
  `cosy-key` 是用服务端 RSA 公钥加密的临时 AES 密钥；`info` 是 AES 加密的身份 JSON；
  另有 `cosy-machineid / cosy-machinetoken / cosy-machinetype / cosy-user / cosy-date` 等设备头。
- 会话按 uid 缓存；token 变化自动重建会话。

## 3. 路由规则

- **区域**：模型名前缀 `cn:` / `global:`；无前缀取 `options.region`（默认 `cn`）。
  `global` 区有 `api2/api3.qoder.sh` 两个 fallback host；`options.gateway_base` 会**覆盖整条
  fallback 链**（只打一个 host）。
- **产品线**：模型名前缀 `qoder/` / `workbuddy/` / `trae/`；无前缀取 `options.product`
  （默认 `qoder`）。`trae` 产品线**不走 COSY**，调用返回 501 并指引改用 provider `trae`；
  `workbuddy` 是 OpenAI 风格直通（`/v2/chat/completions`），只支持 chat 协议。
- `messages` / `responses` 仅支持 `qoder` 产品线，其它产品线调这两个协议返回 501。

## 4. 配置项

无专用环境变量。全部走服务商页 `api_key` + `options`：

| options 键 | 默认 | 说明 |
| --- | --- | --- |
| `region` | `cn` | `cn` / `global` |
| `product` | `qoder` | `qoder` / `workbuddy` / `trae` |
| `gateway_base` | 空 | 覆盖 COSY 网关与 fallback 链 |
| `region_proxies` | 空 | 按区域的代理映射，如 `{"global": "http://..."}` |
| `proxy` | 空 | 全局出口代理（区域代理优先） |
| `uid` | 由 token 派生 | 用户标识，影响会话缓存与设备头 |
| `user_type` | `personal_professional_trial` | 身份 JSON 字段 |
| `nickname` / `organization_id` / `organization_name` / `refresh_token` | 空 | 身份 JSON 字段 |
| `user_agent` | 按区域默认 | 覆盖 UA |
| `workbuddy_base` | 按区域默认 | workbuddy 直通 host |
| `timeout` | 300 | 上游超时秒数 |

## 5. 错误语义

| 触发 | 网关得到 | 说明 |
| --- | --- | --- |
| HTTP 401，或 code `12153`，或文本含 `SessionDead` / `AuthRejected` / `HardCredit` | 429 | 标记 `session_dead`，需重新登录刷新 token；不触发换 host |
| HTTP 429 | 429 | 解析 `retry_after` / `retryAfter` / `retry_after_seconds`；并按该值（缺省 30s）记冷却窗，冷却内请求直接 429 |
| HTTP 404 | 404 | 端点不存在 |
| HTTP 5xx | 502 | 可换 fallback host |
| HTTP 400 | 400 | 请求被拒 |

换 host 条件：状态码 ∈ `{429, 500, 502, 503, 504}` 且非 `session_dead`，按 fallback 链逐 host 试；
全部失败抛最后一个错误。`max_tokens` 夹逼到 `[1, 131072]`，缺省 8192。

## 6. 已知限制

- 上游只有流式通道：**非流式响应是适配器把流聚合出来的**，行为等价但不是上游原生非流式。
- `usage` 依赖上游信封里的 `usage` 字段；上游不给时流水记 `usage_missing`（见 API-REFERENCE §10）。
- `list_models` 失败静默返回空列表，不报错。
- 请求骨架的部分业务字段子集未在真机验证（适配器 docstring 与 [qoder.md](qoder.md) 已标注）。
