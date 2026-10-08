from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from fastapi.testclient import TestClient

from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository
from core.effect_log import EFFECT_V2, EffectLog, EffectReaper, EffectState
from core.product_core import ImportExternalAgentProposal
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def test_document_apply_half_commit_converges_on_new_sidecar_startup_without_replay(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(store)
    document = documents.create(
        DocumentDraft(
            title="External apply saga characterization",
            document_type="notes",
            markdown="Original content.",
            project_id="default",
            source_refs=({"source_id": "source-saga-001", "locator": "char:0-17"},),
        )
    )
    document_id = str(document["id"])
    proposal = {
        "proposal_id": "proposal-saga-half-commit",
        "proposal_type": "document_revision_proposal",
        "summary": "Characterize external apply half commit.",
        "source_refs": [{"source_id": "source-saga-001", "locator": "char:0-17"}],
        "evidence_refs": [{"locator": "document_versions.json#saga"}],
        "suggested_changes": {
            "document_id": document_id,
            "proposed_content": "Externally reviewed content.",
        },
        "requires_user_review": True,
    }
    imported = ImportExternalAgentProposal(store, namespace_id="default").execute(
        proposal=proposal,
        project_id="default",
    )
    draft_id = imported.draft_ids[0]
    original_write = JsonObjectStore.write

    def fail_draft_finalize(self, collection, object_id, payload, expected_revision):
        if collection == "external_agent_review_drafts" and object_id == draft_id:
            raise OSError("injected draft finalize failure")
        return original_write(self, collection, object_id, payload, expected_revision)

    with monkeypatch.context() as patch:
        patch.setattr(JsonObjectStore, "write", fail_draft_finalize)
        with TestClient(
            create_app(SimpleNamespace(root_dir=tmp_path)),
            raise_server_exceptions=False,
        ) as client:
            failed = client.post(
                f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
                json={"confirm": True, "expected_revision": 1},
            )

    assert failed.status_code == 500
    committed_document = documents.read(document_id)
    pending_draft = store.read("external_agent_review_drafts", draft_id)
    assert committed_document is not None
    assert committed_document["revision"] == 2
    assert documents.markdown(document_id) == "Externally reviewed content."
    assert pending_draft is not None
    assert pending_draft["status"] == "pending_review"
    assert pending_draft["application"]["state"] == "not_applied"

    # Recovery authority lives in Core Effect/Reaper.  The Saga identifier is
    # represented only by the immutable intent reference, never as effect id.
    log = EffectLog(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    intent_ref = f"crp://default/external-document-apply-intents/{draft_id}"
    with log._connect() as connection:
        rows = connection.execute(
            "SELECT operation_id, gate_decision_id FROM effect "
            "WHERE kind=? AND contract_version=? AND intent_ref=?",
            ("external_document_apply", EFFECT_V2, intent_ref),
        ).fetchall()
        assert len(rows) == 1
        effect_id, gate_decision_id = map(str, rows[0])
        assert connection.execute(
            "SELECT COUNT(*) FROM effect_gate_fact WHERE decision_id=?",
            (gate_decision_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM effect_intent_fact WHERE operation_id=?",
            (effect_id,),
        ).fetchone()[0] == 1
    inflight = log.get(effect_id)
    assert inflight.operation_id.startswith("eff2_")
    assert inflight.state is EffectState.INFLIGHT
    recovered = EffectReaper(log).recover_expired(now=2**31)
    assert recovered and recovered[0].operation_id == effect_id
    assert recovered[0].state is EffectState.PLANNED

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        settled = client.app.state.effect_runtime.log.get(effect_id)
        replay = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )

    assert settled.state is EffectState.SETTLED_OK
    assert settled.result_ref and "external-document-apply-receipts" in settled.result_ref
    assert replay.status_code == 409
    assert replay.json()["detail"] == "external agent review draft apply rejected"
    assert documents.read(document_id)["revision"] == 2
    assert len(documents.revisions(document_id)) == 2
    recovered_draft = store.read("external_agent_review_drafts", draft_id)
    assert recovered_draft["status"] == "applied"
    assert recovered_draft["application"]["operation_id"] == draft_id
