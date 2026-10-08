from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.composition import build_document_repository
from core.document_engine import (
    DocumentDraft,
    DocumentExpectedRevisionError,
    DocumentRepositoryError,
    ObjectStoreDocumentRepository,
)
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _repository(tmp_path: Path) -> ObjectStoreDocumentRepository:
    return ObjectStoreDocumentRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )


def _source_ref() -> dict[str, object]:
    return {
        "source_id": "source-text-001",
        "locator": "char:0-80",
        "quote": "Document runtime keeps source snapshot.",
    }


def test_object_store_document_repository_persists_document_revision_and_markdown(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    document = repository.create(
        DocumentDraft(
            title="项目复盘草稿",
            document_type="project_doc",
            markdown="Document runtime keeps source snapshot.",
            source_refs=(_source_ref(),),
            project_id="project-alpha",
        )
    )

    document_id = str(document["id"])
    revision = repository.revision(document_id, 1)
    reloaded = _repository(tmp_path)
    loaded_document = reloaded.read(document_id)
    loaded_revision = reloaded.revision(document_id, 1)

    assert loaded_document == document
    assert loaded_revision == revision
    assert reloaded.markdown(document_id) == "Document runtime keeps source snapshot."
    assert loaded_document is not None
    assert loaded_revision is not None
    assert loaded_document["source_snapshot"]["source_refs"] == [_source_ref()]
    assert loaded_revision["source_snapshot"] == loaded_document["source_snapshot"]
    assert validate_contract_instance("document.schema.json", _schema("document.schema.json"), loaded_document) == []
    assert (
        validate_contract_instance(
            "document_revision.schema.json",
            _schema("document_revision.schema.json"),
            loaded_revision,
        )
        == []
    )
    assert not (tmp_path / "library").exists()


def test_user_edit_revision_is_append_only_and_ai_patch_reports_conflict(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    document = repository.create(
        DocumentDraft(
            title="可编辑输出",
            document_type="summary",
            markdown="AI draft with source.",
            source_refs=(_source_ref(),),
        )
    )
    document_id = str(document["id"])

    edited = repository.save_user_edit(
        document_id,
        markdown="用户改写：保留这段人工判断。",
        expected_revision=1,
    )
    user_block = edited["blocks"][0]
    conflicted = repository.apply_ai_patch(
        document_id,
        expected_revision=2,
        blocks=[
            {
                "id": user_block["id"],
                "block_type": user_block["block_type"],
                "origin": "ai",
                "content": "AI 尝试覆盖用户人工判断。",
                "source_refs": [_source_ref()],
                "edited_by_user": False,
                "lock_policy": "source_required",
            }
        ],
    )

    assert edited["revision"] == 2
    assert edited["blocks"][0]["content"] == "用户改写：保留这段人工判断。"
    assert edited["blocks"][0]["edited_by_user"] is True
    assert edited["blocks"][0]["lock_policy"] == "user_edit_protected"
    assert conflicted["revision"] == 3
    assert conflicted["status"] == "conflicted"
    assert conflicted["blocks"][0]["content"] == "用户改写：保留这段人工判断。"
    assert repository.markdown(document_id) == "用户改写：保留这段人工判断。"
    conflict_revision = repository.revision(document_id, 3)
    assert conflict_revision is not None
    assert conflict_revision["operation"] == "ai_patch"
    assert conflict_revision["conflict"]["status"] == "detected"
    assert conflict_revision["conflict"]["conflict_blocks"] == [user_block["id"]]
    assert conflict_revision["source_snapshot"]["source_refs"] == [_source_ref()]
    assert [item["revision"] for item in repository.revisions(document_id)] == [1, 2, 3]
    assert validate_contract_instance("document.schema.json", _schema("document.schema.json"), conflicted) == []
    assert (
        validate_contract_instance(
            "document_revision.schema.json",
            _schema("document_revision.schema.json"),
            conflict_revision,
        )
        == []
    )
    assert not (tmp_path / "library").exists()


def test_document_repository_rejects_stale_expected_revision(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    document = repository.create(
        DocumentDraft(
            title="Revision guard",
            document_type="note",
            markdown="Initial source-backed draft.",
            source_refs=(_source_ref(),),
        )
    )
    document_id = str(document["id"])
    repository.save_user_edit(
        document_id,
        markdown="First user edit.",
        expected_revision=1,
    )

    with pytest.raises(DocumentExpectedRevisionError):
        repository.save_user_edit(
            document_id,
            markdown="Stale user edit.",
            expected_revision=1,
        )


def test_document_repository_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    repository = build_document_repository(ROOT, runtime_root=tmp_path)

    document = repository.create(
        DocumentDraft(
            title="Composed document",
            document_type="note",
            markdown="Composed runtime stays inside temp storage.",
            source_refs=(_source_ref(),),
        )
    )

    assert repository.read(str(document["id"])) == document
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "documents").exists()
    assert not (tmp_path / "library").exists()


def test_generated_document_create_or_replay_reuses_only_untouched_r1_authority(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    draft = DocumentDraft(
        title="幂等生成文档",
        document_type="summary",
        markdown="# 结论\n\n可重放生成必须验证完整 r1 权威。",
        source_refs=(_source_ref(),),
        project_id="project-alpha",
    )

    first = repository.create_or_replay_generated(draft)
    replayed = repository.create_or_replay_generated(draft)
    document_id = str(first["id"])

    assert replayed == first
    assert [item["revision"] for item in repository.revisions(document_id)] == [1]
    assert repository.markdown(document_id, revision=1) == draft.markdown


@pytest.mark.parametrize(
    "changed_draft",
    [
        pytest.param(
            lambda draft: DocumentDraft(
                title=draft.title,
                document_type=draft.document_type,
                markdown=draft.markdown,
                source_refs=draft.source_refs,
                project_id="project-other",
            ),
            id="project-drift",
        ),
        pytest.param(
            lambda draft: DocumentDraft(
                title=draft.title,
                document_type=draft.document_type,
                markdown=draft.markdown,
                source_refs=(
                    {"source_id": "source-other", "locator": "char:10-20"},
                ),
                project_id=draft.project_id,
            ),
            id="source-snapshot-drift",
        ),
    ],
)
def test_generated_document_replay_rejects_deterministic_id_collisions(
    tmp_path: Path,
    changed_draft,
) -> None:
    repository = _repository(tmp_path)
    draft = DocumentDraft(
        title="同一确定性 ID",
        document_type="summary",
        markdown="内容相同但来源和项目也必须匹配。",
        source_refs=(_source_ref(),),
        project_id="project-alpha",
    )
    repository.create_or_replay_generated(draft)

    with pytest.raises(DocumentRepositoryError, match="generated document replay conflict"):
        repository.create_or_replay_generated(changed_draft(draft))


@pytest.mark.parametrize(
    "tamper",
    [
        "missing-revision",
        "missing-markdown",
        "markdown-drift",
        "current-content-drift",
        "unexpected-revision",
    ],
)
def test_generated_document_replay_fails_closed_for_partial_or_drifted_r1_authority(
    tmp_path: Path,
    tamper: str,
) -> None:
    repository = _repository(tmp_path)
    draft = DocumentDraft(
        title="完整 r1 门禁",
        document_type="summary",
        markdown="生成文档的重放不能接受局部权威。",
        source_refs=(_source_ref(),),
        project_id="project-alpha",
    )
    document = repository.create_or_replay_generated(draft)
    document_id = str(document["id"])
    revision_id = f"{document_id}~r1"

    if tamper == "missing-revision":
        assert repository.object_store.delete("document_revisions", revision_id) is True
    elif tamper == "missing-markdown":
        assert repository.object_store.delete("document_markdown", revision_id) is True
    elif tamper == "markdown-drift":
        payload = repository.object_store.read("document_markdown", revision_id)
        assert payload is not None
        altered = dict(payload)
        altered["markdown"] = "被篡改的 Markdown"
        repository.object_store.write("document_markdown", revision_id, altered, expected_revision=1)
    elif tamper == "current-content-drift":
        current = repository.object_store.read("documents", document_id)
        assert current is not None
        altered = dict(current)
        altered["content_hash"] = "sha256:tampered"
        repository.object_store.write("documents", document_id, altered, expected_revision=1)
    else:
        repository.object_store.write(
            "document_revisions",
            f"{document_id}~r2",
            {
                "id": f"document-revision-{document_id}-r2",
                "document_id": document_id,
                "revision": 2,
            },
            expected_revision=0,
        )

    with pytest.raises(DocumentRepositoryError, match="generated document replay conflict"):
        repository.create_or_replay_generated(draft)
