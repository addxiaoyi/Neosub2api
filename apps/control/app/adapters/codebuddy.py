import json
import logging
from typing import Any

import httpx

from ..config import Settings
from .base import AccountsPage, ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient, UpstreamReply

logger = logging.getLogger("unified.gateway")

# 贴近真实 CodeBuddy 客户端的 UA；裸 httpx 头很容易被风控挑出来
CLIENT_UA = "CodeBuddy/2.50.0"
ANTHROPIC_VERSION = "2023-06-01"

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

ADMIN_TOKEN_PREFIX = "wbt_"
ADMIN_TOKEN_HINT = (
    "CodeBuddy 需要 7864 管理端的 wbt_ API Token：先启动管理端（默认本机 7864 端口），"
    "再在管理端「设置 → API Token」生成后导入"
)
ADMIN_CREDENTIAL_TYPES = ("api_key", "bearer")


class _AdminClient(UnifiedApiClient):
    """管理端客户端：连接失败翻译成可执行的进程提示，其余错误原样透传。"""

    def _translate(self, exc: ProviderError) -> ProviderError:
        if exc.status_code != 502 or "上游不可达" not in exc.message:
            return exc
        base = self.base_url.rstrip("/")
        cause = exc.__cause__
        # 原文（含本机地址/端口）只进服务端日志，对外只说能做什么
        logger.warning("CodeBuddy 管理端不可达 base=%s cause=%s", base, type(cause).__name__)
        if isinstance(cause, httpx.ConnectError):
            return ProviderError(502, "CodeBuddy 管理端进程未运行：连接被拒绝，请先启动管理端")
        if isinstance(cause, httpx.TimeoutException):
            return ProviderError(504, "CodeBuddy 管理端响应超时（详情见服务端日志）")
        return ProviderError(502, "CodeBuddy 管理端不可达：连接失败（详情见服务端日志）")

    async def _guard(self, call: Any) -> Any:
        try:
            return await call
        except ProviderError as exc:
            translated = self._translate(exc)
            if translated is exc:
                raise
            raise translated from exc

    async def request_json(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._guard(super().request_json(method, path, payload))

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._guard(super().get_json(path, params))

    async def forward(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        stream: bool,
        method: str = "POST",
        raw: bytes | None = None,
        accept: str | None = None,
    ) -> UpstreamReply:
        return await self._guard(
            super().forward(path, payload, stream=stream, method=method, raw=raw, accept=accept)
        )


class CodeBuddyAdapter(ProviderAdapter):
    """对接 codebuddy2api 的 Python 管理端（默认 7864）。

    账号管理、设置下发走管理端 /api/*；chat/models 也经管理端网关代理到 Go 数据面。
    鉴权用管理端的作用域化 API Token（Authorization: Bearer wbt_...）。
    注意：删除账号 / 重启 / 设置下发在管理端要求会话级管理员，wbt token 可能被拒，
    此时如实透传管理端的错误。
    """

    # 探活把管理端的 401/403 直接落成 auth=missing，能判「已失效」
    auth_probe_supported = True

    spec = ProviderSpec(
        id="codebuddy",
        name="CodeBuddy",
        vendor="腾讯 CodeBuddy（addxiaoyi/codebuddy2api）",
        license="MIT",
        runtime="Python 管理端（7864）+ Go 数据面（workbuddy2api, 7863）",
        status="implemented",
        description="腾讯 CodeBuddy 账号池反向代理，OpenAI Chat Completions / Responses 与 Anthropic Messages 协议。",
        default_base_url="http://127.0.0.1:7864",
        capabilities=stub_capabilities(
            chat=True,
            messages=True,
            models=True,
            responses=True,
            count_tokens=True,
            oauth=True,
            checkin=True,
            credits=True,
        ),
        protocols=("openai.chat.completions", "openai.responses", "anthropic.messages"),
        user_agent=CLIENT_UA,
        readiness="needs_config",
        requires=(
            "7864 管理端进程 + wbt_ token：先启动 codebuddy2api 管理端（默认本机 7864 端口），"
            "再在管理端「设置 → API Token」生成 wbt_ 开头的 token 后导入；缺任一项则账号同步、"
            "签到与设置下发全部失败",
        ),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        super().__init__(base_url)

    def _client(
        self, timeout: float | None = None, *, headers: dict[str, str] | None = None
    ) -> UnifiedApiClient:
        merged = {"user-agent": self.client_user_agent()}
        if headers:
            merged.update(headers)
        effective = timeout or float(self.options.get("timeout") or 120.0)
        return _AdminClient(
            self.base_url, api_key=self.api_key, timeout=effective, extra_headers=merged
        )

    def credential_problem(self, credential_type: str, text: str) -> str:
        if credential_type not in ADMIN_CREDENTIAL_TYPES:
            return f"CodeBuddy 管理端只接受 wbt_ API Token；{credential_type} 类型不能用于管理端鉴权"
        key = text.strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        if not key:
            return ADMIN_TOKEN_HINT
        if not key.startswith(ADMIN_TOKEN_PREFIX):
            return f"CodeBuddy 管理端 token 必须以 wbt_ 开头（当前值前缀不符）；{ADMIN_TOKEN_HINT}"
        return ""

    def readiness_now(
        self,
        *,
        has_credential: bool,
        credential_problem: str,
        last_probe: dict[str, Any] | None,
        credential: str = "",
        options: dict[str, Any] | None = None,
    ) -> tuple[str, tuple[str, ...]]:
        if has_credential and not credential_problem:
            if (last_probe or {}).get("status") == "failed":
                reason = str((last_probe or {}).get("error") or "管理端不可达")
                return "needs_config", (f"上次探活失败：{reason}",)
            return "ready", ()
        if credential_problem:
            return "needs_config", (credential_problem,)
        return "needs_config", self.spec.requires

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
                detail = f"管理端在线，但 wbt_ token 无效或权限不足：{exc.message}"
                return {"connected": True, "auth": "missing", "error": detail}
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
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return await self._client(headers=headers).forward("/v1/messages", payload, stream=stream)

    async def responses(self, payload: dict[str, Any], *, stream: bool):
        return await self._client().forward("/v1/responses", payload, stream=stream)

    async def count_tokens(self, payload: dict[str, Any], *, stream: bool):
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return await self._client(headers=headers).forward("/v1/messages/count_tokens", payload, stream=False)

    async def start_login(self, realm: str = "cn") -> dict[str, Any]:
        payload = await self._client(timeout=30.0).post_json("/api/auth/start", {"realm": realm})
        url = str(payload.get("authUrl") or payload.get("auth_url") or "")
        state = str(payload.get("state") or "")
        if not state or not url:
            raise ProviderError(502, "上游未返回二维码链接，请确认 CodeBuddy 管理端已就绪")
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
