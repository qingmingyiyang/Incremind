"""Durable, non-executing LineMap replay composition.

This bridge owns only immutable replay facts.  It prepares an ordinary
``context.evaluate`` Turn envelope for the normal Turn Driver, verifies that
Turn through the read-only evidence bridge, and finally appends a graph
revision.  It never starts a model, owns a lease, or schedules recovery.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import hashlib
import json
from typing import Protocol

from backend.api.context_graph_replay_evidence import (
    AITurnEvidenceReader,
    ContextGraphReplayEvidenceError,
    verified_replay_completion_from_turn,
)
from backend.api.context_graph_snapshot_runtime import (
    ContextGraphSnapshotConflict,
    ContextGraphSnapshotRecord,
    ContextGraphSnapshotRepository,
)
from core.context_graph import ContextBinding, ContextGraphNode, ContextGraphSnapshot, ContextPermissionGrant, FrozenContextRevisions
from core.context_graph.replay_completion import (
    NodeGenerationReceipt,
    ReplayCompletionAuthority,
    ReplayCompletionConflict,
    ReplayCompletionError,
    ReplayCompletionRepository,
    ReplayPlan,
    ReplayRequest,
)
from core.context_graph.staleness import stale_replay_order
from core.ai_kernel.contracts import AIKernelContractError, validate_turn_request
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


_PLANS = "context_graph_replay_plans"
_REQUESTS = "context_graph_replay_requests"
_RECEIPTS = "context_graph_replay_receipts"
_SCHEMA = "1.0.0"
_MAX_OUTPUT_BYTES = 131_072


class ContextGraphReplayCompositionError(ValueError):
    """A replay plan, ordinary Turn proof, or final graph fence is invalid."""


class CurrentReplayRevisions(Protocol):
    def __call__(
        self, project_id: str, capability_id: str, binding_id: str,
        allow_remote: bool,
    ) -> FrozenContextRevisions: ...


@dataclass(frozen=True, slots=True)
class ReplayPlanCommand:
    command_id: str
    project_id: str
    graph_id: str
    source_graph_revision: str
    binding_ref: str
    binding: ContextBinding
    session_id: str
    allow_remote: bool
    consent_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedReplayTurn:
    request: ReplayRequest
    predecessor_outputs: tuple[tuple[str, str], ...]
    turn_envelope: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ReplayCompletionResult:
    receipt: NodeGenerationReceipt
    finalized_snapshot: ContextGraphSnapshotRecord | None
    idempotent: bool = False


class _Repository(ReplayCompletionRepository):
    def __init__(
        self,
        records: SQLiteStructuredRecordStore,
        staged_outputs: Mapping[str, str] | None = None,
    ) -> None:
        self._records = records
        self._staged_outputs = dict(staged_outputs or {})
        for output_text in self._staged_outputs.values():
            _validate_output(output_text)

    def get_by_request_id(self, replay_request_id: str) -> NodeGenerationReceipt | None:
        found: list[NodeGenerationReceipt] = []
        for record in self._records.list(_RECEIPTS):
            receipt, _ = _receipt_from_payload(record.payload)
            if receipt.replay_request_id == replay_request_id:
                found.append(receipt)
        if len(found) > 1:
            raise ContextGraphReplayCompositionError("replay_request_receipt_ambiguous")
        return found[0] if found else None

    def get_by_plan_node(self, replay_plan_id: str, node_id: str) -> NodeGenerationReceipt | None:
        found: list[NodeGenerationReceipt] = []
        for record in self._records.list(_RECEIPTS):
            receipt, _ = _receipt_from_payload(record.payload)
            if (receipt.replay_plan_id, receipt.node_id) == (replay_plan_id, node_id):
                found.append(receipt)
        if len(found) > 1:
            raise ContextGraphReplayCompositionError("replay_receipt_identity_ambiguous")
        return found[0] if found else None

    def append_if_absent(self, receipt: NodeGenerationReceipt) -> NodeGenerationReceipt:
        object_id = _id("receipt", receipt.replay_request_id)
        output_text = self._staged_outputs.get(receipt.replay_request_id)
        if output_text is None:
            raise ReplayCompletionConflict("replay_output_not_staged")
        payload = _receipt_payload(receipt, output_text=output_text)
        try:
            with self._records.begin() as uow:
                existing = uow.read(_RECEIPTS, object_id)
                if existing is not None:
                    prior, prior_output = _receipt_from_payload(existing.payload)
                    if prior != receipt or prior_output != output_text:
                        raise ReplayCompletionConflict("replay_request_idempotency_conflict")
                    return prior
                uow.put(_RECEIPTS, object_id, payload, expected_revision=0)
                uow.commit()
        except SQLiteUnitOfWorkConflict as error:
            raise ReplayCompletionConflict("replay_receipt_write_conflict") from error
        return receipt


class ContextGraphReplayCompositionService:
    """Persistent composition that delegates execution to the normal Turn path."""

    def __init__(
        self, records: SQLiteStructuredRecordStore, snapshots: ContextGraphSnapshotRepository,
        current_revisions: CurrentReplayRevisions, clock: Callable[[], str],
    ) -> None:
        self._records = records
        self._snapshots = snapshots
        self._current_revisions = current_revisions
        self._clock = clock
        self._receipts = _Repository(records)

    def create_plan(self, command: ReplayPlanCommand) -> ReplayPlan:
        self._validate_command(command)
        source = self._exact_source(command)
        revisions = self._revisions(
            source, command.binding_ref, command.allow_remote,
        )
        order = stale_replay_order(source.snapshot)
        if not order or command.binding.stale_nodes != order or _binding_revisions(command.binding) != revisions:
            raise ContextGraphReplayCompositionError("replay_staleness_not_confirmed")
        stale = command.binding.budget_explanation.get("staleness")
        if not isinstance(stale, Mapping) or stale.get("confirmation_present") is not True or stale.get("current_graph_revision") != source.graph_revision or tuple(stale.get("affected_node_ids", ())) != order or tuple(stale.get("replay_order", ())) != order or not isinstance(stale.get("confirmed_by"), str) or not stale["confirmed_by"].strip() or not isinstance(stale.get("confirmed_at"), str) or not stale["confirmed_at"].strip():
            raise ContextGraphReplayCompositionError("replay_staleness_not_confirmed")
        plan_id = _id("plan", command.command_id)
        result_revision = "replay-" + _digest({"plan": plan_id, "source": source.graph_revision})[:24]
        plan = ReplayPlan.from_evaluated_snapshot(
            replay_plan_id=plan_id, result_graph_revision=result_revision,
            source_snapshot=source.snapshot, binding_ref=command.binding_ref, revisions=revisions,
            input_fingerprints=_input_fingerprints(
                plan_id, order, command.binding_ref, source.graph_revision,
            ),
        )
        object_id = _id("plan-record", plan_id)
        try:
            with self._records.begin() as uow:
                existing = uow.read(_PLANS, object_id)
                if existing is not None:
                    prior, prior_transport = _plan_with_transport_from_payload(existing.payload)
                    expected_transport = _command_transport(
                        command, str(prior_transport["result_created_at"]),
                    )
                    if prior != plan or prior_transport != expected_transport:
                        raise ContextGraphReplayCompositionError("replay_plan_idempotency_conflict")
                    return prior
                transport = _command_transport(command, self._time())
                payload = _plan_payload(plan, transport)
                uow.put(_PLANS, object_id, payload, expected_revision=0)
                uow.commit()
        except SQLiteUnitOfWorkConflict as error:
            raise ContextGraphReplayCompositionError("replay_plan_write_conflict") from error
        return plan

    def prepare_next(self, replay_plan_id: str) -> PreparedReplayTurn | None:
        plan = self._plan(replay_plan_id)
        transport = self._plan_transport(replay_plan_id)
        completed = self._completed(plan)
        if len(completed) == len(plan.node_ids):
            return None
        self._assert_current(plan)
        index = len(completed)
        node_id = plan.node_ids[index]
        predecessor = tuple(item.receipt_ref for item in completed)
        request = ReplayRequest(
            replay_request_id=_id("request", f"{plan.replay_plan_id}:{node_id}"), replay_plan_id=plan.replay_plan_id,
            project_id=plan.project_id, graph_id=plan.graph_id, source_graph_revision=plan.source_graph_revision,
            result_graph_revision=plan.result_graph_revision, node_id=node_id, plan_index=index,
            input_fingerprint=plan.input_fingerprint_for(node_id), binding_ref=plan.binding_ref,
            turn_id=_id("turn", f"{plan.replay_plan_id}:{node_id}"),
            turn_operation_id=_id("operation", f"{plan.replay_plan_id}:{node_id}"),
            predecessor_receipt_refs=predecessor, revisions=plan.revisions,
        )
        outputs = tuple((item.node_id, self._receipt_output(item)) for item in completed)
        envelope = _turn_envelope(
            request, transport, outputs, str(transport["result_created_at"]),
        )
        object_id = _id("request-record", request.replay_request_id)
        try:
            with self._records.begin() as uow:
                existing = uow.read(_REQUESTS, object_id)
                if existing is not None:
                    prior, prior_envelope = _request_with_envelope_from_payload(existing.payload)
                    if prior != request or prior_envelope != envelope:
                        raise ContextGraphReplayCompositionError("replay_request_idempotency_conflict")
                    request = prior
                else:
                    uow.put(_REQUESTS, object_id, _request_payload(request, envelope), expected_revision=0)
                    uow.commit()
        except SQLiteUnitOfWorkConflict as error:
            raise ContextGraphReplayCompositionError("replay_request_write_conflict") from error
        return PreparedReplayTurn(request, outputs, envelope)

    def accept_completed_turn(self, replay_plan_id: str, evidence: AITurnEvidenceReader) -> ReplayCompletionResult:
        plan = self._plan(replay_plan_id)
        prepared = self.prepare_next(plan.replay_plan_id)
        if prepared is None:
            final = self._finalize(plan)
            receipt = self._completed(plan)[-1]
            return ReplayCompletionResult(receipt, final, True)
        try:
            verified = verified_replay_completion_from_turn(
                store=evidence, replay_request=prepared.request,
                expected_operation_id=prepared.request.turn_operation_id,
                expected_turn_request=prepared.turn_envelope,
            )
        except (ContextGraphReplayEvidenceError, AttributeError, TypeError, ValueError) as error:
            raise ContextGraphReplayCompositionError("replay_turn_evidence_rejected") from error
        _validate_output(verified.output_text)
        accepting_repository = _Repository(
            self._records,
            {prepared.request.replay_request_id: verified.output_text},
        )
        authority = ReplayCompletionAuthority(accepting_repository, self)
        try:
            receipt = authority.accept(plan=plan, request=prepared.request, completion=verified.completion)
        except (ReplayCompletionError, ReplayCompletionConflict) as error:
            raise ContextGraphReplayCompositionError(str(error)) from error
        final = self._finalize(plan) if len(self._completed(plan)) == len(plan.node_ids) else None
        return ReplayCompletionResult(receipt, final)

    def assert_session(self, replay_plan_id: str, session_id: str) -> None:
        """Authorize a local API continuation without exposing stored transport."""

        if not isinstance(session_id, str) or not session_id.strip():
            raise ContextGraphReplayCompositionError("replay_session_invalid")
        transport = self._plan_transport(replay_plan_id)
        if transport.get("session_id") != session_id:
            raise ContextGraphReplayCompositionError("replay_session_drift")

    # ReplayRevisionAuthority protocol used by the Core receipt authority.
    def assert_current(
        self, *, replay_plan_id: str, project_id: str, graph_id: str,
        source_graph_revision: str, binding_ref: str,
        revisions: FrozenContextRevisions,
    ) -> None:
        plan = self._plan(replay_plan_id)
        if (
            plan.project_id, plan.graph_id, plan.source_graph_revision,
            plan.binding_ref, plan.revisions,
        ) != (
            project_id, graph_id, source_graph_revision, binding_ref, revisions,
        ):
            raise ReplayCompletionError("replay_plan_revision_identity_drift")
        source = self._snapshots.current(project_id, graph_id)
        if source is None or source.graph_revision != source_graph_revision:
            raise ReplayCompletionError("replay_source_graph_drift")
        transport = self._plan_transport(replay_plan_id)
        allow_remote = transport.get("allow_remote")
        if type(allow_remote) is not bool:
            raise ReplayCompletionError("replay_transport_policy_invalid")
        if self._revisions(source, binding_ref, allow_remote) != revisions:
            raise ReplayCompletionError("replay_revision_drift")

    def _finalize(self, plan: ReplayPlan) -> ContextGraphSnapshotRecord:
        current = self._snapshots.current(plan.project_id, plan.graph_id)
        expected = self._expected_result_snapshot(plan)
        source = self._exact_source_for(plan)
        receipts = self._completed(plan)
        grant, evidence = self._result_authority(plan, source, receipts)
        if current is not None and current.graph_revision == plan.result_graph_revision:
            if not self._result_record_matches(
                current, expected, source, grant, evidence,
            ):
                raise ContextGraphReplayCompositionError("replay_result_snapshot_identity_drift")
            return current
        self._assert_current(plan)
        if len(receipts) != len(plan.node_ids):
            raise ContextGraphReplayCompositionError("replay_not_complete")
        snapshot = expected
        try:
            return self._snapshots.append(snapshot, plan.source_graph_revision,
                capability_id=source.capability_id, capability_revision=source.capability_revision,
                permission_grant=grant, permission_evidence_refs=evidence)
        except ContextGraphSnapshotConflict as error:
            current = self._snapshots.current(plan.project_id, plan.graph_id)
            if current is not None and current.graph_revision == plan.result_graph_revision:
                if self._result_record_matches(
                    current, expected, source, grant, evidence,
                ):
                    return current
                raise ContextGraphReplayCompositionError("replay_result_snapshot_identity_drift") from error
            raise ContextGraphReplayCompositionError("replay_final_snapshot_conflict") from error

    def _replayed_node(self, node: ContextGraphNode, receipt: NodeGenerationReceipt | None) -> ContextGraphNode:
        if receipt is None:
            return node
        metadata = dict(node.metadata)
        metadata["content"] = self._receipt_output(receipt)
        metadata["replay_receipt_ref"] = receipt.receipt_ref
        metadata["replay_terminal_event_ref"] = receipt.terminal_event_ref
        return replace(node, content_ref=receipt.output_ref, content_revision=receipt.receipt_ref,
            updated_at=receipt.accepted_at, stale=False, stale_reason=None, metadata=metadata)

    def _completed(self, plan: ReplayPlan) -> tuple[NodeGenerationReceipt, ...]:
        values: list[NodeGenerationReceipt] = []
        for node_id in plan.node_ids:
            receipt = self._receipts.get_by_plan_node(plan.replay_plan_id, node_id)
            if receipt is None:
                break
            values.append(receipt)
        # A later receipt without every earlier receipt proves tampering.
        for node_id in plan.node_ids[len(values):]:
            if self._receipts.get_by_plan_node(plan.replay_plan_id, node_id) is not None:
                raise ContextGraphReplayCompositionError("replay_receipt_order_corrupt")
        return tuple(values)

    def _plan(self, replay_plan_id: str) -> ReplayPlan:
        record = self._records.read(_PLANS, _id("plan-record", replay_plan_id))
        if record is None:
            raise ContextGraphReplayCompositionError("replay_plan_unavailable")
        return _plan_from_payload(record.payload)

    def _plan_transport(self, replay_plan_id: str) -> Mapping[str, object]:
        record = self._records.read(_PLANS, _id("plan-record", replay_plan_id))
        if record is None:
            raise ContextGraphReplayCompositionError("replay_plan_unavailable")
        return _plan_with_transport_from_payload(record.payload)[1]

    def _expected_result_snapshot(self, plan: ReplayPlan) -> ContextGraphSnapshot:
        source = self._exact_source_for(plan)
        receipts = self._completed(plan)
        if len(receipts) != len(plan.node_ids):
            raise ContextGraphReplayCompositionError("replay_not_complete")
        receipt_by_node = {item.node_id: item for item in receipts}
        nodes = tuple(self._replayed_node(node, receipt_by_node.get(node.node_id)) for node in source.snapshot.nodes)
        transport = self._plan_transport(plan.replay_plan_id)
        return replace(source.snapshot, graph_revision=plan.result_graph_revision,
            source_revision=f"replay:{plan.replay_plan_id}", nodes=nodes,
            provenance=replace(source.snapshot.provenance, source_revision=f"replay:{plan.replay_plan_id}", imported_at=str(transport["result_created_at"])))

    @staticmethod
    def _result_authority(
        plan: ReplayPlan,
        source: ContextGraphSnapshotRecord,
        receipts: tuple[NodeGenerationReceipt, ...],
    ) -> tuple[ContextPermissionGrant, tuple[str, ...]]:
        grant = ContextPermissionGrant(
            source.project_id,
            f"replay:{plan.replay_plan_id}",
            frozenset(
                set(source.permission_grant.allowed_content_refs)
                | {item.output_ref for item in receipts}
            ),
        )
        evidence = tuple(sorted(
            set(source.permission_evidence_refs)
            | {item.receipt_ref for item in receipts}
        ))
        return grant, evidence

    @staticmethod
    def _result_record_matches(
        record: ContextGraphSnapshotRecord,
        expected: ContextGraphSnapshot,
        source: ContextGraphSnapshotRecord,
        grant: ContextPermissionGrant,
        evidence: tuple[str, ...],
    ) -> bool:
        return (
            record.snapshot == expected
            and record.predecessor == source.graph_revision
            and record.capability_id == source.capability_id
            and record.capability_revision == source.capability_revision
            and record.permission_grant == grant
            and record.permission_evidence_refs == evidence
        )

    def _receipt_output(self, receipt: NodeGenerationReceipt) -> str:
        record = self._records.read(_RECEIPTS, _id("receipt", receipt.replay_request_id))
        if record is None:
            raise ContextGraphReplayCompositionError("replay_receipt_missing")
        _, text = _receipt_from_payload(record.payload)
        if not text:
            raise ContextGraphReplayCompositionError("replay_receipt_output_unavailable")
        return text

    def _exact_source(self, command: ReplayPlanCommand) -> ContextGraphSnapshotRecord:
        record = self._snapshots.current(command.project_id, command.graph_id)
        if record is None or record.graph_revision != command.source_graph_revision:
            raise ContextGraphReplayCompositionError("replay_source_graph_drift")
        return record

    def _exact_source_for(self, plan: ReplayPlan) -> ContextGraphSnapshotRecord:
        record = self._snapshots.revision(plan.project_id, plan.graph_id, plan.source_graph_revision)
        if record is None:
            raise ContextGraphReplayCompositionError("replay_source_graph_unavailable")
        return record

    def _revisions(
        self, source: ContextGraphSnapshotRecord, binding_ref: str,
        allow_remote: bool,
    ) -> FrozenContextRevisions:
        current = self._current_revisions(
            source.project_id, source.capability_id,
            _binding_id(binding_ref, source.project_id), allow_remote,
        )
        if not isinstance(current, FrozenContextRevisions) or current.capability_revision != source.capability_revision:
            raise ContextGraphReplayCompositionError("replay_revision_drift")
        return current

    def _assert_current(self, plan: ReplayPlan) -> None:
        try:
            self.assert_current(
                replay_plan_id=plan.replay_plan_id,
                project_id=plan.project_id, graph_id=plan.graph_id,
                source_graph_revision=plan.source_graph_revision,
                binding_ref=plan.binding_ref, revisions=plan.revisions,
            )
        except ReplayCompletionError as error:
            raise ContextGraphReplayCompositionError(str(error)) from error

    def _time(self) -> str:
        value = self._clock()
        if not isinstance(value, str) or not value.strip():
            raise ContextGraphReplayCompositionError("replay_clock_unavailable")
        return value

    @staticmethod
    def _validate_command(command: object) -> None:
        if not isinstance(command, ReplayPlanCommand) or not isinstance(command.binding, ContextBinding):
            raise ContextGraphReplayCompositionError("replay_plan_command_invalid")
        if any(not isinstance(value, str) or not value.strip() for value in (
            command.command_id, command.project_id, command.graph_id, command.source_graph_revision, command.binding_ref,
            command.session_id,
        )) or type(command.allow_remote) is not bool or type(command.consent_refs) is not tuple or any(not isinstance(value, str) or not value.strip() for value in command.consent_refs) or len(command.consent_refs) != len(set(command.consent_refs)) or (command.allow_remote and not command.consent_refs) or (command.binding.graph_id, command.binding.graph_revision) != (command.graph_id, command.source_graph_revision):
            raise ContextGraphReplayCompositionError("replay_plan_command_invalid")


def _id(prefix: str, value: str) -> str:
    return f"{prefix}-{_digest(value)[:24]}"


def _binding_id(binding_ref: str, project_id: str) -> str:
    prefix = f"crp://context-bindings/{project_id}/"
    if (
        not isinstance(binding_ref, str)
        or not binding_ref.startswith(prefix)
        or not binding_ref[len(prefix):]
        or "/" in binding_ref[len(prefix):]
    ):
        raise ContextGraphReplayCompositionError("replay_binding_ref_invalid")
    return binding_ref[len(prefix):]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _input_fingerprints(
    replay_plan_id: str,
    node_ids: tuple[str, ...],
    binding_ref: str,
    source_graph_revision: str,
) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for index, node_id in enumerate(node_ids):
        predecessor_receipts = [
            f"replay-receipt:{_id('request', f'{replay_plan_id}:{prior_node_id}')}"
            for prior_node_id in node_ids[:index]
        ]
        fingerprints[node_id] = _digest({
            "plan": replay_plan_id,
            "node": node_id,
            "binding": binding_ref,
            "source": source_graph_revision,
            "predecessor_receipts": predecessor_receipts,
        })
    return fingerprints


def _revision_payload(value: FrozenContextRevisions) -> dict[str, str]:
    return {field: str(getattr(value, field)) for field in value.__dataclass_fields__}


def _binding_revisions(binding: ContextBinding) -> FrozenContextRevisions:
    return FrozenContextRevisions(
        binding.capability_revision, binding.boundary_revision, binding.provider_revision,
        binding.model_route_revision, binding.compiler_revision,
    )


def _revisions_from_payload(value: object) -> FrozenContextRevisions:
    if not isinstance(value, Mapping):
        raise ContextGraphReplayCompositionError("replay_revisions_invalid")
    try:
        return FrozenContextRevisions(**dict(value))
    except (TypeError, ValueError) as error:
        raise ContextGraphReplayCompositionError("replay_revisions_invalid") from error


def _command_transport(command: ReplayPlanCommand, result_created_at: str) -> dict[str, object]:
    return {"session_id": command.session_id, "allow_remote": command.allow_remote, "consent_refs": list(command.consent_refs), "result_created_at": result_created_at}


def _plan_payload(plan: ReplayPlan, transport: Mapping[str, object]) -> dict[str, object]:
    return {"schema_version": _SCHEMA, "plan": {"replay_plan_id": plan.replay_plan_id, "project_id": plan.project_id, "graph_id": plan.graph_id, "source_graph_revision": plan.source_graph_revision, "result_graph_revision": plan.result_graph_revision, "node_ids": list(plan.node_ids), "input_fingerprints": [list(item) for item in plan.input_fingerprints], "binding_ref": plan.binding_ref, "revisions": _revision_payload(plan.revisions)}, "transport": dict(transport)}


def _plan_from_payload(value: object) -> ReplayPlan:
    return _plan_with_transport_from_payload(value)[0]


def _plan_with_transport_from_payload(value: object) -> tuple[ReplayPlan, Mapping[str, object]]:
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "plan", "transport"} or value.get("schema_version") != _SCHEMA or not isinstance(value.get("plan"), Mapping) or not isinstance(value.get("transport"), Mapping):
        raise ContextGraphReplayCompositionError("replay_plan_payload_invalid")
    raw, transport = value["plan"], value["transport"]
    if set(transport) != {"session_id", "allow_remote", "consent_refs", "result_created_at"} or not isinstance(transport.get("session_id"), str) or not transport["session_id"].strip() or type(transport.get("allow_remote")) is not bool or not isinstance(transport.get("consent_refs"), list) or any(not isinstance(item, str) or not item.strip() for item in transport["consent_refs"]) or len(transport["consent_refs"]) != len(set(transport["consent_refs"])) or (transport["allow_remote"] and not transport["consent_refs"]) or not isinstance(transport.get("result_created_at"), str) or not transport["result_created_at"].strip():
        raise ContextGraphReplayCompositionError("replay_plan_payload_invalid")
    try:
        return ReplayPlan(**{**dict(raw), "node_ids": tuple(raw["node_ids"]), "input_fingerprints": tuple(tuple(item) for item in raw["input_fingerprints"]), "revisions": _revisions_from_payload(raw["revisions"])}), dict(transport)
    except (KeyError, TypeError, ValueError) as error:
        raise ContextGraphReplayCompositionError("replay_plan_payload_invalid") from error


def _request_payload(request: ReplayRequest, envelope: Mapping[str, object]) -> dict[str, object]:
    data = {field: getattr(request, field) for field in request.__dataclass_fields__}
    data["predecessor_receipt_refs"] = list(request.predecessor_receipt_refs)
    data["revisions"] = _revision_payload(request.revisions)
    return {"schema_version": _SCHEMA, "request": data, "turn_envelope": dict(envelope)}


def _request_from_payload(value: object) -> ReplayRequest:
    return _request_with_envelope_from_payload(value)[0]


def _request_with_envelope_from_payload(value: object) -> tuple[ReplayRequest, Mapping[str, object]]:
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "request", "turn_envelope"} or value.get("schema_version") != _SCHEMA or not isinstance(value.get("request"), Mapping) or not isinstance(value.get("turn_envelope"), Mapping):
        raise ContextGraphReplayCompositionError("replay_request_payload_invalid")
    raw, envelope = value["request"], value["turn_envelope"]
    try:
        request = ReplayRequest(**{**dict(raw), "predecessor_receipt_refs": tuple(raw["predecessor_receipt_refs"]), "revisions": _revisions_from_payload(raw["revisions"])})
        parsed = validate_turn_request(envelope)
        if parsed.get("turn_id") != request.turn_id or parsed.get("operation_id") != request.turn_operation_id:
            raise ContextGraphReplayCompositionError("replay_request_envelope_identity_drift")
        return request, dict(parsed)
    except (AIKernelContractError, KeyError, TypeError, ValueError) as error:
        raise ContextGraphReplayCompositionError("replay_request_payload_invalid") from error


def _receipt_payload(receipt: NodeGenerationReceipt, output_text: str) -> dict[str, object]:
    data = {field: getattr(receipt, field) for field in receipt.__dataclass_fields__}
    data["predecessor_receipt_refs"] = list(receipt.predecessor_receipt_refs)
    data["revisions"] = _revision_payload(receipt.revisions)
    return {"schema_version": _SCHEMA, "receipt": data, "output_text": output_text}


def _receipt_from_payload(value: object) -> tuple[NodeGenerationReceipt, str]:
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "receipt", "output_text"} or value.get("schema_version") != _SCHEMA or not isinstance(value.get("receipt"), Mapping) or not isinstance(value.get("output_text"), str):
        raise ContextGraphReplayCompositionError("replay_receipt_payload_invalid")
    raw = value["receipt"]
    try:
        output = str(value["output_text"])
        _validate_output(output)
        return NodeGenerationReceipt(**{**dict(raw), "predecessor_receipt_refs": tuple(raw["predecessor_receipt_refs"]), "revisions": _revisions_from_payload(raw["revisions"])}), output
    except (KeyError, TypeError, ValueError) as error:
        raise ContextGraphReplayCompositionError("replay_receipt_payload_invalid") from error


def _validate_output(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise ContextGraphReplayCompositionError("replay_output_limit_exceeded")


def _turn_envelope(request: ReplayRequest, transport: Mapping[str, object], predecessor_outputs: tuple[tuple[str, str], ...], created_at: str) -> Mapping[str, object]:
    session_id = transport.get("session_id")
    allow_remote = transport.get("allow_remote")
    consent_refs = transport.get("consent_refs")
    if not isinstance(session_id, str) or not session_id.strip() or type(allow_remote) is not bool or not isinstance(consent_refs, list):
        raise ContextGraphReplayCompositionError("replay_plan_transport_invalid")
    for _, output in predecessor_outputs:
        _validate_output(output)
    predecessor_data = [{"node_id": node_id, "content": output, "untrusted_context": True} for node_id, output in predecessor_outputs]
    text = json.dumps({"kind": "linemap_replay", "target_node_id": request.node_id, "input_fingerprint": request.input_fingerprint, "predecessor_outputs": predecessor_data, "untrusted_context": True}, ensure_ascii=False, separators=(",", ":"))
    envelope: dict[str, object] = {
        "schema_version": "1.0.0", "turn_id": request.turn_id, "session_id": session_id,
        "operation_id": request.turn_operation_id, "idempotency_key": request.replay_request_id,
        "scope": {"kind": "project", "project_id": request.project_id, "series_id": None, "authority": None},
        "input": {"kind": "text", "text": text, "refs": [{"kind": "context_binding", "object_id": request.binding_ref.rsplit("/", 1)[-1], "uri": request.binding_ref}]},
        "desired_outcome": "context.evaluate",
        "privacy": {"mode": "remote_allowed" if allow_remote else "local_only", "allow_remote": allow_remote, "pii": "possible", "consent_refs": list(consent_refs), "retention": "local_durable"},
        "capability_policy": {"allowed": [], "denied": [], "require_approval": []},
        "context_policy": {"include_project_skill": False, "include_memory": False, "include_session_history": False, "max_context_bytes": 262_144},
        "approval_policy": {"mode": "risk_based", "auto_approve_read_only": True}, "created_at": created_at,
    }
    try:
        return validate_turn_request(envelope)
    except AIKernelContractError as error:
        raise ContextGraphReplayCompositionError("replay_turn_envelope_invalid") from error
