"""Read-only migration inventory for Project Skill publication aggregates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .migration_inventory import (
    ProjectSkillInventoryIssue,
    _read_collection,
    scan_project_skill_migration_inventory,
)
from .publication_draft import ProjectSkillPublicationDraftError, validate_project_skill_publication_draft
from core.storage_provider import JsonObjectStoreInventory, InventoryCollection, SQLiteMigrationLedger, MigrationRecord


_PUBLICATION_COLLECTIONS = (
    "staging_project_skills",
    "memory_publications",
    "memory_transitions",
)


@dataclass(frozen=True, slots=True)
class ProjectSkillPublicationFixtureInventory:
    inventory: JsonObjectStoreInventory
    aggregate_issues: tuple[ProjectSkillInventoryIssue, ...]
    issues: tuple[ProjectSkillInventoryIssue, ...]

    @property
    def is_migratable(self) -> bool:
        return not self.issues


def scan_project_skill_publication_fixture_inventory(
    object_store_root: Path, *, namespace_id: str
) -> ProjectSkillPublicationFixtureInventory:
    aggregate = scan_project_skill_migration_inventory(object_store_root, namespace_id=namespace_id)
    namespace_root = object_store_root.expanduser().resolve(strict=False) / "objects" / namespace_id
    extra = {collection: _read_collection(namespace_root, collection) for collection in _PUBLICATION_COLLECTIONS}
    records = {collection: value[0] for collection, value in extra.items()}
    current_skills = _read_collection(namespace_root, "project_skills")[0]
    issues = list(aggregate.issues)
    for object_id, draft in records["staging_project_skills"].items():
        try:
            normalized = validate_project_skill_publication_draft(draft)
        except ProjectSkillPublicationDraftError:
            issues.append(_issue("publication_draft_invalid", "staging_project_skills", object_id))
            continue
        if normalized.get("id") != object_id:
            issues.append(_issue("publication_draft_identity_mismatch", "staging_project_skills", object_id))
    for publication_id, publication in records["memory_publications"].items():
        if publication.get("object_type") != "project_skill":
            continue
        project_id = publication.get("project_id")
        skill_id = publication.get("published_object_id")
        if publication.get("id") != publication_id or not isinstance(project_id, str) or not isinstance(skill_id, str):
            issues.append(_issue("publication_identity_invalid", "memory_publications", publication_id))
            continue
        if publication.get("draft_digest") is None or publication.get("review_ref") is None:
            issues.append(_issue("legacy_direct_publication_orphan", "memory_publications", publication_id, project_id, skill_id))
        current = current_skills.get(skill_id)
        published_revision = publication.get("published_revision")
        if (
            not isinstance(published_revision, int)
            or current is None
            or current.get("revision") != published_revision
            or (publication.get("status") == "published" and current.get("status") != "active")
        ):
            issues.append(_issue("publication_current_revision_mismatch", "memory_publications", publication_id, project_id, skill_id))
        transition_ref = publication.get("transition_ref")
        transition_id = transition_ref.rsplit("/", 1)[-1].removesuffix(".json") if isinstance(transition_ref, str) else ""
        transition = records["memory_transitions"].get(transition_id)
        if transition is None or transition.get("id") != transition_id or transition.get("object_type") != "project_skill" or transition.get("object_id") != skill_id:
            issues.append(_issue("publication_transition_missing_or_mismatch", "memory_publications", publication_id, project_id, skill_id))
    return ProjectSkillPublicationFixtureInventory(
        inventory=_combined_inventory(namespace_id, aggregate.inventory.collections, tuple(value[1] for value in extra.values())),
        aggregate_issues=aggregate.issues,
        issues=tuple(sorted(issues, key=lambda item: (item.code, item.collection, item.object_id))),
    )


def plan_project_skill_publication_migration_dry_run(*, ledger: SQLiteMigrationLedger, migration_id: str, target_schema_version: int, inventory: ProjectSkillPublicationFixtureInventory, rollback_pointer: str) -> MigrationRecord:
    if inventory.issues:
        codes = ", ".join(sorted({issue.code for issue in inventory.issues}))
        raise ValueError(f"Project Skill publication inventory is inconsistent: {codes}")
    return ledger.plan_dry_run(migration_id=migration_id, target_schema_version=target_schema_version, inventory=inventory.inventory, rollback_pointer=rollback_pointer)


def _combined_inventory(namespace_id: str, aggregate: tuple[InventoryCollection, ...], extra: tuple[InventoryCollection, ...]) -> JsonObjectStoreInventory:
    collections = tuple(sorted((*aggregate, *extra), key=lambda item: item.collection))
    digest = hashlib.sha256()
    digest.update(namespace_id.encode("utf-8"))
    digest.update(b"\n")
    for item in collections:
        digest.update(
            f"{item.collection}\0{item.object_count}\0{item.fingerprint}".encode("utf-8")
        )
        digest.update(b"\n")
    return JsonObjectStoreInventory(namespace_id, collections, sum(item.object_count for item in collections), digest.hexdigest())


def _issue(code: str, collection: str, object_id: str, project_id: str | None = None, skill_id: str | None = None) -> ProjectSkillInventoryIssue:
    return ProjectSkillInventoryIssue(code, collection, object_id, project_id, skill_id)
