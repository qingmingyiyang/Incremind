from __future__ import annotations

from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository
from core.product_core.workbench_auto_intake import OrchestrateWorkbenchAutoIntake, WorkbenchAutoIntakeItem
from core.product_core.workbench_source_intake import CaptureWorkbenchTextSource
from core.storage_provider import JsonObjectStore


def _failed_item() -> WorkbenchAutoIntakeItem:
    return WorkbenchAutoIntakeItem(
        source_id="source-schema", source_uri="crp://default/sources/source-schema", input_type="webpage",
        workflow="link_auto_organization", status="failed", needs_user_confirmation=False, title="Schema",
        content_read_status="not_started", structure_status="not_started", series_status="not_started",
        series_name="", series_confidence=0.0, inspiration_status="not_started", auto_organization={},
        next_step="review_failure_and_retry",
    )


def test_capture_and_auto_intake_jobs_have_incompatible_runtime_shapes(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    repository = ObjectStoreJobRepository(store)
    sources = ObjectStoreSourceRegistrar(store)
    capture = CaptureWorkbenchTextSource(source_registrar=sources, job_repository=repository)
    captured = capture.execute(title="Schema", content="fixture content")
    capture_job = repository.get(captured.job_id)
    assert capture_job is not None

    intake = OrchestrateWorkbenchAutoIntake(object_store=store, source_registrar=sources, job_repository=repository, fetch_url=lambda _url: "")
    intake._write_intake_job("job-intake-schema", "source-schema", [_failed_item()], label="webpage")
    intake_job = repository.get("job-intake-schema")
    assert intake_job is not None

    assert capture_job["job_type"] == "capture"
    assert intake_job["job_type"] == "workbench_auto_intake"
    assert capture_job["status"] == "completed"
    assert intake_job["status"] == "failed"
    assert "input_refs" not in intake_job
    assert "progress" in intake_job and "progress_percent" not in intake_job
    assert capture_job["steps"][0]["name"] != intake_job["steps"][0]["name"]
