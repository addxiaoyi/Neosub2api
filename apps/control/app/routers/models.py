import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from ..adapters.base import ProviderAdapter
from ..model_catalog import collect_models
from ..security import current_user

router = APIRouter(prefix="/api", tags=["models"])

# 首屏聚合接口必须有上限：任一上游探活卡住时不能拖着整个页面一起等。
STATUS_BUDGET_SEC = 4.0
MODELS_BUDGET_SEC = 5.0
_TIMEOUT_REASON = "探测超时（已返回部分状态，该上游按不可用处理）"


@router.get("/status")
async def upstream_status(request: Request, user: dict = Depends(current_user)):
    registry = request.app.state.registry
    adapters = list(registry.enabled())
    deadline = time.monotonic() + STATUS_BUDGET_SEC

    async def probe(adapter: ProviderAdapter) -> tuple[ProviderAdapter, dict]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return adapter, {"connected": False, "error": _TIMEOUT_REASON}
        try:
            return adapter, await asyncio.wait_for(adapter.health(), timeout=remaining)
        except asyncio.TimeoutError:
            return adapter, {"connected": False, "error": _TIMEOUT_REASON}
        except Exception as exc:
            return adapter, {"connected": False, "error": f"{type(exc).__name__}: {exc}"}

    started = time.monotonic()
    results = await asyncio.gather(*(probe(adapter) for adapter in adapters))
    merged: dict = {"connected": False, "partial": False, "items": [], "unavailable": []}
    errors: list[str] = []
    for adapter, health in results:
        connected = bool(health.get("connected"))
        item = {"id": adapter.spec.id, "name": adapter.spec.name, "connected": connected}
        if connected:
            for key in ("total", "healthy", "cooling", "disabled", "in_flight_full", "realm_totals"):
                if key in health:
                    merged[key] = health[key]
            merged["connected"] = True
            merged.setdefault("provider", adapter.spec.id)
        else:
            reason = str(health.get("error") or "不可达")
            item["error"] = reason
            errors.append(f"{adapter.spec.name}: {reason}")
            merged["unavailable"].append(item)
        merged["items"].append(item)
    if errors:
        merged["error"] = "；".join(errors)
        merged["partial"] = True
    merged["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return merged


@router.get("/models")
async def list_models(
    request: Request,
    provider: str | None = None,
    realm: str | None = None,
    user: dict = Depends(current_user),
):
    registry = request.app.state.registry
    public = {item["id"]: item for item in registry.list_public()}
    targets: list[tuple[ProviderAdapter, bool]] = []
    if provider:
        pid = provider.strip()
        if pid not in public:
            raise HTTPException(status_code=404, detail=f"未知的 provider：{pid}")
        # 显式指定 provider 时不再按 enabled 过滤，便于查看未启用家的声明
        targets.append((registry.get(pid), bool(public[pid]["enabled"])))
    else:
        targets.extend(
            (registry.get(pid), True) for pid, item in public.items() if item["enabled"]
        )
    ttl_sec = int(getattr(request.app.state.settings, "models_cache_ttl_sec", 60))
    models = await collect_models(targets, ttl_sec=ttl_sec, timeout_sec=MODELS_BUDGET_SEC)
    return {
        "models": models,
        "source": "dynamic" if models else "unknown",
        "count": len(models),
    }
