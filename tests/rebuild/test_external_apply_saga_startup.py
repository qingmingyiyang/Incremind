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
from backend.api.external_apply_saga import ExternalDocumentApplySagaService
from backend.api.external_apply_startup import (
    backfill_external_apply_effects,
    dispatch_external_apply_effects,
)
from core.effect_log import EFFECT_V2, EffectLog, EffectReaper, EffectRunner, EffectState
from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    JSON_DOCUMENT_AUTHORITY_IDENTITY,
    STRUCTURED_DATABASE_NAME,
    TARGET_IDENTITY,
    AggregateRepositoryFactory,
)
from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository, SQLiteDocumentRepository
from core.storage_provider import (
    AggregateAuthorityEvidence,
    ExternalApplyEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteExternalApplySagaStore,
    SQLiteStructuredRecordStore,
)


class _FailDocumentAppliedTransition:
    def __init__(self, delegate: SQLiteExternalApplySagaStore) -> None:
        self._delegate = delegate

    def prepare(self, **kwargs):
        return self._delegate.prepare(**kwargs)

    def mark_document_applied(self, *args, **kwargs):
        raise OSError("injected process interruption after document commit")

    def finalize(self, *args, **kwargs):
        return self._delegate.finalize(*args, **kwargs)


def _json_store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _records(tmp_path: Path) -> SQLiteStructuredRecordStore:
    return SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)


def _operations(tmp_path: Path) -> SQLiteExternalApplySagaStore:
    return SQLiteExternalApplySagaStore(_records(tmp_path))


def recover_external_apply_sagas(application, tmp_path, *, max_operations=100):
    effects = EffectLog(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    backfill_external_apply_effects(
        tmp_path, effects, max_operations=max_operations,
    )
    EffectReaper(effects).recover_expired(now=2**31)
    return dispatch_external_apply_effects(
        application, tmp_path, EffectRunner(effects, owner_id="test-external-apply"),
        max_operations=max_operations,
    )


def _v2_effect_for_saga(log: EffectLog, operation_id: str):
    intent_ref = f"crp://default/external-document-apply-intents/{operation_id}"
    with log._connect() as connection:
        rows = connection.execute(
            "SELECT operation_id FROM effect WHERE kind=? AND contract_version=? AND intent_ref=?",
            ("external_document_apply", EFFECT_V2, intent_ref),
        ).fetchall()
        assert len(rows) == 1
        effect_id = str(rows[0][0])
        assert connection.execute(
            "SELECT COUNT(*) FROM effect_gate_fact WHERE decision_id=("
            "SELECT gate_decision_id FROM effect WHERE operation_id=?)",
            (effect_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM effect_intent_fact WHERE operation_id=?",
            (effect_id,),
        ).fetchone()[0] == 1
    effect = log.get(effect_id)
    assert effect.operation_id.startswith("eff2_")
    assert effect.contract_version == EFFECT_V2
    return effect


def _seed_draft(
    drafts: JsonObjectStore,
    documents,
    *,
    draft_id: str,
    title: str,
) -> str:
    document = documents.create(
        DocumentDraft(
            title=title,
            document_type="notes",
            markdown=f"Original content for {draft_id}.",
            source_refs=({"source_id": f"source-{draft_id}", "locator": "char:0-20"},),
        )
    )
    document_id = str(document["id"])
    drafts.write(
        "external_agent_review_drafts",
        draft_id,
        {
            "schema_version": "1.0.0",
            "id": draft_id,
            "draft_type": "document_revision",
            "status": "pending_review",
            "project_id": "default",
            "target_id": document_id,
            "proposed_content": f"Recovered content for {draft_id}.",
            "source_refs": [{"source_id": f"source-{draft_id}", "locator": "char:0-20"}],
            "review": {"state": "pending_review", "requires_user_confirmation": True},
            "application": {"state": "not_applied"},
        },
        expected_revision=0,
    )
    return document_id


def _interrupt_after_document_commit(
    tmp_path: Path,
    *,
    draft_id: str,
    documents=None,
    authority_identity: str = JSON_DOCUMENT_AUTHORITY_IDENTITY,
) -> tuple[JsonObjectStore, object, SQLiteExternalApplySagaStore, str]:
    drafts = _json_store(tmp_path)
    repository = documents or ObjectStoreDocumentRepository(drafts)
    document_id = _seed_draft(drafts, repository, draft_id=draft_id, title=f"Target {draft_id}")
    operations = _operations(tmp_path)
    with pytest.raises(OSError, match="process interruption"):
        ExternalDocumentApplySagaService(
            documents=repository,
            drafts=drafts,
            operations=_FailDocumentAppliedTransition(operations),
            document_authority_identity=authority_identity,
        ).apply(draft_id, expected_revision=1)
    return drafts, repository, operations, document_id


def _activate_documents(records: SQLiteStructuredRecordStore, authority: SQLiteAggregateAuthorityStore) -> None:
    evidence = AggregateAuthorityEvidence(
        migration_id="external-startup-documents-v1",
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
        target_identity=TARGET_IDENTITY,
    )
    with records.begin() as uow:
        uow.put(
            "aggregate_authority_targets",
            "default~documents",
            {
                "namespace_id": "default",
                "aggregate": "documents",
                "migration_id": evidence.migration_id,
                "source_fingerprint": evidence.source_fingerprint,
                "target_fingerprint": evidence.target_fingerprint,
                "target_identity": evidence.target_identity,
            },
            expected_revision=0,
        )
        uow.commit()
    initial = authority.create_json_active(namespace_id="default", aggregate="documents", reason="initial")
    staged = authority.transition(
        namespace_id="default",
        aggregate="documents",
        expected_revision=initial.revision,
        to_state="sqlite_staged",
        evidence=evidence,
        reason="staged",
    )
    authority.transition(
        namespace_id="default",
        aggregate="documents",
        expected_revision=staged.revision,
        to_state="sqlite_active",
        evidence=evidence,
        reason="active",
    )


def test_prepared_operation_recovers_once_on_startup(tmp_path: Path) -> None:
    drafts, documents, operations, document_id = _interrupt_after_document_commit(
        tmp_path,
        draft_id="draft-startup-prepared",
    )
    assert operations.get("draft-startup-prepared").state == "prepared"

    application = FastAPI()
    first = recover_external_apply_sagas(application, tmp_path)
    second = recover_external_apply_sagas(application, tmp_path)

    assert (first.scanned, first.recovered, first.failed) == (1, 1, 0)
    assert (second.scanned, second.attempted) == (0, 0)
    assert operations.get("draft-startup-prepared").state == "finalized"
    assert documents.read(document_id)["revision"] == 2
    assert len(documents.revisions(document_id)) == 2
    assert drafts.read("external_agent_review_drafts", "draft-startup-prepared")["status"] == "applied"


def test_document_applied_operation_finishes_draft_finalize_on_startup(tmp_path: Path, monkeypatch) -> None:
    drafts = _json_store(tmp_path)
    documents = ObjectStoreDocumentRepository(drafts)
    draft_id = "draft-startup-document-applied"
    document_id = _seed_draft(drafts, documents, draft_id=draft_id, title="Document applied target")
    operations = _operations(tmp_path)
    original_write = JsonObjectStore.write

    def fail_finalize(self, collection, object_id, payload, expected_revision):
        if collection == "external_agent_review_drafts" and object_id == draft_id and payload.get("status") == "applied":
            raise OSError("injected draft finalize failure")
        return original_write(self, collection, object_id, payload, expected_revision)

    with monkeypatch.context() as patch:
        patch.setattr(JsonObjectStore, "write", fail_finalize)
        with pytest.raises(OSError, match="draft finalize failure"):
            ExternalDocumentApplySagaService(
                documents=documents,
                drafts=drafts,
                operations=operations,
            ).apply(draft_id, expected_revision=1)

    assert operations.get(draft_id).state == "document_applied"
    report = recover_external_apply_sagas(FastAPI(), tmp_path)

    assert report.recovered == 1
    assert operations.get(draft_id).state == "finalized"
    assert documents.read(document_id)["revision"] == 2
    assert len(documents.revisions(document_id)) == 2


def test_bad_operation_is_diagnostic_and_does_not_block_later_good_operation(tmp_path: Path) -> None:
    operations = _operations(tmp_path)
    operations.prepare(
        operation_id="draft-a-missing",
        evidence=ExternalApplyEvidence(
            namespace_id="default",
            document_id="document-missing",
            base_revision=1,
            payload_sha256="a" * 64,
            document_authority_identity=JSON_DOCUMENT_AUTHORITY_IDENTITY,
        ),
    )
    drafts, documents, _, document_id = _interrupt_after_document_commit(
        tmp_path,
        draft_id="draft-b-good",
    )

    report = recover_external_apply_sagas(FastAPI(), tmp_path)

    assert (report.scanned, report.attempted, report.recovered, report.failed) == (2, 2, 1, 1)
    assert [(item.operation_id, item.outcome, item.error_code) for item in report.items] == [
        ("draft-a-missing", "failed", "operation_invalid"),
        ("draft-b-good", "recovered", None),
    ]
    assert operations.get("draft-a-missing").state == "prepared"
    assert operations.get("draft-b-good").state == "finalized"
    assert documents.read(document_id)["revision"] == 2
    assert drafts.read("external_agent_review_drafts", "draft-b-good")["status"] == "applied"


def test_recovery_batch_limit_defers_without_looping(tmp_path: Path) -> None:
    operations = _operations(tmp_path)
    for index in range(3):
        operations.prepare(
            operation_id=f"draft-batch-{index}",
            evidence=ExternalApplyEvidence(
                namespace_id="default",
                document_id=f"document-batch-{index}",
                base_revision=1,
                payload_sha256=f"{index + 1:064x}",
                document_authority_identity=JSON_DOCUMENT_AUTHORITY_IDENTITY,
            ),
        )

    report = recover_external_apply_sagas(FastAPI(), tmp_path, max_operations=2)

    assert (report.scanned, report.attempted, report.failed, report.deferred) == (3, 2, 2, 1)
    assert len(report.items) == 2
    assert len(operations.list_recoverable()) == 3


@pytest.mark.parametrize(
    ("identity", "error_code"),
    [
        ("unbound:legacy", "authority_unbound"),
        (TARGET_IDENTITY, "authority_drift"),
    ],
)
def test_authority_evidence_drift_fails_closed(tmp_path: Path, identity: str, error_code: str) -> None:
    operations = _operations(tmp_path)
    operations.prepare(
        operation_id="draft-authority-drift",
        evidence=ExternalApplyEvidence(
            namespace_id="default",
            document_id="document-authority-drift",
            base_revision=1,
            payload_sha256="d" * 64,
            document_authority_identity=identity,
        ),
    )

    report = recover_external_apply_sagas(FastAPI(), tmp_path)

    assert report.failed == 1
    assert report.items[0].error_code == error_code
    assert operations.get("draft-authority-drift").state == "prepared"


def test_active_sqlite_startup_recovery_never_creates_json_document(tmp_path: Path) -> None:
    records = _records(tmp_path)
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    _activate_documents(records, authority)
    drafts = _json_store(tmp_path)
    resolution = AggregateRepositoryFactory(
        runtime_root=tmp_path,
        namespace_id="default",
        json_store=drafts,
    ).document_repository_resolution()
    assert resolution.authority_identity == TARGET_IDENTITY
    drafts, documents, operations, document_id = _interrupt_after_document_commit(
        tmp_path,
        draft_id="draft-startup-sqlite",
        documents=resolution.repository,
        authority_identity=resolution.authority_identity,
    )

    report = recover_external_apply_sagas(FastAPI(), tmp_path)

    assert report.recovered == 1
    assert operations.get("draft-startup-sqlite").state == "finalized"
    assert documents.read(document_id)["revision"] == 2
    assert len(documents.revisions(document_id)) == 2
    assert ObjectStoreDocumentRepository(drafts).read(document_id) is None


def test_payload_drift_remains_prepared_and_diagnostic(tmp_path: Path) -> None:
    drafts, documents, operations, document_id = _interrupt_after_document_commit(
        tmp_path,
        draft_id="draft-startup-payload-drift",
    )
    drifted = dict(drafts.read("external_agent_review_drafts", "draft-startup-payload-drift"))
    drifted["proposed_content"] = "Drifted content must not be recovered."
    drafts.write(
        "external_agent_review_drafts",
        "draft-startup-payload-drift",
        drifted,
        expected_revision=None,
    )

    report = recover_external_apply_sagas(FastAPI(), tmp_path)

    assert report.failed == 1
    assert report.items[0].error_code == "evidence_conflict"
    assert operations.get("draft-startup-payload-drift").state == "prepared"
    assert documents.read(document_id)["revision"] == 2
    assert len(documents.revisions(document_id)) == 2


def test_base_revision_drift_fails_closed_without_overwriting_user_revision(tmp_path: Path, monkeypatch) -> None:
    drafts = _json_store(tmp_path)
    documents = ObjectStoreDocumentRepository(drafts)
    draft_id = "draft-startup-base-drift"
    document_id = _seed_draft(drafts, documents, draft_id=draft_id, title="Base drift target")
    operations = _operations(tmp_path)

    with monkeypatch.context() as patch:
        patch.setattr(
            ObjectStoreDocumentRepository,
            "save_user_edit",
            lambda self, *args, **kwargs: (_ for _ in ()).throw(OSError("stop after prepare")),
        )
        with pytest.raises(OSError, match="stop after prepare"):
            ExternalDocumentApplySagaService(
                documents=documents,
                drafts=drafts,
                operations=operations,
            ).apply(draft_id, expected_revision=1)

    documents.save_user_edit(
        document_id,
        markdown="Independent user revision.",
        expected_revision=1,
    )
    report = recover_external_apply_sagas(FastAPI(), tmp_path)

    assert report.failed == 1
    assert report.items[0].error_code == "evidence_conflict"
    assert operations.get(draft_id).state == "prepared"
    assert documents.read(document_id)["revision"] == 2
    assert documents.markdown(document_id) == "Independent user revision."
    assert len(documents.revisions(document_id)) == 2


def test_create_app_recovers_external_apply_through_registered_core_handler(tmp_path: Path) -> None:
    _, documents, operations, document_id = _interrupt_after_document_commit(
        tmp_path,
        draft_id="draft-startup-lifespan",
    )

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        assert "external_document_apply" in client.app.state.effect_runtime.handlers.kinds()
        effect = _v2_effect_for_saga(
            client.app.state.effect_runtime.log, "draft-startup-lifespan",
        )

    assert effect.state is EffectState.SETTLED_OK
    assert operations.get("draft-startup-lifespan").state == "finalized"
    assert documents.read(document_id)["revision"] == 2
    assert len(documents.revisions(document_id)) == 2


def test_independent_process_exit_after_document_commit_recovers_on_new_sidecar_startup(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import os
        import sys
        from pathlib import Path

        from backend.api.external_apply_saga import ExternalDocumentApplySagaService
        from core.aggregate_repository_factory import JSON_DOCUMENT_AUTHORITY_IDENTITY, STRUCTURED_DATABASE_NAME
        from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository
        from core.storage_provider import JsonObjectStore, SQLiteExternalApplySagaStore, SQLiteStructuredRecordStore

        root = Path(sys.argv[1])
        drafts = JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")
        documents = ObjectStoreDocumentRepository(drafts)
        document = documents.create(DocumentDraft(
            title="Independent process crash target",
            document_type="notes",
            markdown="Before process termination.",
            source_refs=({"source_id": "source-process-crash", "locator": "char:0-26"},),
        ))
        draft_id = "draft-process-crash"
        drafts.write("external_agent_review_drafts", draft_id, {
            "schema_version": "1.0.0",
            "id": draft_id,
            "draft_type": "document_revision",
            "status": "pending_review",
            "project_id": "default",
            "target_id": document["id"],
            "proposed_content": "After independent process recovery.",
            "source_refs": [{"source_id": "source-process-crash", "locator": "char:0-26"}],
            "review": {"state": "pending_review", "requires_user_confirmation": True},
            "application": {"state": "not_applied"},
        }, expected_revision=0)
        delegate = SQLiteExternalApplySagaStore(
            SQLiteStructuredRecordStore(root / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
        )

        class ExitAfterDocumentCommit:
            def prepare(self, **kwargs):
                return delegate.prepare(**kwargs)
            def mark_document_applied(self, *args, **kwargs):
                os._exit(73)
            def finalize(self, *args, **kwargs):
                return delegate.finalize(*args, **kwargs)

        ExternalDocumentApplySagaService(
            documents=documents,
            drafts=drafts,
            operations=ExitAfterDocumentCommit(),
            document_authority_identity=JSON_DOCUMENT_AUTHORITY_IDENTITY,
        ).apply(draft_id, expected_revision=1)
        """
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert crashed.returncode == 73, (crashed.stdout, crashed.stderr)

    drafts = _json_store(tmp_path)
    pending = drafts.read("external_agent_review_drafts", "draft-process-crash")
    document_id = str(pending["target_id"])
    documents = ObjectStoreDocumentRepository(drafts)
    assert documents.read(document_id)["revision"] == 2
    assert _operations(tmp_path).get("draft-process-crash").state == "prepared"

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        effect = _v2_effect_for_saga(
            client.app.state.effect_runtime.log, "draft-process-crash",
        )

    assert effect.state is EffectState.SETTLED_OK
    assert _operations(tmp_path).get("draft-process-crash").state == "finalized"
    assert documents.read(document_id)["revision"] == 2
    assert len(documents.revisions(document_id)) == 2
    assert drafts.read("external_agent_review_drafts", "draft-process-crash")["status"] == "applied"
