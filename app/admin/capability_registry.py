from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum


class CapabilityHealth(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    ACTION_REQUIRED = "action_required"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class Capability:
    id: str
    enabled: bool
    available: bool
    health: CapabilityHealth
    required_permissions: tuple[str, ...] = ()
    reason: str = ""


class CapabilityRegistry(Mapping[str, Capability]):
    """Immutable, duplicate-safe registry used as the navigation/API truth."""

    def __init__(self, capabilities: Iterable[Capability] = ()) -> None:
        items: dict[str, Capability] = {}
        for capability in capabilities:
            normalized_id = str(capability.id or "").strip()
            if not normalized_id:
                raise ValueError("capability id cannot be empty")
            if normalized_id in items:
                raise ValueError(f"duplicate capability id: {normalized_id}")
            items[normalized_id] = capability
        self._items = items

    def __getitem__(self, key: str) -> Capability:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def available(self, capability_id: str, *, permissions: Iterable[str] = ()) -> bool:
        capability = self._items.get(capability_id)
        if capability is None or not capability.enabled or not capability.available:
            return False
        granted = frozenset(permissions)
        return all(permission in granted for permission in capability.required_permissions)

    def snapshot(self) -> tuple[Capability, ...]:
        return tuple(self._items.values())
