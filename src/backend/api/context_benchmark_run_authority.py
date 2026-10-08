"""Core-owned, non-executing authority for governed context benchmark runs.

The authority records a frozen six-Turn plan and can hand its exact request to
the ordinary AI Turn runner.  It deliberately has no model gateway, retry
loop, lease, worker, Secret access, or recovery scheduler.  Terminal evidence
is always reconstructed by the existing suite service from the durable Turn
authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Protocol

from core.ai_kernel import TERMINAL_EVENT_TYPES, validate_turn_request
from core.context_graph import FrozenContextRevisions
from core.storage_provider import (
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
    SQLiteUnitOfWorkError,
)


class ContextBenchmarkRunError(ValueError):
    """A benchmark plan cannot be safely prepared, read, or submitted."""


class ContextBenchmarkRunConflict(ContextBenchmarkRunError):
    """A durable benchmark identity is already bound to different content."""


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_COLLECTION = "context_benchmark_runs"
_SCHEMA_VERSION = "1.0.0"
_VARIANTS = ("linear", "linemap")


@dataclass(frozen=True, slots=True)
class ContextBenchmarkRunPrepareRequest:
    run_id: str
    suite_run_id: str
    project_id: str
    session_id: str
    actor_id: str
    consent_refs: tuple[str, ...]
    confirmation_ref: str
    created_at: str
    confirmed: bool
    replicate_index: int = 0


@dataclass(frozen=True, slots=True)
class ContextBenchmarkTurnEnvelope:
    turn_id: str
    operation_id: str
    case_id: str
    variant: str
    request: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ContextBenchmarkRunPlan:
    run_id: str
    suite_run_id: str
    project_id: str
    session_id: str
    actor_id: str
    consent_refs: tuple[str, ...]
    confirmation_ref: str
    created_at: str
    replicate_index: int
    capability_id: str
    capability_revision: str
    revisions: FrozenContextRevisions
    envelopes: tuple[ContextBenchmarkTurnEnvelope, ...]

    @property
    def turn_ids(self) -> tuple[str, ...]:
        return tuple(item.turn_id for item in self.envelopes)

    @property
    def coordinator_turn_id(self) -> str:
        return self.envelopes[0].turn_id

    def envelope(self, turn_id: str) -> ContextBenchmarkTurnEnvelope:
        for item in self.envelopes:
            if item.turn_id == turn_id:
                return item
        raise ContextBenchmarkRunError("benchmark Turn is not in the frozen plan")


class ContextBenchmarkRunRepository:
    """Append-only plan repository; it stores no execution lifecycle state."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise ContextBenchmarkRunError("benchmark record store is invalid")
        self._records = records

    def create(self, plan: ContextBenchmarkRunPlan) -> ContextBenchmarkRunPlan:
        payload = _plan_payload(plan)
        try:
            with self._records.begin() as uow:
                uow.put(_COLLECTION, plan.run_id, payload, expected_revision=0)
                uow.commit()
        except SQLiteUnitOfWorkConflict as error:
            existing = self.get(plan.run_id)
            if existing is not None and _canonical(_plan_payload(existing)) == _canonical(payload):
                return existing
            raise ContextBenchmarkRunConflict("benchmark run identity drifted") from error
        except SQLiteUnitOfWorkError as error:
            raise ContextBenchmarkRunError("benchmark plan persistence is unavailable") from error
        return plan

    def get(self, run_id: str) -> ContextBenchmarkRunPlan | None:
        identity = _identity(run_id, "run id")
        try:
            record = self._records.read(_COLLECTION, identity)
        except SQLiteUnitOfWorkError as error:
            raise ContextBenchmarkRunError("benchmark plan read is unavailable") from error
        if record is None:
            return None
        return _plan_from_payload(record.payload)


class BenchmarkReadiness(Protocol):
    def __call__(
        self, request: ContextBenchmarkRunPrepareRequest,
    ) -> FrozenContextRevisions: ...


class BenchmarkCase(Protocol):
    case_id: str


class BenchmarkBindingIssuer(Protocol):
    def __call__(
        self,
        *,
        case: BenchmarkCase,
        binding_creation: Mapping[str, object],
        project_id: str,
        revisions: FrozenContextRevisions,
    ) -> Mapping[str, object]: ...


class BenchmarkPairBuilder(Protocol):
    def __call__(self, case: BenchmarkCase, **kwargs: object) -> object: ...


class OrdinaryTurnSubmitter(Protocol):
    def __call__(self, request: Mapping[str, object]) -> object: ...


class TurnEvidenceReader(Protocol):
    def get_request(self, turn_id: str) -> Mapping[str, object] | None: ...

    def events_after(
        self, turn_id: str, after_sequence: int = 0,
    ) -> Sequence[Mapping[str, object]]: ...


class BenchmarkFinalizer(Protocol):
    def finalize(
        self, *, suite_run_id: str, turn_ids: Sequence[str], coordinator_turn_id: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class ContextBenchmarkTurnStatus:
    turn_id: str
    state: str
    terminal_event_type: str | None


class ContextBenchmarkRunService:
    """Freeze plans and delegate each execution to the ordinary Turn runner."""

    def __init__(
        self,
        *,
        repository: ContextBenchmarkRunRepository,
        readiness: BenchmarkReadiness,
        case_builder: Callable[[], Sequence[BenchmarkCase]],
        pair_builder: BenchmarkPairBuilder,
        binding_issuer: BenchmarkBindingIssuer,
        capability_id: str,
        capability_revision: str,
        turn_submitter: OrdinaryTurnSubmitter,
        evidence_reader: TurnEvidenceReader,
        finalizer: BenchmarkFinalizer,
    ) -> None:
        if not isinstance(repository, ContextBenchmarkRunRepository):
            raise ContextBenchmarkRunError("benchmark repository is invalid")
        if not all(callable(item) for item in (
            readiness, case_builder, pair_builder, binding_issuer, turn_submitter,
        )) or not all(callable(getattr(evidence_reader, name, None)) for name in (
            "get_request", "events_after",
        )) or not callable(getattr(finalizer, "finalize", None)):
            raise ContextBenchmarkRunError("benchmark authority dependencies are invalid")
        self._repository = repository
        self._readiness = readiness
        self._case_builder = case_builder
        self._pair_builder = pair_builder
        self._binding_issuer = binding_issuer
        self._capability_id = _identity(capability_id, "capability id")
        self._capability_revision = _text(capability_revision, "capability revision")
        self._turn_submitter = turn_submitter
        self._evidence_reader = evidence_reader
        self._finalizer = finalizer

    def prepare(self, request: ContextBenchmarkRunPrepareRequest) -> ContextBenchmarkRunPlan:
        _validate_prepare_request(request)
        existing = self._repository.get(request.run_id)
        if existing is not None:
            _validate_existing_request(existing, request, self._capability_id, self._capability_revision)
            self._assert_frozen_plan_integrity(existing)
            return existing
        revisions = self._resolve_revisions(request)
        plan = self._build_plan(request, revisions)
        return self._repository.create(plan)

    def _build_plan(
        self,
        request: ContextBenchmarkRunPrepareRequest,
        revisions: FrozenContextRevisions,
    ) -> ContextBenchmarkRunPlan:
        try:
            cases = tuple(self._case_builder())
        except (TypeError, ValueError) as error:
            raise ContextBenchmarkRunError("benchmark cases are unavailable") from error
        _validate_cases(cases)

        envelopes: list[ContextBenchmarkTurnEnvelope] = []
        for case in cases:
            binding_id = _binding_id(request.suite_run_id, case.case_id, request.replicate_index)
            linear_turn_id = _turn_id(
                request.suite_run_id, case.case_id, request.replicate_index, "linear",
            )
            linemap_turn_id = _turn_id(
                request.suite_run_id, case.case_id, request.replicate_index, "linemap",
            )
            try:
                pair = self._pair_builder(
                    case,
                    project_id=request.project_id,
                    session_id=request.session_id,
                    linear_turn_id=linear_turn_id,
                    linemap_turn_id=linemap_turn_id,
                    binding_id=binding_id,
                    suite_run_id=request.suite_run_id,
                    revisions=revisions,
                    created_at=request.created_at,
                    consent_refs=request.consent_refs,
                    replicate_index=request.replicate_index,
                )
                binding_creation = getattr(pair, "binding_creation")
                linear_turn = getattr(pair, "linear_turn")
                linemap_turn = getattr(pair, "linemap_turn")
            except (AttributeError, TypeError, ValueError) as error:
                raise ContextBenchmarkRunError("benchmark Turn plan is invalid") from error
            _validate_binding_creation(
                self._binding_issuer(
                    case=case,
                    binding_creation=binding_creation,
                    project_id=request.project_id,
                    revisions=revisions,
                ),
                expected_binding_id=binding_id,
                expected_project_id=request.project_id,
                expected_capability_id=self._capability_id,
                expected_capability_revision=self._capability_revision,
            )
            envelopes.extend((
                _envelope(linear_turn, case.case_id, "linear", linear_turn_id),
                _envelope(linemap_turn, case.case_id, "linemap", linemap_turn_id),
            ))
        plan = ContextBenchmarkRunPlan(
            request.run_id, request.suite_run_id, request.project_id,
            request.session_id, request.actor_id, request.consent_refs,
            request.confirmation_ref, request.created_at, request.replicate_index,
            self._capability_id,
            self._capability_revision, revisions, tuple(envelopes),
        )
        _validate_plan(plan)
        return plan

    def get(self, run_id: str) -> ContextBenchmarkRunPlan:
        plan = self._repository.get(run_id)
        if plan is None:
            raise ContextBenchmarkRunError("benchmark run is unavailable")
        return plan

    def statuses(self, run_id: str) -> tuple[ContextBenchmarkTurnStatus, ...]:
        plan = self.get(run_id)
        return tuple(self._status(envelope) for envelope in plan.envelopes)

    def next_envelope(self, run_id: str) -> ContextBenchmarkTurnEnvelope | None:
        plan = self.get(run_id)
        for envelope in plan.envelopes:
            if self._status(envelope).state == "not_submitted":
                return envelope
        return None

    def submit(self, run_id: str, turn_id: str, *, confirmed: bool) -> object:
        if confirmed is not True:
            raise ContextBenchmarkRunError("benchmark Turn submission requires confirmation")
        plan = self.get(run_id)
        self._assert_frozen_plan_integrity(plan)
        envelope = plan.envelope(_identity(turn_id, "turn id"))
        if self._status(envelope).state != "not_submitted":
            raise ContextBenchmarkRunConflict("benchmark Turn is already submitted")
        receipt = self._turn_submitter(dict(envelope.request))
        receipt_turn_id = (
            receipt.get("turn_id") if isinstance(receipt, Mapping)
            else getattr(receipt, "turn_id", None)
        )
        if receipt_turn_id != envelope.turn_id:
            raise ContextBenchmarkRunError("ordinary Turn submitter identity drifted")
        return receipt

    def _resolve_revisions(
        self, request: ContextBenchmarkRunPrepareRequest,
    ) -> FrozenContextRevisions:
        try:
            revisions = self._readiness(request)
        except (LookupError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise ContextBenchmarkRunError("benchmark readiness is unavailable") from error
        if not isinstance(revisions, FrozenContextRevisions):
            raise ContextBenchmarkRunError("benchmark readiness returned invalid revisions")
        if revisions.capability_revision != self._capability_revision:
            raise ContextBenchmarkRunError("benchmark capability revision drifted")
        return revisions

    def _assert_current_revisions(self, plan: ContextBenchmarkRunPlan) -> None:
        current = self._resolve_revisions(_prepare_request_from_plan(plan))
        if current != plan.revisions:
            raise ContextBenchmarkRunConflict("benchmark frozen revisions drifted")

    def _assert_frozen_plan_integrity(self, plan: ContextBenchmarkRunPlan) -> None:
        """Rebuild the persisted plan before any effectful benchmark operation.

        Rebuilding deliberately re-enters the binding issuer: its idempotent path
        repairs missing Binding/CompilationFact authority after a restart while
        the canonical comparison keeps the persisted execution envelopes frozen.
        """
        self._assert_current_revisions(plan)
        candidate = self._build_plan(_prepare_request_from_plan(plan), plan.revisions)
        if _canonical(_plan_payload(candidate)) != _canonical(_plan_payload(plan)):
            raise ContextBenchmarkRunConflict("benchmark frozen plan drifted")

    def finalize(self, run_id: str) -> object:
        plan = self.get(run_id)
        self._assert_frozen_plan_integrity(plan)
        states = self.statuses(run_id)
        if any(item.state != "completed" for item in states):
            raise ContextBenchmarkRunError("benchmark Turns are not all completed")
        try:
            return self._finalizer.finalize(
                suite_run_id=plan.suite_run_id,
                turn_ids=plan.turn_ids,
                coordinator_turn_id=plan.coordinator_turn_id,
            )
        except (TypeError, ValueError) as error:
            raise ContextBenchmarkRunError("benchmark evidence finalization failed") from error

    def _status(self, envelope: ContextBenchmarkTurnEnvelope) -> ContextBenchmarkTurnStatus:
        stored = self._evidence_reader.get_request(envelope.turn_id)
        if stored is None:
            return ContextBenchmarkTurnStatus(envelope.turn_id, "not_submitted", None)
        if _canonical(_turn_payload(stored)) != _canonical(_turn_payload(envelope.request)):
            raise ContextBenchmarkRunConflict("benchmark durable Turn request drifted")
        events = tuple(self._evidence_reader.events_after(envelope.turn_id))
        terminal = [item for item in events if item.get("type") in TERMINAL_EVENT_TYPES]
        if len(terminal) == 1:
            terminal_type = str(terminal[0]["type"])
            return ContextBenchmarkTurnStatus(
                envelope.turn_id,
                {
                    "turn.completed": "completed",
                    "turn.failed": "failed",
                    "turn.cancelled": "cancelled",
                }[terminal_type],
                terminal_type,
            )
        if len(terminal) > 1:
            raise ContextBenchmarkRunConflict("benchmark Turn terminal evidence is ambiguous")
        return ContextBenchmarkTurnStatus(envelope.turn_id, "submitted", None)


def _validate_prepare_request(value: object) -> None:
    if not isinstance(value, ContextBenchmarkRunPrepareRequest):
        raise ContextBenchmarkRunError("benchmark prepare request is invalid")
    for item, label in (
        (value.run_id, "run id"), (value.suite_run_id, "suite run id"),
        (value.project_id, "project id"), (value.session_id, "session id"),
        (value.actor_id, "actor id"),
    ):
        _identity(item, label)
    _timestamp(value.created_at)
    if value.confirmed is not True:
        raise ContextBenchmarkRunError("benchmark preparation requires confirmation")
    if type(value.replicate_index) is not int or not 0 <= value.replicate_index <= 9_999:
        raise ContextBenchmarkRunError("benchmark replicate index is invalid")
    if (
        not value.consent_refs
        or len(value.consent_refs) > 16
        or len(set(value.consent_refs)) != len(value.consent_refs)
        or any(not isinstance(item, str) or not item.strip() for item in value.consent_refs)
    ):
        raise ContextBenchmarkRunError("benchmark consent refs are invalid")
    _text(value.confirmation_ref, "confirmation ref")


def _validate_existing_request(plan: ContextBenchmarkRunPlan, request: ContextBenchmarkRunPrepareRequest, capability_id: str, capability_revision: str) -> None:
    expected = (
        request.suite_run_id, request.project_id, request.session_id, request.actor_id,
        request.consent_refs, request.confirmation_ref, request.created_at,
        request.replicate_index,
        capability_id, capability_revision,
    )
    actual = (
        plan.suite_run_id, plan.project_id, plan.session_id, plan.actor_id,
        plan.consent_refs, plan.confirmation_ref, plan.created_at,
        plan.replicate_index,
        plan.capability_id, plan.capability_revision,
    )
    if actual != expected:
        raise ContextBenchmarkRunConflict("benchmark run identity drifted")


def _validate_cases(cases: tuple[BenchmarkCase, ...]) -> None:
    ids = tuple(getattr(case, "case_id", None) for case in cases)
    if len(cases) != 3 or any(not isinstance(item, str) or not item.strip() for item in ids) or len(set(ids)) != len(ids):
        raise ContextBenchmarkRunError("benchmark cases are invalid")


def _validate_binding_creation(value: object, *, expected_binding_id: str, expected_project_id: str, expected_capability_id: str, expected_capability_revision: str) -> None:
    if not isinstance(value, Mapping) or (
        value.get("binding_id"), value.get("project_id"), value.get("capability_id"), value.get("capability_revision")
    ) != (expected_binding_id, expected_project_id, expected_capability_id, expected_capability_revision):
        raise ContextBenchmarkRunError("benchmark binding issuance drifted")
    binding_ref = value.get("binding_ref")
    if binding_ref != f"crp://context-bindings/{expected_project_id}/{expected_binding_id}":
        raise ContextBenchmarkRunError("benchmark binding reference drifted")


def _envelope(
    value: object, case_id: str, variant: str, expected_turn_id: str,
) -> ContextBenchmarkTurnEnvelope:
    request = _turn_payload(value)
    if request["turn_id"] != expected_turn_id:
        raise ContextBenchmarkRunError("benchmark Turn identity is invalid")
    return ContextBenchmarkTurnEnvelope(str(request["turn_id"]), str(request["operation_id"]), _identity(case_id, "case id"), variant, request)


def _validate_plan(plan: ContextBenchmarkRunPlan) -> None:
    if len(plan.envelopes) != 6 or len(set(plan.turn_ids)) != 6:
        raise ContextBenchmarkRunError("benchmark plan requires six distinct Turns")
    variants_by_case: dict[str, set[str]] = {}
    for item in plan.envelopes:
        variants_by_case.setdefault(item.case_id, set()).add(item.variant)
    if len(variants_by_case) != 3 or any(
        variants != set(_VARIANTS) for variants in variants_by_case.values()
    ):
        raise ContextBenchmarkRunError("benchmark plan case variants are invalid")
    if type(plan.replicate_index) is not int or not 0 <= plan.replicate_index <= 9_999:
        raise ContextBenchmarkRunError("benchmark plan replicate index is invalid")
    if (
        not plan.consent_refs
        or len(plan.consent_refs) > 16
        or len(set(plan.consent_refs)) != len(plan.consent_refs)
        or any(not isinstance(item, str) or not item.strip() for item in plan.consent_refs)
    ):
        raise ContextBenchmarkRunError("benchmark plan consent references are invalid")
    _text(plan.confirmation_ref, "confirmation ref")
    _timestamp(plan.created_at)
    if any(item.request.get("scope", {}).get("project_id") != plan.project_id for item in plan.envelopes):
        raise ContextBenchmarkRunError("benchmark plan project scope drifted")
    if any(item.request.get("privacy", {}).get("consent_refs") != list(plan.consent_refs) for item in plan.envelopes):
        raise ContextBenchmarkRunError("benchmark plan consent references drifted")


def _plan_payload(plan: ContextBenchmarkRunPlan) -> Mapping[str, object]:
    _validate_plan(plan)
    return {
        "schema_version": _SCHEMA_VERSION, "run_id": plan.run_id,
        "suite_run_id": plan.suite_run_id, "project_id": plan.project_id,
        "session_id": plan.session_id, "actor_id": plan.actor_id,
        "consent_refs": list(plan.consent_refs),
        "confirmation_ref": plan.confirmation_ref, "created_at": plan.created_at,
        "replicate_index": plan.replicate_index, "capability_id": plan.capability_id,
        "capability_revision": plan.capability_revision,
        "revisions": _revisions_payload(plan.revisions),
        "envelopes": [{
            "turn_id": item.turn_id, "operation_id": item.operation_id,
            "case_id": item.case_id, "variant": item.variant,
            "request": _turn_payload(item.request),
        } for item in plan.envelopes],
    }


def _plan_from_payload(payload: Mapping[str, object]) -> ContextBenchmarkRunPlan:
    fields = {
        "schema_version", "run_id", "suite_run_id", "project_id", "session_id", "actor_id",
        "consent_refs", "confirmation_ref", "created_at", "replicate_index",
        "capability_id", "capability_revision",
        "revisions", "envelopes",
    }
    if not isinstance(payload, Mapping) or set(payload) != fields or payload.get("schema_version") != _SCHEMA_VERSION:
        raise ContextBenchmarkRunError("benchmark plan record is invalid")
    refs = payload.get("consent_refs")
    envelopes = payload.get("envelopes")
    if not isinstance(refs, list) or not isinstance(envelopes, list):
        raise ContextBenchmarkRunError("benchmark plan record is invalid")
    revisions = _revisions_from_payload(payload.get("revisions"))
    parsed: list[ContextBenchmarkTurnEnvelope] = []
    for item in envelopes:
        if not isinstance(item, Mapping) or set(item) != {"turn_id", "operation_id", "case_id", "variant", "request"}:
            raise ContextBenchmarkRunError("benchmark plan envelope is invalid")
        request = _turn_payload(item.get("request"))
        if item.get("turn_id") != request.get("turn_id") or item.get("operation_id") != request.get("operation_id"):
            raise ContextBenchmarkRunError("benchmark plan envelope identity drifted")
        variant = item.get("variant")
        if variant not in _VARIANTS:
            raise ContextBenchmarkRunError("benchmark plan envelope variant is invalid")
        parsed.append(ContextBenchmarkTurnEnvelope(
            str(item["turn_id"]), str(item["operation_id"]),
            _identity(item.get("case_id"), "case id"), str(variant), request,
        ))
    plan = ContextBenchmarkRunPlan(
        _identity(payload.get("run_id"), "run id"), _identity(payload.get("suite_run_id"), "suite run id"),
        _identity(payload.get("project_id"), "project id"), _identity(payload.get("session_id"), "session id"),
        _identity(payload.get("actor_id"), "actor id"), tuple(_text(item, "consent ref") for item in refs),
        _text(payload.get("confirmation_ref"), "confirmation ref"),
        _timestamp(payload.get("created_at")), payload.get("replicate_index"),
        _identity(payload.get("capability_id"), "capability id"), _text(payload.get("capability_revision"), "capability revision"),
        revisions, tuple(parsed),
    )
    if type(plan.replicate_index) is not int:
        raise ContextBenchmarkRunError("benchmark plan replicate index is invalid")
    _validate_plan(plan)
    return plan


def _revisions_payload(value: FrozenContextRevisions) -> Mapping[str, str]:
    return {
        "capability_revision": value.capability_revision,
        "boundary_revision": value.boundary_revision,
        "provider_revision": value.provider_revision,
        "model_route_revision": value.model_route_revision,
        "compiler_revision": value.compiler_revision,
    }


def _revisions_from_payload(value: object) -> FrozenContextRevisions:
    fields = {"capability_revision", "boundary_revision", "provider_revision", "model_route_revision", "compiler_revision"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContextBenchmarkRunError("benchmark frozen revisions are invalid")
    try:
        return FrozenContextRevisions(**{key: _text(value.get(key), key) for key in fields})
    except (TypeError, ValueError) as error:
        raise ContextBenchmarkRunError("benchmark frozen revisions are invalid") from error


def _turn_payload(value: object) -> dict[str, object]:
    try:
        result = validate_turn_request(value)
    except (TypeError, ValueError) as error:
        raise ContextBenchmarkRunError("benchmark Turn request is invalid") from error
    return dict(result)


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ContextBenchmarkRunError(f"benchmark {label} is invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextBenchmarkRunError(f"benchmark {label} is invalid")
    return value


def _timestamp(value: object) -> str:
    text = _text(value, "created time")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContextBenchmarkRunError("benchmark created time is invalid") from error
    if parsed.tzinfo is None:
        raise ContextBenchmarkRunError("benchmark created time is invalid")
    return text


def _prepare_request_from_plan(
    plan: ContextBenchmarkRunPlan,
) -> ContextBenchmarkRunPrepareRequest:
    return ContextBenchmarkRunPrepareRequest(
        run_id=plan.run_id,
        suite_run_id=plan.suite_run_id,
        project_id=plan.project_id,
        session_id=plan.session_id,
        actor_id=plan.actor_id,
        consent_refs=plan.consent_refs,
        confirmation_ref=plan.confirmation_ref,
        created_at=plan.created_at,
        confirmed=True,
        replicate_index=plan.replicate_index,
    )


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _binding_id(suite_run_id: str, case_id: str, replicate_index: int) -> str:
    return f"binding-cgbench-{suite_run_id}-{case_id}-r{replicate_index}"


def _turn_id(suite_run_id: str, case_id: str, replicate_index: int, variant: str) -> str:
    return f"turn-cgbench-{suite_run_id}-{case_id}-r{replicate_index}-{variant}"
