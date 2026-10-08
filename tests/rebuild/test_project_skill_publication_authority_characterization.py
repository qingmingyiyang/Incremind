from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.memory_core import ObjectStoreMemoryStore
from core.product_core.memory_publication import (
    MemoryPublicationError,
    PublishStagingMemoryToMemory,
)
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _staged_project_skill() -> dict[str, object]:
    fixture = ROOT / "core-contracts" / "rebuild" / "fixtures" / "project_skill" / "valid-active-skill.json"
    skill = json.loads(fixture.read_text(encoding="utf-8"))
    skill.update(
        {
            "id": "skill-project-publication-beta",
            "project_id": "project-publication-beta",
            "revision": 1,
            "markdown_revision": 1,
            "json_revision": 1,
            "status": "draft",
            "trust_status": "system_generated",
        }
    )
    return skill


def test_direct_project_skill_publication_without_canonical_context_is_quarantined(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    staged = _staged_project_skill()
    skill_id = str(staged["id"])
    ObjectStoreMemoryStore(object_store).save_candidate("project_skill", staged)

    with pytest.raises(MemoryPublicationError, match="canonical manual publication context"):
        PublishStagingMemoryToMemory(object_store, now="2026-07-12T10:00:00+08:00").execute(
            object_id=skill_id,
            layer="project_skill",
            confirm=True,
            reason="Direct Project Skill publication must remain quarantined.",
        )

    assert object_store.read("project_skills", skill_id) is None
    assert object_store.list("memory_publications") == ()
    assert object_store.list("memory_transitions") == ()
