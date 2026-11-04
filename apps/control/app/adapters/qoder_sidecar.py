"""qoder-trae-workbuddy-switch sidecar 代理。

⚠️ 许可证风险：上游仓库使用 **PolyForm Noncommercial 1.0.0**，明确禁止任何商用。
   unified-2api 仅作为 HTTP 代理对接 sidecar 进程，**不复制不修改**上游源码。
   商用前必须移除本 provider。

sidecar（buddy-switch-gateway）暴露：/v1/chat/completions、/v1/models、
/v1/messages、/v1/responses，以及多产品线多地域账号池与降级链。
"""

from typing import Any

from ..config import Settings
from .base import ProviderSpec
from .openai_compat import OpenAICompatAdapter


class QoderSidecarAdapter(OpenAICompatAdapter):
    spec = ProviderSpec(
        id="qoder_sidecar",
        name="Qoder Switch (PolyForm-NC)",
        vendor="NextAgentX/qoder-trae-workbuddy-switch",
        license="PolyForm Noncommercial 1.0.0（禁止商用）",
        runtime="独立进程 sidecar（Rust Axum gateway）",
        status="sidecar",
        description="多产品线多地域账号池 + /v1/chat|messages|models|responses + 降级链。sidecar 自行管理账号切换。",
        default_base_url="http://127.0.0.1:7873",
        capabilities={
            "chat": True,
            "messages": True,
            "models": True,
            "responses": True,
            "oauth": False,
            "checkin": False,
            "credits": False,
            "register": False,
            "sidecar": True,
        },
        protocols=("openai.chat.completions", "anthropic.messages", "openai.responses"),
        risk_level="danger",
        risk_note="PolyForm Noncommercial 1.0.0 禁止任何商用（包括内部商业项目与托管服务）。作者书面授权前不得商用。",
        readiness="needs_config",
        requires=(
            "sidecar 进程（buddy-switch-gateway，默认端口 7873）",
        ),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        super().__init__(settings, base_url)

    async def messages(self, payload: dict[str, Any], *, stream: bool):
        """标准 Anthropic 路径 /v1/messages，非流式与流式都支持。"""
        return await self._client().forward("/v1/messages", payload, stream=stream)

    async def responses(self, payload: dict[str, Any], *, stream: bool):
        """标准 OpenAI 路径 /v1/responses，非流式与流式都支持。"""
        return await self._client().forward("/v1/responses", payload, stream=stream)


def build(settings: Settings, base_url: str = "") -> QoderSidecarAdapter:
    return QoderSidecarAdapter(settings, base_url)
