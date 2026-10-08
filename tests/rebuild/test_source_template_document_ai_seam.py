from __future__ import annotations

from pathlib import Path

import pytest

from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository
from core.product_core.source_template_document import (
    ApprovedSourceDocumentDraftWriter,
    SourceTemplateDocumentError,
    normalize_source_document_ai_output,
    prepare_source_document_ai_evidence,
    source_document_ai_system_prompt,
    source_document_ai_user_payload,
)
from core.storage_provider import JsonObjectStore


def test_source_document_ai_pure_preparation_and_normalization_do_not_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store)
    before = _collections(store)

    evidence = prepare_source_document_ai_evidence(
        source=store.read("sources", "source-1"),
        source_revision=store.revision("sources", "source-1"),
        template_type="answer_manual",
        prompt_context=({"id": "pt-title", "revision": 2, "source": "developer_studio_active", "content": "标题直接说明结论。"},),
        style_prefix="统一输出范式：直接、清晰",
    )
    normalized = normalize_source_document_ai_output(
        evidence=evidence,
        value={"title": "资料结论", "markdown": "## 结论\n\n这是有证据的结论。\n\n## 待确认\n\n无。"},
        provider_name="provider-test",
    )

    assert source_document_ai_system_prompt(evidence).startswith("统一输出范式")
    assert source_document_ai_user_payload(evidence)["source_revision"] == 1
    assert normalized["kind"] == "source.document.normalized"
    assert "Provider: provider-test" in normalized["markdown"]
    assert _collections(store) == before


def test_approved_source_document_writer_replays_generation_without_duplicate_document(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store)
    evidence = _evidence(store)
    writer = ApprovedSourceDocumentDraftWriter(
        object_store=store,
        documents=ObjectStoreDocumentRepository(store),
        namespace_id="default",
    )
    generated = {"title": "资料结论", "markdown": "## 结论\n\n一条可追溯结论。\n\n## 待确认\n\n无。"}

    first = writer.execute(evidence=evidence, generated=generated, provider_id="provider-test", model_name="model-test")
    replay = writer.execute(evidence=evidence, generated=generated, provider_id="provider-test", model_name="model-test")

    assert first.replayed is False and replay.replayed is True
    assert replay.document.document_id == first.document.document_id
    assert replay.receipt_ref == first.receipt_ref
    assert replay.receipt_ref.startswith("crp://default/source-template-outputs/")
    assert len(store.list("documents")) == 1
    assert len(store.list("source_template_outputs")) == 1
    assert store.list("memory_candidates") == ()
    output = store.list("source_template_outputs")[0]
    assert output["request_payload_persisted"] is False
    assert "endpoint" not in output and "api_key" not in output


def test_approved_source_document_writer_repairs_interrupted_source_link_without_duplicate_document(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _write_source(store)
    evidence = _evidence(store)
    interrupted_store = _InterruptSourceLinkOnce(store)
    writer = ApprovedSourceDocumentDraftWriter(
        object_store=interrupted_store,
        documents=ObjectStoreDocumentRepository(interrupted_store),
        namespace_id="default",
    )
    generated = {"title": "资料结论", "markdown": "## 结论\n\n一条可追溯结论。"}
    with pytest.raises(RuntimeError, match="simulated interruption"):
        writer.execute(evidence=evidence, generated=generated, provider_id="provider-test", model_name="model-test")

    replay = writer.execute(evidence=evidence, generated=generated, provider_id="provider-test", model_name="model-test")

    assert replay.replayed is True
    assert len(store.list("documents")) == 1
    assert len(store.list("source_template_outputs")) == 1
    source = store.read("sources", "source-1")
    assert source is not None
    assert source["metadata"]["template_outputs"][0]["generation_id"] == replay.generation_id
    assert store.revision("sources", "source-1") == 2


def test_approved_source_document_writer_fails_closed_on_source_revision_drift(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _write_source(store)
    evidence = _evidence(store)
    changed = dict(source)
    changed["title"] = "漂移后的标题"
    store.write("sources", "source-1", changed, expected_revision=1)

    with pytest.raises(SourceTemplateDocumentError, match="baseline is stale"):
        ApprovedSourceDocumentDraftWriter(
            object_store=store,
            documents=ObjectStoreDocumentRepository(store),
        ).execute(
            evidence=evidence,
            generated={"title": "资料结论", "markdown": "## 结论\n\n内容"},
            provider_id="provider-test",
            model_name="model-test",
        )

    assert store.list("documents") == ()
    assert store.list("source_template_outputs") == ()


def test_approved_source_document_writer_fails_closed_on_document_baseline_drift(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store)
    documents = ObjectStoreDocumentRepository(store)
    baseline_document = documents.create(
        DocumentDraft(
            title="已有文档",
            document_type="answer_manual",
            markdown="# 已有文档",
            source_refs=({"source_id": "source-1", "locator": "source:content"},),
            project_id="project-alpha",
        )
    )
    evidence = prepare_source_document_ai_evidence(
        source=store.read("sources", "source-1"),
        source_revision=1,
        template_type="answer_manual",
        document_baseline={
            "document_id": baseline_document["id"],
            "document_revision": baseline_document["revision"],
            "content_hash": baseline_document["content_hash"],
            "status": baseline_document["status"],
        },
    )
    documents.save(str(baseline_document["id"]), "# 用户修改", baseline_document["source_refs"], expected_revision=1)

    with pytest.raises(SourceTemplateDocumentError, match="document baseline is stale"):
        ApprovedSourceDocumentDraftWriter(object_store=store, documents=documents).execute(
            evidence=evidence,
            generated={"title": "资料结论", "markdown": "## 结论\n\n内容"},
            provider_id="provider-test",
            model_name="model-test",
        )

    assert len(store.list("documents")) == 1
    assert store.list("source_template_outputs") == ()


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


class _InterruptSourceLinkOnce:
    def __init__(self, delegate: JsonObjectStore) -> None:
        self._delegate = delegate
        self._interrupted = False

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    def write(self, collection, object_id, payload, *, expected_revision):
        if collection == "sources" and expected_revision == 1 and not self._interrupted:
            self._interrupted = True
            raise RuntimeError("simulated interruption after output receipt")
        return self._delegate.write(collection, object_id, payload, expected_revision=expected_revision)


def _write_source(store: JsonObjectStore) -> dict[str, object]:
    source = {
        "schema_version": "1.0.0",
        "id": "source-1",
        "project_id": "project-alpha",
        "title": "原始资料",
        "metadata": {
            "content_structure": {
                "status": "completed",
                "summary": "资料摘要",
                "key_points": ["关键点一"],
                "structured_body": "原始资料正文",
                "structure_ref": "crp://default/source-content/source-1.json",
                "series_candidate": "测试系列",
            }
        },
    }
    store.write("sources", "source-1", source, expected_revision=0)
    return source


def _evidence(store: JsonObjectStore) -> dict[str, object]:
    source = store.read("sources", "source-1")
    assert source is not None
    return prepare_source_document_ai_evidence(
        source=source,
        source_revision=store.revision("sources", "source-1"),
        template_type="answer_manual",
    )


def _collections(store: JsonObjectStore) -> dict[str, tuple[object, ...]]:
    return {
        collection: tuple(store.list(collection))
        for collection in ("sources", "documents", "source_template_outputs", "memory_candidates")
    }
