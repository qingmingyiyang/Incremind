from core.search_and_recall import build_recall_entries_from_object_store
from core.storage_provider import JsonObjectStore


def test_projects_real_source_content_reads_and_traceable_memory(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    store.write("sources", "source-alpha", {
        "id": "source-alpha", "title": "项目入口", "trust_status": "user_confirmed",
        "metadata": {"content": "旧词正文", "project_id": "project-alpha"},
    }, expected_revision=None)
    store.write("source_content_reads", "read-alpha", {
        "id": "read-alpha", "source_id": "source-alpha", "status": "completed", "text": "新词完整正文",
    }, expected_revision=None)
    store.write("project_skills", "skill-alpha", {
        "id": "skill-alpha", "project_id": "project-alpha", "purpose": "证据支持的规则",
        "output_rules": ["只引用当前项目"], "source_refs": [{"source_id": "source-alpha", "locator": "char:0-8"}],
        "trust_status": "user_confirmed", "status": "active",
    }, expected_revision=None)

    entries = build_recall_entries_from_object_store(store)

    assert [entry.object_id for entry in entries] == ["source-alpha", "skill-alpha"]
    source = entries[0]
    assert source.project_id == "project-alpha"
    assert "旧词正文" in source.content and "新词完整正文" in source.content
    assert source.source_refs == ("source-alpha#source:content",)
    assert entries[1].layer == "l3_project_skill"
    assert entries[1].source_refs == ("source-alpha#char:0-8",)


def test_non_active_project_skills_never_enter_recall(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    for status in ("draft", "conflicted", "archived", "rolled_back"):
        store.write("project_skills", f"skill-{status}", {
            "id": f"skill-{status}",
            "project_id": "project-alpha",
            "name": f"{status} method",
            "purpose": "尚未成为当前生产方法",
            "output_rules": ["不得进入 Recall"],
            "source_refs": [{"source_id": "source-alpha", "locator": f"skill:{status}"}],
            "trust_status": "user_confirmed",
            "status": status,
        }, expected_revision=None)

    assert build_recall_entries_from_object_store(store) == ()


def test_skips_soft_deleted_sources_and_untraceable_memory(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    store.write("sources", "source-deleted", {
        "id": "source-deleted", "title": "不得命中", "metadata": {},
        "library_lifecycle": {"status": "deleted", "deleted_at": "2026-07-22T00:00:00Z"},
    }, expected_revision=None)
    store.write("memory_atoms", "atom-no-ref", {"id": "atom-no-ref", "content": "没有来源"}, expected_revision=None)

    assert build_recall_entries_from_object_store(store) == ()


def test_restored_source_with_delete_audit_timestamps_returns_to_recall(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    store.write("sources", "source-restored", {
        "id": "source-restored", "title": "恢复后应重新命中", "metadata": {},
        "library_lifecycle": {
            "status": "active",
            "operation_id": "library-delete-test",
            "deleted_at": "2026-07-22T00:00:00Z",
            "undo_expires_at": "2026-07-29T00:00:00Z",
            "restored_at": "2026-07-22T01:00:00Z",
        },
    }, expected_revision=None)

    entries = build_recall_entries_from_object_store(store)

    assert [entry.object_id for entry in entries] == ["source-restored"]
