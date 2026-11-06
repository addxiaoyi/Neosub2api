"""codar2api sidecar 代理（MIT）。

codar2api 是独立的 Go 网关，对外暴露 OpenAI Chat、Anthropic Messages 与 Responses；
账号登录、令牌续期、签到、代理池全部留在 sidecar 进程里。主仓只做 HTTP 透传，
不复制、不内置上游源码或二进制。

本批接入面：/v1/chat/completions（JSON + SSE 直通）、/v1/messages、/v1/responses、/v1/models。
/v1/responses 经契约交叉验证后声明：端点注册见 docs/providers/codar2api.md:55、:283
（app.go:189-202 注册、relay.go:260 同路径转发）；上游缺实现时原样透传 404/501，不伪造。
"""

from typing import Any

from ..config import Settings
from .base import ProviderSpec, stub_capabilities
from .sidecar_link import SidecarLink


class CodarSidecarAdapter(SidecarLink):
    requires_api_key = True

    spec = ProviderSpec(
        id="codar_sidecar",
        name="Codar (codar2api)",
        vendor="AiXiaoc/codar2api",
        license="MIT",
        runtime="独立进程 sidecar（Go 标准库）",
        status="sidecar",
        description="Codar 账号池 + OpenAI Chat / Responses、Anthropic Messages 三协议。签到、令牌续期、代理池在 sidecar 内完成。",
        default_base_url="http://127.0.0.1:8801",
        capabilities=stub_capabilities(
            chat=True,
            messages=True,
            models=True,
            responses=True,
            sidecar=True,
            count_tokens=True,
        ),
        protocols=("openai.chat.completions", "anthropic.messages", "openai.responses"),
        risk_level="safe",
        risk_note="MIT sidecar：主仓只经 HTTP 代理，不复制不内置上游源码。Codar 平台限本人名下账号，转售/共享风险由部署方承担。",
        readiness="needs_config",
        requires=(
            "sidecar 进程（codar2api，默认端口 8801）",
        ),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        super().__init__(settings, base_url)

    async def messages(self, payload: dict[str, Any], *, stream: bool):
        """标准 Anthropic 路径 /v1/messages；流式与非流式都由 sidecar 处理。"""
        return await self._sidecar_forward("/v1/messages", payload, stream=stream)

    async def responses(self, payload: dict[str, Any], *, stream: bool):
        """OpenAI Responses 同路径透传；sidecar 端点契约见 codar2api.md:55、:283。"""
        return await self._sidecar_forward("/v1/responses", payload, stream=stream)

    async def count_tokens(self, payload: dict[str, Any], *, stream: bool):
        """Anthropic token 计数透传；端点契约见 codar2api.md:57。"""
        return await self._sidecar_forward("/v1/messages/count_tokens", payload, stream=stream)


def build(settings: Settings, base_url: str = "") -> CodarSidecarAdapter:
    return CodarSidecarAdapter(settings, base_url)
