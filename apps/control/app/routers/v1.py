import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from ..adapters.base import ProviderError
from ..adapters.stream_util import DONE, sse_error
from ..api_utils import mask_text, now, parse_bool
from ..billing import (
    check_failure_rate,
    check_quota,
    extract_usage,
    record_usage,
    resolve_prices,
    settle,
    totals,
    usage_from_stream,
    usage_in_chunk,
)
from ..db import get_json_setting
from ..gateway_errors import GatewayError, from_provider_error, from_upstream_reply
from ..model_resolve import MODE_CATALOG, resolve_model

router = APIRouter(tags=["gateway"])
logger = logging.getLogger("unified.billing")

# last_used_at 只在超过这个间隔才写库，否则每个请求一次 UPDATE 会把 SQLite 写锁放大（S-06）
LAST_USED_THROTTLE_SEC = 60
# 流式响应单请求最大时长，防止上游持续出块长期占用连接（S-06）
DEFAULT_SSE_MAX_SECONDS = 600


def _anonymous_allowed(request: Request, conn) -> bool:
    """匿名开关：DB 配置优先，缺省回落到环境变量（默认关）。"""
    cfg = get_json_setting(conn, "gateway", {})
    if isinstance(cfg, dict) and "allow_anonymous" in cfg:
        try:
            return parse_bool(cfg["allow_anonymous"], "allow_anonymous")
        except HTTPException:
            # 库里存了脏值就当关闭，宁可拒绝也不静默放行
            return False
    return request.app.state.settings.allow_anonymous_gateway


def _bearer_token(header: str) -> str:
    """Authorization 解析沿用旧口径：剥 `Bearer ` 前缀，裸 token 原样返回。"""
    return header[7:].strip() if header.lower().startswith("bearer ") else header


def _gateway_key(request: Request, conn) -> dict[str, Any]:
    # Anthropic 官方 SDK 默认发 x-api-key；Authorization 仍然优先且行为不变
    auth_header = request.headers.get("authorization", "").strip()
    api_key = request.headers.get("x-api-key", "").strip()
    if auth_header and api_key:
        # 两个头都带就必须同值：静默取其一会让调用方无从判断哪把 key 生效
        if not hmac.compare_digest(_bearer_token(auth_header).encode(), api_key.encode()):
            raise GatewayError(
                401,
                "鉴权头冲突：Authorization 与 x-api-key 的值不一致，请只保留一个",
                type_="authentication_error",
                code="conflicting_auth_headers",
            )
    if not auth_header and not api_key:
        if _anonymous_allowed(request, conn):
            return {"id": None, "name": "anonymous", "prefix": None}
        raise GatewayError(
            401,
            "缺少 Authorization: Bearer <key> 或 x-api-key",
            type_="authentication_error",
            code="invalid_api_key",
        )
    token = _bearer_token(auth_header) if auth_header else api_key
    digest = hashlib.sha256(token.encode()).hexdigest()
    row = conn.execute(
        # prefix 随行带出：日志写入时固化，删 key 后 join 不到仍可溯源
        "SELECT id, name, prefix FROM api_keys WHERE key_hash = ? AND enabled = 1", (digest,)
    ).fetchone()
    if row is None:
        raise GatewayError(401, "无效的 API Key", type_="authentication_error", code="invalid_api_key")
    updated = conn.execute(
        "UPDATE api_keys SET last_used_at = ? WHERE id = ? "
        "AND (last_used_at IS NULL OR last_used_at < ?)",
        (now(), row["id"], now() - LAST_USED_THROTTLE_SEC),
    )
    if updated.rowcount:
        conn.commit()
    else:
        # 节流窗口内命中 0 行不提交（省一次纯写事务）；连接是准入段共用的，
        # 必须回滚掉隐式 BEGIN，后面的配额/价格检查不能读进这条空写事务
        conn.rollback()
    return {"id": row["id"], "name": row["name"], "prefix": row["prefix"]}


def _price_snapshot(conn, provider: str, model: str, at: int) -> dict[str, Any] | None:
    """准入时解析一次价格并随请求冻结。

    计费是旁路：价格表读失败只记日志、不回滚请求；settle 侧按"未定价"记 0 元。
    """
    try:
        lines = resolve_prices(conn, provider=provider, model=model, at=at)
    except Exception as exc:  # noqa: BLE001 - 旁路不能阻断网关
        logger.warning("价格解析失败 provider=%s model=%s：%s", provider, model, exc)
        return None
    return {"provider": provider, "model": model, "at": at, "lines": lines}


def _log_rejection(
    conn, *, key: dict[str, Any], provider: str, model: str, denial: dict[str, Any]
) -> None:
    """被拒请求只留 request_logs 痕迹（现有 /api/logs 可见），不写 usage/ledger。"""
    conn.execute(
        "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, completion_tokens, "
        "latency_ms, stream, error, key_id, key_name, key_prefix) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (now(), mask_text(provider), mask_text(model), 429, 0, 0, 0, 0,
         mask_text(json.dumps(denial, ensure_ascii=False)),
         key.get("id"), mask_text(key.get("name") or ""), key.get("prefix")),
    )


def _quota_guard(conn, key: dict[str, Any], provider: str, model: str) -> None:
    """准入校验：超限直接 429，必须在打上游之前完成。

    与计费旁路同一容错口径：配额读库失败只记日志、不阻断网关。
    """
    key_id = key.get("id")
    if key_id is None:  # 匿名请求没有密钥归属，策略只按 u2a_ 密钥生效
        return
    denial: dict[str, Any] | None = None
    try:
        denial = check_quota(conn, key_id=int(key_id), provider=provider, model=model, at=now())
        if denial is not None:
            _log_rejection(conn, key=key, provider=provider, model=model, denial=denial)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - 查询失败不能打挂请求
        logger.warning("配额检查失败 key_id=%s：%s", key_id, exc)
        # 共用连接：残留事务在这里回滚，等价旧实现各开各关时 close 的隐式回滚
        conn.rollback()
        return
    if denial is not None:
        # 结构化 detail：窗口 / 指标 / 当前用量 / 上限 / 重置时间，不是笼统的「配额不足」
        raise GatewayError(
            429,
            str(denial.get("message") or "配额已用尽"),
            type_="insufficient_quota",
            code="quota_exceeded",
            detail=denial,
            retry_after=str(denial["reset_after"]),
        )


def _failure_guard(request: Request, conn, key: dict[str, Any], provider: str, model: str) -> None:
    """失败请求限流：窗口内失败过多就 429，插在配额校验之后、打上游之前。

    只拦失败刷量，不写 usage/ledger；被拒请求本身落 request_logs(status=429)，
    日志与计数同一份数据。读库失败按旁路口径放过请求。
    """
    key_id = key.get("id")
    if key_id is None:  # 匿名请求没有可归属的计数维度
        return
    settings = request.app.state.settings
    window_sec = int(getattr(settings, "failure_window_sec", 60))
    max_failures = int(getattr(settings, "failure_window_max", 20))
    denial: dict[str, Any] | None = None
    try:
        denial = check_failure_rate(
            conn, key_id=int(key_id), window_sec=window_sec, max_failures=max_failures, at=now()
        )
        if denial is not None:
            _log_rejection(conn, key=key, provider=provider, model=model, denial=denial)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - 限流读库失败不能打挂请求
        logger.warning("失败限流检查失败 key_id=%s：%s", key_id, exc)
        # 同 _quota_guard：共用连接要显式清掉残留事务
        conn.rollback()
        return
    if denial is not None:
        raise GatewayError(
            429,
            str(denial.get("message") or "失败请求过多"),
            type_="rate_limit_error",
            code="failure_rate_limited",
            detail=denial,
            retry_after=str(denial["reset_after"]),
        )


def _record(
    request: Request,
    *,
    key: dict[str, Any],
    provider: str,
    model: str,
    status: int,
    usage: dict[str, Any] | None,
    latency_ms: int,
    stream: bool,
    error: str | None,
    pricing: dict[str, Any] | None = None,
) -> None:
    """写 request_logs（汇总列）+ usage_records（原始 usage 直通）+ billing_ledger（金额定格）。"""
    prompt_tokens, completion_tokens = totals(usage)
    ts = now()
    conn = request.app.state.db.connect()
    try:
        log_cur = conn.execute(
            "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, completion_tokens, "
            "latency_ms, stream, error, key_id, key_name, key_prefix) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, mask_text(provider), mask_text(model), status, prompt_tokens, completion_tokens,
             latency_ms, 1 if stream else 0, mask_text(error) if error else error,
             key.get("id"), mask_text(key.get("name") or ""), key.get("prefix")),
        )
        record_usage(
            conn,
            key_id=key.get("id"),
            key_name=key.get("name") or "",
            provider=provider,
            model=model,
            status=status,
            stream=stream,
            usage=usage,
            latency_ms=latency_ms,
            ts=ts,
        )
        # 同一 conn、同一 commit：正常情况下流水与日志一起落
        try:
            settle(
                conn,
                ts=ts,
                key_id=key.get("id"),
                key_name=key.get("name") or "",
                provider=provider,
                model=model,
                status=status,
                usage=usage,
                pricing=pricing,
                failed=bool(error),
                # 日志行 id 即关联键：流水按它精确挂到本请求上，日志接口无需猜关联
                log_id=int(log_cur.lastrowid),
            )
        except Exception:  # noqa: BLE001 - 计费失败不能带走已产生的日志
            logger.exception("计费 settle 失败 provider=%s model=%s，保留日志与原始 usage", provider, model)
        conn.commit()
    finally:
        conn.close()


async def _record_safe(request: Request, **fields: Any) -> None:
    """sqlite 写盘丢线程池，不占事件循环。

    shield 让写盘在客户端断开（响应任务被取消）时也继续跑完，三种路径都不丢记录，
    也不会因为取消再补写而重复落库。
    """
    await asyncio.shield(asyncio.to_thread(_record, request, **fields))


async def _json_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GatewayError(
            400, f"请求体不是合法 JSON：{exc}", type_="invalid_request_error", code="invalid_json"
        ) from exc
    if not isinstance(payload, dict):
        raise GatewayError(400, "请求体必须是 JSON 对象", type_="invalid_request_error", code="invalid_json")
    return payload


def _stream_error_frame(capability: str, err: GatewayError) -> bytes:
    """中途失败补错误帧，形状跟着端点协议走。"""
    if capability == "messages":
        return sse_error(err.anthropic_payload(), event="error")
    if capability == "responses":
        # Responses 的流内错误事件：event: error + {type, code, message, param}
        event = {
            "type": "error",
            "code": err.error["code"],
            "message": err.error["message"],
            "param": None,
            **err.extra_fields(),
        }
        return sse_error(event, event="error")
    frame = sse_error({"error": err.error})
    return frame + DONE


def _stream_failure(provider: str, exc: Exception) -> GatewayError:
    if isinstance(exc, ProviderError):
        return from_provider_error(exc, provider)
    return GatewayError(
        502,
        f"上游流式中断：{mask_text(str(exc))}",
        type_="api_error",
        code="upstream_stream_broken",
        provider=provider,
    )


async def _drain_reply(reply: Any, limit: int = 8192) -> bytes:
    """读掉上游错误体（只留前 8KB 解析），避免连接悬挂。"""
    body = b""
    try:
        async for chunk in reply.iter_bytes():
            body += chunk
            if len(body) >= limit:
                break
    except Exception:  # noqa: BLE001 - 错误体读失败不影响错误映射
        pass
    return body[:limit]


async def _resolve(request: Request, payload: dict[str, Any], capability: str, conn):
    model = str(payload.get("model") or "").strip()
    if not model:
        raise GatewayError(400, "请求体缺少 model", type_="invalid_request_error", code="missing_model")
    try:
        adapter, effective, mode = await resolve_model(
            request.app.state.registry,
            model,
            request.headers.get("x-provider"),
            capability,
            ttl_sec=int(getattr(request.app.state.settings, "models_cache_ttl_sec", 60)),
            fallback_first=bool(getattr(request.app.state.settings, "model_fallback_first", False)),
            # 兜底日志的关联 id：客户端带 x-request-id 就沿用，否则现生成一个短 id
            request_id=(request.headers.get("x-request-id") or "").strip() or uuid.uuid4().hex[:12],
            conn=conn,
        )
    except ProviderError as exc:
        raise from_provider_error(exc) from exc
    if mode == MODE_CATALOG:
        # 自动解析是新增路径：把命中的 provider 显式写进日志，方便和 request_logs 对照排查
        logger.info("自动解析 model=%s → %s/%s", model, adapter.spec.id, effective)
    if not adapter.spec.capabilities.get(capability):
        raise GatewayError(
            501,
            f"{adapter.spec.name} 不支持 {capability}",
            type_="invalid_request_error",
            code="capability_unsupported",
            provider=adapter.spec.id,
        )
    return adapter, effective


async def _proxy(request: Request, capability: str):
    # 准入段（鉴权/解析/配额/失败限流/取价）共用一条连接：这些小表读各开各关时，
    # 建连+首查+关连接约 1.4ms/次、SQL 本身不到 0.05ms（见 PERFORMANCE.md 3.3）。
    # 连接随请求创建、随请求关闭，不跨请求复用；整段在事件循环线程内同步使用。
    conn = request.app.state.db.connect()
    try:
        key = _gateway_key(request, conn)
        payload = await _json_payload(request)
        adapter, effective = await _resolve(request, payload, capability, conn)
        payload["model"] = effective
        # 先校验额度再冻结价格：被拒请求不产生快照与流水，通过后仍按原逻辑冻结价格
        _quota_guard(conn, key, adapter.spec.id, effective)
        _failure_guard(request, conn, key, adapter.spec.id, effective)
        stream = bool(payload.get("stream"))
        # 价格在准入时定格：请求进行中改价（新插入更晚生效的行）不会改变这笔请求的金额
        pricing = _price_snapshot(conn, adapter.spec.id, effective, now())
    finally:
        conn.close()
    started = time.perf_counter()
    try:
        if capability == "chat":
            reply = await adapter.chat_completion(payload, stream=stream)
        elif capability == "responses":
            reply = await adapter.responses(payload, stream=stream)
        else:
            reply = await adapter.messages(payload, stream=stream)
    except ProviderError as exc:
        await _record_safe(
            request, key=key, provider=adapter.spec.id, model=effective,
            status=exc.status_code, usage=None,
            latency_ms=int((time.perf_counter() - started) * 1000),
            stream=stream, error=exc.message, pricing=pricing,
        )
        raise from_provider_error(exc, adapter.spec.id) from exc

    if stream:
        if reply.status_code >= 400:
            # 上游直接拒绝流式请求时不要吐一个"看起来是 SSE"的 JSON 错误体
            raw = await _drain_reply(reply)
            await _record_safe(
                request, key=key, provider=adapter.spec.id, model=effective,
                status=reply.status_code, usage=None,
                latency_ms=int((time.perf_counter() - started) * 1000),
                stream=True, error=raw[:200].decode("utf-8", "ignore"), pricing=pricing,
            )
            raise from_upstream_reply(
                status_code=reply.status_code, body=raw, headers=reply.headers, provider=adapter.spec.id
            )

        async def body_stream():
            error: str | None = None
            usage_chunks: list[bytes] = []
            max_seconds = getattr(request.app.state.settings, "sse_max_seconds", DEFAULT_SSE_MAX_SECONDS)
            deadline = time.monotonic() + max(int(max_seconds), 1)
            source = reply.iter_bytes()
            try:
                async for chunk in source:
                    if time.monotonic() > deadline:
                        error = f"流式响应超过 {max_seconds}s 上限，已中断"
                        yield _stream_error_frame(capability, GatewayError(
                            504, error, type_="api_error", code="stream_timeout",
                            provider=adapter.spec.id,
                        ))
                        break
                    # 先解析 SSE data 再判断有没有 usage，避免正文里出现 usage 字样就误攒
                    if len(usage_chunks) < 32 and usage_in_chunk(chunk):
                        usage_chunks.append(chunk)
                    yield chunk
            except Exception as exc:  # 流中断要留痕，否则日志里看不到
                error = str(exc)
                # 半路断流不能直接截断：按 SSE 约定补错误事件，调用方才知道这句没说完
                yield _stream_error_frame(capability, _stream_failure(adapter.spec.id, exc))
            finally:
                try:
                    await source.aclose()  # 超时 break 时也要显式释放上游连接
                except Exception:
                    pass
                await _record_safe(
                    request, key=key, provider=adapter.spec.id, model=effective,
                    status=reply.status_code, usage=usage_from_stream(usage_chunks),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    stream=True, error=error, pricing=pricing,
                )

        return StreamingResponse(
            body_stream(),
            status_code=reply.status_code,
            media_type=reply.headers.get("content-type", "text/event-stream"),
        )

    body = reply.body or b""
    if reply.status_code >= 400:
        await _record_safe(
            request, key=key, provider=adapter.spec.id, model=effective,
            status=reply.status_code, usage=None,
            latency_ms=int((time.perf_counter() - started) * 1000),
            stream=False,
            error=body[:200].decode("utf-8", "ignore"),
            pricing=pricing,
        )
        raise from_upstream_reply(
            status_code=reply.status_code, body=body, headers=reply.headers, provider=adapter.spec.id
        )

    usage = extract_usage(body)
    await _record_safe(
        request, key=key, provider=adapter.spec.id, model=effective,
        status=reply.status_code, usage=usage,
        latency_ms=int((time.perf_counter() - started) * 1000),
        stream=False, error=None, pricing=pricing,
    )
    return Response(
        content=body,
        status_code=reply.status_code,
        media_type=reply.headers.get("content-type", "application/json"),
    )


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await _proxy(request, "chat")


@router.post("/v1/messages")
async def anthropic_messages(request: Request):
    return await _proxy(request, "messages")


@router.post("/v1/responses")
async def openai_responses(request: Request):
    return await _proxy(request, "responses")


@router.get("/v1/models")
async def openai_models(request: Request):
    conn = request.app.state.db.connect()
    try:
        _gateway_key(request, conn)
    finally:
        conn.close()
    data: list[dict[str, Any]] = []
    for adapter in request.app.state.registry.enabled():
        if not adapter.spec.capabilities.get("models"):
            continue
        for item in await adapter.list_models():
            model_id = str(item.get("id") or "")
            if model_id:
                data.append({"id": model_id, "object": "model", "owned_by": adapter.spec.id})
    return {"object": "list", "data": data}
