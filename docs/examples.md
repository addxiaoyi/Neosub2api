# 调用示例：Chat / Responses / Messages

三个入口共用同一套鉴权与模型解析，差别只在协议形状（请求体、`stream` 帧、错误体）。

| 协议 | 端点 | 支持它的 provider（2026-11 现状） |
| --- | --- | --- |
| OpenAI Chat Completions | `POST /v1/chat/completions` | 除 grok_register（只做注册）外的全部 provider |
| OpenAI Responses | `POST /v1/responses` | 仅 codar、qoder |
| Anthropic Messages | `POST /v1/messages` | codar、codebuddy、opencode、qoder、sub2api、zcode 及 codar/deepseek/qoder sidecar；trae 系只有 chat |

前置条件：网关已在 `http://127.0.0.1:8010` 启动，已签发以 `u2a_` 开头的网关密钥。下文用两个环境变量占位：

```bash
export U2A_BASE=http://127.0.0.1:8010
export U2A_KEY=u2a_xxxxxxxx
```

PowerShell 写成 `$env:U2A_BASE="http://127.0.0.1:8010"`、`$env:U2A_KEY="u2a_xxxxxxxx"`，其余命令形态不变。

## 鉴权：Authorization 或 x-api-key

网关 `/v1/*` 的密钥校验同时接受下面两种头，值都是同一把网关密钥；**不读 query 参数**。

```text
Authorization: Bearer <gateway key>    # 优先级最高，行为与以前一致
x-api-key: <gateway key>               # Anthropic 官方 SDK 默认发这个
```

`Authorization` 里的 `Bearer ` 前缀可以省，裸 key 也接受。两个头都带时值必须相同，否则返回 `401 conflicting_auth_headers`（鉴权头冲突），网关不会静默挑一个用。两个都不带返回 `401 invalid_api_key`，错误体按端点协议出壳（chat / responses 用 OpenAI 形状，messages 用 Anthropic 形状）。默认必须带密钥；只有打开 `gateway.allow_anonymous`（环境变量 `UNIFIED_ALLOW_ANONYMOUS_GATEWAY=1`）后才允许匿名调用。各语言 SDK 里对应关系是：

- OpenAI SDK：`api_key=...` 默认发 `Authorization: Bearer`，可以直接用。
- Anthropic SDK：`api_key=...` 默认发 `x-api-key`，可以直接用；不需要再改成 `auth_token`。

`X-Api-Key`（注意是另一个头，带横线）仍只对 `GET /api/quota`、`GET /api/quota/logs` 这类读接口生效，对 `/v1/*` 无效。

## 模型名：前缀与裸名

两种写法都合法：

| 写法 | 示例 | 解析方式 |
| --- | --- | --- |
| 带 provider 前缀 | `zcode/GLM-5.3` | 取 `/` 前一段当 provider id，剩余部分是上游模型名；provider 未启用直接 404 |
| 裸模型名 | `GLM-5.3` | 在已启用 provider 的聚合目录里按 id 精确匹配；命中一家就用，命中多家报 `400 ambiguous_model` |

补充规则（与 `apps/control/app/model_resolve.py` 一致）：

- 裸名在聚合目录里找不到：默认 `404 model_not_found`；若该模型只存在于**未启用**的 provider，错误码是 `404 model_unavailable`，候选写进顶层 `detail.candidates`（`/v1/messages` 则挂在 `error.candidates`）。
- 想恢复旧行为（找不到就落第一个可用 provider 兜底），启动时设 `UNIFIED_MODEL_FALLBACK_FIRST=1`；默认关闭是为了避免静默串到别的 provider。
- 加 `X-Provider: <provider id>` 头可强制指定 provider，此时模型名应为上游原名，不再做前缀/目录解析。
- 目标 provider 不支持该协议：`501 capability_unsupported`（例如对 zcode 调 `/v1/responses`）。
- 别名映射（settings 里的 `model_map`，键是请求里的名字、值是 `<provider>/<model>`）在裸名目录匹配之前生效；写 `provider/model` 最不容易出意外。
- 想先看有哪些模型：`curl -H "Authorization: Bearer $U2A_KEY" $U2A_BASE/v1/models`（换成 `-H "x-api-key: $U2A_KEY"` 等价）。

下面统一用 `zcode/GLM-5.3`：`GLM-5.3` 由 zcode 适配器静态声明，能直接对照代码核验。若你只启用了 opencode，先查 `/v1/models` 换成本机实际 id，写法不变。

## 覆盖矩阵

| 协议 | curl 非流式 | curl 流式 | Python 非流式 | Python 流式 | Node 非流式 | Node 流式 |
| --- | --- | --- | --- | --- | --- | --- |
| `/v1/chat/completions` | 有 | 有（`-N` + `stream:true`） | 有（openai SDK） | 有（openai SDK） | 有（openai SDK） | 有（openai SDK） |
| `/v1/responses` | 有 | 有（`-N` + `stream:true`） | 有（openai SDK） | 有（openai SDK） | 有（openai SDK） | 有（openai SDK） |
| `/v1/messages` | 有 | 有（`-N` + `stream:true`） | 有（anthropic SDK） | 有（anthropic SDK） | 有（anthropic SDK） | 有（anthropic SDK） |

每节先给非流式、再给流式。curl 的流式与非流式**请求体只差 `"stream": true`**，另外加 `-N` 关掉缓冲。

## 1. `/v1/chat/completions`

### 1.1 带前缀：`zcode/GLM-5.3`

curl，非流式：

```bash
curl -sS $U2A_BASE/v1/chat/completions \
  -H "Authorization: Bearer $U2A_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "zcode/GLM-5.3",
    "messages": [{"role": "user", "content": "用一句话说明 SSE 和 WebSocket 的区别"}]
  }'
```

把 `-H "Authorization: Bearer $U2A_KEY"` 换成 `-H "x-api-key: $U2A_KEY"` 等价，三个 `/v1` 端点都支持。

curl，流式（`-N` 是为了让 `data:` 帧逐条刷出来）：

```bash
curl -sS -N $U2A_BASE/v1/chat/completions \
  -H "Authorization: Bearer $U2A_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "zcode/GLM-5.3",
    "stream": true,
    "messages": [{"role": "user", "content": "写一个 3 行的 Python 快排"}]
  }'
```

Python（openai SDK），非流式：

```python
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["U2A_KEY"], base_url=os.environ["U2A_BASE"] + "/v1")

resp = client.chat.completions.create(
    model="zcode/GLM-5.3",
    messages=[{"role": "user", "content": "用一句话说明 SSE 和 WebSocket 的区别"}],
)
print(resp.choices[0].message.content)
```

Python（openai SDK），流式：

```python
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["U2A_KEY"], base_url=os.environ["U2A_BASE"] + "/v1")

stream = client.chat.completions.create(
    model="zcode/GLM-5.3",
    stream=True,
    messages=[{"role": "user", "content": "写一个 3 行的 Python 快排"}],
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

Node（openai），非流式：

```js
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: process.env.U2A_KEY,
  baseURL: `${process.env.U2A_BASE}/v1`,
});

const resp = await client.chat.completions.create({
  model: "zcode/GLM-5.3",
  messages: [{ role: "user", content: "用一句话说明 SSE 和 WebSocket 的区别" }],
});
console.log(resp.choices[0].message.content);
```

Node（openai），流式：

```js
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: process.env.U2A_KEY,
  baseURL: `${process.env.U2A_BASE}/v1`,
});

const stream = await client.chat.completions.create({
  model: "zcode/GLM-5.3",
  stream: true,
  messages: [{ role: "user", content: "写一个 3 行的 Python 快排" }],
});
for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
}
```

### 1.2 裸模型名：`GLM-5.3`

裸名在聚合目录里唯一命中时，网关会补上 provider，等价于上面那一组。把上文的 `"zcode/GLM-5.3"` 换成 `"GLM-5.3"` 即可，其余参数、SDK 写法完全不变：

```bash
curl -sS $U2A_BASE/v1/chat/completions \
  -H "Authorization: Bearer $U2A_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "GLM-5.3", "messages": [{"role": "user", "content": "你好"}]}'
```

```python
resp = client.chat.completions.create(
    model="GLM-5.3",
    messages=[{"role": "user", "content": "你好"}],
)
```

```js
const resp = await client.chat.completions.create({
  model: "GLM-5.3",
  messages: [{ role: "user", content: "你好" }],
});
```

如果同时启用了多个都声明 `GLM-5.3` 的 provider，会返回：

```json
{"error": {"message": "模型 GLM-5.3 在多个已启用的 provider 中都存在（zcode、opencode），请写成 provider/model 指定",
           "type": "ambiguous_model", "param": null, "code": "ambiguous_model"},
 "detail": {"message": "模型 GLM-5.3 在多个已启用的 provider 中都存在（zcode、opencode），请写成 provider/model 指定",
            "model": "GLM-5.3", "candidates": [{"provider": "zcode", "readiness": "ready"}, ...]}}
```

此时回到 `zcode/GLM-5.3`，或加 `-H "X-Provider: zcode"`。

## 2. `/v1/responses`

只有 codar、qoder 实现了 Responses，模型目录由上游 `/models` 决定，因此下面用 `<模型 id>` 占位——先 `GET /v1/models` 取一个真实 id，再替换。裸名写法同理，要求该 id 在聚合目录里只出现一次。

### 2.1 带前缀：`codar/<模型 id>`

curl，非流式：

```bash
curl -sS $U2A_BASE/v1/responses \
  -H "Authorization: Bearer $U2A_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "codar/<模型 id>",
    "input": "把下面这句话翻成英文：网关统一了三种协议"
  }'
```

curl，流式：

```bash
curl -sS -N $U2A_BASE/v1/responses \
  -H "Authorization: Bearer $U2A_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "codar/<模型 id>",
    "input": "给一个 3 步的代码评审清单",
    "stream": true
  }'
```

Python（openai SDK），非流式：

```python
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["U2A_KEY"], base_url=os.environ["U2A_BASE"] + "/v1")

resp = client.responses.create(
    model="codar/<模型 id>",
    input="把下面这句话翻成英文：网关统一了三种协议",
)
print(resp.output_text)
```

Python（openai SDK），流式：

```python
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["U2A_KEY"], base_url=os.environ["U2A_BASE"] + "/v1")

stream = client.responses.create(
    model="codar/<模型 id>",
    input="给一个 3 步的代码评审清单",
    stream=True,
)
for event in stream:
    if event.type == "response.output_text.delta":
        print(event.delta, end="", flush=True)
```

Node（openai），非流式：

```js
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: process.env.U2A_KEY,
  baseURL: `${process.env.U2A_BASE}/v1`,
});

const resp = await client.responses.create({
  model: "codar/<模型 id>",
  input: "把下面这句话翻成英文：网关统一了三种协议",
});
console.log(resp.output_text);
```

Node（openai），流式：

```js
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: process.env.U2A_KEY,
  baseURL: `${process.env.U2A_BASE}/v1`,
});

const stream = await client.responses.create({
  model: "codar/<模型 id>",
  input: "给一个 3 步的代码评审清单",
  stream: true,
});
for await (const event of stream) {
  if (event.type === "response.output_text.delta") process.stdout.write(event.delta);
}
```

### 2.2 裸模型名

把上面所有 `codar/<模型 id>` 改成 `<模型 id>` 直接发送。解析规则与前文一致；若没有一家已启用的 provider 提供该 id，返回 `404 model_not_found`，加 `UNIFIED_MODEL_FALLBACK_FIRST=1` 才会退回兜底 provider。

对没有实现 responses 的 provider（如 zcode、opencode、codebuddy，它们只有 chat 与 messages）调 `/v1/responses`，返回 `501`：

```json
{"error": {"message": "ZCode 2API 不支持 responses", "type": "invalid_request_error",
           "param": null, "code": "capability_unsupported", "provider": "zcode"}}
```

## 3. `/v1/messages`

Anthropic 形状。`max_tokens` 必填（上游要求，网关原样透传），`anthropic-version` 头可带可不带，网关不校验。

### 3.1 带前缀：`zcode/GLM-5.3`

curl，非流式（Anthropic 原生头 `x-api-key`）：

```bash
curl -sS $U2A_BASE/v1/messages \
  -H "x-api-key: $U2A_KEY" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "zcode/GLM-5.3",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "用一句话说明 HTTP/2 的多路复用"}]
  }'
```

同一请求换成 `Authorization` 也等价：

```bash
curl -sS $U2A_BASE/v1/messages \
  -H "Authorization: Bearer $U2A_KEY" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "zcode/GLM-5.3",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "用一句话说明 HTTP/2 的多路复用"}]
  }'
```

curl，流式（Anthropic 流没有 `[DONE]`，以 `message_stop` 收尾）：

```bash
curl -sS -N $U2A_BASE/v1/messages \
  -H "x-api-key: $U2A_KEY" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "zcode/GLM-5.3",
    "max_tokens": 256,
    "stream": true,
    "messages": [{"role": "user", "content": "写一个 3 行的 Python 快排"}]
  }'
```

也可以把上面任一 `-H "x-api-key: $U2A_KEY"` 换成 `-H "Authorization: Bearer $U2A_KEY"`，两种头都认。

Python（anthropic SDK），非流式。注意 `base_url` 只到主机端口，SDK 自己拼 `/v1/messages`；认证用默认的 `api_key`（SDK 会发 `x-api-key`）：

```python
import os
from anthropic import Anthropic

client = Anthropic(api_key=os.environ["U2A_KEY"], base_url=os.environ["U2A_BASE"])

msg = client.messages.create(
    model="zcode/GLM-5.3",
    max_tokens=256,
    messages=[{"role": "user", "content": "用一句话说明 HTTP/2 的多路复用"}],
)
print(msg.content[0].text)
```

Python（anthropic SDK），流式：

```python
import os
from anthropic import Anthropic

client = Anthropic(api_key=os.environ["U2A_KEY"], base_url=os.environ["U2A_BASE"])

with client.messages.stream(
    model="zcode/GLM-5.3",
    max_tokens=256,
    messages=[{"role": "user", "content": "写一个 3 行的 Python 快排"}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
```

Node（anthropic SDK），非流式：

```js
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic({
  apiKey: process.env.U2A_KEY,
  baseURL: process.env.U2A_BASE,
});

const msg = await client.messages.create({
  model: "zcode/GLM-5.3",
  max_tokens: 256,
  messages: [{ role: "user", content: "用一句话说明 HTTP/2 的多路复用" }],
});
console.log(msg.content[0].text);
```

Node（anthropic SDK），流式：

```js
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic({
  apiKey: process.env.U2A_KEY,
  baseURL: process.env.U2A_BASE,
});

const stream = await client.messages.create({
  model: "zcode/GLM-5.3",
  max_tokens: 256,
  stream: true,
  messages: [{ role: "user", content: "写一个 3 行的 Python 快排" }],
});
for await (const event of stream) {
  if (event.type === "content_block_delta" && event.delta.type === "text_delta") {
    process.stdout.write(event.delta.text);
  }
}
```

### 3.2 裸模型名：`GLM-5.3`

把 `"zcode/GLM-5.3"` 换成 `"GLM-5.3"`，其余不变：

```bash
curl -sS $U2A_BASE/v1/messages \
  -H "Authorization: Bearer $U2A_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "GLM-5.3", "max_tokens": 256,
       "messages": [{"role": "user", "content": "你好"}]}'
```

```python
msg = client.messages.create(
    model="GLM-5.3",
    max_tokens=256,
    messages=[{"role": "user", "content": "你好"}],
)
```

```js
const msg = await client.messages.create({
  model: "GLM-5.3",
  max_tokens: 256,
  messages: [{ role: "user", content: "你好" }],
});
```

## 错误体：两套形状

Chat / Responses 用 OpenAI 形状（`detail` 是**顶层**兼容字段，不在 `error` 里）：

```json
{"error": {"message": "...", "type": "model_not_found", "param": null, "code": "model_not_found"},
 "detail": "..."}
```

Messages 用 Anthropic 形状：

```json
{"type": "error", "error": {"type": "not_found_error", "message": "..."}}
```

常见状态码：

| 状态 | code | 触发条件 |
| --- | --- | --- |
| 400 | `invalid_json` / `missing_model` | 请求体不是 JSON 对象 / 没写 `model` |
| 400 | `ambiguous_model` | 裸名被多家已启用 provider 同时命中 |
| 401 | `invalid_api_key` | 没带 `Authorization` / `x-api-key`，或 key 不对 |
| 401 | `conflicting_auth_headers` | 同时带 `Authorization` 与 `x-api-key` 且值不同 |
| 404 | `model_not_found` | 聚合目录里查不到该模型 |
| 404 | `model_unavailable` | 模型只存在于未启用的 provider |
| 501 | `capability_unsupported` | provider 不支持该协议 |

流式请求中途失败时，网关会在 SSE 里补一帧错误再断开：chat 先发错误帧再发 `[DONE]`，messages / responses 只发 `event: error` 帧。

## 排查提示

- 401 先确认密钥放在 `Authorization: Bearer` 或 `x-api-key` 头里，而不是 query 或 `X-Api-Key`（后者是配额读接口用的）。两个头同时带且值不同会得到 `conflicting_auth_headers`，删掉其中一个即可。
- 404 先跑 `GET /v1/models` 确认 id 拼写与 provider 是否启用，再决定写不写前缀。
- 501 说明模型解析对了、而是 provider 能力不够，换支持该协议的 provider（responses 目前只有 codar、qoder）。
- 客户端报 `baseURL` 重复 `/v1`（404 或 `/v1/v1/...`）：OpenAI SDK 的 `base_url` 要含 `/v1`，Anthropic SDK 的 `base_url` 只到主机端口。
