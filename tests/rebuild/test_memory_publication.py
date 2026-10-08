from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.composition import build_memory_publication, build_memory_rollback
from core.memory_core import ObjectStoreMemoryStore, build_manual_publication_context
from core.product_core import (
    MemoryPublicationError,
    PublishStagingAtomToMemory,
    PublishStagingMemoryToMemory,
    RollbackPublishedAtomMemory,
    RollbackPublishedMemory,
    ServeMemoryPublicationEndpoint,
)
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _stage(object_store: JsonObjectStore, layer: str, payload: dict[str, object]) -> dict[str, object]:
    source_refs = payload["source_refs"]
    ObjectStoreMemoryStore(object_store).save_candidate(
        layer,
        payload,
        publication_context=build_manual_publication_context(
            namespace_id="default", layer=layer, draft_id=str(payload["id"]),
            candidate_id=f"candidate-{payload['id']}", reviewed_at="2026-07-01T18:00:00+08:00",
            review_reason="用户确认候选进入草稿。", source_refs=source_refs, evidence_refs=source_refs,
        ),
    )
    return payload


def _stage_atom(object_store: JsonObjectStore, atom_id: str = "atom-draft-publication-001") -> dict[str, object]:
    atom = {
        "schema_version": "1.0.0",
        "id": atom_id,
        "source_id": "source-alpha",
        "content": "用户确认后的草稿 Atom 可以进入长期记忆。",
        "atom_type": "fact",
        "tags": ["memory", "publication"],
        "confidence": 0.8,
        "source_refs": [
            {
                "source_id": "source-alpha",
                "locator": "source:content",
                "quote": "用户确认后的草稿 Atom 可以进入长期记忆。",
            }
        ],
        "revision": 1,
        "created_at": "2026-07-01T19:00:00+08:00",
        "updated_at": "2026-07-01T19:00:00+08:00",
        "trust_status": "system_generated",
    }
    return _stage(object_store, "atom", atom)


def _stage_scenario(object_store: JsonObjectStore, scenario_id: str = "scenario-draft-publication-001") -> dict[str, object]:
    scenario = {
        "schema_version": "1.0.0",
        "id": scenario_id,
        "title": "多层记忆发布场景",
        "summary": "Scenario 草稿必须经用户二次确认后才能写入长期记忆。",
        "atom_ids": [],
        "source_refs": [{"source_id": "source-alpha", "locator": "char:0-80"}],
        "tags": ["memory", "scenario"],
        "series_id": "series-alpha",
        "project_id": "project-alpha",
        "stale": False,
        "stale_reason": None,
        "revision": 1,
        "created_at": "2026-07-02T11:00:00+08:00",
        "updated_at": "2026-07-02T11:00:00+08:00",
        "trust_status": "system_generated",
    }
    return _stage(object_store, "scenario", scenario)


def _stage_series_memory(
    object_store: JsonObjectStore,
    series_memory_id: str = "series-memory-draft-publication-001",
) -> dict[str, object]:
    series_memory = {
        "schema_version": "1.0.0",
        "id": series_memory_id,
        "series_id": "series-alpha",
        "scope": "project",
        "overview": "Series Memory 草稿必须经用户二次确认后才能写入长期记忆。",
        "scenario_ids": [],
        "source_refs": [{"source_id": "source-alpha", "locator": "char:0-80"}],
        "project_ids": ["project-alpha"],
        "stale": False,
        "stale_reason": None,
        "revision": 1,
        "created_at": "2026-07-02T11:00:00+08:00",
        "updated_at": "2026-07-02T11:00:00+08:00",
        "trust_status": "system_generated",
    }
    return _stage(object_store, "series_memory", series_memory)


def _stage_project_skill(
    object_store: JsonObjectStore,
    skill_id: str = "skill-draft-publication-001",
) -> dict[str, object]:
    skill = {
        "schema_version": "1.0.0",
        "id": skill_id,
        "project_id": "project-alpha",
        "name": "多层记忆发布技能",
        "purpose": "Project Skill 草稿必须经用户二次确认后才能写入长期技能记忆。",
        "markdown_uri": "crp://default/projects/project-alpha/project-skill.md",
        "json_uri": "crp://default/projects/project-alpha/project-skill.json",
        "markdown_revision": 1,
        "json_revision": 1,
        "required_context": [
            {
                "context_id": "ctx-source-alpha",
                "kind": "source",
                "object_id": "source-alpha",
                "uri": "crp://default/sources/source-alpha.json",
                "reason": "作为 Project Skill 发布证据。",
                "stale": False,
            }
        ],
        "output_rules": [
            {
                "rule_id": "rule-layered-publication",
                "origin": "ai",
                "rule": "多层 Project Skill 必须二次确认发布。",
                "priority": "must",
                "source_refs": [{"source_id": "source-alpha", "locator": "char:0-80"}],
                "locked_by_user": False,
            }
        ],
        "style_preferences": {"voice": "direct", "format_defaults": ["Markdown"]},
        "update_rules": {
            "patch_strategy": "patch_existing_first",
            "user_edit_policy": "user_wins",
            "allowed_auto_updates": ["append_low_risk_context"],
        },
        "source_refs": [{"source_id": "source-alpha", "locator": "char:0-80"}],
        "evidence_refs": [{"source_id": "source-alpha", "locator": "char:0-80"}],
        "decision_log": [
            {
                "decision_id": "decision-skill-draft-publication",
                "reason": "用户确认进入 staging 后等待发布。",
                "actor": "user",
                "created_at": "2026-07-02T11:00:00+08:00",
            }
        ],
        "conflict": {"status": "none", "conflict_refs": [], "resolution": None},
        "revision": 1,
        "status": "draft",
        "trust_status": "system_generated",
        "created_at": "2026-07-02T11:00:00+08:00",
        "updated_at": "2026-07-02T11:00:00+08:00",
    }
    return _stage(object_store, "project_skill", skill)


def test_publish_staging_atom_writes_long_term_memory_transition_and_record(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    staged = _stage_atom(object_store)
    use_case = PublishStagingAtomToMemory(object_store, now="2026-07-01T19:05:00+08:00")

    result = use_case.execute(
        atom_id=str(staged["id"]),
        confirm=True,
        reason="用户确认该草稿 Atom 可以发布到长期记忆。",
    )
    memory = ObjectStoreMemoryStore(object_store)
    published = memory.get("atom", str(staged["id"]))
    staging = memory.staged("atom", str(staged["id"]))
    transition = object_store.read("memory_transitions", result.transition_id)
    publication = object_store.read("memory_publications", result.publication_id)

    assert result.status == "published"
    assert result.memory_publication_state == "published_with_rollback_ref"
    assert result.published_ref == f"crp://default/memory/atom/{staged['id']}.json"
    assert result.rollback_ref == f"crp://default/memory-publications/{result.publication_id}/rollback"
    assert published is not None
    assert staging is None
    assert published["trust_status"] == "user_confirmed"
    assert published["updated_at"] == "2026-07-01T19:05:00+08:00"
    assert validate_contract_instance("atom.schema.json", _schema("atom.schema.json"), published) == []
    assert transition is not None
    assert transition["transition_type"] == "confirm"
    assert transition["from_trust_status"] == "system_generated"
    assert transition["to_trust_status"] == "user_confirmed"
    assert transition["actor"] == "user"
    assert validate_contract_instance(
        "memory_transition.schema.json",
        _schema("memory_transition.schema.json"),
        transition,
    ) == []
    assert publication is not None
    assert publication["status"] == "published"
    assert publication["source_candidate_id"] == f"candidate-{staged['id']}"
    assert publication["reviewer"] == "user"
    assert publication["policy_id"] == "local-manual-v1"
    assert publication["published_revision"] == 1
    assert publication["published_at"] == "2026-07-01T19:05:00+08:00"
    assert publication["published_ref"] == result.published_ref
    assert publication["rollback_ref"] == result.rollback_ref
    assert not (tmp_path / "library").exists()


def test_memory_publication_rejects_legacy_staging_without_canonical_context(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    staged = _stage_atom(object_store, atom_id="atom-draft-legacy-context")
    object_store.delete("staging_memory_publication_contexts", f"atom~{staged['id']}")

    with pytest.raises(MemoryPublicationError, match="canonical manual publication context"):
        PublishStagingAtomToMemory(object_store).execute(
            atom_id=str(staged["id"]), confirm=True, reason="用户确认发布。"
        )

    assert ObjectStoreMemoryStore(object_store).staged("atom", str(staged["id"])) is not None
    assert ObjectStoreMemoryStore(object_store).get("atom", str(staged["id"])) is None
    assert object_store.list("memory_publications") == ()
    assert object_store.list("memory_transitions") == ()


def test_rollback_published_atom_removes_long_term_memory_and_marks_publication(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    staged = _stage_atom(object_store)
    publisher = PublishStagingAtomToMemory(object_store, now="2026-07-01T19:05:00+08:00")
    published = publisher.execute(
        atom_id=str(staged["id"]),
        confirm=True,
        reason="用户确认发布。",
    )
    rollback = RollbackPublishedAtomMemory(object_store, now="2026-07-01T20:05:00+08:00")

    result = rollback.execute(
        publication_id=published.publication_id,
        confirm=True,
        reason="用户撤回该长期记忆。",
    )
    memory = ObjectStoreMemoryStore(object_store)
    publication = object_store.read("memory_publications", published.publication_id)
    transition = object_store.read("memory_transitions", result.transition_id)

    assert result.status == "rolled_back"
    assert result.object_id == staged["id"]
    assert result.memory_publication_state == "rolled_back_not_published"
    assert memory.get("atom", str(staged["id"])) is None
    assert publication is not None
    assert publication["status"] == "rolled_back"
    assert publication["rollback_reason"] == "用户撤回该长期记忆。"
    assert publication["rolled_back_by"] == "user"
    assert publication["rollback_transition_ref"] == f"crp://default/memory-transitions/{result.transition_id}.json"
    assert transition is not None
    assert transition["transition_type"] == "demote"
    assert transition["from_trust_status"] == "user_confirmed"
    assert transition["to_trust_status"] == "system_generated"
    assert validate_contract_instance(
        "memory_transition.schema.json",
        _schema("memory_transition.schema.json"),
        transition,
    ) == []
    assert not (tmp_path / "library").exists()


def test_rollback_requires_confirmation_existing_publication_and_published_status(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    staged = _stage_atom(object_store)
    publisher = PublishStagingAtomToMemory(object_store)
    published = publisher.execute(
        atom_id=str(staged["id"]),
        confirm=True,
        reason="用户确认发布。",
    )
    rollback = RollbackPublishedAtomMemory(object_store)

    with pytest.raises(MemoryPublicationError, match="confirm=true"):
        rollback.execute(
            publication_id=published.publication_id,
            confirm=False,
            reason="未确认。",
        )
    with pytest.raises(MemoryPublicationError, match="memory publication not found"):
        rollback.execute(
            publication_id="memory-publication-missing",
            confirm=True,
            reason="不存在。",
        )

    first = rollback.execute(
        publication_id=published.publication_id,
        confirm=True,
        reason="用户撤回。",
    )
    with pytest.raises(MemoryPublicationError, match="not published"):
        rollback.execute(
            publication_id=first.publication_id,
            confirm=True,
            reason="不能重复回滚。",
        )


@pytest.mark.parametrize(
    ("layer", "stage_factory", "long_term_collection", "staging_collection"),
    [
        ("scenario", _stage_scenario, "memory_scenarios", "staging_scenarios"),
        ("series_memory", _stage_series_memory, "memory_series_memory", "staging_series_memory"),
        ("project_skill", _stage_project_skill, "project_skills", "staging_project_skills"),
    ],
)
def test_rollback_published_multilayer_memory_removes_long_term_object_and_marks_publication(
    tmp_path: Path,
    layer: str,
    stage_factory,
    long_term_collection: str,
    staging_collection: str,
) -> None:
    object_store = _store(tmp_path)
    staged = stage_factory(object_store)
    publisher = PublishStagingMemoryToMemory(object_store, now="2026-07-02T11:05:00+08:00")
    published = publisher.execute(
        object_id=str(staged["id"]),
        layer=layer,
        confirm=True,
        reason=f"用户确认发布 {layer}。",
    )
    rollback = RollbackPublishedMemory(object_store, now="2026-07-02T12:05:00+08:00")

    result = rollback.execute(
        publication_id=published.publication_id,
        confirm=True,
        reason=f"用户撤回 {layer} 长期记忆。",
    )
    publication = object_store.read("memory_publications", published.publication_id)
    transition = object_store.read("memory_transitions", result.transition_id)

    assert result.status == "rolled_back"
    assert result.layer == layer
    assert result.object_id == staged["id"]
    assert result.memory_publication_state == "rolled_back_not_published"
    assert ObjectStoreMemoryStore(object_store).get(layer, str(staged["id"])) is None
    assert ObjectStoreMemoryStore(object_store).staged(layer, str(staged["id"])) is None
    assert publication is not None
    assert publication["status"] == "rolled_back"
    assert publication["rollback_reason"] == f"用户撤回 {layer} 长期记忆。"
    assert publication["rolled_back_by"] == "user"
    assert publication["rollback_transition_ref"] == f"crp://default/memory-transitions/{result.transition_id}.json"
    assert transition is not None
    assert transition["object_type"] == layer
    assert transition["object_id"] == staged["id"]
    assert transition["transition_type"] == "demote"
    assert transition["from_trust_status"] == "user_confirmed"
    assert transition["to_trust_status"] == "system_generated"
    assert validate_contract_instance(
        "memory_transition.schema.json",
        _schema("memory_transition.schema.json"),
        transition,
    ) == []
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / long_term_collection / f"{staged['id']}.json").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / staging_collection / f"{staged['id']}.json").exists()
    assert not (tmp_path / "library").exists()


def test_rollback_published_multilayer_memory_rejects_repeated_or_missing_objects(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    staged = _stage_scenario(object_store)
    published = PublishStagingMemoryToMemory(object_store).execute(
        object_id=str(staged["id"]),
        layer="scenario",
        confirm=True,
        reason="用户确认发布。",
    )
    rollback = RollbackPublishedMemory(object_store)

    with pytest.raises(MemoryPublicationError, match="confirm=true"):
        rollback.execute(
            publication_id=published.publication_id,
            confirm=False,
            reason="未确认。",
        )
    first = rollback.execute(
        publication_id=published.publication_id,
        confirm=True,
        reason="用户撤回。",
    )
    with pytest.raises(MemoryPublicationError, match="not published"):
        rollback.execute(
            publication_id=first.publication_id,
            confirm=True,
            reason="不能重复撤回。",
        )


def test_publish_staging_atom_requires_confirmation_and_existing_staging_atom(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    staged = _stage_atom(object_store)
    use_case = PublishStagingAtomToMemory(object_store)

    with pytest.raises(MemoryPublicationError, match="confirm=true"):
        use_case.execute(
            atom_id=str(staged["id"]),
            confirm=False,
            reason="缺少确认。",
        )
    with pytest.raises(MemoryPublicationError, match="staging atom not found"):
        use_case.execute(
            atom_id="atom-draft-missing",
            confirm=True,
            reason="不存在的草稿不能发布。",
        )

    assert ObjectStoreMemoryStore(object_store).staged("atom", str(staged["id"])) is not None
    assert ObjectStoreMemoryStore(object_store).get("atom", str(staged["id"])) is None


@pytest.mark.parametrize(
    ("layer", "stage_factory", "schema_name", "long_term_collection", "staging_collection", "published_ref"),
    [
        (
            "scenario",
            _stage_scenario,
            "scenario.schema.json",
            "memory_scenarios",
            "staging_scenarios",
            "crp://default/memory/scenario/scenario-draft-publication-001.json",
        ),
        (
            "series_memory",
            _stage_series_memory,
            "series_memory.schema.json",
            "memory_series_memory",
            "staging_series_memory",
            "crp://default/memory/series/series-memory-draft-publication-001.json",
        ),
        (
            "project_skill",
            _stage_project_skill,
            "project_skill.schema.json",
            "project_skills",
            "staging_project_skills",
            "crp://default/memory/project-skill/skill-draft-publication-001.json",
        ),
    ],
)
def test_publish_staging_multilayer_memory_writes_long_term_transition_and_record(
    tmp_path: Path,
    layer: str,
    stage_factory,
    schema_name: str,
    long_term_collection: str,
    staging_collection: str,
    published_ref: str,
) -> None:
    object_store = _store(tmp_path)
    staged = stage_factory(object_store)
    use_case = PublishStagingMemoryToMemory(object_store, now="2026-07-02T11:05:00+08:00")

    result = use_case.execute(
        object_id=str(staged["id"]),
        layer=layer,
        confirm=True,
        reason=f"用户确认发布 {layer} 长期记忆。",
    )
    published = ObjectStoreMemoryStore(object_store).get(layer, str(staged["id"]))
    staging = ObjectStoreMemoryStore(object_store).staged(layer, str(staged["id"]))
    transition = object_store.read("memory_transitions", result.transition_id)
    publication = object_store.read("memory_publications", result.publication_id)

    assert result.status == "published"
    assert result.layer == layer
    assert result.published_ref == published_ref
    assert result.rollback_ref == f"crp://default/memory-publications/{result.publication_id}/rollback"
    assert published is not None
    assert staging is None
    assert published["trust_status"] == "user_confirmed"
    assert published["updated_at"] == "2026-07-02T11:05:00+08:00"
    assert validate_contract_instance(schema_name, _schema(schema_name), published) == []
    assert transition is not None
    assert transition["object_type"] == layer
    assert transition["transition_type"] == "confirm"
    assert transition["to_trust_status"] == "user_confirmed"
    assert validate_contract_instance(
        "memory_transition.schema.json",
        _schema("memory_transition.schema.json"),
        transition,
    ) == []
    assert publication is not None
    assert publication["layer"] == layer
    assert publication["object_type"] == layer
    assert publication["status"] == "published"
    assert publication["published_ref"] == published_ref
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / long_term_collection).exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / staging_collection / f"{staged['id']}.json").exists()
    assert not (tmp_path / "library").exists()


def test_publish_staging_multilayer_memory_requires_matching_layer_and_staging_object(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    staged = _stage_scenario(object_store)
    use_case = PublishStagingMemoryToMemory(object_store)

    with pytest.raises(MemoryPublicationError, match="layer is not supported"):
        use_case.execute(
            object_id=str(staged["id"]),
            layer="persona",
            confirm=True,
            reason="不支持的层级。",
        )
    with pytest.raises(MemoryPublicationError, match="staging series_memory not found"):
        use_case.execute(
            object_id=str(staged["id"]),
            layer="series_memory",
            confirm=True,
            reason="层级不匹配。",
        )
    with pytest.raises(MemoryPublicationError, match="confirm=true"):
        use_case.execute(
            object_id=str(staged["id"]),
            layer="scenario",
            confirm=False,
            reason="未确认。",
        )

    assert ObjectStoreMemoryStore(object_store).staged("scenario", str(staged["id"])) is not None
    assert ObjectStoreMemoryStore(object_store).get("scenario", str(staged["id"])) is None


def test_memory_publication_endpoint_publishes_staging_atom_and_rejects_bad_requests(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    staged = _stage_atom(object_store)
    use_case = PublishStagingAtomToMemory(object_store)
    endpoint = ServeMemoryPublicationEndpoint()

    wrong_method = endpoint.execute(
        method="GET",
        path=f"/api/rebuild/staging-atoms/{staged['id']}/publication",
        body=None,
        publish_atom=use_case.execute,
    )
    missing_confirm = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/staging-atoms/{staged['id']}/publication",
        body={"confirm": False, "reason": "未确认。"},
        publish_atom=use_case.execute,
    )
    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/staging-atoms/{staged['id']}/publication",
        body={"confirm": True, "reason": "用户确认发布。"},
        publish_atom=use_case.execute,
    )
    repeated = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/staging-atoms/{staged['id']}/publication",
        body={"confirm": True, "reason": "不能重复发布。"},
        publish_atom=use_case.execute,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert missing_confirm.status_code == 400
    assert missing_confirm.body["reason"] == "memory publication requires confirm=true"
    assert response.status_code == 200
    assert response.body["status"] == "published"
    assert response.body["published_object_id"] == staged["id"]
    assert response.body["memory_publication_state"] == "published_with_rollback_ref"
    assert repeated.status_code == 404
    assert repeated.body["reason"] == "staging atom not found"


def test_memory_publication_endpoint_publishes_layered_staging_memory(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    staged = _stage_scenario(object_store)
    publish_atom = PublishStagingAtomToMemory(object_store)
    publish_memory = PublishStagingMemoryToMemory(object_store)
    endpoint = ServeMemoryPublicationEndpoint()

    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/staging-scenarios/{staged['id']}/publication",
        body={"confirm": True, "reason": "用户确认发布场景记忆。"},
        publish_atom=publish_atom.execute,
        publish_memory=publish_memory.execute,
    )
    published = ObjectStoreMemoryStore(object_store).get("scenario", str(staged["id"]))

    assert response.status_code == 200
    assert response.body["status"] == "published"
    assert response.body["layer"] == "scenario"
    assert response.body["published_object_id"] == staged["id"]
    assert response.body["published_ref"] == f"crp://default/memory/scenario/{staged['id']}.json"
    assert published is not None
    assert published["trust_status"] == "user_confirmed"


def test_memory_publication_endpoint_rolls_back_published_atom_and_rejects_bad_requests(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    staged = _stage_atom(object_store)
    publisher = PublishStagingAtomToMemory(object_store)
    rollback = RollbackPublishedAtomMemory(object_store)
    endpoint = ServeMemoryPublicationEndpoint()
    published = publisher.execute(
        atom_id=str(staged["id"]),
        confirm=True,
        reason="用户确认发布。",
    )

    wrong_method = endpoint.execute(
        method="GET",
        path=f"/api/rebuild/memory-publications/{published.publication_id}/rollback",
        body=None,
        publish_atom=publisher.execute,
        rollback_publication=rollback.execute,
    )
    missing_confirm = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/memory-publications/{published.publication_id}/rollback",
        body={"confirm": False, "reason": "未确认。"},
        publish_atom=publisher.execute,
        rollback_publication=rollback.execute,
    )
    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/memory-publications/{published.publication_id}/rollback",
        body={"confirm": True, "reason": "用户撤回长期记忆。"},
        publish_atom=publisher.execute,
        rollback_publication=rollback.execute,
    )
    repeated = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/memory-publications/{published.publication_id}/rollback",
        body={"confirm": True, "reason": "不能重复撤回。"},
        publish_atom=publisher.execute,
        rollback_publication=rollback.execute,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert missing_confirm.status_code == 400
    assert missing_confirm.body["reason"] == "memory rollback requires confirm=true"
    assert response.status_code == 200
    assert response.body["status"] == "rolled_back"
    assert response.body["object_id"] == staged["id"]
    assert response.body["memory_publication_state"] == "rolled_back_not_published"
    assert repeated.status_code == 400
    assert repeated.body["reason"] == "memory publication is not published"


def test_memory_publication_endpoint_rolls_back_layered_memory(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    staged = _stage_series_memory(object_store)
    publisher = PublishStagingMemoryToMemory(object_store)
    rollback = RollbackPublishedMemory(object_store)
    endpoint = ServeMemoryPublicationEndpoint()
    published = publisher.execute(
        object_id=str(staged["id"]),
        layer="series_memory",
        confirm=True,
        reason="用户确认发布系列记忆。",
    )

    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/memory-publications/{published.publication_id}/rollback",
        body={"confirm": True, "reason": "用户撤回系列记忆。"},
        publish_atom=PublishStagingAtomToMemory(object_store).execute,
        rollback_publication=rollback.execute,
    )

    assert response.status_code == 200
    assert response.body["status"] == "rolled_back"
    assert response.body["layer"] == "series_memory"
    assert response.body["object_id"] == staged["id"]
    assert ObjectStoreMemoryStore(object_store).get("series_memory", str(staged["id"])) is None


def test_memory_publication_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    staged = _stage_atom(object_store, atom_id="atom-draft-composed-publication")
    use_case = build_memory_publication(ROOT, runtime_root=tmp_path)

    result = use_case.execute(
        atom_id=str(staged["id"]),
        confirm=True,
        reason="组合入口发布草稿 Atom。",
    )
    published = ObjectStoreMemoryStore(object_store).get("atom", str(staged["id"]))

    assert result.status == "published"
    assert published is not None
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms" / f"{staged['id']}.json").exists()
    assert not (tmp_path / "library").exists()


def test_memory_rollback_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    staged = _stage_atom(object_store, atom_id="atom-draft-composed-rollback")
    published = build_memory_publication(ROOT, runtime_root=tmp_path).execute(
        atom_id=str(staged["id"]),
        confirm=True,
        reason="组合入口发布草稿 Atom。",
    )
    result = build_memory_rollback(ROOT, runtime_root=tmp_path).execute(
        publication_id=published.publication_id,
        confirm=True,
        reason="组合入口回滚长期记忆。",
    )

    assert result.status == "rolled_back"
    assert ObjectStoreMemoryStore(object_store).get("atom", str(staged["id"])) is None
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms" / f"{staged['id']}.json").exists()
    assert not (tmp_path / "library").exists()
