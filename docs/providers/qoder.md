# qoder 协议契约（clean-room 重写）

> 依据：Qoder 客户端（COSY 网关）的公开协议行为、`qoder.com` / `qoder.com.cn` 与 `qoder.sh`
> 两套地域端点、以及按该契约请求时观察到的 HTTP / SSE 行为。本文件只记录协议事实与实现取舍，
> 不含上游源码或二进制。
> 主仓实现：`apps/control/app/adapters/qoder.py`（MIT；自定义 base64、AES-128-CBC、RSA PKCS#1 v1.5、
> 签名全部用标准库自写，不引入三方依赖）。
> 能力位：`chat`、`messages`、`models`、`responses`；协议：`openai.chat.completions`、
> `anthropic.messages`、`openai.responses`。

## 1. 地域矩阵

| 区域 | 站点 | OpenAPI | 网关 | fallback |
| --- | --- | --- | --- | --- |
| `cn` | `https://qoder.com.cn` | `https://openapi.qoder.com.cn` | `https://gateway.qoder.com.cn` | 无 |
| `global` | `https://qoder.com` | `https://openapi.qoder.sh` | `https://api1.qoder.sh` | `api2.qoder.sh`、`api3.qoder.sh` |

模型名前缀决定区域：`cn:` / `global:`；无前缀时取 `options.region`（默认 `cn`）。
`options.gateway_base` 会**覆盖整条 fallback 链**（只打一个 host），用于自建代理或固定出口。

## 2. 产品线

`qoder` / `workbuddy` / `trae` 三条，模型名可用 `产品线/模型` 前缀，缺省 `qoder`：

| 产品线 | 上游 | 路径 | 说明 |
| --- | --- | --- | --- |
| `qoder` | `REGIONS[region].gateway` | `/algo/api/v2/service/pro/sse/agent_chat_generation?Encode=1` | COSY 加密信封 SSE |
| `workbuddy` | `REGIONS[region].workbuddy`（`cn` → `copilot.tencent.com`，`global` → `www.workbuddy.ai`） | `/v2/chat/completions` | OpenAI 风格直通，Bearer token |
| `trae` | 无 | — | 返回 501，指引改用 provider `trae`（Solo/Work 通道） |

`workbuddy_base` 可覆盖 workbuddy host。模型列表固定走 `REGIONS[region]` 的网关，遍历 fallback。

## 3. COSY 加密与签名（自写）

1. **自定义 base64**：字母表 `_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbfY^w.(kIQyXqWA!`，
   补齐符 `$`（不是 `=`）。
2. **键排序紧凑 JSON**：按 key 的 UTF-8 字节序；`null` 归一成空字符串。签名对这份字节计算。
3. **会话材料**：
   - 随机 16 字节 `temp_key`；
   - `cosy_key = base64(RSA_PKCS1v15(temp_key, 服务端公钥))`；
   - `info = base64(AES-128-CBC(sorted_compact(identity), temp_key))`；
   - `identity` 含 `aid` / `uid` / `user_type`（默认 `personal_professional_trial`）/
     `security_oauth_token`（即 `api_key`）/ `organization_id` / `refresh_token`。
   - 同一 token 的会话材料缓存复用；token 变化即重建。
4. **机器身份**：`sha512("machinetoken:" + uid)` 派生 `machine_token`（43 字符），
   `md5("<kind>:<uid>")` 派生 `machine_id` / `machine_type` / `session_id`。
5. **签名**：
   `md5(payload_b64 \n cosy_key \n date \n body \n strip_algo_prefix(path))`；
   路径以 `/algo` 开头时剥掉前 5 个字符。

请求头（COSY 路由）：

| 头 | 值 |
| --- | --- |
| `authorization` | `Bearer COSY.<payload_b64>.<sig>` |
| `cosy-key` | RSA 加密后的 temp_key（base64） |
| `cosy-date` | 秒级时间戳 |
| `cosy-user` / `cosy-machineid` / `cosy-machinetype` / `cosy-machinetoken` | 身份 |
| `cosy-clienttype` | `5` |
| `cosy-clientip` | `169.254.198.161` |
| `cosy-data-policy` | `AGREE` |
| `cosy-version` | `1.1.64` |
| `user-agent` | `cn` → `QoderWork/1.1.64`，`global` → `Qoder/1.1.64` |
| `x-model-key` / `x-model-source` | 模型名 / `system` |

请求体是**自定义 base64 编码的 JSON**，不是明文 JSON。

## 4. 请求体骨架

- `agent_id=agent_common`、`chat_task=FREE_INPUT`、`session_type=qodercli`、
  `stream=true`、`aliyun_user_type`；
- `model_config.key` = 去前缀后的模型名；
- `parameters`：`max_tokens`（1..131072，默认 8192）、`temperature`（默认 0.7）、可选 `top_p`；
- 消息里 `developer` 归一成 `system`；`content` 统一转文本；
- `request_id` / `chat_record_id` / `session_id` / `request_set_id` 均本地生成 UUID。

## 5. 三套对外协议

| 网关路径 | 转换 | 流式 |
| --- | --- | --- |
| `POST /v1/chat/completions` | OpenAI 直通（内部改写成 COSY 骨架） | `data: {chunk}` + `[DONE]` |
| `POST /v1/messages` | Anthropic Messages → OpenAI → COSY；响应再转回 Anthropic | `message_start` → `content_block_start` → `content_block_delta` → `content_block_stop` → `message_delta` → `message_stop` |
| `POST /v1/responses` | Responses → OpenAI → COSY | `response.created` → `response.output_text.delta` → `response.completed` |
| `GET /v1/models` | COSY `model/list` | JSON |

信封 SSE：上游每帧是 `data: {"body":"<转义后的内层 JSON>","statusCodeValue":200}`；
本仓先解 `body` 再分发给对应渲染器，`[DONE]` 透传。

聚合模式由本仓收流后合成：

- Chat：`chat.completion`，`usage.prompt_tokens` / `completion_tokens`；
- Messages：`message`，`usage.input_tokens` / `output_tokens`；
- Responses：`response`，`output[].content[].type=output_text`。

## 6. 地域代理（可选）

没有全局代理要求，按需配置：

```json
{
  "proxy": "http://127.0.0.1:7890",
  "region_proxies": {"cn": "http://127.0.0.1:7890", "global": "socks5://127.0.0.1:1080"}
}
```

- `region_proxies[region]` 优先，其次 `proxy`，都没有就直连；
- 直连时错误信息会追加「（未配置 <region> 地域代理，已直连；若为地域限制导致，请配置 options.proxy 或 region_proxies.<region>）」，
  不让调用方猜是网络问题还是地域封锁。

## 7. fallback 与错误分类

fallback 触发：状态码 ∈ `{429,500,502,503,504}`，或响应体含 `model_gate` / `model_rate` /
`model_blocked` / `server` / `timeout` / `not_found`；`session_dead` **不**换 host（换 host 也救不回来）。

| 上游 | 网关返回 | 处理 |
| --- | --- | --- |
| 401 或 `code=12153` 或含 `SessionDead` / `AuthRejected` / `HardCredit` | 429 | 标记 `session_dead`，要求重新登录刷新 token |
| 429 | 429 | 解析 `retry_after` / `retryAfter` / `retry_after_seconds`，缺省 30s 冷却；冷却期内同一 `region:uid` 直接 429 |
| 404 | 404 | 端点不存在 |
| ≥500 | 502 | 上游异常，可换 fallback host |
| 400 | 400 | 请求被拒 |
| 其余 | 原状态 | 附上游原文前 160 字 |

冷却按 `region:uid` 记忆，不跨区域；命中原生 429 时把冷却截止时间写入内存。

## 8. 验证与未验证部分

已验证（`apps/control/tests/test_f2.py`）：

- 加密原语：自定义 base64 官方向量、往返、`sorted_compact` 的 null 归一、AES-128-CBC 官方向量、
  签名官方向量、机器身份官方向量、RSA 输出长度与零字节规避；
- 路由：区域前缀与默认、产品线前缀、fallback host 链、直连提示、错误分类、冷却隔离；
- 协议：Anthropic / Responses 请求转换、信封解包、聚合、三套流式事件顺序；
- 网关路由：`/v1/responses` 与 `/v1/messages` 均已注册。

未在真机验证：

- `chat_body` 的业务字段是公开契约可见子集，真实账号下可能还有未列出的必填字段；
- `workbuddy` 直通只做了 Bearer + 路径转发，未验证其真实鉴权头（可能还需要额外租户头）；
- `trae` 前缀只做 501 指引，未在 qoder 侧实现跨产品线回退。
