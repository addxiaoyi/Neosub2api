"""provider 单次连通性探测。

账号导入的「保存前试连」和定时健康检测共用这一套判定，避免出现两种口径：
- 覆盖了 health() 的适配器按它自己报的 connected 判定；
- 只覆盖 fetch_models() 的拉一次模型目录，拉得到就算连通；
- 两个都没覆盖的明确返回 unsupported，不伪造失败。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from ..adapters.base import ProviderAdapter, ProviderError
from ..api_utils import mask_text
from ..model_catalog import redact

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_UNSUPPORTED = "unsupported"
STATUSES = (STATUS_OK, STATUS_FAILED, STATUS_UNSUPPORTED)

# 单次探测上限：上游卡住时手动/定时那一轮不能无限挂着
PROBE_TIMEOUT_SEC = 20.0


def implements(adapter: ProviderAdapter, method: str) -> bool:
    """适配器是否**真的**覆盖了基类方法。

    能力位（spec.capabilities）是给转发/模型用的声明，不能直接当作探测能力：
    声明了 checkin 不代表实现了账号方法。以实际覆写为准。
    """
    impl = getattr(type(adapter), method, None)
    base = getattr(ProviderAdapter, method, None)
    return callable(impl) and (base is None or impl is not base)


@dataclass(frozen=True)
class ProbeOutcome:
    status: str
    latency_ms: int
    message: str
    upstream_status: int | None = None
    # 探活本身带出了「上游拒了这把凭据」的证据；只有明确证据才置 True，不按文案猜
    auth_failed: bool = False


AUTH_STATUS_CODES = (401, 403)
# health() 用这几个键自报鉴权状态（codebuddy 的 auth、sidecar 的 state=auth）
AUTH_STATE_VALUES = ("missing", "invalid", "expired", "unauthorized", "forbidden")


def _auth_from_health(result: dict[str, Any]) -> bool:
    auth = str(result.get("auth") or "").strip().lower()
    if auth in AUTH_STATE_VALUES:
        return True
    return str(result.get("state") or "").strip().lower() == "auth"


def _clean(raw: Any, adapter: ProviderAdapter) -> str:
    """已知密钥 + URL userinfo + Bearer 先去掉，再走通用掩码，最后截断。"""
    return mask_text(redact(str(raw or ""), adapter))[:300]


async def _run(adapter: ProviderAdapter) -> ProbeOutcome:
    started = time.perf_counter()
    upstream: int | None = None
    auth_failed = False
    try:
        if implements(adapter, "health"):
            result = await adapter.health()
            connected = bool(result.get("connected"))
            status = STATUS_OK if connected else STATUS_FAILED
            # codebuddy 的管理端会 connected=True 但 auth=missing，鉴权失败要单独算，不能只看连通
            auth_failed = _auth_from_health(result)
            message = str(result.get("error") or result.get("note") or "") or (
                "上游已确认连通" if connected else "上游未确认连通"
            )
        elif implements(adapter, "fetch_models"):
            models = await adapter.fetch_models()
            status = STATUS_OK
            message = f"已连通，模型目录 {len(models)} 个"
        else:
            status = STATUS_UNSUPPORTED
            message = f"{adapter.spec.name} 没有可探测的健康检查"
    except ProviderError as exc:
        status, upstream, message = STATUS_FAILED, exc.status_code, exc.message
        auth_failed = exc.status_code in AUTH_STATUS_CODES
    except Exception as exc:  # 网络错误形态不一，统一成一条脱敏摘要
        status, message = STATUS_FAILED, str(exc)
        upstream = getattr(exc, "status_code", None)
        auth_failed = upstream in AUTH_STATUS_CODES
    return ProbeOutcome(
        status=status,
        latency_ms=int((time.perf_counter() - started) * 1000),
        message=_clean(message, adapter),
        upstream_status=upstream,
        auth_failed=auth_failed,
    )


async def probe(adapter: ProviderAdapter, *, timeout: float = PROBE_TIMEOUT_SEC) -> ProbeOutcome:
    try:
        return await asyncio.wait_for(_run(adapter), timeout=timeout)
    except asyncio.TimeoutError:
        return ProbeOutcome(
            status=STATUS_FAILED,
            latency_ms=int(timeout * 1000),
            message=f"探测超时（{timeout:g}s）",
        )
