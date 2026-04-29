from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_WEBUI_HOST = "127.0.0.1"
DEFAULT_INDEPENDENT_WEBUI_PORT = 5751
DEFAULT_MAIN_SERVER_URL = "http://127.0.0.1:3000"
DEFAULT_MAIN_ONEBOT_WS_URL = "ws://127.0.0.1:6199/ws/"
DEFAULT_MAIN_ONEBOT_SELF_ID = 910001
DEFAULT_SUB_BOT_NAME = "sub_bot"
DEFAULT_SUB_BOT_ONEBOT_WS_URL = "ws://127.0.0.1:6200/ws/"
DEFAULT_RECONNECT_DELAY = 5.0
DEFAULT_MAX_RECONNECT_ATTEMPTS = 10
DEFAULT_ENABLE_SUBCHANNEL_SESSION_ISOLATION = True
DEFAULT_REMOTE_MEDIA_MAX_SIZE = 20 * 1024 * 1024
DEFAULT_LLM_THINKING_REACTION = ":heart:"
DEFAULT_LLM_DONE_REACTION = ":sunny:"


def build_default_sub_bot_payload() -> dict[str, Any]:
    return {
        "name": DEFAULT_SUB_BOT_NAME,
        "enabled": False,
        "server_url": "",
        "username": "",
        "password": "",
        "e2ee_password": "",
        "onebot_ws_url": DEFAULT_SUB_BOT_ONEBOT_WS_URL,
        "onebot_access_token": "",
        "reconnect_delay": DEFAULT_RECONNECT_DELAY,
        "max_reconnect_attempts": DEFAULT_MAX_RECONNECT_ATTEMPTS,
        "enable_subchannel_session_isolation": DEFAULT_ENABLE_SUBCHANNEL_SESSION_ISOLATION,
        "remote_media_max_size": DEFAULT_REMOTE_MEDIA_MAX_SIZE,
        "skip_own_messages": True,
        "debug": False,
    }


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def _coerce_int(value: Any, default: int) -> int:
    try:
        if value is None:
            return int(default)
        if isinstance(value, str) and not value.strip():
            return int(default)
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _coerce_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return float(default)
        if isinstance(value, str) and not value.strip():
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _coerce_reaction_shortcode(value: Any, default: str) -> str:
    token = str(value or "").strip()
    if not token:
        token = default
    token = token.strip()
    if token.startswith(":") and token.endswith(":") and len(token) > 2:
        return token
    token = token.strip(":").strip()
    if not token:
        normalized_default = str(default or "").strip()
        return normalized_default if normalized_default else DEFAULT_LLM_THINKING_REACTION
    return f":{token}:"


@dataclass(slots=True)
class BridgeReactionConfig:
    llm_thinking_reaction: str
    llm_done_reaction: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "BridgeReactionConfig":
        data = dict(payload or {})
        return cls(
            llm_thinking_reaction=_coerce_reaction_shortcode(
                data.get("llm_thinking_reaction", DEFAULT_LLM_THINKING_REACTION),
                DEFAULT_LLM_THINKING_REACTION,
            ),
            llm_done_reaction=_coerce_reaction_shortcode(
                data.get("llm_done_reaction", DEFAULT_LLM_DONE_REACTION),
                DEFAULT_LLM_DONE_REACTION,
            ),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.llm_thinking_reaction:
            errors.append("llm_thinking_reaction 不能为空")
        if not self.llm_done_reaction:
            errors.append("llm_done_reaction 不能为空")
        return errors


@dataclass(slots=True)
class BridgeControlConfig:
    enabled: bool
    main_bot_enabled: bool
    enable_independent_webui: bool
    webui_access_password: str
    independent_webui_port: int
    reactions: BridgeReactionConfig

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "BridgeControlConfig":
        data = dict(payload or {})
        return cls(
            enabled=_coerce_bool(data.get("enabled", False)),
            main_bot_enabled=_coerce_bool(data.get("main_bot_enabled", False)),
            enable_independent_webui=_coerce_bool(data.get("enable_independent_webui", False)),
            webui_access_password=str(data.get("webui_access_password", "") or "").strip(),
            independent_webui_port=_coerce_int(
                data.get("independent_webui_port", DEFAULT_INDEPENDENT_WEBUI_PORT),
                DEFAULT_INDEPENDENT_WEBUI_PORT,
            ),
            reactions=BridgeReactionConfig.from_mapping(data),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.independent_webui_port <= 0 or self.independent_webui_port > 65535:
            errors.append("independent_webui_port 必须在 1 到 65535 之间")
        errors.extend(self.reactions.validate())
        return errors


@dataclass(slots=True)
class BridgeConfig:
    enabled: bool
    server_url: str
    username: str
    password: str
    e2ee_password: str
    onebot_ws_url: str
    onebot_access_token: str
    onebot_self_id: int
    reconnect_delay: float
    max_reconnect_attempts: int
    enable_subchannel_session_isolation: bool
    remote_media_max_size: int
    skip_own_messages: bool
    debug: bool
    bot_id: str = ""
    display_name: str = ""
    transport_type: str = "websocket-client"
    is_main_bot: bool = False

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "BridgeConfig":
        data = dict(payload or {})
        return cls(
            enabled=_coerce_bool(data.get("enabled", False)),
            server_url=str(data.get("server_url", DEFAULT_MAIN_SERVER_URL)).rstrip("/"),
            username=str(data.get("username", "")),
            password=str(data.get("password", "")),
            e2ee_password=str(data.get("e2ee_password", "")),
            onebot_ws_url=str(data.get("onebot_ws_url", DEFAULT_MAIN_ONEBOT_WS_URL)).strip(),
            onebot_access_token=str(data.get("onebot_access_token", "")),
            onebot_self_id=_coerce_int(data.get("onebot_self_id", DEFAULT_MAIN_ONEBOT_SELF_ID), DEFAULT_MAIN_ONEBOT_SELF_ID),
            reconnect_delay=_coerce_float(data.get("reconnect_delay", DEFAULT_RECONNECT_DELAY), DEFAULT_RECONNECT_DELAY),
            max_reconnect_attempts=_coerce_int(data.get("max_reconnect_attempts", DEFAULT_MAX_RECONNECT_ATTEMPTS), DEFAULT_MAX_RECONNECT_ATTEMPTS),
            enable_subchannel_session_isolation=_coerce_bool(data.get("enable_subchannel_session_isolation", DEFAULT_ENABLE_SUBCHANNEL_SESSION_ISOLATION)),
            remote_media_max_size=_coerce_int(
                data.get("remote_media_max_size", DEFAULT_REMOTE_MEDIA_MAX_SIZE),
                DEFAULT_REMOTE_MEDIA_MAX_SIZE,
            ),
            skip_own_messages=_coerce_bool(data.get("skip_own_messages", True)),
            debug=_coerce_bool(data.get("debug", False)),
            bot_id=str(data.get("id") or data.get("bot_id") or ""),
            display_name=str(data.get("name") or data.get("display_name") or ""),
            transport_type=str(data.get("type") or data.get("transport_type") or "websocket-client"),
            is_main_bot=_coerce_bool(data.get("is_main_bot", False)),
        )

    @classmethod
    def runtime_payload_from_main_settings(
        cls,
        payload: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        control = BridgeControlConfig.from_mapping(payload)
        data = dict(payload or {})
        data["enabled"] = control.enabled and control.main_bot_enabled
        data["id"] = "main"
        data["name"] = "主bot"
        data["type"] = "websocket-client"
        data["is_main_bot"] = True
        return cls.from_mapping(data).to_mapping()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "server_url": self.server_url,
            "username": self.username,
            "password": self.password,
            "e2ee_password": self.e2ee_password,
            "onebot_ws_url": self.onebot_ws_url,
            "onebot_access_token": self.onebot_access_token,
            "onebot_self_id": self.onebot_self_id,
            "reconnect_delay": self.reconnect_delay,
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "enable_subchannel_session_isolation": self.enable_subchannel_session_isolation,
            "remote_media_max_size": self.remote_media_max_size,
            "skip_own_messages": self.skip_own_messages,
            "debug": self.debug,
            "id": self.bot_id,
            "name": self.display_name,
            "type": self.transport_type,
            "is_main_bot": self.is_main_bot,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.enabled:
            if not self.server_url.startswith(("http://", "https://")):
                errors.append("server_url 必须以 http:// 或 https:// 开头")
            if not self.onebot_ws_url.startswith(("ws://", "wss://")):
                errors.append("onebot_ws_url 必须以 ws:// 或 wss:// 开头")
            if not self.username:
                errors.append("enabled=true 时 username 不能为空")
            if not self.password:
                errors.append("enabled=true 时 password 不能为空")
            if self.onebot_self_id <= 0:
                errors.append("onebot_self_id 必须为正整数")
        if self.reconnect_delay < 0:
            errors.append("reconnect_delay 不能小于 0")
        if self.max_reconnect_attempts < 0:
            errors.append("max_reconnect_attempts 不能小于 0")
        if self.remote_media_max_size < 0:
            errors.append("remote_media_max_size 不能小于 0")
        return errors