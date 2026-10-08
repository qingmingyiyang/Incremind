"""Fixture-only JSON-to-SQLite Project Skill publication comparison."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.storage_provider import SQLiteStructuredRecordStore

from .migration_inventory import _read_collection
from .sqlite_runtime import SQLiteProjectSkillRepository


_COLLECTIONS = (
    "project_skill_index", "project_skill_json", "project_skill_markdown",
    "project_skill_revisions", "project_skills", "memory_publications",
    "memory_transitions", "staging_project_skills",
)


class ProjectSkillPublicationCompatibilityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectSkillPublicationCompatibilityReport:
    compared_collections: tuple[str, ...]
    object_count: int


def compare_project_skill_publication_fixture(
    *, object_store_root: Path, target_database_path: Path, namespace_id: str
) -> ProjectSkillPublicationCompatibilityReport:
    source_root = object_store_root.expanduser().resolve(strict=False)
    target = target_database_path.expanduser().resolve(strict=False)
    if not target.exists():
        raise ProjectSkillPublicationCompatibilityError("Project Skill publication target is missing")
    namespace_root = source_root / "objects" / namespace_id
    source = {collection: _read_collection(namespace_root, collection)[0] for collection in _COLLECTIONS}
    records = SQLiteStructuredRecordStore(target)
    for collection, expected in source.items():
        actual = {record.object_id: dict(record.payload) for record in records.list(collection)}
        if actual != expected:
            raise ProjectSkillPublicationCompatibilityError(f"Project Skill publication compatibility mismatch: {collection}")
    sqlite = SQLiteProjectSkillRepository(records)
    for project_id, index in source["project_skill_index"].items():
        skill_id = index.get("skill_id")
        if not isinstance(skill_id, str) or sqlite.load(project_id) != source["project_skills"].get(skill_id):
            raise ProjectSkillPublicationCompatibilityError(f"Project Skill current read mismatch: {project_id}")
    return ProjectSkillPublicationCompatibilityReport(_COLLECTIONS, sum(len(items) for items in source.values()))
