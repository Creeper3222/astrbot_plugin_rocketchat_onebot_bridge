from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig

from .bridge.manager import BridgeManager


@register(
    "rocketchat_onebot_bridge",
    "Creeper3222",
    "将 Rocket.Chat 通过内嵌 OneBot v11 bridge 的类napcat方式接入 AstrBot。",
    "v0.1.2",
)
class RocketCatPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict[str, Any]):
        super().__init__(context)
        self.config = config
        self.manager = BridgeManager.from_plugin_root(self.config)

    async def initialize(self) -> None:
        await self.manager.start()
        logger.info("[RocketChatOneBotBridge] 插件初始化完成。")

    async def terminate(self) -> None:
        await self.manager.stop()
        logger.info("[RocketChatOneBotBridge] 插件已停止。")