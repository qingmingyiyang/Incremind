from __future__ import annotations

from core.product_core import (
    CreateInspirationCollision,
    GetInspirationOverview,
    RecordInspirationFromSource,
    serialize_inspiration_collision_result,
    serialize_inspiration_overview_result,
    serialize_inspiration_record_result,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path):
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def test_record_inspiration_from_completed_source_read_updates_series_and_source(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(
        "sources",
        "source-idea",
        {
            "id": "source-idea",
            "title": "灵感记录",
            "type": "text",
            "metadata": {
                "content_read": {
                    "read_ref": "crp://default/source-content-reads/content-read-source-idea.json"
                }
            },
        },
        expected_revision=None,
    )
    store.write(
        "source_content_reads",
        "content-read-source-idea",
        {
            "id": "content-read-source-idea",
            "source_id": "source-idea",
            "status": "completed",
            "text": "突然想到：资料库可以把灵感作为默认系列，并参与后续项目构思。\n\n如果结合四层记忆，可以让想法参与输出。",
        },
        expected_revision=None,
    )

    result = RecordInspirationFromSource(store).execute(source_id="source-idea", project_id="chriptmas-os")
    body = serialize_inspiration_record_result(result)

    assert body["status"] == "recorded"
    assert body["series_name"] == "灵感 · chriptmas-os"
    assert {"产品", "记忆", "项目"}.intersection(set(body["themes"]))
    assert body["memory_publication_state"] == "not_published"
    assert "automatic_long_term_memory_publication" in body["blocked_operations"]

    record = store.read("inspiration_records", "inspiration-source-idea")
    assert record is not None
    assert record["source_id"] == "source-idea"
    assert record["memory_publication"] == "not_started"

    series = store.read("inspiration_series", body["series_id"])
    assert series is not None
    assert series["inspiration_ids"] == ["inspiration-source-idea"]
    assert series["source_ids"] == ["source-idea"]

    source = store.read("sources", "source-idea")
    inspiration = source["metadata"]["inspiration"]
    assert inspiration["status"] == "recorded"
    assert inspiration["inspiration_id"] == "inspiration-source-idea"
    assert "sk-" not in str(body).lower()


def test_inspiration_collision_uses_local_records_without_provider(tmp_path) -> None:
    store = _store(tmp_path)
    for source_id, text in {
        "source-a": "灵感：资料库的标签可以和项目 skill 发生碰撞，形成输出模板。",
        "source-b": "想法：视频转写摘要可以沉淀为记忆，再服务项目推进。",
    }.items():
        store.write(
            "sources",
            source_id,
            {"id": source_id, "title": source_id, "type": "text", "metadata": {}},
            expected_revision=None,
        )
        RecordInspirationFromSource(store).execute(source_id=source_id, text=text, project_id="project-alpha")

    result = CreateInspirationCollision(store).execute(
        query="项目 skill 和资料库",
        themes=("项目", "记忆"),
        project_id="project-alpha",
    )
    body = serialize_inspiration_collision_result(result)

    assert body["status"] == "created"
    assert len(body["selected_fragments"]) >= 2
    assert body["prompts"]
    assert body["memory_publication_state"] == "not_published"
    assert "model_provider_execution" in body["blocked_operations"]
    assert store.read("inspiration_collisions", body["collision_id"]) is not None


def test_inspiration_overview_groups_records_heatmap_and_prompts(tmp_path) -> None:
    store = _store(tmp_path)
    for source_id, text in {
        "source-a": "灵感：资料库的标签可以和项目 skill 发生碰撞，形成输出模板。",
        "source-b": "想法：视频转写摘要可以沉淀为记忆，再服务项目推进。",
    }.items():
        store.write(
            "sources",
            source_id,
            {"id": source_id, "title": source_id, "type": "text", "metadata": {}},
            expected_revision=None,
        )
        RecordInspirationFromSource(store).execute(source_id=source_id, text=text, project_id="project-alpha")

    CreateInspirationCollision(store).execute(
        query="项目 skill 和资料库",
        themes=("项目", "记忆"),
        project_id="project-alpha",
    )

    overview = GetInspirationOverview(store).execute(project_id="project-alpha")
    body = serialize_inspiration_overview_result(overview)

    assert body["status"] == "ready"
    assert body["counts"]["records"] == 2
    assert body["counts"]["series"] == 1
    assert body["counts"]["collisions"] == 1
    assert len(body["heatmap_days"]) == 35
    assert body["heatmap_days"][-2]["date"] == "2026-07-02"
    assert body["heatmap_days"][-2]["count"] == 2
    assert body["themes"]
    assert body["records"][0]["fragments"]
    assert body["recommended_prompts"]
    assert body["memory_publication_state"] == "not_published"
    assert "automatic_long_term_memory_publication" in body["blocked_operations"]
