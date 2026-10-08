"""Core-owned, read-only composition for authorized LineMap graph imports.

The service deliberately accepts an already-confirmed local file selection, but
does not trust the caller's claimed format, project scope, content grant, or
graph predecessor.  Format-specific parsing remains behind an injected
capability registry; this module never interprets a concrete graph format or
external text itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from backend.api.context_graph_snapshot_runtime import (
    ContextGraphSnapshotConflict,
    ContextGraphSnapshotRecord,
    ContextGraphSnapshotRepository,
    ContextGraphSnapshotRepositoryError,
)
from core.context_graph import (
    AuthorizedContextFileError,
    ContextGraphImportRegistration,
    ContextGraphImporter,
    ContextGraphSnapshot,
    ContextGraphValidationError,
    ContextPermissionGrant,
    ImportLimits,
    issue_authorized_context_file,
)
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError


class ContextGraphImportRegistry(Protocol):
    """Capability-owned format lookup, injected into this Core composition."""

    def resolve(self, source_type: str) -> "ContextGraphImportRegistration | None": ...


class ContextGraphFileSelectionError(ValueError):
    """A trusted local-file selection could not be consumed safely."""


class ContextGraphImporterExecutionError(RuntimeError):
    """An importer violated its fail-closed exception contract."""


class ContextGraphFileSelectionResolver(Protocol):
    """Consumes one Core-issued, project-bound local file selection."""

    def consume(
        self,
        selection_id: str,
        *,
        project_id: str,
        source_type: str,
        actor_id: str,
        session_instance_id: str,
    ) -> "ContextGraphFileSelection | None": ...


class Clock(Protocol):
    def __call__(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ContextGraphImportRequest:
    """Strict server-side request after a UI has obtained a file selection."""

    project_id: str
    source_type: str
    command_id: str
    selection_id: str
    actor_id: str
    session_instance_id: str
    confirm_read: bool
    expected_predecessor: str | None = None


@dataclass(frozen=True, slots=True)
class ContextGraphFileSelection:
    """Trusted result of a local file picker; never supplied by the renderer."""

    selection_id: str
    project_id: str
    selected_path: Path
    source_revision: str
    selection_evidence_ref: str


@dataclass(frozen=True, slots=True)
class ContextGraphImportEvidence:
    evidence_ref: str
    command_id: str
    project_id: str
    selection_id: str
    expected_predecessor: str | None
    graph_id: str
    graph_revision: str
    source_type: str
    source_revision: str
    authorized_file_revision: str
    importer_id: str
    importer_revision: str
    capability_id: str
    capability_revision: str
    actor_id: str
    selection_evidence_ref: str
    imported_at: str


@dataclass(frozen=True, slots=True)
class ContextGraphImportPreview:
    """Read-only response metadata; external source text is never executed."""

    project_id: str
    graph_id: str
    graph_revision: str
    source_type: str
    source_revision: str
    node_count: int
    edge_count: int
    selected_outputs: tuple[str, ...]
    token_estimate: int
    integrity_issue_codes: tuple[str, ...]
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class ContextGraphImportResult:
    record: ContextGraphSnapshotRecord | None = None
    preview: ContextGraphImportPreview | None = None
    evidence: ContextGraphImportEvidence | None = None
    error_code: str | None = None
    error_detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None


class SQLiteContextGraphImportEvidenceRepository:
    """Append-only Core evidence for an authorized external file read."""

    _EVIDENCE = "context_graph_import_evidence"
    _SEQUENCES = "context_graph_import_evidence_sequences"
    _SEQUENCE_ID = "sequence"
    _SCHEMA = "1.0.0"

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise ValueError("context_graph_import_evidence_store_invalid")
        self._records = records

    def record(self, evidence: ContextGraphImportEvidence) -> ContextGraphImportEvidence:
        _validate_evidence(evidence)
        try:
            with self._records.begin() as uow:
                existing = self._lookup_from_records(uow.list(self._EVIDENCE), evidence.command_id)
                if existing is not None:
                    if _same_command(existing, evidence):
                        uow.commit()
                        return existing
                    raise ValueError("context_graph_import_command_identity_drift")
                number = self._next_sequence(uow)
                stored = ContextGraphImportEvidence(
                    evidence_ref=f"context-import-evidence-{number}",
                    command_id=evidence.command_id,
                    project_id=evidence.project_id,
                    selection_id=evidence.selection_id,
                    expected_predecessor=evidence.expected_predecessor,
                    graph_id=evidence.graph_id,
                    graph_revision=evidence.graph_revision,
                    source_type=evidence.source_type,
                    source_revision=evidence.source_revision,
                    authorized_file_revision=evidence.authorized_file_revision,
                    importer_id=evidence.importer_id,
                    importer_revision=evidence.importer_revision,
                    capability_id=evidence.capability_id,
                    capability_revision=evidence.capability_revision,
                    actor_id=evidence.actor_id,
                    selection_evidence_ref=evidence.selection_evidence_ref,
                    imported_at=evidence.imported_at,
                )
                uow.put(self._EVIDENCE, stored.evidence_ref, _evidence_payload(stored), expected_revision=0)
                uow.commit()
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise ValueError("context_graph_import_evidence_write_conflict") from error
        return stored

    def read(self, evidence_ref: str) -> ContextGraphImportEvidence | None:
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise ValueError("context_graph_import_evidence_ref_invalid")
        record = self._records.read(self._EVIDENCE, evidence_ref)
        return _evidence_from_payload(record.payload) if record is not None else None

    def lookup(self, command_id: str) -> ContextGraphImportEvidence | None:
        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("context_graph_import_command_invalid")
        return self._lookup_from_records(self._records.list(self._EVIDENCE), command_id)

    @staticmethod
    def _lookup_from_records(records, command_id: str) -> ContextGraphImportEvidence | None:
        matches = [
            evidence for evidence in (_evidence_from_payload(item.payload) for item in records)
            if evidence.command_id == command_id
        ]
        if len(matches) > 1:
            raise ValueError("context_graph_import_command_ambiguous")
        return matches[0] if matches else None

    def _next_sequence(self, uow) -> int:
        record = uow.read(self._SEQUENCES, self._SEQUENCE_ID)
        if record is None:
            number, expected = 1, 0
        else:
            payload = record.payload
            if set(payload) != {"schema_version", "next"} or payload.get("schema_version") != self._SCHEMA or type(payload.get("next")) is not int or payload["next"] < 1:
                raise ValueError("context_graph_import_evidence_sequence_invalid")
            number, expected = payload["next"], record.revision
        uow.put(self._SEQUENCES, self._SEQUENCE_ID, {"schema_version": self._SCHEMA, "next": number + 1}, expected_revision=expected)
        return number


class ContextGraphImportService:
    """Import one explicitly authorized graph file into immutable Core storage.

    This is intentionally not a model, Effect, secret, formal-object writer,
    or background runner.  A route may add UI-specific field filtering later;
    the service itself only accepts its narrow dataclass contract.
    """

    def __init__(
        self,
        snapshots: ContextGraphSnapshotRepository,
        registry: ContextGraphImportRegistry,
        evidence: SQLiteContextGraphImportEvidenceRepository,
        selections: ContextGraphFileSelectionResolver,
        *,
        limits: ImportLimits,
        clock: Clock,
    ) -> None:
        self._snapshots = snapshots
        self._registry = registry
        self._evidence = evidence
        self._selections = selections
        if not isinstance(limits, ImportLimits):
            raise ValueError("context_graph_import_limits_invalid")
        self._limits = limits
        if not callable(clock):
            raise ValueError("context_graph_import_clock_invalid")
        self._clock = clock

    def import_file(self, request: ContextGraphImportRequest) -> ContextGraphImportResult:
        try:
            self._validate_request(request)
            registration = self._registry.resolve(request.source_type)
            if registration is None:
                return _failure("importer_unavailable")
            self._validate_registration(registration)
            if registration.source_type != request.source_type:
                raise ValueError("context_graph_importer_source_type_drift")
            replay = self._idempotent_result(request, registration)
            if replay is not None:
                return replay
            selection = self._selections.consume(
                request.selection_id, project_id=request.project_id,
                source_type=request.source_type, actor_id=request.actor_id,
                session_instance_id=request.session_instance_id,
            )
            self._validate_selection(request, selection)
            grant = issue_authorized_context_file(
                selection.selected_path,
                project_id=request.project_id,
                importer_ids=(registration.importer.importer_id,),
                allowed_paths=(selection.selected_path,),
            )
            if grant.source_revision != selection.source_revision:
                raise ValueError("context_graph_import_selection_revision_drift")
            try:
                snapshot = registration.importer.import_authorized_file(
                    grant=grant, limits=self._limits,
                )
            except (AuthorizedContextFileError, ContextGraphValidationError):
                raise
            except Exception as error:
                raise ContextGraphImporterExecutionError(
                    "context_graph_importer_execution_unavailable"
                ) from error
            self._validate_snapshot_authority(request, registration, grant.source_revision, snapshot)
            permission = ContextPermissionGrant(
                project_id=snapshot.project_id,
                permission_revision=f"import:{snapshot.source_revision}",
                allowed_content_refs=frozenset(node.content_ref for node in snapshot.nodes),
            )
            evidence = self._evidence.record(ContextGraphImportEvidence(
                evidence_ref="pending",
                command_id=request.command_id,
                project_id=request.project_id,
                selection_id=selection.selection_id,
                expected_predecessor=request.expected_predecessor,
                graph_id=snapshot.graph_id,
                graph_revision=snapshot.graph_revision,
                source_type=request.source_type,
                source_revision=snapshot.source_revision,
                authorized_file_revision=grant.source_revision,
                importer_id=registration.importer.importer_id,
                importer_revision=registration.importer.importer_revision,
                capability_id=registration.capability_id,
                capability_revision=registration.capability_revision,
                actor_id=request.actor_id,
                selection_evidence_ref=selection.selection_evidence_ref,
                imported_at=self._imported_at(),
            ))
            record = self._snapshots.append(
                snapshot,
                request.expected_predecessor,
                capability_id=registration.capability_id,
                capability_revision=registration.capability_revision,
                permission_grant=permission,
                permission_evidence_refs=(evidence.evidence_ref,),
            )
            return ContextGraphImportResult(record, _preview(record, evidence.evidence_ref), evidence)
        except AuthorizedContextFileError as error:
            return _failure("file_authorization_rejected", str(error))
        except ContextGraphValidationError as error:
            return _failure("importer_rejected", ";".join(error.issues))
        except ContextGraphFileSelectionError as error:
            return _failure("selection_rejected", str(error))
        except ContextGraphImporterExecutionError:
            return _failure("importer_execution_unavailable")
        except ContextGraphSnapshotConflict as error:
            return _failure("snapshot_conflict", str(error))
        except ContextGraphSnapshotRepositoryError as error:
            return _failure("snapshot_rejected", str(error))
        except (TypeError, ValueError) as error:
            return _failure("import_request_invalid", str(error))

    @staticmethod
    def _validate_request(request: object) -> None:
        if not isinstance(request, ContextGraphImportRequest):
            raise ValueError("context_graph_import_request_invalid")
        if any(not isinstance(value, str) or not value.strip() for value in (
            request.project_id, request.source_type, request.command_id,
            request.selection_id, request.actor_id, request.session_instance_id,
        )):
            raise ValueError("context_graph_import_request_identity_invalid")
        if not request.confirm_read:
            raise ValueError("context_graph_import_confirmation_required")
        if request.expected_predecessor is not None and (not isinstance(request.expected_predecessor, str) or not request.expected_predecessor.strip()):
            raise ValueError("context_graph_import_predecessor_invalid")

    @staticmethod
    def _validate_selection(request: ContextGraphImportRequest, selection: object) -> None:
        if not isinstance(selection, ContextGraphFileSelection):
            raise ValueError("context_graph_import_selection_unavailable")
        if (selection.selection_id, selection.project_id) != (request.selection_id, request.project_id):
            raise ValueError("context_graph_import_selection_scope_drift")
        if not isinstance(selection.selected_path, Path):
            raise ValueError("context_graph_import_selection_invalid")
        if any(not isinstance(value, str) or not value.strip() for value in (selection.source_revision, selection.selection_evidence_ref)):
            raise ValueError("context_graph_import_selection_evidence_missing")

    @staticmethod
    def _validate_registration(registration: object) -> None:
        if not isinstance(registration, ContextGraphImportRegistration) or not isinstance(registration.importer, ContextGraphImporter):
            raise ValueError("context_graph_importer_registration_invalid")
        if any(not isinstance(value, str) or not value.strip() for value in (
            registration.source_type, registration.contribution_id,
            registration.importer.importer_id, registration.importer.importer_revision,
            registration.capability_id, registration.capability_revision,
        )):
            raise ValueError("context_graph_importer_registration_invalid")

    @staticmethod
    def _validate_snapshot_authority(
        request: ContextGraphImportRequest,
        registration: ContextGraphImportRegistration,
        authorized_revision: str,
        snapshot: object,
    ) -> None:
        if not isinstance(snapshot, ContextGraphSnapshot):
            raise ValueError("context_graph_importer_snapshot_invalid")
        if snapshot.project_id != request.project_id:
            raise ValueError("context_graph_import_project_scope_drift")
        if snapshot.source_type != request.source_type:
            raise ValueError("context_graph_import_source_type_drift")
        # ``AuthorizedContextFile`` verifies the selected file revision before
        # and after the importer reads it.  Format-specific source revisions
        # may include a format prefix, so they cannot be compared byte-for-byte
        # to that generic grant revision.
        if not isinstance(authorized_revision, str) or not authorized_revision.strip() or not snapshot.source_revision.strip():
            raise ValueError("context_graph_import_source_revision_drift")
        if (
            snapshot.provenance.importer_id != registration.importer.importer_id
            or snapshot.provenance.importer_revision != registration.importer.importer_revision
        ):
            raise ValueError("context_graph_importer_provenance_drift")

    def _idempotent_result(
        self, request: ContextGraphImportRequest, registration: ContextGraphImportRegistration,
    ) -> ContextGraphImportResult | None:
        evidence = self._evidence.lookup(request.command_id)
        if evidence is None:
            return None
        if (
            evidence.project_id != request.project_id
            or evidence.source_type != request.source_type
            or evidence.selection_id != request.selection_id
            or evidence.expected_predecessor != request.expected_predecessor
            or evidence.actor_id != request.actor_id
            or evidence.capability_id != registration.capability_id
            or evidence.capability_revision != registration.capability_revision
            or evidence.importer_id != registration.importer.importer_id
            or evidence.importer_revision != registration.importer.importer_revision
        ):
            raise ValueError("context_graph_import_command_identity_drift")
        record = self._snapshots.revision(evidence.project_id, evidence.graph_id, evidence.graph_revision)
        if record is None:
            return _failure("snapshot_conflict", "authorized_read_not_committed")
        if (
            record.capability_id != evidence.capability_id
            or record.capability_revision != evidence.capability_revision
            or record.permission_evidence_refs != (evidence.evidence_ref,)
        ):
            raise ValueError("context_graph_import_command_snapshot_drift")
        return ContextGraphImportResult(record, _preview(record, evidence.evidence_ref), evidence)

    def _imported_at(self) -> str:
        value = self._clock()
        if not isinstance(value, str) or not value.strip():
            raise ValueError("context_graph_import_clock_invalid")
        return value


def _failure(code: str, detail: str | None = None) -> ContextGraphImportResult:
    return ContextGraphImportResult(error_code=code, error_detail=detail or code)


def _preview(record: ContextGraphSnapshotRecord, evidence_ref: str) -> ContextGraphImportPreview:
    snapshot = record.snapshot
    return ContextGraphImportPreview(
        project_id=snapshot.project_id,
        graph_id=snapshot.graph_id,
        graph_revision=snapshot.graph_revision,
        source_type=snapshot.source_type,
        source_revision=snapshot.source_revision,
        node_count=len(snapshot.nodes),
        edge_count=len(snapshot.edges),
        selected_outputs=snapshot.selected_outputs,
        token_estimate=snapshot.token_estimate,
        integrity_issue_codes=tuple(issue.code for issue in snapshot.integrity_issues),
        evidence_ref=evidence_ref,
    )


def _validate_evidence(value: object) -> None:
    if not isinstance(value, ContextGraphImportEvidence) or any(
        not isinstance(item, str) or not item.strip()
        for item in (
            value.project_id, value.graph_id, value.graph_revision, value.source_type,
            value.command_id, value.selection_id, value.source_revision, value.authorized_file_revision,
            value.importer_id, value.importer_revision,
            value.capability_id, value.capability_revision, value.actor_id,
            value.selection_evidence_ref, value.imported_at,
        )
    ):
        raise ValueError("context_graph_import_evidence_invalid")
    if value.expected_predecessor is not None and (
        not isinstance(value.expected_predecessor, str) or not value.expected_predecessor.strip()
    ):
        raise ValueError("context_graph_import_evidence_invalid")


def _evidence_payload(value: ContextGraphImportEvidence) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "evidence_ref": value.evidence_ref, "command_id": value.command_id,
        "project_id": value.project_id, "selection_id": value.selection_id, "expected_predecessor": value.expected_predecessor, "graph_id": value.graph_id,
        "graph_revision": value.graph_revision, "source_type": value.source_type,
        "source_revision": value.source_revision, "authorized_file_revision": value.authorized_file_revision, "importer_id": value.importer_id,
        "importer_revision": value.importer_revision, "capability_id": value.capability_id,
        "capability_revision": value.capability_revision, "actor_id": value.actor_id,
        "selection_evidence_ref": value.selection_evidence_ref, "imported_at": value.imported_at,
    }


def _evidence_from_payload(payload: object) -> ContextGraphImportEvidence:
    fields = {
        "schema_version", "evidence_ref", "command_id", "project_id", "selection_id", "expected_predecessor", "graph_id", "graph_revision",
        "source_type", "source_revision", "authorized_file_revision", "importer_id", "importer_revision",
        "capability_id", "capability_revision", "actor_id", "selection_evidence_ref", "imported_at",
    }
    if not isinstance(payload, dict) or set(payload) != fields or payload.get("schema_version") != "1.0.0":
        raise ValueError("context_graph_import_evidence_payload_invalid")
    try:
        result = ContextGraphImportEvidence(**{key: payload[key] for key in fields - {"schema_version"}})
    except (KeyError, TypeError) as error:
        raise ValueError("context_graph_import_evidence_payload_invalid") from error
    _validate_evidence(result)
    if not result.evidence_ref.startswith("context-import-evidence-"):
        raise ValueError("context_graph_import_evidence_payload_invalid")
    return result


def _same_command(stored: ContextGraphImportEvidence, requested: ContextGraphImportEvidence) -> bool:
    return (
        stored.command_id == requested.command_id
        and stored.project_id == requested.project_id
        and stored.selection_id == requested.selection_id
        and stored.expected_predecessor == requested.expected_predecessor
        and stored.graph_id == requested.graph_id
        and stored.graph_revision == requested.graph_revision
        and stored.source_type == requested.source_type
        and stored.source_revision == requested.source_revision
        and stored.authorized_file_revision == requested.authorized_file_revision
        and stored.importer_id == requested.importer_id
        and stored.importer_revision == requested.importer_revision
        and stored.capability_id == requested.capability_id
        and stored.capability_revision == requested.capability_revision
        and stored.actor_id == requested.actor_id
        and stored.selection_evidence_ref == requested.selection_evidence_ref
    )
