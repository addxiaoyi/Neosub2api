from ..config import Settings
from . import (
    codebuddy,
    codar,
    codar_sidecar,
    deepseek_sidecar,
    gptgrok,
    grok_register,
    muse,
    opencode,
    qoder,
    qoder_sidecar,
    sub2api,
    trae,
    trae_sidecar,
    zcode,
)
from .base import ProviderAdapter, ProviderError, ProviderSpec  # noqa: F401

BUILDERS = (
    codebuddy.build,
    muse.build,
    grok_register.build,
    gptgrok.build,
    zcode.build,
    opencode.build,
    sub2api.build,
    trae.build,
    trae_sidecar.build,
    deepseek_sidecar.build,
    qoder.build,
    qoder_sidecar.build,
    codar.build,
    codar_sidecar.build,
)


def build_adapters(settings: Settings) -> list[ProviderAdapter]:
    return [build(settings) for build in BUILDERS]
