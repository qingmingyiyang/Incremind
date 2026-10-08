from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.external_series_apply_saga import ExternalSeriesApplySagaService
from backend.api.external_series_apply_startup import recover_external_series_apply_sagas
from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.storage_provider import (
    ExternalSeriesApplyEvidence,
    JsonObjectStore,
    SQLiteExternalSeriesApplySagaStore,
    SQLiteStructuredRecordStore,
)


ROOT = Path(__file__).resolve().parents[2]


def _store(root): return JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")
def _operations(root): return SQLiteExternalSeriesApplySagaStore(SQLiteStructuredRecordStore(root / ".rebuild-data" / STRUCTURED_DATABASE_NAME))


def _seed(root, draft_id):
    store = _store(root)
    current = {"id": f"series-memory-{draft_id}", "series_id": f"series-{draft_id}", "overview": "before", "revision": 1}
    proposed = dict(current)
    proposed.update({"overview": "after", "revision": 2})
    store.write("memory_series_memory", current["id"], current, expected_revision=0)
    store.write("external_agent_review_drafts", draft_id, {
        "id": draft_id, "draft_type": "series_update", "status": "pending_review", "target_id": current["series_id"],
        "suggested_changes": {"structured": proposed}, "review": {"state": "pending_review"},
        "application": {"state": "blocked"},
    }, expected_revision=0)
    return store, proposed


class _FailAfterSeriesWrite:
    def __init__(self, delegate): self.delegate = delegate
    def prepare(self, **kwargs): return self.delegate.prepare(**kwargs)
    def mark_series_applied(self, *args, **kwargs): raise OSError("injected interruption after series write")
    def finalize(self, *args, **kwargs): return self.delegate.finalize(*args, **kwargs)


class _FailDraftFinalize:
    def __init__(self, delegate): self.delegate = delegate
    def read(self, collection, object_id): return self.delegate.read(collection, object_id)
    def revision(self, collection, object_id): return self.delegate.revision(collection, object_id)
    def write(self, collection, object_id, payload, expected_revision):
        if collection == "external_agent_review_drafts" and payload.get("status") == "applied":
            raise OSError("injected draft finalize failure")
        return self.delegate.write(collection, object_id, payload, expected_revision)


def test_prepared_and_repeated_startup_recover_without_revision_growth(tmp_path: Path):
    store, proposed = _seed(tmp_path, "draft-series-prepared")
    operations = _operations(tmp_path)
    with pytest.raises(OSError):
        ExternalSeriesApplySagaService(objects=store, operations=_FailAfterSeriesWrite(operations)).apply(
            "draft-series-prepared", expected_object_revision=1
        )
    first = recover_external_series_apply_sagas(FastAPI(), tmp_path)
    second = recover_external_series_apply_sagas(FastAPI(), tmp_path)
    assert (first.recovered, first.failed, second.attempted) == (1, 0, 0)
    assert operations.get("draft-series-prepared").state == "finalized"
    assert store.revision("memory_series_memory", proposed["id"]) == 2
    assert store.read("memory_series_memory", proposed["id"])["revision"] == 2


def test_series_applied_finishes_draft_finalize_on_startup(tmp_path: Path):
    store, proposed = _seed(tmp_path, "draft-series-applied")
    operations = _operations(tmp_path)
    with pytest.raises(OSError):
        ExternalSeriesApplySagaService(objects=_FailDraftFinalize(store), operations=operations).apply(
            "draft-series-applied", expected_object_revision=1
        )
    assert operations.get("draft-series-applied").state == "series_applied"
    report = recover_external_series_apply_sagas(FastAPI(), tmp_path)
    assert report.recovered == 1
    assert operations.get("draft-series-applied").state == "finalized"
    assert store.revision("memory_series_memory", proposed["id"]) == 2


def test_bad_operation_isolated_and_batch_is_bounded(tmp_path: Path):
    operations = _operations(tmp_path)
    operations.prepare(operation_id="draft-a-missing", evidence=ExternalSeriesApplyEvidence(
        "default", "series-missing", "series-memory-missing", 0, 0, "a" * 64
    ))
    store, proposed = _seed(tmp_path, "draft-b-good")
    with pytest.raises(OSError):
        ExternalSeriesApplySagaService(objects=store, operations=_FailAfterSeriesWrite(operations)).apply(
            "draft-b-good", expected_object_revision=1
        )
    report = recover_external_series_apply_sagas(FastAPI(), tmp_path)
    assert (report.scanned, report.recovered, report.failed) == (2, 1, 1)
    assert report.items[0].error_code == "operation_invalid"
    for index in range(3):
        operations.prepare(operation_id=f"draft-z-{index}", evidence=ExternalSeriesApplyEvidence(
            "default", f"series-z-{index}", f"series-memory-z-{index}", 0, 0, f"{index + 1:064x}"
        ))
    bounded = recover_external_series_apply_sagas(FastAPI(), tmp_path, max_operations=2)
    assert (bounded.attempted, bounded.deferred) == (2, 2)
    assert store.revision("memory_series_memory", proposed["id"]) == 2


def test_payload_and_revision_drift_remain_prepared_and_diagnostic(tmp_path: Path):
    store, proposed = _seed(tmp_path, "draft-series-drift")
    operations = _operations(tmp_path)
    with pytest.raises(OSError):
        ExternalSeriesApplySagaService(objects=store, operations=_FailAfterSeriesWrite(operations)).apply(
            "draft-series-drift", expected_object_revision=1
        )
    drifted = dict(proposed)
    drifted.update({"overview": "unrelated revision", "revision": 3})
    store.write("memory_series_memory", proposed["id"], drifted, expected_revision=2)

    report = recover_external_series_apply_sagas(FastAPI(), tmp_path)

    assert report.failed == 1
    assert report.items[0].error_code == "evidence_conflict"
    assert operations.get("draft-series-drift").state == "prepared"
    assert store.revision("memory_series_memory", proposed["id"]) == 3


def test_existing_lifespan_quarantines_legacy_direct_series_operation(tmp_path: Path):
    store, proposed = _seed(tmp_path, "draft-series-lifespan")
    operations = _operations(tmp_path)
    with pytest.raises(OSError):
        ExternalSeriesApplySagaService(objects=store, operations=_FailAfterSeriesWrite(operations)).apply(
            "draft-series-lifespan", expected_object_revision=1
        )
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        report = client.app.state.external_series_apply_startup_recovery
    assert report.recovered == 0
    assert report.failed == 1
    assert report.items[0].error_code == "legacy_direct_series_apply_quarantined"
    assert operations.get("draft-series-lifespan").state == "prepared"
    assert store.revision("memory_series_memory", proposed["id"]) == 2


def test_independent_process_legacy_direct_write_is_quarantined_on_new_startup(tmp_path: Path):
    script = textwrap.dedent("""
        import os, sys
        from pathlib import Path
        from backend.api.external_series_apply_saga import ExternalSeriesApplySagaService
        from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
        from core.storage_provider import JsonObjectStore, SQLiteExternalSeriesApplySagaStore, SQLiteStructuredRecordStore
        root=Path(sys.argv[1]); store=JsonObjectStore(root/'.rebuild-data',legacy_root=root/'library')
        draft_id='draft-series-process'; current={'id':'series-memory-process','series_id':'series-process','overview':'before','revision':1}; proposed=dict(current); proposed.update({'overview':'after','revision':2})
        store.write('memory_series_memory',current['id'],current,expected_revision=0)
        store.write('external_agent_review_drafts',draft_id,{'id':draft_id,'draft_type':'series_update','status':'pending_review','target_id':'series-process','suggested_changes':{'structured':proposed},'review':{'state':'pending_review'},'application':{'state':'blocked'}},expected_revision=0)
        delegate=SQLiteExternalSeriesApplySagaStore(SQLiteStructuredRecordStore(root/'.rebuild-data'/STRUCTURED_DATABASE_NAME))
        class Exit:
            def prepare(self,**kwargs): return delegate.prepare(**kwargs)
            def mark_series_applied(self,*args,**kwargs): os._exit(75)
            def finalize(self,*args,**kwargs): return delegate.finalize(*args,**kwargs)
        ExternalSeriesApplySagaService(objects=store,operations=Exit()).apply(draft_id,expected_object_revision=1)
    """)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    crashed = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=30, check=False)
    assert crashed.returncode == 75, (crashed.stdout, crashed.stderr)
    store = _store(tmp_path)
    assert store.revision("memory_series_memory", "series-memory-process") == 2
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        report = client.app.state.external_series_apply_startup_recovery
        assert report.recovered == 0
        assert report.failed == 1
        assert report.items[0].error_code == "legacy_direct_series_apply_quarantined"
    assert store.revision("memory_series_memory", "series-memory-process") == 2
    assert store.read("memory_series_memory", "series-memory-process")["revision"] == 2
