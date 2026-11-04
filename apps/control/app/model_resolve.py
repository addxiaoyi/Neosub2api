"""model 字段解析：显式指定优先，其次按已启用 provider 的模型目录自动匹配。

三条 /v1 协议共用同一套优先级：

1. `x-provider` 头；
2. `<provider_id>/<model>` 前缀（前缀是已知 provider 时）；
3. 「设置 → 模型别名」：沿别名链走到尽头（自指 A→A 当没有别名），撞到循环报 404
   `model_not_found` 并指向请求入口，`detail.cycle` 列出入环成员；
4. 模型目录自动匹配：唯一命中直接走，多家命中 400，无命中 404。

兜底开关（`UNIFIED_MODEL_FALLBACK_FIRST`，默认关）只作用在「一家都没命中」这一步：
打开后用首家启用的、支持当前端点能力位的 provider 承接未知模型名。多家命中（歧义）
仍返回 400，不会被兜底掩盖。

头与前缀同时给出且指向不同 provider 时直接 400 `conflicting_provider_hints`（不静默取
其一），同源则剥离前缀后转发；本地注册表未命中报 `provider_not_found`，与真正的上游
404（`upstream_not_found`）分开。

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
CONFLICTING_HINTS = "conflicting_provider_hints"
MODEL_NOT_FOUND = "model_not_found"
MODEL_UNAVAILABLE = "model_unavailable"
PROVIDER_DISABLED = "provider_disabled"
PROVIDER_NOT_FOUND = "provider_not_found"

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


def _known_provider(registry: Any, pid: str, conn=None) -> ProviderAdapter | None:
    try:
        return registry.get(pid, conn=conn)
    except ProviderError as exc:
        if exc.status_code != 404:
            raise
        return None


def _enabled_ids(registry: Any, conn=None) -> set[str]:
    """当前启用的 provider 集合：显式指定不能绕过「停用即不可用」。

    网关解析把准入段的连接借进来复用；测试等独立调用不传则自开自关。
    """
    own = conn is None
    if own:
        conn = registry.db.connect()
    try:
        rows = conn.execute("SELECT id FROM providers WHERE enabled = 1").fetchall()
    finally:
        if own:
            conn.close()
    return {str(row["id"]) for row in rows}


def _require_enabled(registry: Any, adapter: ProviderAdapter, conn=None) -> None:
    pid = adapter.spec.id
    if pid in _enabled_ids(registry, conn):
        return
    message = f"provider {mask_text(pid)} 已停用，请先在「服务商」页启用后再调用"
    raise ModelResolutionError(
        503,
        message,
        type_="api_error",
        code=PROVIDER_DISABLED,
        detail={"message": message, "provider": mask_text(pid)},
    )


def _prefix_of(registry: Any, model: str, conn=None) -> tuple[ProviderAdapter, str] | None:
    """只判「前缀是不是已知 provider」，不查停用：冲突判定必须先于停用判定。"""
    if "/" not in model:
        return None
    prefix, rest = model.split("/", 1)
    if not rest:
        return None
    adapter = _known_provider(registry, prefix, conn)
    if adapter is None:
        return None
    return adapter, rest


def _prefixed(registry: Any, model: str, conn=None) -> tuple[ProviderAdapter, str] | None:
    hit = _prefix_of(registry, model, conn)
    if hit is None:
        return None
    _require_enabled(registry, hit[0], conn)
    return hit


def _alias_target(registry: Any, model: str, conn=None) -> str:
    """沿别名链逐跳走到尽头，返回终点；没有别名或自指（A→A）时就是 model 自己。

    撞到别名循环时直接抛 404：报请求入口而不是链末端，入环成员列在 detail.cycle。
    """
    own = conn is None
    if own:
        conn = registry.db.connect()
    try:
        mapping = get_json_setting(conn, "model_map", {})
    finally:
        if own:
            conn.close()
    if not isinstance(mapping, dict):
        return model
    chain = [model]
    final = model
    while True:
        target = str(mapping.get(final) or "").strip()
        if not target or target == final:
            return final
        if target in chain:
            raise _alias_cycle(model, chain[chain.index(target):])
        chain.append(target)
        final = target


def _alias_cycle(entry: str, cycle: list[str]) -> ModelResolutionError:
    """别名循环：错误名指向请求入口而不是链末端，入环成员在 detail.cycle 里可核。"""
    ring = " → ".join(mask_text(name) for name in [*cycle, cycle[0]])
    message = (
        f"模型 {mask_text(entry)} 存在别名循环（{ring}），"
        "请在「设置 → 模型别名」里改掉入环任一项"
    )
    return ModelResolutionError(
        404,
        message,
        type_=MODEL_NOT_FOUND,
        code=MODEL_NOT_FOUND,
        detail={
            "message": message,
            "model": mask_text(entry),
            "cycle": [mask_text(name) for name in cycle],
        },
    )


async def _catalog(
    registry: Any, ttl_sec: int, conn=None
) -> tuple[list[dict[str, Any]], dict[str, ProviderAdapter]]:
    adapters = registry.enabled(conn=conn)
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


def _unknown_provider(pid: str) -> ModelResolutionError:
    """本地注册表未命中：与真实上游 404（upstream_not_found）分开命名。"""
    message = f"未知的 provider：{mask_text(pid)}"
    return ModelResolutionError(
        404,
        message,
        type_=PROVIDER_NOT_FOUND,
        code=PROVIDER_NOT_FOUND,
        detail={"message": message, "provider": mask_text(pid)},
    )


def _conflict(header_pid: str, prefix_pid: str, model: str) -> ModelResolutionError:
    """x-provider 与 provider/ 前缀指向不同 provider：要求调用方二选一，不静默取其一。"""
    message = (
        f"x-provider 指定 {mask_text(header_pid)}，模型名 {mask_text(model)} 的前缀指向 "
        f"{mask_text(prefix_pid)}：两个来源冲突，请去掉其中一个 provider 指定"
    )
    return ModelResolutionError(
        400,
        message,
        type_=CONFLICTING_HINTS,
        code=CONFLICTING_HINTS,
        detail={
            "message": message,
            "model": mask_text(model),
            "provider": mask_text(header_pid),
            "header_provider": mask_text(header_pid),
            "prefix_provider": mask_text(prefix_pid),
        },
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


def _fallback_provider(registry: Any, capability: str, conn=None) -> tuple[ProviderAdapter, int] | None:
    """首个启用的、支持该能力位的 provider；候选数一并返回给日志。"""
    capable = [adapter for adapter in registry.enabled(conn=conn) if adapter.spec.capabilities.get(capability)]
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
    conn=None,
) -> tuple[ProviderAdapter, str, str]:
    """返回 (adapter, 上游使用的模型名, 解析模式)；ProviderError 交给调用方统一映射。

    conn：网关准入段共用连接，借给需要读库的判定步骤复用，不传则各步骤自开自关。
    """
    pid = (explicit or "").strip()
    # 前缀只做存在性识别；冲突/停用的判定顺序：本地存在性 → 冲突 → 停用
    prefix_hit = _prefix_of(registry, model, conn)
    if pid:
        adapter = _known_provider(registry, pid, conn)
        if adapter is None:
            raise _unknown_provider(pid)
        if prefix_hit is not None and prefix_hit[0].spec.id != pid:
            raise _conflict(pid, prefix_hit[0].spec.id, model)
        _require_enabled(registry, adapter, conn)
        # 头与前缀同源时按前缀剥离转发，避免把整串 provider/model 交给上游
        return adapter, prefix_hit[1] if prefix_hit else model, MODE_EXPLICIT

    if prefix_hit is not None:
        adapter, rest = prefix_hit
        _require_enabled(registry, adapter, conn)
        return adapter, rest, MODE_PREFIX

    target = _alias_target(registry, model, conn)
    if target != model:
        prefixed = _prefixed(registry, target, conn)
        if prefixed is not None:
            adapter, rest = prefixed
            return adapter, rest, MODE_ALIAS

    entries, adapters = await _catalog(registry, ttl_sec, conn)
    entry = next((item for item in entries if str(item.get("id") or "") == target), None)
    if entry is None:
        if fallback_first:
            picked = _fallback_provider(registry, capability, conn)
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
