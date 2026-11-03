"""sub2api 订阅配额分发适配器（clean-room 重写）。

语义见 docs/providers/sub2api.md：分组 → 账号池 → 配额扣减 → 429。
上游由账号池里的每个账号自带 base_url/api_key 指定，不依赖 sub2api 进程。
"""

import asyncio
from typing import Any

from ..billing import extract_usage, usage_from_stream, usage_in_chunk
from ..config import Settings
from .base import ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient, UpstreamReply
from .sub2api_store import QuotaStore, build_store

MAX_ACCOUNT_ATTEMPTS = 3
ACCOUNT_RETRY_STATUS = {429, 500, 502, 503, 504}


class _MeteredReply:
    """流式响应包装：边转发边攒 usage，流结束时扣减配额。"""

    def __init__(self, inner: UpstreamReply, on_done) -> None:
        self._inner = inner
        self.status_code = inner.status_code
        self.headers = inner.headers
        self.body = inner.body
        self._on_done = on_done

    async def iter_bytes(self):
        chunks: list[bytes] = []
        try:
            async for chunk in self._inner.iter_bytes():
                if len(chunks) < 16 and usage_in_chunk(chunk):
                    chunks.append(chunk)
                yield chunk
        finally:
            self._on_done(usage_from_stream(chunks))


class Sub2ApiAdapter(ProviderAdapter):
    spec = ProviderSpec(
        id="sub2api",
        name="Sub2API",
        vendor="Wei-Shaw/sub2api（协议参考）",
        license="本仓自研（MIT）",
        runtime="主仓内置（配额分发）",
        status="implemented",
        description="重写的订阅额度分发：分组、账号池、token 配额扣减，额度耗尽返回 429。",
        default_base_url="",
        capabilities=stub_capabilities(chat=True, messages=True, models=True),
        protocols=("openai.chat.completions", "anthropic.messages"),
        readiness="needs_config",
        requires=(
            "先建分组与账号（/api/sub2api/groups、/api/sub2api/accounts，缺了请求直接 503）",
        ),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        self._store: QuotaStore = build_store(settings)
        super().__init__(base_url)

    @property
    def store(self) -> QuotaStore:
        return self._store

    def _group_id(self) -> str:
        return str(self.options.get("group") or "default").strip() or "default"

    def _client(self, account: dict[str, Any]) -> UnifiedApiClient:
        base = str(account.get("upstream_base") or self.base_url or "").rstrip("/")
        if not base:
            raise ProviderError(503, f"sub2api 账号 {account['id']} 未配置 upstream_base，且 provider 未设 base_url")
        return UnifiedApiClient(
            base,
            api_key=str(account.get("api_key") or self.api_key or ""),
            timeout=float(self.options.get("timeout") or 120.0),
            proxy=str(self.options.get("proxy") or ""),
        )

    def _charge(
        self,
        group_id: str,
        account_id: int | None,
        model: str,
        usage,
        status: int,
        note: str = "",
        reason: str = "",
    ) -> None:
        try:
            self._store.charge(
                group_id, account_id, model=model, usage=usage, status=status, note=note, reason=reason
            )
        except Exception:
            # 扣减失败不能改变已经返回给客户端的响应；流水缺失由下一次对账发现
            pass

    def _pick(self, group_id: str) -> dict[str, Any]:
        account = self._store.pick_account_credentials(group_id)
        if account is None:
            raise ProviderError(429, f"sub2api 分组 {group_id} 账号池无可用账号（全部冷却或额度耗尽）")
        return account

    async def _forward(self, capability: str, payload: dict[str, Any], *, stream: bool):
        group_id = self._group_id()
        self._store.require_group(group_id)
        model = str(payload.get("model") or "")
        replied: UpstreamReply | None = None
        account: dict[str, Any] | None = None
        for attempt in range(MAX_ACCOUNT_ATTEMPTS):
            try:
                account = self._pick(group_id)
            except ProviderError as exc:
                # 空池拒绝也落一笔流水，标明「无可用账号」，保证对账不断层
                self._charge(
                    group_id,
                    None,
                    model,
                    None,
                    status=exc.status_code,
                    note="空池拒绝",
                    reason="no_available_account",
                )
                raise
            client = self._client(account)
            path = "/v1/chat/completions" if capability == "chat" else "/v1/messages"
            replied = await client.forward(path, payload, stream=stream)
            if replied.status_code not in ACCOUNT_RETRY_STATUS:
                break
            self._store.cool_down(int(account["id"]))
            if attempt == MAX_ACCOUNT_ATTEMPTS - 1:
                break
            await replied.aclose()
            await asyncio.sleep(0.2 * (attempt + 1))

        assert replied is not None and account is not None
        account_id = int(account["id"])
        if stream:
            return _MeteredReply(
                replied,
                lambda usage: self._charge(group_id, account_id, model, usage, replied.status_code),
            )
        usage = extract_usage(replied.body or b"")
        self._charge(group_id, account_id, model, usage, replied.status_code)
        return replied

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        return await self._forward("chat", payload, stream=stream)

    async def messages(self, payload: dict[str, Any], *, stream: bool):
        return await self._forward("messages", payload, stream=stream)

    async def fetch_models(self) -> list[dict[str, Any]]:
        groups = self._store.list_groups()
        if not groups:
            raise ProviderError(503, "尚未创建配额分组（/api/sub2api/groups）")
        group_id = self._group_id()
        account = self._store.peek_account_credentials(group_id) if any(
            item["id"] == group_id for item in groups
        ) else None
        if account is None:
            raise ProviderError(503, f"分组 {group_id} 无可用账号")
        data = await self._client(account).get_json("/v1/models")
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict) and item.get("id")]

    async def health(self) -> dict[str, Any]:
        groups = self._store.list_groups()
        if not groups:
            return {"connected": False, "error": "尚未创建配额分组（/api/sub2api/groups）"}
        group_id = self._group_id()
        group = next((item for item in groups if item["id"] == group_id), None)
        if group is None:
            return {"connected": False, "error": f"分组 {group_id} 不存在"}
        if group["quota_limit"] > 0 and group["quota_used"] >= group["quota_limit"]:
            return {"connected": False, "error": f"分组 {group_id} 额度已耗尽", "quota_used": group["quota_used"]}
        if group["active_accounts"] == 0:
            return {"connected": False, "error": f"分组 {group_id} 无可用账号", "groups": len(groups)}
        try:
            models = await self.list_models()
        except Exception as exc:
            return {"connected": False, "error": str(exc), "groups": len(groups)}
        return {
            "connected": True,
            "groups": len(groups),
            "group": group_id,
            "active_accounts": group["active_accounts"],
            "models": len(models),
        }


def build(settings: Settings, base_url: str = "") -> Sub2ApiAdapter:
    return Sub2ApiAdapter(settings, base_url)
