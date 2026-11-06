"""trae2api sidecar 代理。

trae2api（MIT）自身是完整的 Trae Solo + Work 双通道 2api 服务，暴露 OpenAI 兼容的
/v1/chat/completions 与 /v1/models（无 /v1/messages），账号池 / OAuth / 积分探测全在
sidecar 进程里自己管理。unified-2api 只做透明转发。
"""

from ..config import Settings
from .base import ProviderSpec
from .sidecar_link import SidecarLink


class TraeSidecarAdapter(SidecarLink):
    spec = ProviderSpec(
        id="trae_sidecar",
        name="Trae Solo+Work (trae2api)",
        vendor="JeffHu0912/trae2api",
        license="MIT",
        runtime="独立进程 sidecar（Go）",
        status="sidecar",
        description="Trae Solo 裸模型 + Work 积分通道的自动降级双通道。sidecar 自行管理账号池与 OAuth。",
        default_base_url="http://127.0.0.1:7871",
        capabilities={
            "chat": True,
            "messages": False,
            "models": True,
            "oauth": False,
            "checkin": False,
            "credits": False,
            "register": False,
            "sidecar": True,
        },
        protocols=("openai.chat.completions",),
        user_agent="Trae/0.1.52",
        risk_level="safe",
        risk_note="",
        readiness="needs_config",
        requires=(
            "sidecar 进程（trae2api，默认端口 7871）",
        ),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        super().__init__(settings, base_url)


def build(settings: Settings, base_url: str = "") -> TraeSidecarAdapter:
    return TraeSidecarAdapter(settings, base_url)
