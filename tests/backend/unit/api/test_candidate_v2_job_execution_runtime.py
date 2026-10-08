from __future__ import annotations

import ast
from dataclasses import replace
import inspect
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from backend.api import job_execution_runtime
from core.effect_log import EFFECT_V2, EffectReceipt, EffectState, build_effect_runtime
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.job_runner import (
    SQLiteJobAdmissionCommand,
    SQLiteJobStore,
)
from core.product_core.candidate_effect_execution import CandidateEffectExecutionError, CandidateEffectExecutionHandler
from core.product_core import CandidateMemoryJobInput, ReadSourceTextContent, build_candidate_memory_job
from core.product_core.candidate_job_admission import CandidateJobAdmissionFactory
from core.storage_provider import JsonObjectStore


def _candidate_v2_registration():
    """Resolve the deliberate v2 production seam without importing legacy setup."""

    registration = getattr(
        job_execution_runtime, "register_candidate_v2_job_execution_handler", None,
    )
    assert callable(registration), (
        "extract_memory_candidate needs an isolated effect-v2 execution handler; "
        "it must not reuse the legacy Job lifecycle handler"
    )
    return registration


def _admitted_candidate(tmp_path: Path):
    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    source = ObjectStoreSourceRegistrar(objects).register(SourceSubmission(
        kind="text",
        title="Effect-v2 candidate source",
        content="Create one proposal for review; never publish memory directly.",
    ))
    read = ReadSourceTextContent(objects).execute(source_id=str(source["id"]))
    evidence_id = str(read.read_ref).removesuffix(".json").rsplit("/", 1)[-1]
    payload = build_candidate_memory_job(CandidateMemoryJobInput(
        parent_job_id="job-parent-v2",
        source_id=str(source["id"]),
        project_id="project-v2",
        evidence_kind="source_content_read",
        evidence_id=evidence_id,
    ), now="2026-08-30T00:00:00Z")
    payload["execution_version"] = EFFECT_V2
    database = tmp_path / ".rebuild-data" / "jobs.sqlite3"
    runtime = build_effect_runtime(database, owner_id="candidate-v2-test")
    source_record = objects.read("sources", str(source["id"]))
    read_record = objects.read("source_content_reads", evidence_id)
    admitted = CandidateJobAdmissionFactory(admitted_at=100).build(
        job_payload=payload,
        source_record=source_record,
        source_revision=objects.revision("sources", str(source["id"])),
        source_content_read_record=read_record,
        source_content_read_revision=objects.revision("source_content_reads", evidence_id),
    )
    admission = SQLiteJobAdmissionCommand(database).admit(
        payload=payload,
        authorization=admitted.authorization,
        intent=admitted.intent,
    )
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=runtime))
    return application, objects, runtime, admission.effect


def _register(tmp_path: Path, application, runtime) -> None:
    _candidate_v2_registration()(application, tmp_path, runtime)


def _restart_runtime(tmp_path: Path):
    runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3",
        owner_id="candidate-v2-restarted",
    )
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=runtime))
    _register(tmp_path, application, runtime)
    return runtime


def _assert_single_proposal_and_receipt(objects, runtime, operation_id: str) -> None:
    candidates = objects.list("memory_candidates")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["status"] == "pending_review"
    assert candidate["review"]["requires_user_confirmation"] is True
    assert candidate["review"]["auto_promote_allowed"] is False
    assert objects.list("memory_atoms") == ()
    candidate_events = [
        event for event in objects.list("activity_events")
        if event.get("type") == "memory_candidate_created"
    ]
    assert len(candidate_events) == 1
    with sqlite3.connect(runtime.log.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_effect_domain_receipt "
            "WHERE operation_id=?", (operation_id,),
        ).fetchone()[0] == 1


def test_v2_candidate_handler_creates_only_review_proposal_and_returns_strict_receipt(tmp_path: Path) -> None:
    application, objects, runtime, effect = _admitted_candidate(tmp_path)
    _register(tmp_path, application, runtime)

    registration = runtime.handlers.resolve(effect)
    assert registration.contract_version == EFFECT_V2
    assert registration.intent_schema_version == effect.intent_schema_version
    assert registration.receipt_kind == effect.expected_receipt_kind
    assert registration.receipt_schema_version == effect.expected_receipt_schema_version

    receipt = registration.handler(effect)

    assert isinstance(receipt, EffectReceipt)
    assert receipt.receipt_kind == "candidate-memory-job-execution.receipt"
    assert receipt.receipt_schema_version == "candidate-memory-job-execution-receipt-v2"
    assert receipt.intent_schema_version == "candidate-memory-job-execution-v2"
    candidates = objects.list("memory_candidates")
    assert len(candidates) == 1
    assert candidates[0]["status"] == "pending_review"
    assert candidates[0]["review"]["requires_user_confirmation"] is True
    assert candidates[0]["review"]["auto_promote_allowed"] is False
    assert objects.list("memory_atoms") == ()


def test_v2_candidate_probe_uses_immutable_receipt_evidence_for_planned_and_settled_states(tmp_path: Path) -> None:
    application, _objects, runtime, effect = _admitted_candidate(tmp_path)
    _register(tmp_path, application, runtime)
    probe = runtime.handlers.resolve(effect).probe
    assert probe is not None

    planned, retry_ref = probe(effect)
    assert planned is EffectState.PLANNED
    assert isinstance(retry_ref, str) and retry_ref.startswith("facts:candidate-effect-retry/")
    receipt = runtime.handlers.resolve(effect).handler(effect)

    assert probe(effect) == (EffectState.SETTLED_OK, receipt.receipt_ref)


def test_v2_candidate_admission_survives_restart_before_first_core_dispatch(tmp_path: Path) -> None:
    _application, objects, _runtime, effect = _admitted_candidate(tmp_path)

    restarted = _restart_runtime(tmp_path)
    settled = restarted.dispatch_operation(effect.operation_id, now=101)

    assert settled.state is EffectState.SETTLED_OK
    assert settled.result_ref == f"receipt:memory-candidate/{effect.operation_id}"
    _assert_single_proposal_and_receipt(objects, restarted, effect.operation_id)


def test_v2_candidate_domain_write_crash_replays_same_operation_once_then_writes_receipt(
    tmp_path: Path,
) -> None:
    application, objects, runtime, effect = _admitted_candidate(tmp_path)
    _register(tmp_path, application, runtime)
    creator = runtime.handlers.resolve(effect).handler.creator

    def crash_after_domain_write() -> None:
        raise OSError("simulated crash after candidate domain write")

    crashing = CandidateEffectExecutionHandler(
        runtime.log.database,
        creator,
        after_domain_write=crash_after_domain_write,
    )
    with pytest.raises(OSError, match="after candidate domain write"):
        crashing.handle(effect)

    assert len(objects.list("memory_candidates")) == 1
    assert len([
        event for event in objects.list("activity_events")
        if event.get("type") == "memory_candidate_created"
    ]) == 1
    with sqlite3.connect(runtime.log.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidate_effect_domain_receipt").fetchone()[0] == 0

    restarted = _restart_runtime(tmp_path)
    settled = restarted.dispatch_operation(effect.operation_id, now=101)

    assert settled.state is EffectState.SETTLED_OK
    _assert_single_proposal_and_receipt(objects, restarted, effect.operation_id)


def test_v2_candidate_receipt_before_effect_settle_recovers_after_restart_without_reexecution(
    tmp_path: Path,
) -> None:
    application, objects, runtime, effect = _admitted_candidate(tmp_path)
    _register(tmp_path, application, runtime)
    inflight, claimed = runtime.runner.claim_planned(effect.operation_id, now=101)
    assert claimed is True
    receipt = runtime.handlers.resolve(inflight).handler(inflight)
    assert runtime.log.get(effect.operation_id).state is EffectState.INFLIGHT

    restarted = _restart_runtime(tmp_path)
    outcomes = restarted.recover_expired(now=132)
    recovered = restarted.log.get(effect.operation_id)

    assert recovered.state is EffectState.SETTLED_OK
    assert recovered.result_ref == receipt.receipt_ref
    assert any(outcome.operation_id == effect.operation_id for outcome in outcomes)
    _assert_single_proposal_and_receipt(objects, restarted, effect.operation_id)


@pytest.mark.parametrize("collection", ("sources", "source_content_reads"))
def test_v2_candidate_real_source_or_read_revision_drift_fails_closed(
    tmp_path: Path, collection: str,
) -> None:
    application, objects, runtime, effect = _admitted_candidate(tmp_path)
    _register(tmp_path, application, runtime)
    job_id = effect.root_id
    with sqlite3.connect(runtime.log.database) as connection:
        job = connection.execute(
            "SELECT payload_json FROM job_effect_fact WHERE job_id=?", (job_id,),
        ).fetchone()[0]
    payload = json.loads(job)
    object_id = str(payload["source_id"] if collection == "sources" else payload["evidence_id"])
    record = dict(objects.read(collection, object_id))
    if collection == "sources":
        record["project_id"] = "other-project"
    else:
        record["text_sha256"] = "b" * 64
    objects.write(collection, object_id, record, expected_revision=objects.revision(collection, object_id))

    with pytest.raises(CandidateEffectExecutionError, match="evidence"):
        runtime.dispatch_operation(effect.operation_id, now=101)
    probe = runtime.handlers.resolve(effect).probe
    assert probe is not None
    assert probe(effect) == (EffectState.UNKNOWN, "error:candidate-effect-evidence-drift")
    assert objects.list("memory_candidates") == ()
    with sqlite3.connect(runtime.log.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidate_effect_domain_receipt").fetchone()[0] == 0


@pytest.mark.parametrize("evidence_case", ("missing_proposal", "contradictory_receipt"))
def test_v2_candidate_probe_fails_closed_for_incomplete_or_contradictory_evidence(
    tmp_path: Path, evidence_case: str,
) -> None:
    application, _objects, runtime, effect = _admitted_candidate(tmp_path)
    _register(tmp_path, application, runtime)
    probe = runtime.handlers.resolve(effect).probe
    assert probe is not None

    if evidence_case == "missing_proposal":
        checked = replace(effect, operation_id=f"{effect.operation_id}-unbound")
    else:
        checked = replace(
            effect,
            intent_ref="intent:candidate-job-execution/contradictory-evidence",
        )

    state, evidence_ref = probe(checked)
    assert state in {EffectState.UNKNOWN, EffectState.SETTLED_ERR}
    assert isinstance(evidence_ref, str) and evidence_ref


def test_v2_candidate_handler_does_not_enter_retired_job_execution_paths(
    tmp_path: Path,
) -> None:
    application, _objects, runtime, effect = _admitted_candidate(tmp_path)
    _register(tmp_path, application, runtime)
    settled = runtime.dispatch_operation(effect.operation_id, now=101)

    assert settled.state is EffectState.SETTLED_OK
    source = inspect.getsource(_candidate_v2_registration())
    names = {
        node.id for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name)
    }
    assert "SQLiteJobRuntimeLifecycle" not in names
    assert "SQLiteDeterministicJobWorker" not in names
