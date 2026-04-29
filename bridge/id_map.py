from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .storage import JsonStore


@dataclass(frozen=True, slots=True)
class IdMapping:
    namespace: str
    source_id: str
    surrogate_id: int


class DurableIdMap:
    _BASES = {
        "user": 1000000000,
        "room": 2000000000,
        "message": 3000000000,
        "thread": 4000000000,
        "context": 5000000000,
    }

    def __init__(self, store: JsonStore):
        self._store = store

    @classmethod
    def empty_payload(cls) -> dict[str, Any]:
        return {
            "counters": {namespace: 0 for namespace in cls._BASES},
            "forward": {namespace: {} for namespace in cls._BASES},
            "reverse": {namespace: {} for namespace in cls._BASES},
        }

    async def get_or_create(self, namespace: str, source_id: str) -> IdMapping:
        source_key = str(source_id)

        def mutate(payload: dict[str, Any]) -> IdMapping:
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
            return IdMapping(namespace, source_key, surrogate_id)

        return await self._store.mutate(self.empty_payload(), mutate)

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