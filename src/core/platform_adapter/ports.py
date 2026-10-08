from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SelectedFile:
    path: str
    media_type: str | None
    size: int


class FilePickerPort(Protocol):
    """Selects local files without exposing Electron or OS APIs to ProductCore."""

    def choose_files(self, *, multiple: bool = True) -> tuple[SelectedFile, ...]:
        """Return user-selected file metadata."""


class CredentialStorePort(Protocol):
    """Stores secrets behind a platform-neutral identifier."""

    def set_secret(self, name: str, value: str) -> None:
        """Store a secret."""

    def get_secret(self, name: str) -> str | None:
        """Return a secret without exposing storage implementation details."""


class NotificationPort(Protocol):
    """Shows a local platform notification."""

    def notify(self, title: str, body: str) -> None:
        """Display one notification."""
