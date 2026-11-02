from abc import ABC
from dataclasses import dataclass, field
from typing import Any


CAPABILITY_KEYS = (
    "chat",
    "messages",
    "models",
    "responses",
    "oauth",
    "checkin",
    "credits",
    "register",
    "sidecar",
)


class ProviderError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _caps(**overrides: bool) -> dict[str, bool]:
    caps = {key: False for key in CAPABILITY_KEYS}
    for key, value in overrides.items():
        if key not in caps:
            raise ValueError(f"未知能力位：{key}")
        caps[key] = value
    return caps


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    vendor: str
    license: str
    runtime: str
    status: str
    description: str
    default_base_url: str
    capabilities: dict[str, bool] = field(default_factory=dict)
    protocols: tuple[str, ...] = ()
    """风险等级：safe = MIT/Apache/BSD 可自由合入；warn = GPL/LGPL 传染性隔离；danger = 无 LICENSE 或非商用禁止，只能 sidecar 仅供评估。"""
    risk_level: str = "safe"
    """风险说明（danger/warn 必填，safe 可留空）。"""
    risk_note: str = ""

    def to_public(self, *, enabled: bool, base_url: str, label: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "label": label or self.name,
            "vendor": self.vendor,
            "license": self.license,
            "runtime": self.runtime,
            "status": self.status,
            "description": self.description,
            "base_url": base_url or self.default_base_url,
            "default_base_url": self.default_base_url,
            "enabled": enabled,
            "capabilities": dict(self.capabilities),
            "protocols": list(self.protocols),
            "risk_level": self.risk_level,
            "risk_note": self.risk_note,
        }


@dataclass
class AccountsPage:
    accounts: list[dict[str, Any]] = field(default_factory=list)
    pool_available: bool = True
    note: str = ""


class ProviderAdapter(ABC):
    """统一适配器接口。子类只需覆盖真正支持的方法，其余继承基类的显式失败实现。"""

    spec: ProviderSpec

    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url or self.spec.default_base_url
        # 控制面下发的每-provider 配置（settings 表 provider_config.{pid}）
        self.api_key = ""
        self.options: dict[str, Any] = {}

    def configure(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        if base_url:
            self.base_url = base_url
        if api_key is not None:
            self.api_key = api_key
        if options:
            self.options.update(options)

    async def list_models(self) -> list[dict[str, Any]]:
        return []

    async def sync_accounts(self) -> AccountsPage:
        return AccountsPage(accounts=[], pool_available=False, note="适配器未实现账号同步")

    async def health(self) -> dict[str, Any]:
        return {"connected": False, "error": f"{self.spec.name} 适配器未实现健康检查"}

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool) -> Any:
        raise ProviderError(501, f"{self.spec.name} 适配器未实现 /v1/chat/completions")

    async def messages(self, payload: dict[str, Any], *, stream: bool) -> Any:
        raise ProviderError(501, f"{self.spec.name} 适配器未实现 /v1/messages")

    async def responses(self, payload: dict[str, Any], *, stream: bool) -> Any:
        raise ProviderError(501, f"{self.spec.name} 适配器未实现 /v1/responses")

    async def start_login(self, realm: str = "cn") -> dict[str, Any]:
        raise ProviderError(501, f"{self.spec.name} 不支持扫码登录")

    async def poll_login(self, state: str, realm: str | None = None, region: str | None = None) -> dict[str, Any]:
        raise ProviderError(501, f"{self.spec.name} 不支持扫码登录轮询")

    async def account_action(self, file: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        raise ProviderError(501, f"{self.spec.name} 不支持账号操作「{action}」")

    async def delete_account(self, file: str) -> dict[str, Any]:
        raise ProviderError(501, f"{self.spec.name} 不支持删除账号")

    async def restart(self) -> dict[str, Any]:
        raise ProviderError(501, f"{self.spec.name} 的运行时不归本控制面托管，请在其自身部署处重启")

    async def push_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        raise ProviderError(501, f"{self.spec.name} 不支持配置下发")

    async def pull_config(self) -> dict[str, Any] | None:
        return None


def stub_capabilities(**overrides: bool) -> dict[str, bool]:
    return _caps(**overrides)
