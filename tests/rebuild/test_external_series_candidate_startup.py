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
from backend.api.external_series_candidate_saga import ExternalSeriesCandidateSagaService
from backend.api.external_series_candidate_startup import (
    backfill_external_series_candidate_effects,
    dispatch_external_series_candidate_effects,
)
from core.effect_log import EffectLog, EffectReaper, EffectRunner, EffectState
from core.aggregate_repository_factory import AUTHORITY_DATABASE_NAME, STRUCTURED_DATABASE_NAME, TARGET_IDENTITY
from core.memory_core import shared_trust_audit_activation_id, shared_trust_audit_activation_payload
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteExternalSeriesCandidateSagaStore,
    SQLiteStructuredRecordStore,
)


ROOT = Path(__file__).resolve().parents[2]


def _store(root: Path) -> JsonObjectStore:
    return JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")


def _operations(root: Path) -> SQLiteExternalSeriesCandidateSagaStore:
    return SQLiteExternalSeriesCandidateSagaStore(
        SQLiteStructuredRecordStore(root / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    )


def recover_external_series_candidate_sagas(application, root, *, max_operations=100):
    effects = EffectLog(root / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    backfill_external_series_candidate_effects(root, effects, max_operations=max_operations)
    EffectReaper(effects).recover_expired(now=2**31)
    return dispatch_external_series_candidate_effects(
        application, root, EffectRunner(effects, owner_id="test-series-candidate"),
        max_operations=max_operations,
    )


def _activate_compound_sqlite(root: Path) -> SQLiteStructuredRecordStore:
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    authority = SQLiteAggregateAuthorityStore(root / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    members = ("memory_atoms", "memory_publications", "memory_scenarios", "memory_series_memory", "memory_transitions", "project_skills")
    evidence = AggregateAuthorityEvidence("sqlite-series-process-v1", "a" * 64, "b" * 64, TARGET_IDENTITY)
    with records.begin() as transaction:
        for member in members:
            transaction.put("aggregate_authority_targets", f"default~{member}", {"namespace_id": "default", "aggregate": member, "migration_id": evidence.migration_id, "source_fingerprint": evidence.source_fingerprint, "target_fingerprint": evidence.target_fingerprint, "target_identity": evidence.target_identity}, expected_revision=0)
        transaction.put("aggregate_authority_compound_activations", shared_trust_audit_activation_id("default"), shared_trust_audit_activation_payload(namespace_id="default", target_identity=TARGET_IDENTITY, activation_id="sqlite-series-process-v1", member_migrations={member: evidence.migration_id for member in members}, source_fingerprint=evidence.source_fingerprint, target_fingerprint=evidence.target_fingerprint, activated_at="2026-07-12T22:00:00+08:00"), expected_revision=0)
        transaction.commit()
    for member in members:
        initial = authority.create_json_active(namespace_id="default", aggregate=member, reason="test")
        staged = authority.transition(namespace_id="default", aggregate=member, expected_revision=initial.revision, to_state="sqlite_staged", evidence=evidence, reason="test")
        authority.transition(namespace_id="default", aggregate=member, expected_revision=staged.revision, to_state="sqlite_active", evidence=evidence, reason="test")
    return records


def _seed(root: Path, draft_id: str) -> tuple[JsonObjectStore, dict[str, object]]:
    store = _store(root)
    current = {
        "id": f"series-memory-{draft_id}",
        "series_id": f"series-{draft_id}",
        "overview": "before",
        "revision": 1,
        "source_refs": [{"source_id": "source-alpha", "locator": "char:0-20"}],
        "trust_status": "user_confirmed",
    }
    proposed = {**current, "overview": "after", "revision": 2}
    store.write("memory_series_memory", str(current["id"]), current, expected_revision=0)
    store.write(
        "external_agent_review_drafts",
        draft_id,
        {
            "id": draft_id,
            "draft_type": "series_update",
            "status": "pending_review",
            "project_id": "project-alpha",
            "target_id": current["series_id"],
            "suggested_changes": {"structured": proposed},
            "source_refs": [{"source_id": "source-alpha", "locator": "char:0-20"}],
            "evidence_refs": [{"source_id": "source-alpha", "locator": "char:0-20"}],
            "review": {"state": "pending_review"},
            "application": {"state": "not_applied"},
        },
        expected_revision=0,
    )
    return store, proposed


class _FailAfterCandidateWrite:
    def __init__(self, delegate: SQLiteExternalSeriesCandidateSagaStore) -> None:
        self._delegate = delegate

    def prepare(self, **kwargs):
        return self._delegate.prepare(**kwargs)

    def mark_candidate_created(self, *args, **kwargs):
        raise OSError("injected interruption after candidate write")

    def finalize(self, *args, **kwargs):
        return self._delegate.finalize(*args, **kwargs)


def test_prepared_candidate_operation_recovers_without_current_series_write(tmp_path: Path) -> None:
    store, proposed = _seed(tmp_path, "draft-series-startup")
    operations = _operations(tmp_path)
    with pytest.raises(OSError):
        ExternalSeriesCandidateSagaService(
            objects=store,
            operations=_FailAfterCandidateWrite(operations),
        ).apply("draft-series-startup", expected_object_revision=1)

    first = recover_external_series_candidate_sagas(FastAPI(), tmp_path)
    second = recover_external_series_candidate_sagas(FastAPI(), tmp_path)

    assert (first.recovered, first.failed, second.attempted) == (1, 0, 0)
    assert operations.get("draft-series-startup").state == "finalized"
    assert len(store.list("memory_candidates")) == 1
    assert store.revision("memory_series_memory", str(proposed["id"])) == 1
    assert store.list("staging_series_memory") == ()


def test_independent_process_candidate_write_recovers_on_new_sidecar_startup(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import os, sys
        from pathlib import Path
        from backend.api.external_series_candidate_saga import ExternalSeriesCandidateSagaService
        from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
        from core.storage_provider import JsonObjectStore, SQLiteExternalSeriesCandidateSagaStore, SQLiteStructuredRecordStore
        root=Path(sys.argv[1]); store=JsonObjectStore(root/'.rebuild-data',legacy_root=root/'library')
        draft_id='draft-series-process-candidate'; current={'id':'series-memory-process-candidate','series_id':'series-process-candidate','overview':'before','revision':1,'source_refs':[{'source_id':'source-alpha','locator':'char:0-20'}],'trust_status':'user_confirmed'}; proposed={**current,'overview':'after','revision':2}
        store.write('memory_series_memory',current['id'],current,expected_revision=0)
        store.write('external_agent_review_drafts',draft_id,{'id':draft_id,'draft_type':'series_update','status':'pending_review','project_id':'project-alpha','target_id':'series-process-candidate','suggested_changes':{'structured':proposed},'source_refs':[{'source_id':'source-alpha','locator':'char:0-20'}],'evidence_refs':[{'source_id':'source-alpha','locator':'char:0-20'}],'review':{'state':'pending_review'},'application':{'state':'not_applied'}},expected_revision=0)
        delegate=SQLiteExternalSeriesCandidateSagaStore(SQLiteStructuredRecordStore(root/'.rebuild-data'/STRUCTURED_DATABASE_NAME))
        class Exit:
            def prepare(self,**kwargs): return delegate.prepare(**kwargs)
            def mark_candidate_created(self,*args,**kwargs): os._exit(76)
            def finalize(self,*args,**kwargs): return delegate.finalize(*args,**kwargs)
        ExternalSeriesCandidateSagaService(objects=store,operations=Exit()).apply(draft_id,expected_object_revision=1)
        """
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == 76, (crashed.stdout, crashed.stderr)
    store = _store(tmp_path)
    assert len(store.list("memory_candidates")) == 1
    assert store.revision("memory_series_memory", "series-memory-process-candidate") == 1
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        assert "external_series_candidate" in client.app.state.effect_runtime.handlers.kinds()
        effect = client.app.state.effect_runtime.log.get("draft-series-process-candidate")
    assert effect.state is EffectState.SETTLED_OK
    assert _operations(tmp_path).get("draft-series-process-candidate").state == "finalized"


def test_sqlite_authority_process_kill_recovers_on_new_sidecar_startup(tmp_path: Path) -> None:
    store, _proposed = _seed(tmp_path, "draft-series-sqlite-process")
    records = _activate_compound_sqlite(tmp_path)
    current = store.read("memory_series_memory", "series-memory-draft-series-sqlite-process")
    assert current is not None
    with records.begin() as transaction:
        transaction.put("memory_series_memory", str(current["id"]), current, expected_revision=0)
        transaction.commit()
    script = """
import os,sys
from pathlib import Path
from backend.api.external_series_candidate_saga import ExternalSeriesCandidateSagaService,SQLiteSeriesCurrentProjection
from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.storage_provider import JsonObjectStore,SQLiteExternalSeriesCandidateSagaStore,SQLiteStructuredRecordStore
root=Path(sys.argv[1]); store=JsonObjectStore(root/'.rebuild-data',legacy_root=root/'library'); records=SQLiteStructuredRecordStore(root/'.rebuild-data'/STRUCTURED_DATABASE_NAME); delegate=SQLiteExternalSeriesCandidateSagaStore(records)
class Exit:
 def prepare(self,**kwargs): return delegate.prepare(**kwargs)
 def mark_candidate_created(self,*args,**kwargs): os._exit(77)
 def finalize(self,*args,**kwargs): return delegate.finalize(*args,**kwargs)
ExternalSeriesCandidateSagaService(objects=store,operations=Exit(),authority_identity='sqlite:structured-records-v1',current=SQLiteSeriesCurrentProjection(records)).apply('draft-series-sqlite-process',expected_object_revision=1)
"""
    environment = dict(os.environ); environment["PYTHONPATH"] = str(ROOT / "src")
    crashed = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30, check=False)
    assert crashed.returncode == 77, (crashed.stdout, crashed.stderr)
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        effect = client.app.state.effect_runtime.log.get("draft-series-sqlite-process")
    assert effect.state is EffectState.SETTLED_OK
    assert _operations(tmp_path).get("draft-series-sqlite-process").state == "finalized"
    assert len(store.list("memory_candidates")) == 1
