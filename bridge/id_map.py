from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .storage import JsonStore


@dataclass(frozen=True, slots=True)
class IdMapping:
    namespace: str
    source_id: str
    surrogate_id: int


MessageWindowCallback = Callable[[dict[str, int]], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class MessageWindowSnapshot:
    active_mappings: dict[str, int]
    changed: bool
    removed_count: int
    compacted: bool
    active_count: int
    max_entries: int


class DurableIdMap:
    _BASES = {
        "user": 1000000000,
        "room": 2000000000,
        "message": 3000000000,
        "thread": 4000000000,
        "context": 5000000000,
    }

    _DEFAULT_MESSAGE_WINDOW_SIZE = 1000

    def __init__(
        self,
        store: JsonStore,
        *,
        message_window_size: int = _DEFAULT_MESSAGE_WINDOW_SIZE,
        on_message_window_changed: MessageWindowCallback | None = None,
    ):
        self._store = store
        self._message_window_size = self.normalize_message_window_size(message_window_size)
        self._on_message_window_changed = on_message_window_changed

    @classmethod
    def normalize_message_window_size(cls, value: Any) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return cls._DEFAULT_MESSAGE_WINDOW_SIZE
        return normalized if normalized > 0 else cls._DEFAULT_MESSAGE_WINDOW_SIZE

    @classmethod
    def message_window_lower_surrogate_id(cls) -> int:
        return cls._BASES["message"] + 1

    @classmethod
    def message_window_upper_surrogate_id(cls, message_window_size: Any) -> int:
        return cls._BASES["message"] + cls.normalize_message_window_size(message_window_size)

    @classmethod
    def message_reset_counter(cls, message_window_size: Any) -> int:
        return cls.normalize_message_window_size(message_window_size) * 2

    @classmethod
    def message_reset_surrogate_id(cls, message_window_size: Any) -> int:
        return cls._BASES["message"] + cls.message_reset_counter(message_window_size)

    def set_message_window_size(self, message_window_size: Any) -> None:
        self._message_window_size = self.normalize_message_window_size(message_window_size)

    @classmethod
    def empty_payload(cls) -> dict[str, Any]:
        return {
            "counters": {namespace: 0 for namespace in cls._BASES},
            "forward": {namespace: {} for namespace in cls._BASES},
            "reverse": {namespace: {} for namespace in cls._BASES},
        }

    async def get_or_create(self, namespace: str, source_id: str) -> IdMapping:
        source_key = str(source_id)
        maintenance_snapshot: MessageWindowSnapshot | None = None

        def mutate(payload: dict[str, Any]) -> IdMapping:
            nonlocal maintenance_snapshot
            self._ensure_namespace(payload, namespace)
            forward = payload["forward"][namespace]
            reverse = payload["reverse"][namespace]
            counters = payload["counters"]
            if source_key in forward:
                surrogate_id = int(forward[source_key])
                return IdMapping(namespace, source_key, surrogate_id)

            counters[namespace] += 1
            surrogate_id = self._BASES[namespace] + int(counters[namespace])
            forward[source_key] = surrogate_id
            reverse[str(surrogate_id)] = source_key
            if namespace == "message":
                maintenance_snapshot = self._maintain_message_window(payload)
                surrogate_id = int(payload["forward"][namespace][source_key])
            return IdMapping(namespace, source_key, surrogate_id)

        mapping = await self._store.mutate(self.empty_payload(), mutate)
        if namespace == "message":
            await self._notify_message_window_changed(maintenance_snapshot)
        return mapping

    async def rebuild_message_window(self, *, force_compact: bool = False) -> dict[str, Any]:
        snapshot: MessageWindowSnapshot | None = None

        def mutate(payload: dict[str, Any]) -> None:
            nonlocal snapshot
            self._ensure_namespace(payload, "message")
            snapshot = self._maintain_message_window(
                payload,
                force_compact=force_compact,
                return_snapshot_when_unchanged=True,
            )
            return None

        await self._store.mutate(self.empty_payload(), mutate)
        if snapshot is None:
            snapshot = MessageWindowSnapshot(
                active_mappings={},
                changed=False,
                removed_count=0,
                compacted=False,
                active_count=0,
                max_entries=self._message_window_size,
            )
        await self._notify_message_window_changed(snapshot)
        return self._snapshot_to_dict(snapshot)

    async def get_source(self, namespace: str, surrogate_id: int | str) -> str | None:
        payload = await self._store.read(self.empty_payload())
        self._ensure_namespace(payload, namespace)
        return payload["reverse"][namespace].get(str(surrogate_id))

    async def get_surrogate(self, namespace: str, source_id: str) -> int | None:
        payload = await self._store.read(self.empty_payload())
        self._ensure_namespace(payload, namespace)
        value = payload["forward"][namespace].get(str(source_id))
        return int(value) if value is not None else None

    def _ensure_namespace(self, payload: dict[str, Any], namespace: str) -> None:
        if namespace not in self._BASES:
            raise KeyError(f"unsupported namespace: {namespace}")
        payload.setdefault("counters", {}).setdefault(namespace, 0)
        payload.setdefault("forward", {}).setdefault(namespace, {})
        payload.setdefault("reverse", {}).setdefault(namespace, {})

    async def _notify_message_window_changed(self, snapshot: MessageWindowSnapshot | None) -> None:
        if snapshot is None or not snapshot.changed or self._on_message_window_changed is None:
            return
        maybe_awaitable = self._on_message_window_changed(dict(snapshot.active_mappings))
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable

    def _snapshot_to_dict(self, snapshot: MessageWindowSnapshot) -> dict[str, Any]:
        highest_surrogate_id = max(snapshot.active_mappings.values(), default=None)
        return {
            "changed": snapshot.changed,
            "removed_count": snapshot.removed_count,
            "compacted": snapshot.compacted,
            "active_count": snapshot.active_count,
            "max_entries": snapshot.max_entries,
            "highest_surrogate_id": highest_surrogate_id,
            "reset_surrogate_id": self.message_reset_surrogate_id(snapshot.max_entries),
        }

    def _maintain_message_window(
        self,
        payload: dict[str, Any],
        *,
        force_compact: bool = False,
        return_snapshot_when_unchanged: bool = False,
    ) -> MessageWindowSnapshot | None:
        namespace = "message"
        forward = payload["forward"][namespace]
        reverse = payload["reverse"][namespace]
        changed = False
        removed_count = 0

        overflow = len(forward) - self._message_window_size
        if overflow > 0:
            ordered_surrogates = sorted(reverse.items(), key=lambda item: int(item[0]))
            for surrogate_key, source_key in ordered_surrogates[:overflow]:
                reverse.pop(str(surrogate_key), None)
                forward.pop(str(source_key), None)
            removed_count = overflow
            changed = True

        compacted = False
        reset_counter = self.message_reset_counter(self._message_window_size)
        if force_compact or int(payload["counters"].get(namespace) or 0) >= reset_counter:
            compacted = bool(forward)
            changed = self._compact_message_window(payload) or changed

        active_mappings = {
            str(active_source_id): int(active_surrogate_id)
            for active_source_id, active_surrogate_id in payload["forward"][namespace].items()
        }

        if not changed and not return_snapshot_when_unchanged:
            return None

        return MessageWindowSnapshot(
            active_mappings=active_mappings,
            changed=changed,
            removed_count=removed_count,
            compacted=compacted,
            active_count=len(active_mappings),
            max_entries=self._message_window_size,
        )

    def _compact_message_window(self, payload: dict[str, Any]) -> bool:
        namespace = "message"
        forward = payload["forward"][namespace]
        reverse = payload["reverse"][namespace]
        ordered_sources = [
            str(source_key)
            for _, source_key in sorted(reverse.items(), key=lambda item: int(item[0]))
        ]
        if not ordered_sources:
            payload["counters"][namespace] = 0
            return False

        new_forward: dict[str, int] = {}
        new_reverse: dict[str, str] = {}
        changed = False
        for index, source_key in enumerate(ordered_sources, start=1):
            new_surrogate_id = self._BASES[namespace] + index
            old_surrogate_id = int(forward.get(source_key) or 0)
            if old_surrogate_id != new_surrogate_id:
                changed = True
            new_forward[source_key] = new_surrogate_id
            new_reverse[str(new_surrogate_id)] = source_key

        payload["forward"][namespace] = new_forward
        payload["reverse"][namespace] = new_reverse
        payload["counters"][namespace] = len(ordered_sources)
        return changed