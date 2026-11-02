from fastapi import APIRouter, Depends, Request

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
    realm: str | None = None,
    user: dict = Depends(current_user),
):
    registry = request.app.state.registry
    models: list[dict] = []
    seen: set[str] = set()
    for adapter in registry.enabled():
        if not adapter.spec.capabilities.get("models"):
            continue
        for item in await adapter.list_models():
            model_id = str(item.get("id") or "")
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            models.append({**item, "id": model_id, "owned_by": adapter.spec.id})
    return {
        "models": models,
        "source": "dynamic" if models else "unknown",
        "count": len(models),
    }
