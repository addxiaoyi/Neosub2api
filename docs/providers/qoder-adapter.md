# qoder（内置直连，本仓实现）

> 适配器：`apps/control/app/adapters/qoder.py`（clean-room，标准库自写加密原语，无三方依赖）。
> 本篇只写**本仓实现**的行为与配置；COSY 协议事实与上游字段形状见 [qoder.md](qoder.md)。
> 能力位：`chat`、`messages`、`models`、`responses`；
> 协议：`openai.chat.completions`、`anthropic.messages`、`openai.responses`。

## 1. 是什么

主仓内置的 Qoder COSY 网关客户端，**不经过任何 sidecar**。自定义 base64、AES-128-CBC、
RSA PKCS#1 v1.5、签名全部用标准库实现。对外同时提供 OpenAI Chat、Anthropic Messages、
OpenAI Responses 三套协议——后两套由适配器在内部转成 OpenAI 形状再走同一条 COSY 通道。

## 2. 鉴权、账号与身份（本仓自写）

- 每条 `provider_credentials` 是一个 Qoder 账号（多账号 CRUD 走既有服务商凭据通路，适配器不自建存储）：
  - 纯 access token（带不带 `Bearer ` 前缀都行，会剥掉）；
  - 或 JSON 身份凭据：`{"access_token": "...", "uid": "...", "user_type": "...",
    "nickname": "...", "organization_id": "...", "organization_name": "...",
    "refresh_token": "..."}`。`access_token` 也认 `token` / `api_key`，其余字段可选。
  - 一条凭据内的 `uid` / `user_type` / 组织等字段只作用于该账号，覆盖 `options` 里的同名默认值。
- 每个请求头由适配器构造：`authorization: Bearer COSY.<payload>.<md5 签名>`；
  `cosy-key` 是用服务端 RSA 公钥加密的临时 AES 密钥；`info` 是 AES 加密的身份 JSON；
  另有 `cosy-machineid / cosy-machinetoken / cosy-machinetype / cosy-user / cosy-date` 等设备头。
- 会话按 `uid + token 指纹` 缓存：token 或身份字段变化自动重建；鉴权失败时作废该账号会话，
  下次请求重新走 COSY 握手（登录态刷新，见 `_invalidate_session`）。

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
| `uid` | 由 token 派生 | 默认账号身份，影响会话缓存与设备头；可被单条 JSON 凭据覆盖 |
| `user_type` | `personal_professional_trial` | 默认身份 JSON 字段；可被单条凭据覆盖 |
| `nickname` / `organization_id` / `organization_name` / `refresh_token` | 空 | 默认身份 JSON 字段；可被单条凭据覆盖 |
| `extra_keys` | 空 | 追加账号（与 `api_keys` 同权），便于不建凭据行时临时补号 |
| `group` / `rotation_group` | `default` | 轮转组名，粘性/轮转状态按组隔离 |
| `key_weights` | 空 | 账号权重映射（`CredentialPool` 加权选择） |
| `max_attempts` | 3 | 单请求最多换号次数（上限 10，且不超过账号数 ×2） |
| `user_agent` | 按区域默认 | 覆盖 UA |
| `workbuddy_base` | 按区域默认 | workbuddy 直通 host |
| `timeout` | 300 | 上游超时秒数 |

## 5. 多账号选择与失败分档

- 选号复用 `apps/control/app/services/rotation.py`：`轮转组粘性 > 加权 > 轮转`，同一
  `session_id` / `user` 等粘性键优先回到上次成功账号；粘性键取不到就纯轮转。
- 单请求换号上限见 `max_attempts`；换号与换 host 是两个层次：同一账号内先按 fallback
  host 链试，账号失败才换下一个号。
- 可换号状态：`401/402/403/408/429` 与 `5xx`；`400/404` 属请求问题，直接返回不换号。
- 冷却档位与 `rotation.py` 完全一致（`COOLDOWN_BY_STATUS`）：
  `401/403` 600s、`402` 900s、`408` 60s、`429` 120s、`5xx` 60s；429 的 `Retry-After`
  只用于放大冷却，小于档位时不缩短。连续失败按 `TIER_FACTOR` 递增，达到阈值进熔断档。
- **唯一可用账号不冷却**：避免把真实 401/402 伪装成全池 429；多账号全部失败才返回 429。
- 网络错误/断流只解绑粘性并清轮转组，不判死账号。
- `fetch_models`、`health` 同样走账号池；`health` 附带 `accounts` 与 `rotation` 快照
  （含每个账号的状态与剩余冷却，仅指纹，不含明文凭据）。

## 6. 错误语义

| 触发 | 网关得到 | 说明 |
| --- | --- | --- |
| HTTP 401，或 code `12153`，或文本含 `SessionDead` / `AuthRejected` / `HardCredit` | 401（唯一账号）/ 429（多账号全失败） | 标记 `session_dead`，作废该账号会话并按 401/402 档冷却换号 |
| HTTP 429 | 429 | 解析 `retry_after` / `retryAfter` / `retry_after_seconds`；按 `max(120s, Retry-After)` 冷却后换号 |
| HTTP 404 | 404 | 端点不存在，不换号 |
| HTTP 5xx | 502 | 可换 fallback host；全部失败按 5xx 档冷却换号 |
| HTTP 400 | 400 | 请求被拒，不换号 |

换 host 条件：状态码 ∈ `{429, 500, 502, 503, 504}` 且非 `session_dead`，按 fallback 链逐 host 试；
全部失败抛最后一个错误。`max_tokens` 夹逼到 `[1, 131072]`，缺省 8192。

## 7. 已知限制

- 上游只有流式通道：**非流式响应是适配器把流聚合出来的**，行为等价但不是上游原生非流式。
- `usage` 依赖上游信封里的 `usage` 字段；上游不给时流水记 `usage_missing`（见 API-REFERENCE §10）。
- `list_models` 失败静默返回空列表，不报错。
- 请求骨架的部分业务字段子集未在真机验证（适配器 docstring 与 [qoder.md](qoder.md) 已标注）。
