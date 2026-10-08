from types import SimpleNamespace

from backend.api.workbench_content_transform_runtime import WorkbenchContentTransformDomain
from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


def test_transform_receipt_survives_human_review_revision(tmp_path):
    root = tmp_path / "runtime"
    store = JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library", namespace_id="default")
    store.write("sources", "source-one", {
        "id": "source-one", "project_id": "default", "original_url": "https://example.org/one",
    }, expected_revision=0)
    store.write("memory_candidates", "candidate-one", {
        "id": "candidate-one", "status": "pending_review",
        "review": {"requires_user_confirmation": True, "auto_promote_allowed": False},
    }, expected_revision=0)
    store.write("source_structures", "structure-one", {
        "id": "structure-one", "source_id": "source-one",
    }, expected_revision=0)
    documents = SQLiteDocumentRepository(SQLiteStructuredRecordStore(root / ".rebuild-data" / "structured-records.sqlite3"))
    document = documents.create(DocumentDraft(
        title="Source", document_type="web_page", markdown="Machine output",
        source_refs=({"source_id": "source-one", "locator": "source://source-one"},),
        project_id="default",
    ))
    domain = WorkbenchContentTransformDomain(root, store, "default", documents)
    item = {"pipeline": "web_read", "source_id": "source-one", "source_revision": 1,
            "original_asset_ref": "https://example.org/one"}
    output = {"execution_ref": "facts:effect/op-one", "document_id": document["id"],
              "document_revision": 1, "markdown_uri": document["markdown_uri"],
              "candidate_id": "candidate-one", "summary_ref": "source_structures/structure-one.json"}
    effect = SimpleNamespace(operation_id="op-one")

    domain.verify_output(effect, item, output)
    documents.save_user_edit(document["id"], markdown="Reviewed output", expected_revision=1)
    domain.verify_output(effect, item, output)
