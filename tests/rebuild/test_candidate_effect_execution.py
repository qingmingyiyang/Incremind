from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from core.effect_log import EFFECT_V2, NOT_APPLICABLE, V2_REVISION_KEYS, EffectClass, EffectIntent, EffectLog, EffectState, GateDecision, GateDecisionFact
from core.job_runner.job_projection import JobProjectionBuilder, initialize_job_projection_schema
from core.product_core.candidate_effect_execution import (
    EFFECT_KIND, INTENT_SCHEMA, RECEIPT_KIND, RECEIPT_SCHEMA, CandidateEffectExecutionError,
    CandidateEffectExecutionHandler, CandidateEffectExecutionProbe,
)


@dataclass(frozen=True)
class _Result:
    candidate_id: str


class _Creator:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, str]] = {}
        self.calls = 0

    def execute_from_content_read(self, *, source_id: str, project_id: str, content_read_id: str,
                                  target_layer: str, candidate_type: str, created_at: str,
                                  execution_ref: str) -> _Result:
        self.calls += 1
        candidate_id = f"candidate-{source_id}-{content_read_id}"
        self.records.setdefault(candidate_id, {"id": candidate_id, "execution_ref": execution_ref})
        return _Result(candidate_id)

    def read_candidate(self, candidate_id: str):
        return self.records.get(candidate_id)


def _revisions() -> dict[str, str]:
    values = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
    values.update(policy="candidate-policy-v2", handler="candidate-handler-v2", budget="candidate-budget-v2")
    return values


def _gate() -> GateDecisionFact:
    return GateDecisionFact(GateDecision.ALLOW, "rule:candidate", "scope:candidate", {}, "scope:secret-candidate", "candidate-policy-v2")


def _intent() -> EffectIntent:
    admission_ref = "facts:candidate-job-admission/job-candidate-1/read-1"
    return EffectIntent(
        session_id="candidate-session", root_id="job-candidate-1", step_key="execution", kind=EFFECT_KIND,
        effect_class=EffectClass.QUERYABLE, intent_ref="intent:candidate-1", gate_decision_id="gate:candidate-1",
        rev_set=_revisions(), payload={
            "job_ref": admission_ref,
            "admission_ref": admission_ref,
            "parent_job_ref": "facts:candidate-job/parents/job-parent-1",
            "source_ref": "crp://candidate-job/sources/source-1",
            "evidence_ref": "crp://candidate-job/source-content-reads/read-1",
            "mode": "admit",
            "attempt_index": 0,
        },
        contract_version=EFFECT_V2, intent_schema_version=INTENT_SCHEMA,
        expected_receipt_kind=RECEIPT_KIND, expected_receipt_schema_version=RECEIPT_SCHEMA,
    )


def _job() -> dict[str, object]:
    return {
        "id": "job-candidate-1",
        "job_type": "extract_memory_candidate",
        "execution_version": EFFECT_V2,
        "parent_job_id": "job-parent-1",
        "source_id": "source-1",
        "project_id": "project-1",
        "evidence_kind": "source_content_read",
        "evidence_id": "read-1",
        "created_at": "2026-08-30T00:00:00+00:00",
        "steps": [{"name": "create_candidate"}],
    }


def _effect(tmp_path):
    log = EffectLog(tmp_path / "jobs.sqlite")
    effect, _ = log.plan_v2(_intent(), gate_decision_id="gate:candidate-1", gate_fact=_gate(), now=100)
    with sqlite3.connect(log.database) as connection:
        connection.row_factory = sqlite3.Row
        initialize_job_projection_schema(connection)
        builder = JobProjectionBuilder()
        builder.register_node_in_connection(connection, job_id="job-candidate-1", node_kind="attempt", node_key="candidate", attempt=0, effect_operation_id=effect.operation_id)
        builder.append_fact_in_connection(connection, job_id="job-candidate-1", effect_operation_id=effect.operation_id, payload=_job(), recorded_at="2026-08-30T00:00:00+00:00")
        builder.rebuild_in_connection(connection, job_id="job-candidate-1", rebuilt_at="2026-08-30T00:00:00+00:00")
    return log, effect


def test_receipt_before_settle_crash_recovers_without_job_lifecycle(tmp_path) -> None:
    log, effect = _effect(tmp_path)
    creator = _Creator()
    receipt = CandidateEffectExecutionHandler(log.database, creator).handle(effect)
    # Simulates a process death after the domain receipt transaction and before
    # Core binds it as SETTLED_OK.
    assert log.get(effect.operation_id).state is EffectState.PLANNED
    assert CandidateEffectExecutionProbe(log.database, creator).probe(effect) == (EffectState.SETTLED_OK, receipt.receipt_ref)


def test_replay_reuses_receipt_and_candidate_without_second_domain_write(tmp_path) -> None:
    log, effect = _effect(tmp_path)
    creator = _Creator()
    handler = CandidateEffectExecutionHandler(log.database, creator)
    assert handler.handle(effect).receipt_kind == RECEIPT_KIND
    assert handler.handle(effect).receipt_schema_version == RECEIPT_SCHEMA
    assert creator.calls == 1
    with sqlite3.connect(log.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidate_effect_domain_receipt").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact WHERE job_id='job-candidate-1'").fetchone()[0] == 2


def test_probe_returns_unknown_on_candidate_binding_drift(tmp_path) -> None:
    log, effect = _effect(tmp_path)
    creator = _Creator()
    CandidateEffectExecutionHandler(log.database, creator).handle(effect)
    creator.records["candidate-source-1-read-1"]["execution_ref"] = "facts:effect/eff2_other"
    assert CandidateEffectExecutionProbe(log.database, creator).probe(effect) == (EffectState.UNKNOWN, "error:candidate-effect-evidence-drift")


def test_production_creator_without_execution_seam_fails_red(tmp_path) -> None:
    log, effect = _effect(tmp_path)
    class LegacyCreator:
        def execute_from_content_read(self, **_kwargs):
            return _Result("candidate-1")
    with pytest.raises(CandidateEffectExecutionError, match="execution_ref seam"):
        CandidateEffectExecutionHandler(log.database, LegacyCreator()).handle(effect)


def test_module_never_imports_job_lifecycle_or_worker() -> None:
    import ast
    from pathlib import Path
    source = Path(__file__).parents[2] / "src" / "core" / "product_core" / "candidate_effect_execution.py"
    names = {node.names[0].name for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))) if isinstance(node, ast.ImportFrom) and node.names}
    assert not any("sqlite_worker" in name or "sqlite_lifecycle" in name for name in names)
