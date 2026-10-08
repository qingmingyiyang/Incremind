from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Sequence

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource


DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
EXPECTED_REQUIRED = {
    "question_response.schema.json": {
        "schema_version",
        "status",
        "answer",
        "evidence_items",
        "source_links",
        "error",
        "privacy",
    },
    "answer_feedback.schema.json": {
        "schema_version",
        "id",
        "project_id",
        "recall_result_id",
        "model_request_id",
        "model_result_id",
        "document_id",
        "memory_candidate_id",
        "candidate_status",
        "feedback_type",
        "source_refs",
        "review",
        "input_refs",
        "created_at",
    },
    "asset.schema.json": {
        "schema_version",
        "id",
        "source_id",
        "uri",
        "content_hash",
        "size_bytes",
        "media_type",
        "storage_mode",
        "availability",
        "availability_reason",
        "created_at",
        "verified_at",
        "metadata",
    },
    "source.schema.json": {
        "schema_version",
        "id",
        "type",
        "title",
        "capture_mode",
        "storage_uri",
        "original_url",
        "content_hash",
        "media_type",
        "size_bytes",
        "parser_version",
        "processing_state",
        "created_at",
        "imported_from_legacy",
        "trust_status",
        "metadata",
    },
    "storage_namespace.schema.json": {
        "schema_version",
        "namespace_id",
        "storage_version",
        "app_root_uri",
        "root_uri",
        "reference_root_uri",
        "legacy_access",
        "legacy_root_uri",
        "write_policy",
        "backup_policy",
        "migration",
        "created_at",
        "updated_at",
    },
    "atom.schema.json": {
        "schema_version",
        "id",
        "source_id",
        "content",
        "atom_type",
        "tags",
        "confidence",
        "source_refs",
        "revision",
        "created_at",
        "updated_at",
        "trust_status",
    },
    "scenario.schema.json": {
        "schema_version",
        "id",
        "title",
        "summary",
        "atom_ids",
        "source_refs",
        "tags",
        "series_id",
        "project_id",
        "stale",
        "stale_reason",
        "revision",
        "created_at",
        "updated_at",
        "trust_status",
    },
    "persona.schema.json": {
        "schema_version",
        "id",
        "scope",
        "statements",
        "evidence_refs",
        "confirmation",
        "revision",
        "created_at",
        "updated_at",
        "trust_status",
    },
    "series_memory.schema.json": {
        "schema_version",
        "id",
        "series_id",
        "scope",
        "overview",
        "scenario_ids",
        "source_refs",
        "project_ids",
        "stale",
        "stale_reason",
        "revision",
        "created_at",
        "updated_at",
        "trust_status",
    },
    "memory_transition.schema.json": {
        "schema_version",
        "id",
        "object_type",
        "object_id",
        "transition_type",
        "from_trust_status",
        "to_trust_status",
        "from_revision",
        "to_revision",
        "actor",
        "reason",
        "evidence_refs",
        "created_at",
    },
    "memory_candidate.schema.json": {
        "schema_version",
        "id",
        "project_id",
        "target_layer",
        "candidate_type",
        "status",
        "proposed_content",
        "source_refs",
        "provenance",
        "review",
        "created_at",
        "updated_at",
    },
    "model_request.schema.json": {
        "schema_version",
        "id",
        "project_id",
        "capability",
        "provider_preference",
        "payload",
        "privacy",
        "timeout",
        "cancel",
        "budget",
        "response_schema",
        "created_at",
    },
    "model_result.schema.json": {
        "schema_version",
        "id",
        "request_id",
        "status",
        "provider",
        "model",
        "output",
        "usage",
        "latency",
        "error",
        "safety",
        "created_at",
    },
    "project.schema.json": {
        "schema_version",
        "id",
        "name",
        "purpose",
        "status",
        "series_ids",
        "skill_id",
        "document_ids",
        "default_scope",
        "cross_project_policy",
        "revision",
        "created_at",
        "updated_at",
    },
    "project_skill.schema.json": {
        "schema_version",
        "id",
        "project_id",
        "name",
        "purpose",
        "markdown_uri",
        "json_uri",
        "markdown_revision",
        "json_revision",
        "required_context",
        "output_rules",
        "style_preferences",
        "update_rules",
        "source_refs",
        "evidence_refs",
        "decision_log",
        "conflict",
        "revision",
        "status",
        "trust_status",
        "created_at",
        "updated_at",
    },
    "project_skill_publication_draft.schema.json": {
        "id",
        "schema_version",
        "target_layer",
        "source_candidate_id",
        "project_id",
        "skill_id",
        "expected_project_skill_revision",
        "structured_payload",
        "markdown",
        "review_ref",
        "reviewed_by",
        "reviewed_at",
        "review_reason",
        "source_refs",
        "evidence_refs",
        "draft_digest",
        "created_at",
        "updated_at",
    },
    "recall_request.schema.json": {
        "schema_version",
        "id",
        "query",
        "project_id",
        "scope",
        "layers",
        "trust_filter",
        "budget",
        "cross_project",
        "required_context_refs",
        "created_at",
    },
    "recall_result.schema.json": {
        "schema_version",
        "id",
        "request_id",
        "project_id",
        "status",
        "hits",
        "coverage",
        "truncation",
        "explanation",
        "cross_project",
        "errors",
        "created_at",
    },
    "progressive_memory_retrieval_plan.schema.json": {
        "schema_version",
        "plan_version",
        "query_fingerprint",
        "intent",
        "read_order",
        "context_lanes",
        "stages",
        "budget",
        "requires_source_body",
        "allow_cross_series_fallback",
        "rationale_codes",
        "safety",
    },
    "memory_retrieval_projection.schema.json": {
        "schema_version",
        "projection_version",
        "project_id",
        "authority_identity",
        "authority_fingerprint",
        "generated_at",
        "status",
        "derived_from",
        "project_skill_refs",
        "r0_items",
        "r1_items",
        "safety",
    },
    "progressive_recall_shadow_trace.schema.json": {
        "schema_version",
        "trace_version",
        "trace_id",
        "mode",
        "router_policy_version",
        "project_id",
        "query_fingerprint",
        "authority_identity_fingerprint",
        "authority_fingerprint",
        "projection",
        "route",
        "fallback",
        "legacy",
        "comparison",
        "performance",
        "safety",
    },
    "progressive_recall_context_bundle.schema.json": {
        "schema_version",
        "bundle_version",
        "bundle_id",
        "project_id",
        "authority_fingerprint",
        "query_fingerprint",
        "intent",
        "stages_read",
        "items",
        "budget",
        "drop_codes",
        "safety",
    },
    "progressive_recall_drilldown_trace.schema.json": {
        "schema_version",
        "trace_version",
        "trace_id",
        "bundle_id",
        "project_id",
        "authority_fingerprint",
        "query_fingerprint",
        "intent",
        "signals",
        "stages",
        "budget",
        "drop_codes",
        "error_codes",
        "performance",
        "safety",
    },
    "progressive_direct_question_trace.schema.json": {
        "trace_id",
        "schema_version",
        "trace_version",
        "mode",
        "project_id",
        "query_fingerprint",
        "authority_fingerprint",
        "projection",
        "route",
        "layers_read",
        "fallback",
        "rebuild",
        "evidence",
        "safety",
    },
    "progressive_memory_scale_benchmark.schema.json": {
        "schema_version",
        "benchmark_version",
        "profile",
        "metrics",
        "outcome",
        "privacy",
    },
    "team_memory_import_draft.schema.json": {
        "schema_version",
        "id",
        "project_id",
        "target_layer",
        "candidate_type",
        "status",
        "proposed_content",
        "source",
        "authorization",
        "review",
        "safety",
        "created_at",
        "updated_at",
    },
    "team_memory_source_staging.schema.json": {
        "schema_version",
        "id",
        "preview_id",
        "draft_id",
        "draft_revision",
        "project_id",
        "status",
        "origin",
        "proposed_source",
        "difference",
        "receipt",
        "safety",
        "created_at",
        "updated_at",
    },
    "document.schema.json": {
        "schema_version",
        "id",
        "title",
        "type",
        "project_id",
        "markdown_uri",
        "content_hash",
        "source_refs",
        "source_snapshot",
        "blocks",
        "revision",
        "status",
        "created_at",
        "updated_at",
    },
    "document_revision.schema.json": {
        "schema_version",
        "id",
        "document_id",
        "revision",
        "parent_revision",
        "operation",
        "author",
        "reason",
        "base_content_hash",
        "new_content_hash",
        "source_snapshot",
        "changed_blocks",
        "conflict",
        "created_at",
    },
    "job.schema.json": {
        "schema_version",
        "id",
        "source_id",
        "job_type",
        "idempotency_key",
        "status",
        "attempt",
        "max_attempts",
        "lease",
        "progress",
        "steps",
        "error",
        "checkpoint",
        "staged_outputs",
        "published_outputs",
        "log_refs",
        "created_at",
        "updated_at",
    },
    "platform_capability.schema.json": {
        "schema_version",
        "name",
        "platform",
        "adapter_id",
        "available",
        "permission_required",
        "permission",
        "provided_uri",
        "error",
        "checked_at",
    },
    "platform_recovery_action.schema.json": {
        "schema_version",
        "id",
        "capability",
        "severity",
        "status",
        "action_type",
        "title",
        "description",
        "source",
        "evidence",
        "execution",
        "user_decision",
        "resolution",
        "created_at",
        "updated_at",
    },
}
EXPECTED_TRUST_STATUS = [
    "trusted",
    "imported_unverified",
    "user_confirmed",
    "system_generated",
    "failed",
]
EXPECTED_JOB_STATUS = [
    "pending",
    "running",
    "waiting_user",
    "completed",
    "failed",
    "cancelled",
]
EXPECTED_ASSET_STORAGE_MODE = [
    "copy",
    "reference",
]
EXPECTED_ASSET_AVAILABILITY = [
    "available",
    "missing",
    "permission_denied",
    "hash_mismatch",
    "offline",
    "unknown",
]
EXPECTED_LEGACY_ACCESS = [
    "disabled",
    "read_only",
]
EXPECTED_PLATFORM_CAPABILITY_NAMES = [
    "app_data_dir",
    "documents_dir",
    "open_path",
    "reveal_path",
    "file_picker",
    "shortcut",
    "notification",
    "credential_store",
    "worker_lifecycle",
    "system_info",
    "update",
    "backup_destination",
]
EXPECTED_PLATFORM_NAMES = [
    "windows",
    "macos",
    "linux",
    "unknown",
]
EXPECTED_PLATFORM_PERMISSION = [
    "granted",
    "denied",
    "prompt_required",
    "not_applicable",
    "unknown",
]
FIXTURE_REQUIRED_CONTRACTS = {
    "question_response.schema.json": "question_response",
    "answer_feedback.schema.json": "answer_feedback",
    "atom.schema.json": "atom",
    "asset.schema.json": "asset",
    "document.schema.json": "document",
    "document_revision.schema.json": "document_revision",
    "job.schema.json": "job",
    "memory_transition.schema.json": "memory_transition",
    "memory_candidate.schema.json": "memory_candidate",
    "model_request.schema.json": "model_request",
    "model_result.schema.json": "model_result",
    "persona.schema.json": "persona",
    "platform_capability.schema.json": "platform_capability",
    "platform_recovery_action.schema.json": "platform_recovery_action",
    "project.schema.json": "project",
    "project_skill.schema.json": "project_skill",
    "project_skill_publication_draft.schema.json": "project_skill_publication_draft",
    "recall_request.schema.json": "recall_request",
    "recall_result.schema.json": "recall_result",
    "scenario.schema.json": "scenario",
    "series_memory.schema.json": "series_memory",
    "source.schema.json": "source",
    "storage_namespace.schema.json": "storage_namespace",
}


def validate_contracts(contract_root: Path) -> list[str]:
    errors: list[str] = []
    actual = {path.name for path in contract_root.glob("*.schema.json") if path.is_file()}
    expected = set(EXPECTED_REQUIRED)
    registry = load_contract_registry(contract_root)
    for name in sorted(expected - actual):
        errors.append(f"missing contract: {name}")
    for name in sorted(actual - expected):
        errors.append(f"unexpected contract: {name}")
    for name in sorted(expected & actual):
        path = contract_root / name
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            errors.append(f"{name}: invalid JSON: {error}")
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            errors.append(f"{name}: invalid Draft 2020-12 schema: {error.message}")
            continue
        if schema.get("$schema") != DRAFT_2020_12:
            errors.append(f"{name}: expected JSON Schema Draft 2020-12")
        if schema.get("type") != "object":
            errors.append(f"{name}: root type must be object")
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            errors.append(f"{name}: properties must be an object")
            continue
        required = schema.get("required")
        if not isinstance(required, list):
            errors.append(f"{name}: required must be an array")
            continue
        missing_required = EXPECTED_REQUIRED[name] - set(required)
        if missing_required:
            errors.append(f"{name}: missing required fields: {', '.join(sorted(missing_required))}")
        missing_properties = EXPECTED_REQUIRED[name] - set(properties)
        if missing_properties:
            errors.append(f"{name}: missing properties: {', '.join(sorted(missing_properties))}")
        fixture_name = FIXTURE_REQUIRED_CONTRACTS.get(name)
        if fixture_name:
            _validate_fixtures(
                schema=schema,
                fixture_root=contract_root / "fixtures" / fixture_name,
                contract_name=name,
                registry=registry,
                errors=errors,
            )
    if "source.schema.json" in actual:
        _check_enum(
            contract_root / "source.schema.json",
            "trust_status",
            EXPECTED_TRUST_STATUS,
            errors,
        )
    if "asset.schema.json" in actual:
        _check_enum(
            contract_root / "asset.schema.json",
            "storage_mode",
            EXPECTED_ASSET_STORAGE_MODE,
            errors,
        )
        _check_enum(
            contract_root / "asset.schema.json",
            "availability",
            EXPECTED_ASSET_AVAILABILITY,
            errors,
        )
    if "storage_namespace.schema.json" in actual:
        _check_enum(
            contract_root / "storage_namespace.schema.json",
            "legacy_access",
            EXPECTED_LEGACY_ACCESS,
            errors,
        )
    if "job.schema.json" in actual:
        _check_enum(
            contract_root / "job.schema.json",
            "status",
            EXPECTED_JOB_STATUS,
            errors,
        )
    if "platform_capability.schema.json" in actual:
        _check_enum(
            contract_root / "platform_capability.schema.json",
            "name",
            EXPECTED_PLATFORM_CAPABILITY_NAMES,
            errors,
        )
        _check_enum(
            contract_root / "platform_capability.schema.json",
            "platform",
            EXPECTED_PLATFORM_NAMES,
            errors,
        )
        _check_enum(
            contract_root / "platform_capability.schema.json",
            "permission",
            EXPECTED_PLATFORM_PERMISSION,
            errors,
        )
    return errors


def validate_contract_instance(
    contract_name: str,
    schema: dict[str, object],
    instance: object,
    *,
    registry: Registry | None = None,
) -> list[str]:
    """Validate one instance with JSON Schema plus rebuild semantic gates."""

    validator_kwargs: dict[str, object] = {"format_checker": FormatChecker()}
    if registry is not None:
        validator_kwargs["registry"] = registry
    validator = Draft202012Validator(schema, **validator_kwargs)
    errors: list[str] = []
    for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.path)):
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(f"{location}: {error.message}")
    if contract_name == "atom.schema.json":
        errors.extend(_atom_semantic_errors(instance))
    if contract_name == "document.schema.json":
        errors.extend(_document_semantic_errors(instance))
    if contract_name == "document_revision.schema.json":
        errors.extend(_document_revision_semantic_errors(instance))
    if contract_name == "job.schema.json":
        errors.extend(_job_semantic_errors(instance))
    if contract_name == "memory_transition.schema.json":
        errors.extend(_memory_transition_semantic_errors(instance))
    if contract_name == "memory_candidate.schema.json":
        errors.extend(_memory_candidate_semantic_errors(instance))
    if contract_name == "answer_feedback.schema.json":
        errors.extend(_answer_feedback_semantic_errors(instance))
    if contract_name == "model_request.schema.json":
        errors.extend(_model_request_semantic_errors(instance))
    if contract_name == "model_result.schema.json":
        errors.extend(_model_result_semantic_errors(instance))
    if contract_name == "persona.schema.json":
        errors.extend(_persona_semantic_errors(instance))
    if contract_name == "platform_capability.schema.json":
        errors.extend(_platform_capability_semantic_errors(instance))
    if contract_name == "platform_recovery_action.schema.json":
        errors.extend(_platform_recovery_action_semantic_errors(instance))
    if contract_name == "project.schema.json":
        errors.extend(_project_semantic_errors(instance))
    if contract_name == "project_skill.schema.json":
        errors.extend(_project_skill_semantic_errors(instance))
    if contract_name == "project_skill_publication_draft.schema.json":
        errors.extend(_project_skill_publication_draft_semantic_errors(instance))
    if contract_name == "recall_request.schema.json":
        errors.extend(_recall_request_semantic_errors(instance))
    if contract_name == "recall_result.schema.json":
        errors.extend(_recall_result_semantic_errors(instance))
    if contract_name == "scenario.schema.json":
        errors.extend(_stale_object_semantic_errors(instance, "scenario"))
    if contract_name == "series_memory.schema.json":
        errors.extend(_series_memory_semantic_errors(instance))
    if contract_name == "storage_namespace.schema.json":
        errors.extend(_storage_namespace_semantic_errors(instance))
    return errors


def _validate_fixtures(
    *,
    schema: dict[str, object],
    fixture_root: Path,
    contract_name: str,
    registry: Registry,
    errors: list[str],
) -> None:
    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))
    if not valid_paths:
        errors.append(f"{contract_name}: missing valid fixtures in {fixture_root}")
    if not invalid_paths:
        errors.append(f"{contract_name}: missing invalid fixtures in {fixture_root}")
    for path in valid_paths:
        instance = _read_fixture(path, contract_name, errors)
        if instance is None:
            continue
        fixture_errors = validate_contract_instance(contract_name, schema, instance, registry=registry)
        for error in fixture_errors:
            errors.append(f"{contract_name}: valid fixture {path.name} failed: {error}")
    for path in invalid_paths:
        instance = _read_fixture(path, contract_name, errors)
        if instance is None:
            continue
        if not validate_contract_instance(contract_name, schema, instance, registry=registry):
            errors.append(f"{contract_name}: invalid fixture {path.name} was accepted")


def load_contract_registry(contract_root: Path) -> Registry:
    """Load local contract IDs so fixture validation can resolve cross-contract refs."""

    resources: list[tuple[str, Resource]] = []
    for path in sorted(contract_root.glob("*.schema.json")):
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(schema, dict) or schema.get("$schema") != DRAFT_2020_12:
            continue
        resource = Resource.from_contents(schema)
        identifier = resource.id()
        if identifier is not None:
            resources.append((identifier, resource))
    return Registry().with_resources(resources)


def _read_fixture(
    path: Path,
    contract_name: str,
    errors: list[str],
) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"{contract_name}: fixture {path.name} is invalid JSON: {error}")
        return None


def _storage_namespace_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: storage namespace instance must be an object"]
    errors: list[str] = []
    namespace = instance.get("namespace_id")
    if not isinstance(namespace, str):
        return errors
    expected_root = f"crp://{namespace}/"
    expected_reference = f"crp-ref://{namespace}/"
    if instance.get("root_uri") != expected_root:
        errors.append(f"root_uri: must equal {expected_root}")
    if instance.get("reference_root_uri") != expected_reference:
        errors.append(f"reference_root_uri: must equal {expected_reference}")
    backup = instance.get("backup_policy")
    if isinstance(backup, dict):
        expected_backup = f"crp://{namespace}/backups/"
        if backup.get("snapshot_uri_prefix") != expected_backup:
            errors.append(f"backup_policy.snapshot_uri_prefix: must equal {expected_backup}")
    migration = instance.get("migration")
    if isinstance(migration, dict):
        status = migration.get("status")
        manifest = migration.get("manifest_uri")
        rollback = migration.get("rollback_snapshot_uri")
        if status == "idle":
            if manifest is not None:
                errors.append("migration.manifest_uri: idle migration must not have a manifest")
            if rollback is not None:
                errors.append("migration.rollback_snapshot_uri: idle migration must not have a rollback snapshot")
        elif isinstance(status, str):
            if not isinstance(manifest, str):
                errors.append("migration.manifest_uri: non-idle migration requires a manifest")
            elif not manifest.startswith(f"crp://{namespace}/migrations/"):
                errors.append(f"migration.manifest_uri: must use namespace {namespace}")
            if not isinstance(rollback, str):
                errors.append("migration.rollback_snapshot_uri: non-idle migration requires a rollback snapshot")
            elif not rollback.startswith(f"crp://{namespace}/backups/"):
                errors.append(f"migration.rollback_snapshot_uri: must use namespace {namespace}")
    return errors


def _atom_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: atom instance must be an object"]
    errors: list[str] = []
    source_id = instance.get("source_id")
    source_refs = instance.get("source_refs")
    if isinstance(source_id, str) and isinstance(source_refs, list):
        ref_source_ids = {item.get("source_id") for item in source_refs if isinstance(item, dict)}
        if source_id not in ref_source_ids:
            errors.append("source_refs: must include top-level source_id")
    return errors


def _document_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: document instance must be an object"]
    errors: list[str] = []
    errors.extend(_source_snapshot_covers_refs(instance))
    blocks = instance.get("blocks")
    for index, block in _iter_dict_items(blocks):
        errors.extend(_document_block_errors(block, f"blocks.{index}"))
    return errors


def _document_revision_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: document revision instance must be an object"]
    errors: list[str] = []
    revision = instance.get("revision")
    parent_revision = instance.get("parent_revision")
    if isinstance(revision, int):
        if parent_revision is None:
            if revision != 1:
                errors.append("revision: create revision without parent must be 1")
        elif isinstance(parent_revision, int) and revision <= parent_revision:
            errors.append("revision: must be greater than parent_revision")
    operation = instance.get("operation")
    author = instance.get("author")
    if operation == "user_edit" and author != "user":
        errors.append("author: user_edit operation requires user author")
    if operation == "create":
        if parent_revision is not None:
            errors.append("parent_revision: create operation must not have a parent")
        if instance.get("base_content_hash") is not None:
            errors.append("base_content_hash: create operation must not have a base hash")
    else:
        if parent_revision is None:
            errors.append("parent_revision: non-create operation requires a parent")
        if instance.get("base_content_hash") is None:
            errors.append("base_content_hash: non-create operation requires a base hash")
    changed_blocks = instance.get("changed_blocks")
    touches_user_protected = False
    for index, change in _iter_dict_items(changed_blocks):
        block = change.get("block")
        if change.get("operation") == "delete" and block is not None:
            errors.append(f"changed_blocks.{index}.block: delete operation must use null block")
        if change.get("operation") in {"add", "update"} and not isinstance(block, dict):
            errors.append(f"changed_blocks.{index}.block: add/update operation requires a block")
        if isinstance(block, dict):
            errors.extend(_document_block_errors(block, f"changed_blocks.{index}.block"))
            if block.get("edited_by_user") is True or block.get("lock_policy") == "user_edit_protected":
                touches_user_protected = True
    conflict = instance.get("conflict")
    conflict_status = None
    conflict_blocks = None
    resolution = None
    if isinstance(conflict, dict):
        conflict_status = conflict.get("status")
        conflict_blocks = conflict.get("conflict_blocks")
        resolution = conflict.get("resolution")
        if conflict_status == "none":
            if isinstance(conflict_blocks, list) and conflict_blocks:
                errors.append("conflict.conflict_blocks: none conflict must not list blocks")
            if resolution is not None:
                errors.append("conflict.resolution: none conflict must not have resolution")
        elif conflict_status in {"detected", "resolved"}:
            if not isinstance(conflict_blocks, list) or not conflict_blocks:
                errors.append("conflict.conflict_blocks: conflict requires affected blocks")
            if conflict_status == "resolved" and not isinstance(resolution, str):
                errors.append("conflict.resolution: resolved conflict requires resolution")
    if operation == "ai_patch" and touches_user_protected and conflict_status == "none":
        errors.append("conflict.status: ai_patch touching user protected blocks requires conflict")
    return errors


def _document_block_errors(block: dict[str, object], location: str) -> list[str]:
    errors: list[str] = []
    if block.get("edited_by_user") is True and block.get("lock_policy") != "user_edit_protected":
        errors.append(f"{location}.lock_policy: user edited block must be user_edit_protected")
    if block.get("origin") == "ai":
        source_refs = block.get("source_refs")
        if not isinstance(source_refs, list) or not source_refs:
            errors.append(f"{location}.source_refs: ai block requires source refs")
        if block.get("lock_policy") == "none":
            errors.append(f"{location}.lock_policy: ai block requires source_required or explicit protection")
    return errors


def _source_snapshot_covers_refs(instance: dict[str, object]) -> list[str]:
    errors: list[str] = []
    source_refs = instance.get("source_refs")
    snapshot = instance.get("source_snapshot")
    if not isinstance(source_refs, list) or not isinstance(snapshot, dict):
        return errors
    snapshot_refs = snapshot.get("source_refs")
    if not isinstance(snapshot_refs, list):
        return errors
    document_source_ids = {item.get("source_id") for item in source_refs if isinstance(item, dict)}
    snapshot_source_ids = {item.get("source_id") for item in snapshot_refs if isinstance(item, dict)}
    missing = sorted(str(source_id) for source_id in document_source_ids - snapshot_source_ids)
    if missing:
        errors.append(f"source_snapshot.source_refs: missing document sources {', '.join(missing)}")
    return errors


def _stale_object_semantic_errors(instance: object, object_name: str) -> list[str]:
    if not isinstance(instance, dict):
        return [f"<root>: {object_name} instance must be an object"]
    errors: list[str] = []
    stale = instance.get("stale")
    stale_reason = instance.get("stale_reason")
    if stale is True and not isinstance(stale_reason, str):
        errors.append("stale_reason: stale object requires a reason")
    if stale is False and stale_reason is not None:
        errors.append("stale_reason: non-stale object must not have a stale reason")
    return errors


def _persona_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: persona instance must be an object"]
    errors: list[str] = []
    if instance.get("trust_status") == "imported_unverified":
        errors.append("trust_status: imported_unverified cannot enter L3 persona")
    confirmation = instance.get("confirmation")
    if isinstance(confirmation, dict):
        status = confirmation.get("status")
        actor = confirmation.get("actor")
        reason = confirmation.get("reason")
        if status in {"confirmed", "rejected", "rule_allowed"}:
            if not isinstance(actor, str):
                errors.append("confirmation.actor: finalized confirmation requires an actor")
            if not isinstance(reason, str):
                errors.append("confirmation.reason: finalized confirmation requires a reason")
        if instance.get("trust_status") == "user_confirmed" and status != "confirmed":
            errors.append("confirmation.status: user_confirmed persona requires confirmed status")
    return errors


def _series_memory_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: series memory instance must be an object"]
    errors = _stale_object_semantic_errors(instance, "series_memory")
    if instance.get("trust_status") == "imported_unverified":
        errors.append("trust_status: imported_unverified cannot enter L3 series memory")
    if instance.get("scope") == "cross_project":
        project_ids = instance.get("project_ids")
        if not isinstance(project_ids, list) or not project_ids:
            errors.append("project_ids: cross_project series memory requires project_ids")
    return errors


def _project_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: project instance must be an object"]
    errors: list[str] = []
    status = instance.get("status")
    skill_id = instance.get("skill_id")
    series_ids = instance.get("series_ids")
    if status == "active":
        if not isinstance(skill_id, str) or not skill_id:
            errors.append("skill_id: active project requires a skill")
        if not isinstance(series_ids, list) or not series_ids:
            errors.append("series_ids: active project requires at least one series")
    cross_project_policy = instance.get("cross_project_policy")
    if isinstance(cross_project_policy, dict):
        mode = cross_project_policy.get("mode")
        grant_ids = cross_project_policy.get("grant_ids")
        if mode == "disabled" and isinstance(grant_ids, list) and grant_ids:
            errors.append("cross_project_policy.grant_ids: disabled cross-project policy must not include grants")
        if mode == "single_grant_required" and grant_ids is not None and not isinstance(grant_ids, list):
            errors.append("cross_project_policy.grant_ids: single grant policy requires a grant list")
    return errors


def _project_skill_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: project skill instance must be an object"]
    errors: list[str] = []
    revision = instance.get("revision")
    markdown_revision = instance.get("markdown_revision")
    json_revision = instance.get("json_revision")
    if isinstance(revision, int):
        if markdown_revision != revision:
            errors.append("markdown_revision: must equal revision")
        if json_revision != revision:
            errors.append("json_revision: must equal revision")
    project_id = instance.get("project_id")
    if isinstance(project_id, str):
        expected_markdown = f"/projects/{project_id}/project-skill.md"
        expected_json = f"/projects/{project_id}/project-skill.json"
        markdown_uri = instance.get("markdown_uri")
        json_uri = instance.get("json_uri")
        if isinstance(markdown_uri, str) and not markdown_uri.endswith(expected_markdown):
            errors.append(f"markdown_uri: must end with {expected_markdown}")
        if isinstance(json_uri, str) and not json_uri.endswith(expected_json):
            errors.append(f"json_uri: must end with {expected_json}")
    status = instance.get("status")
    trust_status = instance.get("trust_status")
    if status == "active" and trust_status == "imported_unverified":
        errors.append("trust_status: imported_unverified project skill cannot be active")
    required_context = instance.get("required_context")
    if status == "active":
        for index, context in _iter_dict_items(required_context):
            if context.get("stale") is True:
                errors.append(f"required_context.{index}.stale: active project skill cannot require stale context")
    output_rules = instance.get("output_rules")
    for index, rule in _iter_dict_items(output_rules):
        source_refs = rule.get("source_refs")
        if rule.get("origin") == "ai" and (not isinstance(source_refs, list) or not source_refs):
            errors.append(f"output_rules.{index}.source_refs: ai output rule requires source refs")
        if rule.get("locked_by_user") is True and rule.get("origin") != "user":
            errors.append(f"output_rules.{index}.origin: user locked rule must have user origin")
    update_rules = instance.get("update_rules")
    if isinstance(update_rules, dict):
        user_edit_policy = update_rules.get("user_edit_policy")
        patch_strategy = update_rules.get("patch_strategy")
        if status == "active" and user_edit_policy != "user_wins":
            errors.append("update_rules.user_edit_policy: active project skill must preserve user edits")
        if patch_strategy == "rewrite_requires_confirmation" and user_edit_policy != "conflict_required":
            errors.append("update_rules.user_edit_policy: rewrite strategy requires conflict_required")
    conflict = instance.get("conflict")
    if isinstance(conflict, dict):
        conflict_status = conflict.get("status")
        conflict_refs = conflict.get("conflict_refs")
        resolution = conflict.get("resolution")
        if conflict_status == "none":
            if isinstance(conflict_refs, list) and conflict_refs:
                errors.append("conflict.conflict_refs: none conflict must not list refs")
            if resolution is not None:
                errors.append("conflict.resolution: none conflict must not have resolution")
        if conflict_status == "detected":
            if status != "conflicted":
                errors.append("status: detected conflict requires conflicted status")
            if not isinstance(conflict_refs, list) or not conflict_refs:
                errors.append("conflict.conflict_refs: detected conflict requires refs")
        if conflict_status == "resolved" and not isinstance(resolution, str):
                errors.append("conflict.resolution: resolved conflict requires resolution")
    return errors


def _project_skill_publication_draft_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: project skill publication draft must be an object"]
    errors: list[str] = []
    structured = instance.get("structured_payload")
    if not isinstance(structured, dict):
        return errors
    project_id = instance.get("project_id")
    skill_id = instance.get("skill_id")
    expected_revision = instance.get("expected_project_skill_revision")
    if structured.get("project_id") != project_id:
        errors.append("structured_payload.project_id: must equal project_id")
    if structured.get("id") != skill_id:
        errors.append("structured_payload.id: must equal skill_id")
    if isinstance(expected_revision, int) and not isinstance(expected_revision, bool):
        revision = expected_revision + 1
        if expected_revision == 0 and skill_id != f"skill-{project_id}":
            errors.append("skill_id: create draft must use the canonical skill-{project_id} identity")
        for field in ("revision", "markdown_revision", "json_revision"):
            if structured.get(field) != revision:
                errors.append(f"structured_payload.{field}: must equal expected_project_skill_revision + 1")
    if structured.get("status") != "draft":
        errors.append("structured_payload.status: publication draft must remain draft")
    for field in ("source_refs", "evidence_refs"):
        if structured.get(field) != instance.get(field):
            errors.append(f"structured_payload.{field}: must equal {field}")
    reviewed_at = instance.get("reviewed_at")
    for field in ("created_at", "updated_at"):
        if instance.get(field) != reviewed_at:
            errors.append(f"{field}: must equal reviewed_at")
        if structured.get(field) != reviewed_at:
            errors.append(f"structured_payload.{field}: must equal reviewed_at")
    candidate_id = instance.get("source_candidate_id")
    review_ref = instance.get("review_ref")
    if isinstance(candidate_id, str) and isinstance(review_ref, str):
        if not review_ref.endswith(f"/memory-candidates/{candidate_id}.json#review"):
            errors.append("review_ref: must point to source_candidate_id review")
    name = structured.get("name")
    purpose = structured.get("purpose")
    if isinstance(name, str) and isinstance(purpose, str):
        if instance.get("markdown") != f"# {name}\n\n{purpose}\n":
            errors.append("markdown: must be the reviewed structured name and purpose")
    try:
        material = {
            key: value
            for key, value in instance.items()
            if key not in {"id", "draft_digest"}
        }
        digest = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError):
        errors.append("draft: must be JSON serializable for digest validation")
        return errors
    if instance.get("draft_digest") != digest:
        errors.append("draft_digest: must cover the complete draft material")
    if instance.get("id") != f"project-skill-publication-draft-{digest[:24]}":
        errors.append("id: must be derived from draft_digest")
    return errors


def _recall_request_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: recall request instance must be an object"]
    errors: list[str] = []
    layers = instance.get("layers")
    if isinstance(layers, list):
        starts_with_project_skill = bool(layers) and layers[0] == "l3_project_skill"
        starts_with_persona_then_skill = (
            len(layers) >= 2
            and layers[0] == "l4_persona"
            and layers[1] == "l3_project_skill"
        )
        if not (starts_with_project_skill or starts_with_persona_then_skill):
            errors.append(
                "layers: recall must start with l4_persona then l3_project_skill, "
                "or l3_project_skill when no Persona is requested"
            )
    required_context_refs = instance.get("required_context_refs")
    if isinstance(required_context_refs, list):
        has_project_skill_context = any(
            isinstance(item, dict) and item.get("kind") == "project_skill"
            for item in required_context_refs
        )
        if not has_project_skill_context:
            errors.append("required_context_refs: recall request requires project_skill context")
    trust_filter = instance.get("trust_filter")
    if isinstance(trust_filter, dict):
        include = trust_filter.get("include")
        allow_imported = trust_filter.get("allow_imported_unverified")
        if isinstance(include, list) and "imported_unverified" in include and allow_imported is not True:
            errors.append("trust_filter.allow_imported_unverified: imported_unverified requires explicit allow")
    budget = instance.get("budget")
    if isinstance(budget, dict):
        max_hits = budget.get("max_hits")
        max_tokens = budget.get("max_tokens")
        if isinstance(max_hits, int) and max_hits > 12:
            errors.append("budget.max_hits: must be less than or equal to 12")
        if isinstance(max_tokens, int) and max_tokens > 12000:
            errors.append("budget.max_tokens: must be less than or equal to 12000")
    scope = instance.get("scope")
    cross_project = instance.get("cross_project")
    if isinstance(cross_project, dict):
        allowed = cross_project.get("allowed")
        grant_id = cross_project.get("grant_id")
        project_ids = cross_project.get("project_ids")
        has_projects = isinstance(project_ids, list) and bool(project_ids)
        if scope == "cross_project":
            if allowed is not True:
                errors.append("cross_project.allowed: cross_project scope requires explicit allowance")
            if not isinstance(grant_id, str):
                errors.append("cross_project.grant_id: cross_project scope requires a grant")
            if not has_projects:
                errors.append("cross_project.project_ids: cross_project scope requires target projects")
        else:
            if allowed is not False:
                errors.append("cross_project.allowed: project scope must keep cross-project disabled")
            if grant_id is not None:
                errors.append("cross_project.grant_id: project scope must not include a grant")
            if has_projects:
                errors.append("cross_project.project_ids: project scope must not include target projects")
    return errors


def _recall_result_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: recall result instance must be an object"]
    errors: list[str] = []
    status = instance.get("status")
    hits = instance.get("hits")
    hit_count = len(hits) if isinstance(hits, list) else 0
    if status == "evidence_found" and hit_count == 0:
        errors.append("hits: evidence_found result requires at least one hit")
    if status == "insufficient_evidence":
        if hit_count != 0:
            errors.append("hits: insufficient_evidence result must not include hits")
        if not _has_error_code(instance, "insufficient_evidence"):
            errors.append("errors: insufficient_evidence result requires insufficient_evidence error")
    if status == "index_unavailable" and not _has_error_code(instance, "index_unavailable"):
        errors.append("errors: index_unavailable result requires index_unavailable error")

    coverage = instance.get("coverage")
    covered_layers: set[str] = set()
    requested_layers: set[str] = set()
    if isinstance(coverage, dict):
        coverage_status = coverage.get("status")
        requested_layers = _string_set(coverage.get("requested_layers"))
        covered_layers = _string_set(coverage.get("covered_layers"))
        missing_layers = _string_set(coverage.get("missing_layers"))
        if not covered_layers.issubset(requested_layers):
            errors.append("coverage.covered_layers: must be a subset of requested_layers")
        if not missing_layers.issubset(requested_layers):
            errors.append("coverage.missing_layers: must be a subset of requested_layers")
        if status == "evidence_found" and coverage_status == "insufficient":
            errors.append("coverage.status: evidence_found result cannot be insufficient")
        source_ref_count = coverage.get("source_ref_count")
        actual_source_refs = _count_recall_source_refs(hits)
        if isinstance(source_ref_count, int) and source_ref_count != actual_source_refs:
            errors.append("coverage.source_ref_count: must equal hit source ref count")

    truncation = instance.get("truncation")
    if isinstance(truncation, dict):
        applied = truncation.get("applied")
        reason = truncation.get("reason")
        dropped_hit_ids = truncation.get("dropped_hit_ids")
        final_hit_count = truncation.get("final_hit_count")
        final_token_estimate = truncation.get("final_token_estimate")
        if isinstance(final_hit_count, int) and final_hit_count != hit_count:
            errors.append("truncation.final_hit_count: must equal returned hit count")
        if isinstance(final_token_estimate, int) and isinstance(hits, list):
            actual_tokens = sum(
                item.get("token_estimate")
                for item in hits
                if isinstance(item, dict) and isinstance(item.get("token_estimate"), int)
            )
            if final_token_estimate != actual_tokens:
                errors.append("truncation.final_token_estimate: must equal returned hit token estimate")
        has_drops = isinstance(dropped_hit_ids, list) and bool(dropped_hit_ids)
        if applied is False:
            if reason != "none":
                errors.append("truncation.reason: non-truncated result must use none")
            if has_drops:
                errors.append("truncation.dropped_hit_ids: non-truncated result must not include drops")
        if applied is True:
            if reason == "none":
                errors.append("truncation.reason: truncated result requires a non-none reason")
            if not has_drops:
                errors.append("truncation.dropped_hit_ids: truncated result requires dropped hit ids")

    project_id = instance.get("project_id")
    cross_project = instance.get("cross_project")
    cross_used = False
    cross_project_ids: set[str] = set()
    if isinstance(cross_project, dict):
        cross_used = cross_project.get("used") is True
        grant_id = cross_project.get("grant_id")
        project_ids = cross_project.get("project_ids")
        cross_project_ids = _string_set(project_ids)
        if cross_used:
            if not isinstance(grant_id, str):
                errors.append("cross_project.grant_id: cross-project result requires a grant")
            if not cross_project_ids:
                errors.append("cross_project.project_ids: cross-project result requires project ids")
        else:
            if grant_id is not None:
                errors.append("cross_project.grant_id: project-local result must not include a grant")
            if cross_project_ids:
                errors.append("cross_project.project_ids: project-local result must not include project ids")

    for index, hit in _iter_dict_items(hits):
        layer = hit.get("layer")
        if isinstance(layer, str) and covered_layers and layer not in covered_layers:
            errors.append(f"hits.{index}.layer: hit layer must be listed in coverage.covered_layers")
        hit_project_id = hit.get("project_id")
        if isinstance(project_id, str) and isinstance(hit_project_id, str) and hit_project_id != project_id:
            if not cross_used:
                errors.append(f"hits.{index}.project_id: cross-project hit requires cross_project.used")
            elif hit_project_id not in cross_project_ids:
                errors.append(f"hits.{index}.project_id: cross-project hit must be covered by grant project ids")
            if not isinstance(hit.get("source_project_label"), str):
                errors.append(f"hits.{index}.source_project_label: cross-project hit requires source project label")
    return errors


def _has_error_code(instance: dict[str, object], code: str) -> bool:
    errors = instance.get("errors")
    return any(isinstance(item, dict) and item.get("code") == code for item in _list_items(errors))


def _count_recall_source_refs(hits: object) -> int:
    count = 0
    for _, hit in _iter_dict_items(hits):
        source_refs = hit.get("source_refs")
        if isinstance(source_refs, list):
            count += len(source_refs)
    return count


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def _list_items(value: object) -> list[object]:
    if not isinstance(value, list):
        return []
    return value


def _model_request_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: model request instance must be an object"]
    errors: list[str] = []
    provider = instance.get("provider_preference")
    privacy = instance.get("privacy")
    payload = instance.get("payload")
    budget = instance.get("budget")
    cancel = instance.get("cancel")
    response_schema = instance.get("response_schema")
    capability = instance.get("capability")

    provider_mode = None
    provider_allow_remote = None
    if isinstance(provider, dict):
        provider_mode = provider.get("mode")
        provider_allow_remote = provider.get("allow_remote")
        provider_id = provider.get("provider")
        model = provider.get("model")
        if provider_mode == "local_only":
            if provider_allow_remote is not False:
                errors.append("provider_preference.allow_remote: local_only mode must disable remote")
        if provider_mode == "remote_allowed":
            if provider_allow_remote is not True:
                errors.append("provider_preference.allow_remote: remote_allowed mode must allow remote")
            if not isinstance(provider_id, str):
                errors.append("provider_preference.provider: remote_allowed mode requires a provider")
            if not isinstance(model, str):
                errors.append("provider_preference.model: remote_allowed mode requires a model")

    privacy_scope = None
    privacy_allow_remote = None
    if isinstance(privacy, dict):
        privacy_scope = privacy.get("scope")
        privacy_allow_remote = privacy.get("allow_remote")
        pii = privacy.get("pii")
        retention = privacy.get("retention")
        redaction = privacy.get("redaction")
        redaction_applied = None
        redaction_strategy = None
        if isinstance(redaction, dict):
            redaction_applied = redaction.get("applied")
            redaction_strategy = redaction.get("strategy")
            if redaction_applied is False and redaction_strategy not in {None, "none"}:
                errors.append("privacy.redaction.strategy: unapplied redaction must use none")
            if redaction_applied is True and redaction_strategy in {None, "none"}:
                errors.append("privacy.redaction.strategy: applied redaction requires a strategy")
        if privacy_scope == "local_only":
            if privacy_allow_remote is not False:
                errors.append("privacy.allow_remote: local_only scope must disable remote")
            if retention != "none":
                errors.append("privacy.retention: local_only scope must not retain provider logs")
        if privacy_allow_remote is True and provider_allow_remote is not True:
            errors.append("privacy.allow_remote: remote privacy requires provider remote allowance")
        if provider_allow_remote is True and privacy_allow_remote is not True:
            errors.append("provider_preference.allow_remote: remote provider requires privacy remote allowance")
        if pii in {"possible", "present"} and privacy_allow_remote is True and redaction_applied is not True:
            errors.append("privacy.redaction.applied: remote request with pii requires redaction")
        if retention == "provider_default" and privacy_allow_remote is not True:
            errors.append("privacy.retention: provider_default requires remote allowance")

    if privacy_scope == "local_only" and provider_mode == "remote_allowed":
        errors.append("provider_preference.mode: local_only privacy cannot use remote_allowed mode")

    if isinstance(payload, dict):
        kind = payload.get("kind")
        source_refs = payload.get("source_refs")
        recall_result_id = payload.get("recall_result_id")
        if kind in {"answer", "document_draft", "memory_extraction"}:
            if not isinstance(source_refs, list) or not source_refs:
                errors.append("payload.source_refs: generation and extraction requests require source refs")
        if kind == "answer" and not isinstance(recall_result_id, str):
            errors.append("payload.recall_result_id: answer request requires recall result")

    if isinstance(cancel, dict):
        cancellable = cancel.get("cancellable")
        cancel_token = cancel.get("cancel_token")
        requested = cancel.get("requested")
        if cancellable is True and not isinstance(cancel_token, str):
            errors.append("cancel.cancel_token: cancellable request requires token")
        if requested is True and cancellable is not True:
            errors.append("cancel.cancellable: requested cancellation requires cancellable=true")
        if requested is True and not isinstance(cancel_token, str):
            errors.append("cancel.cancel_token: requested cancellation requires token")
        if cancellable is False and cancel_token is not None:
            errors.append("cancel.cancel_token: non-cancellable request must not include token")

    if isinstance(budget, dict):
        max_input = budget.get("max_input_tokens")
        max_output = budget.get("max_output_tokens")
        max_total = budget.get("max_total_tokens")
        max_cost = budget.get("max_cost_usd")
        if isinstance(max_input, int) and isinstance(max_output, int) and isinstance(max_total, int):
            if max_input + max_output > max_total:
                errors.append("budget.max_total_tokens: must cover input plus output token budgets")
        if provider_allow_remote is True and not isinstance(max_cost, (int, float)):
            errors.append("budget.max_cost_usd: remote requests require a cost budget")
        if provider_allow_remote is False and max_cost not in {0, 0.0, None}:
            errors.append("budget.max_cost_usd: local-only requests must not reserve remote cost")

    if isinstance(response_schema, dict):
        response_type = response_schema.get("type")
        json_schema_uri = response_schema.get("json_schema_uri")
        strict = response_schema.get("strict")
        if capability == "structured_generation":
            if response_type != "json":
                errors.append("response_schema.type: structured_generation requires json response")
            if not isinstance(json_schema_uri, str):
                errors.append("response_schema.json_schema_uri: structured_generation requires schema uri")
            if strict is not True:
                errors.append("response_schema.strict: structured_generation requires strict schema")
        if capability == "embedding" and response_type != "embedding":
            errors.append("response_schema.type: embedding capability requires embedding response")
        if capability == "transcription" and response_type != "transcript":
            errors.append("response_schema.type: transcription capability requires transcript response")
    return errors


def _model_result_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: model result instance must be an object"]
    errors: list[str] = []
    status = instance.get("status")
    provider = instance.get("provider")
    model = instance.get("model")
    output = instance.get("output")
    usage = instance.get("usage")
    latency = instance.get("latency")
    error = instance.get("error")
    safety = instance.get("safety")

    if isinstance(provider, dict):
        mode = provider.get("mode")
        remote = provider.get("remote")
        provider_id = provider.get("provider_id")
        config_version = provider.get("config_version")
        if mode == "remote" and remote is not True:
            errors.append("provider.remote: remote provider mode requires remote=true")
        if mode == "local" and remote is not False:
            errors.append("provider.remote: local provider mode requires remote=false")
        if mode in {"local", "remote"}:
            if not isinstance(provider_id, str):
                errors.append("provider.provider_id: local/remote provider requires id")
            if not isinstance(config_version, int):
                errors.append("provider.config_version: local/remote provider requires config version")
        if mode == "none":
            if provider_id is not None:
                errors.append("provider.provider_id: none provider must not include id")
            if config_version is not None:
                errors.append("provider.config_version: none provider must not include config version")

    if isinstance(model, dict):
        capability = model.get("capability")
        if capability == "none":
            if model.get("name") is not None:
                errors.append("model.name: none capability must not include model name")
            if model.get("version") is not None:
                errors.append("model.version: none capability must not include model version")

    if isinstance(usage, dict):
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")
        cost_usd = usage.get("cost_usd")
        currency = usage.get("currency")
        if isinstance(input_tokens, int) and isinstance(output_tokens, int) and isinstance(total_tokens, int):
            if input_tokens + output_tokens != total_tokens:
                errors.append("usage.total_tokens: must equal input_tokens plus output_tokens")
        if currency == "none" and cost_usd != 0:
            errors.append("usage.cost_usd: currency none requires zero cost")
        if isinstance(cost_usd, (int, float)) and cost_usd > 0 and currency != "USD":
            errors.append("usage.currency: positive cost requires USD")

    has_output = False
    if isinstance(output, dict):
        kind = output.get("kind")
        content = output.get("content")
        structured = output.get("structured")
        output_refs = output.get("output_refs")
        has_output = kind != "none" and (content is not None or structured is not None)
        if kind == "none":
            if content is not None:
                errors.append("output.content: none output must not include content")
            if structured is not None:
                errors.append("output.structured: none output must not include structured data")
            if isinstance(output_refs, list) and output_refs:
                errors.append("output.output_refs: none output must not include refs")

    timed_out = None
    if isinstance(latency, dict):
        timed_out = latency.get("timed_out")
        completed_at = latency.get("completed_at")
        if status == "completed":
            if completed_at is None:
                errors.append("latency.completed_at: completed result requires completed_at")
            if timed_out is not False:
                errors.append("latency.timed_out: completed result must not be timed out")
        if status == "timeout" and timed_out is not True:
            errors.append("latency.timed_out: timeout result requires timed_out=true")

    error_code = error.get("code") if isinstance(error, dict) else None
    if status == "completed":
        if error is not None:
            errors.append("error: completed result must not include error")
        if not has_output:
            errors.append("output: completed result requires output content or structured data")
    else:
        if not isinstance(error, dict):
            errors.append("error: non-completed result requires error")
        if has_output:
            errors.append("output: non-completed result must not include generated output")
    expected_error_codes = {
        "cancelled": {"cancelled"},
        "timeout": {"timeout"},
        "provider_error": {"provider_unavailable", "provider_error"},
        "privacy_blocked": {"privacy_blocked"},
        "budget_exceeded": {"budget_exceeded"},
        "invalid_response": {"invalid_response", "schema_validation_failed"},
        "safety_blocked": {"safety_blocked"},
    }
    if status in expected_error_codes and error_code not in expected_error_codes[status]:
        errors.append(f"error.code: {status} result requires matching error code")

    if status == "privacy_blocked":
        if isinstance(provider, dict) and provider.get("mode") != "none":
            errors.append("provider.mode: privacy_blocked result must not reach provider")
        if isinstance(usage, dict) and usage.get("total_tokens") != 0:
            errors.append("usage.total_tokens: privacy_blocked result must not spend tokens")

    if isinstance(safety, dict):
        blocked = safety.get("blocked")
        categories = safety.get("categories")
        if blocked is True:
            if status not in {"privacy_blocked", "safety_blocked"}:
                errors.append("status: blocked safety requires privacy_blocked or safety_blocked status")
            if not isinstance(categories, list) or not categories:
                errors.append("safety.categories: blocked result requires categories")
        if blocked is False and status == "safety_blocked":
            errors.append("safety.blocked: safety_blocked status requires blocked=true")
    return errors


def _memory_transition_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: memory transition instance must be an object"]
    errors: list[str] = []
    from_revision = instance.get("from_revision")
    to_revision = instance.get("to_revision")
    if isinstance(from_revision, int) and isinstance(to_revision, int) and to_revision <= from_revision:
        errors.append("to_revision: must be greater than from_revision")
    object_type = instance.get("object_type")
    actor = instance.get("actor")
    from_trust = instance.get("from_trust_status")
    to_trust = instance.get("to_trust_status")
    if object_type in {"persona", "series_memory", "project_skill"}:
        if from_trust == "imported_unverified" and to_trust in {"user_confirmed", "trusted"} and actor != "user":
            errors.append("actor: imported_unverified L3 promotion requires user actor")
    if to_trust == "user_confirmed" and actor != "user":
        errors.append("actor: user_confirmed transition requires user actor")
    return errors


def _memory_candidate_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: memory candidate instance must be an object"]
    errors: list[str] = []
    status = instance.get("status")
    provenance = instance.get("provenance")
    review = instance.get("review")
    if isinstance(provenance, dict):
        model_result_id = provenance.get("model_result_id")
        model_request_id = provenance.get("model_request_id")
        recall_result_id = provenance.get("recall_result_id")
        document_id = provenance.get("document_id")
        document_revision = provenance.get("document_revision")
        source_content_read_id = provenance.get("source_content_read_id")
        media_processing_output_id = provenance.get("media_processing_output_id")
        media_processing_job_id = provenance.get("media_processing_job_id")
        if document_id is None and document_revision is not None:
            errors.append("provenance.document_revision: document revision requires document_id")
        if isinstance(document_id, str) and not isinstance(document_revision, int):
            errors.append("provenance.document_revision: document_id requires document revision")
        if media_processing_output_id is None and media_processing_job_id is not None:
            errors.append("provenance.media_processing_job_id: media processing job requires output")
        if isinstance(media_processing_output_id, str) and not isinstance(media_processing_job_id, str):
            errors.append("provenance.media_processing_job_id: media processing output requires job")
        model_provenance_complete = (
            isinstance(model_result_id, str)
            and isinstance(model_request_id, str)
            and isinstance(recall_result_id, str)
        )
        document_provenance_complete = isinstance(document_id, str) and isinstance(document_revision, int)
        content_read_provenance_complete = isinstance(source_content_read_id, str)
        media_output_provenance_complete = isinstance(media_processing_output_id, str) and isinstance(
            media_processing_job_id, str
        )
        if not (
            model_provenance_complete
            or document_provenance_complete
            or content_read_provenance_complete
            or media_output_provenance_complete
        ):
            errors.append("provenance: memory candidate requires model, document, or source output provenance")
        input_refs = provenance.get("input_refs")
        kinds: set[object] = set()
        if isinstance(input_refs, list):
            kinds = {ref.get("kind") for ref in input_refs if isinstance(ref, dict)}
        if model_provenance_complete:
            for required_kind in ("recall_result", "model_request", "model_result"):
                if required_kind not in kinds:
                    errors.append(f"provenance.input_refs: must include {required_kind}")
        if document_provenance_complete and "document" not in kinds:
            errors.append("provenance.input_refs: must include document")
        if content_read_provenance_complete:
            for required_kind in ("source", "source_content_read"):
                if required_kind not in kinds:
                    errors.append(f"provenance.input_refs: must include {required_kind}")
        if media_output_provenance_complete:
            for required_kind in ("source", "media_processing_job", "media_processing_output"):
                if required_kind not in kinds:
                    errors.append(f"provenance.input_refs: must include {required_kind}")
    if isinstance(review, dict):
        reviewed_by = review.get("reviewed_by")
        reviewed_at = review.get("reviewed_at")
        auto_promote = review.get("auto_promote_allowed")
        requires_user = review.get("requires_user_confirmation")
        if status == "pending_review":
            if reviewed_by is not None:
                errors.append("review.reviewed_by: pending candidate must not be reviewed")
            if reviewed_at is not None:
                errors.append("review.reviewed_at: pending candidate must not have reviewed_at")
            if auto_promote is not False:
                errors.append("review.auto_promote_allowed: pending Model Result candidate cannot auto-promote")
            if requires_user is not True:
                errors.append("review.requires_user_confirmation: pending Model Result candidate requires user confirmation")
        if status in {"rejected", "promoted"}:
            if reviewed_by not in {"user", "system"}:
                errors.append(f"review.reviewed_by: {status} candidate requires reviewer")
            if not isinstance(reviewed_at, str):
                errors.append(f"review.reviewed_at: {status} candidate requires reviewed_at")
        if status == "promoted" and requires_user is True and reviewed_by != "user":
            errors.append("review.reviewed_by: user-confirmed candidate promotion requires user reviewer")
    return errors


def _answer_feedback_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: answer feedback instance must be an object"]
    errors: list[str] = []
    candidate_status = instance.get("candidate_status")
    feedback_type = instance.get("feedback_type")
    if candidate_status == "promoted" and feedback_type != "candidate_promoted_to_draft":
        errors.append("feedback_type: promoted candidate requires candidate_promoted_to_draft")
    if candidate_status == "rejected" and feedback_type != "candidate_rejected":
        errors.append("feedback_type: rejected candidate requires candidate_rejected")
    review = instance.get("review")
    if isinstance(review, dict):
        if candidate_status == "promoted" and review.get("reviewed_by") != "user":
            errors.append("review.reviewed_by: promoted answer feedback requires user reviewer")
    input_refs = instance.get("input_refs")
    kinds: set[object] = set()
    if isinstance(input_refs, list):
        kinds = {ref.get("kind") for ref in input_refs if isinstance(ref, dict)}
    for required_kind in ("recall_result", "model_request", "model_result", "memory_candidate"):
        if required_kind not in kinds:
            errors.append(f"input_refs: must include {required_kind}")
    if instance.get("document_id") is not None and "document" not in kinds:
        errors.append("input_refs: document feedback requires document ref")
    return errors


def _job_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: job instance must be an object"]
    errors: list[str] = []
    status = instance.get("status")
    attempt = instance.get("attempt")
    max_attempts = instance.get("max_attempts")
    if isinstance(attempt, int) and isinstance(max_attempts, int) and attempt > max_attempts:
        errors.append("attempt: must be less than or equal to max_attempts")
    progress = instance.get("progress")
    if isinstance(progress, dict):
        current = progress.get("current")
        total = progress.get("total")
        percent = progress.get("percent")
        if isinstance(current, int) and isinstance(total, int) and current > total:
            errors.append("progress.current: must be less than or equal to progress.total")
        if status == "completed" and percent != 100:
            errors.append("progress.percent: completed job must be 100")
    lease = instance.get("lease")
    if status == "running" and lease is None:
        errors.append("lease: running job requires a lease")
    if status in {"pending", "completed", "failed", "cancelled"} and lease is not None:
        errors.append(f"lease: {status} job must not hold a lease")
    error = instance.get("error")
    if status == "failed" and error is None:
        errors.append("error: failed job requires an error")
    if status in {"pending", "running", "waiting_user", "completed", "cancelled"} and error is not None:
        errors.append(f"error: {status} job must not have an error")
    staged_outputs = instance.get("staged_outputs")
    published_outputs = instance.get("published_outputs")
    if status == "completed":
        if isinstance(staged_outputs, list) and staged_outputs:
            errors.append("staged_outputs: completed job must not keep staged outputs")
        if isinstance(published_outputs, list) and not published_outputs:
            errors.append("published_outputs: completed job requires published outputs")
    elif isinstance(published_outputs, list) and published_outputs:
        errors.append("published_outputs: non-completed job must not publish outputs")
    for index, output in _iter_dict_items(staged_outputs):
        if output.get("published") is not False:
            errors.append(f"staged_outputs.{index}.published: staged output must be false")
    for index, output in _iter_dict_items(published_outputs):
        if output.get("published") is not True:
            errors.append(f"published_outputs.{index}.published: published output must be true")
    steps = instance.get("steps")
    for index, step in _iter_dict_items(steps):
        step_status = step.get("status")
        started_at = step.get("started_at")
        completed_at = step.get("completed_at")
        step_error = step.get("error")
        if step_status == "running":
            if not isinstance(started_at, str):
                errors.append(f"steps.{index}.started_at: running step requires started_at")
            if completed_at is not None:
                errors.append(f"steps.{index}.completed_at: running step must not be completed")
            if step_error is not None:
                errors.append(f"steps.{index}.error: running step must not have an error")
        if step_status in {"completed", "failed", "cancelled"} and not isinstance(completed_at, str):
            errors.append(f"steps.{index}.completed_at: {step_status} step requires completed_at")
        if step_status == "failed" and step_error is None:
            errors.append(f"steps.{index}.error: failed step requires an error")
        if step_status in {"pending", "completed", "skipped"} and step_error is not None:
            errors.append(f"steps.{index}.error: {step_status} step must not have an error")
    return errors


def _platform_capability_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: platform capability instance must be an object"]
    errors: list[str] = []
    name = instance.get("name")
    platform = instance.get("platform")
    available = instance.get("available")
    permission_required = instance.get("permission_required")
    permission = instance.get("permission")
    provided_uri = instance.get("provided_uri")
    error = instance.get("error")
    if available is True:
        if error is not None:
            errors.append("error: available capability must not have an error")
        if permission not in {"granted", "not_applicable"}:
            errors.append("permission: available capability requires granted or not_applicable permission")
        if platform == "unknown":
            errors.append("platform: unknown platform cannot provide an available capability")
    if available is False and error is None:
        errors.append("error: unavailable capability requires an error")
    if permission_required is True and permission == "not_applicable":
        errors.append("permission: permission_required capability cannot use not_applicable")
    if permission_required is False and permission in {"denied", "prompt_required"}:
        errors.append("permission: non-permission capability cannot be denied or prompt_required")
    path_capabilities = {"app_data_dir", "documents_dir", "backup_destination"}
    if available is True and name in path_capabilities and not isinstance(provided_uri, str):
        errors.append("provided_uri: available path capability requires a platform URI")
    if name not in path_capabilities and provided_uri is not None:
        errors.append("provided_uri: non-path capability must not provide a URI")
    for location, value in _iter_strings(instance):
        if _looks_like_os_path(value):
            errors.append(f"{location}: must not contain an OS path or file URI")
    return errors


def _platform_recovery_action_semantic_errors(instance: object) -> list[str]:
    if not isinstance(instance, dict):
        return ["<root>: platform recovery action instance must be an object"]
    errors: list[str] = []
    capability = instance.get("capability")
    evidence = instance.get("evidence")
    if isinstance(capability, str) and isinstance(evidence, dict):
        degraded = evidence.get("degraded_capabilities")
        missing = evidence.get("missing_capabilities")
        leaks = evidence.get("os_path_leaks")
        evidence_capabilities: set[str] = set()
        for value in (degraded, missing, leaks):
            if isinstance(value, list):
                evidence_capabilities.update(item for item in value if isinstance(item, str))
        if capability not in evidence_capabilities:
            errors.append("evidence: must include recovery action capability")
    execution = instance.get("execution")
    if isinstance(execution, dict):
        if execution.get("auto_execute") is not False:
            errors.append("execution.auto_execute: platform recovery action must not auto execute")
        if execution.get("job_id") is not None and execution.get("requires_user_confirmation") is not True:
            errors.append("execution.requires_user_confirmation: job action requires user confirmation")
    source = instance.get("source")
    if isinstance(source, dict) and source.get("health_status") != "degraded":
        errors.append("source.health_status: recovery action requires degraded Product Health")
    status = instance.get("status")
    user_decision = instance.get("user_decision")
    if status == "open" and user_decision is not None:
        errors.append("user_decision: open recovery action must not have user decision")
    resolution = instance.get("resolution")
    if status != "resolved" and resolution is not None:
        errors.append("resolution: only resolved recovery action may have resolution evidence")
    if status in {"acknowledged", "dismissed"}:
        if not isinstance(user_decision, dict):
            errors.append("user_decision: acknowledged or dismissed action requires user decision")
        else:
            if user_decision.get("decision") != status:
                errors.append("user_decision.decision: must match action status")
            if user_decision.get("execution_requested") is not False:
                errors.append("user_decision.execution_requested: recovery action decision must not execute repair")
    if status == "resolved":
        if not isinstance(resolution, dict):
            errors.append("resolution: resolved recovery action requires readiness evidence")
        else:
            if resolution.get("health_status") != "ready":
                errors.append("resolution.health_status: resolved action requires ready platform health")
            if resolution.get("readiness_verified") is not True:
                errors.append("resolution.readiness_verified: resolved action requires verified readiness")
            if resolution.get("recovered_capability") != capability:
                errors.append("resolution.recovered_capability: must match action capability")
            for key in (
                "remaining_degraded_capabilities",
                "remaining_missing_capabilities",
                "remaining_os_path_leaks",
            ):
                value = resolution.get(key)
                if not isinstance(value, list):
                    errors.append(f"resolution.{key}: resolved action requires remaining capability lists")
                elif capability in value:
                    errors.append(f"resolution.{key}: resolved action must not list recovered capability")
            if not isinstance(resolution.get("resolved_by"), str) or not resolution.get("resolved_by"):
                errors.append("resolution.resolved_by: resolved action requires actor")
            if not isinstance(resolution.get("resolved_at"), str) or not resolution.get("resolved_at"):
                errors.append("resolution.resolved_at: resolved action requires timestamp")
    return errors


def _iter_strings(value: object, prefix: str = "<root>") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(prefix, value)]
    if isinstance(value, list):
        strings: list[tuple[str, str]] = []
        for index, item in enumerate(value):
            strings.extend(_iter_strings(item, f"{prefix}.{index}"))
        return strings
    if isinstance(value, dict):
        strings = []
        for key, item in value.items():
            strings.extend(_iter_strings(item, f"{prefix}.{key}"))
        return strings
    return []


def _looks_like_os_path(value: str) -> bool:
    lowered = value.lower()
    if lowered.startswith(("platform-app-data://", "platform-documents://", "platform-backup://")):
        return False
    if "file://" in lowered:
        return True
    for index in range(0, max(len(value) - 2, 0)):
        if value[index].isalpha() and value[index + 1] == ":" and value[index + 2] in {"\\", "/"}:
            return True
    return value.startswith(("/Users/", "/home/", "/var/", "/tmp/", "/Volumes/"))


def _iter_dict_items(value: object) -> list[tuple[int, dict[str, object]]]:
    if not isinstance(value, list):
        return []
    return [(index, item) for index, item in enumerate(value) if isinstance(item, dict)]


def _check_enum(path: Path, field: str, expected: list[str], errors: list[str]) -> None:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        actual = schema["properties"][field]["enum"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        errors.append(f"{path.name}: cannot read {field} enum: {error}")
        return
    if actual != expected:
        errors.append(f"{path.name}: {field} enum does not match the rebuild contract")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate R001 rebuild JSON contracts.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("core-contracts/rebuild"),
        help="Directory containing rebuild JSON Schema files.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    errors = validate_contracts(args.root)
    if errors:
        for error in errors:
            print(f"ERROR {error}")
        return 1
    for name in sorted(EXPECTED_REQUIRED):
        print(f"OK {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
