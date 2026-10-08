from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from .ports import CapabilityDefinition, CapabilityProviderPort, CapabilityRegistrationPort


class CapabilityRegistryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CapabilityRegistrySnapshot:
    """One atomic, provider-free view of the registered capability contracts."""

    generation: int
    definitions: tuple[CapabilityDefinition, ...]


class _Registration(CapabilityRegistrationPort):
    def __init__(self, registry: "ScopedCapabilityRegistry", capability_id: str, generation: int) -> None:
        self._registry = registry
        self._capability_id = capability_id
        self._generation = generation
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._registry._remove(self._capability_id, self._generation)
            self._closed = True


class ScopedCapabilityRegistry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, tuple[int, CapabilityDefinition, CapabilityProviderPort]] = {}
        self._generation = 0

    def register(self, definition: CapabilityDefinition, provider: CapabilityProviderPort) -> _Registration:
        with self._lock:
            if definition.capability_id in self._entries:
                raise CapabilityRegistryError(f"capability already registered: {definition.capability_id}")
            self._generation += 1
            generation = self._generation
            self._entries[definition.capability_id] = (generation, definition, provider)
            return _Registration(self, definition.capability_id, generation)

    def get(self, capability_id: str) -> CapabilityDefinition | None:
        resolved = self.resolve(capability_id)
        return resolved[0] if resolved else None

    def list(self) -> tuple[CapabilityDefinition, ...]:
        return self.snapshot().definitions

    def snapshot(self) -> CapabilityRegistrySnapshot:
        """Return contracts and generation under one registry lock.

        The snapshot deliberately does not expose providers or registrations,
        so diagnostics and catalog projections cannot dispatch through it.
        """
        with self._lock:
            return CapabilityRegistrySnapshot(
                generation=self._generation,
                definitions=tuple(
                    entry[1]
                    for entry in sorted(
                        self._entries.values(), key=lambda item: item[1].capability_id
                    )
                ),
            )

    def resolve(self, capability_id: str) -> tuple[CapabilityDefinition, CapabilityProviderPort] | None:
        with self._lock:
            entry = self._entries.get(capability_id)
            return (entry[1], entry[2]) if entry else None

    def _remove(self, capability_id: str, generation: int) -> None:
        with self._lock:
            entry = self._entries.get(capability_id)
            if entry and entry[0] == generation:
                del self._entries[capability_id]
                # Removal is an observable contract change too.  A catalog
                # consumer can therefore never mistake a post-removal list
                # for the same registry revision it saw before.
                self._generation += 1
