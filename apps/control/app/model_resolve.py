"""model 字段解析：显式指定优先，其次按已启用 provider 的模型目录自动匹配。

三条 /v1 协议共用同一套优先级：

1. `x-provider` 头；
2. `<provider_id>/<model>` 前缀（前缀是已知 provider 时）；
3. 「设置 → 模型别名」；
4. 模型目录自动匹配：唯一命中直接走，多家命中 400，无命中 404。

兜底开关（`UNIFIED_MODEL_FALLBACK_FIRST`，默认关）只作用在「一家都没命中」这一步：
打开后用首家启用的、支持当前端点能力位的 provider 承接未知模型名。多家命中（歧义）
仍返回 400，不会被兜底掩盖。

解析出来的 provider / model 就是 request_logs 里写的两个字段，不新增表。
"""

from __future__ import annotations

import logging
from typing import Any

from .adapters.base import ProviderAdapter, ProviderError
from .api_utils import mask_text
from .db import get_json_setting
from .gateway_errors import GatewayError
from .model_catalog import collect_models

logger = logging.getLogger("unified.model_resolve")

AMBIGUOUS_MODEL = "ambiguous_model"
MODEL_NOT_FOUND = "model_not_found"
MODEL_UNAVAILABLE = "model_unavailable"

MODE_EXPLICIT = "explicit"
MODE_PREFIX = "prefix"
MODE_ALIAS = "alias"
MODE_CATALOG = "catalog"
MODE_FALLBACK = "fallback"


class ModelResolutionError(GatewayError):
    """解析失败：OpenAI 壳保持原样，Anthropic 壳补 code 与候选列表，便于客户端消歧。"""

    def anthropic_payload(self) -> dict[str, Any]:
        payload = super().anthropic_payload()
        payload["error"]["code"] = self.error["code"]
        candidates = self.detail.get("candidates") if isinstance(self.detail, dict) else None
        if candidates:
            payload["error"]["candidates"] = candidates
        return payload


def _known_provider(registry: Any, pid: str) -> ProviderAdapter | None:
    try:
        return registry.get(pid)
    except ProviderError as exc:
        if exc.status_code != 404:
            raise
        return None


def _prefixed(registry: Any, model: str) -> tuple[ProviderAdapter, str] | None:
    if "/" not in model:
        return None
    prefix, rest = model.split("/", 1)
    if not rest:
        return None
    adapter = _known_provider(registry, prefix)
    if adapter is None:
        return None
    return adapter, rest


def _alias_target(registry: Any, model: str) -> str | None:
    conn = registry.db.connect()
    try:
        mapping = get_json_setting(conn, "model_map", {})
    finally:
        conn.close()
    if not isinstance(mapping, dict):
        return None
    target = str(mapping.get(model) or "").strip()
    return target if target and target != model else None


async def _catalog(
    registry: Any, ttl_sec: int
) -> tuple[list[dict[str, Any]], dict[str, ProviderAdapter]]:
    adapters = registry.enabled()
    entries = await collect_models([(adapter, True) for adapter in adapters], ttl_sec=ttl_sec)
    return entries, {adapter.spec.id: adapter for adapter in adapters}


def _capable_hits(
    entry: dict[str, Any],
    adapters: dict[str, ProviderAdapter],
    capability: str,
) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for pid in entry.get("providers") or []:
        adapter = adapters.get(pid)
        if adapter is None or not adapter.spec.capabilities.get(capability):
            continue
        state = (entry.get("provider_states") or {}).get(pid) or {}
        hits.append((pid, str(state.get("readiness") or "unknown")))
    return hits


def _ambiguous(model: str, hits: list[tuple[str, str]]) -> ModelResolutionError:
    names = "、".join(pid for pid, _ in hits)
    message = f"模型 {mask_text(model)} 在多个已启用的 provider 中都存在（{names}），请写成 provider/model 指定"
    candidates = [{"provider": pid, "readiness": readiness} for pid, readiness in hits]
    return ModelResolutionError(
        400,
        message,
        type_=AMBIGUOUS_MODEL,
        code=AMBIGUOUS_MODEL,
        detail={"message": message, "model": mask_text(model), "candidates": candidates},
    )


def _reason(item: dict[str, Any]) -> str:
    head = "provider 未启用"
    if str(item.get("readiness") or "") == "needs_config":
        head = "provider 未启用且配置未就绪"
    requires = [str(need).strip() for need in item.get("requires") or [] if str(need).strip()]
    return f"{head}（{'；'.join(requires)}）" if requires else head


def _unavailable_candidates(registry: Any, model: str) -> list[dict[str, Any]]:
    """只在 404 时调用：拿未启用 provider 的静态声明解释「存在但不可用」。"""
    found: list[dict[str, Any]] = []
    for item in registry.list_public():
        if item.get("enabled"):
            continue  # 已启用家的目录已经在自动匹配里查过
        adapter = _known_provider(registry, str(item.get("id") or ""))
        if adapter is None:
            continue
        try:
            declared = adapter.declared_models()
        except Exception:  # noqa: BLE001 - 提示信息不值得打挂请求
            continue
        if not any(str(entry.get("id") or "").strip() == model for entry in declared if isinstance(entry, dict)):
            continue
        found.append(
            {
                "provider": item["id"],
                "readiness": item.get("readiness"),
                "enabled": False,
                "requires": list(item.get("requires") or []),
                "reason": _reason(item),
            }
        )
    return found


def _missing(registry: Any, model: str) -> ModelResolutionError:
    hidden = _unavailable_candidates(registry, model)
    if hidden:
        names = "、".join(item["provider"] for item in hidden)
        reasons = "；".join(f"{item['provider']}：{item['reason']}" for item in hidden)
        message = f"模型 {mask_text(model)} 已存在但当前不可用（{names}）：{reasons}"
        return ModelResolutionError(
            404,
            message,
            type_=MODEL_UNAVAILABLE,
            code=MODEL_UNAVAILABLE,
            detail={"message": message, "model": mask_text(model), "candidates": hidden},
        )
    message = f"没有已启用的 provider 提供模型 {mask_text(model)}"
    return ModelResolutionError(
        404,
        message,
        type_=MODEL_NOT_FOUND,
        code=MODEL_NOT_FOUND,
        detail={"message": message, "model": mask_text(model), "candidates": []},
    )


def _fallback_provider(registry: Any, capability: str) -> tuple[ProviderAdapter, int] | None:
    """首个启用的、支持该能力位的 provider；候选数一并返回给日志。"""
    capable = [adapter for adapter in registry.enabled() if adapter.spec.capabilities.get(capability)]
    if not capable:
        return None
    return capable[0], len(capable)


async def resolve_model(
    registry: Any,
    model: str,
    explicit: str | None = None,
    capability: str = "chat",
    ttl_sec: int = 60,
    *,
    fallback_first: bool = False,
    request_id: str | None = None,
) -> tuple[ProviderAdapter, str, str]:
    """返回 (adapter, 上游使用的模型名, 解析模式)；ProviderError 交给调用方统一映射。"""
    pid = (explicit or "").strip()
    if pid:
        return registry.get(pid), model, MODE_EXPLICIT

    prefixed = _prefixed(registry, model)
    if prefixed is not None:
        adapter, rest = prefixed
        return adapter, rest, MODE_PREFIX

    target = model
    alias = _alias_target(registry, model)
    if alias is not None:
        target = alias
        prefixed = _prefixed(registry, target)
        if prefixed is not None:
            adapter, rest = prefixed
            return adapter, rest, MODE_ALIAS

    entries, adapters = await _catalog(registry, ttl_sec)
    entry = next((item for item in entries if str(item.get("id") or "") == target), None)
    if entry is None:
        if fallback_first:
            picked = _fallback_provider(registry, capability)
            if picked is not None:
                adapter, candidates = picked
                logger.info(
                    "unified.model_fallback request_id=%s model=%s provider=%s candidates=%d",
                    request_id or "-", mask_text(target), adapter.spec.id, candidates,
                )
                return adapter, target, MODE_FALLBACK
        raise _missing(registry, target)

    hits = _capable_hits(entry, adapters, capability)
    if len(hits) > 1:
        raise _ambiguous(target, hits)
    provider = hits[0][0] if hits else str(entry["providers"][0])
    return adapters[provider], target, MODE_CATALOG
