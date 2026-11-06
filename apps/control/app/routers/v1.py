import asyncio
import hashlib
import hmac
import json
import logging
import posixpath
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from ..adapters.base import ProviderAdapter, ProviderError
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
from ..config import GATEWAY_MAX_BODY_BYTES_DEFAULT
from ..db import get_json_setting
from ..gateway_errors import GatewayError, from_provider_error, from_upstream_reply, public_status
from ..model_resolve import (
    MODE_CATALOG,
    failover_enabled,
    failover_max_attempts,
    resolve_targets,
    retryable_status,
)
from ..usage_estimate import StreamText, estimate_from_body, estimate_from_text

router = APIRouter(tags=["gateway"])
logger = logging.getLogger("unified.billing")

# last_used_at 只在超过这个间隔才写库，否则每个请求一次 UPDATE 会把 SQLite 写锁放大（S-06）
LAST_USED_THROTTLE_SEC = 60
# 流式响应单请求最大时长，防止上游持续出块长期占用连接（S-06）
DEFAULT_SSE_MAX_SECONDS = 600
# 本批新增的能力端点：请求一律非流式（客户端误传 stream 不改传输方式，也不透传上游）；
# 响应也没有可计费的 token 用量（images/videos 按张按秒、count_tokens/rerank 只有非标准 usage；
# search 虽走会话通道，但响应体只回 answer/sources，同样不带 usage），
# 全零 usage 一律按 usage_missing 落账，不许静默记成 billed(0)
EXTENSION_CAPABILITIES = frozenset({"embeddings", "images", "videos", "media", "count_tokens", "rerank", "search"})
# 这三类端点的响应正文能按文本口径估算；其他端点没有可提取的正文，一律维持原状
ESTIMATABLE_CAPABILITIES = frozenset({"chat", "responses", "messages"})


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


def _gateway_key(request: Request, conn, *, allow_anonymous: bool = False) -> dict[str, Any]:
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
        # allow_anonymous 只给签名媒体这类上游本就免鉴权的路径用，不改变全局匿名开关
        if allow_anonymous or _anonymous_allowed(request, conn):
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
            conn,
            key_id=int(key_id),
            provider=provider,
            window_sec=window_sec,
            max_failures=max_failures,
            at=now(),
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
    estimated_usage: dict[str, Any] | None = None,
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
                estimated_usage=estimated_usage,
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


def _usage_for_ledger(capability: str, usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """新增端点的空/全零 usage 一律按 usage_missing 落账，避免按 0 元 billed 混进账。"""
    if capability in EXTENSION_CAPABILITIES and not any(totals(usage)):
        return None
    return usage


def _estimate_on(request: Request, capability: str) -> bool:
    """估算计费默认关：关时调用链完全不变，连正文提取都不会发生。"""
    return (
        capability in ESTIMATABLE_CAPABILITIES
        and bool(getattr(request.app.state.settings, "estimate_usage", False))
    )


def _try_estimate(kind: str, payload: Any, source: Any) -> dict[str, Any] | None:
    """估算只是兜底：任何异常都退回 None，保持 usage_missing，不能影响响应与计费事务。"""
    try:
        return estimate_from_body(payload, source) if kind == "body" else estimate_from_text(payload, source)
    except Exception:  # noqa: BLE001
        logger.exception("usage 估算失败，本次保持 usage_missing")
        return None


def _body_limit(request: Request) -> int:
    return int(
        getattr(request.app.state.settings, "gateway_max_body_bytes", GATEWAY_MAX_BODY_BYTES_DEFAULT)
    )


def _body_too_large(limit: int) -> GatewayError:
    return GatewayError(
        413,
        f"请求体超过网关上限 {limit} 字节（UNIFIED_GATEWAY_MAX_BODY_BYTES），已拒绝且未向上游转发",
        type_="invalid_request_error",
        code="request_too_large",
    )


async def _read_body_limited(request: Request, limit: int) -> bytes:
    """按字节上限读请求体：Content-Length 先拦，再按流累计，超限立刻中止。

    只在这里读一次请求体；超限时直接抛 413，不把剩余字节读进内存。
    """
    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > limit:
        raise _body_too_large(limit)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _body_too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


async def _json_payload(request: Request) -> dict[str, Any]:
    media_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if media_type.startswith("multipart/"):
        # images/edits 的 multipart 变体需客户端层扩展才能透传（muse2api-protocols.md §5）
        raise GatewayError(
            415, "本网关只透传 application/json 请求体，multipart 变体暂不支持，请改用 JSON",
            type_="invalid_request_error", code="unsupported_media_type",
        )
    raw = await _read_body_limited(request, _body_limit(request))
    try:
        payload = json.loads(raw)
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


async def _resolve(
    request: Request, payload: dict[str, Any], capability: str, conn, request_id: str
):
    model = str(payload.get("model") or "").strip()
    if not model:
        raise GatewayError(400, "请求体缺少 model", type_="invalid_request_error", code="missing_model")
    try:
        targets = await resolve_targets(
            request.app.state.registry,
            model,
            request.headers.get("x-provider"),
            capability,
            ttl_sec=int(getattr(request.app.state.settings, "models_cache_ttl_sec", 60)),
            fallback_first=bool(getattr(request.app.state.settings, "model_fallback_first", False)),
            request_id=request_id,
            conn=conn,
        )
    except ProviderError as exc:
        raise from_provider_error(exc) from exc
    adapter, effective, mode = targets[0]
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
    return targets


def _attempt_text(index: int, total: int, detail: str, on: bool) -> str:
    """失败尝试的落库文本：开关打开且确有备选时带「第 N 次尝试」。"""
    if not on or total < 2:
        return detail
    return f"[尝试{index + 1}/{total}] {detail}"


def _transferred(on: bool, index: int, total: int, status: int) -> bool:
    return on and index + 1 < total and retryable_status(status)


def _log_transfer(request_id: str, from_pid: str, to_pid: str, status: int, detail: str) -> None:
    logger.warning(
        "failover request_id=%s %s → %s，原因：%s",
        mask_text(request_id), mask_text(from_pid), mask_text(to_pid),
        mask_text(f"{status} {detail}"[:200]),
    )


async def _proxy(request: Request, capability: str, method: str | None = None, *, default_model: str = ""):
    # 准入段（鉴权/解析/配额/失败限流/取价）共用一条连接：这些小表读各开各关时，
    # 建连+首查+关连接约 1.4ms/次、SQL 本身不到 0.05ms（见 PERFORMANCE.md 3.3）。
    # 连接随请求创建、随请求关闭，不跨请求复用；整段在事件循环线程内同步使用。
    request_id = (request.headers.get("x-request-id") or "").strip() or uuid.uuid4().hex[:12]
    conn = request.app.state.db.connect()
    try:
        key = _gateway_key(request, conn)
        payload = await _json_payload(request)
        # 上游 /v1/search 允许省略 model 并用它的默认值；其余端点仍在 _resolve 里按缺 model 拒绝
        if default_model and not str(payload.get("model") or "").strip():
            payload["model"] = default_model
        targets = await _resolve(request, payload, capability, conn, request_id)
        primary_adapter, primary_effective, _ = targets[0]
        payload["model"] = primary_effective
        # 先校验额度再冻结价格：被拒请求不产生快照与流水，通过后仍按原逻辑冻结价格
        _quota_guard(conn, key, primary_adapter.spec.id, primary_effective)
        _failure_guard(request, conn, key, primary_adapter.spec.id, primary_effective)
        if capability in EXTENSION_CAPABILITIES:
            payload.pop("stream", None)
            stream = False
        else:
            stream = bool(payload.get("stream"))
        # 价格在准入时定格：请求进行中改价（新插入更晚生效的行）不会改变这笔请求的金额
        admission_at = now()
        pricings = [
            _price_snapshot(conn, adapter.spec.id, effective, admission_at)
            for adapter, effective, _mode in targets
        ]
    finally:
        conn.close()

    on = failover_enabled()
    attempt_targets = targets[: failover_max_attempts()] if on else targets[:1]
    pricings = pricings[: len(attempt_targets)]
    started = time.perf_counter()
    reply = None
    final_adapter, final_effective, _ = attempt_targets[0]
    final_pricing = pricings[0]

    for index, (adapter, effective, _mode) in enumerate(attempt_targets):
        pricing = pricings[index]
        payload["model"] = effective
        try:
            if method is not None:
                reply = await getattr(adapter, method)(payload, stream=stream)
            elif capability == "chat":
                reply = await adapter.chat_completion(payload, stream=stream)
            elif capability == "responses":
                reply = await adapter.responses(payload, stream=stream)
            else:
                reply = await adapter.messages(payload, stream=stream)
        except ProviderError as exc:
            await _record_safe(
                request, key=key, provider=adapter.spec.id, model=effective,
                status=public_status(exc.status_code), usage=None,
                latency_ms=int((time.perf_counter() - started) * 1000),
                stream=stream,
                error=_attempt_text(index, len(attempt_targets), exc.message, on),
                pricing=pricing,
            )
            if _transferred(on, index, len(attempt_targets), exc.status_code):
                _log_transfer(
                    request_id, adapter.spec.id, attempt_targets[index + 1][0].spec.id,
                    exc.status_code, exc.message,
                )
                continue
            raise from_provider_error(exc, adapter.spec.id) from exc

        final_adapter, final_effective, final_pricing = adapter, effective, pricing
        if reply.status_code < 400:
            break

        if stream:
            # 上游直接拒绝流式请求时不要吐一个"看起来是 SSE"的 JSON 错误体
            raw = await _drain_reply(reply)
            detail = raw[:200].decode("utf-8", "ignore")
        else:
            raw = reply.body or b""
            # 错误片段截断后再过一次掩码：凭据只允许以掩码形态落库
            detail = mask_text(raw[:200].decode("utf-8", "ignore"))
        await _record_safe(
            request, key=key, provider=adapter.spec.id, model=effective,
            status=public_status(reply.status_code), usage=None,
            latency_ms=int((time.perf_counter() - started) * 1000),
            stream=stream, error=_attempt_text(index, len(attempt_targets), detail, on),
            pricing=pricing,
        )
        if _transferred(on, index, len(attempt_targets), reply.status_code):
            _log_transfer(
                request_id, adapter.spec.id, attempt_targets[index + 1][0].spec.id,
                reply.status_code, detail,
            )
            continue
        raise from_upstream_reply(
            status_code=reply.status_code, body=raw, headers=reply.headers, provider=adapter.spec.id
        )

    # 走到这里只剩成功目标：StreamingResponse 在整条候选链跑完后才构造，
    # 因此「已经开始出 SSE 帧」之后不可能再换 provider——首帧后的断流沿用既有补偿逻辑。
    estimate_on = _estimate_on(request, capability)
    if stream:
        async def body_stream():
            error: str | None = None
            finished = False
            usage_chunks: list[bytes] = []
            # 只有开关打开且拿不到 usage 时才需要攒正文；关闭时连实例都不创建
            text_acc = StreamText() if estimate_on else None
            max_seconds = getattr(request.app.state.settings, "sse_max_seconds", DEFAULT_SSE_MAX_SECONDS)
            deadline = time.monotonic() + max(int(max_seconds), 1)
            source = reply.iter_bytes()
            try:
                async for chunk in source:
                    if time.monotonic() > deadline:
                        error = f"流式响应超过 {max_seconds}s 上限，已中断"
                        yield _stream_error_frame(capability, GatewayError(
                            504, error, type_="api_error", code="stream_timeout",
                            provider=final_adapter.spec.id,
                        ))
                        break
                    # 先解析 SSE data 再判断有没有 usage，避免正文里出现 usage 字样就误攒
                    if len(usage_chunks) < 32 and usage_in_chunk(chunk):
                        usage_chunks.append(chunk)
                    if text_acc is not None:
                        text_acc.feed(chunk)
                    yield chunk
                finished = True
            except Exception as exc:  # 流中断要留痕，否则日志里看不到
                error = str(exc)
                # 半路断流不能直接截断：按 SSE 约定补错误事件，调用方才知道这句没说完
                yield _stream_error_frame(capability, _stream_failure(final_adapter.spec.id, exc))
            finally:
                try:
                    await source.aclose()  # 超时 break 时也要显式释放上游连接
                except Exception:
                    pass
                raw_usage = usage_from_stream(usage_chunks)
                # 断流/被客户端提前取消时 finished=False：不拿半截正文估算
                estimated = (
                    _try_estimate("text", payload, text_acc.output())
                    if (text_acc is not None and not raw_usage and finished and error is None)
                    else None
                )
                await _record_safe(
                    request, key=key, provider=final_adapter.spec.id, model=final_effective,
                    status=reply.status_code, usage=raw_usage,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    stream=True, error=error, pricing=final_pricing,
                    estimated_usage=estimated,
                )

        return StreamingResponse(
            body_stream(),
            status_code=reply.status_code,
            media_type=reply.headers.get("content-type", "text/event-stream"),
            headers={"x-upstream-provider": final_adapter.spec.id},
        )

    body = reply.body or b""
    raw_usage = extract_usage(body)
    usage = _usage_for_ledger(capability, raw_usage)
    # 上游只要给了 usage（含全 0）就优先，估算不介入；没有才按正文估算
    estimated = (
        _try_estimate("body", payload, body)
        if (estimate_on and not raw_usage)
        else None
    )
    await _record_safe(
        request, key=key, provider=final_adapter.spec.id, model=final_effective,
        status=reply.status_code, usage=usage,
        latency_ms=int((time.perf_counter() - started) * 1000),
        stream=False, error=None, pricing=final_pricing,
        estimated_usage=estimated,
    )
    return Response(
        content=body,
        status_code=reply.status_code,
        media_type=reply.headers.get("content-type", "application/json"),
        headers={"x-upstream-provider": final_adapter.spec.id},
    )


def _pick_capable(request: Request, capability: str, conn):
    """无请求体的 GET 端点没有 model 可解析：按能力位挑启用的 provider。"""
    registry = request.app.state.registry
    enabled = registry.enabled(conn=conn)
    capable = [adapter for adapter in enabled if adapter.spec.capabilities.get(capability)]
    pid = (request.headers.get("x-provider") or "").strip()
    if pid:
        if not any(adapter.spec.id == pid for adapter in enabled):
            raise GatewayError(
                404, f"未知或已停用的 provider：{mask_text(pid)}",
                type_="invalid_request_error", code="provider_not_found",
            )
        if not any(adapter.spec.id == pid for adapter in capable):
            known = next(adapter for adapter in enabled if adapter.spec.id == pid)
            raise GatewayError(
                501, f"{known.spec.name} 不支持 {capability}",
                type_="invalid_request_error", code="capability_unsupported", provider=pid,
            )
        return next(adapter for adapter in capable if adapter.spec.id == pid)
    if len(capable) == 1:
        return capable[0]
    if not capable:
        raise GatewayError(
            501, f"没有已启用的 provider 支持 {capability}",
            type_="invalid_request_error", code="capability_unsupported",
        )
    names = "、".join(adapter.spec.id for adapter in capable)
    raise GatewayError(
        400, f"多个 provider 支持 {capability}（{names}），请用 x-provider 指定",
        type_="invalid_request_error", code="ambiguous_provider",
    )


_CLIENT_COMPAT_REPLY_HEADERS = ("location", "etag", "vary", "cache-control", "retry-after")
# 逐跳头与网关自己的鉴权/选路头不转发；转发到上游的凭据固定使用账号池里的 key
_CLIENT_COMPAT_SKIP_HEADERS = frozenset(
    {
        "host", "content-length", "connection", "transfer-encoding",
        "accept-encoding", "authorization", "x-api-key", "cookie", "x-provider",
    }
)


def _client_compat_provider(request: Request, conn):
    """兼容面只认真正实现 compat_forward 的 provider，其余显式 501。"""
    enabled = request.app.state.registry.enabled(conn=conn)
    capable = [adapter for adapter in enabled if getattr(type(adapter), "compat_forward", None) is not None]
    pid = (request.headers.get("x-provider") or "").strip()
    if pid:
        known = next((adapter for adapter in enabled if adapter.spec.id == pid), None)
        if known is None:
            raise GatewayError(
                404, f"未知或已停用的 provider：{mask_text(pid)}",
                type_="invalid_request_error", code="provider_not_found",
            )
        if known not in capable:
            raise GatewayError(
                501, f"{known.spec.name} 不支持客户端兼容端点",
                type_="invalid_request_error", code="capability_unsupported", provider=pid,
            )
        return known
    if len(capable) == 1:
        return capable[0]
    if not capable:
        raise GatewayError(
            501, "没有已启用的 provider 支持客户端兼容端点",
            type_="invalid_request_error", code="capability_unsupported",
        )
    names = "、".join(adapter.spec.id for adapter in capable)
    raise GatewayError(
        400, f"多个 provider 支持客户端兼容端点（{names}），请用 x-provider 指定",
        type_="invalid_request_error", code="ambiguous_provider",
    )


def _client_compat_headers(request: Request) -> dict[str, str]:
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in _CLIENT_COMPAT_SKIP_HEADERS
    }


def _client_compat_reply_headers(reply: Any) -> dict[str, str]:
    headers = reply.headers or {}
    return {name: headers[name] for name in _CLIENT_COMPAT_REPLY_HEADERS if headers.get(name)}


def _sora_signed_filepath(filepath: str) -> str:
    """签名媒体只允许 image/ 与 video/ 下的本地相对路径。

    上游 proxySoraMedia 同样是 path.Clean 后只认这两个前缀（gateway handler:681-691）；
    这里先拦掉 `..` 与其它前缀，避免本仓被当成通用代理打到上游任意接口。
    """
    segments = [part for part in filepath.split("/") if part]
    if not segments or ".." in segments or "." in segments:
        raise GatewayError(
            404, "Sora 签名媒体路径非法", type_="invalid_request_error", code="not_found",
        )
    cleaned = posixpath.normpath("/" + filepath)
    if not (cleaned.startswith("/image/") or cleaned.startswith("/video/")):
        raise GatewayError(
            404, "Sora 签名媒体只支持 image/ 与 video/ 子目录",
            type_="invalid_request_error", code="not_found",
        )
    return cleaned.lstrip("/")


async def _client_compat(
    request: Request,
    method: str,
    upstream_path: str,
    *,
    allow_anonymous: bool = False,
    authenticated: bool = True,
) -> Response:
    """sub2api 专用客户端兼容面：路径、查询串、请求体、状态码、二进制与流全部原样透传。

    不做模型解析（那会改写语义），但鉴权/配额/失败限流/记账沿用 /v1 同一套。
    """
    path = f"{upstream_path}?{request.url.query}" if request.url.query else upstream_path
    conn = request.app.state.db.connect()
    try:
        key = _gateway_key(request, conn, allow_anonymous=allow_anonymous)
        adapter = _client_compat_provider(request, conn)
        raw = await request.body()
        model = ""
        stream = False
        if raw:
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict):
                model = str(payload.get("model") or "")
                stream = bool(payload.get("stream"))
        _quota_guard(conn, key, adapter.spec.id, model)
        _failure_guard(request, conn, key, adapter.spec.id, model)
    finally:
        conn.close()

    started = time.perf_counter()
    try:
        reply = await adapter.compat_forward(
            method, path, raw=raw, headers=_client_compat_headers(request), stream=stream,
            authenticated=authenticated,
        )
    except ProviderError as exc:
        await _record_safe(
            request, key=key, provider=adapter.spec.id, model=model,
            status=public_status(exc.status_code),
            usage=None, latency_ms=int((time.perf_counter() - started) * 1000),
            stream=stream, error=exc.message, pricing=None,
        )
        raise from_provider_error(exc, adapter.spec.id) from exc

    provider_headers = {"x-upstream-provider": adapter.spec.id, **_client_compat_reply_headers(reply)}
    if stream and reply.status_code < 400:
        async def body_stream():
            usage_chunks: list[bytes] = []
            try:
                async for chunk in reply.iter_bytes():
                    if len(usage_chunks) < 32 and usage_in_chunk(chunk):
                        usage_chunks.append(chunk)
                    yield chunk
            finally:
                usage = usage_from_stream(usage_chunks)
                await _record_safe(
                    request, key=key, provider=adapter.spec.id, model=model,
                    status=reply.status_code, usage=usage if any(totals(usage)) else None,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    stream=True, error=None, pricing=None,
                )

        return StreamingResponse(
            body_stream(),
            status_code=reply.status_code,
            media_type=reply.headers.get("content-type", "text/event-stream"),
            headers=provider_headers,
        )

    # 流式请求也可能在首帧前被上游拒绝：此时必须读掉错误体，按原状态码与 content-type 直出
    body = await _drain_reply(reply) if stream else (reply.body or b"")
    usage = extract_usage(body)
    await _record_safe(
        request, key=key, provider=adapter.spec.id, model=model, status=reply.status_code,
        usage=usage if any(totals(usage)) else None,
        latency_ms=int((time.perf_counter() - started) * 1000),
        stream=stream, error=None, pricing=None,
    )
    no_body = reply.status_code in (204, 304)
    return Response(
        content=body,
        status_code=reply.status_code,
        media_type=None if no_body else reply.headers.get("content-type", "application/json"),
        headers=provider_headers,
    )


async def _proxy_get(request: Request, capability: str, method: str, *path_args: str) -> Response:
    """GET 端点（任务轮询、媒体取回）：鉴权/配额/失败限流/记账与 POST 同一套。"""
    conn = request.app.state.db.connect()
    try:
        key = _gateway_key(request, conn)
        adapter = _pick_capable(request, capability, conn)
        # 家级能力位为真但未实现读取方法（如 sub2api 只有建视频任务没有查询）：
        # 明确 501，别让 getattr 炸成 500
        if getattr(adapter, method, None) is None:
            raise GatewayError(
                501, f"{adapter.spec.name} 不支持 {capability} 的读取端点 {method}",
                type_="invalid_request_error", code="capability_unsupported",
                provider=adapter.spec.id,
            )
        # 无 model 的读取端点：配额与失败限流按 provider 维度判（model 级策略不适用）
        _quota_guard(conn, key, adapter.spec.id, "")
        _failure_guard(request, conn, key, adapter.spec.id, "")
    finally:
        conn.close()
    started = time.perf_counter()
    try:
        reply = await getattr(adapter, method)(*path_args)
    except ProviderError as exc:
        await _record_safe(
            request, key=key, provider=adapter.spec.id, model="",
            status=exc.status_code, usage=None,
            latency_ms=int((time.perf_counter() - started) * 1000),
            stream=False, error=exc.message, pricing=None,
        )
        raise from_provider_error(exc, adapter.spec.id) from exc
    body = reply.body or b""
    latency_ms = int((time.perf_counter() - started) * 1000)
    if reply.status_code >= 400:
        await _record_safe(
            request, key=key, provider=adapter.spec.id, model="",
            status=reply.status_code, usage=None, latency_ms=latency_ms,
            stream=False,
            error=mask_text(body[:200].decode("utf-8", "ignore")),
            pricing=None,
        )
        raise from_upstream_reply(
            status_code=reply.status_code, body=body, headers=reply.headers, provider=adapter.spec.id
        )
    await _record_safe(
        request, key=key, provider=adapter.spec.id, model="",
        status=reply.status_code, usage=None, latency_ms=latency_ms,
        stream=False, error=None, pricing=None,
    )
    return Response(
        content=body,
        status_code=reply.status_code,
        media_type=reply.headers.get("content-type", "application/octet-stream"),
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


def _model_object(item: dict[str, Any], adapter: ProviderAdapter) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "object": "model",
        "owned_by": str(item.get("owned_by") or adapter.spec.id),
    }


@router.get("/v1/models/{model_id}")
async def openai_model(request: Request, model_id: str):
    model_id = model_id.strip()
    conn = request.app.state.db.connect()
    try:
        _gateway_key(request, conn)
    finally:
        conn.close()
    providers = [
        adapter
        for adapter in request.app.state.registry.enabled()
        if adapter.spec.capabilities.get("models")
    ]
    pid = (request.headers.get("x-provider") or "").strip()
    if pid:
        providers = [adapter for adapter in providers if adapter.spec.id == pid]
        if not providers:
            raise GatewayError(
                404,
                f"provider 未启用或不支持模型目录：{mask_text(pid)}",
                type_="invalid_request_error",
                code="provider_not_found",
            )
    for adapter in providers:
        hit = next((m for m in adapter.declared_models() if str(m.get("id") or "") == model_id), None)
        if hit is not None:
            return _model_object(hit, adapter)
    # 未显式指定 provider 且有多家模型源时只查静态表：一次明细请求不该把全部上游都打一遍
    if pid or len(providers) == 1:
        for adapter in providers:
            if type(adapter).fetch_models is ProviderAdapter.fetch_models:
                continue
            try:
                items = await adapter.fetch_models()
            except ProviderError:
                continue
            hit = next((m for m in items if str(m.get("id") or "") == model_id), None)
            if hit is not None:
                return _model_object(hit, adapter)
    raise GatewayError(
        404,
        f"模型不存在：{mask_text(model_id)}",
        type_="invalid_request_error",
        code="model_not_found",
    )


@router.post("/v1/search")
async def openai_search(request: Request):
    return await _proxy(request, "search", "search", default_model="gpt-4o-search-preview")


@router.post("/v1/embeddings")
async def openai_embeddings(request: Request):
    return await _proxy(request, "embeddings", "embeddings")


@router.post("/v1/images/generations")
async def openai_image_generations(request: Request):
    return await _proxy(request, "images", "image_generation")


@router.post("/v1/images/edits")
async def openai_image_edits(request: Request):
    return await _proxy(request, "images", "image_edit")


@router.get("/v1/images/tasks/{task_id}")
async def openai_image_task(request: Request, task_id: str):
    return await _proxy_get(request, "images", "image_task", task_id)


@router.post("/v1/videos")
async def openai_videos(request: Request):
    return await _proxy(request, "videos", "video_create")


@router.get("/v1/videos/{task_id}")
async def openai_video_task(request: Request, task_id: str):
    return await _proxy_get(request, "videos", "video_task", task_id)


@router.get("/v1/media/{name}")
async def openai_media(request: Request, name: str):
    return await _proxy_get(request, "media", "media", name)


@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(request: Request):
    return await _proxy(request, "count_tokens", "count_tokens")


@router.post("/v1/rerank")
async def openai_rerank(request: Request):
    return await _proxy(request, "rerank", "rerank")


@router.get("/v1/usage")
async def openai_usage(request: Request):
    # 日期窗口（start_date/end_date 等）原样透传给上游聚合统计
    return await _proxy_get(request, "usage", "usage", request.url.query)


# ---------- sub2api 专用客户端兼容面（Claude Code / Antigravity / Sora） ----------
# 对外路径与上游 gateway.go:158-163、:166/:169-181、:208-219 完全一致：
# 官方客户端把 base_url 指向本仓后按上游约定拼路径即可，无需额外前缀。


@router.get("/api/claude_cli/bootstrap")
async def claude_cli_bootstrap(request: Request):
    return await _client_compat(request, "GET", "/api/claude_cli/bootstrap")


@router.get("/api/claude_code/organizations/metrics_enabled")
async def claude_code_metrics_enabled(request: Request):
    return await _client_compat(request, "GET", "/api/claude_code/organizations/metrics_enabled")


@router.get("/api/claude_code/settings")
async def claude_code_settings(request: Request):
    return await _client_compat(request, "GET", "/api/claude_code/settings")


@router.get("/api/claude_code/policy_limits")
async def claude_code_policy_limits(request: Request):
    return await _client_compat(request, "GET", "/api/claude_code/policy_limits")


@router.get("/api/claude_code/user_settings")
async def claude_code_user_settings(request: Request):
    return await _client_compat(request, "GET", "/api/claude_code/user_settings")


@router.put("/api/claude_code/user_settings")
async def claude_code_update_user_settings(request: Request):
    return await _client_compat(request, "PUT", "/api/claude_code/user_settings")


@router.get("/antigravity/models")
async def antigravity_models(request: Request):
    return await _client_compat(request, "GET", "/antigravity/models")


@router.post("/antigravity/v1/messages")
async def antigravity_messages(request: Request):
    return await _client_compat(request, "POST", "/antigravity/v1/messages")


@router.post("/antigravity/v1/messages/count_tokens")
async def antigravity_count_tokens(request: Request):
    return await _client_compat(request, "POST", "/antigravity/v1/messages/count_tokens")


@router.get("/antigravity/v1/usage")
async def antigravity_usage(request: Request):
    return await _client_compat(request, "GET", "/antigravity/v1/usage")


@router.post("/sora/v1/chat/completions")
async def sora_chat_completions(request: Request):
    return await _client_compat(request, "POST", "/sora/v1/chat/completions")


@router.get("/sora/media/{filepath:path}")
async def sora_media(request: Request, filepath: str):
    return await _client_compat(request, "GET", f"/sora/media/{filepath}")


@router.get("/sora/media-signed/{filepath:path}")
async def sora_media_signed(request: Request, filepath: str):
    # 上游 gateway.go:219 这条前缀不挂 apiKeyAuth：鉴权是 expires+sig 的 HMAC 签名本身
    # （handler:677-679 → 694-727，签名算法见 service/sora_media_sign.go:31-41）。
    # 因此这里只对 image/ video/ 子路径免 key 放行，且不向上游发送账号凭据。
    relative = _sora_signed_filepath(filepath)
    return await _client_compat(
        request, "GET", f"/sora/media-signed/{relative}",
        allow_anonymous=True, authenticated=False,
    )


@router.get("/antigravity/v1beta/models")
async def antigravity_v1beta_models(request: Request):
    return await _client_compat(request, "GET", "/antigravity/v1beta/models")


@router.get("/antigravity/v1beta/models/{model}")
async def antigravity_v1beta_model(request: Request, model: str):
    return await _client_compat(request, "GET", f"/antigravity/v1beta/models/{model}")


@router.post("/antigravity/v1beta/models/{model_action:path}")
async def antigravity_v1beta_model_action(request: Request, model_action: str):
    return await _client_compat(request, "POST", f"/antigravity/v1beta/models/{model_action}")


@router.get("/sora/v1/models")
async def sora_models(request: Request):
    return await _client_compat(request, "GET", "/sora/v1/models")
