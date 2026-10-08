"""Immutable, verified copies of bundled capability code.

The catalog stores declarations; this module stores the executable closure those
declarations name.  It deliberately has no knowledge of any capability id.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class CapabilityArtifactError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CapabilityArtifact:
    capability_id: str
    capability_revision: str
    artifact_id: str
    root: Path
    metadata: dict[str, object]


class CapabilityArtifactStore:
    """Captures only Python source and a manifest in a content-addressed bundle."""

    MAX_FILES = 128
    MAX_FILE_BYTES = 512 * 1024
    MAX_TOTAL_BYTES = 4 * 1024 * 1024
    MAX_DEPTH = 8
    _METADATA_VERSION = 1

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        if _is_link_or_reparse(self.root):
            raise CapabilityArtifactError("capability_artifact_store_link_forbidden")
        # Narrow fault-injection seam: publication precedes the durable pointer.
        self._before_publish: Callable[[Path, Path], None] = lambda _staging, _destination: None

    def capture(self, package_root: Path, *, capability_id: str, capability_revision: str) -> CapabilityArtifact:
        source_input = Path(package_root)
        if not source_input.is_absolute() or _is_link_or_reparse(source_input):
            raise CapabilityArtifactError("capability_artifact_root_invalid")
        try:
            source = source_input.resolve(strict=True)
        except OSError as exc:
            raise CapabilityArtifactError("capability_artifact_root_invalid") from exc
        if not source.is_dir() or _is_link_or_reparse(source):
            raise CapabilityArtifactError("capability_artifact_root_invalid")
        contents: list[tuple[str, bytes]] = []
        total = 0
        for base, directories, names in os.walk(source, followlinks=False):
            base_path = Path(base)
            if _is_link_or_reparse(base_path):
                raise CapabilityArtifactError("capability_artifact_link_forbidden")
            relative_base = base_path.relative_to(source)
            if relative_base != Path(".") and _unsafe_relative(relative_base):
                raise CapabilityArtifactError("capability_artifact_hidden_or_deep_path")
            kept: list[str] = []
            for directory in sorted(directories):
                candidate = base_path / directory
                relative = candidate.relative_to(source)
                if _is_link_or_reparse(candidate):
                    raise CapabilityArtifactError("capability_artifact_link_forbidden")
                if directory == "__pycache__":
                    continue
                if _unsafe_relative(relative):
                    raise CapabilityArtifactError("capability_artifact_hidden_or_deep_path")
                kept.append(directory)
            directories[:] = kept
            for name in sorted(names):
                path = base_path / name
                relative = path.relative_to(source)
                if name.endswith(".pyc") and "__pycache__" in relative.parts:
                    continue
                if _is_link_or_reparse(path):
                    raise CapabilityArtifactError("capability_artifact_link_forbidden")
                if _unsafe_relative(relative):
                    raise CapabilityArtifactError("capability_artifact_hidden_or_deep_path")
                relative_name = relative.as_posix()
                if relative_name == "metadata.json":
                    raise CapabilityArtifactError("capability_artifact_metadata_collision")
                if relative_name != "manifest.json" and path.suffix != ".py":
                    raise CapabilityArtifactError("capability_artifact_unapproved_file")
                data = _read_checked(path, self.MAX_FILE_BYTES)
                total += len(data)
                if total > self.MAX_TOTAL_BYTES:
                    raise CapabilityArtifactError("capability_artifact_too_large")
                contents.append((relative_name, data))
                if len(contents) > self.MAX_FILES:
                    raise CapabilityArtifactError("capability_artifact_too_large")
        contents.sort(key=lambda item: item[0])
        if not any(relative == "manifest.json" for relative, _data in contents):
            raise CapabilityArtifactError("capability_artifact_manifest_missing")
        entries = [{"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for relative, data in contents]
        artifact_id = _artifact_id(capability_id, capability_revision, entries)
        destination = self._destination(capability_id, capability_revision, artifact_id)
        self._assert_managed_path(destination)
        if destination.exists():
            return self.verify(capability_id, capability_revision, artifact_id)
        staging = destination.with_name(destination.name + ".staging-" + uuid.uuid4().hex)
        try:
            staging.mkdir(parents=True)
            for relative, data in contents:
                target = staging.joinpath(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            metadata = {"artifact_schema_version": self._METADATA_VERSION, "capability_id": capability_id, "capability_revision": capability_revision, "artifact_id": artifact_id, "files": entries}
            (staging / "metadata.json").write_text(json.dumps(metadata, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            self._before_publish(staging, destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._assert_managed_path(destination.parent)
            try:
                os.replace(staging, destination)
            except OSError:
                if destination.exists():
                    return self.verify(
                        capability_id, capability_revision, artifact_id,
                    )
                raise
            return self.verify(capability_id, capability_revision, artifact_id)
        except CapabilityArtifactError:
            raise
        except OSError as exc:
            raise CapabilityArtifactError("capability_artifact_materialize_failed") from exc
        finally:
            if staging.exists():
                _safe_remove_tree(staging, self.root)

    def verify(self, capability_id: str, capability_revision: str, artifact_id: str) -> CapabilityArtifact:
        destination = self._destination(capability_id, capability_revision, artifact_id)
        if not destination.is_dir() or _is_link_or_reparse(destination):
            raise CapabilityArtifactError("capability_artifact_unavailable")
        try:
            metadata = json.loads((destination / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CapabilityArtifactError("capability_artifact_unavailable") from exc
        files = self._validate_metadata(metadata, capability_id, capability_revision, artifact_id)
        actual: set[str] = set()
        for base, directories, names in os.walk(destination, followlinks=False):
            base_path = Path(base)
            if _is_link_or_reparse(base_path):
                raise CapabilityArtifactError("capability_artifact_drift")
            kept: list[str] = []
            for directory in sorted(directories):
                candidate = base_path / directory
                if _is_link_or_reparse(candidate):
                    raise CapabilityArtifactError("capability_artifact_drift")
                if directory != "__pycache__":
                    kept.append(directory)
            directories[:] = kept
            for name in names:
                path = base_path / name
                if name.endswith(".pyc") and "__pycache__" in path.relative_to(destination).parts:
                    continue
                if _is_link_or_reparse(path) or not path.is_file():
                    raise CapabilityArtifactError("capability_artifact_drift")
                actual.add(path.relative_to(destination).as_posix())
        expected = {str(item["path"]) for item in files} | {"metadata.json"}
        if actual != expected:
            raise CapabilityArtifactError("capability_artifact_drift")
        for entry in files:
            path = destination.joinpath(*str(entry["path"]).split("/"))
            data = _read_checked(path, self.MAX_FILE_BYTES)
            if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise CapabilityArtifactError("capability_artifact_drift")
        return CapabilityArtifact(capability_id, capability_revision, artifact_id, destination, metadata)

    def _assert_managed_path(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise CapabilityArtifactError(
                "capability_artifact_identity_invalid"
            ) from exc
        cursor = self.root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.exists() and _is_link_or_reparse(cursor):
                raise CapabilityArtifactError(
                    "capability_artifact_store_link_forbidden"
                )

    def _destination(self, capability_id: str, capability_revision: str, artifact_id: str) -> Path:
        if not _safe_identity(capability_id) or not _safe_identity(capability_revision) or not _safe_digest(artifact_id):
            raise CapabilityArtifactError("capability_artifact_identity_invalid")
        destination = self.root / capability_id / capability_revision / artifact_id[:24]
        try:
            destination.relative_to(self.root)
        except ValueError as exc:
            raise CapabilityArtifactError("capability_artifact_identity_invalid") from exc
        return destination

    def _validate_metadata(self, metadata: object, capability_id: str, capability_revision: str, artifact_id: str) -> list[dict[str, object]]:
        if not isinstance(metadata, dict) or set(metadata) != {"artifact_schema_version", "capability_id", "capability_revision", "artifact_id", "files"}:
            raise CapabilityArtifactError("capability_artifact_metadata_invalid")
        if metadata["artifact_schema_version"] != self._METADATA_VERSION or metadata["capability_id"] != capability_id or metadata["capability_revision"] != capability_revision or metadata["artifact_id"] != artifact_id or not isinstance(metadata["files"], list):
            raise CapabilityArtifactError("capability_artifact_identity_drift")
        entries: list[dict[str, object]] = []
        last_path = ""
        total = 0
        for entry in metadata["files"]:
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
                raise CapabilityArtifactError("capability_artifact_metadata_invalid")
            path, digest, size = entry["path"], entry["sha256"], entry["size"]
            if not isinstance(path, str) or not _safe_artifact_path(path) or not isinstance(digest, str) or not _safe_digest(digest) or not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= self.MAX_FILE_BYTES or path <= last_path:
                raise CapabilityArtifactError("capability_artifact_metadata_invalid")
            last_path = path
            total += size
            entries.append({"path": path, "sha256": digest, "size": size})
        if not entries or len(entries) > self.MAX_FILES or total > self.MAX_TOTAL_BYTES or not any(entry["path"] == "manifest.json" for entry in entries):
            raise CapabilityArtifactError("capability_artifact_metadata_invalid")
        if _artifact_id(capability_id, capability_revision, entries) != artifact_id:
            raise CapabilityArtifactError("capability_artifact_identity_drift")
        return entries


def _artifact_id(capability_id: str, capability_revision: str, entries: list[dict[str, object]]) -> str:
    evidence = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256((capability_id + "\0" + capability_revision + "\0" + evidence).encode()).hexdigest()


def _read_checked(path: Path, limit: int) -> bytes:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or _is_link_or_reparse(path):
            raise CapabilityArtifactError("capability_artifact_link_forbidden")
        if before.st_size > limit:
            raise CapabilityArtifactError("capability_artifact_file_too_large")
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > limit:
                raise CapabilityArtifactError(
                    "capability_artifact_file_too_large"
                )
            data = handle.read(limit + 1)
            after_open = os.fstat(handle.fileno())
        after_path = path.lstat()
    except OSError as exc:
        raise CapabilityArtifactError("capability_artifact_read_failed") from exc
    if len(data) > limit:
        raise CapabilityArtifactError("capability_artifact_file_too_large")
    if not (
        _same_file_state(before, opened)
        and _same_file_state(opened, after_open)
        and _same_file_state(after_open, after_path)
        and not _is_link_or_reparse(path)
    ):
        raise CapabilityArtifactError("capability_artifact_read_drift")
    return data


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns,
    ) == (
        right.st_dev, right.st_ino, right.st_size, right.st_mtime_ns,
    )


def _unsafe_relative(relative: Path) -> bool:
    return len(relative.parts) > CapabilityArtifactStore.MAX_DEPTH or any(part.startswith(".") for part in relative.parts)


def _safe_artifact_path(path: str) -> bool:
    candidate = Path(path)
    return (
        path != "metadata.json" and candidate.as_posix() == path
        and not candidate.is_absolute() and bool(candidate.parts)
        and ".." not in candidate.parts and not _unsafe_relative(candidate)
        and (path == "manifest.json" or path.endswith(".py"))
    )


def _safe_identity(value: str) -> bool:
    candidate = Path(value)
    return (
        bool(value) and not candidate.is_absolute()
        and len(candidate.parts) == 1 and candidate.name == value
        and value not in {".", ".."}
    )


def _safe_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_remove_tree(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        return
    if _is_link_or_reparse(path):
        return
    try:
        shutil.rmtree(path)
    except OSError:
        pass
