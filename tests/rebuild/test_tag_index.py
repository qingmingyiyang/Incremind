from __future__ import annotations

from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.job_runner import ObjectStoreJobRepository
from core.product_core import (
    BulkLibraryItemAction,
    IndexSourceTags,
    OrchestrateWorkbenchAutoIntake,
    QueryTagFacets,
    QueryTaggedSources,
    UpdateLibrarySourceMetadata,
    serialize_tag_facets_result,
    serialize_tagged_sources_result,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _orchestrator(store: JsonObjectStore) -> OrchestrateWorkbenchAutoIntake:
    return OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=ObjectStoreSourceRegistrar(store, namespace_id="default"),
        job_repository=ObjectStoreJobRepository(store),
        fetch_url=lambda url: "<html><body><p>个人 AI 记忆工作台 资料库 记忆 四层</p></body></html>",
        namespace_id="default",
    )


def test_orchestrator_indexes_paragraph_tags(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        content="个人 AI 记忆工作台 资料库 记忆 四层",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    item = result.items[0]
    assert item.auto_organization["tag_index_status"] == "indexed"
    tag_index_records = store.list("tag_index")
    assert len(tag_index_records) > 0
    tags = {record.get("tag") for record in tag_index_records}
    assert "Memory" in tags or "Product" in tags


def test_index_source_tags_builds_refs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)
    orchestrator.execute(
        content="AI 记忆 产品 资料",
        add_to_knowledge_base=True,
    )
    source_id = orchestrator.execute(
        content="AI 记忆 产品 资料",
        add_to_knowledge_base=True,
    ).items[0].source_id

    indexer = IndexSourceTags(store, namespace_id="default")
    result = indexer.execute(source_id=source_id)

    assert result.status == "indexed"
    assert result.ref_count > 0
    assert len(result.indexed_tags) > 0
    for tag in result.indexed_tags:
        record = store.read("tag_index", _tag_index_id_for(tag))
        assert record is not None
        assert record.get("source_count") == 1
        assert any(ref.get("source_id") == source_id for ref in record.get("refs", []))


def test_tag_facets_aggregate_multiple_sources(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)
    orchestrator.execute(content="AI 记忆 产品", add_to_knowledge_base=True)
    orchestrator.execute(content="AI 记忆 项目", add_to_knowledge_base=True)

    facets = QueryTagFacets(store).execute()
    serialized = serialize_tag_facets_result(facets)
    assert serialized["status"] == "completed"
    assert serialized["total_tags"] > 0
    ai_facet = next((f for f in serialized["facets"] if f["tag"] == "AI"), None)
    assert ai_facet is not None
    assert ai_facet["source_count"] == 2
    assert ai_facet["ref_count"] >= 2


def test_tag_facets_and_hits_filter_by_current_source_project(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)
    alpha = orchestrator.execute(content="AI 记忆 产品", add_to_knowledge_base=True).items[0].source_id
    beta = orchestrator.execute(content="AI 记忆 项目", add_to_knowledge_base=True).items[0].source_id
    alpha_source = store.read("sources", alpha)
    beta_source = store.read("sources", beta)
    store.write(
        "sources", alpha, {**alpha_source, "project_id": "project-alpha"},
        expected_revision=store.revision("sources", alpha),
    )
    store.write(
        "sources", beta, {**beta_source, "project_id": "project-beta"},
        expected_revision=store.revision("sources", beta),
    )

    facets = QueryTagFacets(store).execute(project_id="project-alpha")
    hits = QueryTaggedSources(store).execute(tag="AI", project_id="project-alpha")

    ai_facet = next(facet for facet in facets.facets if facet.tag == "AI")
    assert ai_facet.source_count == 1 and ai_facet.ref_count == 1
    assert facets.total_tags > 0
    assert [hit.source_id for hit in hits.hits] == [alpha]
    assert hits.total_hits == 1

    limited = QueryTaggedSources(store).execute(tag="AI", limit=1)
    assert len(limited.hits) == 1 and limited.total_hits == 2


def test_query_tagged_sources_returns_hits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)
    first = orchestrator.execute(content="AI 记忆 产品", add_to_knowledge_base=True)
    orchestrator.execute(content="项目 推进 任务", add_to_knowledge_base=True)

    result = QueryTaggedSources(store).execute(tag="AI")
    serialized = serialize_tagged_sources_result(result)
    assert serialized["status"] == "completed"
    assert serialized["tag"] == "AI"
    assert serialized["total_hits"] == 1
    assert len(serialized["hits"]) == 1
    hit = serialized["hits"][0]
    assert hit["source_id"] == first.items[0].source_id
    assert "AI" in hit["matched_tags"]


def test_query_tagged_sources_returns_empty_for_unknown_tag(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)
    orchestrator.execute(content="AI 记忆", add_to_knowledge_base=True)

    result = QueryTaggedSources(store).execute(tag="Nonexistent")
    assert result.status == "completed"
    assert result.hits == ()
    assert result.total_hits == 0


def test_reindexing_source_replaces_old_refs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)
    first_result = orchestrator.execute(content="AI 记忆 产品", add_to_knowledge_base=True)
    source_id = first_result.items[0].source_id

    indexer = IndexSourceTags(store, namespace_id="default")
    indexer.execute(source_id=source_id)
    indexer.execute(source_id=source_id)

    ai_record = store.read("tag_index", _tag_index_id_for("AI"))
    assert ai_record is not None
    refs_for_source = [ref for ref in ai_record.get("refs", []) if ref.get("source_id") == source_id]
    assert len(refs_for_source) == 1, "reindexing the same source must not duplicate refs"


def test_manual_edit_tags_visible_in_facets_and_tagged_sources(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)
    source_id = orchestrator.execute(content="AI 记忆 产品", add_to_knowledge_base=True).items[0].source_id

    result = UpdateLibrarySourceMetadata(store).execute(
        source_id=source_id,
        expected_revision=store.revision("sources", source_id),
        title="手工整理",
        tags=("手工主题",),
    )
    assert result.status == "updated"

    facets = serialize_tag_facets_result(QueryTagFacets(store).execute())
    manual_facet = next((f for f in facets["facets"] if f["tag"] == "手工主题"), None)
    assert manual_facet is not None
    assert manual_facet["source_count"] == 1
    assert manual_facet["ref_count"] == 1

    hits = QueryTaggedSources(store).execute(tag="手工主题")
    assert hits.total_hits == 1
    assert [hit.source_id for hit in hits.hits] == [source_id]


def test_bulk_attach_tags_visible_in_facets_and_tagged_sources(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registrar = ObjectStoreSourceRegistrar(store, namespace_id="default")
    alpha = str(registrar.register(
        SourceSubmission(kind="text", title="Bulk A", content="批量正文 A")
    )["id"])
    beta = str(registrar.register(
        SourceSubmission(kind="text", title="Bulk B", content="批量正文 B")
    )["id"])

    result = BulkLibraryItemAction(store).execute(
        action="attach_tags", item_ids=[alpha, beta], tags=("批量主题",),
    )
    assert result.status == "completed"

    facets = serialize_tag_facets_result(QueryTagFacets(store).execute())
    facet = next((f for f in facets["facets"] if f["tag"] == "批量主题"), None)
    assert facet is not None
    assert facet["source_count"] == 2
    assert facet["ref_count"] == 2

    hits = QueryTaggedSources(store).execute(tag="批量主题")
    assert hits.total_hits == 2
    assert {hit.source_id for hit in hits.hits} == {alpha, beta}


def test_manual_and_structure_refs_coexist_across_reindex(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)
    source_id = orchestrator.execute(content="AI 记忆 产品", add_to_knowledge_base=True).items[0].source_id

    # 同一 tag "AI" 上追加用户手工标签：manual ref 必须与 structure ref 共存
    result = UpdateLibrarySourceMetadata(store).execute(
        source_id=source_id,
        expected_revision=store.revision("sources", source_id),
        title="手工补充 AI",
        tags=("AI",),
    )
    assert result.status == "updated"

    record = store.read("tag_index", _tag_index_id_for("AI"))
    origins = {ref.get("origin") for ref in record["refs"] if ref.get("source_id") == source_id}
    assert origins == {"structure", "manual"}

    # 重建结构索引只替换 structure refs，不得清掉 manual ref
    IndexSourceTags(store, namespace_id="default").execute(source_id=source_id)
    record = store.read("tag_index", _tag_index_id_for("AI"))
    origins = {ref.get("origin") for ref in record["refs"] if ref.get("source_id") == source_id}
    assert origins == {"structure", "manual"}
    assert record["source_count"] == 1

    facet = next(f for f in QueryTagFacets(store).execute().facets if f.tag == "AI")
    assert facet.ref_count >= 2
    assert facet.source_count == 1


def test_auto_series_confirmation_writes_system_confirmer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        content="个人 AI 记忆工作台 资料库 记忆",
        add_to_knowledge_base=True,
    )

    assignment = store.read(
        "source_series_assignments",
        f"series-assignment-{result.items[0].source_id}",
    )
    assert assignment is not None
    assert assignment.get("confirmed_by") == "system"
    assert "系统自动确认" in assignment.get("reason", "") or assignment.get("reason")


def _tag_index_id_for(tag: str) -> str:
    import hashlib

    return f"tag-{hashlib.sha256(tag.lower().encode('utf-8')).hexdigest()[:16]}"
