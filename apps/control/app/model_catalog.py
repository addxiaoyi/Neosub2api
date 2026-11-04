"""跨 provider 模型目录聚合：真实并集、来源标注与 TTL 缓存。

来源判定只看实际行为，不做推测：
- declared：适配器只有静态声明（没有覆盖 fetch_models）；
- upstream：覆盖了 fetch_models 且本次调用成功（缓存命中同样视为成功实拉）；
- declared_fallback：覆盖了 fetch_models 但调用失败，回落到该适配器的静态声明。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Sequence

from .adapters.base import ProviderAdapter, ProviderError

logger = logging.getLogger("unified.models")

SOURCE_DECLARED = "declared"
SOURCE_UPSTREAM = "upstream"
SOURCE_DECLARED_FALLBACK = "declared_fallback"

_USERINFO = re.compile(r"(://)[^/@\s]+@")
_BEARER = re.compile(r"(Bearer\s+)[^\s,;]+", re.IGNORECASE)
# 上游返回的模型对象只保留元数据，密钥类字段一律不进响应
_SENSITIVE_KEYS = {"api_key", "apikey", "secret", "token", "authorization", "base_url"}
_OPTION_KEYS = ("models_path", "tier")


class ModelCatalogCache:
    """进程内 TTL 缓存；键由 provider、base_url 与凭据指纹组成，切地址/换凭据自然失效。"""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def get(self, key: str, ttl_sec: int) -> list[dict[str, Any]] | None:
        if ttl_sec <= 0:
            return None
        row = self._rows.get(key)
        if row is None:
            return None
        stored_at, items = row
        if time.monotonic() - stored_at >= ttl_sec:
            self._rows.pop(key, None)
            return None
        return [dict(item) for item in items]

    def put(self, key: str, items: list[dict[str, Any]]) -> None:
        self._rows[key] = (time.monotonic(), [dict(item) for item in items])

    def clear(self) -> None:
        self._rows.clear()


DEFAULT_CACHE = ModelCatalogCache()


def _secrets(adapter: ProviderAdapter) -> list[str]:
    raw: list[Any] = [adapter.api_key, *adapter.api_keys]
    extra = adapter.options.get("extra_keys")
    if isinstance(extra, list):
        raw.extend(extra)
    return [str(item) for item in raw if str(item or "").strip()]


def redact(text: str, adapter: ProviderAdapter | None = None) -> str:
    """失败原因脱敏：去掉 URL userinfo、Bearer 与已知密钥，再截断到 200 字。"""
    value = _USERINFO.sub(r"\1", str(text or ""))
    value = _BEARER.sub(r"\1***", value)
    for secret in _secrets(adapter) if adapter else []:
        value = value.replace(secret, "***")
    return value[:200]


def cache_key(adapter: ProviderAdapter) -> str:
    """缓存键含 provider、base_url、凭据指纹与影响目录的选项，任何一项变化都会换键。"""
    secrets = sorted({item for item in _secrets(adapter) if item})
    cred_digest = hashlib.sha256("\x1f".join(secrets).encode("utf-8")).hexdigest()[:16]
    extra = {key: adapter.options.get(key) for key in _OPTION_KEYS if adapter.options.get(key)}
    extra_digest = hashlib.sha256(
        json.dumps(extra, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:8]
    return f"{adapter.spec.id}|{adapter.base_url.rstrip('/')}|{cred_digest}|{extra_digest}"


def _supports_upstream(adapter: ProviderAdapter) -> bool:
    return type(adapter).fetch_models is not ProviderAdapter.fetch_models


def _clean_items(raw: Any) -> list[dict[str, Any]]:
    items = raw if isinstance(raw, list) else []
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        safe = {key: value for key, value in item.items() if str(key).lower() not in _SENSITIVE_KEYS}
        safe["id"] = model_id
        cleaned.append(safe)
    return cleaned


def _declared(adapter: ProviderAdapter) -> list[dict[str, Any]]:
    try:
        return _clean_items(adapter.declared_models())
    except Exception as exc:  # 声明表本身出错时如实留空，不补条目
        logger.warning(
            "模型声明读取失败 provider=%s reason=%s",
            adapter.spec.id,
            redact(f"{type(exc).__name__}: {exc}", adapter),
        )
        return []


async def _load(
    adapter: ProviderAdapter, ttl_sec: int, cache: ModelCatalogCache
) -> tuple[list[dict[str, Any]], str, str]:
    if _supports_upstream(adapter):
        key = cache_key(adapter)
        hit = cache.get(key, ttl_sec)
        if hit is not None:
            return hit, SOURCE_UPSTREAM, ""
        try:
            items = _clean_items(await adapter.fetch_models())
        except ProviderError as exc:
            reason = redact(exc.message, adapter)
            logger.warning("模型目录拉取失败 provider=%s reason=%s", adapter.spec.id, reason)
            declared = _declared(adapter)
            if declared:
                return declared, SOURCE_DECLARED_FALLBACK, reason
            return [], SOURCE_DECLARED_FALLBACK, reason
        except Exception as exc:
            reason = redact(f"{type(exc).__name__}: {exc}", adapter)
            logger.warning("模型目录拉取异常 provider=%s reason=%s", adapter.spec.id, reason)
            declared = _declared(adapter)
            if declared:
                return declared, SOURCE_DECLARED_FALLBACK, reason
            return [], SOURCE_DECLARED_FALLBACK, reason
        cache.put(key, items)
        return items, SOURCE_UPSTREAM, ""
    return _declared(adapter), SOURCE_DECLARED, ""


def _state(
    adapter: ProviderAdapter,
    enabled: bool,
    source: str,
    fallback_reason: str,
) -> dict[str, Any]:
    spec = adapter.spec
    ready = spec.readiness == "ready"
    available = ready and enabled
    if not enabled:
        reason = "provider 未启用"
    elif not ready:
        reason = "；".join(spec.requires) or "配置未就绪"
    else:
        reason = None
    return {
        "source": source,
        "readiness": spec.readiness,
        "requires": list(spec.requires),
        "limitations": dict(spec.limitations),
        "enabled": enabled,
        "available": available,
        "unavailable_reason": reason,
        "fallback_reason": fallback_reason or None,
    }


def _refresh(entry: dict[str, Any]) -> None:
    states = list(entry["provider_states"].values())
    entry["available"] = any(state["available"] for state in states)
    entry["enabled"] = any(state["enabled"] for state in states)
    entry["readiness"] = "ready" if entry["available"] else "needs_config"
    requires: list[str] = []
    for state in states:
        for item in state["requires"]:
            if item not in requires:
                requires.append(item)
    entry["requires"] = requires
    reasons: list[str] = []
    for state in states:
        reason = state["unavailable_reason"]
        if reason and reason not in reasons:
            reasons.append(reason)
    entry["unavailable_reason"] = None if entry["available"] else ("；".join(reasons) or None)
    primary = entry["providers"][0]
    entry["provider"] = primary
    entry["source"] = entry["provider_states"][primary]["source"]
    entry["fallback_reason"] = entry["provider_states"][primary]["fallback_reason"]


def _merge(
    merged: dict[str, dict[str, Any]],
    adapter: ProviderAdapter,
    enabled: bool,
    item: dict[str, Any],
    source: str,
    fallback_reason: str,
) -> None:
    pid = adapter.spec.id
    model_id = item["id"]
    state = _state(adapter, enabled, source, fallback_reason)
    entry = merged.get(model_id)
    if entry is None:
        entry = {
            **item,
            "id": model_id,
            "object": str(item.get("object") or "model"),
            "owned_by": pid,
            "provider": pid,
            "source": source,
            "providers": [pid],
            "provider_states": {pid: state},
        }
        merged[model_id] = entry
    else:
        if pid not in entry["providers"]:
            entry["providers"].append(pid)
        entry["provider_states"][pid] = state
    _refresh(entry)


async def collect_models(
    targets: Sequence[tuple[ProviderAdapter, bool]],
    *,
    ttl_sec: int = 60,
    cache: ModelCatalogCache | None = None,
) -> list[dict[str, Any]]:
    """按 provider 顺序聚合并集；同 id 合并为一条，保留各 provider 的真实来源与就绪状态。"""
    store = cache if cache is not None else DEFAULT_CACHE
    merged: dict[str, dict[str, Any]] = {}
    for adapter, enabled in targets:
        if not adapter.spec.capabilities.get("models"):
            continue
        items, source, fallback_reason = await _load(adapter, ttl_sec, store)
        for item in items:
            _merge(merged, adapter, enabled, item, source, fallback_reason)
    return list(merged.values())
