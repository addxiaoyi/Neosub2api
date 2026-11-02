"""Grok 注册任务服务的 provider 门面（不对外暴露推理协议）。

能力位只有 register；任务队列与状态机在 services/grok_register.py，协议见
docs/providers/grok-register.md。
"""

from typing import Any

from ..config import Settings
from ..services.grok_register import queue_stats
from .base import ProviderAdapter, ProviderSpec, stub_capabilities


class GrokRegisterAdapter(ProviderAdapter):
    spec = ProviderSpec(
        id="grok-register",
        name="Grok Register",
        vendor="kaibush/grok-register（协议参考）",
        license="本仓自研（MIT）",
        runtime="控制面内置任务服务",
        status="implemented",
        description="重写的注册任务编排：任务队列、并发控制、状态机与失败重试；浏览器执行由外部运行时承担。",
        default_base_url="",
        capabilities=stub_capabilities(register=True),
        protocols=(),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        super().__init__(base_url)

    async def health(self) -> dict[str, Any]:
        stats = queue_stats(self._settings.db_path)
        executor = "http" if self._settings.grok_register_executor_url else "unconfigured"
        return {
            "connected": True,
            "queue": stats,
            "executor": executor,
            "note": "" if executor == "http" else "未配置 GROK_REGISTER_EXECUTOR_URL，入队后任务会以明确原因失败",
        }


def build(settings: Settings, base_url: str = "") -> GrokRegisterAdapter:
    return GrokRegisterAdapter(settings, base_url)
