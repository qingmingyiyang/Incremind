from __future__ import annotations

import json
from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository
from core.product_core.workbench_auto_intake import (
    OrchestrateWorkbenchAutoIntake,
    WorkbenchAutoIntakeItem,
    reduce_workbench_auto_intake_job,
)
from core.storage_provider import JsonObjectStore
from tests.rebuild.test_contract_validation import validate_contract_instance


def _item(status: str, *, source_id: str = "source-job-truth") -> WorkbenchAutoIntakeItem:
    return WorkbenchAutoIntakeItem(
        source_id=source_id,
        source_uri=f"crp://default/sources/{source_id}",
        input_type="webpage",
        workflow="link_auto_organization",
        status=status,  # type: ignore[arg-type]
        needs_user_confirmation=status in {"needs_confirmation", "completed_pending_series"},
        title="Job truth",
        content_read_status="completed" if status == "completed" else "not_started",
        structure_status="completed" if status == "completed" else "not_started",
        series_status="confirmed" if status == "completed" else "not_started",
        series_name="",
        series_confidence=0.0,
        inspiration_status="not_started",
        auto_organization={},
        next_step="review_failure_and_retry" if status == "failed" else "await_user_confirmation",
    )


def _orchestrator(store: JsonObjectStore) -> OrchestrateWorkbenchAutoIntake:
    return OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=ObjectStoreSourceRegistrar(store),
        job_repository=ObjectStoreJobRepository(store),
        fetch_url=lambda _url: "",
    )


def test_reducer_marks_all_completed_items_as_completed() -> None:
    reduction = reduce_workbench_auto_intake_job([_item("completed")], label="webpage", now="2026-07-10T00:00:00+00:00")

    assert reduction["status"] == "completed"
    assert reduction["progress"] == {"current": 1, "total": 1, "percent": 100, "message": "orchestrated 1 workbench intake item(s)"}
    assert reduction["error"] is None
    assert reduction["max_attempts"] == 3


def test_reducer_preserves_partial_outputs_but_fails_when_any_item_failed() -> None:
    reduction = reduce_workbench_auto_intake_job(
        [_item("completed", source_id="source-ok"), _item("failed", source_id="source-failed")],
        label="bookmark_collection",
        now="2026-07-10T00:00:00+00:00",
    )

    assert reduction["status"] == "failed"
    assert reduction["progress"]["percent"] == 50  # type: ignore[index]
    assert reduction["error"]["code"] == "workbench_intake_failed"  # type: ignore[index]
    failed_step = reduction["steps"][1]  # type: ignore[index]
    assert failed_step["status"] == "failed"
    assert failed_step["error"]["retryable"] is True  # type: ignore[index]
    assert failed_step["retry_action"] == "review_failure_and_retry"


def test_reducer_does_not_fabricate_completion_for_confirmation_or_external_work() -> None:
    waiting = reduce_workbench_auto_intake_job([_item("needs_confirmation")], label="webpage", now="2026-07-10T00:00:00+00:00")
    pending = reduce_workbench_auto_intake_job([_item("needs_extractor")], label="file", now="2026-07-10T00:00:00+00:00")

    assert waiting["status"] == "waiting_user"
    assert waiting["progress"]["percent"] == 0  # type: ignore[index]
    assert pending["status"] == "pending"
    assert pending["progress"]["percent"] == 0  # type: ignore[index]


def test_persisted_auto_intake_job_satisfies_job_contract_when_failed(tmp_path: Path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    orchestrator = _orchestrator(store)
    orchestrator._write_intake_job("job-intake-truth", "source-parent", [_item("failed")], label="webpage")

    job = store.read("jobs", "job-intake-truth")
    assert job is not None
    schema = json.loads((Path("core-contracts/rebuild/job.schema.json")).read_text(encoding="utf-8"))
    assert validate_contract_instance("job.schema.json", schema, job) == []
    assert job["status"] == "failed"
    assert job["progress"]["percent"] == 0
    assert job["error"]["failed_step"] == "orchestrate_webpage_1"
