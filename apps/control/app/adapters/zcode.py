from typing import Any

from ..config import Settings
from .base import ProviderSpec, stub_capabilities
from .openai_compat import OpenAICompatAdapter


class ZCodeAdapter(OpenAICompatAdapter):
    """ZCode2api：ZCode 上游的 OpenAI/Anthropic 双协议反代。

    风控开关（providers 页 options）：
    - system_prompt_override: 非空字符串时，转发前把它作为 system 消息置顶注入。
      ZCode 上游自身另有 system prompt 改写（zcode-profile.json）；本开关是控制面
      这一层的可配置改写，默认关闭，避免与上游改写叠加。
    """

    spec = ProviderSpec(
        id="zcode",
        name="ZCode 2API",
        vendor="addxiaoyi/ZCode2api",
        license="MIT",
        runtime="Node 独立服务（zcgw-server.mjs）",
        status="implemented",
        description="ZCode 上游的 OpenAI/Anthropic 兼容反代，chat + messages + models 直通，system 改写可开关。",
        default_base_url="http://127.0.0.1:8787",
        capabilities=stub_capabilities(chat=True, messages=True, models=True),
        protocols=("openai.chat.completions", "anthropic.messages"),
    )

    def _transform_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        override = str(self.options.get("system_prompt_override") or "").strip()
        if not override:
            return payload
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return payload
        out = [m for m in messages]
        out.insert(0, {"role": "system", "content": override})
        return {**payload, "messages": out}

    async def messages(self, payload: dict[str, Any], *, stream: bool):
        return await self._client().forward("/v1/messages", payload, stream=stream)


def build(settings: Settings, base_url: str = "") -> ZCodeAdapter:
    return ZCodeAdapter(settings, base_url)
