from __future__ import annotations

import pytest

from core.product_core.related_memory import (
    RelatedMemoryError,
    RelatedMemoryHit,
    RelatedMemoryQuery,
    RelatedMemoryService,
)
from core.storage_provider import JsonObjectStore


# ── 测试夹具 ──

def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _atom(
    atom_id: str,
    *,
    source_id: str = "source-001",
    series_id: str = "",
    title: str = "",
    summary: str = "",
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": atom_id,
        "layer": "atom",
        "source_id": source_id,
        "series_id": series_id,
        "title": title,
        "summary": summary,
        "source_refs": [{"source_id": source_id, "locator": "char:0-80"}],
        "trust_status": "user_confirmed",
    }


def _scenario(
    scenario_id: str,
    *,
    atom_ids: list[str] | None = None,
    series_id: str = "",
    title: str = "",
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": scenario_id,
        "layer": "scenario",
        "atom_ids": atom_ids or [],
        "series_id": series_id,
        "title": title,
        "source_refs": [],
        "trust_status": "user_confirmed",
    }


def _series_memory(
    sm_id: str,
    *,
    series_id: str = "series-001",
    project_ids: list[str] | None = None,
    scenario_ids: list[str] | None = None,
    title: str = "",
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": sm_id,
        "layer": "series_memory",
        "series_id": series_id,
        "project_ids": project_ids or [],
        "scenario_ids": scenario_ids or [],
        "title": title,
        "source_refs": [],
        "trust_status": "user_confirmed",
    }


def _project_skill(
    skill_id: str,
    *,
    project_id: str = "project-001",
    required_context: list[dict[str, object]] | None = None,
    title: str = "",
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": skill_id,
        "layer": "project_skill",
        "project_id": project_id,
        "required_context": required_context or [],
        "title": title,
        "source_refs": [],
        "trust_status": "user_confirmed",
    }


def _persona(
    persona_id: str = "persona-001",
    *,
    evidence_refs: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": persona_id,
        "scope": "global",
        "evidence_refs": evidence_refs or [],
        "statements": [],
        "revision": 1,
        "trust_status": "user_confirmed",
    }


# ── 基础校验 ──

def test_query_rejects_empty_object_id(tmp_path) -> None:
    service = RelatedMemoryService(_store(tmp_path))
    with pytest.raises(RelatedMemoryError, match="object_id is required"):
        service.query(RelatedMemoryQuery(object_id="", layer="atom"))


def test_query_rejects_unsupported_layer(tmp_path) -> None:
    service = RelatedMemoryService(_store(tmp_path))
    with pytest.raises(RelatedMemoryError, match="unsupported layer"):
        service.query(RelatedMemoryQuery(object_id="x", layer="unknown"))


def test_query_returns_empty_when_seed_missing(tmp_path) -> None:
    service = RelatedMemoryService(_store(tmp_path))
    hits = service.query(RelatedMemoryQuery(object_id="missing-001", layer="atom"))
    assert hits == ()


# ── atom 关联 ──

def test_atom_finds_same_source_atoms(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_atoms", "atom-a", _atom("atom-a", source_id="source-shared"), expected_revision=None)
    store.write("memory_atoms", "atom-b", _atom("atom-b", source_id="source-shared", title="同源 B"), expected_revision=None)
    store.write("memory_atoms", "atom-c", _atom("atom-c", source_id="source-other"), expected_revision=None)

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="atom-a", layer="atom"))
    object_ids = [h.object_id for h in hits]
    assert "atom-b" in object_ids
    assert "atom-c" not in object_ids
    same_source_hit = next(h for h in hits if h.object_id == "atom-b")
    assert same_source_hit.relation == "same_source"
    assert same_source_hit.layer == "atom"


def test_atom_finds_containing_scenario(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_atoms", "atom-a", _atom("atom-a"), expected_revision=None)
    store.write(
        "memory_scenarios",
        "scenario-1",
        _scenario("scenario-1", atom_ids=["atom-a"], title="包含场景"),
        expected_revision=None,
    )

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="atom-a", layer="atom"))
    scenario_hit = next(h for h in hits if h.object_id == "scenario-1")
    assert scenario_hit.layer == "scenario"
    assert scenario_hit.relation == "contains"


def test_atom_finds_series_memory_via_series_id(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_atoms", "atom-a", _atom("atom-a", series_id="series-001"), expected_revision=None)
    store.write(
        "memory_series_memory",
        "sm-1",
        _series_memory("sm-1", series_id="series-001", title="系列记忆"),
        expected_revision=None,
    )

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="atom-a", layer="atom"))
    sm_hit = next(h for h in hits if h.object_id == "sm-1")
    assert sm_hit.layer == "series_memory"
    assert sm_hit.relation == "same_series"


# ── scenario 关联 ──

def test_scenario_finds_same_series_scenarios(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_scenarios", "scen-a", _scenario("scen-a", series_id="series-shared"), expected_revision=None)
    store.write("memory_scenarios", "scen-b", _scenario("scen-b", series_id="series-shared"), expected_revision=None)
    store.write("memory_scenarios", "scen-c", _scenario("scen-c", series_id="series-other"), expected_revision=None)

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="scen-a", layer="scenario"))
    object_ids = [h.object_id for h in hits]
    assert "scen-b" in object_ids
    assert "scen-c" not in object_ids


def test_scenario_finds_contained_atoms(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_atoms", "atom-x", _atom("atom-x", source_id="source-scen"), expected_revision=None)
    store.write("memory_scenarios", "scen-a", _scenario("scen-a", atom_ids=["atom-x"]), expected_revision=None)

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="scen-a", layer="scenario"))
    atom_hit = next(h for h in hits if h.object_id == "atom-x")
    assert atom_hit.layer == "atom"
    assert atom_hit.relation == "contained_in"


# ── series_memory 关联 ──

def test_series_memory_finds_same_project(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_series_memory", "sm-a", _series_memory("sm-a", project_ids=["project-shared"]), expected_revision=None)
    store.write("memory_series_memory", "sm-b", _series_memory("sm-b", project_ids=["project-shared"]), expected_revision=None)
    store.write("memory_series_memory", "sm-c", _series_memory("sm-c", project_ids=["project-other"]), expected_revision=None)

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="sm-a", layer="series_memory"))
    object_ids = [h.object_id for h in hits]
    assert "sm-b" in object_ids
    assert "sm-c" not in object_ids


def test_series_memory_finds_contained_scenarios(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_scenarios", "scen-1", _scenario("scen-1"), expected_revision=None)
    store.write("memory_series_memory", "sm-1", _series_memory("sm-1", scenario_ids=["scen-1"]), expected_revision=None)

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="sm-1", layer="series_memory"))
    scen_hit = next(h for h in hits if h.object_id == "scen-1")
    assert scen_hit.layer == "scenario"
    assert scen_hit.relation == "contained_in"


# ── project_skill 关联 ──

def test_project_skill_finds_same_project(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("project_skills", "skill-a", _project_skill("skill-a", project_id="proj-shared"), expected_revision=None)
    store.write("project_skills", "skill-b", _project_skill("skill-b", project_id="proj-shared"), expected_revision=None)
    store.write("project_skills", "skill-c", _project_skill("skill-c", project_id="proj-other"), expected_revision=None)

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="skill-a", layer="project_skill"))
    object_ids = [h.object_id for h in hits]
    assert "skill-b" in object_ids
    assert "skill-c" not in object_ids


def test_project_skill_finds_required_context_evidence(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_atoms", "atom-ctx", _atom("atom-ctx", title="被引用的 atom"), expected_revision=None)
    store.write(
        "project_skills",
        "skill-a",
        _project_skill(
            "skill-a",
            required_context=[{"kind": "atom", "object_id": "atom-ctx"}],
        ),
        expected_revision=None,
    )

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="skill-a", layer="project_skill"))
    atom_hit = next(h for h in hits if h.object_id == "atom-ctx")
    assert atom_hit.layer == "atom"
    assert atom_hit.relation == "evidence"


# ── persona 关联 ──

def test_persona_finds_evidence_refs(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_atoms", "atom-ev", _atom("atom-ev", title="Persona 证据"), expected_revision=None)
    store.write(
        "memory_persona",
        "persona-001",
        _persona(
            "persona-001",
            evidence_refs=[{"object_type": "atom", "object_id": "atom-ev", "source_refs": []}],
        ),
        expected_revision=None,
    )

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="persona-001", layer="persona"))
    atom_hit = next(h for h in hits if h.object_id == "atom-ev")
    assert atom_hit.layer == "atom"
    assert atom_hit.relation == "evidence"


def test_persona_skips_unsupported_object_types(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(
        "memory_persona",
        "persona-001",
        _persona(
            "persona-001",
            evidence_refs=[
                {"object_type": "source", "object_id": "source-001", "source_refs": []},
                {"object_type": "document", "object_id": "doc-001", "source_refs": []},
            ],
        ),
        expected_revision=None,
    )

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="persona-001", layer="persona"))
    assert hits == ()


# ── 去重与 limit ──

def test_query_deduplicates_by_object_id(tmp_path) -> None:
    store = _store(tmp_path)
    # atom-a 与 atom-b 同源；同时 atom-b 被 scenario-1 包含
    # 查询 atom-a 时，scenario-1 应只出现一次（虽然包含关系只触发一次，但测试去重）
    store.write("memory_atoms", "atom-a", _atom("atom-a", source_id="source-shared"), expected_revision=None)
    store.write("memory_atoms", "atom-b", _atom("atom-b", source_id="source-shared"), expected_revision=None)
    store.write("memory_scenarios", "scen-1", _scenario("scen-1", atom_ids=["atom-a", "atom-b"]), expected_revision=None)

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="atom-a", layer="atom"))
    object_ids = [h.object_id for h in hits]
    assert len(object_ids) == len(set(object_ids))  # 无重复
    assert "atom-b" in object_ids
    assert "scen-1" in object_ids


def test_query_excludes_self(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_atoms", "atom-a", _atom("atom-a", source_id="source-shared"), expected_revision=None)

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="atom-a", layer="atom"))
    assert all(h.object_id != "atom-a" for h in hits)


def test_query_respects_limit(tmp_path) -> None:
    store = _store(tmp_path)
    # 创建 5 个同源 atom
    for i in range(5):
        store.write(
            "memory_atoms",
            f"atom-{i}",
            _atom(f"atom-{i}", source_id="source-shared"),
            expected_revision=None,
        )

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="atom-0", layer="atom", limit=3))
    assert len(hits) <= 3


def test_query_limit_clamped_to_max_20(tmp_path) -> None:
    store = _store(tmp_path)
    for i in range(25):
        store.write(
            "memory_atoms",
            f"atom-{i:02d}",
            _atom(f"atom-{i:02d}", source_id="source-shared"),
            expected_revision=None,
        )

    service = RelatedMemoryService(store)
    hits = service.query(RelatedMemoryQuery(object_id="atom-00", layer="atom", limit=100))
    assert len(hits) <= 20


# ── to_payload ──

def test_hit_to_payload_serializes_fields(tmp_path) -> None:
    hit = RelatedMemoryHit(
        object_id="atom-001",
        layer="atom",
        title="测试标题",
        summary="测试摘要",
        relation="same_source",
        source_refs=({"source_id": "source-001"},),
    )
    payload = hit.to_payload()
    assert payload["object_id"] == "atom-001"
    assert payload["layer"] == "atom"
    assert payload["title"] == "测试标题"
    assert payload["summary"] == "测试摘要"
    assert payload["relation"] == "same_source"
    assert payload["source_refs"] == [{"source_id": "source-001"}]


# ── RelatedMemoryQuery ──

def test_query_supported_layer_returns_true_for_valid_layers() -> None:
    for layer in ("atom", "scenario", "series_memory", "project_skill", "persona"):
        q = RelatedMemoryQuery(object_id="x", layer=layer)
        assert q.supported_layer() is True


def test_query_supported_layer_returns_false_for_invalid_layer() -> None:
    q = RelatedMemoryQuery(object_id="x", layer="unknown")
    assert q.supported_layer() is False
