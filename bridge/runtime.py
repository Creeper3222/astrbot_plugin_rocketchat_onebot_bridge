from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from .config import BridgeConfig, BridgeReactionConfig
from .id_map import DurableIdMap
from .onebot_actions import OneBotActionHandler
from .onebot_client import OneBotReverseWsClient
from .paths import resolve_plugin_data_dir
from .rocketchat_client import RocketChatClient
from .storage import ContextRoomStore, JsonStore, MessageStore, PrivateRoomStore
from .translator_inbound import InboundTranslator
from .translator_outbound import OutboundMessageTranslator


DisableCallback = Callable[[], Awaitable[None] | None]


class BridgeRuntime:
    def __init__(
        self,
        plugin_root: Path,
        raw_config: dict[str, Any] | Any,
        *,
        data_dir: Path | None = None,
        instance_name: str = "bridge",
        reaction_config: BridgeReactionConfig | None = None,
        disable_callback: DisableCallback | None = None,
    ):
        self.plugin_root = plugin_root
        self.raw_config = raw_config
        self.instance_name = instance_name
        if data_dir is None:
            data_dir = resolve_plugin_data_dir(plugin_root)
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.reaction_config = reaction_config or BridgeReactionConfig.from_mapping(raw_config)
        self._disable_callback = disable_callback

        self.config = BridgeConfig.from_mapping(raw_config)
        self.state_store = JsonStore(self.data_dir / "runtime_state.json")
        self.id_map = DurableIdMap(JsonStore(self.data_dir / "id_map.json"))
        self.message_store = MessageStore(JsonStore(self.data_dir / "message_registry.json"))
        self.private_room_store = PrivateRoomStore(JsonStore(self.data_dir / "private_rooms.json"))
        self.context_room_store = ContextRoomStore(JsonStore(self.data_dir / "context_room_registry.json"))
        self.rocketchat: RocketChatClient | None = None
        self.inbound_translator: InboundTranslator | None = None
        self.outbound_translator: OutboundMessageTranslator | None = None
        self.action_handler: OneBotActionHandler | None = None
        self.onebot: OneBotReverseWsClient | None = None
        self._failure_task: asyncio.Task | None = None
        self._restart_lock = asyncio.Lock()
        self._failure_handled = False
        self._started = False

    @classmethod
    def from_plugin_root(cls, raw_config: dict[str, Any] | Any) -> "BridgeRuntime":
        plugin_root = Path(__file__).resolve().parent.parent
        return cls(plugin_root=plugin_root, raw_config=raw_config)

    async def start(self) -> None:
        self._reload_config_snapshot()
        errors = self.config.validate()
        if errors:
            logger.error(
                f"[RocketChatOneBotBridge][{self.instance_name}] 配置校验失败: {'; '.join(errors)}"
            )
            return

        if not self.config.enabled:
            logger.info(
                f"[RocketChatOneBotBridge][{self.instance_name}] 当前 bot enabled=false，桥接不会启动。"
            )
            return

        await self._start_clients()

        await self.state_store.write(
            {
                "status": "running",
                "server_url": self.config.server_url,
                "onebot_ws_url": self.config.onebot_ws_url,
                "onebot_self_id": self.config.onebot_self_id,
                "max_reconnect_attempts": self.config.max_reconnect_attempts,
            }
        )
        self._started = True
        logger.info(f"[RocketChatOneBotBridge][{self.instance_name}] bridge 运行时已启动。")

    async def stop(self) -> None:
        if self._failure_task is not None:
            self._failure_task.cancel()
            try:
                await self._failure_task
            except asyncio.CancelledError:
                pass
            self._failure_task = None
        await self._stop_clients()
        await self.state_store.write({"status": "stopped"})
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    async def get_basic_info_summary(self) -> dict[str, Any] | None:
        self._reload_config_snapshot()
        if not self.config.enabled:
            return None

        client_name = "主bot" if self.config.is_main_bot else (self.config.display_name or self.instance_name)
        login_username = str(self.config.username or "").strip()
        nickname = login_username or client_name
        avatar_url = ""
        user_id = ""
        server_display_name = ""
        server_avatar_url = ""

        if self.rocketchat is not None:
            try:
                user_info = await self.rocketchat.get_current_user_info()
            except Exception:
                user_info = {}

            if user_info:
                login_username = str(
                    user_info.get("username")
                    or self.rocketchat.bot_username
                    or login_username
                ).strip()
                nickname = str(
                    user_info.get("name")
                    or user_info.get("nickname")
                    or login_username
                    or client_name
                ).strip()
                user_id = str(user_info.get("_id") or self.rocketchat.user_id or "").strip()
            else:
                login_username = str(self.rocketchat.bot_username or login_username).strip()
                user_id = str(self.rocketchat.user_id or "").strip()

            avatar_url = self.rocketchat.build_avatar_url(login_username)
            if self.rocketchat.auth_token and self.rocketchat.user_id:
                try:
                    server_branding_summary = await self.rocketchat.get_server_branding_summary()
                except Exception:
                    server_branding_summary = None
                if server_branding_summary:
                    server_display_name = str(server_branding_summary.get("display_name") or "").strip()
                    server_avatar_url = str(server_branding_summary.get("avatar_url") or "").strip()

        status_code = "offline"
        status_label = "未接入"
        if self.started:
            status_code = "online" if self.rocketchat and self.rocketchat.auth_token and self.rocketchat.user_id else "starting"
            status_label = "已连接" if status_code == "online" else "连接中"

        return {
            "bot_id": self.config.bot_id,
            "client_name": client_name,
            "login_username": login_username,
            "nickname": nickname or login_username or client_name,
            "avatar_url": avatar_url,
            "status_code": status_code,
            "status_label": status_label,
            "server_url": self.config.server_url,
            "onebot_self_id": self.config.onebot_self_id,
            "server_display_name": server_display_name,
            "server_avatar_url": server_avatar_url,
            "is_main_bot": self.config.is_main_bot,
            "user_id": user_id,
        }

    async def _handle_rocketchat_message(self, raw_msg: dict[str, Any]) -> None:
        if self.inbound_translator is None or self.onebot is None:
            return
        event = await self.inbound_translator.translate(raw_msg)
        if event is None:
            return
        await self.onebot.emit_event(event)

    def _reload_config_snapshot(self) -> None:
        self.config = BridgeConfig.from_mapping(self.raw_config)

    async def _start_clients(self) -> None:
        self._reload_config_snapshot()
        self._failure_handled = False

        self.rocketchat = RocketChatClient(
            self.config,
            on_message=self._handle_rocketchat_message,
            on_reconnect_exhausted=self._handle_reconnect_exhausted,
        )
        self.inbound_translator = InboundTranslator(
            rocketchat=self.rocketchat,
            id_map=self.id_map,
            messages=self.message_store,
            private_rooms=self.private_room_store,
            context_rooms=self.context_room_store,
            self_id=self.config.onebot_self_id,
        )
        self.outbound_translator = OutboundMessageTranslator(
            rocketchat=self.rocketchat,
            id_map=self.id_map,
            messages=self.message_store,
            private_rooms=self.private_room_store,
            context_rooms=self.context_room_store,
        )
        self.action_handler = OneBotActionHandler(
            config=self.config,
            reaction_config=self.reaction_config,
            rocketchat=self.rocketchat,
            id_map=self.id_map,
            messages=self.message_store,
            private_rooms=self.private_room_store,
            context_rooms=self.context_room_store,
            inbound=self.inbound_translator,
            outbound=self.outbound_translator,
        )
        self.onebot = OneBotReverseWsClient(
            self.config,
            action_handler=self.action_handler.handle,
            on_reconnect_exhausted=self._handle_reconnect_exhausted,
        )

        await self.onebot.start()
        await self.rocketchat.start()

    async def _stop_clients(self) -> None:
        if self.rocketchat is not None:
            await self.rocketchat.stop()
        if self.onebot is not None:
            await self.onebot.stop()
        self.rocketchat = None
        self.inbound_translator = None
        self.outbound_translator = None
        self.action_handler = None
        self.onebot = None

    async def restart_connections(self, reason: str) -> None:
        async with self._restart_lock:
            self._reload_config_snapshot()
            errors = self.config.validate()
            if errors:
                logger.error(
                    f"[RocketChatOneBotBridge][{self.instance_name}] 配置校验失败: {'; '.join(errors)}"
                )
                await self.state_store.write({"status": "error", "error": '; '.join(errors)})
                return
            if not self.config.enabled:
                logger.info(
                    f"[RocketChatOneBotBridge][{self.instance_name}] 当前 enabled=false，跳过 bridge 重连。"
                )
                await self._stop_clients()
                await self.state_store.write({"status": "stopped", "reason": reason, "enabled": False})
                self._started = False
                return

            await self.state_store.write({"status": "restarting", "reason": reason})
            await self._stop_clients()
            await self._start_clients()
            await self.state_store.write(
                {
                    "status": "running",
                    "server_url": self.config.server_url,
                    "onebot_ws_url": self.config.onebot_ws_url,
                    "onebot_self_id": self.config.onebot_self_id,
                    "max_reconnect_attempts": self.config.max_reconnect_attempts,
                    "restart_reason": reason,
                }
            )
            logger.info(
                f"[RocketChatOneBotBridge][{self.instance_name}] bridge 连接已重启，reason={reason}"
            )

    async def _handle_reconnect_exhausted(
        self,
        client_name: str,
        attempts: int,
        error: str,
    ) -> None:
        if self._failure_handled:
            return
        self._failure_handled = True
        await self._disable_bridge_after_reconnect_failure()
        await self.state_store.write(
            {
                "status": "failed",
                "client": client_name,
                "attempts": attempts,
                "error": error,
                "enabled": False,
                "auto_disabled": True,
            }
        )
        logger.error(
            f'[RocketChatOneBotBridge][{self.instance_name}] 当前 bot 已因重连失败被自动关闭，请重新开启后再尝试连接。'
        )
        if self._failure_task is None or self._failure_task.done():
            self._failure_task = asyncio.create_task(self._enter_failed_state())

    async def _enter_failed_state(self) -> None:
        async with self._restart_lock:
            await self._stop_clients()
            self._started = False

    async def _disable_bridge_after_reconnect_failure(self) -> None:
        self.config.enabled = False
        if isinstance(self.raw_config, dict):
            self.raw_config["enabled"] = False
        elif hasattr(self.raw_config, "__setitem__"):
            self.raw_config["enabled"] = False
        if self._disable_callback is not None:
            result = self._disable_callback()
            if inspect.isawaitable(result):
                await result
            return
        if hasattr(self.raw_config, "save_config"):
            self.raw_config.save_config()
