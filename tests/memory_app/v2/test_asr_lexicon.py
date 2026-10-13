"""词表预览、原子追加与同用户旧入口兼容，不调用模型。"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.transaction_records import TransactionRecords
from backend.memory_app.v2.asr_lexicon import AsrLexiconError, AsrLexiconService, MAX_IMPORT_BYTES, install_asr_lexicon_routes
from core.product_core.realtime_asr_lexicon import RealtimeAsrLexicon
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


def service(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    return AsrLexiconService(records, runtime_root=tmp_path)


def test_preview_then_commit_appends_updates_and_preserves_unmentioned_terms(tmp_path):
    subject = service(tmp_path)
    subject.import_text(text="旧词\n保留词", expected_revision=0, preview=False)
    preview = subject.import_text(text="旧词\t5\n新增词", expected_revision=1)
    assert preview["preview"] and preview["revision"] == 1
    assert (preview["added_count"], preview["updated_count"], preview["unchanged_count"]) == (1, 1, 0)
    assert len(subject.read()["terms"]) == 2
    committed = subject.import_text(text="旧词\t5\n新增词", expected_revision=1, preview=False)
    assert committed["revision"] == 2
    assert {row["term"]: row["weight"] for row in subject.read()["terms"]} == {"旧词": 5, "保留词": 4, "新增词": 4}
    unchanged = subject.import_text(text="新增词", expected_revision=2, preview=False)
    assert unchanged["revision"] == 2 and unchanged["unchanged_count"] == 1


@pytest.mark.parametrize("format,text", [
    ("txt", "Alpha\t3\nalpha\t3"),
    ("csv", "term,weight,is_super\nAlpha,3,false\nalpha,3,false"),
    ("json", '[{"term":"Alpha","weight":3},{"term":"alpha","weight":3}]'),
])
def test_supported_formats_deduplicate_case_insensitively(tmp_path, format, text):
    result = service(tmp_path).import_text(text=text, format=format, expected_revision=0, preview=False)
    assert result["imported_count"] == 1
    assert result["terms"][0]["weight"] == 3


@pytest.mark.parametrize("format,text", [
    ("txt", "合法\n无效\t9"),
    ("txt", "alpha\t2\nALPHA\t4"),
    ("json", '[{"term":"合法"},{"term":"无效","weight":true}]'),
    ("json", '[{"term":"无效","is_super":"true"}]'),
    ("csv", "term,weight,unknown\n合法,4,hidden"),
    ("txt", "词" * 16),
])
def test_invalid_batch_never_partially_writes(tmp_path, format, text):
    subject = service(tmp_path)
    before = subject.read()
    with pytest.raises(AsrLexiconError, match="invalid_asr_lexicon_import"):
        subject.import_text(text=text, format=format, expected_revision=0, preview=False)
    assert subject.read() == before
    assert subject.records.list("realtime_asr_lexicon_state") == ()


def test_stale_revision_rejected_even_for_preview(tmp_path):
    subject = service(tmp_path)
    subject.import_text(text="已写入", expected_revision=0, preview=False)
    for preview in (True, False):
        with pytest.raises(AsrLexiconError, match="asr_lexicon_revision_conflict"):
            subject.import_text(text="不应写入", expected_revision=0, preview=preview)
    assert len(subject.read()["terms"]) == 1


def test_legacy_terms_and_pending_candidates_move_together_only_on_explicit_write(tmp_path):
    subject = service(tmp_path)
    legacy = RealtimeAsrLexicon(JsonObjectStore(tmp_path / ".rebuild-data"))
    original = legacy.manual_upsert("原热词", weight=3)
    candidate = legacy.propose_candidate("待确认词")
    assert subject.read()["revision"] == 2
    subject.import_text(text="新热词", expected_revision=2)
    assert subject.records.list("realtime_asr_lexicon_state") == ()
    subject.import_text(text="新热词", expected_revision=2, preview=False)
    assert subject.read()["revision"] == 3
    assert original.term_id in {term.term_id for term in subject.list_terms()}
    assert subject.list_candidates()[0].candidate_id == candidate.candidate_id
    assert legacy.current_revision() == 2
    subject.manual_upsert("实时入口新增", weight=2)
    assert {row["term"] for row in subject.read()["terms"]} == {"原热词", "新热词", "实时入口新增"}


def test_tool_enlisted_write_rolls_back_with_its_receipt_transaction(tmp_path):
    subject = service(tmp_path)
    with pytest.raises(RuntimeError, match="receipt failed"):
        with subject.records.begin() as tx:
            result = AsrLexiconService(TransactionRecords(tx), runtime_root=tmp_path).import_text(
                text="原子更新", expected_revision=0, preview=False)
            assert result["revision"] == 1
            raise RuntimeError("receipt failed")
    assert subject.read()["revision"] == 0
    assert subject.read()["terms"] == []


def test_user_records_are_isolated(tmp_path):
    left, right = service(tmp_path / "left"), service(tmp_path / "right")
    left.import_text(text="甲用户专属", expected_revision=0, preview=False)
    assert right.read()["terms"] == []
    assert right.read()["revision"] == 0


def test_super_capacity_is_whole_batch_failure(tmp_path):
    subject = service(tmp_path)
    text = json.dumps([{"term": f"super-{index}", "is_super": True} for index in range(51)])
    with pytest.raises(AsrLexiconError, match="asr_lexicon_capacity_exceeded"):
        subject.import_text(text=text, format="json", expected_revision=0, preview=False)
    assert subject.read()["terms"] == []


def test_api_has_safe_errors_and_marks_actual_provider_capability(tmp_path):
    subject = service(tmp_path)
    app = FastAPI()
    install_asr_lexicon_routes(app, records=subject.records, runtime_root=tmp_path)
    with TestClient(app) as client:
        current = client.get("/api/v2/asr/lexicon").json()
        assert current["providers"]["local-faster-whisper"]["hotwords_supported"]
        assert not current["providers"]["tokenhub-asr"]["hotwords_supported"]
        committed = client.post("/api/v2/asr/lexicon/import", json={"text": "术语", "expected_revision": 0, "preview": False})
        assert committed.status_code == 200 and committed.json()["revision"] == 1
        stale = client.post("/api/v2/asr/lexicon/import", json={"text": "术语", "expected_revision": 0, "preview": False})
        assert stale.status_code == 409
        assert stale.json() == {"detail": "asr_lexicon_revision_conflict"}
        oversized = client.post("/api/v2/asr/lexicon/import", json={"text": "a" * (MAX_IMPORT_BYTES + 1), "expected_revision": 1})
        assert oversized.status_code == 413
        for invalid_format in ([], {}, 1, None):
            malformed = client.post("/api/v2/asr/lexicon/import", json={"text": "术语", "expected_revision": 1, "format": invalid_format})
            assert malformed.status_code == 400 and malformed.json() == {"detail": "invalid_asr_lexicon_import"}


def test_actual_legacy_route_uses_same_sqlite_authority(tmp_path):
    from backend.api.routes.realtime_asr import _lexicon
    from backend.shared.asr_lexicon import user_asr_lexicon
    shared = user_asr_lexicon(tmp_path)
    shared.import_text(text="上传和实时共用", expected_revision=0, preview=False)
    realtime = _lexicon(tmp_path)
    assert realtime.select_for_session().terms[0].term == "上传和实时共用"
    realtime.manual_upsert("实时追加", weight=3)
    assert {row["term"] for row in shared.read()["terms"]} == {"上传和实时共用", "实时追加"}


def test_json_import_accepts_utf8_bom(tmp_path):
    result = service(tmp_path).import_text(text='\ufeff[{"term":"Alpha","weight":3}]', format="json", expected_revision=0, preview=False)
    assert result["imported_count"] == 1 and result["terms"][0]["weight"] == 3


def test_csv_import_rejects_duplicate_column_names_atomically(tmp_path):
    subject = service(tmp_path)
    with pytest.raises(AsrLexiconError, match="invalid_asr_lexicon_import"):
        subject.import_text(text="term,term\n被覆盖词,其他词", format="csv", expected_revision=0, preview=False)
    assert subject.read()["terms"] == []


def test_fresh_user_runtime_and_product_composition_share_one_record_authority(tmp_path):
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    from backend.shared.asr_lexicon import user_asr_lexicon
    runtime = user_asr_lexicon(tmp_path)
    runtime.import_text(text="启动前词表", expected_revision=0, preview=False)
    records, namespace = resolve_recognition_document_store(tmp_path)
    assert namespace == "default" and records.database_path == runtime.records.database_path
    product = AsrLexiconService(records, runtime_root=tmp_path)
    assert product.read()["revision"] == 1
    product.import_text(text="启动后词表", expected_revision=1, preview=False)
    assert user_asr_lexicon(tmp_path).read() == product.read()
    assert not (tmp_path / "recognition.sqlite3").exists()


def test_old_recognition_namespace_keeps_existing_record_authority(tmp_path):
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    from backend.shared.asr_lexicon import user_asr_lexicon
    previous = SQLiteStructuredRecordStore(tmp_path / "recognition.sqlite3")
    AsrLexiconService(previous, runtime_root=tmp_path).import_text(text="旧用户词表", expected_revision=0, preview=False)
    records, namespace = resolve_recognition_document_store(tmp_path)
    assert namespace == "recognition" and records.database_path == previous.database_path
    runtime = user_asr_lexicon(tmp_path)
    assert runtime.records.database_path == previous.database_path
    assert runtime.read()["terms"][0]["term"] == "旧用户词表"


def test_populated_legacy_documents_require_cutover_in_both_entry_points(tmp_path):
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    from backend.shared.asr_lexicon import user_asr_lexicon
    from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository
    legacy = ObjectStoreDocumentRepository(JsonObjectStore(tmp_path / ".rebuild-data"), namespace_id="default")
    legacy.create(DocumentDraft(title="正式旧文档", document_type="project_doc", markdown="旧内容",
                                source_refs=({"source_id": "source-a", "locator": "file:original", "quote": "旧内容"},), project_id="inbox"))
    for resolve in (resolve_recognition_document_store, user_asr_lexicon):
        with pytest.raises(RuntimeError, match="recognition_document_cutover_required_for_old_documents"):
            resolve(tmp_path)
    assert not (tmp_path / "recognition.sqlite3").exists()


def test_runtime_records_preserve_exact_records_evidence_gate(tmp_path):
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    from backend.shared.asr_lexicon import user_asr_lexicon
    from tests.rebuild.test_aggregate_repository_factory import _authority, _marker, _target_records
    records = _target_records(tmp_path)
    _marker(records, "documents")
    _authority(tmp_path, "documents", active=True)
    for resolve in (resolve_recognition_document_store, user_asr_lexicon):
        with pytest.raises(RuntimeError, match="recognition_document_cutover_evidence_required"):
            resolve(tmp_path)


def test_bulk_domain_prevalidates_duplicate_terms_before_any_mutation(tmp_path):
    domain = RealtimeAsrLexicon(JsonObjectStore(tmp_path / ".rebuild-data"))
    assert len(domain.bulk_upsert((("Alpha", 4), ("ALPHA", 4)))) == 1
    assert domain.current_revision() == 1 and len(domain.list_terms()) == 1
    with pytest.raises(ValueError, match="duplicate lexicon weights conflict"):
        domain.bulk_upsert((("Beta", 3), ("BETA", 5)))
    assert domain.current_revision() == 1 and len(domain.list_terms()) == 1


def test_active_capacity_checks_whole_result_without_losing_existing_terms(tmp_path):
    subject = service(tmp_path)
    filled = subject.import_text(text="\n".join(f"term-{index}" for index in range(2000)), expected_revision=0, preview=False)
    assert filled["imported_count"] == 2000 and filled["revision"] == 1
    with pytest.raises(AsrLexiconError, match="asr_lexicon_capacity_exceeded"):
        subject.import_text(text="one-more-term", expected_revision=1, preview=False)
    assert subject.current_revision() == 1 and len(subject.read()["terms"]) == 2000


def test_json_surrogate_rejects_whole_batch_without_partial_state(tmp_path):
    subject = service(tmp_path)
    text = json.dumps([{"term": "valid-term"}, {"term": chr(0xD800)}], ensure_ascii=True)
    with pytest.raises(AsrLexiconError, match="invalid_asr_lexicon_import"):
        subject.import_text(text=text, format="json", expected_revision=0, preview=False)
    assert subject.read()["revision"] == 0
    assert subject.read()["terms"] == []
    assert subject.records.list("realtime_asr_lexicon_state") == ()
