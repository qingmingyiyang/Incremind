from __future__ import annotations

import hashlib

from core.product_core.progressive_recall_authority_reader import (
    ObjectStoreProgressiveRecallAuthorityReader,
)
from core.product_core.progressive_recall_drilldown import EvidenceSourceRef
from core.storage_provider import JsonObjectStore


CONTENT = "真实中文原文\n第二段证据"
CONTENT_HASH = hashlib.sha256(CONTENT.encode("utf-8")).hexdigest()
ALLOWED = (("source-1", "section:one"),)


def _store(tmp_path) -> JsonObjectStore:
    store = JsonObjectStore(tmp_path / "vault")
    store.write(
        "sources",
        "source-1",
        {
            "id": "source-1",
            "capture_mode": "inline",
            "content_hash": CONTENT_HASH,
            "processing_state": "ready",
            "trust_status": "user_confirmed",
            "metadata": {"content": CONTENT},
        },
        expected_revision=None,
    )
    return store


def test_reads_current_document_structure_and_media_summary(tmp_path) -> None:
    store = _store(tmp_path)
    source = dict(store.read("sources", "source-1"))
    source["metadata"] = {
        **source["metadata"],
        "content_structure": {
            "status": "completed",
            "structure_id": "structure-1",
        },
        "audio_track_extraction": {
            "summary_state": "completed",
            "summary_output_id": "summary-1",
        },
    }
    store.write("sources", "source-1", source, expected_revision=None)
    store.write(
        "documents",
        "document-1",
        {
            "id": "document-1",
            "project_id": "project-1",
            "status": "published",
            "revision": 2,
            "content_hash": f"sha256:{'c' * 64}",
            "blocks": [
                {
                    "id": "block-1",
                    "content": "Document 结构化段落",
                    "source_refs": [
                        {"source_id": "source-1", "locator": "section:one"}
                    ],
                }
            ],
        },
        expected_revision=None,
    )
    store.write(
        "source_structures",
        "structure-1",
        {
            "id": "structure-1",
            "source_id": "source-1",
            "status": "completed",
            "structured_body": "章节\n- 要点",
            "series_candidate": "series-1",
        },
        expected_revision=None,
    )
    store.write(
        "media_processing_outputs",
        "summary-1",
        {
            "id": "summary-1",
            "source_id": "source-1",
            "status": "completed",
            "output_kind": "summary",
            "markdown": "媒体摘要",
        },
        expected_revision=None,
    )
    reader = ObjectStoreProgressiveRecallAuthorityReader(store)
    items = reader.read_structured(
        project_id="project-1",
        series_ids=("series-1",),
        allowed_source_refs=ALLOWED,
        query="结构化媒体",
    )
    assert {item.object_type for item in items} == {
        "document_block",
        "source_structure",
        "media_structure",
    }
    assert all(item.source_refs[0].source_content_hash == CONTENT_HASH for item in items)


def test_archived_conflicted_cross_project_and_unreferenced_are_excluded(tmp_path) -> None:
    store = _store(tmp_path)
    for index, (project, status, source_id) in enumerate(
        (
            ("project-1", "archived", "source-1"),
            ("project-1", "conflicted", "source-1"),
            ("project-2", "published", "source-1"),
            ("project-1", "published", "source-other"),
        )
    ):
        store.write(
            "documents",
            f"document-{index}",
            {
                "id": f"document-{index}",
                "project_id": project,
                "status": status,
                "revision": 1,
                "content_hash": f"sha256:{'d' * 64}",
                "blocks": [
                    {
                        "id": "block",
                        "content": "PRIVATE-CROSS-PROJECT",
                        "source_refs": [
                            {"source_id": source_id, "locator": "section:one"}
                        ],
                    }
                ],
            },
            expected_revision=None,
        )
    reader = ObjectStoreProgressiveRecallAuthorityReader(store)
    assert reader.read_structured(
        project_id="project-1",
        series_ids=("series-1",),
        allowed_source_refs=ALLOWED,
        query="PRIVATE",
    ) == ()


def test_reads_completed_content_read_and_inline_fallback(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(
        "source_content_reads",
        "read-1",
        {
            "id": "read-1",
            "source_id": "source-1",
            "status": "completed",
            "text": "抽取后的完整正文",
            "char_count": len("抽取后的完整正文"),
            "byte_count": len("抽取后的完整正文".encode()),
            "text_sha256": hashlib.sha256("抽取后的完整正文".encode()).hexdigest(),
        },
        expected_revision=None,
    )
    source = dict(store.read("sources", "source-1"))
    source["metadata"] = {
        **source["metadata"],
        "content_read": {
            "status": "completed",
            "read_ref": "crp://default/source-content-reads/read-1.json",
        },
    }
    store.write("sources", "source-1", source, expected_revision=None)
    reader = ObjectStoreProgressiveRecallAuthorityReader(store)
    items = reader.read_source_evidence(
        project_id="project-1",
        source_refs=(),
        allowed_source_refs=ALLOWED,
        query="完整正文",
    )
    assert [(item.object_type, item.content) for item in items] == [
        ("source_content_read", "抽取后的完整正文")
    ]

    store.delete("source_content_reads", "read-1")
    source = dict(store.read("sources", "source-1"))
    source["metadata"] = {"content": CONTENT}
    store.write("sources", "source-1", source, expected_revision=None)
    fallback = reader.read_source_evidence(
        project_id="project-1",
        source_refs=(),
        allowed_source_refs=ALLOWED,
        query="原文",
    )
    assert [(item.object_type, item.content) for item in fallback] == [
        ("inline_source", CONTENT)
    ]


def test_hash_drift_untrusted_and_missing_body_fail_closed(tmp_path) -> None:
    store = _store(tmp_path)
    reader = ObjectStoreProgressiveRecallAuthorityReader(store)
    drifted = reader.read_source_evidence(
        project_id="project-1",
        source_refs=(
            EvidenceSourceRef("source-1", "section:one", "f" * 64),
        ),
        allowed_source_refs=ALLOWED,
        query="原文",
    )
    assert drifted == ()

    source = dict(store.read("sources", "source-1"))
    source["trust_status"] = "imported_unverified"
    store.write("sources", "source-1", source, expected_revision=None)
    assert reader.read_source_evidence(
        project_id="project-1",
        source_refs=(),
        allowed_source_refs=ALLOWED,
        query="原文",
    ) == ()


def test_only_current_content_read_and_current_transcript_are_visible(tmp_path) -> None:
    store = _store(tmp_path)
    source = dict(store.read("sources", "source-1"))
    source["metadata"] = {
        "content_read": {
            "status": "completed",
            "read_ref": "crp://default/source-content-reads/read-current.json",
        },
        "audio_transcription": {
            "asr_state": "completed",
            "transcript_output_id": "transcript-current",
        },
    }
    store.write("sources", "source-1", source, expected_revision=None)
    for read_id, text in (
        ("read-old", "PRIVATE-STALE-READ"),
        ("read-current", "CURRENT-READ"),
    ):
        store.write(
            "source_content_reads",
            read_id,
            {
                "id": read_id,
                "source_id": "source-1",
                "status": "completed",
                "text": text,
                "char_count": len(text),
                "byte_count": len(text.encode()),
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
            expected_revision=None,
        )
    for output_id, text in (
        ("transcript-old", "PRIVATE-STALE-TRANSCRIPT"),
        ("transcript-current", "CURRENT-TRANSCRIPT"),
    ):
        store.write(
            "media_processing_outputs",
            output_id,
            {
                "id": output_id,
                "source_id": "source-1",
                "status": "completed",
                "output_kind": "transcript",
                "text": text,
                "char_count": len(text),
                "byte_count": len(text.encode()),
            },
            expected_revision=None,
        )
    items = ObjectStoreProgressiveRecallAuthorityReader(
        store
    ).read_source_evidence(
        project_id="project-1",
        source_refs=(),
        allowed_source_refs=ALLOWED,
        query="CURRENT",
    )
    assert {item.content for item in items} == {"CURRENT-READ", "CURRENT-TRANSCRIPT"}


def test_logically_deleted_source_is_not_read(tmp_path) -> None:
    store = _store(tmp_path)
    source = dict(store.read("sources", "source-1"))
    source["library_lifecycle"] = {"status": "deleted"}
    store.write("sources", "source-1", source, expected_revision=None)
    reader = ObjectStoreProgressiveRecallAuthorityReader(store)
    assert reader.read_structured(
        project_id="project-1",
        series_ids=("series-1",),
        allowed_source_refs=ALLOWED,
        query="原文",
    ) == ()
    assert reader.read_source_evidence(
        project_id="project-1",
        source_refs=(),
        allowed_source_refs=ALLOWED,
        query="原文",
    ) == ()

    source["trust_status"] = "user_confirmed"
    source["capture_mode"] = "reference"
    source["metadata"] = {"content_snapshot": None}
    store.write("sources", "source-1", source, expected_revision=None)
    assert reader.read_source_evidence(
        project_id="project-1",
        source_refs=(),
        allowed_source_refs=ALLOWED,
        query="原文",
    ) == ()
