from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import time
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def read(self, default: Any) -> Any:
        async with self._lock:
            if not self.file_path.exists():
                return default
            with self.file_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)

    async def write(self, payload: Any) -> None:
        async with self._lock:
            with self.file_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)

    async def mutate(self, default: Any, mutator):
        async with self._lock:
            if self.file_path.exists():
                with self.file_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            else:
                payload = default
            result = mutator(payload)
            with self.file_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            return result


class MessageStore:
    def __init__(self, store: JsonStore):
        self._store = store

    @staticmethod
    def empty_payload() -> dict[str, Any]:
        return {"by_source": {}, "by_surrogate": {}, "latest_by_context_sender": {}}

    async def put(self, entry: dict[str, Any]) -> None:
        source_id = str(entry["source_id"])
        surrogate_id = str(entry["surrogate_id"])
        context_source_id = str(entry.get("context_source_id") or "").strip()
        sender_source_id = str(entry.get("sender_source_id") or "").strip()
        room_source_id = str(entry.get("room_source_id") or "").strip()
        timestamp = int(entry.get("timestamp") or 0)

        def mutate(payload: dict[str, Any]) -> None:
            payload.setdefault("by_source", {})[source_id] = entry
            payload.setdefault("by_surrogate", {})[surrogate_id] = entry
            if context_source_id and sender_source_id and room_source_id:
                context_bucket = payload.setdefault("latest_by_context_sender", {}).setdefault(
                    context_source_id,
                    {},
                )
                existing = context_bucket.get(sender_source_id)
                existing_timestamp = int(existing.get("timestamp") or 0) if isinstance(existing, dict) else 0
                if timestamp >= existing_timestamp:
                    context_bucket[sender_source_id] = {
                        "source_id": source_id,
                        "room_source_id": room_source_id,
                        "timestamp": timestamp,
                    }
            return None

        await self._store.mutate(self.empty_payload(), mutate)

    async def get_by_source(self, source_id: str) -> dict[str, Any] | None:
        payload = await self._store.read(self.empty_payload())
        return payload.get("by_source", {}).get(str(source_id))

    async def get_by_surrogate(self, surrogate_id: int | str) -> dict[str, Any] | None:
        payload = await self._store.read(self.empty_payload())
        return payload.get("by_surrogate", {}).get(str(surrogate_id))

    async def rebuild_for_active_mappings(self, active_mappings: dict[str, int]) -> None:
        normalized_mappings = {
            str(source_id): int(surrogate_id)
            for source_id, surrogate_id in (active_mappings or {}).items()
        }

        def mutate(payload: dict[str, Any]) -> None:
            by_source = payload.get("by_source", {})
            retained_entries: list[dict[str, Any]] = []
            new_by_source: dict[str, Any] = {}
            new_by_surrogate: dict[str, Any] = {}

            for source_id, surrogate_id in sorted(normalized_mappings.items(), key=lambda item: item[1]):
                existing_entry = by_source.get(source_id)
                if not isinstance(existing_entry, dict):
                    continue
                entry = deepcopy(existing_entry)
                self._rewrite_entry_surrogate(entry, surrogate_id, normalized_mappings)
                new_by_source[source_id] = entry
                new_by_surrogate[str(surrogate_id)] = entry
                retained_entries.append(entry)

            payload["by_source"] = new_by_source
            payload["by_surrogate"] = new_by_surrogate
            payload["latest_by_context_sender"] = self._build_latest_by_context_sender(retained_entries)
            return None

        await self._store.mutate(self.empty_payload(), mutate)

    async def get_latest_room_by_context_sender(
        self,
        context_source_id: str,
        sender_source_id: str,
        *,
        max_age_seconds: int | float | None = None,
    ) -> str | None:
        payload = await self._store.read(self.empty_payload())
        context_bucket = payload.get("latest_by_context_sender", {}).get(str(context_source_id), {})
        if not isinstance(context_bucket, dict):
            return None

        entry = context_bucket.get(str(sender_source_id))
        if not isinstance(entry, dict):
            return None

        room_source_id = str(entry.get("room_source_id") or "").strip()
        if not room_source_id:
            return None

        if max_age_seconds is not None:
            timestamp = int(entry.get("timestamp") or 0)
            if timestamp > 0 and (time.time() - timestamp) > float(max_age_seconds):
                return None

        return room_source_id

    @staticmethod
    def _rewrite_entry_surrogate(
        entry: dict[str, Any],
        surrogate_id: int,
        active_mappings: dict[str, int],
    ) -> None:
        entry["surrogate_id"] = int(surrogate_id)
        event = entry.get("onebot_message")
        if not isinstance(event, dict):
            return

        event["message_id"] = int(surrogate_id)
        reply_source_id = str(event.get("rocketchat_reply_source_id") or "").strip()
        segments = event.get("message")
        if not isinstance(segments, list):
            return

        rewritten_segments: list[Any] = []
        for segment in segments:
            if not isinstance(segment, dict) or str(segment.get("type") or "") != "reply":
                rewritten_segments.append(segment)
                continue
            if reply_source_id and reply_source_id in active_mappings:
                updated_segment = deepcopy(segment)
                updated_data = dict(updated_segment.get("data") or {})
                updated_data["id"] = str(active_mappings[reply_source_id])
                updated_segment["data"] = updated_data
                rewritten_segments.append(updated_segment)

        event["message"] = rewritten_segments

    @staticmethod
    def _build_latest_by_context_sender(entries: list[dict[str, Any]]) -> dict[str, Any]:
        latest_by_context_sender: dict[str, Any] = {}
        for entry in entries:
            context_source_id = str(entry.get("context_source_id") or "").strip()
            sender_source_id = str(entry.get("sender_source_id") or "").strip()
            room_source_id = str(entry.get("room_source_id") or "").strip()
            source_id = str(entry.get("source_id") or "").strip()
            if not context_source_id or not sender_source_id or not room_source_id or not source_id:
                continue

            timestamp = int(entry.get("timestamp") or 0)
            context_bucket = latest_by_context_sender.setdefault(context_source_id, {})
            existing = context_bucket.get(sender_source_id)
            existing_timestamp = int(existing.get("timestamp") or 0) if isinstance(existing, dict) else 0
            if timestamp >= existing_timestamp:
                context_bucket[sender_source_id] = {
                    "source_id": source_id,
                    "room_source_id": room_source_id,
                    "timestamp": timestamp,
                }

        return latest_by_context_sender


class PrivateRoomStore:
    def __init__(self, store: JsonStore):
        self._store = store

    @staticmethod
    def empty_payload() -> dict[str, Any]:
        return {"by_user_source": {}, "by_user_surrogate": {}}

    async def bind(self, user_source_id: str, user_surrogate_id: int, room_source_id: str) -> None:
        def mutate(payload: dict[str, Any]) -> None:
            payload.setdefault("by_user_source", {})[str(user_source_id)] = str(room_source_id)
            payload.setdefault("by_user_surrogate", {})[str(user_surrogate_id)] = str(room_source_id)
            return None

        await self._store.mutate(self.empty_payload(), mutate)

    async def get_room_by_user_source(self, user_source_id: str) -> str | None:
        payload = await self._store.read(self.empty_payload())
        return payload.get("by_user_source", {}).get(str(user_source_id))

    async def get_room_by_user_surrogate(self, user_surrogate_id: int | str) -> str | None:
        payload = await self._store.read(self.empty_payload())
        return payload.get("by_user_surrogate", {}).get(str(user_surrogate_id))


class ContextRoomStore:
    def __init__(self, store: JsonStore):
        self._store = store

    @staticmethod
    def empty_payload() -> dict[str, Any]:
        return {"by_context_source": {}, "by_context_surrogate": {}}

    async def bind(
        self,
        context_source_id: str,
        context_surrogate_id: int | str,
        room_source_id: str,
        room_surrogate_id: int | str,
        room_name: str,
        room_slug: str,
    ) -> None:
        entry = {
            "context_source_id": str(context_source_id),
            "context_surrogate_id": int(context_surrogate_id),
            "room_source_id": str(room_source_id),
            "room_surrogate_id": int(room_surrogate_id),
            "room_name": str(room_name),
            "room_slug": str(room_slug),
        }

        def mutate(payload: dict[str, Any]) -> None:
            payload.setdefault("by_context_source", {})[str(context_source_id)] = entry
            payload.setdefault("by_context_surrogate", {})[str(context_surrogate_id)] = entry
            return None

        await self._store.mutate(self.empty_payload(), mutate)

    async def get_by_context_source(self, context_source_id: str) -> dict[str, Any] | None:
        payload = await self._store.read(self.empty_payload())
        return payload.get("by_context_source", {}).get(str(context_source_id))

    async def get_by_context_surrogate(
        self, context_surrogate_id: int | str
    ) -> dict[str, Any] | None:
        payload = await self._store.read(self.empty_payload())
        return payload.get("by_context_surrogate", {}).get(str(context_surrogate_id))