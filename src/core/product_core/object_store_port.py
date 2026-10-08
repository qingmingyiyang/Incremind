from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol


class ProductObjectStorePort(Protocol):
    """Logical structured-object storage required by Product Core.

    The port deliberately exposes no filesystem path, JSON layout, SQLite
    handle, or concrete exception type.
    """

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        """Read one current logical object."""

    def list(self, collection: str) -> Sequence[Mapping[str, object]]:
        """List current logical objects in deterministic order."""

    def delete(self, collection: str, object_id: str) -> bool:
        """Delete one derived or staging object."""

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        """Write with optional compare-and-swap and return the new revision."""

    def is_revision_conflict(self, error: BaseException) -> bool:
        """Return whether ``error`` represents a failed revision CAS."""


class RevisionedProductObjectStorePort(ProductObjectStorePort, Protocol):
    def revision(self, collection: str, object_id: str) -> int:
        """Return logical storage metadata revision without exposing layout."""


def is_revision_conflict(
    store: ProductObjectStorePort,
    error: BaseException,
) -> bool:
    """Ask the adapter to classify one failure without importing its exception."""

    classifier = getattr(store, "is_revision_conflict", None)
    return bool(callable(classifier) and classifier(error))
