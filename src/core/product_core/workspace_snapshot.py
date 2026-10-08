"""Host-owned, bounded workspace metadata snapshots.

This module is intentionally narrower than a synchronizer.  It inspects one
explicit workspace root and emits a content-addressed manifest for a later
*plan-only* reconciliation.  It never retains a root path, file body, secret,
or an instruction to write a workspace.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time


class WorkspaceSnapshotError(ValueError):
    """Raised when a workspace cannot be represented safely and completely."""


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshotBudget:
    """Hard bounds for a single host-owned workspace inspection."""

    max_entries: int = 4096
    max_path_length: int = 512
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_scan_seconds: float = 5.0
    read_chunk_bytes: int = 1024 * 1024

    def validate(self) -> None:
        if self.max_entries < 1 or self.max_path_length < 1:
            raise WorkspaceSnapshotError("workspace snapshot entry bounds are invalid")
        if self.max_file_bytes < 0 or self.max_total_bytes < 0:
            raise WorkspaceSnapshotError("workspace snapshot byte bounds are invalid")
        if self.max_scan_seconds <= 0 or self.read_chunk_bytes < 1:
            raise WorkspaceSnapshotError("workspace snapshot scan bounds are invalid")


@dataclass(frozen=True, slots=True)
class WorkspaceManifestEntry:
    """Portable evidence for one regular file, without its content or location."""

    relative_path: str
    digest: str
    size_bytes: int
    base_revision: str


@dataclass(frozen=True, slots=True)
class WorkspaceManifestSnapshot:
    """A deterministic manifest that can be safely placed in a resume packet."""

    manifest_ref: str
    base_revision: str
    entries: tuple[WorkspaceManifestEntry, ...]
    total_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "manifest_ref": self.manifest_ref,
            "base_revision": self.base_revision,
            "entries": [asdict(entry) for entry in self.entries],
            "total_bytes": self.total_bytes,
        }


_OPAQUE_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SENSITIVE_COMPONENT = re.compile(
    r"(?:^|[._-])(?:api[_-]?key|credential|cookie|id_rsa|passphrase|password|private[_-]?key|"
    r"secret|token)(?:$|[._-])",
    re.IGNORECASE,
)
_EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".hg", ".idea", ".svn", ".ssh", ".vscode", "__pycache__", "build", "cache", "dist", "node_modules",
})
_EXCLUDED_FILENAMES = frozenset({".env", ".npmrc", ".pypirc", "credentials", "known_secrets"})
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


def create_workspace_manifest_snapshot(
    workspace_root: str | Path,
    *,
    base_revision: str,
    budget: WorkspaceSnapshotBudget = WorkspaceSnapshotBudget(),
    monotonic: Callable[[], float] = time.monotonic,
) -> WorkspaceManifestSnapshot:
    """Return a stable, bounded manifest for a host-controlled workspace.

    SHA-256 is used here only because the manifest crosses a device boundary
    and must provide byte-exact content identity for later reconciliation.  No
    bytes are included in the manifest or retained by this function.
    """
    budget.validate()
    base_revision = _base_revision(base_revision)
    root = _workspace_root(workspace_root)
    started = monotonic()
    entries: list[WorkspaceManifestEntry] = []
    total_bytes = 0
    pending = [root]

    while pending:
        _within_deadline(started, budget, monotonic)
        directory = pending.pop()
        _assert_directory(directory, root)
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as error:
            raise WorkspaceSnapshotError("workspace directory cannot be read") from error
        for child in children:
            _within_deadline(started, budget, monotonic)
            child_path = Path(child.path)
            relative_path = _portable_relative(child_path, root, budget)
            if _is_excluded(relative_path):
                continue
            try:
                before = child_path.lstat()
            except OSError as error:
                raise WorkspaceSnapshotError("workspace entry changed during scan") from error
            _assert_not_link_or_reparse(before)
            if stat.S_ISDIR(before.st_mode):
                _assert_resolves_within(child_path, root)
                pending.append(child_path)
                continue
            if not stat.S_ISREG(before.st_mode):
                raise WorkspaceSnapshotError("workspace contains a non-regular file")
            _assert_resolves_within(child_path, root)
            if before.st_size > budget.max_file_bytes:
                raise WorkspaceSnapshotError("workspace file exceeds snapshot byte budget")
            if len(entries) >= budget.max_entries:
                raise WorkspaceSnapshotError("workspace exceeds snapshot entry budget")
            if total_bytes + before.st_size > budget.max_total_bytes:
                raise WorkspaceSnapshotError("workspace exceeds total snapshot byte budget")
            digest, after = _digest_regular_file(child_path, before, budget, started, monotonic)
            _assert_same_metadata(before, after)
            entries.append(WorkspaceManifestEntry(relative_path, digest, before.st_size, base_revision))
            total_bytes += before.st_size

    ordered = tuple(sorted(entries, key=lambda entry: entry.relative_path))
    manifest_ref = _manifest_ref(base_revision, ordered)
    return WorkspaceManifestSnapshot(manifest_ref, base_revision, ordered, total_bytes)


def _workspace_root(value: str | Path) -> Path:
    root = Path(value)
    try:
        metadata = root.lstat()
    except OSError as error:
        raise WorkspaceSnapshotError("workspace root does not exist") from error
    _assert_not_link_or_reparse(metadata)
    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceSnapshotError("workspace root must be a directory")
    try:
        return root.resolve(strict=True)
    except OSError as error:
        raise WorkspaceSnapshotError("workspace root cannot be resolved") from error


def _base_revision(value: object) -> str:
    if not isinstance(value, str) or not _OPAQUE_REVISION.fullmatch(value):
        raise WorkspaceSnapshotError("workspace base revision must be an opaque identifier")
    return value


def _portable_relative(path: Path, root: Path, budget: WorkspaceSnapshotBudget) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise WorkspaceSnapshotError("workspace entry escapes its root") from error
    portable = PurePosixPath(relative).as_posix()
    if not portable or portable == "." or len(portable) > budget.max_path_length:
        raise WorkspaceSnapshotError("workspace entry path exceeds snapshot budget")
    if any(component in {"", ".", ".."} for component in portable.split("/")):
        raise WorkspaceSnapshotError("workspace entry path is not portable")
    return portable


def _is_excluded(relative_path: str) -> bool:
    components = relative_path.split("/")
    for component in components:
        lowered = component.casefold()
        if lowered in _EXCLUDED_DIRECTORIES or lowered in _EXCLUDED_FILENAMES:
            return True
        if lowered.startswith(".env.") or _SENSITIVE_COMPONENT.search(component):
            return True
    return False


def _assert_directory(path: Path, root: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise WorkspaceSnapshotError("workspace directory changed during scan") from error
    _assert_not_link_or_reparse(metadata)
    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceSnapshotError("workspace directory changed during scan")
    _assert_resolves_within(path, root)


def _assert_not_link_or_reparse(metadata: os.stat_result) -> None:
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT:
        raise WorkspaceSnapshotError("workspace contains a symlink or reparse point")


def _assert_resolves_within(path: Path, root: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise WorkspaceSnapshotError("workspace entry escapes its root") from error


def _digest_regular_file(
    path: Path,
    expected: os.stat_result,
    budget: WorkspaceSnapshotBudget,
    started: float,
    monotonic: Callable[[], float],
) -> tuple[str, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise WorkspaceSnapshotError("workspace file cannot be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        _assert_not_link_or_reparse(opened)
        if not stat.S_ISREG(opened.st_mode):
            raise WorkspaceSnapshotError("workspace contains a non-regular file")
        _assert_same_metadata(expected, opened)
        digest = sha256()
        read_total = 0
        while True:
            _within_deadline(started, budget, monotonic)
            chunk = os.read(descriptor, budget.read_chunk_bytes)
            if not chunk:
                break
            read_total += len(chunk)
            if read_total > budget.max_file_bytes:
                raise WorkspaceSnapshotError("workspace file exceeds snapshot byte budget")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if read_total != expected.st_size:
            raise WorkspaceSnapshotError("workspace file changed during scan")
        return digest.hexdigest(), after
    finally:
        os.close(descriptor)


def _assert_same_metadata(before: os.stat_result, after: os.stat_result) -> None:
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_mode != after.st_mode
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise WorkspaceSnapshotError("workspace file changed during scan")


def _within_deadline(started: float, budget: WorkspaceSnapshotBudget, monotonic: Callable[[], float]) -> None:
    if monotonic() - started > budget.max_scan_seconds:
        raise WorkspaceSnapshotError("workspace snapshot scan timed out")


def _manifest_ref(base_revision: str, entries: tuple[WorkspaceManifestEntry, ...]) -> str:
    canonical = json.dumps(
        {
            "schema_version": "workspace_manifest.v1",
            "base_revision": base_revision,
            "entries": [asdict(entry) for entry in entries],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"workspace-manifest:sha256:{sha256(canonical).hexdigest()}"
