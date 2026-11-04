"""Deepseek2Go sidecar 代理。

⚠️ 许可证风险：上游仓库 **无 LICENSE 文件**（All Rights Reserved）。
   unified-2api 仅作为 HTTP 代理对接 sidecar 进程，**不复制不修改**上游源码。
   商用前必须移除本 provider。

sidecar 暴露：/v1/chat/completions（流式+非流式）、/v1/models、
/anthropic/v1/messages（仅非流式，路径非标准——适配器做了重写）。
"""

from typing import Any

from ..config import Settings
from .base import ProviderError, ProviderSpec
from .openai_compat import OpenAICompatAdapter

# 限制声明与 501 报错共用这一句，两处措辞必须一致
MESSAGES_STREAM_LIMIT = "Deepseek2Go sidecar 只支持非流式 messages"


class DeepseekSidecarAdapter(OpenAICompatAdapter):
    spec = ProviderSpec(
        id="deepseek_sidecar",
        name="DeepSeek (Deepseek2Go)",
        vendor="ColudAI/Deepseek2Go",
        license="无 LICENSE（All Rights Reserved）",
        runtime="独立进程 sidecar（Go + WASM PoW）",
        status="sidecar",
        description="DeepSeek 官方 chat.deepseek.com 代理，含 WASM PoW。sidecar 自行管理账号登录与 token 刷新。",
        default_base_url="http://127.0.0.1:5001",
        capabilities={
            "chat": True,
            "messages": True,
            "models": True,
            "oauth": False,
            "checkin": False,
            "credits": False,
            "register": False,
            "sidecar": True,
        },
        protocols=("openai.chat.completions", "anthropic.messages"),
        risk_level="danger",
        risk_note="上游无 LICENSE，版权全保留。本 provider 仅供个人评估，商用前必须移除 sidecar 及其适配器。",
        readiness="needs_config",
        requires=(
            "sidecar 进程（Deepseek2Go，默认端口 5001）",
        ),
        limitations={"messages_stream": MESSAGES_STREAM_LIMIT},
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        super().__init__(settings, base_url)

    async def messages(self, payload: dict[str, Any], *, stream: bool):
        """sidecar 的 Anthropic 端点路径非标准（/anthropic/v1/messages），做一层重写。"""
        if stream:
            raise ProviderError(501, MESSAGES_STREAM_LIMIT)
        return await self._client().forward("/anthropic/v1/messages", payload, stream=False)


def build(settings: Settings, base_url: str = "") -> DeepseekSidecarAdapter:
    return DeepseekSidecarAdapter(settings, base_url)
