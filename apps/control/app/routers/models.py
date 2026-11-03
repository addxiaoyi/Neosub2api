from fastapi import APIRouter, Depends, HTTPException, Request

from ..adapters.base import ProviderAdapter
from ..model_catalog import collect_models
from ..security import current_user

router = APIRouter(prefix="/api", tags=["models"])


@router.get("/status")
async def upstream_status(request: Request, user: dict = Depends(current_user)):
    registry = request.app.state.registry
    errors: list[str] = []
    merged: dict = {"connected": False}
    for adapter in registry.enabled():
        health = await adapter.health()
        if health.get("connected"):
            for key in ("total", "healthy", "cooling", "disabled", "in_flight_full", "realm_totals"):
                if key in health:
                    merged[key] = health[key]
            merged["connected"] = True
            merged.setdefault("provider", adapter.spec.id)
        else:
            errors.append(f"{adapter.spec.name}: {health.get('error', '不可达')}")
    if errors:
        merged["error"] = "；".join(errors)
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
    models = await collect_models(targets, ttl_sec=ttl_sec)
    return {
        "models": models,
        "source": "dynamic" if models else "unknown",
        "count": len(models),
    }
