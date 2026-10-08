from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import BinaryIO, Protocol


@dataclass(frozen=True, slots=True)
class StoredBlob:
    uri: str
    sha256: str
    size: int


class BlobStorePort(Protocol):
    """Stores immutable source bytes behind stable URIs."""

    def put(self, stream: BinaryIO, *, expected_sha256: str | None = None) -> StoredBlob:
        """Store bytes and return their verified identity."""

    def open(self, uri: str) -> BinaryIO:
        """Open a read stream for a controlled URI."""


class ObjectStorePort(Protocol):
    """Stores versioned structured objects independently of an index engine."""

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        """Read one object."""

    def list(self, collection: str) -> Sequence[Mapping[str, object]]:
        """List objects in deterministic repository order."""

    def delete(self, collection: str, object_id: str) -> bool:
        """Delete one object and return whether it existed."""

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        """Write an object and return the new revision."""
