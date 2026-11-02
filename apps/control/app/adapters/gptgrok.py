from ..config import Settings
from .base import ProviderSpec, stub_capabilities
from .openai_compat import OpenAICompatAdapter


class GptGrokAdapter(OpenAICompatAdapter):
    """gptGrok2api：GPT/Grok 的 OpenAI 兼容反代，chat + models 直通。

    风控开关（providers 页 options）：
    - proxy: 出口代理 URL，只作用于本 provider 的上游请求
    - fingerprint: true 时注入 extra_headers 里的指纹头（UA、sec-ch-ua 等）
    - extra_headers: 自定义头 dict，fingerprint 关闭时不注入
    """

    spec = ProviderSpec(
        id="gptgrok",
        name="GPT/Grok 2API",
        vendor="addxiaoyi/gptGrok2api",
        license="MIT",
        runtime="容器/二进制独立服务",
        status="implemented",
        description="GPT 与 Grok 上游的 OpenAI 兼容反代，chat + models 直通，支持代理与指纹头开关。",
        default_base_url="http://127.0.0.1:7867",
        capabilities=stub_capabilities(chat=True, models=True),
        protocols=("openai.chat.completions",),
    )

    def _extra_headers(self):
        # 指纹头默认不注入：上游服务自己已有指纹处理，这里是额外覆盖层
        if not self.options.get("fingerprint"):
            return None
        return super()._extra_headers()


def build(settings: Settings, base_url: str = "") -> GptGrokAdapter:
    return GptGrokAdapter(settings, base_url)
