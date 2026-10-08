from __future__ import annotations

import os
from pathlib import Path
import subprocess
import textwrap
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.memory_publication_review_staging_startup import (
    backfill_memory_review_staging_effects,
    dispatch_memory_review_staging_effects,
)
from core.effect_log import EffectLog, EffectReaper, EffectRunner
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.product_core import MemoryPublicationReviewStagingSagaService
from core.storage_provider import (
    JsonObjectStore,
    SQLiteMemoryPublicationReviewStagingSagaStore,
    SQLiteStructuredRecordStore,
)


ROOT = Path(__file__).resolve().parents[2]


def recover_memory_publication_review_staging_sagas(application, root, *, max_operations=100):
    effects = EffectLog(root / ".rebuild-data" / "structured-records.sqlite3")
    backfill_memory_review_staging_effects(root, effects, max_operations=max_operations)
    EffectReaper(effects).recover_expired(now=2**31)
    return dispatch_memory_review_staging_effects(
        application, root, EffectRunner(effects, owner_id="test-memory-review"),
        max_operations=max_operations,
    )


def _candidate(candidate_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": candidate_id,
        "project_id": "project-alpha",
        "target_layer": "atom",
        "candidate_type": "answer_summary",
        "status": "pending_review",
        "proposed_content": "恢复必须保留候选、草稿与来源。",
        "source_refs": [{"source_id": "source-alpha", "locator": "char:0-40"}],
        "provenance": {
            "model_result_id": None,
            "model_request_id": None,
            "recall_result_id": None,
            "document_id": "document-alpha",
            "document_revision": 1,
            "source_content_read_id": None,
            "media_processing_output_id": None,
            "media_processing_job_id": None,
            "input_refs": [
                {
                    "kind": "document",
                    "object_id": "document-alpha",
                    "uri": "crp://default/documents/document-alpha.json",
                }
            ],
        },
        "review": {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
            "reason": "等待审核。",
            "reviewed_by": None,
            "reviewed_at": None,
        },
        "created_at": "2026-07-12T20:00:00+08:00",
        "updated_at": "2026-07-12T20:00:00+08:00",
    }


def _prepared(root: Path, candidate_id: str = "candidate-startup"):
    candidates = JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "structured-records.sqlite3")
    operations = SQLiteMemoryPublicationReviewStagingSagaStore(records)
    ObjectStoreMemoryCandidateRepository(candidates).save(_candidate(candidate_id))
    service = MemoryPublicationReviewStagingSagaService(candidates, records, operations)
    return (
        candidates,
        operations,
        service.prepare_review(
            candidate_id,
            review_reason="用户确认可恢复草稿。",
            reviewed_at="2026-07-12T20:05:00+08:00",
        ),
    )


def test_prepared_operation_recovers_once_and_lifespan_records_report(tmp_path: Path) -> None:
    candidates, operations, operation = _prepared(tmp_path)

    first = recover_memory_publication_review_staging_sagas(FastAPI(), tmp_path)
    second = recover_memory_publication_review_staging_sagas(FastAPI(), tmp_path)

    assert (first.scanned, first.recovered, first.failed) == (1, 1, 0)
    assert second.attempted == 0
    assert operations.get(operation.operation_id).state == "finalized"
    assert candidates.revision("memory_candidates", "candidate-startup") == 2
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        assert "memory_review_staging" in client.app.state.effect_runtime.handlers.kinds()


def test_bad_operation_isolated_and_recovery_is_bounded(tmp_path: Path) -> None:
    candidates, operations, good = _prepared(tmp_path, "candidate-good")
    bad = _prepared(tmp_path, "candidate-bad")[2]
    drifted = candidates.read("memory_candidates", "candidate-bad")
    assert drifted is not None
    candidates.write(
        "memory_candidates",
        "candidate-bad",
        {**drifted, "proposed_content": "drift"},
        expected_revision=1,
    )

    report = recover_memory_publication_review_staging_sagas(FastAPI(), tmp_path, max_operations=2)

    assert (report.scanned, report.recovered, report.failed, report.deferred) == (2, 1, 1, 0)
    assert operations.get(good.operation_id).state == "finalized"
    assert operations.get(bad.operation_id).state == "prepared"
    assert {item.error_code for item in report.items if item.outcome == "failed"} == {"evidence_conflict"}


def test_candidate_reviewed_operation_only_finalizes_on_startup(tmp_path: Path) -> None:
    candidates, operations, prepared = _prepared(tmp_path, "candidate-reviewed")
    service = MemoryPublicationReviewStagingSagaService(candidates, operations.records, operations)
    service._stage(prepared)
    staged = operations.mark_sqlite_staging_created(
        prepared.operation_id,
        expected_revision=prepared.revision,
    )
    service._review_candidate(staged)
    reviewed = operations.mark_candidate_reviewed(
        staged.operation_id,
        expected_revision=staged.revision,
    )

    report = recover_memory_publication_review_staging_sagas(FastAPI(), tmp_path)

    assert reviewed.state == "candidate_reviewed"
    assert (report.recovered, report.failed) == (1, 0)
    assert operations.get(reviewed.operation_id).state == "finalized"
    assert candidates.revision("memory_candidates", "candidate-reviewed") == 2


def test_independent_process_exit_after_prepare_recovers_on_new_startup(tmp_path: Path) -> None:
    candidate = _candidate("candidate-process")
    script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        from core.memory_core import ObjectStoreMemoryCandidateRepository
        from core.product_core import MemoryPublicationReviewStagingSagaService
        from core.storage_provider import JsonObjectStore, SQLiteMemoryPublicationReviewStagingSagaStore, SQLiteStructuredRecordStore
        root = Path(r'{tmp_path}')
        store = JsonObjectStore(root / '.rebuild-data', legacy_root=root / 'library')
        records = SQLiteStructuredRecordStore(root / '.rebuild-data' / 'structured-records.sqlite3')
        ObjectStoreMemoryCandidateRepository(store).save({candidate!r})
        MemoryPublicationReviewStagingSagaService(store, records, SQLiteMemoryPublicationReviewStagingSagaStore(records)).prepare_review('candidate-process', review_reason='进程中断后恢复。', reviewed_at='2026-07-12T20:10:00+08:00')
        os._exit(83)
        """
    )
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    crashed = subprocess.run(
        [str(ROOT / "runtime" / "python.exe"), "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert crashed.returncode == 83, (crashed.stdout, crashed.stderr)
    candidates = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        assert "memory_review_staging" in client.app.state.effect_runtime.handlers.kinds()
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    assert SQLiteMemoryPublicationReviewStagingSagaStore(records).list_recoverable() == ()
    assert candidates.revision("memory_candidates", "candidate-process") == 2
