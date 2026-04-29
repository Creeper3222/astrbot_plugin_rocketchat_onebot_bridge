from __future__ import annotations

import re
from typing import Any

from .id_map import DurableIdMap
from .rocketchat_client import RocketChatClient
from .storage import ContextRoomStore, MessageStore, PrivateRoomStore


class OutboundMessageTranslator:
    _TEXT_MENTION_PATTERN = re.compile(r"(?<!\S)@([A-Za-z0-9._-]+)")
    _RECENT_SELF_CONTEXT_ROOM_TTL_SECONDS = 120

    def __init__(
        self,
        rocketchat: RocketChatClient,
        id_map: DurableIdMap,
        messages: MessageStore,
        private_rooms: PrivateRoomStore,
        context_rooms: ContextRoomStore,
    ):
        self._rocketchat = rocketchat
        self._id_map = id_map
        self._messages = messages
        self._private_rooms = private_rooms
        self._context_rooms = context_rooms

    async def translate(
        self,
        message: Any,
        *,
        group_id: int | str | None = None,
        user_id: int | str | None = None,
    ) -> dict[str, Any]:
        segments = self._normalize_segments(message)
        normalized_segments: list[dict[str, Any]] = []
        reply_source_id: str | None = None
        mention_usernames: list[str] = []

        for segment in segments:
            segment_type = str(segment.get("type") or "text")
            data = segment.get("data", {}) or {}
            if segment_type == "text":
                text = self._normalize_text(str(data.get("text") or ""))
                if text:
                    mention_usernames.extend(self._extract_usernames_from_text(text))
                    normalized_segments.append({"type": "text", "data": {"text": text}})
                continue
            if segment_type == "at":
                mention_username = await self._resolve_mention_username(data)
                if mention_username:
                    mention_usernames.append(mention_username)
                mention_text = self._format_mention_text(mention_username)
                if mention_text:
                    normalized_segments.append({"type": "text", "data": {"text": mention_text}})
                continue
            if segment_type == "reply" and reply_source_id is None:
                entry = await self._messages.get_by_surrogate(data.get("id"))
                if entry and entry.get("source_id"):
                    reply_source_id = str(entry["source_id"])
                else:
                    reply_source_id = await self._id_map.get_source("message", data.get("id"))
                continue
            if segment_type in {"image", "file", "record", "video"}:
                normalized_segments.append({"type": segment_type, "data": dict(data)})
                continue
            if segment_type == "markdown":
                text = self._normalize_text(str(data.get("markdown") or data.get("content") or ""))
                if text:
                    normalized_segments.append({"type": "text", "data": {"text": text}})
                continue

        room_id = await self._resolve_room(
            group_id=group_id,
            user_id=user_id,
            reply_source_id=reply_source_id,
        )

        if group_id is not None:
            await self._refresh_context_room_binding(group_id, room_id)

        return {
            "room_id": room_id,
            "segments": normalized_segments,
            "reply_source_id": reply_source_id,
            "reply_mention_username": await self._resolve_reply_mention_username(reply_source_id)
            if reply_source_id
            else "",
            "mention_usernames": self._dedupe_preserve_order(mention_usernames),
        }

    async def _resolve_room(
        self,
        *,
        group_id: int | str | None,
        user_id: int | str | None,
        reply_source_id: str | None,
    ) -> str:
        if group_id is not None:
            room_id = await self._resolve_group_room(
                group_id=group_id,
                reply_source_id=reply_source_id,
            )
            if not room_id:
                raise ValueError(f"未知 group_id: {group_id}")
            return room_id

        if user_id is not None:
            room_id = await self._private_rooms.get_room_by_user_surrogate(user_id)
            if room_id:
                return room_id
            user_source_id = await self._id_map.get_source("user", user_id)
            if not user_source_id:
                raise ValueError(f"未知 user_id: {user_id}")
            room_id = await self._rocketchat.get_or_create_direct_room(user_source_id)
            surrogate = await self._id_map.get_or_create("user", user_source_id)
            await self._private_rooms.bind(user_source_id, surrogate.surrogate_id, room_id)
            return room_id

        raise ValueError("缺少 group_id 或 user_id")

    async def _resolve_group_room(
        self,
        *,
        group_id: int | str,
        reply_source_id: str | None,
    ) -> str | None:
        if reply_source_id:
            entry = await self._messages.get_by_source(reply_source_id)
            if entry and entry.get("room_source_id"):
                return str(entry["room_source_id"])

        room_id = await self._id_map.get_source("room", group_id)
        if room_id:
            return room_id

        context_entry = await self._context_rooms.get_by_context_surrogate(group_id)
        if context_entry:
            recent_self_room_id = await self._resolve_recent_self_context_room(context_entry)
            if recent_self_room_id:
                return recent_self_room_id
            if context_entry.get("room_source_id"):
                return str(context_entry["room_source_id"])
        return None

    async def _resolve_recent_self_context_room(self, context_entry: dict[str, Any]) -> str | None:
        context_source_id = str(context_entry.get("context_source_id") or "").strip()
        sender_source_id = str(self._rocketchat.user_id or "").strip()
        if not context_source_id or not sender_source_id:
            return None

        return await self._messages.get_latest_room_by_context_sender(
            context_source_id,
            sender_source_id,
            max_age_seconds=self._RECENT_SELF_CONTEXT_ROOM_TTL_SECONDS,
        )

    async def _refresh_context_room_binding(self, group_id: int | str, room_id: str) -> None:
        context_entry = await self._context_rooms.get_by_context_surrogate(group_id)
        if not isinstance(context_entry, dict):
            return

        context_source_id = str(context_entry.get("context_source_id") or "").strip()
        context_surrogate_id = context_entry.get("context_surrogate_id")
        if not context_source_id or context_surrogate_id is None:
            return

        room_mapping = await self._id_map.get_or_create("room", room_id)
        room_info = await self._rocketchat.get_room_info(room_id)
        room_name = str(room_info.get("fname") or room_info.get("name") or room_id)
        room_slug = str(room_info.get("name") or room_info.get("fname") or room_id)

        await self._context_rooms.bind(
            context_source_id=context_source_id,
            context_surrogate_id=context_surrogate_id,
            room_source_id=room_id,
            room_surrogate_id=room_mapping.surrogate_id,
            room_name=room_name,
            room_slug=room_slug,
        )

    def _normalize_segments(self, message: Any) -> list[dict[str, Any]]:
        if isinstance(message, str):
            return [{"type": "text", "data": {"text": message}}]
        if isinstance(message, list):
            return [segment for segment in message if isinstance(segment, dict)]
        return [{"type": "text", "data": {"text": str(message or "")}}]

    async def _resolve_mention_username(self, data: dict[str, Any]) -> str:
        qq = data.get("qq")
        if str(qq) == "all":
            return "all"
        if qq is None:
            return ""
        if str(qq) == str(self._rocketchat.config.onebot_self_id):
            username = self._rocketchat.bot_username or self._rocketchat.config.username
            return str(username or "")
        source_user_id = await self._id_map.get_source("user", qq)
        if source_user_id:
            user_info = await self._rocketchat.get_user_info(source_user_id)
            username = user_info.get("username") or user_info.get("name") or source_user_id
            return str(username)
        name = str(data.get("name") or "").strip()
        return name

    def _format_mention_text(self, mention_username: str) -> str:
        normalized = str(mention_username or "").strip()
        if not normalized:
            return ""
        if normalized == "all":
            return "@all "
        return f"@{normalized} "

    def _dedupe_preserve_order(self, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = str(value or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    async def _resolve_reply_mention_username(self, reply_source_id: str) -> str:
        entry = await self._messages.get_by_source(reply_source_id)
        if not isinstance(entry, dict):
            return ""

        sender_source_id = str(entry.get("sender_source_id") or "").strip()
        if sender_source_id:
            user_info = await self._rocketchat.get_user_info(sender_source_id)
            username = str(
                user_info.get("username")
                or user_info.get("name")
                or entry.get("sender_name")
                or ""
            ).strip()
            if username:
                return username

        return str(entry.get("sender_name") or "").strip()

    def _extract_usernames_from_text(self, text: str) -> list[str]:
        usernames: list[str] = []
        for match in self._TEXT_MENTION_PATTERN.finditer(text):
            username = str(match.group(1) or "").strip()
            if not username:
                continue
            usernames.append(username)
        return usernames

    def _normalize_text(self, text: str) -> str:
        cleaned = text.replace("\u200b", "")
        if not cleaned.strip():
            return ""
        return cleaned