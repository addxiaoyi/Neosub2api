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


def _first_system(payload: dict) -> str:
    for m in payload.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "system":
            return str(m.get("content") or "")[:80]
    system = payload.get("system")
    if isinstance(system, str):
        return system[:80]
    return ""


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/status")
def status():
    return {"connected": True, "total": 1, "healthy": 1}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "fake"} for m in MODELS]}


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
    payload = await request.json()
    model = payload.get("model", "?")
    text = f"[fake] model={model} system={_first_system(payload)!r}"
    usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    if payload.get("stream"):
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
    payload = await request.json()
    model = payload.get("model", "?")
    text = f"[fake] model={model} system={_first_system(payload)!r}"
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


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7899
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
