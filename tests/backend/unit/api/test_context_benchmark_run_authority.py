from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace

import pytest

from backend.api.context_benchmark_run_authority import (
    ContextBenchmarkRunConflict,
    ContextBenchmarkRunError,
    ContextBenchmarkRunPrepareRequest,
    ContextBenchmarkRunRepository,
    ContextBenchmarkRunService,
)
from core.capability_packages.thought_graph_context import (
    build_model_benchmark_cases,
    build_model_benchmark_turn_pair,
)
from core.context_graph import FrozenContextRevisions
from core.storage_provider import SQLiteStructuredRecordStore


@dataclass
class _Turns:
    requests: dict[str, dict[str, object]]
    events: dict[str, tuple[dict[str, object], ...]]
    submitted: list[dict[str, object]]

    def get_request(self, turn_id: str):
        value = self.requests.get(turn_id)
        return dict(value) if value is not None else None

    def events_after(self, turn_id: str, after_sequence: int = 0):
        assert after_sequence == 0
        return self.events.get(turn_id, ())

    def submit(self, request):
        payload = dict(request)
        self.submitted.append(payload)
        existing = self.requests.get(str(payload["turn_id"]))
        if existing is None:
            self.requests[str(payload["turn_id"])] = payload
        elif existing != payload:
            raise AssertionError("ordinary turn request changed")
        return {"turn_id": payload["turn_id"], "status": "accepted"}


class _Finalizer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def finalize(self, **values: object):
        self.calls.append(dict(values))
        return {"artifact_ref": "crp://session/benchmark/artifact"}


def _request(*, confirmed: bool = True, run_id: str = "benchmark-run-a"):
    return ContextBenchmarkRunPrepareRequest(
        run_id=run_id,
        suite_run_id="suite-a",
        project_id="project-alpha",
        session_id="session-alpha",
        actor_id="desktop-agent",
        consent_refs=("crp://consents/project-alpha/benchmark-a",),
        confirmation_ref="crp://confirmations/project-alpha/benchmark-a",
        created_at="2026-08-30T00:00:00Z",
        confirmed=confirmed,
    )


def _service(
    tmp_path,
    *,
    revisions: FrozenContextRevisions | None = None,
    pair_builder=build_model_benchmark_turn_pair,
):
    readiness_calls: list[ContextBenchmarkRunPrepareRequest] = []
    issued: list[dict[str, object]] = []
    turns = _Turns({}, {}, [])
    finalizer = _Finalizer()
    active = {
        "revisions": revisions or FrozenContextRevisions(
            "4.1.0", "boundary-r1", "provider-r1", "route-r1", "2.0.0",
        ),
        "compilation_facts": set(),
    }

    def readiness(request):
        readiness_calls.append(request)
        return active["revisions"]

    def issuer(*, case, binding_creation, project_id, revisions):
        binding_id = binding_creation["binding_id"]
        repaired = binding_id not in active["compilation_facts"]
        active["compilation_facts"].add(binding_id)
        issued.append({
            "case_id": case.case_id,
            "project_id": project_id,
            "binding_creation": binding_creation,
            "revisions": revisions,
            "compilation_fact_repaired": repaired,
        })
        return {
            "binding_id": binding_id,
            "project_id": project_id,
            "capability_id": "thought_graph_context",
            "capability_revision": revisions.capability_revision,
            "binding_ref": (
                f"crp://context-bindings/{project_id}/{binding_creation['binding_id']}"
            ),
        }

    repository = ContextBenchmarkRunRepository(
        SQLiteStructuredRecordStore(tmp_path / "context-benchmark.sqlite3")
    )
    service = ContextBenchmarkRunService(
        repository=repository,
        readiness=readiness,
        case_builder=build_model_benchmark_cases,
        pair_builder=pair_builder,
        binding_issuer=issuer,
        capability_id="thought_graph_context",
        capability_revision="4.1.0",
        turn_submitter=turns.submit,
        evidence_reader=turns,
        finalizer=finalizer,
    )
    return service, repository, turns, finalizer, readiness_calls, issued, active


def test_prepare_persists_exact_three_case_six_turn_plan_and_restarts(tmp_path) -> None:
    service, repository, _turns, _finalizer, readiness_calls, issued, _active = _service(tmp_path)

    plan = service.prepare(_request())

    assert len(readiness_calls) == 1
    assert [item["case_id"] for item in issued] == [
        "project_skill", "document", "research_turn",
    ]
    assert len(plan.envelopes) == 6
    assert plan.confirmation_ref == "crp://confirmations/project-alpha/benchmark-a"
    assert [item.variant for item in plan.envelopes] == [
        "linear", "linemap", "linear", "linemap", "linear", "linemap",
    ]
    assert all(item.request["scope"]["project_id"] == "project-alpha" for item in plan.envelopes)
    assert all(
        item.request["privacy"]["consent_refs"] == ["crp://consents/project-alpha/benchmark-a"]
        for item in plan.envelopes
    )

    restarted = ContextBenchmarkRunRepository(
        SQLiteStructuredRecordStore(tmp_path / "context-benchmark.sqlite3")
    ).get("benchmark-run-a")
    assert restarted == plan
    assert repository.get("benchmark-run-a") == plan


def test_prepare_requires_confirmation_and_readiness_before_any_binding_or_turn(tmp_path) -> None:
    service, _repository, turns, _finalizer, readiness_calls, issued, _active = _service(tmp_path)

    with pytest.raises(ContextBenchmarkRunError, match="requires confirmation"):
        service.prepare(_request(confirmed=False))

    assert readiness_calls == []
    assert issued == []
    assert turns.submitted == []

    with pytest.raises(ContextBenchmarkRunError, match="confirmation ref"):
        service.prepare(replace(_request(), confirmation_ref=""))
    assert readiness_calls == []
    assert issued == []

    mismatch = FrozenContextRevisions(
        "other-capability", "boundary-r1", "provider-r1", "route-r1", "2.0.0",
    )
    unavailable, _repo, turns, _finalizer, _calls, issued, _active = _service(
        tmp_path, revisions=mismatch,
    )
    with pytest.raises(ContextBenchmarkRunError, match="capability revision drifted"):
        unavailable.prepare(_request(run_id="benchmark-run-b"))
    assert issued == []
    assert turns.submitted == []


def test_prepare_is_idempotent_but_rejects_same_run_id_with_different_identity(tmp_path) -> None:
    service, _repository, _turns, _finalizer, readiness_calls, issued, _active = _service(tmp_path)
    first = service.prepare(_request())
    replay = service.prepare(_request())

    assert replay == first
    assert len(readiness_calls) == 2
    assert len(issued) == 6

    with pytest.raises(ContextBenchmarkRunConflict, match="identity drifted"):
        service.prepare(replace(_request(), session_id="session-other"))


def test_prepare_repairs_existing_plan_binding_compilation_facts_idempotently(tmp_path) -> None:
    service, _repository, _turns, _finalizer, _calls, issued, active = _service(tmp_path)
    plan = service.prepare(_request())
    assert active["compilation_facts"] == {
        "binding-cgbench-suite-a-project_skill-r0",
        "binding-cgbench-suite-a-document-r0",
        "binding-cgbench-suite-a-research_turn-r0",
    }

    active["compilation_facts"].clear()
    repaired = service.prepare(_request())

    assert repaired == plan
    assert len(issued) == 6
    assert all(item["compilation_fact_repaired"] is True for item in issued[3:])
    assert active["compilation_facts"] == {
        "binding-cgbench-suite-a-project_skill-r0",
        "binding-cgbench-suite-a-document-r0",
        "binding-cgbench-suite-a-research_turn-r0",
    }


def test_prepare_rejects_existing_plan_when_deterministic_candidate_drifts(tmp_path) -> None:
    drift = {"enabled": False}

    def pair_builder(case, **kwargs):
        pair = build_model_benchmark_turn_pair(case, **kwargs)
        if not drift["enabled"]:
            return pair
        linear = deepcopy(dict(pair.linear_turn))
        payload = dict(linear["input"])
        payload["text"] = "candidate drift"
        linear["input"] = payload
        return replace(pair, linear_turn=linear)

    service, _repository, _turns, _finalizer, _calls, issued, _active = _service(
        tmp_path, pair_builder=pair_builder,
    )
    service.prepare(_request())
    drift["enabled"] = True

    with pytest.raises(ContextBenchmarkRunConflict, match="frozen plan drifted"):
        service.prepare(_request())

    assert len(issued) == 6


def test_submit_uses_only_frozen_plan_request_and_detects_durable_drift(tmp_path) -> None:
    service, _repository, turns, _finalizer, _calls, _issued, _active = _service(tmp_path)
    plan = service.prepare(_request())
    first = service.next_envelope(plan.run_id)
    assert first is not None

    with pytest.raises(ContextBenchmarkRunError, match="requires confirmation"):
        service.submit(plan.run_id, first.turn_id, confirmed=False)
    assert turns.submitted == []

    receipt = service.submit(plan.run_id, first.turn_id, confirmed=True)
    assert receipt["turn_id"] == first.turn_id
    assert turns.submitted == [dict(first.request)]
    assert service.next_envelope(plan.run_id).turn_id != first.turn_id

    with pytest.raises(ContextBenchmarkRunConflict, match="already submitted"):
        service.submit(plan.run_id, first.turn_id, confirmed=True)
    assert turns.submitted == [dict(first.request)]

    tampered = dict(first.request)
    tampered["input"] = {"kind": "text", "text": "client replacement", "refs": []}
    turns.requests[first.turn_id] = tampered
    with pytest.raises(ContextBenchmarkRunConflict, match="request drifted"):
        service.statuses(plan.run_id)


@pytest.mark.parametrize("operation", ("submit", "finalize"))
def test_persisted_envelope_input_tamper_fails_closed_before_submit_or_finalize(
    tmp_path, operation: str,
) -> None:
    service, _repository, turns, finalizer, _calls, issued, _active = _service(tmp_path)
    plan = service.prepare(_request())
    records = SQLiteStructuredRecordStore(tmp_path / "context-benchmark.sqlite3")
    record = records.read("context_benchmark_runs", plan.run_id)
    assert record is not None
    payload = deepcopy(dict(record.payload))
    envelopes = list(payload["envelopes"])
    first = dict(envelopes[0])
    request = dict(first["request"])
    input_value = dict(request["input"])
    input_value["text"] = "persisted plan tamper"
    request["input"] = input_value
    first["request"] = request
    envelopes[0] = first
    payload["envelopes"] = envelopes
    with records.begin() as uow:
        uow.put(
            "context_benchmark_runs", plan.run_id, payload,
            expected_revision=record.revision,
        )
        uow.commit()

    if operation == "submit":
        with pytest.raises(ContextBenchmarkRunConflict, match="frozen plan drifted"):
            service.submit(plan.run_id, plan.turn_ids[0], confirmed=True)
    else:
        with pytest.raises(ContextBenchmarkRunConflict, match="frozen plan drifted"):
            service.finalize(plan.run_id)

    assert turns.submitted == []
    assert finalizer.calls == []
    assert len(issued) == 6


def test_finalize_reads_terminal_state_and_delegates_only_after_all_six_complete(tmp_path) -> None:
    service, _repository, turns, finalizer, _calls, _issued, _active = _service(tmp_path)
    plan = service.prepare(_request())

    with pytest.raises(ContextBenchmarkRunError, match="not all completed"):
        service.finalize(plan.run_id)
    assert finalizer.calls == []

    for envelope in plan.envelopes:
        service.submit(plan.run_id, envelope.turn_id, confirmed=True)
        turns.events[envelope.turn_id] = ({
            "turn_id": envelope.turn_id,
            "type": "turn.completed",
        },)

    result = service.finalize(plan.run_id)
    assert result["artifact_ref"] == "crp://session/benchmark/artifact"
    assert finalizer.calls == [{
        "suite_run_id": "suite-a",
        "turn_ids": plan.turn_ids,
        "coordinator_turn_id": plan.coordinator_turn_id,
    }]


@pytest.mark.parametrize(
    "revision_field",
    (
        "capability_revision",
        "boundary_revision",
        "provider_revision",
        "model_route_revision",
        "compiler_revision",
    ),
)
def test_existing_plan_and_submit_fail_closed_on_active_revision_drift(
    tmp_path, revision_field: str,
) -> None:
    service, _repository, turns, _finalizer, _calls, issued, active = _service(tmp_path)
    plan = service.prepare(_request())
    active["revisions"] = replace(
        active["revisions"], **{revision_field: f"drifted-{revision_field}"},
    )

    with pytest.raises(ContextBenchmarkRunError, match="revision"):
        service.prepare(_request())
    with pytest.raises(ContextBenchmarkRunError, match="revision"):
        service.submit(plan.run_id, plan.turn_ids[0], confirmed=True)

    assert len(issued) == 3
    assert turns.submitted == []


@pytest.mark.parametrize("corruption", ("replicate", "consent", "case_variants"))
def test_repository_rejects_corrupt_persisted_plan(tmp_path, corruption: str) -> None:
    service, repository, _turns, _finalizer, _calls, _issued, _active = _service(tmp_path)
    plan = service.prepare(_request())
    records = SQLiteStructuredRecordStore(tmp_path / "context-benchmark.sqlite3")
    record = records.read("context_benchmark_runs", plan.run_id)
    assert record is not None
    payload = deepcopy(dict(record.payload))
    if corruption == "replicate":
        payload["replicate_index"] = 10_000
    elif corruption == "consent":
        payload["consent_refs"] = []
    else:
        envelopes = list(payload["envelopes"])
        first = dict(envelopes[0])
        first["case_id"] = "orphan_case"
        envelopes[0] = first
        payload["envelopes"] = envelopes
    with records.begin() as uow:
        uow.put(
            "context_benchmark_runs", plan.run_id, payload,
            expected_revision=record.revision,
        )
        uow.commit()

    with pytest.raises(ContextBenchmarkRunError, match="benchmark plan"):
        repository.get(plan.run_id)


@pytest.mark.parametrize(
    ("terminal_type", "expected_state"),
    (("turn.failed", "failed"), ("turn.cancelled", "cancelled")),
)
def test_status_projects_ordinary_terminal_failures(
    tmp_path, terminal_type: str, expected_state: str,
) -> None:
    service, _repository, turns, _finalizer, _calls, _issued, _active = _service(tmp_path)
    plan = service.prepare(_request())
    first = plan.envelopes[0]
    service.submit(plan.run_id, first.turn_id, confirmed=True)
    turns.events[first.turn_id] = ({"turn_id": first.turn_id, "type": terminal_type},)

    status = service.statuses(plan.run_id)[0]
    assert status.state == expected_state
    assert status.terminal_event_type == terminal_type
