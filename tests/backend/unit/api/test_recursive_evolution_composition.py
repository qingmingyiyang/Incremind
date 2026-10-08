from pathlib import Path

import pytest

from backend.api.recursive_evolution_composition import (
    RecursiveEvolutionCompositionError,
    SQLiteAITurnVerifiedSource,
    VerifiedRecursiveEvolutionWorkflow,
    build_recursive_evolution_composition,
)
from core.ai_kernel import SQLiteAITurnStore
from core.personal_world_model import WorldEventDraft, WorldEventKind
from core.recursive_evolution import (
    EvaluationVerdict,
    EvolutionEpisode,
    EvolutionEvaluation,
    EvolutionEvent,
    EvolutionPolicy,
    EvolutionProposal,
    EvolutionResourceEnvelope,
    EvolutionTargetKind,
    evolution_event_world_identity,
)


def test_production_composition_constructs_and_recovery_is_idempotent(tmp_path: Path) -> None:
    first = build_recursive_evolution_composition(runtime_root=tmp_path, testing=True)
    second = build_recursive_evolution_composition(runtime_root=tmp_path, testing=True)
    assert first.runtime is not None
    assert second.policy.active("workbench.default") is not None
    first.recover(("project-alpha", "project-alpha"))
    second.recover(("project-alpha",))


def test_local_action_service_derives_stop_evidence_and_replays_the_command(
    tmp_path: Path,
) -> None:
    composition = build_recursive_evolution_composition(
        runtime_root=tmp_path, testing=True,
    )
    episode = EvolutionEpisode(
        "episode-stop", "project-alpha", EvolutionTargetKind.PROMPT_STRATEGY,
        EvolutionPolicy(2, 2, 2, 20, 2, "2026-09-05T00:00:00Z"),
    )
    composition.runtime.create_episode(episode=episode, command_id="episode-stop")
    first = composition.local_actions.execute(
        project_id="project-alpha", episode_id="episode-stop", proposal_id=None,
        action="stop", command_id="local-stop",
    )
    replay = composition.local_actions.execute(
        project_id="project-alpha", episode_id="episode-stop", proposal_id=None,
        action="stop", command_id="local-stop",
    )
    assert first.projection.stop_reason == "user"
    assert replay.replayed is True


def test_source_adapter_rejects_a_raw_or_unrecorded_payload(tmp_path: Path) -> None:
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    source = SQLiteAITurnVerifiedSource(store)
    store.claim_turn({
        "turn_id": "turn-alpha", "session_id": "session-alpha",
        "operation_id": "operation-alpha", "idempotency_key": "key-alpha",
    })
    raw_ref = store.put("turn-alpha", "recursive-evolution", {"kind": "recursive_evolution.evaluation.v1"})
    with pytest.raises(Exception, match="verified external outcome"):
        source.read_verified_outcome(source_ref=raw_ref)


def test_source_adapter_rejects_non_session_forged_ref(tmp_path: Path) -> None:
    source = SQLiteAITurnVerifiedSource(SQLiteAITurnStore(tmp_path / "turns.sqlite3"))
    with pytest.raises(RecursiveEvolutionCompositionError):
        SQLiteAITurnVerifiedSource(source._store, capabilities=("project.write",))  # type: ignore[attr-defined]


def test_source_adapter_accepts_only_a_real_recorded_terminal_success(tmp_path: Path) -> None:
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    request = {"turn_id": "turn-alpha", "session_id": "session-alpha", "operation_id": "operation-alpha", "idempotency_key": "key-alpha"}
    store.claim_turn(request)
    store.append(_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    evidence = {
        "kind": "recursive_evolution.evaluation.v1",
        "evaluation": {"evidence_refs": []},
    }
    source_ref = store.put("turn-alpha", "recursive-evolution", evidence)
    outcome = _event(request, 2, "tool.outcome.recorded", "recorded")
    outcome["data"].update({"capability_id": "recursive_evolution.evaluate", "payload_ref": source_ref})
    outcome["correlation"]["tool_call_id"] = "call-alpha"
    store.append(outcome, expected_sequence=1)
    completed = _event(request, 3, "tool.completed", "completed")
    completed["data"].update({"capability_id": "recursive_evolution.evaluate", "status": "completed"})
    completed["correlation"]["tool_call_id"] = "call-alpha"
    store.append(completed, expected_sequence=2)
    terminal = _event(request, 4, "turn.completed", "completed")
    terminal["data"]["status"] = "completed"
    store.append(terminal, expected_sequence=3)
    assert SQLiteAITurnVerifiedSource(store).read_verified_outcome(source_ref=source_ref) == {
        **evidence,
        "source_ref": source_ref,
        "evaluation": {"evidence_refs": [source_ref]},
    }


def test_verified_workflow_rejects_non_enveloped_candidate_before_any_write() -> None:
    class Source:
        def read_verified_outcome(self, *, source_ref: str):
            return {"kind": "recursive_evolution.candidate.v1", "source_ref": source_ref}

    workflow = VerifiedRecursiveEvolutionWorkflow(Source(), object(), object(), object(), object())  # type: ignore[arg-type]
    with pytest.raises(RecursiveEvolutionCompositionError, match="workflow envelope"):
        workflow.record_candidate(source_ref="crp://session/turn-alpha/outcome")


def test_source_adapter_binds_each_payload_kind_to_its_exact_capability(
    tmp_path: Path,
) -> None:
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    source_ref = _record_verified_payload(
        store,
        turn_id="turn-mismatch",
        capability="recursive_evolution.evaluate",
        payload={"kind": "recursive_evolution.candidate.v1"},
    )
    with pytest.raises(Exception, match="capability is invalid"):
        SQLiteAITurnVerifiedSource(store).read_verified_outcome(
            source_ref=source_ref,
        )


def test_verified_workflow_records_candidate_and_evaluation_then_recovers_provenance(
    tmp_path: Path,
) -> None:
    composition = build_recursive_evolution_composition(
        runtime_root=tmp_path, testing=True,
    )
    episode = EvolutionEpisode(
        "episode-workflow", "project-alpha",
        EvolutionTargetKind.PROMPT_STRATEGY,
        EvolutionPolicy(2, 2, 2, 20, 2, "2099-09-05T00:00:00Z"),
    )
    composition.runtime.create_episode(
        episode=episode, command_id=episode.episode_id,
    )
    envelope = EvolutionResourceEnvelope((), 1, 0, 0, ())
    proposal = EvolutionProposal(
        "proposal-workflow", episode.episode_id, 1, episode.target_kind,
        "proposer-alpha", "executor-alpha",
        "crp://recursive-evolution/prompt-strategies/default.safe/active", "r1",
        None,
        "crp://recursive-evolution/prompt-strategies/default.safe/candidate-r2", "r2",
        envelope, envelope,
    )
    candidate_source = _record_verified_payload(
        composition.turns,
        turn_id="turn-candidate",
        capability="recursive_evolution.candidate",
        payload={
            "kind": "recursive_evolution.candidate.v1",
            "project_id": episode.project_id,
            "proposal": proposal.to_payload(),
            "candidate": {
                "strategy_id": "default.safe",
                "revision": 2,
                "template_id": "safe-template",
                "parameters": {},
                "baseline_ref": proposal.baseline_ref,
                "baseline_revision": proposal.baseline_revision,
                "candidate_ref": proposal.candidate_ref,
                "candidate_revision": proposal.candidate_revision,
            },
        },
    )
    composition.verified_workflow.record_candidate(source_ref=candidate_source)

    unverified = EvolutionEvaluation(
        "evaluation-unverified", episode.episode_id, proposal.proposal_id,
        "evaluator-unverified", 0.4, 0.4, "metric-v1",
        "crp://inputs/unverified", "input-v1",
        "crp://results/unverified-baseline",
        "crp://results/unverified-candidate",
        1, EvaluationVerdict.INCONCLUSIVE, ("crp://evidence/unverified",),
    )
    _append_evolution(
        composition,
        episode.project_id,
        EvolutionEvent.evaluation_recorded(
            unverified,
            composition.world.events(episode.project_id)[-1].recorded_at,
        ),
    )
    composition.recover(composition.world.project_ids())
    unverified_trace = composition.provenance.project(episode.project_id)
    assert len(unverified_trace.subjects) == 1
    assert unverified_trace.validations == ()

    evaluation = EvolutionEvaluation(
        "evaluation-workflow", episode.episode_id, proposal.proposal_id,
        "evaluator-alpha", 0.4, 0.6, "metric-v1",
        "crp://inputs/workflow", "input-v1",
        "crp://results/workflow-baseline", "crp://results/workflow-candidate",
        1, EvaluationVerdict.QUALIFIED, ("crp://evidence/workflow",),
    )
    evaluation_source = _record_verified_payload(
        composition.turns,
        turn_id="turn-evaluation",
        capability="recursive_evolution.evaluate",
        payload={
            "kind": "recursive_evolution.evaluation.v1",
            "project_id": episode.project_id,
            "baseline_ref": proposal.baseline_ref,
            "baseline_revision": proposal.baseline_revision,
            "candidate_ref": proposal.candidate_ref,
            "candidate_revision": proposal.candidate_revision,
            "evaluation": evaluation.to_payload(),
        },
    )
    composition.verified_workflow.record_evaluation(source_ref=evaluation_source)
    composition.recover(composition.world.project_ids())

    trace = composition.provenance.project(episode.project_id)
    assert [item.version.authority_ref for item in trace.subjects] == [
        proposal.candidate_ref,
    ]
    assert [(item.validation_id, item.verdict) for item in trace.validations] == [
        (evaluation.evaluation_id, "verified"),
    ]


def test_recovery_projects_only_verified_evolution_facts_into_shared_provenance(
    tmp_path: Path,
) -> None:
    composition = build_recursive_evolution_composition(
        runtime_root=tmp_path, testing=True,
    )
    policy = EvolutionPolicy(
        2, 2, 2, 20, 2, "2026-09-05T00:00:00Z",
    )
    episode = EvolutionEpisode(
        "episode-alpha", "project-alpha",
        EvolutionTargetKind.PROMPT_STRATEGY, policy,
    )
    envelope = EvolutionResourceEnvelope((), 1, 0, 0, ())
    proposal = EvolutionProposal(
        "proposal-alpha", episode.episode_id, 1, episode.target_kind,
        "proposer-alpha", "executor-alpha",
        "crp://recursive-evolution/prompt-strategies/default.safe/active", "r1",
        None,
        "crp://recursive-evolution/prompt-strategies/default.safe/candidate-r2", "r2",
        envelope, envelope,
    )
    evaluation = EvolutionEvaluation(
        "evaluation-alpha", episode.episode_id, proposal.proposal_id,
        "reviewer-alpha", 0.4, 0.6, "metric-v1",
        "crp://recursive-evolution/inputs/project-alpha/evaluation-alpha", "input-v1",
        "crp://recursive-evolution/results/project-alpha/baseline-alpha",
        "crp://recursive-evolution/results/project-alpha/candidate-alpha",
        1, EvaluationVerdict.QUALIFIED,
        ("crp://recursive-evolution/source/project-alpha/evaluation-alpha",),
    )
    for event in (
        EvolutionEvent.episode_created(episode, "2026-09-04T10:00:00Z"),
        EvolutionEvent.proposal_recorded(proposal, "2026-09-04T10:00:01Z"),
        EvolutionEvent.evaluation_recorded(evaluation, "2026-09-04T10:00:02Z"),
    ):
        _append_evolution(composition, episode.project_id, event)

    composition.recover((episode.project_id, episode.project_id))
    trace = composition.provenance.project(episode.project_id)
    assert trace.subjects == ()
    assert trace.validations == ()


def _event(request: dict[str, str], sequence: int, event_type: str, summary: str) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "event_id": f"event-{sequence:032x}",
        "turn_id": request["turn_id"], "session_id": request["session_id"], "sequence": sequence,
        "type": event_type, "actor": "kernel",
        "correlation": {"step_id": None, "tool_call_id": None, "model_request_id": None, "operation_id": request["operation_id"]},
        "data": {"status": "accepted" if event_type == "turn.accepted" else "running", "summary": summary, "capability_id": None, "payload_ref": None, "receipt_ref": None, "evidence_refs": [], "error_code": None, "retryable": False},
        "occurred_at": "2026-08-25T00:00:00+00:00",
    }


def _record_verified_payload(
    store: SQLiteAITurnStore,
    *,
    turn_id: str,
    capability: str,
    payload: dict[str, object],
) -> str:
    request = {
        "turn_id": turn_id,
        "session_id": "session-alpha",
        "operation_id": f"operation-{turn_id}",
        "idempotency_key": f"key-{turn_id}",
    }
    marker = {"turn-candidate": "a", "turn-evaluation": "b"}.get(turn_id, "c")

    def turn_event(sequence: int, event_type: str, summary: str):
        value = _event(request, sequence, event_type, summary)
        value["event_id"] = f"event-{marker}{sequence:031x}"
        return value

    store.claim_turn(request)
    store.append(turn_event(1, "turn.accepted", "accepted"), expected_sequence=0)
    source_ref = store.put(turn_id, "recursive-evolution", payload)
    outcome = turn_event(2, "tool.outcome.recorded", "recorded")
    outcome["data"].update({"capability_id": capability, "payload_ref": source_ref})
    outcome["correlation"]["tool_call_id"] = f"call-{turn_id}"
    store.append(outcome, expected_sequence=1)
    completed = turn_event(3, "tool.completed", "completed")
    completed["data"].update({"capability_id": capability, "status": "completed"})
    completed["correlation"]["tool_call_id"] = f"call-{turn_id}"
    store.append(completed, expected_sequence=2)
    terminal = turn_event(4, "turn.completed", "completed")
    terminal["data"]["status"] = "completed"
    store.append(terminal, expected_sequence=3)
    return source_ref


def _append_evolution(composition, project_id: str, event: EvolutionEvent) -> None:
    event_id, source_ref, source_revision = evolution_event_world_identity(event)
    composition.world.append_event(WorldEventDraft(
        event_id=event_id,
        project_id=project_id,
        kind=WorldEventKind.EVOLUTION_EVENT_RECORDED,
        actor="system",
        source_ref=source_ref,
        source_revision=source_revision,
        occurred_at=event.recorded_at,
        recorded_at=event.recorded_at,
        payload=event.to_payload(),
    ))
