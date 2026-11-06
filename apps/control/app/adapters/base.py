import base64
import json
import time
from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


READINESS_STATES = ("ready", "needs_config")

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
    "embeddings",
    "images",
    "videos",
    "media",
    "rerank",
    "usage",
    "count_tokens",
)


class ProviderError(Exception):
    """适配器失败。upstream_code / upstream_message 是厂商原始信息，拿不到就留空。"""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        upstream_code: str | None = None,
        upstream_message: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.upstream_code = upstream_code
        self.upstream_message = upstream_message

    def with_vendor_detail(self, text: str) -> "ProviderError":
        """用厂商错误体补 upstream_code / upstream_message；解析不出就保持原样。"""
        code, message = parse_vendor_error(text)
        self.upstream_code = self.upstream_code or code
        self.upstream_message = self.upstream_message or message
        return self


def jwt_expired_reason(token: str) -> str:
    """只读 JWT 的 exp 判断是否过期：不验签、不外呼，判不出来就返回空串。

    凭据本身带过期时间时，本地就能给出确定性结论，不必等探活或真实请求。
    """
    raw = (token or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    parts = raw.split(".")
    if len(parts) != 3 or not parts[1]:
        return ""
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return ""
    if float(exp) <= time.time():
        when = datetime.fromtimestamp(float(exp), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return f"凭据已失效，需要重新粘贴：JWT 已过期（exp {when}）"
    return ""


def parse_vendor_error(text: str) -> tuple[str | None, str | None]:
    """从厂商错误体抽 (code, message)，兼容 OpenAI / Anthropic / 自定义 / 裸文本。"""
    raw = (text or "").strip()
    if not raw:
        return None, None
    code: Any = None
    message: Any = None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict):
        inner = data.get("error")
        if isinstance(inner, dict):
            message = inner.get("message") or inner.get("msg")
            code = inner.get("code") or inner.get("type")
        elif isinstance(inner, str):
            message = inner
        if message is None:
            message = data.get("message") or data.get("msg") or data.get("detail")
        if code is None:
            code = data.get("code") or data.get("error_code")
    else:
        message = raw
    if isinstance(message, (dict, list)):
        message = json.dumps(message, ensure_ascii=False)
    clean_code = str(code)[:200] if code is not None else None
    clean_message = str(message)[:200] if message is not None else None
    return clean_code, clean_message


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
    """就绪状态：ready = 填好 api_key / base_url 就能调用；needs_config = 还要补齐额外配置或外部进程才通。"""
    readiness: str = "ready"
    """连通前仍需补齐的项，给人看的短句（ready 时为空）。"""
    requires: tuple[str, ...] = ()
    """能力限制：key 为受限能力（如 messages_stream），value 为限制说明；与 501 报错同一句话。"""
    limitations: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.readiness not in READINESS_STATES:
            raise ValueError(f"{self.id} 的 readiness 非法：{self.readiness}")
        if self.readiness == "needs_config" and not self.requires:
            raise ValueError(f"{self.id} 标了 needs_config 却没写 requires")

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
            "readiness": self.readiness,
            "requires": list(self.requires),
            "limitations": dict(self.limitations),
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
    # 模型目录来源：upstream = 调上游；upstream_fallback = 先调上游，失败回落声明；
    # declared = 仅仓库内静态声明；none = 不提供模型目录
    catalog_mode: str = "none"
    # 探活/模型目录会不会把上游的 401/403 原样带到 registry；决定了这家能否自己判「已失效」
    auth_probe_supported: bool = False

    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url or self.spec.default_base_url
        # 控制面下发的每-provider 配置（settings 表 provider_config.{pid}）
        self.api_key = ""
        # registry 给出的完整凭据链（含 api_key 本身），供支持换 key 的适配器轮换
        self.api_keys: list[str] = []
        self.options: dict[str, Any] = {}

    def configure(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        options: dict[str, Any] | None = None,
        api_keys: list[str] | None = None,
    ) -> None:
        if base_url:
            self.base_url = base_url
        if api_key is not None:
            self.api_key = api_key
        if api_keys is not None:
            self.api_keys = list(api_keys)
        if options:
            self.options.update(options)

    def credential_problem(self, credential_type: str, text: str) -> str:
        """导入前服务商特有凭证校验；空串=通过，否则返回给人看的缺项说明。"""
        return ""

    def credential_expiry_hint(
        self, credential: str, options: dict[str, Any] | None = None
    ) -> str:
        """本地可判的失效原因（当前只读 JWT exp）；空串=判不出来，交给探活/真实请求证据。

        有些服务商把登录态放在 options.tokens 而不是 api_key，一并看一眼；只认 JWT 形状，
        普通字符串不会被误判。
        """
        candidates = [credential]
        for key in ("token", "tokens", "access_token", "jwt"):
            value = (options or {}).get(key)
            if isinstance(value, str):
                candidates.append(value)
            elif isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, str))
        for candidate in candidates:
            hint = jwt_expired_reason(candidate)
            if hint:
                return hint
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
        """按已存配置推导就绪状态：只读配置与上次探测结果，绝不外呼。

        credential / options 是 registry 刚读到的库内配置：适配器实例可能还没
        configure（列表渲染先于任何请求），所以不能依赖 self.api_key / self.options。
        """
        return self.spec.readiness, self.spec.requires

    def declared_models(self) -> list[dict[str, Any]]:
        """仓库内静态声明的模型。没有声明就返回空列表，不做任何推测。"""
        return []

    async def fetch_models(self) -> list[dict[str, Any]]:
        """从上游拉真实模型目录；失败必须抛 ProviderError，由调用方标注来源。"""
        raise ProviderError(501, f"{self.spec.name} 未提供上游模型目录")

    async def list_models(self) -> list[dict[str, Any]]:
        """兼容旧调用：声明型直接返回声明，上游型吞掉失败返回空列表。"""
        if self.catalog_mode == "declared":
            return self.declared_models()
        if self.catalog_mode in {"upstream", "upstream_fallback"}:
            try:
                return await self.fetch_models()
            except ProviderError:
                return self.declared_models() if self.catalog_mode == "upstream_fallback" else []
        # 未显式标注时按实际覆盖的方法推断，避免声明/上游目录在旧调用里恒为空
        if type(self).fetch_models is not ProviderAdapter.fetch_models:
            try:
                return await self.fetch_models()
            except ProviderError:
                return self.declared_models()
        return self.declared_models()

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
    """走本函数的 provider 输出 CAPABILITY_KEYS 全量 16 键；手写 dict 的 sidecar 只输出自己声明的子集，故 /api/providers 的 capabilities 不保证键集合一致，消费方须按真值读、不能按全集假设。"""
    return _caps(**overrides)
