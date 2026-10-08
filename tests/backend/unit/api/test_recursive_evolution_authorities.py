from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.recursive_evolution_authorities import (
    LocalHumanConfirmationRequest,
    RecursiveEvolutionAuthority,
    RecursiveEvolutionAuthorityConflict,
    RecursiveEvolutionAuthorityError,
)
from core.recursive_evolution import (
    EvaluationVerdict,
    EvolutionEvaluation,
    EvolutionProposal,
    EvolutionResourceEnvelope,
    EvolutionTargetKind,
)
from core.storage_provider import SQLiteStructuredRecordStore


PROJECT = "project-alpha"
EPISODE = "episode-alpha"
PROPOSAL = "proposal-alpha"
SOURCE = "crp://external-tool/evaluation-alpha"


class _Source:
    def __init__(self) -> None:
        self.outcomes: dict[str, dict[str, object]] = {}

    def read_verified_outcome(self, *, source_ref: str):
        return self.outcomes[source_ref]


def _proposal(*, candidate_revision: str = "candidate-v1") -> EvolutionProposal:
    envelope = EvolutionResourceEnvelope(("read",), 1, 0, 0, ())
    return EvolutionProposal(
        PROPOSAL, EPISODE, 1, EvolutionTargetKind.AGENT_PROFILE,
        "proposer-alpha", "executor-alpha", "crp://target/baseline", "base-v1",
        None, "crp://target/candidate", candidate_revision, envelope, envelope,
    )


def _evaluation(source: str = SOURCE, *, evaluator: str = "evaluator-alpha") -> dict[str, object]:
    return EvolutionEvaluation(
        "evaluation-alpha", EPISODE, PROPOSAL, evaluator, 0.5, 0.7, "metric-v1",
        "crp://inputs/evaluation-alpha", "input-v1", "crp://results/baseline-alpha",
        "crp://results/candidate-alpha", 1, EvaluationVerdict.QUALIFIED, (source,),
    ).to_payload()


def _authority(tmp_path: Path):
    source = _Source()
    authority = RecursiveEvolutionAuthority(
        SQLiteStructuredRecordStore(tmp_path / "authority.sqlite3"), source,
    )
    return authority, source


def test_evaluation_receipt_requires_verified_source_and_rejects_scope_or_identity_drift(tmp_path: Path) -> None:
    authority, source = _authority(tmp_path)
    source.outcomes[SOURCE] = {
        "kind": "recursive_evolution.evaluation.v1", "source_ref": SOURCE,
        "project_id": PROJECT, "baseline_ref": "crp://target/baseline",
        "baseline_revision": "base-v1", "candidate_ref": "crp://target/candidate",
        "candidate_revision": "candidate-v1", "evaluation": _evaluation(),
    }
    receipt = authority.record_evaluation_receipt(project_id=PROJECT, proposal=_proposal(), source_ref=SOURCE)
    evaluation = authority.verify_evaluation(project_id=PROJECT, proposal=_proposal(), receipt_ref=receipt)
    assert evaluation.evaluation_id == "evaluation-alpha"
    with pytest.raises(RecursiveEvolutionAuthorityError, match="identity drifted"):
        authority.verify_evaluation(
            project_id=PROJECT, proposal=_proposal(candidate_revision="candidate-v2"), receipt_ref=receipt,
        )
    source.outcomes[SOURCE]["evaluation"] = _evaluation(evaluator="proposer-alpha")
    with pytest.raises(RecursiveEvolutionAuthorityError, match="independence"):
        authority.record_evaluation_receipt(project_id=PROJECT, proposal=_proposal(), source_ref=SOURCE)
    stored = authority._records.read("recursive_evolution_evaluation_receipts", "evaluation-alpha")  # noqa: SLF001 - durable corruption gate
    assert stored is not None
    tampered = dict(stored.payload)
    tampered["schema_version"] = "2"
    with authority._records.begin() as uow:  # noqa: SLF001 - durable corruption gate
        uow.put("recursive_evolution_evaluation_receipts", "evaluation-alpha", tampered, expected_revision=stored.revision)
        uow.commit()
    with pytest.raises(RecursiveEvolutionAuthorityError, match="identity drifted"):
        authority.verify_evaluation(project_id=PROJECT, proposal=_proposal(), receipt_ref=receipt)
    with pytest.raises(RecursiveEvolutionAuthorityConflict, match="reference drifted"):
        authority.verify_evaluation(
            project_id="project-other", proposal=_proposal(), receipt_ref=receipt,
        )


def test_canary_replay_and_local_human_command_binding_fail_closed(tmp_path: Path) -> None:
    authority, source = _authority(tmp_path)
    canary_source = "crp://external-tool/canary-alpha"
    source.outcomes[canary_source] = {
        "kind": "recursive_evolution.canary.v1", "source_ref": canary_source,
        "project_id": PROJECT, "observation_id": "observation-alpha", "episode_id": EPISODE,
        "proposal_id": PROPOSAL, "candidate_ref": "crp://target/candidate",
        "candidate_revision": "candidate-v1", "passed": True, "samples": 2,
        "evidence_refs": [canary_source],
    }
    evidence_ref = authority.record_canary_observation(
        project_id=PROJECT, proposal=_proposal(), source_ref=canary_source,
    )
    observation = authority.verify_canary(
        project_id=PROJECT, proposal=_proposal(), evidence_ref=evidence_ref,
    )
    assert observation.samples == 2
    assert observation.evidence_refs == (canary_source, evidence_ref)
    source.outcomes[canary_source]["samples"] = 3
    with pytest.raises(RecursiveEvolutionAuthorityConflict, match="identity drifted"):
        authority.record_canary_observation(project_id=PROJECT, proposal=_proposal(), source_ref=canary_source)

    ref = authority.create(LocalHumanConfirmationRequest(
        PROJECT, "human-alpha", "promote-alpha", "promote", PROPOSAL, "2026-09-04T10:00:00Z",
    ))
    authority.verify_local_human(
        project_id=PROJECT, user_id="human-alpha", command_id="promote-alpha",
        action="promote", proposal_id=PROPOSAL, confirmation_ref=ref,
    )
    with pytest.raises(RecursiveEvolutionAuthorityConflict, match="authority drifted"):
        authority.verify_local_human(
            project_id=PROJECT, user_id="human-alpha", command_id="promote-alpha",
            action="rollback", proposal_id=PROPOSAL, confirmation_ref=ref,
        )


def test_local_confirmation_retry_keeps_original_trusted_time_and_rejects_action_drift(
    tmp_path: Path,
) -> None:
    authority, _source = _authority(tmp_path)
    first = authority.create_or_replay(LocalHumanConfirmationRequest(
        PROJECT, "human-alpha", "decision-alpha", "promote", PROPOSAL,
        "2026-09-04T10:00:00Z",
    ))
    replay = authority.create_or_replay(LocalHumanConfirmationRequest(
        PROJECT, "human-alpha", "decision-alpha", "promote", PROPOSAL,
        "2026-09-04T10:05:00Z",
    ))
    assert replay == first
    with pytest.raises(RecursiveEvolutionAuthorityConflict, match="authority drifted"):
        authority.create_or_replay(LocalHumanConfirmationRequest(
            PROJECT, "human-alpha", "decision-alpha", "rollback", PROPOSAL,
            "2026-09-04T10:05:00Z",
        ))
