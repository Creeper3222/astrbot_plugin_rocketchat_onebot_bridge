from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from astrbot.api import logger

from .id_map import DurableIdMap
from .media import summarize_unsupported_media
from .rocketchat_client import RocketChatClient
from .storage import ContextRoomStore, MessageStore, PrivateRoomStore


class InboundTranslator:
    _QUOTE_PATTERN = re.compile(r"\[[^\]]*\]\(([^)]*msg=[^)]*)\)|(https?://\S*msg=\S+)", re.IGNORECASE)
    _MAX_QUOTE_DEPTH = 2

    def __init__(
        self,
        rocketchat: RocketChatClient,
        id_map: DurableIdMap,
        messages: MessageStore,
        private_rooms: PrivateRoomStore,
        context_rooms: ContextRoomStore,
        self_id: int,
    ):
        self._rocketchat = rocketchat
        self._id_map = id_map
        self._messages = messages
        self._private_rooms = private_rooms
        self._context_rooms = context_rooms
        self._self_id = self_id

    async def translate(self, raw_msg: dict) -> dict | None:
        room_id = str(raw_msg.get("rid") or "")
        sender = raw_msg.get("u", {}) or {}
        sender_source_id = str(sender.get("_id") or "")
        source_message_id = str(raw_msg.get("_id") or "")
        if not room_id or not sender_source_id or not source_message_id:
            return None

        room_type = await self._rocketchat.get_room_type(room_id)
        room_info = await self._rocketchat.get_room_info(room_id)
        room_mapping = await self._id_map.get_or_create("room", room_id)
        sender_mapping = await self._id_map.get_or_create("user", sender_source_id)
        message_mapping = await self._id_map.get_or_create("message", source_message_id)
        context_source_id = self._build_group_context_source_id(room_type, room_id)
        context_surrogate_id = await self._resolve_context_surrogate_id(
            room_type=room_type,
            room_mapping=room_mapping,
            context_source_id=context_source_id,
        )
        room_name = self._resolve_room_display_name(room_info, room_id)
        room_slug = self._resolve_room_slug(room_info, room_id)
        room_context_label = self._format_room_context_label(room_type, room_name)
        sender_name = sender.get("name") or sender.get("username") or sender_source_id
        thread_source_id = str(raw_msg.get("tmid") or "").strip()
        timestamp = self._extract_timestamp(raw_msg)

        if room_type == "d":
            await self._private_rooms.bind(sender_source_id, sender_mapping.surrogate_id, room_id)
        elif context_surrogate_id is not None:
            await self._context_rooms.bind(
                context_source_id=context_source_id,
                context_surrogate_id=context_surrogate_id,
                room_source_id=room_id,
                room_surrogate_id=room_mapping.surrogate_id,
                room_name=room_name,
                room_slug=room_slug,
                thread_source_id=thread_source_id,
                timestamp=timestamp,
            )

        reply_source_id, cleaned_text = self._extract_reply_source_id(raw_msg)
        current_input_text = cleaned_text.strip()
        message_segments, cleaned_text = await self._build_mention_segments(raw_msg, cleaned_text)
        quote_contexts = await self._build_quote_contexts(raw_msg, max_depth=self._MAX_QUOTE_DEPTH)
        mention_display_names = self._extract_mention_display_names(raw_msg)
        mention_metadata = await self._extract_mention_metadata(raw_msg)
        media_segments = await self._rocketchat.media.extract_onebot_segments(raw_msg)
        quote_media_segments = self._build_quote_media_segments(
            quote_contexts,
            max_depth=self._MAX_QUOTE_DEPTH,
        )
        current_media = await self._extract_context_media_descriptors(raw_msg)
        quote_context_block = self._format_quote_context_block(quote_contexts)

        segments: list[dict] = []
        if reply_source_id:
            reply_mapping = await self._id_map.get_or_create("message", reply_source_id)
            segments.append({"type": "reply", "data": {"id": str(reply_mapping.surrogate_id)}})
        segments.extend(message_segments)

        message_text = cleaned_text.strip()
        current_message_line = self._format_current_message_line(
            room_context_label=room_context_label,
            sender_name=sender_name,
            message_text=message_text,
            mention_names=mention_display_names,
            media=current_media,
        )

        segments.extend(quote_media_segments)
        segments.extend(media_segments)

        if not segments:
            media_placeholder = summarize_unsupported_media(raw_msg)
            if media_placeholder:
                segments.append({"type": "text", "data": {"text": media_placeholder}})
                message_text = media_placeholder
                current_input_text = media_placeholder

        if not segments:
            return None

        timestamp = self._extract_timestamp(raw_msg)
        direct_reply_context = quote_contexts[0] if quote_contexts else {}
        combined_raw_message = self._compose_raw_message(
            current_message_text=current_input_text,
            fallback=message_text,
        )
        event = {
            "time": timestamp,
            "self_id": self._self_id,
            "post_type": "message",
            "message_type": "private" if room_type == "d" else "group",
            "sub_type": "friend" if room_type == "d" else "normal",
            "message_id": message_mapping.surrogate_id,
            "user_id": sender_mapping.surrogate_id,
            "message": segments,
            "raw_message": combined_raw_message,
            "font": 0,
            "sender": {
                "user_id": sender_mapping.surrogate_id,
                "nickname": sender_name,
                "card": sender_name,
            },
            "message_format": "array",
            "rocketchat_sender_name": sender_name,
            "rocketchat_sender_username": str(sender.get("username") or ""),
            "rocketchat_sender_source_id": sender_source_id,
            "rocketchat_sender_surrogate_id": sender_mapping.surrogate_id,
            "rocketchat_mentions": mention_metadata,
            "rocketchat_quote_contexts": quote_contexts,
            "rocketchat_quote_context_text": quote_context_block,
            "rocketchat_current_message_input_text": current_input_text,
            "rocketchat_quote_media_segments": quote_media_segments,
            "rocketchat_current_message_text": message_text,
            "rocketchat_current_message_line": current_message_line,
            "rocketchat_reply_source_id": reply_source_id,
            "rocketchat_reply_sender_name": str(direct_reply_context.get("sender_name") or ""),
            "rocketchat_reply_message_text": str(direct_reply_context.get("text") or ""),
            "rocketchat_room_source_id": room_id,
            "rocketchat_room_name": room_name,
            "rocketchat_room_slug": room_slug,
            "rocketchat_room_label": room_context_label,
            "rocketchat_room_surrogate_id": room_mapping.surrogate_id,
            "rocketchat_context_source_id": context_source_id,
            "rocketchat_context_group_id": context_surrogate_id,
            "rocketchat_thread_source_id": thread_source_id,
        }

        if room_type != "d":
            event["group_id"] = context_surrogate_id if context_surrogate_id is not None else room_mapping.surrogate_id
            event["group_name"] = room_name

        if quote_contexts:
            logger.info(
                self._format_inbound_quote_log(
                    room_context_label=room_context_label,
                    sender_name=sender_name,
                    sender_surrogate_id=sender_mapping.surrogate_id,
                    current_message_line=current_message_line,
                    quote_contexts=quote_contexts,
                )
            )
        else:
            logger.info(
                self._format_inbound_message_log(
                    room_context_label=room_context_label,
                    sender_name=sender_name,
                    sender_surrogate_id=sender_mapping.surrogate_id,
                    current_message_line=current_message_line,
                )
            )

        await self._messages.put(
            {
                "source_id": source_message_id,
                "surrogate_id": message_mapping.surrogate_id,
                "room_source_id": room_id,
                "room_surrogate_id": room_mapping.surrogate_id,
                "room_type": room_type,
                "room_name": room_name,
                "room_slug": room_slug,
                "context_source_id": context_source_id,
                "context_surrogate_id": context_surrogate_id,
                "sender_source_id": sender_source_id,
                "sender_surrogate_id": sender_mapping.surrogate_id,
                "sender_name": sender_name,
                "sender_username": str(sender.get("username") or ""),
                "mention_metadata": mention_metadata,
                "input_text": current_input_text,
                "text": message_text,
                "quote_contexts": quote_contexts,
                "quote_context_text": quote_context_block,
                "reply_sender_name": str(direct_reply_context.get("sender_name") or ""),
                "reply_message_text": str(direct_reply_context.get("text") or ""),
                "timestamp": timestamp,
                "onebot_message": event,
                "thread_source_id": thread_source_id,
            }
        )
        return event

    async def hydrate(self, surrogate_message_id: int | str) -> dict | None:
        cached = await self._messages.get_by_surrogate(surrogate_message_id)
        cached_event = cached.get("onebot_message") if isinstance(cached, dict) else None
        source_id = str(cached.get("source_id") or "") if isinstance(cached, dict) else ""
        if cached_event and not self._should_refresh_cached_reply_message(cached):
            return cached_event

        if not source_id:
            resolved_source_id = await self._id_map.get_source("message", surrogate_message_id)
            source_id = str(resolved_source_id or "")
        if not source_id:
            return cached_event
        raw_msg = await self._rocketchat.fetch_message_by_id(source_id)
        if not raw_msg:
            return cached_event
        return await self.translate(raw_msg)

    def _should_refresh_cached_reply_message(self, cached: dict[str, Any] | None) -> bool:
        if not isinstance(cached, dict):
            return False
        if cached.get("quote_contexts"):
            return True
        event = cached.get("onebot_message")
        if not isinstance(event, dict):
            return False
        return bool(event.get("rocketchat_reply_source_id"))

    async def _build_mention_segments(self, raw_msg: dict, text: str) -> tuple[list[dict], str]:
        mentions = raw_msg.get("mentions")
        if not isinstance(mentions, list) or not mentions:
            stripped = text.strip()
            return self._build_text_segments(stripped), stripped

        segments: list[dict] = []
        text_parts: list[str] = []
        unmatched_segments: list[dict] = []
        cursor = 0

        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mention_id = mention.get("_id")
            if not mention_id:
                continue

            mention_segment = await self._build_mention_segment(mention)
            if mention_segment is None:
                continue

            username = mention.get("username")
            token = f"@{username}" if username else ""
            if not token:
                unmatched_segments.append(mention_segment)
                continue

            position = text.find(token, cursor)
            if position < 0:
                unmatched_segments.append(mention_segment)
                continue

            if position > cursor:
                chunk = text[cursor:position]
                text_parts.append(chunk)
                self._append_text_segment(segments, chunk)

            segments.append(mention_segment)
            cursor = position + len(token)

        if cursor < len(text):
            chunk = text[cursor:]
            text_parts.append(chunk)
            self._append_text_segment(segments, chunk)

        segments.extend(unmatched_segments)
        return segments, "".join(text_parts).strip()

    async def _build_mention_segment(self, mention: dict[str, Any]) -> dict[str, Any] | None:
        mention_id = mention.get("_id")
        if not mention_id:
            return None

        username = mention.get("username")
        name = mention.get("name") or username or str(mention_id)
        if str(mention_id) == str(self._rocketchat.user_id):
            mention_qq = str(self._self_id)
        else:
            mapping = await self._id_map.get_or_create("user", str(mention_id))
            mention_qq = str(mapping.surrogate_id)
        return {"type": "at", "data": {"qq": mention_qq, "name": name}}

    def _append_text_segment(self, segments: list[dict[str, Any]], text: str) -> None:
        if not text or not text.strip():
            return
        segments.append({"type": "text", "data": {"text": text}})

    def _build_text_segments(self, text: str) -> list[dict[str, Any]]:
        if not text:
            return []
        return [{"type": "text", "data": {"text": text}}]

    def _extract_reply_source_id(self, raw_msg: dict) -> tuple[str | None, str]:
        text = str(raw_msg.get("msg") or "")
        urls = raw_msg.get("urls")
        if isinstance(urls, list):
            for url_obj in urls:
                if not isinstance(url_obj, dict):
                    continue
                parsed_url = url_obj.get("parsedUrl", {})
                if isinstance(parsed_url, dict):
                    query = parsed_url.get("query", {})
                    if isinstance(query, dict) and query.get("msg"):
                        value = query.get("msg")
                        if isinstance(value, list):
                            value = value[0] if value else None
                        if value:
                            return str(value), self._QUOTE_PATTERN.sub("", text).strip()
                candidate = self._extract_message_id_from_url(str(url_obj.get("url") or ""))
                if candidate:
                    return candidate, self._QUOTE_PATTERN.sub("", text).strip()

        attachments = raw_msg.get("attachments")
        if isinstance(attachments, dict):
            attachments = [attachments]
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                candidate = self._extract_message_id_from_url(str(attachment.get("message_link") or ""))
                if candidate:
                    return candidate, self._QUOTE_PATTERN.sub("", text).strip()

        for match in self._QUOTE_PATTERN.finditer(text):
            candidate = self._extract_message_id_from_url(match.group(1) or match.group(2) or "")
            if candidate:
                return candidate, self._QUOTE_PATTERN.sub("", text).strip()

        return None, text

    async def _build_quote_contexts(
        self,
        raw_msg: dict[str, Any],
        *,
        max_depth: int,
    ) -> list[dict[str, Any]]:
        contexts: list[dict[str, Any]] = []
        visited: set[str] = set()
        await self._collect_quote_contexts_from_payload(
            raw_msg,
            contexts=contexts,
            depth=1,
            max_depth=max_depth,
            visited=visited,
        )
        if contexts:
            return contexts

        reply_source_id, _ = self._extract_reply_source_id(raw_msg)
        if not reply_source_id:
            return contexts

        await self._collect_quote_contexts_from_message_id(
            reply_source_id,
            contexts=contexts,
            depth=1,
            max_depth=max_depth,
            visited=visited,
        )
        return contexts

    async def _collect_quote_contexts_from_payload(
        self,
        payload: dict[str, Any],
        *,
        contexts: list[dict[str, Any]],
        depth: int,
        max_depth: int,
        visited: set[str],
    ) -> None:
        if depth > max_depth:
            return

        attachments = payload.get("attachments")
        if isinstance(attachments, dict):
            attachments = [attachments]
        if not isinstance(attachments, list):
            return

        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            source_id = self._extract_message_id_from_url(str(attachment.get("message_link") or ""))
            if not source_id or source_id in visited:
                continue

            visited.add(source_id)
            contexts.append(await self._build_quote_context_entry(attachment, source_id, depth))
            await self._collect_quote_contexts_from_payload(
                attachment,
                contexts=contexts,
                depth=depth + 1,
                max_depth=max_depth,
                visited=visited,
            )

    async def _collect_quote_contexts_from_message_id(
        self,
        source_id: str,
        *,
        contexts: list[dict[str, Any]],
        depth: int,
        max_depth: int,
        visited: set[str],
    ) -> None:
        if depth > max_depth or source_id in visited:
            return

        raw_msg = await self._rocketchat.fetch_message_by_id(source_id)
        if not raw_msg:
            return

        visited.add(source_id)
        contexts.append(await self._build_quote_context_entry(raw_msg, source_id, depth))
        await self._collect_quote_contexts_from_payload(
            raw_msg,
            contexts=contexts,
            depth=depth + 1,
            max_depth=max_depth,
            visited=visited,
        )

    async def _build_quote_context_entry(
        self,
        payload: dict[str, Any],
        source_id: str,
        depth: int,
    ) -> dict[str, Any]:
        return {
            "depth": depth,
            "source_id": source_id,
            "sender_name": self._extract_context_sender_name(payload),
            "text": self._clean_quote_text(payload),
            "media": await self._extract_context_media_descriptors(payload),
        }

    def _build_quote_media_segments(
        self,
        quote_contexts: list[dict[str, Any]],
        *,
        max_depth: int,
    ) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for context in quote_contexts:
            depth = int(context.get("depth") or 0)
            if depth <= 0 or depth > max_depth:
                continue
            for media in context.get("media") or []:
                segment = self._build_media_segment_from_descriptor(media)
                if not segment:
                    continue
                data = segment.get("data") or {}
                file_ref = str(data.get("file") or data.get("url") or "")
                key = (str(segment.get("type") or ""), file_ref)
                if not file_ref or key in seen:
                    continue
                seen.add(key)
                segments.append(segment)

        return segments

    def _build_media_segment_from_descriptor(self, media: dict[str, Any]) -> dict[str, Any] | None:
        kind = str(media.get("kind") or "")
        file_ref = str(media.get("path") or media.get("url") or "")
        if not file_ref:
            return None

        if kind == "image":
            return {"type": "image", "data": {"file": file_ref}}
        if kind == "audio":
            return {"type": "record", "data": {"file": file_ref}}
        if kind == "video":
            return {"type": "video", "data": {"file": file_ref}}

        name = str(media.get("name") or "attachment")
        if media.get("path"):
            return {
                "type": "text",
                "data": {
                    "text": f"[加密文件] {name}",
                },
            }
        return {
            "type": "file",
            "data": {
                "url": file_ref,
                "file_name": name,
                "name": name,
            },
        }

    async def _extract_context_media_descriptors(
        self,
        payload: dict[str, Any],
    ) -> list[dict[str, str]]:
        media: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        candidates: list[dict[str, Any]] = []
        media_shaped_keys = (
            "type",
            "mimeType",
            "contentType",
            "image_url",
            "imageUrl",
            "audio_url",
            "audioUrl",
            "video_url",
            "videoUrl",
            "title_link",
            "titleLink",
            "url",
            "path",
            "link",
        )

        def collect_candidates(source: dict[str, Any]) -> None:
            files_raw = source.get("files", [])
            if isinstance(files_raw, dict):
                candidates.append(files_raw)
            elif isinstance(files_raw, list):
                candidates.extend([item for item in files_raw if isinstance(item, dict)])

            for key in ("file", "fileUpload"):
                single_file = source.get(key)
                if isinstance(single_file, dict):
                    candidates.append(single_file)

            if any(source.get(key) for key in media_shaped_keys):
                candidates.append(source)

        collect_candidates(payload)
        for attachment in self._rocketchat.media.get_all_attachments_recursive(
            payload,
            skip_quote_attachments=True,
        ):
            collect_candidates(attachment)

        for candidate in candidates:
            kind = self._rocketchat.media.classify_file_kind(candidate)
            materialized = await self._rocketchat.media._materialize_media_reference(candidate, kind)
            if not materialized:
                continue
            file_ref = str(materialized.get("path") or materialized.get("url") or "")
            if not file_ref:
                continue
            key = (kind, file_ref)
            if key in seen:
                continue
            seen.add(key)
            media.append(
                {
                    "kind": kind,
                    "name": str(materialized.get("name") or self._extract_context_media_name(candidate, file_ref)),
                    "url": str(materialized.get("url") or ""),
                    "path": str(materialized.get("path") or ""),
                }
            )

        return media

    def _extract_context_media_name(self, payload: dict[str, Any], media_url: str) -> str:
        return str(
            payload.get("name")
            or payload.get("title")
            or payload.get("file_name")
            or os.path.basename(urlparse(media_url).path)
            or "attachment"
        )

    def _extract_context_sender_name(self, payload: dict[str, Any]) -> str:
        sender = payload.get("u") if isinstance(payload.get("u"), dict) else {}
        return str(
            payload.get("author_name")
            or sender.get("name")
            or sender.get("username")
            or sender.get("_id")
            or "未知"
        )

    def _clean_quote_text(self, payload: dict[str, Any]) -> str:
        text = str(payload.get("text") or payload.get("msg") or "")
        return self._QUOTE_PATTERN.sub("", text).strip()

    def _extract_mention_display_names(self, raw_msg: dict[str, Any]) -> list[str]:
        mentions = raw_msg.get("mentions")
        if not isinstance(mentions, list):
            return []
        result: list[str] = []
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            name = mention.get("name") or mention.get("username") or mention.get("_id")
            if name:
                result.append(str(name))
        return result

    async def _extract_mention_metadata(self, raw_msg: dict[str, Any]) -> list[dict[str, Any]]:
        mentions = raw_msg.get("mentions")
        if not isinstance(mentions, list):
            return []

        metadata: list[dict[str, Any]] = []
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mention_id = mention.get("_id")
            if not mention_id:
                continue

            segment = await self._build_mention_segment(mention)
            if segment is None:
                continue

            data = segment.get("data") or {}
            metadata.append(
                {
                    "source_id": str(mention_id),
                    "username": str(mention.get("username") or ""),
                    "name": str(mention.get("name") or data.get("name") or mention_id),
                    "qq": str(data.get("qq") or ""),
                }
            )
        return metadata

    def _format_quote_context_block(self, quote_contexts: list[dict[str, Any]]) -> str:
        if not quote_contexts:
            return ""
        return "引用历史上下文：[\n" + self._format_quote_context_lines(quote_contexts) + "\n]"

    def _format_inbound_quote_log(
        self,
        *,
        room_context_label: str,
        sender_name: str,
        sender_surrogate_id: int | str,
        current_message_line: str,
        quote_contexts: list[dict[str, Any]],
    ) -> str:
        lines = [
            "[RocketChatOneBotBridge] 收到 Rocket.Chat 引用消息",
        ]

        if room_context_label:
            lines.append(f"来源房间：{room_context_label}")

        lines.extend(
            [
                f"当前消息：{current_message_line}",
                f"发送者映射：{sender_name}/{sender_surrogate_id}",
            ]
        )

        quote_chain = self._format_quote_chain_summary(sender_name, quote_contexts)
        if quote_chain:
            lines.append(f"引用链：{quote_chain}")

        lines.append("引用历史上下文：")
        lines.extend(self._format_quote_context_log_lines(quote_contexts))
        return "\n".join(lines)

    def _format_inbound_message_log(
        self,
        *,
        room_context_label: str,
        sender_name: str,
        sender_surrogate_id: int | str,
        current_message_line: str,
    ) -> str:
        parts = ["[RocketChatOneBotBridge] 收到 Rocket.Chat 消息"]
        if room_context_label:
            parts.append(f"room={room_context_label}")
        parts.append(f"sender={sender_name}/{sender_surrogate_id}")
        parts.append(f"message={current_message_line}")
        return " | ".join(parts)

    def _format_quote_context_log_payload(self, quote_contexts: list[dict[str, Any]]) -> str:
        return self._format_quote_context_block(quote_contexts)

    def _format_quote_chain_summary(
        self,
        current_sender_name: str,
        quote_contexts: list[dict[str, Any]],
    ) -> str:
        chain = [f"{current_sender_name}(当前消息)"]
        for index, context in enumerate(quote_contexts, start=1):
            depth = int(context.get("depth") or index)
            sender_name = str(context.get("sender_name") or "未知")
            chain.append(f"{sender_name}(第{depth}层)")
        return " -> ".join(chain)

    def _format_quote_context_log_lines(self, quote_contexts: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for index, context in enumerate(quote_contexts, start=1):
            depth = int(context.get("depth") or index)
            lines.append(f"  {index}. 第{depth}层：{self._format_context_message_line(context)}")
        return lines

    def _format_quote_context_lines(self, quote_contexts: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for index, context in enumerate(quote_contexts):
            suffix = "  引用回复：" if index + 1 < len(quote_contexts) else ""
            lines.append(f"    {self._format_context_message_line(context)}{suffix}")

        return "\n".join(lines)

    def _format_context_message_line(self, context: dict[str, Any]) -> str:
        sender_name = str(context.get("sender_name") or "未知")
        content = self._format_message_content(
            message_text=str(context.get("text") or ""),
            media=context.get("media") or [],
        )
        return f"{sender_name}：{content}"

    def _format_current_message_line(
        self,
        *,
        room_context_label: str,
        sender_name: str,
        message_text: str,
        mention_names: list[str],
        media: list[dict[str, str]],
    ) -> str:
        content = self._format_message_content(
            message_text=message_text,
            mention_names=mention_names,
            media=media,
        )
        if room_context_label:
            sender_prefix = sender_name or "未知"
            return f"{room_context_label}：{sender_prefix}：{content}"
        return content

    def _format_message_content(
        self,
        *,
        message_text: str,
        mention_names: list[str] | None = None,
        media: list[dict[str, str]] | None = None,
    ) -> str:
        text = message_text.strip()
        mention_prefix = " ".join(f"@{name}" for name in (mention_names or []) if name)
        if mention_prefix:
            text = " ".join(part for part in (mention_prefix.strip(), text) if part)

        media_text = self._format_media_brief(media or [])
        if text and media_text:
            return f"{text} {media_text}"
        if text:
            return text
        if media_text:
            return media_text
        return "(无纯文本，仅引用上文)"

    def _format_media_brief(self, media: list[dict[str, str]]) -> str:
        parts: list[str] = []
        for item in media:
            kind = str(item.get("kind") or "file")
            name = str(item.get("name") or "attachment")
            if kind == "image":
                parts.append(f"[图片:{name}]")
            elif kind == "audio":
                parts.append(f"[语音:{name}]")
            elif kind == "video":
                parts.append(f"[视频:{name}]")
            else:
                parts.append(f"[文件:{name}]")
        return " ".join(parts)

    def _compose_raw_message(
        self,
        *,
        current_message_text: str,
        fallback: str,
    ) -> str:
        return current_message_text or fallback

    async def _resolve_context_surrogate_id(
        self,
        *,
        room_type: str,
        room_mapping: Any,
        context_source_id: str,
    ) -> int | None:
        if room_type == "d" or not context_source_id:
            return None
        if getattr(self._rocketchat.config, "enable_subchannel_session_isolation", False):
            return int(room_mapping.surrogate_id)
        context_mapping = await self._id_map.get_or_create("context", context_source_id)
        return None if context_mapping is None else int(context_mapping.surrogate_id)

    def _build_group_context_source_id(self, room_type: str, room_id: str) -> str:
        if room_type == "d":
            return ""
        server_url = str(getattr(self._rocketchat.config, "server_url", "") or "").rstrip("/")
        if getattr(self._rocketchat.config, "enable_subchannel_session_isolation", False):
            return f"rocketchat-room-context::{server_url or 'default'}::{room_id}"
        return f"rocketchat-group-context::{server_url or 'default'}"

    def _resolve_room_display_name(self, room_info: dict[str, Any], room_id: str) -> str:
        return str(room_info.get("fname") or room_info.get("name") or room_id)

    def _resolve_room_slug(self, room_info: dict[str, Any], room_id: str) -> str:
        return str(room_info.get("name") or room_info.get("fname") or room_id)

    def _format_room_context_label(self, room_type: str, room_name: str) -> str:
        if room_type == "d" or not room_name:
            return ""
        return f"子频道：[{room_name}]"

    def _extract_message_id_from_url(self, url: str) -> str | None:
        if not url or "msg=" not in url:
            return None
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        values = query.get("msg")
        if not values:
            return None
        return str(values[0])

    def _extract_timestamp(self, raw_msg: dict) -> int:
        ts = raw_msg.get("ts")
        if isinstance(ts, dict) and "$date" in ts:
            return int(int(ts["$date"]) / 1000)
        return int(time.time())