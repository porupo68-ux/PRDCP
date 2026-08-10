from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from common.role_definitions.models import RoleDefinitionSnapshot


@dataclass(frozen=True)
class CacheEntry:
    snapshot: RoleDefinitionSnapshot
    fingerprint: tuple[int, int]


class RoleDefinitionCache:
    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._lock = RLock()

    def get(self, agent_id: str) -> CacheEntry | None:
        with self._lock:
            return self._entries.get(agent_id)

    def set(
        self,
        agent_id: str,
        snapshot: RoleDefinitionSnapshot,
        fingerprint: tuple[int, int],
    ) -> None:
        with self._lock:
            self._entries[agent_id] = CacheEntry(snapshot=snapshot, fingerprint=fingerprint)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    @property
    def agent_ids(self) -> set[str]:
        with self._lock:
            return set(self._entries)
