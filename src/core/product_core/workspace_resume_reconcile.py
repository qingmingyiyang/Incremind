"""Read-only workspace reconciliation for paired-device session resume.

The module deliberately accepts *manifests*, never filesystem paths or a
workspace root.  It produces a deterministic plan which a future host-owned
Effect, after a fresh confirmation, may use as an input.  It cannot apply the
plan, write files, follow links, or delete anything.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal


class WorkspaceResumeReconcileError(ValueError):
    """Raised when an unportable or unsafe workspace manifest is supplied."""


ReconcileClassification = Literal["unchanged", "add", "modify", "delete", "conflict"]


@dataclass(frozen=True, slots=True)
class WorkspaceManifestEntry:
    """A portable, content-addressed workspace entry.

    ``base_revision`` is an opaque revision identifier.  The reconciler does
    not resolve it or inspect a repository; it is retained as evidence for a
    later host-owned operation.
    """

    digest: str
    size_bytes: int
    base_revision: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceReconcileEntry:
    relative_path: str
    classification: ReconcileClassification
    source: WorkspaceManifestEntry | None
    host: WorkspaceManifestEntry | None
    base: WorkspaceManifestEntry | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WorkspaceReconcilePlan:
    """A plan-only reconciliation result; it is never an execution grant."""

    mode: Literal["plan_only"]
    entries: tuple[WorkspaceReconcileEntry, ...]

    @property
    def classifications(self) -> Mapping[str, tuple[str, ...]]:
        buckets: dict[str, list[str]] = {
            "unchanged": [],
            "add": [],
            "modify": [],
            "delete": [],
            "conflict": [],
        }
        for entry in self.entries:
            buckets[entry.classification].append(entry.relative_path)
        return {classification: tuple(paths) for classification, paths in buckets.items()}

    @property
    def has_conflicts(self) -> bool:
        return bool(self.classifications["conflict"])

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "entries": [entry.as_dict() for entry in self.entries],
            "classifications": {key: list(value) for key, value in self.classifications.items()},
            "has_conflicts": self.has_conflicts,
            "apply_supported": False,
            "apply_boundary": "Requires a host-owned Effect and a fresh user confirmation.",
        }


_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_SECRET_COMPONENT = re.compile(
    r"(?:^|[._-])(?:api[_-]?key|credential|cookie|id_rsa|passphrase|password|private[_-]?key|secret|token)(?:$|[._-])",
    re.IGNORECASE,
)
_SECRET_FILENAMES = {".env", ".npmrc", ".pypirc", "credentials", "known_secrets"}
_MANIFEST_FIELDS = frozenset({"digest", "size_bytes", "base_revision"})


def reconcile_workspace_manifests(
    *,
    source_manifest: Mapping[str, Mapping[str, object]],
    host_manifest: Mapping[str, Mapping[str, object]],
    base_manifest: Mapping[str, Mapping[str, object]],
    mode: Literal["plan_only"] = "plan_only",
) -> WorkspaceReconcilePlan:
    """Classify a source resume manifest against host and common-base states.

    A path is a conflict only where the host and source both changed it from
    their common base and did not converge to the same manifest entry.  All
    output ordering is lexical by POSIX relative path.
    """
    if mode != "plan_only":
        raise WorkspaceResumeReconcileError("workspace reconciliation supports plan_only mode only")
    source = _normalize_manifest(source_manifest, label="source_manifest")
    host = _normalize_manifest(host_manifest, label="host_manifest")
    base = _normalize_manifest(base_manifest, label="base_manifest")
    paths = sorted(set(source) | set(host) | set(base))
    return WorkspaceReconcilePlan(
        mode="plan_only",
        entries=tuple(
            WorkspaceReconcileEntry(
                relative_path=path,
                classification=_classify(source.get(path), host.get(path), base.get(path)),
                source=source.get(path),
                host=host.get(path),
                base=base.get(path),
            )
            for path in paths
        ),
    )


def _normalize_manifest(
    manifest: Mapping[str, Mapping[str, object]], *, label: str
) -> dict[str, WorkspaceManifestEntry]:
    if not isinstance(manifest, Mapping):
        raise WorkspaceResumeReconcileError(f"{label} must be a path-to-entry mapping")
    normalized: dict[str, WorkspaceManifestEntry] = {}
    for path, value in manifest.items():
        clean_path = validate_workspace_relative_path(path, label=label)
        normalized[clean_path] = _validate_entry(value, path=clean_path, label=label)
    return normalized


def validate_workspace_relative_path(value: object, *, label: str = "workspace manifest") -> str:
    """Validate the shared portable-path and sensitive-name policy.

    Resume Bundle serialization and reconciliation use this same policy so a
    paired-device packet cannot advertise a file that reconciliation would
    subsequently reject as sensitive or non-portable.
    """
    if not isinstance(value, str) or not value or value.strip() != value:
        raise WorkspaceResumeReconcileError(f"{label} path must be a non-empty relative path")
    if "\\" in value or value.startswith("/") or _DRIVE_PATH.match(value) or value.startswith("file:"):
        raise WorkspaceResumeReconcileError(f"{label} path must use a portable relative POSIX path")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise WorkspaceResumeReconcileError(f"{label} path traversal is not allowed")
    if any(component.lower() in _SECRET_FILENAMES or _SECRET_COMPONENT.search(component) for component in components):
        raise WorkspaceResumeReconcileError(f"{label} path appears to contain secret material")
    return value


def _validate_entry(value: object, *, path: str, label: str) -> WorkspaceManifestEntry:
    if not isinstance(value, Mapping):
        raise WorkspaceResumeReconcileError(f"{label} entry for {path} must be an object")
    unknown = set(value) - _MANIFEST_FIELDS
    if unknown:
        raise WorkspaceResumeReconcileError(
            f"{label} entry for {path} contains unsupported field: {sorted(unknown)[0]}"
        )
    digest = value.get("digest")
    size_bytes = value.get("size_bytes")
    base_revision = value.get("base_revision")
    if not isinstance(digest, str) or not digest or digest.strip() != digest:
        raise WorkspaceResumeReconcileError(f"{label} entry for {path} requires digest")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
        raise WorkspaceResumeReconcileError(f"{label} entry for {path} requires non-negative size_bytes")
    if base_revision is not None and (not isinstance(base_revision, str) or not base_revision or base_revision.strip() != base_revision):
        raise WorkspaceResumeReconcileError(f"{label} entry for {path} base_revision must be a non-empty string or null")
    return WorkspaceManifestEntry(digest=digest, size_bytes=size_bytes, base_revision=base_revision)


def _classify(
    source: WorkspaceManifestEntry | None,
    host: WorkspaceManifestEntry | None,
    base: WorkspaceManifestEntry | None,
) -> ReconcileClassification:
    source_changed = source != base
    host_changed = host != base
    if source_changed and host_changed:
        return "unchanged" if source == host else "conflict"
    if not source_changed:
        return "unchanged"
    if source is None:
        return "delete"
    return "add" if base is None else "modify"
