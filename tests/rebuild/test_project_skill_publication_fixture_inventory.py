from __future__ import annotations

import json
from pathlib import Path

from core.project_skill_core import (
    ObjectStoreProjectSkillRepository,
    ProjectSkillPublicationTarget,
    ProjectSkillUpdate,
    build_project_skill_publication_draft,
    scan_project_skill_publication_fixture_inventory,
)
from core.storage_provider import JsonObjectStore


def test_publication_inventory_rejects_legacy_direct_skill_without_aggregate_or_audit(tmp_path: Path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    store.write(
        "project_skills",
        "skill-direct-legacy",
        {"id": "skill-direct-legacy", "project_id": "project-legacy", "revision": 1},
        expected_revision=0,
    )

    inventory = scan_project_skill_publication_fixture_inventory(
        tmp_path / ".rebuild-data", namespace_id="default"
    )

    assert inventory.is_migratable is False
    assert "project_index_missing" in {issue.code for issue in inventory.issues}


def _canonical_publication_fixture(tmp_path: Path) -> tuple[JsonObjectStore, str]:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    structured = json.loads(
        (Path(__file__).resolve().parents[2] / "core-contracts" / "rebuild" / "fixtures" / "project_skill" / "valid-active-skill.json").read_text(encoding="utf-8")
    )
    skill = ObjectStoreProjectSkillRepository(store, now="2026-07-12T13:00:00+08:00").save(
        ProjectSkillUpdate("project-alpha", "# Alpha\n\nSkill", structured, 0, "fixture")
    )
    draft = build_project_skill_publication_draft(
        target=ProjectSkillPublicationTarget.for_create("project-alpha"),
        source_candidate_id="candidate-alpha-001",
        proposed_content="沿用既有结构并保留来源。",
        source_refs=({"source_id": "source-alpha", "locator": "char:0-64"},),
        evidence_refs=({"source_id": "source-alpha", "locator": "char:0-64"},),
        reviewed_by="user",
        reviewed_at="2026-07-12T12:00:00+08:00",
        review_reason="用户确认发布。",
    )
    publication_id = f"memory-publication-project-skill-{draft['id']}"
    transition_id = f"transition-project-skill-publication-{draft['id']}"
    store.write("memory_transitions", transition_id, {"id": transition_id, "object_type": "project_skill", "object_id": skill["id"]}, expected_revision=0)
    store.write("memory_publications", publication_id, {"id": publication_id, "object_type": "project_skill", "project_id": "project-alpha", "published_object_id": skill["id"], "published_revision": 1, "status": "published", "draft_digest": draft["draft_digest"], "review_ref": draft["review_ref"], "transition_ref": f"crp://default/memory-transitions/{transition_id}.json"}, expected_revision=0)
    return store, publication_id


def test_publication_inventory_accepts_complete_aggregate_and_rejects_transition_drift(tmp_path: Path) -> None:
    store, publication_id = _canonical_publication_fixture(tmp_path)
    root = tmp_path / ".rebuild-data"

    healthy = scan_project_skill_publication_fixture_inventory(root, namespace_id="default")
    assert healthy.is_migratable is True

    publication = store.read("memory_publications", publication_id)
    assert publication is not None
    publication["transition_ref"] = "crp://default/memory-transitions/transition-other.json"
    store.write("memory_publications", publication_id, publication, expected_revision=1)
    drifted = scan_project_skill_publication_fixture_inventory(root, namespace_id="default")
    assert drifted.is_migratable is False
    assert "publication_transition_missing_or_mismatch" in {issue.code for issue in drifted.issues}


def test_publication_inventory_rejects_published_revision_drift(tmp_path: Path) -> None:
    store, publication_id = _canonical_publication_fixture(tmp_path)
    publication = store.read("memory_publications", publication_id)
    assert publication is not None
    publication["published_revision"] = 9
    store.write("memory_publications", publication_id, publication, expected_revision=1)

    inventory = scan_project_skill_publication_fixture_inventory(
        tmp_path / ".rebuild-data", namespace_id="default"
    )

    assert inventory.is_migratable is False
    assert "publication_current_revision_mismatch" in {issue.code for issue in inventory.issues}
