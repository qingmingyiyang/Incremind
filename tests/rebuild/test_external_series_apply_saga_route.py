from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.external_series_apply_saga import ExternalSeriesApplySagaService
from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.storage_provider import JsonObjectStore, SQLiteExternalSeriesApplySagaStore, SQLiteStructuredRecordStore


class _FailDraftFinalize:
    def __init__(self, delegate): self.delegate = delegate
    def read(self, collection, object_id): return self.delegate.read(collection, object_id)
    def revision(self, collection, object_id): return self.delegate.revision(collection, object_id)
    def write(self, collection, object_id, payload, expected_revision):
        if collection == "external_agent_review_drafts" and payload.get("status") == "applied":
            raise OSError("injected draft finalize failure")
        return self.delegate.write(collection, object_id, payload, expected_revision)


def test_http_route_does_not_recover_legacy_direct_series_operation(tmp_path: Path):
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    operations = SQLiteExternalSeriesApplySagaStore(
        SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    )
    draft_id = "draft-series-http-half-commit"
    current = {"id": "series-memory-http", "series_id": "series-http", "overview": "before", "revision": 1}
    proposed = dict(current)
    proposed.update({"overview": "after", "revision": 2})

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        store.write("memory_series_memory", current["id"], current, expected_revision=0)
        store.write("external_agent_review_drafts", draft_id, {
            "id": draft_id, "draft_type": "series_update", "status": "pending_review", "target_id": current["series_id"],
            "suggested_changes": {"structured": proposed}, "review": {"state": "pending_review"},
            "application": {"state": "blocked"},
        }, expected_revision=0)
        with pytest.raises(OSError, match="draft finalize failure"):
            ExternalSeriesApplySagaService(objects=_FailDraftFinalize(store), operations=operations).apply(
                draft_id, expected_object_revision=1
            )
        assert operations.get(draft_id).state == "series_applied"
        replay = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )

    assert replay.status_code == 400
    assert replay.json()["detail"] == "external agent review draft apply rejected"
    assert operations.get(draft_id).state == "series_applied"
    assert store.revision("memory_series_memory", current["id"]) == 2
    assert store.read("memory_series_memory", current["id"])["revision"] == 2
