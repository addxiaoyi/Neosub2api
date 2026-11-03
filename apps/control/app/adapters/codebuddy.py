import json
from typing import Any

from ..config import Settings
from .base import AccountsPage, ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient

# 管理端设置下发时只透传这些组（与 workbuddy2api config.json 的分组一致）
PUSH_GROUPS = (
    "schedule",
    "prompt",
    "cooldown",
    "pool",
    "features",
    "session_sticky",
    "server",
    "upstream",
    "global",
    "admin",
)

# 账号列表里不进主表字段、但对界面有价值的运行时信息，收进 meta_json
META_KEYS = (
    "healthy",
    "cooling",
    "cooldown_remaining",
    "checkin_today",
    "status",
    "in_flight",
    "pool",
)


class CodeBuddyAdapter(ProviderAdapter):
    """对接 codebuddy2api 的 Python 管理端（默认 7864）。

    账号管理、设置下发走管理端 /api/*；chat/models 也经管理端网关代理到 Go 数据面。
    鉴权用管理端的作用域化 API Token（Authorization: Bearer wbt_...）。
    注意：删除账号 / 重启 / 设置下发在管理端要求会话级管理员，wbt token 可能被拒，
    此时如实透传管理端的错误。
    """

    spec = ProviderSpec(
        id="codebuddy",
        name="CodeBuddy",
        vendor="腾讯 CodeBuddy（addxiaoyi/codebuddy2api）",
        license="MIT",
        runtime="Python 管理端（7864）+ Go 数据面（workbuddy2api, 7863）",
        status="implemented",
        description="腾讯 CodeBuddy 账号池反向代理，OpenAI Chat Completions / Anthropic Messages 双协议。",
        default_base_url="http://127.0.0.1:7864",
        capabilities=stub_capabilities(
            chat=True,
            messages=True,
            models=True,
            oauth=True,
            checkin=True,
            credits=True,
        ),
        protocols=("openai.chat.completions", "anthropic.messages"),
        readiness="needs_config",
        requires=(
            "7864 管理端进程 + wbt_ token（缺了账号同步、签到与设置下发全部失败）",
        ),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        super().__init__(base_url)

    def _client(self, timeout: float = 120.0) -> UnifiedApiClient:
        return UnifiedApiClient(self.base_url, api_key=self.api_key, timeout=timeout)

    @staticmethod
    def _map_account(raw: dict[str, Any]) -> dict[str, Any]:
        uid = str(raw.get("uid") or "")
        meta = {k: raw[k] for k in META_KEYS if k in raw}
        return {
            "file": str(raw.get("file") or f"{uid}.json"),
            "uid": uid,
            "nickname": str(raw.get("nickname") or ""),
            "enterprise_id": str(raw.get("enterprise_id") or raw.get("enterpriseId") or ""),
            "realm": str(raw.get("realm") or "cn"),
            "expires_at": int(raw.get("expires_at") or 0),
            "credits": raw.get("credits"),
            "note": str(raw.get("note") or ""),
            "disabled": 1 if raw.get("disabled") else 0,
            "meta_json": json.dumps(meta, ensure_ascii=False),
        }

    async def sync_accounts(self) -> AccountsPage:
        client = self._client(timeout=30.0)
        try:
            payload = await client.get_json("/api/accounts")
        except ProviderError as exc:
            return AccountsPage(accounts=[], pool_available=False, note=exc.message)
        items = payload.get("accounts") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return AccountsPage(accounts=[], pool_available=False, note="管理端返回结构异常")
        mapped = [self._map_account(a) for a in items if isinstance(a, dict) and a.get("uid")]
        return AccountsPage(
            accounts=mapped,
            pool_available=bool(payload.get("pool_available")),
            note="",
        )

    async def health(self) -> dict[str, Any]:
        client = self._client(timeout=15.0)
        try:
            payload = await client.get_json("/api/status")
        except ProviderError as exc:
            # 未配置 token 时管理端会 401——服务本身可能仍在线，如实区分
            if exc.status_code in (401, 403):
                return {"connected": True, "auth": "missing", "error": f"管理端在线但需配置 API Token：{exc.message}"}
            return {"connected": False, "error": str(exc)}
        if not isinstance(payload, dict):
            return {"connected": True}
        merged = {"connected": True}
        for key in ("total", "healthy", "cooling", "disabled", "in_flight_full", "realm_totals"):
            if key in payload:
                merged[key] = payload[key]
        return merged

    async def fetch_models(self) -> list[dict[str, Any]]:
        client = self._client(timeout=30.0)
        try:
            data = await client.get_json("/v1/models")
        except ProviderError as first_error:
            # 管理端网关不通时退到管理端 /api/models（上游静态表，仍算上游实拉）
            try:
                payload = await client.get_json("/api/models")
            except ProviderError:
                raise first_error
            items = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise first_error
            return [m for m in items if isinstance(m, dict) and m.get("id")]
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict) and item.get("id")]

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        return await self._client().forward("/v1/chat/completions", payload, stream=stream)

    async def messages(self, payload: dict[str, Any], *, stream: bool):
        return await self._client().forward("/v1/messages", payload, stream=stream)

    async def start_login(self, realm: str = "cn") -> dict[str, Any]:
        payload = await self._client(timeout=30.0).post_json("/api/auth/start", {"realm": realm})
        url = str(payload.get("authUrl") or payload.get("auth_url") or "")
        state = str(payload.get("state") or "")
        if not state or not url:
            raise ProviderError(502, "上游未返回二维码链接，请确认 CodeBuddy 管理端（7864）已就绪")
        return {"state": state, "authUrl": url, "realm": str(payload.get("realm") or realm)}

    async def poll_login(self, state: str, realm: str | None = None, region: str | None = None) -> dict[str, Any]:
        params = {"state": state}
        if realm:
            params["realm"] = realm
        if region:
            params["region"] = region
        return await self._client(timeout=30.0).get_json("/api/auth/poll", params=params)

    async def account_action(self, file: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if action == "disabled":
            return await self._client(timeout=60.0).post_json(
                f"/api/accounts/{file}/disabled", payload or {}
            )
        return await self._client(timeout=60.0).post_json(f"/api/accounts/{file}/{action}")

    async def set_note(self, file: str, note: str) -> dict[str, Any]:
        return await self._client(timeout=30.0).put_json(f"/api/accounts/{file}/note", {"note": note})

    async def delete_account(self, file: str) -> dict[str, Any]:
        return await self._client(timeout=30.0).delete_json(f"/api/accounts/{file}")

    async def restart(self) -> dict[str, Any]:
        return await self._client(timeout=60.0).post_json("/api/restart")

    async def checkin_all(self) -> dict[str, Any]:
        return await self._client(timeout=180.0).post_json("/api/accounts/checkin-all")

    async def refresh_credits(self, force: bool = False) -> dict[str, Any]:
        return await self._client(timeout=120.0).request_json(
            "POST", f"/api/accounts/refresh-credits?force={'1' if force else '0'}"
        )

    async def push_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        body = {k: v for k, v in cfg.items() if k in PUSH_GROUPS and isinstance(v, dict)}
        if not body:
            raise ProviderError(400, "没有可下发的配置组")
        return await self._client(timeout=60.0).post_json("/api/settings/upstream", body)

    async def pull_config(self) -> dict[str, Any] | None:
        try:
            return await self._client(timeout=30.0).get_json("/api/settings/upstream")
        except ProviderError:
            return None


def build(settings: Settings, base_url: str = "") -> CodeBuddyAdapter:
    return CodeBuddyAdapter(settings, base_url)
