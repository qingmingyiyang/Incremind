from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest

from core.effect_log import EffectHandlerAbandoned, EffectState
from core.effect_log.runtime import EffectExecutionCancelled
from core.job_runner import SQLiteJobAdmissionCommand, SQLiteJobStore
from core.product_core.workbench_content_transform_admission import (
    WorkbenchContentTransformAdmissionFactory,
)
from core.product_core.workbench_content_transform_execution import (
    WorkbenchContentTransformEffectHandler,
    WorkbenchContentTransformEffectProbe,
    read_workbench_transform_receipt,
)
from core.task_reference_contract import workbench_transform_task_reference, workbench_transform_task_tie_key


def _item() -> dict[str, object]:
    return {
        "source_id": "source-doc-1",
        "pipeline": "document_extract",
        "source_revision": 2,
        "authorization_id": "authorized-document-source-doc-1",
        "authorization_revision": 1,
        "original_asset_ref": "crp-ref-default-assets-originals-doc-1",
        "source_title": "Stage 2 document",
        "project_id": "default",
        "asr_provider": "not-applicable",
        "asr_binding_id": "not-applicable",
    }


def _job() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": "job-intake-source-doc-1",
        "source_id": "source-doc-1",
        "project_id": "default",
        "job_type": "workbench_content_transform",
        "idempotency_key": "workbench-auto-intake-source-doc-1",
        "status": "pending",
        "attempt": 0,
        "max_attempts": 2,
        "lease": None,
        "progress": {"current": 0, "total": 1, "percent": 0},
        "steps": [{"name": "document_extract", "status": "pending"}],
        "error": None,
        "checkpoint": None,
        "staged_outputs": [],
        "published_outputs": [],
        "log_refs": [],
        "transform_items": [_item()],
        "created_at": "2026-09-04T12:00:00+00:00",
        "updated_at": "2026-09-04T12:00:00+00:00",
        "execution_version": "effect-v2",
    }


def _output(effect) -> dict[str, object]:
    return {
        "source_id": "source-doc-1",
        "pipeline": "document_extract",
        "document_id": "document-doc-1",
        "document_revision": 1,
        "markdown_uri": "crp://default/documents/document-doc-1.md",
        "summary_ref": "crp://default/source-structures/structure-source-doc-1.json",
        "candidate_id": "candidate-doc-1",
        "candidate_status": "pending_review",
        "execution_ref": f"facts:effect/{effect.operation_id}",
    }


def test_transform_admission_is_atomic_and_job_starts_pending(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    job = _job()
    admission = WorkbenchContentTransformAdmissionFactory(admitted_at=100).build(job_payload=job)

    result = SQLiteJobAdmissionCommand(database).admit(
        payload=job,
        authorization=admission.authorization,
        intent=admission.intent,
    )

    assert result.created is True
    assert result.effect.kind == "workbench_content_transform"
    assert result.effect.contract_version == "effect-v2"
    assert result.record.payload["status"] == "pending"
    replay = SQLiteJobAdmissionCommand(database).admit(
        payload=job,
        authorization=admission.authorization,
        intent=admission.intent,
    )
    assert replay.created is False
    assert replay.effect.operation_id == result.effect.operation_id


def test_transform_candidate_page_uses_utc_timestamp_then_injected_reference(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    fixtures = (
        ("transform-alpha", "2026-09-06T08:00:00+08:00"),
        ("transform-beta", "2026-09-06T00:00:00Z"),
        ("transform-newest", "2026-09-06T00:00:00.000001+00:00"),
    )
    for index, (job_id, updated_at) in enumerate(fixtures):
        job = deepcopy(_job())
        job.update({"id": job_id, "idempotency_key": job_id, "updated_at": updated_at})
        admission = WorkbenchContentTransformAdmissionFactory(admitted_at=100 + index).build(job_payload=job)
        SQLiteJobAdmissionCommand(database).admit(
            payload=job, authorization=admission.authorization, intent=admission.intent,
        )

    store = SQLiteJobStore(database)
    reference = lambda job_id: workbench_transform_task_reference(project_id="project-a", job_id=job_id)
    first = store.list_effect_jobs_page(
        effect_kind="workbench_content_transform", contract_version="effect-v2",
        project_id="project-a", limit=2,
    )
    assert [job["id"] for job in first] == ["transform-newest", "transform-beta"]
    second = store.list_effect_jobs_page(
        effect_kind="workbench_content_transform", contract_version="effect-v2",
        project_id="project-a",
        after=("2026-09-06T00:00:00.000000+00:00", reference("transform-beta")), limit=2,
    )
    assert [job["id"] for job in second] == ["transform-alpha"]


def test_transform_candidate_index_pages_every_same_instant_without_scan_or_repeat(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    for index in range(500):
        job = deepcopy(_job())
        job_id = f"transform-tie-{index:04d}"
        job.update({
            "id": job_id, "idempotency_key": job_id,
            "updated_at": "2026-09-06T08:00:00+08:00" if index % 2 else "2026-09-06T00:00:00Z",
        })
        admission = WorkbenchContentTransformAdmissionFactory(admitted_at=100 + index).build(job_payload=job)
        SQLiteJobAdmissionCommand(database).admit(
            payload=job, authorization=admission.authorization, intent=admission.intent,
        )

    store = SQLiteJobStore(database)
    reference = lambda job_id: workbench_transform_task_reference(project_id="project-a", job_id=job_id)
    after = None
    seen: list[str] = []
    while True:
        page = store.list_effect_jobs_page(
            effect_kind="workbench_content_transform", contract_version="effect-v2",
            project_id="project-a", after=after, limit=32,
        )
        if not page:
            break
        seen.extend(str(job["id"]) for job in page)
        after = ("2026-09-06T00:00:00.000000+00:00", reference(str(page[-1]["id"])))

    expected = sorted((f"transform-tie-{index:04d}" for index in range(500)), key=reference, reverse=True)
    assert seen == expected
    with sqlite3.connect(database) as connection:
        plan = [str(row[3]) for row in connection.execute(
            """EXPLAIN QUERY PLAN SELECT job_id FROM job_task_candidate_projection
               WHERE effect_kind=? AND contract_version=?
               ORDER BY updated_at_key DESC,tie_key DESC LIMIT ?""",
            ("workbench_content_transform", "effect-v2", 32),
        )]
        continued_plan = [str(row[3]) for row in connection.execute(
            """EXPLAIN QUERY PLAN SELECT job_id FROM job_task_candidate_projection
               WHERE effect_kind=? AND contract_version=?
                 AND (updated_at_key,tie_key) < (?,?)
               ORDER BY updated_at_key DESC,tie_key DESC LIMIT ?""",
            (
                "workbench_content_transform", "effect-v2",
                "2026-09-06T00:00:00.000000+00:00",
                workbench_transform_task_tie_key(expected[249]), 32,
            ),
        )]
    assert any("ix_job_task_candidate_projection_page" in row for row in plan)
    assert not any("SCAN" in row or "TEMP B-TREE" in row for row in plan)
    assert any("ix_job_task_candidate_projection_page" in row for row in continued_plan)
    assert not any("SCAN" in row or "TEMP B-TREE" in row for row in continued_plan)


def test_candidate_index_backfill_is_explicit_read_falls_back_and_failure_rolls_back(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    for index in range(2):
        job = deepcopy(_job())
        job_id = f"transform-backfill-{index}"
        job.update({"id": job_id, "idempotency_key": job_id, "updated_at": f"2026-09-06T00:00:0{index}Z"})
        admission = WorkbenchContentTransformAdmissionFactory(admitted_at=100 + index).build(job_payload=job)
        SQLiteJobAdmissionCommand(database).admit(payload=job, authorization=admission.authorization, intent=admission.intent)

    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM job_task_candidate_projection")
        connection.execute("DELETE FROM job_task_candidate_projection_meta")
    store = SQLiteJobStore(database)
    reference = lambda job_id: workbench_transform_task_reference(project_id="project-a", job_id=job_id)
    fallback = store.list_effect_jobs_page(
        effect_kind="workbench_content_transform", contract_version="effect-v2",
        project_id="project-a", limit=32,
    )
    assert {job["id"] for job in fallback} == {"transform-backfill-0", "transform-backfill-1"}
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_task_candidate_projection").fetchone()[0] == 0

    store.rebuild_task_candidate_projection()
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            "SELECT job_id,fact_sequence FROM job_task_candidate_projection ORDER BY job_id"
        ).fetchall()
        assert connection.execute(
            "SELECT value FROM job_task_candidate_projection_meta WHERE key='coverage'"
        ).fetchone()[0] == "ready-v1"
        connection.execute("UPDATE job_effect_fact SET payload_json='{}' WHERE job_id='transform-backfill-1'")
    with pytest.raises(ValueError, match="stored Job fact payload"):
        store.rebuild_task_candidate_projection()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT job_id,fact_sequence FROM job_task_candidate_projection ORDER BY job_id"
        ).fetchall() == before


def test_transform_handler_receipt_drives_completed_job_and_document_output(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    job = _job()
    admission = WorkbenchContentTransformAdmissionFactory(admitted_at=100).build(job_payload=job)
    admitted = SQLiteJobAdmissionCommand(database).admit(
        payload=job,
        authorization=admission.authorization,
        intent=admission.intent,
    )
    verified: list[str] = []

    def execute(effect, item, checkpoint):
        checkpoint()
        assert item == _item()
        return _output(effect)

    def verify(effect, item, output):
        assert item == _item()
        assert output == _output(effect)
        verified.append(str(output["document_id"]))

    handler = WorkbenchContentTransformEffectHandler(
        database, execute, verify, lambda _effect: (lambda: None),
    )
    receipt = handler.handle(admitted.effect)

    assert receipt.receipt_kind == "workbench-content-transform.receipt"
    assert WorkbenchContentTransformEffectProbe(database, verify).probe(admitted.effect)[0] is EffectState.SETTLED_OK
    stored = read_workbench_transform_receipt(database, str(job["id"]))
    assert stored is not None
    assert stored["outputs"][0]["document_id"] == "document-doc-1"
    projection = SQLiteJobStore(database).read(str(job["id"]))
    assert projection is not None
    assert projection.payload["published_outputs"] == [{
        "kind": "document",
        "object_id": "document-doc-1",
        "uri": "crp://default/documents/document-doc-1.md",
        "status": "published",
    }]
    with sqlite3.connect(database) as connection:
        candidate = connection.execute(
            "SELECT fact_sequence,updated_at_key FROM job_task_candidate_projection WHERE job_id=?",
            (job["id"],),
        ).fetchone()
        latest = connection.execute(
            "SELECT json_extract(payload_json,'$.updated_at') FROM job_effect_fact "
            "WHERE job_id=? ORDER BY sequence DESC LIMIT 1", (job["id"],),
        ).fetchone()
    assert candidate is not None and latest is not None
    assert candidate[0] == 2
    assert candidate[1] == str(latest[0])
    assert verified


def test_transform_restarts_from_idempotent_outputs_when_receipt_write_was_interrupted(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    job = _job()
    admission = WorkbenchContentTransformAdmissionFactory(admitted_at=100).build(job_payload=job)
    effect = SQLiteJobAdmissionCommand(database).admit(
        payload=job,
        authorization=admission.authorization,
        intent=admission.intent,
    ).effect
    calls = 0

    def execute(current, _item_value, _checkpoint):
        nonlocal calls
        calls += 1
        return _output(current)

    def interrupt():
        raise RuntimeError("simulated process interruption")

    with pytest.raises(RuntimeError, match="simulated process interruption"):
        WorkbenchContentTransformEffectHandler(
            database, execute, lambda *_args: None, lambda _effect: (lambda: None),
            after_domain_write=interrupt,
        ).handle(effect)
    assert WorkbenchContentTransformEffectProbe(database, lambda *_args: None).probe(effect)[0] is EffectState.PLANNED

    WorkbenchContentTransformEffectHandler(
        database, execute, lambda *_args: None, lambda _effect: (lambda: None),
    ).handle(effect)
    assert calls == 2
    assert WorkbenchContentTransformEffectProbe(database, lambda *_args: None).probe(effect)[0] is EffectState.SETTLED_OK


def test_transform_cancellation_does_not_create_a_failure_fact(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    job = _job()
    admission = WorkbenchContentTransformAdmissionFactory(admitted_at=100).build(job_payload=job)
    effect = SQLiteJobAdmissionCommand(database).admit(
        payload=job,
        authorization=admission.authorization,
        intent=admission.intent,
    ).effect

    def cancelled_checkpoint():
        raise EffectExecutionCancelled("cancelled by Core fact")

    with pytest.raises(EffectHandlerAbandoned, match="user_cancelled"):
        WorkbenchContentTransformEffectHandler(
            database,
            lambda *_args: pytest.fail("domain execution must not start"),
            lambda *_args: None,
            lambda _effect: cancelled_checkpoint,
        ).handle(effect)

    assert WorkbenchContentTransformEffectProbe(
        database, lambda *_args: None,
    ).probe(effect)[0] is EffectState.PLANNED
