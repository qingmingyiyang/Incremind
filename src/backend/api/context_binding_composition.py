"""Core-owned, server-authoritative ContextBinding composition.

This boundary accepts only identifiers and an acknowledgement.  It resolves all
graph, permission, package and revision authority itself, so a client cannot
smuggle a previous graph, grant, or frozen revision set into compilation.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping, Protocol

from backend.api.context_binding_runtime import ContextBindingRegistry, ContextBindingRegistryError
from backend.api.context_graph_snapshot_runtime import (
    ContextGraphSnapshotRecord,
    ContextGraphSnapshotRepository,
    ContextGraphSnapshotRepositoryError,
)
from core.context_graph import (
    CapabilityPackageLoader,
    ContextBinding,
    ContextCompilationError,
    ContextCompiler,
    ContextPermissionError,
    FrozenContextRevisions,
    StalenessConfirmation,
    StalenessEvaluationInput,
    StalenessImpactPreview,
    context_binding_to_payload,
    evaluate_staleness,
    staleness_impact_preview,
)
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError


class CurrentRevisionResolver(Protocol):
    """Resolves the Core's current, complete compilation baseline."""

    def __call__(
        self, project_id: str, capability_id: str, binding_id: str, allow_remote: bool,
    ) -> FrozenContextRevisions: ...


class Clock(Protocol):
    def __call__(self) -> str: ...


class CompilationFactRepository(Protocol):
    """Core-owned record of revisions used by successful immutable bindings."""

    def revision(
        self, project_id: str, graph_id: str, graph_revision: str,
    ) -> FrozenContextRevisions | None: ...

    def latest(self, project_id: str, graph_id: str) -> FrozenContextRevisions | None: ...

    def record(
        self, project_id: str, graph_id: str, graph_revision: str,
        revisions: FrozenContextRevisions, binding_id: str, registry_revision: int,
    ) -> None: ...

    def binding_fact(
        self, project_id: str, graph_id: str, graph_revision: str, binding_id: str,
        registry_revision: int, revisions: FrozenContextRevisions,
    ) -> bool: ...


class InMemoryCompilationFactRepository:
    """Small Core-owned fact repository suitable for one composed runtime.

    A production host may inject a durable implementation.  Facts are append
    only: a graph revision can never be assigned a second compiler baseline.
    """

    def __init__(self) -> None:
        self._facts: dict[tuple[str, str, str, str], tuple[FrozenContextRevisions, int]] = {}

    def revision(self, project_id: str, graph_id: str, graph_revision: str) -> FrozenContextRevisions | None:
        candidates = [
            (registry_revision, revisions) for (project, graph, revision, _), (revisions, registry_revision) in self._facts.items()
            if (project, graph, revision) == (project_id, graph_id, graph_revision)
        ]
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def latest(self, project_id: str, graph_id: str) -> FrozenContextRevisions | None:
        candidates = [
            (registry_revision, fact) for (project, graph, _, _), (fact, registry_revision) in self._facts.items()
            if (project, graph) == (project_id, graph_id)
        ]
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def record(self, project_id: str, graph_id: str, graph_revision: str, revisions: FrozenContextRevisions, binding_id: str, registry_revision: int) -> None:
        key = (project_id, graph_id, graph_revision, binding_id)
        existing = self._facts.setdefault(key, (revisions, registry_revision))
        if existing != (revisions, registry_revision):
            raise ValueError("compilation_fact_revision_drift")

    def binding_fact(self, project_id: str, graph_id: str, graph_revision: str, binding_id: str, registry_revision: int, revisions: FrozenContextRevisions) -> bool:
        return self._facts.get((project_id, graph_id, graph_revision, binding_id)) == (revisions, registry_revision)


class SQLiteCompilationFactRepository:
    """Durable append-only compilation facts with one CAS head per graph."""

    _FACTS = "context_compilation_facts"
    _HEADS = "context_compilation_fact_heads"
    _SEQUENCES = "context_compilation_fact_sequences"
    _SEQUENCE_ID = "sequence"
    _SCHEMA = "1.0.0"

    def __init__(self, records: SQLiteStructuredRecordStore, clock: Clock) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise ValueError("compilation_fact_store_invalid")
        self._records, self._clock = records, clock

    def revision(self, project_id: str, graph_id: str, graph_revision: str) -> FrozenContextRevisions | None:
        head = self._head(project_id, graph_id, graph_revision)
        if head is None:
            return None
        fact = self._fact(head.payload["fact_id"])
        self._validate_head_fact(head, fact)
        return fact.revisions

    def latest(self, project_id: str, graph_id: str) -> FrozenContextRevisions | None:
        heads = [self._head_from_record(item) for item in self._records.list(self._HEADS)]
        matches = [head for head in heads if head.project_id == project_id and head.graph_id == graph_id]
        if not matches:
            return None
        head = max(matches, key=lambda item: item.sequence)
        fact = self._fact(head.payload["fact_id"])
        self._validate_head_fact(head, fact)
        return fact.revisions

    def record(self, project_id: str, graph_id: str, graph_revision: str, revisions: FrozenContextRevisions, binding_id: str, registry_revision: int) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (project_id, graph_id, graph_revision, binding_id)) or type(registry_revision) is not int or registry_revision < 1:
            raise ValueError("compilation_fact_invalid")
        if not isinstance(revisions, FrozenContextRevisions):
            raise ValueError("compilation_fact_revisions_invalid")
        created_at = self._clock()
        if not isinstance(created_at, str) or not created_at.strip():
            raise ValueError("compilation_fact_clock_invalid")
        try:
            with self._records.begin() as uow:
                facts = [self._fact_from_record(item) for item in uow.list(self._FACTS)]
                same = [fact for fact in facts if (fact.project_id, fact.graph_id, fact.graph_revision, fact.binding_id) == (project_id, graph_id, graph_revision, binding_id)]
                if same:
                    existing = same[0]
                    if existing.revisions != revisions or existing.registry_revision != registry_revision:
                        raise ValueError("compilation_fact_revision_drift")
                    uow.commit()
                    return
                sequence = self._next_sequence(uow)
                fact_id = f"fact-{sequence}"
                uow.put(self._FACTS, fact_id, _fact_payload(project_id, graph_id, graph_revision, binding_id, registry_revision, revisions, created_at, sequence), expected_revision=0)
                heads = [self._head_from_record(item) for item in uow.list(self._HEADS)]
                same_heads = [head for head in heads if (head.project_id, head.graph_id, head.graph_revision) == (project_id, graph_id, graph_revision)]
                if len(same_heads) > 1:
                    raise ValueError("compilation_fact_head_ambiguous")
                head = same_heads[0] if same_heads else None
                payload = _head_payload(project_id, graph_id, graph_revision, fact_id, registry_revision, sequence)
                uow.put(self._HEADS, head.object_id if head else f"head-{sequence}", payload, expected_revision=head.store_revision if head else 0)
                uow.commit()
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise ValueError("compilation_fact_write_conflict") from error

    def binding_fact(self, project_id: str, graph_id: str, graph_revision: str, binding_id: str, registry_revision: int, revisions: FrozenContextRevisions) -> bool:
        if not all(isinstance(value, str) and value.strip() for value in (project_id, graph_id, graph_revision, binding_id)) or type(registry_revision) is not int or registry_revision < 1 or not isinstance(revisions, FrozenContextRevisions):
            raise ValueError("compilation_fact_identity_invalid")
        matches = [
            fact for fact in (self._fact_from_record(item) for item in self._records.list(self._FACTS))
            if (fact.project_id, fact.graph_id, fact.graph_revision, fact.binding_id) == (project_id, graph_id, graph_revision, binding_id)
        ]
        if len(matches) > 1:
            raise ValueError("compilation_fact_binding_ambiguous")
        if not matches:
            return False
        fact = matches[0]
        return fact.registry_revision == registry_revision and fact.revisions == revisions

    def _head(self, project_id: str, graph_id: str, graph_revision: str):
        heads = [self._head_from_record(item) for item in self._records.list(self._HEADS)]
        matches = [head for head in heads if (head.project_id, head.graph_id, head.graph_revision) == (project_id, graph_id, graph_revision)]
        if len(matches) > 1:
            raise ValueError("compilation_fact_head_ambiguous")
        return matches[0] if matches else None

    def _fact(self, fact_id: object):
        if not isinstance(fact_id, str):
            raise ValueError("compilation_fact_head_invalid")
        record = self._records.read(self._FACTS, fact_id)
        if record is None:
            raise ValueError("compilation_fact_head_dangling")
        return self._fact_from_record(record)

    @staticmethod
    def _validate_head_fact(head, fact) -> None:
        if (
            (head.project_id, head.graph_id, head.graph_revision) != (fact.project_id, fact.graph_id, fact.graph_revision)
            or head.payload.get("registry_revision") != fact.registry_revision
            or head.sequence != fact.sequence
        ):
            raise ValueError("compilation_fact_head_identity_drift")

    def _next_sequence(self, uow) -> int:
        record = uow.read(self._SEQUENCES, self._SEQUENCE_ID)
        if record is None:
            number, expected = 1, 0
        else:
            payload = record.payload
            if set(payload) != {"schema_version", "next"} or payload.get("schema_version") != self._SCHEMA or type(payload.get("next")) is not int or payload["next"] < 1:
                raise ValueError("compilation_fact_sequence_invalid")
            number, expected = payload["next"], record.revision
        uow.put(self._SEQUENCES, self._SEQUENCE_ID, {"schema_version": self._SCHEMA, "next": number + 1}, expected_revision=expected)
        return number

    def _fact_from_record(self, record):
        payload = record.payload
        required = {"schema_version", "project_id", "graph_id", "graph_revision", "binding_id", "registry_revision", "revisions", "created_at", "sequence"}
        if set(payload) != required or payload.get("schema_version") != self._SCHEMA:
            raise ValueError("compilation_fact_payload_invalid")
        revisions = _revisions_from_payload(payload.get("revisions"))
        values = (payload.get("project_id"), payload.get("graph_id"), payload.get("graph_revision"), payload.get("binding_id"), payload.get("created_at"))
        if any(not isinstance(value, str) or not value.strip() for value in values) or type(payload.get("registry_revision")) is not int or payload["registry_revision"] < 1 or type(payload.get("sequence")) is not int or payload["sequence"] < 1:
            raise ValueError("compilation_fact_payload_invalid")
        return _Fact(record.object_id, record.revision, values[0], values[1], values[2], values[3], payload["registry_revision"], revisions, values[4], payload["sequence"])

    def _head_from_record(self, record):
        payload = record.payload
        required = {"schema_version", "project_id", "graph_id", "graph_revision", "fact_id", "registry_revision", "sequence"}
        if set(payload) != required or payload.get("schema_version") != self._SCHEMA or any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in ("project_id", "graph_id", "graph_revision", "fact_id")) or type(payload.get("registry_revision")) is not int or payload["registry_revision"] < 1 or type(payload.get("sequence")) is not int or payload["sequence"] < 1:
            raise ValueError("compilation_fact_head_invalid")
        return _Head(record.object_id, record.revision, payload["project_id"], payload["graph_id"], payload["graph_revision"], payload, payload["sequence"])


@dataclass(frozen=True, slots=True)
class _Fact:
    object_id: str; store_revision: int; project_id: str; graph_id: str; graph_revision: str; binding_id: str; registry_revision: int; revisions: FrozenContextRevisions; created_at: str; sequence: int


@dataclass(frozen=True, slots=True)
class _Head:
    object_id: str; store_revision: int; project_id: str; graph_id: str; graph_revision: str; payload: Mapping[str, object]; sequence: int


@dataclass(frozen=True, slots=True)
class ContextBindingCompositionRequest:
    project_id: str
    graph_id: str
    graph_revision: str
    binding_id: str
    token_budget: int
    acknowledge_staleness: bool
    actor_id: str
    allow_remote: bool = False


@dataclass(frozen=True, slots=True)
class ContextBindingCompositionError(ValueError):
    code: str
    detail: str
    preview: StalenessImpactPreview | None = None


@dataclass(frozen=True, slots=True)
class ContextBindingCompositionResult:
    binding: ContextBinding | None = None
    registry_record: Mapping[str, object] | None = None
    preview: StalenessImpactPreview | None = None
    error: ContextBindingCompositionError | None = None
    idempotent: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


class ContextBindingCompositionService:
    """Composes a binding without provider, model, effect or secret access."""

    def __init__(
        self,
        snapshots: ContextGraphSnapshotRepository,
        registry: ContextBindingRegistry,
        packages: CapabilityPackageLoader,
        compiler: ContextCompiler,
        current_revisions: CurrentRevisionResolver,
        clock: Clock,
        *,
        facts: CompilationFactRepository,
    ) -> None:
        self._snapshots = snapshots
        self._registry = registry
        self._packages = packages
        self._compiler = compiler
        self._current_revisions = current_revisions
        self._clock = clock
        self._facts = facts

    def create(self, request: ContextBindingCompositionRequest) -> ContextBindingCompositionResult:
        """Resolve authority, preview stale impact, then compile/create atomically enough.

        The registry is immutable and has no transaction with the graph store;
        recording a fact after a successful create is deliberately idempotent.
        """
        try:
            self._validate_request(request)
            record = self._resolve_exact_record(request)
            current = self._resolve_current_revisions(request, record.capability_id)
            self._validate_manifest(record.capability_id, current)
            previous, previous_revisions = self._previous_authority(record, current)
            evaluated = evaluate_staleness(
                previous.snapshot, record.snapshot,
                previous_revisions=_revision_map(previous_revisions),
                current_revisions=_revision_map(current),
            )
            preview = staleness_impact_preview(evaluated)
            if preview.confirmation_required and not request.acknowledge_staleness:
                return self._failure("staleness_confirmation_required", preview=preview)
            confirmation = self._confirmation(request, preview)
            binding = self._compiler.compile(
                record.snapshot,
                revisions=current,
                expected_revisions=current,
                permission_grant=record.permission_grant,
                token_budget=request.token_budget,
                staleness_input=StalenessEvaluationInput(
                    previous.snapshot, previous_revisions, current, confirmation,
                ),
            )
            self._validate_manifest(record.capability_id, current)
            return self._create_or_verify(request, record, binding, current, preview)
        except ContextBindingCompositionError as error:
            return ContextBindingCompositionResult(error=error, preview=error.preview)
        except ContextGraphSnapshotRepositoryError as error:
            return self._failure("graph_snapshot_unavailable", str(error))
        except ContextCompilationError as error:
            return self._failure(_compiler_error_code(str(error)), str(error))
        except ContextPermissionError as error:
            return self._failure("permission_denied", str(error))
        except (ValueError, TypeError, RuntimeError) as error:
            return self._failure("composition_invalid", str(error))

    def _resolve_exact_record(self, request: ContextBindingCompositionRequest) -> ContextGraphSnapshotRecord:
        record = self._snapshots.revision(request.project_id, request.graph_id, request.graph_revision)
        if record is None:
            raise ContextBindingCompositionError("graph_revision_unavailable", "exact graph revision is unavailable")
        if (record.project_id, record.graph_id, record.graph_revision) != (
            request.project_id, request.graph_id, request.graph_revision,
        ):
            raise ContextBindingCompositionError("graph_scope_drift", "graph record scope drifted")
        return record

    def _resolve_current_revisions(self, request: ContextBindingCompositionRequest, capability_id: str) -> FrozenContextRevisions:
        try:
            current = self._current_revisions(
                request.project_id, capability_id, request.binding_id,
                request.allow_remote,
            )
        except (TypeError, ValueError) as error:
            raise ContextBindingCompositionError(
                "current_revisions_unavailable", str(error),
            ) from error
        if not isinstance(current, FrozenContextRevisions):
            raise ContextBindingCompositionError("current_revisions_unavailable", "current revisions are invalid")
        if current.compiler_revision != self._compiler.compiler_revision:
            raise ContextBindingCompositionError("unsupported_current_compiler", "current compiler revision is unsupported")
        return current

    def _validate_manifest(self, capability_id: str, current: FrozenContextRevisions) -> None:
        active = [item for item in self._packages.active() if item.capability_id == capability_id]
        if len(active) != 1:
            raise ContextBindingCompositionError("active_capability_unavailable", "active capability must be unique")
        manifest = active[0]
        if manifest.core_api != "2":
            raise ContextBindingCompositionError("unsupported_core_api", "capability requires core_api=2")
        if "context_compiler_extension" not in manifest.contributions:
            raise ContextBindingCompositionError("compiler_extension_missing", "capability lacks context compiler extension")
        if manifest.capability_revision != current.capability_revision:
            raise ContextBindingCompositionError("capability_revision_drift", "active capability revision drifted")

    def _previous_authority(
        self, record: ContextGraphSnapshotRecord, current: FrozenContextRevisions,
    ) -> tuple[ContextGraphSnapshotRecord, FrozenContextRevisions]:
        predecessor = self._snapshots.previous(record.project_id, record.graph_id, record.graph_revision)
        if predecessor is None:
            # A graph snapshot's registration metadata is not compilation fact.
            return record, self._facts.revision(record.project_id, record.graph_id, record.graph_revision) or current
        fact = self._facts.revision(predecessor.project_id, predecessor.graph_id, predecessor.graph_revision)
        # A graph can be revised before it is ever compiled.  Compare its shape,
        # but use today's baseline for both sides rather than inventing drift.
        return predecessor, fact or current

    def _confirmation(
        self, request: ContextBindingCompositionRequest, preview: StalenessImpactPreview,
    ) -> StalenessConfirmation | None:
        if not preview.confirmation_required:
            return None
        if not request.acknowledge_staleness:
            return None
        confirmed_at = self._clock()
        if not isinstance(confirmed_at, str) or not confirmed_at.strip():
            raise ContextBindingCompositionError("clock_unavailable", "clock returned invalid confirmation time")
        return StalenessConfirmation(
            preview.graph_id, preview.graph_revision, preview.affected_node_ids,
            preview.replay_order, preview.stale_reasons, request.actor_id, confirmed_at,
        )

    def _create_or_verify(
        self, request: ContextBindingCompositionRequest, graph: ContextGraphSnapshotRecord,
        binding: ContextBinding, revisions: FrozenContextRevisions, preview: StalenessImpactPreview,
    ) -> ContextBindingCompositionResult:
        try:
            stored = self._registry.create(
                binding_id=request.binding_id, project_id=request.project_id,
                capability_id=graph.capability_id, capability_revision=revisions.capability_revision,
                binding=binding, expected_revision=0,
            )
            self._facts.record(graph.project_id, graph.graph_id, graph.graph_revision, revisions, request.binding_id, int(stored["registry_revision"]))
            return ContextBindingCompositionResult(binding, stored, preview)
        except ContextBindingRegistryError as error:
            if "already exists" not in str(error):
                return self._failure("binding_registry_rejected", str(error), preview)
        try:
            existing = self._registry.resolve(
                f"crp://context-bindings/{request.project_id}/{request.binding_id}",
                project_id=request.project_id,
            )
        except ContextBindingRegistryError as error:
            return self._failure("binding_registry_drift", str(error), preview)
        if (
            existing.capability_id != graph.capability_id
            or existing.capability_revision != revisions.capability_revision
            or _canonical_payload(existing.binding) != _canonical_payload(binding)
        ):
            return self._failure("binding_identity_drift", "existing immutable binding differs", preview)
        self._facts.record(graph.project_id, graph.graph_id, graph.graph_revision, revisions, existing.binding_id, existing.registry_revision)
        return ContextBindingCompositionResult(binding, {
            "binding_id": existing.binding_id, "project_id": existing.project_id,
            "capability_id": existing.capability_id,
            "capability_revision": existing.capability_revision,
            "registry_revision": existing.registry_revision, "binding_ref": existing.payload_ref,
        }, preview, idempotent=True)

    @staticmethod
    def _validate_request(request: object) -> None:
        if not isinstance(request, ContextBindingCompositionRequest):
            raise ContextBindingCompositionError("invalid_request", "request shape is invalid")
        if any(not isinstance(value, str) or not value.strip() for value in (
            request.project_id, request.graph_id, request.graph_revision,
            request.binding_id, request.actor_id,
        )) or (
            type(request.token_budget) is not int
            or request.token_budget < 1
            or type(request.acknowledge_staleness) is not bool
            or type(request.allow_remote) is not bool
        ):
            raise ContextBindingCompositionError("invalid_request", "request values are invalid")

    @staticmethod
    def _failure(code: str, detail: str = "", preview: StalenessImpactPreview | None = None) -> ContextBindingCompositionResult:
        return ContextBindingCompositionResult(error=ContextBindingCompositionError(code, detail or code, preview), preview=preview)


def _revision_map(value: FrozenContextRevisions) -> Mapping[str, str]:
    return {
        "capability_revision": value.capability_revision,
        "boundary_revision": value.boundary_revision,
        "provider_revision": value.provider_revision,
        "model_route_revision": value.model_route_revision,
        "compiler_revision": value.compiler_revision,
    }


def _fact_payload(project_id, graph_id, graph_revision, binding_id, registry_revision, revisions, created_at, sequence) -> Mapping[str, object]:
    return {"schema_version": "1.0.0", "project_id": project_id, "graph_id": graph_id, "graph_revision": graph_revision, "binding_id": binding_id, "registry_revision": registry_revision, "revisions": _revision_map(revisions), "created_at": created_at, "sequence": sequence}


def _head_payload(project_id, graph_id, graph_revision, fact_id, registry_revision, sequence) -> Mapping[str, object]:
    return {"schema_version": "1.0.0", "project_id": project_id, "graph_id": graph_id, "graph_revision": graph_revision, "fact_id": fact_id, "registry_revision": registry_revision, "sequence": sequence}


def _revisions_from_payload(value: object) -> FrozenContextRevisions:
    if not isinstance(value, Mapping) or set(value) != {"capability_revision", "boundary_revision", "provider_revision", "model_route_revision", "compiler_revision"}:
        raise ValueError("compilation_fact_revisions_invalid")
    try:
        return FrozenContextRevisions(**dict(value))
    except (TypeError, ValueError) as error:
        raise ValueError("compilation_fact_revisions_invalid") from error


def _compiler_error_code(detail: str) -> str:
    if detail.startswith("content_permission_denied") or detail.startswith("permission_"):
        return "permission_denied"
    if detail.startswith("revision_drift"):
        return "revision_drift"
    return "compiler_rejected"


def _canonical_payload(binding: ContextBinding) -> object:
    """Normalize tuple/list differences introduced by JSON registry storage."""
    return json.loads(json.dumps(context_binding_to_payload(binding), sort_keys=True))
