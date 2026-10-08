from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

from .models import ContextGraphSnapshot


class AuthorizedContextFileError(ValueError):
    """Raised when a Context Graph file grant is absent, forged or has drifted."""


_AUTHORIZED_CONTEXT_FILE_ISSUER = object()
_CONTEXT_GRAPH_IMPORT_PURPOSE = "context_graph_import"


def _resolved_file(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AuthorizedContextFileError("authorized_context_file_not_found") from exc
    if not resolved.is_file():
        raise AuthorizedContextFileError("authorized_context_file_not_regular")
    return resolved


def _file_revision(path: Path) -> str:
    stat = path.stat()
    return f"mtime-{stat.st_mtime_ns}:size-{stat.st_size}"


@dataclass(frozen=True, slots=True)
class AuthorizedContextFile:
    """Opaque, project-scoped authorization to read one external graph file.

    Instances are issued only by :func:`issue_authorized_context_file`.  The
    token is identity-checked so a caller cannot turn an arbitrary ``Path``
    into a grant just by constructing this dataclass.
    """

    resolved_path: Path
    project_id: str
    purpose: str
    importer_ids: tuple[str, ...]
    source_revision: str
    _issuer_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer_token is not _AUTHORIZED_CONTEXT_FILE_ISSUER:
            raise AuthorizedContextFileError("authorized_context_file_forged")
        if not isinstance(self.resolved_path, Path) or not self.resolved_path.is_absolute():
            raise AuthorizedContextFileError("authorized_context_file_path_invalid")
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise AuthorizedContextFileError("authorized_context_file_project_required")
        if self.purpose != _CONTEXT_GRAPH_IMPORT_PURPOSE:
            raise AuthorizedContextFileError("authorized_context_file_wrong_purpose")
        if (
            type(self.importer_ids) is not tuple
            or not self.importer_ids
            or any(not isinstance(item, str) or not item.strip() for item in self.importer_ids)
            or len(self.importer_ids) != len(set(self.importer_ids))
        ):
            raise AuthorizedContextFileError("authorized_context_file_importer_required")
        if not isinstance(self.source_revision, str) or not self.source_revision.strip():
            raise AuthorizedContextFileError("authorized_context_file_revision_required")

    def validate_for(self, *, project_id: str, importer_id: str) -> Path:
        if not isinstance(project_id, str) or not project_id.strip():
            raise AuthorizedContextFileError("authorized_context_file_project_required")
        if not isinstance(importer_id, str) or not importer_id.strip():
            raise AuthorizedContextFileError("authorized_context_file_importer_required")
        if self.purpose != _CONTEXT_GRAPH_IMPORT_PURPOSE:
            raise AuthorizedContextFileError("authorized_context_file_wrong_purpose")
        if self.project_id != project_id:
            raise AuthorizedContextFileError("authorized_context_file_project_scope_violation")
        if importer_id not in self.importer_ids:
            raise AuthorizedContextFileError("authorized_context_file_importer_not_permitted")
        path = _resolved_file(self.resolved_path)
        if path != self.resolved_path:
            raise AuthorizedContextFileError("authorized_context_file_path_drift")
        if _file_revision(path) != self.source_revision:
            raise AuthorizedContextFileError("authorized_context_file_revision_drift")
        return path


def issue_authorized_context_file(
    path: Path,
    *,
    project_id: str,
    importer_ids: Iterable[str],
    allowed_paths: Iterable[Path] = (),
    allowed_roots: Iterable[Path] = (),
) -> AuthorizedContextFile:
    """Issue one explicit graph-import grant after allow-list scope validation.

    A caller must present either the selected file itself in ``allowed_paths``
    or a selected directory in ``allowed_roots``.  Paths are resolved before
    comparison, so a symlink cannot escape the user-authorized scope.
    """

    if not isinstance(project_id, str):
        raise AuthorizedContextFileError("authorized_context_file_project_required")
    if isinstance(importer_ids, (str, bytes)):
        raise AuthorizedContextFileError("authorized_context_file_importer_required")
    clean_project_id = project_id.strip()
    if not clean_project_id:
        raise AuthorizedContextFileError("authorized_context_file_project_required")
    clean_importers = tuple(sorted({item.strip() for item in importer_ids if isinstance(item, str) and item.strip()}))
    if not clean_importers:
        raise AuthorizedContextFileError("authorized_context_file_importer_required")
    resolved = _resolved_file(path)
    explicit_paths = {_resolved_file(item) for item in allowed_paths}
    roots: set[Path] = set()
    for root in allowed_roots:
        try:
            resolved_root = root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AuthorizedContextFileError("authorized_context_file_root_not_found") from exc
        if not resolved_root.is_dir():
            raise AuthorizedContextFileError("authorized_context_file_root_not_directory")
        roots.add(resolved_root)
    if not explicit_paths and not roots:
        raise AuthorizedContextFileError("authorized_context_file_allow_scope_required")
    within_root = any(resolved.is_relative_to(root) for root in roots)
    if resolved not in explicit_paths and not within_root:
        raise AuthorizedContextFileError("authorized_context_file_outside_allow_scope")
    return AuthorizedContextFile(
        resolved_path=resolved,
        project_id=clean_project_id,
        purpose=_CONTEXT_GRAPH_IMPORT_PURPOSE,
        importer_ids=clean_importers,
        source_revision=_file_revision(resolved),
        _issuer_token=_AUTHORIZED_CONTEXT_FILE_ISSUER,
    )


@dataclass(frozen=True, slots=True)
class ImportLimits:
    max_file_bytes: int = 8 * 1024 * 1024
    max_nodes: int = 2_000
    max_edges: int = 8_000
    max_node_content_chars: int = 200_000
    max_total_content_chars: int = 2_000_000
    max_depth: int = 128
    secret_canaries: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        numeric = (
            self.max_file_bytes,
            self.max_nodes,
            self.max_edges,
            self.max_node_content_chars,
            self.max_total_content_chars,
            self.max_depth,
        )
        if any(type(value) is not int or value < 0 for value in numeric):
            raise ValueError("invalid_context_import_limits")
        if (
            type(self.secret_canaries) is not tuple
            or any(not isinstance(value, str) or not value for value in self.secret_canaries)
        ):
            raise ValueError("invalid_context_import_secret_canaries")


@runtime_checkable
class ContextGraphImporter(Protocol):
    importer_id: str
    importer_revision: str

    def import_authorized_file(
        self,
        *,
        grant: AuthorizedContextFile | None = None,
        authorized_path: Path | None = None,
        project_id: str | None = None,
        limits: ImportLimits = ImportLimits(),
    ) -> ContextGraphSnapshot: ...


@runtime_checkable
class ContextGraphExporter(Protocol):
    exporter_id: str
    exporter_revision: str

    def export_snapshot(self, snapshot: ContextGraphSnapshot) -> str: ...
