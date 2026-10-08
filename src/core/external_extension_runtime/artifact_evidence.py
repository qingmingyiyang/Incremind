"""Immutable, non-executable quarantine evidence for acquired extension artifacts.

This module deliberately owns bytes only.  It has no scheduler, Effect state,
or activation path: Core Effect Log remains the execution authority and the
stored artifact tree is evidence for its receipt/probe contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from core.external_extensions import ArtifactInventory, ResolvedSource
from core.external_extension_runtime.windows_handle_io import (
    WindowsHandleIoError,
    WindowsHandleTreeIo,
)


class ArtifactEvidenceError(ValueError):
    """Raised when quarantine evidence is unsafe, incomplete, or has drifted."""


_OPERATION_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:-]{7,159}$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_PREFIX: Final = "crp://external-extension-artifacts/"
_SCHEMA_VERSION: Final = "1.0.0"
_OPERATIONS: Final = "operations"
_PENDING: Final = ".pending"
_ARTIFACT: Final = "artifact"
_RECEIPT: Final = "receipt.json"
_MAX_ARTIFACT_FILES: Final = 512
_MAX_ARTIFACT_FILE_BYTES: Final = 2 * 1024 * 1024
_MAX_ARTIFACT_TOTAL_BYTES: Final = 16 * 1024 * 1024
_MAX_RECEIPT_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class ArtifactEvidence:
    """A self-validating immutable summary of one quarantined artifact tree."""

    operation_id: str
    artifact_ref: str
    source_revision: str
    artifact_receipt_ref: str
    inventory: ArtifactInventory
    content_sha256: str
    file_count: int
    total_bytes: int

    def __post_init__(self) -> None:
        _operation(self.operation_id)
        if not isinstance(self.artifact_ref, str) or not self.artifact_ref.startswith("crp://"):
            raise ArtifactEvidenceError("artifact reference is invalid")
        if not isinstance(self.source_revision, str) or not self.source_revision:
            raise ArtifactEvidenceError("source revision is invalid")
        if self.artifact_receipt_ref != artifact_receipt_ref(self.operation_id):
            raise ArtifactEvidenceError("artifact receipt reference is invalid")
        if not isinstance(self.inventory, ArtifactInventory):
            raise ArtifactEvidenceError("artifact inventory is invalid")
        if not isinstance(self.file_count, int) or self.file_count != len(self.inventory.paths):
            raise ArtifactEvidenceError("artifact file count drifted")
        if not isinstance(self.total_bytes, int) or self.total_bytes != self.inventory.total_bytes:
            raise ArtifactEvidenceError("artifact byte count drifted")
        actual = _tree_sha256(self.inventory)
        if (
            not isinstance(self.content_sha256, str)
            or not _SHA256.fullmatch(self.content_sha256)
            or self.content_sha256 != actual
        ):
            raise ArtifactEvidenceError("artifact content tree digest drifted")


class ImmutableQuarantineArtifactStore:
    """Filesystem-backed evidence store with no executable artifact access path."""

    def __init__(self, root: str | Path, *, handle_io: WindowsHandleTreeIo | None = None) -> None:
        candidate = Path(root)
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise ArtifactEvidenceError("quarantine root is unsafe")
        self._root = candidate
        self._handle_io = handle_io or WindowsHandleTreeIo()
        if os.name == "nt":
            try:
                # Find one existing non-reparse anchor without mutating it,
                # then create the entire missing chain below a single HANDLE.
                _ensure_windows_directory(candidate, self._handle_io)
                self._handle_io.ensure_directory_chain(candidate, (_OPERATIONS,))
                self._handle_io.ensure_directory_chain(candidate, (_PENDING,))
            except (OSError, WindowsHandleIoError) as error:
                raise ArtifactEvidenceError("quarantine root cannot be created safely") from error
            return
        _mkdir_safe_path(candidate)
        _assert_safe_directory(candidate)
        for child in (_OPERATIONS, _PENDING):
            directory = self._root / child
            directory.mkdir(exist_ok=True)
            _assert_safe_directory(directory)
            _chmod_directory(directory, writable=True)

    def commit(
        self,
        operation_id: str,
        source: ResolvedSource,
        inventory: ArtifactInventory,
    ) -> ArtifactEvidence:
        operation = _operation(operation_id)
        _assert_frozen_source(source)
        expected = _new_evidence(operation, source, inventory)
        if os.name == "nt":
            return self._commit_windows(operation, source, expected)
        final = self._final_dir(operation)
        pending = self._pending_dir(operation)

        if final.exists():
            return self._require_same(_read_evidence(final, operation, source), expected)
        if pending.exists():
            recovered = self._require_same(_read_evidence(pending, operation, source), expected)
            self._promote(pending, final)
            return recovered

        self._write_pending(pending, expected)
        self._promote(pending, final)
        return expected

    def probe(self, operation_id: str, source: ResolvedSource) -> ArtifactEvidence | None:
        operation = _operation(operation_id)
        _assert_frozen_source(source)
        if os.name == "nt":
            return self._probe_windows(operation, source)
        final = self._final_dir(operation)
        pending = self._pending_dir(operation)
        if final.exists():
            return _read_evidence(final, operation, source)
        if not pending.exists():
            return None
        evidence = _read_evidence(pending, operation, source)
        self._promote(pending, final)
        return evidence

    def load_final(self, operation_id: str, source: ResolvedSource) -> ArtifactEvidence | None:
        """Read only committed evidence; pending evidence is never recoverable here."""
        operation = _operation(operation_id)
        _assert_frozen_source(source)
        if os.name == "nt":
            return self._load_windows(operation, source, pending=False)
        final = self._final_dir(operation)
        if not final.exists():
            return None
        return _read_evidence(final, operation, source)

    def _write_pending(self, pending: Path, evidence: ArtifactEvidence) -> None:
        if os.name == "nt":
            if pending != self._pending_dir(evidence.operation_id):
                raise ArtifactEvidenceError("quarantine pending path is invalid")
            try:
                created = self._handle_io.write_new_tree(
                    self._root,
                    (_PENDING, _operation_component(evidence.operation_id)),
                    _evidence_tree_files(evidence),
                )
            except (OSError, WindowsHandleIoError) as error:
                raise ArtifactEvidenceError("quarantine artifact write failed") from error
            if not created:
                raise ArtifactEvidenceError("quarantine operation is already being committed")
            return
        pending.parent.mkdir(exist_ok=True)
        _assert_safe_directory(pending.parent)
        try:
            pending.mkdir(mode=0o700)
        except FileExistsError as error:
            raise ArtifactEvidenceError("quarantine operation is already being committed") from error
        try:
            _assert_safe_directory(pending)
            artifact = pending / _ARTIFACT
            artifact.mkdir(mode=0o700)
            _assert_safe_directory(artifact)
            for relative in evidence.inventory.paths:
                target = _safe_artifact_child(artifact, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                _assert_safe_directory_chain(artifact, target.parent)
                _write_bytes_exclusive(target, evidence.inventory.read_bytes(relative))
            _seal_tree(artifact)
            # Receipt is deliberately last: a pending tree without it is never recoverable.
            _write_bytes_exclusive(pending / _RECEIPT, _receipt_payload(evidence))
            _chmod_file(pending / _RECEIPT)
            _fsync_directory(pending)
        except Exception:
            # Keep the incomplete pending directory for forensic inspection; probe fails closed.
            raise

    def _commit_windows(
        self, operation: str, source: ResolvedSource, expected: ArtifactEvidence
    ) -> ArtifactEvidence:
        final = self._load_windows(operation, source, pending=False)
        if final is not None:
            return self._require_same(final, expected)
        pending = self._load_windows(operation, source, pending=True)
        if pending is not None:
            recovered = self._require_same(pending, expected)
            return self._promote_windows(operation, source, recovered)
        try:
            created = self._handle_io.write_new_tree(
                self._root,
                (_PENDING, _operation_component(operation)),
                _evidence_tree_files(expected),
            )
        except (OSError, WindowsHandleIoError) as error:
            raise ArtifactEvidenceError("quarantine artifact write failed") from error
        if not created:
            pending = self._load_windows(operation, source, pending=True)
            if pending is None:
                raise ArtifactEvidenceError("quarantine operation is already being committed")
            return self._promote_windows(operation, source, self._require_same(pending, expected))
        return self._promote_windows(operation, source, expected)

    def _probe_windows(self, operation: str, source: ResolvedSource) -> ArtifactEvidence | None:
        final = self._load_windows(operation, source, pending=False)
        if final is not None:
            return final
        pending = self._load_windows(operation, source, pending=True)
        if pending is None:
            return None
        return self._promote_windows(operation, source, pending)

    def _load_windows(
        self, operation: str, source: ResolvedSource, *, pending: bool
    ) -> ArtifactEvidence | None:
        try:
            tree = self._handle_io.read_bounded_tree(
                self._root,
                ((_PENDING if pending else _OPERATIONS), _operation_component(operation)),
                max_files=_MAX_ARTIFACT_FILES + 1,
                max_file_bytes=_MAX_ARTIFACT_FILE_BYTES,
                max_total_bytes=_MAX_ARTIFACT_TOTAL_BYTES + _MAX_RECEIPT_BYTES,
            )
        except FileNotFoundError:
            return None
        except (OSError, WindowsHandleIoError) as error:
            # An incomplete pending tree and a reparse/tree race are equally
            # non-recoverable.  Keep the receipt-oriented contract for callers
            # while retaining the original HANDLE failure as the cause.
            raise ArtifactEvidenceError("quarantine receipt is missing or unsafe") from error
        return _read_evidence_mapping(tree, operation, source)

    def _promote_windows(
        self, operation: str, source: ResolvedSource, expected: ArtifactEvidence
    ) -> ArtifactEvidence:
        try:
            self._handle_io.move_dir_no_replace(
                self._root,
                (_PENDING, _operation_component(operation)),
                (_OPERATIONS,),
                _operation_component(operation),
            )
            return expected
        except FileNotFoundError as error:
            # A concurrent winner may have moved the same immutable pending
            # tree.  Its final receipt must still equal the expected fact.
            final = self._load_windows(operation, source, pending=False)
            if final is None:
                raise ArtifactEvidenceError("quarantine artifact promotion failed") from error
            return self._require_same(final, expected)
        except (OSError, WindowsHandleIoError) as error:
            final = self._load_windows(operation, source, pending=False)
            if final is not None:
                return self._require_same(final, expected)
            raise ArtifactEvidenceError("quarantine artifact promotion failed") from error

    def _promote(self, pending: Path, final: Path) -> None:
        if os.name == "nt":
            if (
                pending.parent != self._root / _PENDING
                or final.parent != self._root / _OPERATIONS
                or pending.name != final.name
            ):
                raise ArtifactEvidenceError("quarantine promotion path is invalid")
            try:
                self._handle_io.move_dir_no_replace(
                    self._root,
                    (_PENDING, pending.name),
                    (_OPERATIONS,),
                    final.name,
                )
            except (OSError, WindowsHandleIoError) as error:
                raise ArtifactEvidenceError("quarantine artifact promotion failed") from error
            return
        _assert_safe_directory(pending)
        if final.exists():
            raise ArtifactEvidenceError("quarantine final operation already exists")
        try:
            os.replace(pending, final)
        except OSError as error:
            raise ArtifactEvidenceError("quarantine artifact promotion failed") from error
        _assert_safe_directory(final)
        _fsync_directory(final.parent)

    def _final_dir(self, operation: str) -> Path:
        return _safe_child(self._root / _OPERATIONS, _operation_component(operation))

    def _pending_dir(self, operation: str) -> Path:
        return _safe_child(self._root / _PENDING, _operation_component(operation))

    @staticmethod
    def _require_same(actual: ArtifactEvidence, expected: ArtifactEvidence) -> ArtifactEvidence:
        if actual != expected:
            raise ArtifactEvidenceError("immutable quarantine evidence drifted")
        return actual


def artifact_receipt_ref(operation_id: str) -> str:
    return _RECEIPT_PREFIX + _operation(operation_id) + "/receipt"


def _new_evidence(operation: str, source: ResolvedSource, inventory: ArtifactInventory) -> ArtifactEvidence:
    return ArtifactEvidence(
        operation_id=operation,
        artifact_ref=source.artifact_ref,
        source_revision=source.immutable_revision or "",
        artifact_receipt_ref=artifact_receipt_ref(operation),
        inventory=inventory,
        content_sha256=_tree_sha256(inventory),
        file_count=len(inventory.paths),
        total_bytes=inventory.total_bytes,
    )


def _evidence_tree_files(evidence: ArtifactEvidence) -> dict[str, bytes]:
    files = {
        f"{_ARTIFACT}/{path}": evidence.inventory.read_bytes(path)
        for path in evidence.inventory.paths
    }
    # Receipt stays last so a fully written tree always has the same causal
    # meaning as the legacy pending protocol.
    files[_RECEIPT] = _receipt_payload(evidence)
    return files


def _read_evidence(directory: Path, operation: str, source: ResolvedSource) -> ArtifactEvidence:
    _assert_safe_directory(directory)
    receipt = directory / _RECEIPT
    if not receipt.is_file() or _is_reparse(receipt):
        raise ArtifactEvidenceError("quarantine receipt is missing or unsafe")
    try:
        payload = json.loads(_read_bytes_no_follow(receipt, max_bytes=_MAX_RECEIPT_BYTES).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ArtifactEvidenceError("quarantine receipt is invalid") from error
    if not isinstance(payload, dict):
        raise ArtifactEvidenceError("quarantine receipt is invalid")
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ArtifactEvidenceError("quarantine receipt schema is invalid")
    if payload.get("operation_id") != operation or payload.get("artifact_ref") != source.artifact_ref:
        raise ArtifactEvidenceError("quarantine receipt source drifted")
    if payload.get("source_revision") != source.immutable_revision:
        raise ArtifactEvidenceError("quarantine receipt source drifted")
    if payload.get("artifact_receipt_ref") != artifact_receipt_ref(operation):
        raise ArtifactEvidenceError("quarantine receipt reference is invalid")
    inventory = _read_inventory(directory / _ARTIFACT)
    try:
        return ArtifactEvidence(
            operation_id=operation,
            artifact_ref=str(payload["artifact_ref"]),
            source_revision=str(payload["source_revision"]),
            artifact_receipt_ref=str(payload["artifact_receipt_ref"]),
            inventory=inventory,
            content_sha256=str(payload["content_sha256"]),
            file_count=payload["file_count"],
            total_bytes=payload["total_bytes"],
        )
    except ArtifactEvidenceError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise ArtifactEvidenceError("quarantine receipt evidence is invalid") from error


def _read_evidence_mapping(
    tree: dict[str, bytes], operation: str, source: ResolvedSource
) -> ArtifactEvidence:
    """Validate a one-shot, handle-frozen quarantine tree without reopening it."""
    if not isinstance(tree, dict) or _RECEIPT not in tree:
        raise ArtifactEvidenceError("quarantine receipt is missing or unsafe")
    receipt = tree[_RECEIPT]
    if not isinstance(receipt, bytes) or len(receipt) > _MAX_RECEIPT_BYTES:
        raise ArtifactEvidenceError("quarantine receipt is invalid")
    artifact_files: dict[str, bytes] = {}
    for path, content in tree.items():
        if path == _RECEIPT:
            continue
        if not isinstance(path, str) or not path.startswith(_ARTIFACT + "/"):
            raise ArtifactEvidenceError("quarantine artifact tree is unsafe")
        relative = path[len(_ARTIFACT) + 1 :]
        if not relative or not isinstance(content, bytes):
            raise ArtifactEvidenceError("quarantine artifact tree is unsafe")
        artifact_files[relative] = content
    if not artifact_files or len(artifact_files) > _MAX_ARTIFACT_FILES:
        raise ArtifactEvidenceError("quarantine artifact inventory is invalid")
    try:
        payload = json.loads(receipt.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ArtifactEvidenceError("quarantine receipt is invalid") from error
    if not isinstance(payload, dict):
        raise ArtifactEvidenceError("quarantine receipt is invalid")
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ArtifactEvidenceError("quarantine receipt schema is invalid")
    if payload.get("operation_id") != operation or payload.get("artifact_ref") != source.artifact_ref:
        raise ArtifactEvidenceError("quarantine receipt source drifted")
    if payload.get("source_revision") != source.immutable_revision:
        raise ArtifactEvidenceError("quarantine receipt source drifted")
    if payload.get("artifact_receipt_ref") != artifact_receipt_ref(operation):
        raise ArtifactEvidenceError("quarantine receipt reference is invalid")
    try:
        inventory = ArtifactInventory.capture(artifact_files)
        return ArtifactEvidence(
            operation_id=operation,
            artifact_ref=str(payload["artifact_ref"]),
            source_revision=str(payload["source_revision"]),
            artifact_receipt_ref=str(payload["artifact_receipt_ref"]),
            inventory=inventory,
            content_sha256=str(payload["content_sha256"]),
            file_count=payload["file_count"],
            total_bytes=payload["total_bytes"],
        )
    except ArtifactEvidenceError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise ArtifactEvidenceError("quarantine receipt evidence is invalid") from error


def _read_inventory(artifact: Path) -> ArtifactInventory:
    _assert_safe_directory(artifact)
    files: dict[str, bytes] = {}
    for current, directories, filenames in os.walk(artifact, topdown=True, followlinks=False):
        current_path = Path(current)
        _assert_safe_directory(current_path)
        for name in directories:
            child = current_path / name
            if _is_reparse(child):
                raise ArtifactEvidenceError("quarantine artifact contains a reparse directory")
        for name in filenames:
            child = current_path / name
            if _is_reparse(child) or not child.is_file():
                raise ArtifactEvidenceError("quarantine artifact contains an unsafe file")
            relative = child.relative_to(artifact).as_posix()
            files[relative] = _read_bytes_no_follow(child)
    try:
        return ArtifactInventory.capture(files)
    except ValueError as error:
        raise ArtifactEvidenceError("quarantine artifact inventory is invalid") from error


def _receipt_payload(evidence: ArtifactEvidence) -> bytes:
    # No machine path, source locator, credential, or transport URL is persisted here.
    value = {
        "schema_version": _SCHEMA_VERSION,
        "operation_id": evidence.operation_id,
        "artifact_ref": evidence.artifact_ref,
        "source_revision": evidence.source_revision,
        "artifact_receipt_ref": evidence.artifact_receipt_ref,
        "content_sha256": evidence.content_sha256,
        "file_count": evidence.file_count,
        "total_bytes": evidence.total_bytes,
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _tree_sha256(inventory: ArtifactInventory) -> str:
    digest = hashlib.sha256()
    for path in inventory.paths:
        content = inventory.read_bytes(path)
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def _operation(value: object) -> str:
    if not isinstance(value, str) or not _OPERATION_ID.fullmatch(value):
        raise ArtifactEvidenceError("quarantine operation id is invalid")
    return value


def _operation_component(operation: str) -> str:
    """Return a stable filesystem component for an opaque operation identity.

    POSIX keeps the established raw-name layout.  Windows encodes every
    operation, including apparently ordinary ids: reserved device names and
    Win32's trailing-dot/space normalization otherwise make distinct durable
    operation identities collide.
    """
    _operation(operation)
    if os.name != "nt":
        return operation
    # URL-safe base64 is injective, Windows-component-safe, and still shorter
    # than MAX_PATH's per-component limit for the accepted 160-byte id.
    return "operation-" + urlsafe_b64encode(operation.encode("ascii")).decode("ascii").rstrip("=")


def _assert_frozen_source(source: ResolvedSource) -> None:
    if not isinstance(source, ResolvedSource) or not source.is_immutable:
        raise ArtifactEvidenceError("quarantine source must have an immutable revision")


def _safe_child(parent: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ArtifactEvidenceError("quarantine path is unsafe")
    child = parent / name
    try:
        child.relative_to(parent)
    except ValueError as error:
        raise ArtifactEvidenceError("quarantine path escapes its root") from error
    return child


def _safe_artifact_child(root: Path, relative: str) -> Path:
    """Join a normalized inventory path without permitting tree escape."""
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ArtifactEvidenceError("quarantine artifact path is unsafe")
    parts = relative.split("/")
    if any(not part or part in {".", ".."} or ":" in part for part in parts):
        raise ArtifactEvidenceError("quarantine artifact path is unsafe")
    target = root.joinpath(*parts)
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ArtifactEvidenceError("quarantine artifact path escapes its root") from error
    return target


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


def _assert_safe_directory(directory: Path) -> None:
    if _is_reparse(directory) or not directory.is_dir():
        raise ArtifactEvidenceError("quarantine directory is unsafe")


def _mkdir_safe_path(directory: Path) -> None:
    """Create a root only while checking every existing ancestor for reparse."""
    anchor = Path(directory.anchor)
    _assert_safe_directory(anchor)
    try:
        parts = directory.relative_to(anchor).parts
    except ValueError as error:
        raise ArtifactEvidenceError("quarantine root is unsafe") from error
    current = anchor
    for part in parts:
        current = current / part
        if os.path.lexists(current):
            _assert_safe_directory(current)
            continue
        try:
            current.mkdir()
        except OSError as error:
            raise ArtifactEvidenceError("quarantine root cannot be created safely") from error
        _assert_safe_directory(current)


def _ensure_windows_directory(
    directory: Path, handle_io: WindowsHandleTreeIo,
) -> None:
    missing: list[str] = []
    anchor = directory
    while not os.path.lexists(anchor):
        if anchor.parent == anchor or not anchor.name:
            raise ArtifactEvidenceError("quarantine root cannot be created safely")
        missing.append(anchor.name)
        anchor = anchor.parent
    _assert_safe_directory(anchor)
    if missing:
        handle_io.ensure_directory_chain(anchor, tuple(reversed(missing)))


def _assert_safe_directory_chain(root: Path, directory: Path) -> None:
    current = directory
    while current != root:
        _assert_safe_directory(current)
        current = current.parent
    _assert_safe_directory(root)


def _write_bytes_exclusive(path: Path, content: bytes) -> None:
    if _is_reparse(path):
        raise ArtifactEvidenceError("quarantine target is unsafe")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as error:
        raise ArtifactEvidenceError("quarantine artifact write failed") from error
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    _chmod_file(path)


def _read_bytes_no_follow(path: Path, *, max_bytes: int | None = None) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise ArtifactEvidenceError("quarantine artifact read failed") from error
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            if max_bytes is None:
                return handle.read()
            content = handle.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise ArtifactEvidenceError("quarantine receipt is invalid")
            return content
    finally:
        os.close(fd)


def _chmod_file(path: Path) -> None:
    try:
        os.chmod(path, 0o400)
    except OSError as error:
        raise ArtifactEvidenceError("quarantine artifact seal failed") from error


def _chmod_directory(path: Path, *, writable: bool = False) -> None:
    try:
        os.chmod(path, 0o700 if writable else 0o500)
    except OSError as error:
        raise ArtifactEvidenceError("quarantine directory seal failed") from error


def _seal_tree(root: Path) -> None:
    for current, directories, _filenames in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in directories:
            _assert_safe_directory(current_path / name)
        _chmod_directory(current_path)


def _fsync_directory(directory: Path) -> None:
    # Windows does not support opening a directory descriptor.  The file fsync
    # above is still required; directory fsync is a best-effort POSIX barrier.
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
