from ..config import Settings
from .base import stub_capabilities
from .base import ProviderSpec
from .openai_compat import OpenAICompatAdapter


class MuseAdapter(OpenAICompatAdapter):
    """muse2api：OpenAI 兼容端点直通（/v1/chat/completions + /v1/models）。

    鉴权：MUSE2API_KEY，这里以 Bearer 形式注入（providers 页配置 api_key）。
    """

    spec = ProviderSpec(
        id="muse",
        name="Muse2API",
        vendor="czg86389-hub/muse2api",
        license="MIT",
        runtime="容器/二进制独立服务",
        status="implemented",
        description="muse 上游的 OpenAI 兼容代理，chat + models 直通转发。",
        default_base_url="http://127.0.0.1:7865",
        capabilities=stub_capabilities(chat=True, models=True),
        protocols=("openai.chat.completions",),
    )


def build(settings: Settings, base_url: str = "") -> MuseAdapter:
    return MuseAdapter(settings, base_url)
