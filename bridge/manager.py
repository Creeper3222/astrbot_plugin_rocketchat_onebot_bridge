from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote

from astrbot.api import logger

from ..webui import IndependentWebUIServer
from .config import (
    DEFAULT_SUB_BOT_NAME,
    DEFAULT_WEBUI_HOST,
    BridgeConfig,
    BridgeControlConfig,
    build_default_sub_bot_payload,
)
from .paths import resolve_plugin_data_dir
from .runtime import BridgeRuntime
from .storage import JsonStore


class BridgeManager:
    def __init__(self, plugin_root: Path, raw_config: dict[str, Any] | Any):
        self.plugin_root = plugin_root
        self.raw_config = raw_config
        self.data_dir = resolve_plugin_data_dir(plugin_root)

        self.sub_bot_store = JsonStore(self.data_dir / "sub_bots.json")
        self._main_runtime: BridgeRuntime | None = None
        self._sub_runtimes: dict[str, BridgeRuntime] = {}
        self._webui_server: IndependentWebUIServer | None = None
        self._started = False
        self._lock = asyncio.Lock()

    @classmethod
    def from_plugin_root(cls, raw_config: dict[str, Any] | Any) -> "BridgeManager":
        plugin_root = Path(__file__).resolve().parent.parent
        return cls(plugin_root=plugin_root, raw_config=raw_config)

    async def start(self) -> None:
        self._started = True
        async with self._lock:
            await self._ensure_webui_state_locked()
            await self._reconcile_runtimes_locked("plugin start")

    async def stop(self) -> None:
        self._started = False
        async with self._lock:
            await self._stop_all_runtimes_locked()
            await self._stop_webui_locked()

    async def get_webui_state(self) -> dict[str, Any]:
        control = BridgeControlConfig.from_mapping(self.raw_config)
        items = await self.list_sub_bots()
        actual_port = (
            self._webui_server.port
            if self._webui_server is not None
            else control.independent_webui_port
        )
        return {
            "bridge_enabled": control.enabled,
            "main_bot_enabled": control.main_bot_enabled,
            "independent_webui_enabled": control.enable_independent_webui,
            "independent_webui_port": control.independent_webui_port,
            "independent_webui_actual_port": actual_port,
            "access_url": f"http://{DEFAULT_WEBUI_HOST}:{actual_port}/",
            "main_bot_onebot_self_id": self._get_main_bot_onebot_self_id(),
            "suggested_onebot_self_id": self._get_next_onebot_self_id(items),
            "items": items,
        }

    async def get_basic_info_state(self) -> dict[str, Any]:
        control = BridgeControlConfig.from_mapping(self.raw_config)
        async with self._lock:
            main_payload = BridgeConfig.runtime_payload_from_main_settings(self.raw_config)
            sub_bots = await self._read_sub_bots_locked()
            main_runtime = self._main_runtime
            sub_runtimes = dict(self._sub_runtimes)

        items: list[dict[str, Any]] = []
        main_config = BridgeConfig.from_mapping(main_payload)
        if main_config.enabled:
            items.append(
                await self._build_basic_info_item(
                    config=main_config,
                    runtime=main_runtime,
                    control=control,
                )
            )

        for item in sub_bots:
            config = BridgeConfig.from_mapping(item)
            if not config.enabled:
                continue
            items.append(
                await self._build_basic_info_item(
                    config=config,
                    runtime=sub_runtimes.get(config.bot_id),
                    control=control,
                )
            )

        items = [item for item in items if item is not None]
        items.sort(key=lambda item: (0 if item.get("is_main_bot") else 1, str(item.get("client_name") or "")))
        online_count = sum(1 for item in items if item.get("status_code") == "online")
        return {
            "items": items,
            "summary": {
                "enabled_count": len(items),
                "online_count": online_count,
            },
        }

    async def list_sub_bots(self) -> list[dict[str, Any]]:
        async with self._lock:
            items = await self._read_sub_bots_locked()
            return [dict(item) for item in items]

    async def create_sub_bot(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            items = await self._read_sub_bots_locked()
            candidate_payload = dict(payload or {})
            if self._should_fill_default_self_id(candidate_payload):
                candidate_payload["onebot_self_id"] = self._get_next_onebot_self_id(items)

            candidate = self._normalize_sub_bot_payload(candidate_payload)
            errors = self._validate_sub_bot(candidate, items, exclude_bot_id=None)
            if errors:
                raise ValueError("；".join(errors))
            items.append(candidate)
            await self._write_sub_bots_locked(items)
            await self._reconcile_runtimes_locked("副bot created")
            return dict(candidate)

    async def update_sub_bot(self, bot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            items = await self._read_sub_bots_locked()
            target_index = -1
            for index, item in enumerate(items):
                if str(item.get("id")) == bot_id:
                    target_index = index
                    break
            if target_index < 0:
                raise KeyError(bot_id)

            candidate = self._normalize_sub_bot_payload(payload, forced_id=bot_id)
            errors = self._validate_sub_bot(candidate, items, exclude_bot_id=bot_id)
            if errors:
                raise ValueError("；".join(errors))
            items[target_index] = candidate
            await self._write_sub_bots_locked(items)
            await self._reconcile_runtimes_locked("副bot updated")
            return dict(candidate)

    async def delete_sub_bot(self, bot_id: str) -> None:
        async with self._lock:
            items = await self._read_sub_bots_locked()
            filtered = [item for item in items if str(item.get("id")) != bot_id]
            if len(filtered) == len(items):
                raise KeyError(bot_id)
            await self._write_sub_bots_locked(filtered)
            await self._reconcile_runtimes_locked("副bot deleted")

    async def _ensure_webui_state_locked(self) -> None:
        control = BridgeControlConfig.from_mapping(self.raw_config)
        errors = control.validate()
        if errors:
            logger.error(f"[RocketChatOneBotBridge] 独立WebUI 配置校验失败: {'; '.join(errors)}")
            await self._stop_webui_locked()
            return

        if not control.enable_independent_webui:
            await self._stop_webui_locked()
            return

        if (
            self._webui_server is not None
            and self._webui_server.requested_port == control.independent_webui_port
        ):
            return

        await self._stop_webui_locked()
        server = IndependentWebUIServer(
            manager=self,
            host=DEFAULT_WEBUI_HOST,
            port=control.independent_webui_port,
            access_password=control.webui_access_password,
        )
        try:
            await server.start()
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] 启动独立WebUI失败: {exc!r}")
            self._webui_server = None
            return
        if server.port != control.independent_webui_port:
            logger.warning(
                "[RocketChatOneBotBridge] 独立WebUI请求端口 %s 不可用，已自动回退到 %s。",
                control.independent_webui_port,
                server.port,
            )
        self._webui_server = server

    async def _stop_webui_locked(self) -> None:
        if self._webui_server is None:
            return
        await self._webui_server.stop()
        self._webui_server = None

    async def _reconcile_runtimes_locked(self, reason: str) -> None:
        await self._stop_all_runtimes_locked()

        control = BridgeControlConfig.from_mapping(self.raw_config)
        reaction_config = control.reactions
        if not control.enabled:
            logger.info("[RocketChatOneBotBridge] 启用桥接总开关已关闭，所有 bot 保持停用。")
            return

        main_payload = BridgeConfig.runtime_payload_from_main_settings(self.raw_config)
        main_config = BridgeConfig.from_mapping(main_payload)
        if main_config.enabled:
            main_runtime = BridgeRuntime(
                plugin_root=self.plugin_root,
                raw_config=dict(main_payload),
                data_dir=self.data_dir,
                instance_name=main_config.display_name or "主bot",
                reaction_config=reaction_config,
                disable_callback=self._disable_main_bot_after_failure,
            )
            await main_runtime.start()
            if main_runtime.started:
                self._main_runtime = main_runtime

        if not control.enable_independent_webui:
            return

        sub_bots = await self._read_sub_bots_locked()
        for item in sub_bots:
            config = BridgeConfig.from_mapping(item)
            if not config.enabled:
                continue

            errors = self._validate_sub_bot(item, sub_bots, exclude_bot_id=config.bot_id)
            if errors:
                logger.error(
                    f"[RocketChatOneBotBridge] 副bot {config.display_name or config.bot_id} 配置校验失败: {'; '.join(errors)}"
                )
                continue

            runtime = BridgeRuntime(
                plugin_root=self.plugin_root,
                raw_config=dict(item),
                data_dir=self.data_dir / "sub_bots" / config.bot_id,
                instance_name=config.display_name or config.bot_id,
                reaction_config=reaction_config,
                disable_callback=lambda bot_id=config.bot_id: self._disable_sub_bot_after_failure(bot_id),
            )
            await runtime.start()
            if runtime.started:
                self._sub_runtimes[config.bot_id] = runtime

        logger.info(
            f"[RocketChatOneBotBridge] 运行时重建完成 | reason={reason} | sub_bots={len(self._sub_runtimes)}"
        )

    async def _stop_all_runtimes_locked(self) -> None:
        runtimes = []
        if self._main_runtime is not None:
            runtimes.append(self._main_runtime)
        runtimes.extend(self._sub_runtimes.values())

        self._main_runtime = None
        self._sub_runtimes = {}

        for runtime in runtimes:
            try:
                await runtime.stop()
            except Exception as exc:
                logger.warning(f"[RocketChatOneBotBridge] 停止 runtime 失败: {exc!r}")

    async def _disable_main_bot_after_failure(self) -> None:
        if hasattr(self.raw_config, "__setitem__"):
            self.raw_config["main_bot_enabled"] = False
        if hasattr(self.raw_config, "save_config"):
            self.raw_config.save_config()
        logger.error('[RocketChatOneBotBridge] 主bot 已因重连失败被自动关闭，请在设置页重新开启“主bot启用”。')

    async def _disable_sub_bot_after_failure(self, bot_id: str) -> None:
        async with self._lock:
            items = await self._read_sub_bots_locked()
            changed = False
            for item in items:
                if str(item.get("id")) == bot_id and bool(item.get("enabled", False)):
                    item["enabled"] = False
                    changed = True
                    break
            if changed:
                await self._write_sub_bots_locked(items)
                logger.error(f"[RocketChatOneBotBridge] 副bot {bot_id} 已因重连失败被自动关闭。")

    async def _read_sub_bots_locked(self) -> list[dict[str, Any]]:
        payload = await self.sub_bot_store.read({"bots": []})
        raw_items = payload.get("bots", [])
        if not isinstance(raw_items, list):
            raw_items = []

        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        changed = False
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                changed = True
                continue
            item = self._normalize_sub_bot_payload(raw_item)
            while item["id"] in seen_ids:
                item["id"] = self._generate_bot_id()
                changed = True
            seen_ids.add(item["id"])
            if item != raw_item:
                changed = True
            normalized.append(item)

        if changed:
            await self._write_sub_bots_locked(normalized)
        return normalized

    async def _write_sub_bots_locked(self, items: list[dict[str, Any]]) -> None:
        await self.sub_bot_store.write({"bots": items})

    def _normalize_sub_bot_payload(
        self,
        payload: dict[str, Any] | None,
        *,
        forced_id: str | None = None,
    ) -> dict[str, Any]:
        data = build_default_sub_bot_payload()
        data.update(dict(payload or {}))
        if forced_id is not None:
            data["id"] = forced_id
        data["type"] = "websocket-client"
        data["is_main_bot"] = False

        config = BridgeConfig.from_mapping(data)
        bot_id = forced_id or config.bot_id.strip() or self._generate_bot_id()
        display_name = config.display_name.strip() or DEFAULT_SUB_BOT_NAME

        normalized = config.to_mapping()
        normalized["id"] = bot_id
        normalized["name"] = display_name
        normalized["type"] = "websocket-client"
        normalized["is_main_bot"] = False
        return normalized

    def _validate_sub_bot(
        self,
        candidate: dict[str, Any],
        existing_items: list[dict[str, Any]],
        *,
        exclude_bot_id: str | None,
    ) -> list[str]:
        config = BridgeConfig.from_mapping(candidate)
        errors = config.validate()
        if config.transport_type != "websocket-client":
            errors.append("当前仅支持 websocket客户端 类型")

        if config.enabled:
            owner = self._find_self_id_owner(
                config.onebot_self_id,
                existing_items,
                exclude_bot_id=exclude_bot_id,
            )
            if owner is not None:
                errors.append(f"onebot_self_id {config.onebot_self_id} 已被{owner}占用")
        return errors

    def _find_self_id_owner(
        self,
        onebot_self_id: int,
        existing_items: list[dict[str, Any]],
        *,
        exclude_bot_id: str | None,
    ) -> str | None:
        main_self_id = self._get_main_bot_onebot_self_id()
        if exclude_bot_id != "main" and main_self_id == onebot_self_id:
                return "主bot"

        for item in existing_items:
            config = BridgeConfig.from_mapping(item)
            if config.bot_id == exclude_bot_id:
                continue
            if config.onebot_self_id == onebot_self_id:
                return config.display_name or config.bot_id or "另一个副bot"
        return None

    def _get_main_bot_onebot_self_id(self) -> int:
        main_config = BridgeConfig.from_mapping(self.raw_config)
        if main_config.onebot_self_id > 0:
            return main_config.onebot_self_id
        return 910001

    def _get_next_onebot_self_id(self, existing_items: list[dict[str, Any]]) -> int:
        max_self_id = self._get_main_bot_onebot_self_id()

        for item in existing_items:
            config = BridgeConfig.from_mapping(item)
            if config.onebot_self_id > max_self_id:
                max_self_id = config.onebot_self_id

        return max_self_id + 1

    def _should_fill_default_self_id(self, payload: dict[str, Any]) -> bool:
        if "onebot_self_id" not in payload:
            return True

        value = payload.get("onebot_self_id")
        if value is None:
            return True
        if isinstance(value, str) and not value.strip():
            return True

        try:
            return int(value) <= 0
        except (TypeError, ValueError):
            return True

    def _generate_bot_id(self) -> str:
        return secrets.token_hex(6)

    async def _build_basic_info_item(
        self,
        *,
        config: BridgeConfig,
        runtime: BridgeRuntime | None,
        control: BridgeControlConfig,
    ) -> dict[str, Any] | None:
        if not config.enabled:
            return None

        if runtime is not None:
            return await runtime.get_basic_info_summary()

        status_code = "pending"
        status_label = "未接入"
        if not control.enabled:
            status_code = "blocked"
            status_label = "受总开关禁用"

        client_name = "主bot" if config.is_main_bot else (config.display_name or config.bot_id or "副bot")
        username = str(config.username or "").strip()
        return {
            "bot_id": config.bot_id,
            "client_name": client_name,
            "login_username": username,
            "nickname": username or client_name,
            "avatar_url": self._guess_avatar_url(config.server_url, username),
            "status_code": status_code,
            "status_label": status_label,
            "server_url": config.server_url,
            "onebot_self_id": config.onebot_self_id,
            "server_display_name": "",
            "server_avatar_url": "",
            "is_main_bot": config.is_main_bot,
            "user_id": "",
        }

    def _guess_avatar_url(self, server_url: str, username: str) -> str:
        normalized_server = str(server_url or "").strip().rstrip("/")
        normalized_username = str(username or "").strip()
        if not normalized_server or not normalized_username:
            return ""
        return f"{normalized_server}/avatar/{quote(normalized_username, safe='')}"