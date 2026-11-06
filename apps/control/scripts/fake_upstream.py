"""本地假上游：模拟 OpenAI/Anthropic 流式响应，用于无真实凭证时验证转发链路。

用法：python fake_upstream.py [port]   （默认 7899）
回声约定：响应 content 里带首条 system 消息文本与 model，便于验证改写/路由是否生效。
"""

import asyncio
import json
import sys
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="fake-upstream")

MODELS = ["fake-chat-1", "fake-chat-2"]

# 最近一次真实外呼：测试用它断言网关发到上游的路径、头与 body，而不是只看响应形状
RECENT: list[dict] = []


def _remember(request: Request, payload=None) -> None:
    body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    RECENT.append({
        "method": request.method,
        "path": request.url.path,
        "headers": {key.lower(): value for key, value in request.headers.items()},
        "body": body.decode("utf-8", "ignore"),
    })
    del RECENT[:-20]


@app.delete("/__fake/last")
def clear_last():
    RECENT.clear()
    return {"ok": True}


@app.get("/__fake/last")
def last_request():
    return RECENT[-1] if RECENT else {}


@app.get("/__fake/recent")
def recent_requests(path: str = ""):
    """按路径过滤最近的真实外呼；换号/多阶段链路要断言多次请求时用它。"""
    return [item for item in RECENT if not path or item["path"] == path]

# codar 假上游的 token 约定：expired 触发 401（续期后换成 fresh），forbidden 触发 403
CODAR_EXPIRED = "codar-expired"
CODAR_FRESH = "codar-fresh-1"
CODAR_FORBIDDEN = "codar-forbidden"


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization", "").strip()
    return raw[7:].strip() if raw.lower().startswith("bearer ") else ""


def _codar_denied(request: Request):
    token = _bearer(request)
    if token == CODAR_EXPIRED:
        return JSONResponse(
            {"error": {"message": "token expired", "type": "unauthorized"}}, status_code=401
        )
    if token == CODAR_FORBIDDEN:
        return JSONResponse(
            {"error": {"message": "account restricted", "code": "account_restricted"}},
            status_code=403,
        )
    return None


def _first_system(payload: dict) -> str:
    for m in payload.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "system":
            return str(m.get("content") or "")[:80]
    system = payload.get("system")
    if isinstance(system, str):
        return system[:80]
    if isinstance(system, list):
        return "".join(str(b.get("text") or "") for b in system if isinstance(b, dict))[:80]
    return ""


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/status")
def status():
    return {"connected": True, "total": 1, "healthy": 1}


@app.get("/v1/models")
def models(request: Request):
    _remember(request)
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "fake"} for m in MODELS]}


@app.get("/v1/codar/models")
def codar_models(request: Request):
    denied = _codar_denied(request)
    if denied is not None:
        return denied
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "codar"} for m in MODELS]}


@app.post("/v1/token/refresh")
async def codar_refresh(request: Request):
    if _bearer(request) == CODAR_EXPIRED:
        return {"data": {"token": CODAR_FRESH}}
    return JSONResponse({"error": "refresh rejected"}, status_code=401)


@app.post("/api/auth/start")
async def auth_start(request: Request):
    """伪 codebuddy 管理端扫码登录：返回二维码链接与 state。"""
    body = await request.json()
    realm = "global" if str(body.get("realm") or "").lower() == "global" else "cn"
    return {"state": "fake-state", "authUrl": "https://example.com/scan?state=fake-state", "realm": realm}


@app.get("/api/auth/poll")
def auth_poll(state: str = "", realm: str = "cn"):
    if not state:
        return {"status": "invalid"}
    return {"status": "success", "uid": "u-fake", "nickname": "Fake", "realm": realm, "updated": False}


def _sse(lines):
    for line in lines:
        yield f"data: {json.dumps(line, ensure_ascii=False)}\n\n".encode()
    yield b"data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat(request: Request):
    denied = _codar_denied(request)
    if denied is not None:
        return denied
    payload = await request.json()
    model = payload.get("model", "?")
    err = _vendor_error(str(model))
    if err is not None:
        return err
    if "slow" in str(model):
        await asyncio.sleep(1.5)
    text = (
        f"[fake] model={model} system={_first_system(payload)!r} "
        f"{_echo(request, 'user-agent', 'anthropic-version', 'x-api-key')}"
    )
    usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    if payload.get("stream"):
        if "stream-break" in str(model):
            async def broken():
                yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
                await asyncio.sleep(0)
                raise RuntimeError("fake upstream aborted mid-stream")

            return StreamingResponse(broken(), media_type="text/event-stream")
        chunks = [
            {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": model,
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
            {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": model,
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
        ]
        return StreamingResponse(_sse(chunks), media_type="text/event-stream")
    return JSONResponse({
        "id": "chatcmpl-fake", "object": "chat.completion", "model": model,
        "created": int(time.time()),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": usage,
    })


@app.post("/v1/messages")
@app.post("/anthropic/v1/messages")  # deepseek 上游的非标准路径，适配器会重写
async def messages(request: Request):
    denied = _codar_denied(request)
    if denied is not None:
        return denied
    payload = await request.json()
    model = payload.get("model", "?")
    err = _vendor_error(str(model))
    if err is not None:
        return err
    text = (
        f"[fake] model={model} system={_first_system(payload)!r} "
        f"{_echo(request, 'user-agent', 'anthropic-version', 'x-api-key')}"
    )
    if payload.get("stream"):
        async def gen():
            events = [
                {"type": "message_start", "message": {"id": "msg_fake", "type": "message", "role": "assistant",
                                                      "model": model, "usage": {"input_tokens": 11, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"input_tokens": 11, "output_tokens": 7}},
                {"type": "message_stop"},
            ]
            for e in events:
                yield f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n".encode()
                await asyncio.sleep(0)
        return StreamingResponse(gen(), media_type="text/event-stream")
    return JSONResponse({
        "id": "msg_fake", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 7},
    })


@app.post("/v1/responses")
async def responses(request: Request):
    denied = _codar_denied(request)
    if denied is not None:
        return denied
    payload = await request.json()
    _remember(request, payload)
    model = payload.get("model", "?")
    err = _vendor_error(str(model))
    if err is not None:
        return err
    text = f"[fake] responses model={model}"
    if payload.get("stream"):
        async def gen():
            events = [
                {"type": "response.created", "response": {"id": "resp_fake", "model": model}},
                {"type": "response.output_text.delta", "delta": text},
                {"type": "response.completed", "response": {"id": "resp_fake", "model": model}},
            ]
            for e in events:
                yield f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n".encode()
                await asyncio.sleep(0)
        return StreamingResponse(gen(), media_type="text/event-stream")
    return JSONResponse({
        "id": "resp_fake", "object": "response", "model": model,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    })


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    payload = await request.json()
    _remember(request, payload)
    size = len(json.dumps(payload, ensure_ascii=False))
    return {"input_tokens": max(1, size // 3)}


# ---------------------------------------------------------------------------
# 仓内自研三家（muse / gptgrok / zcode）的假上游路由
# 约定：响应正文回显关键鉴权头，便于断言；model 里带 err-<code> 时返回该状态码。
# ---------------------------------------------------------------------------

VENDOR_ERROR_CODES = (400, 401, 402, 403, 408, 422, 429, 500, 502, 503)


def _echo(request: Request, *names: str) -> str:
    return " ".join(f"{name}={request.headers.get(name, '')}" for name in names)


def _bad_key(request: Request) -> JSONResponse | None:
    """凭证轮换测试：含 bad-key 的凭证一律 401。"""
    for header in ("authorization", "cookie", "x-api-key"):
        if "bad-key" in request.headers.get(header, ""):
            return JSONResponse({"error": {"message": "bad key", "code": 401}}, status_code=401)
    return None


def _vendor_error(model: str) -> JSONResponse | None:
    if "err-url" in model:
        # 模拟真实厂商错误体里带请求端点，用来验证网关对外脱敏、服务端留底
        return JSONResponse(
            {"error": {"message": "upstream POST http://127.0.0.1:8505/v1/messages failed on port 8505",
                       "code": 403}},
            status_code=403,
        )
    for code in VENDOR_ERROR_CODES:
        if f"err-{code}" in model:
            return JSONResponse({"error": {"message": f"fake vendor error {code}", "code": code}},
                                status_code=code)
    return None


def _openai_stream(text: str, model: str):
    head, tail = text[:12], text[12:]
    return [
        {"id": "chatcmpl-vendor", "object": "chat.completion.chunk", "model": model,
         "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"id": "chatcmpl-vendor", "object": "chat.completion.chunk", "model": model,
         "choices": [{"index": 0, "delta": {"content": head}, "finish_reason": None}]},
        {"id": "chatcmpl-vendor", "object": "chat.completion.chunk", "model": model,
         "choices": [{"index": 0, "delta": {"content": tail}, "finish_reason": None}]},
        {"id": "chatcmpl-vendor", "object": "chat.completion.chunk", "model": model,
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}},
    ]


@app.get("/muse/v1/models")
def muse_models(request: Request):
    denied = _bad_key(request)
    if denied is not None:
        return denied
    return {"object": "list", "data": [
        {"id": m, "object": "model", "owned_by": "muse"} for m in ("muse-spark", "muse-image", "muse-video")
    ]}


@app.post("/muse/v1/chat/completions")
async def muse_chat(request: Request):
    denied = _bad_key(request)
    if denied is not None:
        return denied
    payload = await request.json()
    model = payload.get("model", "?")
    err = _vendor_error(str(model))
    if err is not None:
        return err
    if "slow" in str(model):
        await asyncio.sleep(1.5)  # 给短超时客户端制造 ReadTimeout
    text = f"[fake] muse model={model} system={_first_system(payload)!r} {_echo(request, 'authorization', 'cookie')}"
    if payload.get("stream"):
        if "stream-break" in str(model):
            async def broken():
                yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
                await asyncio.sleep(0)
                raise RuntimeError("fake muse upstream aborted mid-stream")

            return StreamingResponse(broken(), media_type="text/event-stream")
        return StreamingResponse(_sse(_openai_stream(text, str(model))), media_type="text/event-stream")
    return JSONResponse({
        "id": "chatcmpl-vendor", "object": "chat.completion", "model": model,
        "created": int(time.time()),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    })


ZCODE_MESSAGES = "/api/v1/zcode-plan/anthropic/v1/messages"


def _anthropic_body(model: str, text: str) -> dict:
    return {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def _anthropic_events(model: str, text: str):
    events = [
        {"type": "message_start", "message": {"id": "msg_fake", "type": "message", "role": "assistant",
                                              "model": model, "usage": {"input_tokens": 11, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[:12]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[12:]}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"input_tokens": 11, "output_tokens": 7}},
        {"type": "message_stop"},
    ]
    return events


@app.post(ZCODE_MESSAGES)
async def zcode_messages(request: Request):
    denied = _bad_key(request)
    if denied is not None:
        return denied
    payload = await request.json()
    model = str(payload.get("model", "?"))
    if "risk-3012" in model:
        return JSONResponse({"code": 3012, "message": "system fingerprint mismatch"}, status_code=400)
    err = _vendor_error(model)
    if err is not None:
        return err
    text = (f"[fake] zcode model={model} system={_first_system(payload)!r} "
            f"{_echo(request, 'authorization', 'x-api-key', 'x-device-mid', 'anthropic-beta')}")
    if payload.get("stream"):
        async def gen():
            for e in _anthropic_events(model, text):
                yield f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n".encode()
                await asyncio.sleep(0)
        return StreamingResponse(gen(), media_type="text/event-stream")
    return JSONResponse(_anthropic_body(model, text))


@app.get("/api/v1/zcode-plan/billing/balance")
def zcode_balance(request: Request):
    denied = _bad_key(request)
    if denied is not None:
        return denied
    return {"code": 0, "message": "ok", "data": {"balance": 123.4, "currency": "USD"}}


@app.get("/backend-api/me")
def chatgpt_me(request: Request):
    denied = _bad_key(request)
    if denied is not None:
        return denied
    return {"id": "user-fake", "email": "fake@example.com", "plan_type": "plus"}


@app.post("/backend-api/conversation")
async def chatgpt_chat(request: Request):
    denied = _bad_key(request)
    if denied is not None:
        return denied
    payload = await request.json()
    _remember(request, payload)
    model = str(payload.get("model", "?"))
    err = _vendor_error(model)
    if err is not None:
        return err
    parts = ["[fake] chatgpt ", f"model={model} ", _echo(request, "authorization", "oai-device-id")]

    async def gen():
        for index, _ in enumerate(parts):
            status = "finished_successfully" if index == len(parts) - 1 else "in_progress"
            obj = {"message": {
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": ["".join(parts[: index + 1])]},
                "status": status,
            }}
            yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()
            await asyncio.sleep(0)
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/rest/models")
def grok_models(request: Request):
    denied = _bad_key(request)
    if denied is not None:
        return denied
    return {"models": [{"modelId": "grok-4.20-fast"}, {"modelId": "grok-4.3-fast"}]}


@app.post("/rest/app-chat/conversations/new")
async def grok_chat(request: Request):
    denied = _bad_key(request)
    if denied is not None:
        return denied
    payload = await request.json()
    model = str(payload.get("modelName") or payload.get("model") or "?")
    err = _vendor_error(model)
    if err is not None:
        return err
    text = f"[fake] grok model={model} {_echo(request, 'cookie')}"

    async def gen():
        for token, thinking in ((text[:10], False), ("[think]", True), (text[10:], False)):
            response = {"token": token}
            if thinking:
                response["isThinking"] = True
            yield json.dumps({"result": {"response": response}}, ensure_ascii=False).encode() + b"\n"
            await asyncio.sleep(0)
        yield json.dumps({"result": {"response": {"isSoftStop": True}, "finalMetadata": {}}}).encode() + b"\n"

    return StreamingResponse(gen(), media_type="application/json")


# ---------------------------------------------------------------------------
# Trae（Solo / Work）假上游
# 契约取自 app/adapters/trae.py：Solo 单端点 SSE；Work 分「流式」与
# 「create_agent_task → workflow/start → query/sync_history_state」有状态桥。
# 注入约定：authorization 的 Cloud-IDE-JWT 命中固定值时返回对应失败；
# model/config_name 里带 trae-err-<code> 时返回该状态码。
# ---------------------------------------------------------------------------

TRAE_CHAT = "/api/agent/v3/llm_utils_chat"
TRAE_MODELS = "/api/ide/v1/get_detail_param"
TRAE_CREATE_TASK = "/api/agent/v3/create_agent_task"
TRAE_WORKFLOW_START = "/api/agent/v3/workflow/start"
TRAE_QUERY_HISTORY = "/api/agent/v3/query_history_state"
TRAE_SYNC_HISTORY = "/api/agent/v3/sync_history_state"

# 换号用例固定使用的 JWT；真实 Trae 用 Cloud-IDE-JWT 头，这里按同一形状取值
TRAE_JWT_429 = "trae-jwt-429"
TRAE_JWT_PLAN = "trae-jwt-plan"
TRAE_JWT_500 = "trae-jwt-500"
TRAE_JWT_503 = "trae-jwt-503"
TRAE_JWT_OK = "trae-jwt-ok"


def _trae_token(request: Request) -> str:
    raw = request.headers.get("authorization", "").strip()
    if raw.lower().startswith("cloud-ide-jwt "):
        return raw[len("cloud-ide-jwt "):].strip()
    return request.headers.get("x-cloudide-token", "").strip()


def _trae_model(payload: dict) -> str:
    return str(payload.get("model") or payload.get("config_name") or payload.get("model_name") or "?")


def _trae_reject(request: Request, payload: dict) -> JSONResponse | None:
    token = _trae_token(request)
    model = _trae_model(payload)
    if token == TRAE_JWT_429:
        # 不带 Retry-After：适配器应立刻换号，而不是原地睡等
        return JSONResponse({"code": 4011, "message": "soft rate limited"}, status_code=429)
    if token == TRAE_JWT_PLAN or "trae-plan" in model:
        return JSONResponse({"code": 1005, "message": "plan credits exhausted"}, status_code=402)
    if token == TRAE_JWT_500 or "trae-err-500" in model:
        return JSONResponse({"error": {"message": "fake trae boom", "code": 500}}, status_code=500)
    if token == TRAE_JWT_503 or "trae-err-503" in model:
        return JSONResponse({"error": {"message": "fake trae unavailable", "code": 503}}, status_code=503)
    if "trae-err-401" in model:
        return JSONResponse({"code": 401, "message": "session expired"}, status_code=401)
    if "trae-err-url" in model:
        # 模拟真实上游把请求端点写进错误体：用于验证网关对外脱敏
        host = request.headers.get("host", "127.0.0.1")
        port = host.rsplit(":", 1)[-1]
        return JSONResponse(
            {"error": {"message": f"upstream POST http://{host}{TRAE_CHAT} failed on port {port}",
                       "code": 403}},
            status_code=403,
        )
    return None


def _trae_sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _trae_echo(request: Request) -> str:
    return _echo(request, "authorization", "x-cloudide-token", "x-uid", "x-app-id", "x-ide-version")


@app.post(TRAE_CHAT)
async def trae_chat(request: Request):
    payload = await request.json()
    _remember(request, payload)
    denied = _trae_reject(request, payload)
    if denied is not None:
        return denied
    model = _trae_model(payload)
    if "trae-sse-plan" in model:
        async def denied_stream():
            yield _trae_sse("error", {"code": 1005, "message": "no credits"})

        return StreamingResponse(denied_stream(), media_type="text/event-stream")
    text = f"[fake] trae model={model} system={_first_system(payload)!r} {_trae_echo(request)}"
    parts = [text[:12], text[12:24], text[24:]]

    async def gen():
        for index, piece in enumerate(parts):
            if not piece:
                continue
            body = {"response": piece}
            if index == 0:
                body["reasoning_content"] = "think"
            yield _trae_sse("output", body)
            await asyncio.sleep(0)
        yield _trae_sse("token_usage", {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
        yield _trae_sse("done", {"finish_reason": "stop"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post(TRAE_MODELS)
async def trae_models(request: Request):
    payload = await request.json()
    _remember(request, payload)
    return {"models": [
        {"id": "glm-5.2"},
        "glm-5.1",
        {"config_name": "DeepSeek-V4-Flash-Official"},
    ]}


@app.post(TRAE_CREATE_TASK)
async def trae_create_task(request: Request):
    payload = await request.json()
    _remember(request, payload)
    denied = _trae_reject(request, payload)
    if denied is not None:
        return denied
    model = _trae_model(payload)
    accept = request.headers.get("accept", "")
    if "text/event-stream" not in accept:
        # 有状态桥：先拿会话/任务 id，再走 workflow/start 与轮询端点
        return {"data": {
            "conversation_id": payload.get("conversation_id") or "fake-conv",
            "session_id": payload.get("session_id") or "fake-sess",
            "task_id": "fake-task",
        }}
    if "work-plan" in model:
        async def credit_limited():
            yield b'data: {"code":1005,"message":"credits not enough"}\n\n'
            await asyncio.sleep(0)

        return StreamingResponse(credit_limited(), media_type="text/event-stream")
    text = f"[fake] trae work model={model} {_trae_echo(request)}"
    parts = [text[:10], text[10:]]

    async def gen():
        yield _trae_sse("plan_item", {"thought": ""})
        for piece in parts:
            if piece:
                yield _trae_sse("output", {"choices": [{"text": piece}]})
                await asyncio.sleep(0)
        yield _trae_sse("token_usage", {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13})
        yield _trae_sse("done", {"last_assistant_response": json.dumps([text], ensure_ascii=False)})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post(TRAE_WORKFLOW_START)
async def trae_workflow_start(request: Request):
    payload = await request.json()
    _remember(request, payload)
    return {"ok": True, "task_id": payload.get("task_id") or "fake-task"}


@app.post(TRAE_QUERY_HISTORY)
@app.post(TRAE_SYNC_HISTORY)
async def trae_history_state(request: Request):
    payload = await request.json()
    _remember(request, payload)
    return {
        "status": "done",
        "last_assistant_response": json.dumps(["[fake] trae work stateful"], ensure_ascii=False),
        "token_usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
    }


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7899
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
