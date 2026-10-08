from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.storage_provider import (
    InventoryCollection,
    JsonObjectStoreInventory,
    MigrationRecord,
    ObjectStorePathError,
    SQLiteMigrationLedger,
    read_json_object_store_collection,
)


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_COLLECTIONS = (
    "project_skill_index",
    "project_skill_json",
    "project_skill_markdown",
    "project_skill_revisions",
    "project_skills",
)


class ProjectSkillMigrationInventoryError(ValueError):
    """Raised when a Project Skill inventory cannot be planned safely."""


@dataclass(frozen=True, slots=True)
class ProjectSkillInventoryIssue:
    code: str
    collection: str
    object_id: str
    project_id: str | None = None
    skill_id: str | None = None
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectSkillMigrationInventory:
    inventory: JsonObjectStoreInventory
    issues: tuple[ProjectSkillInventoryIssue, ...]


def scan_project_skill_migration_inventory(
    object_store_root: Path,
    *,
    namespace_id: str,
) -> ProjectSkillMigrationInventory:
    _require_segment("namespace_id", namespace_id)
    namespace_root = object_store_root.expanduser().resolve(strict=False) / "objects" / namespace_id
    records: dict[str, dict[str, dict[str, object]]] = {}
    collections: list[InventoryCollection] = []
    for collection in _COLLECTIONS:
        collection_records, collection_inventory = _read_collection(
            namespace_root,
            collection,
        )
        records[collection] = collection_records
        collections.append(collection_inventory)
    ordered = tuple(collections)
    inventory = JsonObjectStoreInventory(
        namespace_id=namespace_id,
        collections=ordered,
        object_count=sum(item.object_count for item in ordered),
        fingerprint=_inventory_fingerprint(namespace_id, ordered),
    )
    issues = _project_skill_issues(records)
    return ProjectSkillMigrationInventory(inventory, issues)


def plan_project_skill_migration_dry_run(
    *,
    ledger: SQLiteMigrationLedger,
    migration_id: str,
    target_schema_version: int,
    inventory: ProjectSkillMigrationInventory,
    rollback_pointer: str,
) -> MigrationRecord:
    if inventory.issues:
        codes = ", ".join(sorted({issue.code for issue in inventory.issues}))
        raise ProjectSkillMigrationInventoryError(
            f"Project Skill inventory is inconsistent: {codes}"
        )
    return ledger.plan_dry_run(
        migration_id=migration_id,
        target_schema_version=target_schema_version,
        inventory=inventory.inventory,
        rollback_pointer=rollback_pointer,
    )


def _read_collection(
    namespace_root: Path,
    collection: str,
) -> tuple[dict[str, dict[str, object]], InventoryCollection]:
    directory = namespace_root / collection
    records: dict[str, dict[str, object]] = {}
    pairs: list[tuple[str, str]] = []
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise ProjectSkillMigrationInventoryError(
                "Project Skill inventory collection root must be a directory"
            )
        for path in directory.iterdir():
            if path.name.endswith(".meta.json"):
                if path.is_symlink() or not path.is_file():
                    raise ProjectSkillMigrationInventoryError(
                        "Project Skill inventory metadata entry is invalid"
                    )
                continue
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise ProjectSkillMigrationInventoryError(
                    "Project Skill inventory contains an unexpected entry"
                )
    try:
        stored = read_json_object_store_collection(
            namespace_root.parent.parent,
            namespace_id=namespace_root.name,
            collection=collection,
        )
    except ObjectStorePathError as exc:
        raise ProjectSkillMigrationInventoryError(str(exc)) from exc
    for record in stored:
        records[record.object_id] = dict(record.payload)
        pairs.append((f"{record.object_id}.json", hashlib.sha256(record.payload_bytes).hexdigest()))
    return records, InventoryCollection(
        collection,
        len(records),
        _fingerprint_parts(f"{name}\0{digest}" for name, digest in pairs),
    )


def _project_skill_issues(
    records: dict[str, dict[str, dict[str, object]]],
) -> tuple[ProjectSkillInventoryIssue, ...]:
    issues: list[ProjectSkillInventoryIssue] = []
    skills = records["project_skills"]
    indices = records["project_skill_index"]
    child_indices = {
        collection: _child_index(collection, records[collection], issues)
        for collection in (
            "project_skill_markdown",
            "project_skill_json",
            "project_skill_revisions",
        )
    }
    current_by_skill: dict[str, tuple[str, int]] = {}

    for skill_id, skill in skills.items():
        project_id = skill.get("project_id")
        revision = _positive_int(skill.get("revision"))
        if skill.get("id") != skill_id or not isinstance(project_id, str) or not project_id:
            issues.append(_issue("skill_identity_invalid", "project_skills", skill_id))
            continue
        if revision is None:
            issues.append(_issue("skill_revision_invalid", "project_skills", skill_id, project_id, skill_id))
            continue
        current_by_skill[skill_id] = (project_id, revision)
        if skill.get("markdown_revision") != revision or skill.get("json_revision") != revision:
            issues.append(_issue("current_revision_pointer_mismatch", "project_skills", skill_id, project_id, skill_id, revision))
        index = indices.get(project_id)
        if index is None:
            issues.append(_issue("project_index_missing", "project_skill_index", project_id, project_id, skill_id))
        elif index.get("project_id") != project_id or index.get("skill_id") != skill_id:
            issues.append(_issue("project_index_mismatch", "project_skill_index", project_id, project_id, skill_id))

        for expected_revision in range(1, revision + 1):
            object_id = f"{skill_id}~r{expected_revision}"
            markdown = child_indices["project_skill_markdown"].get((skill_id, expected_revision))
            structured = child_indices["project_skill_json"].get((skill_id, expected_revision))
            revision_record = child_indices["project_skill_revisions"].get((skill_id, expected_revision))
            if markdown is None:
                issues.append(_issue("skill_markdown_missing", "project_skill_markdown", object_id, project_id, skill_id, expected_revision))
            elif not isinstance(markdown.get("markdown"), str) or markdown.get("project_id") != project_id:
                issues.append(_issue("skill_markdown_invalid", "project_skill_markdown", object_id, project_id, skill_id, expected_revision))
            if structured is None:
                issues.append(_issue("skill_json_missing", "project_skill_json", object_id, project_id, skill_id, expected_revision))
            else:
                value = structured.get("structured")
                if not _structured_matches(value, project_id, skill_id, expected_revision):
                    issues.append(_issue("skill_json_pointer_mismatch", "project_skill_json", object_id, project_id, skill_id, expected_revision))
                elif expected_revision == revision and dict(value) != skill:
                    issues.append(_issue("current_skill_json_mismatch", "project_skill_json", object_id, project_id, skill_id, expected_revision))
            if revision_record is None:
                issues.append(_issue("skill_revision_missing", "project_skill_revisions", object_id, project_id, skill_id, expected_revision))
            else:
                expected_parent = None if expected_revision == 1 else expected_revision - 1
                if revision_record.get("project_id") != project_id or revision_record.get("parent_revision") != expected_parent:
                    issues.append(_issue("skill_revision_chain_mismatch", "project_skill_revisions", object_id, project_id, skill_id, expected_revision))
        issues.extend(_active_gate_issues(skill, project_id, skill_id, revision))

    for project_id, index in indices.items():
        skill_id = index.get("skill_id")
        if index.get("project_id") != project_id or not isinstance(skill_id, str):
            issues.append(_issue("project_index_identity_invalid", "project_skill_index", project_id, project_id))
        elif skill_id not in current_by_skill:
            issues.append(_issue("orphan_project_index", "project_skill_index", project_id, project_id, skill_id))

    for collection, child_index in child_indices.items():
        for (skill_id, revision), payload in child_index.items():
            current = current_by_skill.get(skill_id)
            project_id = payload.get("project_id") if isinstance(payload.get("project_id"), str) else None
            if current is None:
                issues.append(_issue("orphan_skill_child", collection, f"{skill_id}~r{revision}", project_id, skill_id, revision))
            elif revision > current[1]:
                issues.append(_issue("future_skill_child", collection, f"{skill_id}~r{revision}", current[0], skill_id, revision))
    return tuple(sorted(issues, key=_issue_sort_key))


def _child_index(
    collection: str,
    records: dict[str, dict[str, object]],
    issues: list[ProjectSkillInventoryIssue],
) -> dict[tuple[str, int], dict[str, object]]:
    index: dict[tuple[str, int], dict[str, object]] = {}
    for object_id, payload in records.items():
        skill_id = payload.get("skill_id")
        revision = _positive_int(payload.get("revision"))
        if not isinstance(skill_id, str) or not skill_id or revision is None:
            issues.append(_issue("skill_child_identity_invalid", collection, object_id))
            continue
        if object_id != f"{skill_id}~r{revision}":
            issues.append(_issue("skill_child_object_id_mismatch", collection, object_id, skill_id=skill_id, revision=revision))
            continue
        index[(skill_id, revision)] = payload
    return index


def _structured_matches(
    value: object,
    project_id: str,
    skill_id: str,
    revision: int,
) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("id") == skill_id
        and value.get("project_id") == project_id
        and value.get("revision") == revision
        and value.get("markdown_revision") == revision
        and value.get("json_revision") == revision
    )


def _active_gate_issues(
    skill: Mapping[str, object],
    project_id: str,
    skill_id: str,
    revision: int,
) -> list[ProjectSkillInventoryIssue]:
    if skill.get("status") != "active":
        return []
    issue = lambda code: _issue(code, "project_skills", skill_id, project_id, skill_id, revision)
    found: list[ProjectSkillInventoryIssue] = []
    conflict = skill.get("conflict")
    if not isinstance(conflict, Mapping) or conflict.get("status") != "none":
        found.append(issue("active_conflict_unresolved"))
    if skill.get("trust_status") == "imported_unverified":
        found.append(issue("active_trust_unverified"))
    contexts = skill.get("required_context")
    if not isinstance(contexts, list) or any(
        isinstance(item, Mapping) and item.get("stale") is True for item in contexts
    ):
        found.append(issue("active_context_stale"))
    rules = skill.get("update_rules")
    if not isinstance(rules, Mapping) or rules.get("user_edit_policy") != "user_wins":
        found.append(issue("active_user_edit_policy_invalid"))
    return found


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _issue(
    code: str,
    collection: str,
    object_id: str,
    project_id: str | None = None,
    skill_id: str | None = None,
    revision: int | None = None,
) -> ProjectSkillInventoryIssue:
    return ProjectSkillInventoryIssue(code, collection, object_id, project_id, skill_id, revision)


def _issue_sort_key(item: ProjectSkillInventoryIssue):
    return (
        item.code,
        item.project_id or "",
        item.skill_id or "",
        item.revision or 0,
        item.collection,
        item.object_id,
    )


def _inventory_fingerprint(namespace_id: str, collections: tuple[InventoryCollection, ...]) -> str:
    return _fingerprint_parts(
        (
            namespace_id,
            *(f"{item.collection}\0{item.object_count}\0{item.fingerprint}" for item in collections),
        )
    )


def _fingerprint_parts(parts) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _require_segment(label: str, value: str) -> None:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise ProjectSkillMigrationInventoryError(f"{label} must be a safe repository segment")
