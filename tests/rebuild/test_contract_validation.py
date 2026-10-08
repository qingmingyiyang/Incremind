from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from tools.validate_rebuild_contracts import validate_contracts, validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def test_repository_rebuild_contracts_pass_structural_validation() -> None:
    assert validate_contracts(CONTRACT_ROOT) == []


def test_validator_rejects_missing_contracts_and_required_fields(tmp_path: Path) -> None:
    source = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }
    (tmp_path / "source.schema.json").write_text(
        json.dumps(source),
        encoding="utf-8",
    )

    errors = validate_contracts(tmp_path)

    assert "missing contract: asset.schema.json" in errors
    assert "missing contract: atom.schema.json" in errors
    assert any(error.startswith("source.schema.json: missing required fields:") for error in errors)


def test_source_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "source.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    fixture_root = CONTRACT_ROOT / "fixtures" / "source"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert valid_paths
    assert invalid_paths
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert list(validator.iter_errors(instance)) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validator.is_valid(instance) is False, path.name


def test_source_contract_rejects_unknown_fields() -> None:
    schema = json.loads((CONTRACT_ROOT / "source.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    source = json.loads(
        (CONTRACT_ROOT / "fixtures" / "source" / "valid-inline-text.json").read_text(encoding="utf-8")
    )
    source["windows_path"] = "C:\\Users\\example\\source.txt"

    errors = list(validator.iter_errors(source))

    assert any(error.validator == "additionalProperties" for error in errors)


def test_asset_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "asset.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    fixture_root = CONTRACT_ROOT / "fixtures" / "asset"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 2
    assert len(invalid_paths) >= 3
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert list(validator.iter_errors(instance)) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validator.is_valid(instance) is False, path.name


def test_asset_contract_keeps_copy_and_reference_uri_schemes_distinct() -> None:
    schema = json.loads((CONTRACT_ROOT / "asset.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    copy_asset = json.loads(
        (CONTRACT_ROOT / "fixtures" / "asset" / "valid-copy-available.json").read_text(encoding="utf-8")
    )
    reference_asset = json.loads(
        (CONTRACT_ROOT / "fixtures" / "asset" / "valid-reference-missing.json").read_text(encoding="utf-8")
    )

    copy_asset["uri"] = reference_asset["uri"]
    reference_asset["uri"] = "crp://default/assets/asset-audio-001"

    assert validator.is_valid(copy_asset) is False
    assert validator.is_valid(reference_asset) is False


def test_storage_namespace_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "storage_namespace.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "storage_namespace"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 2
    assert len(invalid_paths) >= 5
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("storage_namespace.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("storage_namespace.schema.json", schema, instance) != [], path.name


def test_storage_namespace_semantic_gate_rejects_namespace_mismatch() -> None:
    schema = json.loads((CONTRACT_ROOT / "storage_namespace.schema.json").read_text(encoding="utf-8"))
    namespace = json.loads(
        (CONTRACT_ROOT / "fixtures" / "storage_namespace" / "valid-default-disabled.json").read_text(
            encoding="utf-8"
        )
    )
    namespace["backup_policy"]["snapshot_uri_prefix"] = "crp://other/backups/"

    errors = validate_contract_instance("storage_namespace.schema.json", schema, namespace)

    assert any("backup_policy.snapshot_uri_prefix" in error for error in errors)


def test_job_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "job.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "job"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 4
    assert len(invalid_paths) >= 6
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("job.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("job.schema.json", schema, instance) != [], path.name


def test_job_semantic_gate_blocks_publish_before_completed() -> None:
    schema = json.loads((CONTRACT_ROOT / "job.schema.json").read_text(encoding="utf-8"))
    job = json.loads(
        (CONTRACT_ROOT / "fixtures" / "job" / "valid-running-with-checkpoint.json").read_text(
            encoding="utf-8"
        )
    )
    job["published_outputs"] = [
        {
            "kind": "atom",
            "uri": "crp://default/memory/atoms/atom-early",
            "object_id": "atom-early",
            "published": True,
        }
    ]

    errors = validate_contract_instance("job.schema.json", schema, job)

    assert any("non-completed job must not publish outputs" in error for error in errors)


def test_job_semantic_gate_requires_completed_publish_boundary() -> None:
    schema = json.loads((CONTRACT_ROOT / "job.schema.json").read_text(encoding="utf-8"))
    job = json.loads(
        (CONTRACT_ROOT / "fixtures" / "job" / "valid-completed-published.json").read_text(
            encoding="utf-8"
        )
    )
    job["staged_outputs"] = [
        {
            "kind": "atom",
            "uri": "crp://default/staging/jobs/job-capture-completed-001/atoms/atom-candidate-001",
            "object_id": "atom-candidate-001",
            "published": False,
        }
    ]
    job["published_outputs"] = []

    errors = validate_contract_instance("job.schema.json", schema, job)

    assert any("completed job must not keep staged outputs" in error for error in errors)
    assert any("completed job requires published outputs" in error for error in errors)


def test_platform_capability_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "platform_capability.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "platform_capability"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 3
    assert len(invalid_paths) >= 5
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("platform_capability.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("platform_capability.schema.json", schema, instance) != [], path.name


def test_platform_capability_semantic_gate_rejects_os_paths() -> None:
    schema = json.loads((CONTRACT_ROOT / "platform_capability.schema.json").read_text(encoding="utf-8"))
    capability = json.loads(
        (
            CONTRACT_ROOT
            / "fixtures"
            / "platform_capability"
            / "valid-worker-degraded.json"
        ).read_text(encoding="utf-8")
    )
    capability["error"]["message"] = "Worker failed at C:\\Users\\example\\worker.log"

    errors = validate_contract_instance("platform_capability.schema.json", schema, capability)

    assert any("must not contain an OS path" in error for error in errors)


def test_platform_capability_semantic_gate_requires_error_for_degradation() -> None:
    schema = json.loads((CONTRACT_ROOT / "platform_capability.schema.json").read_text(encoding="utf-8"))
    capability = json.loads(
        (
            CONTRACT_ROOT
            / "fixtures"
            / "platform_capability"
            / "valid-worker-degraded.json"
        ).read_text(encoding="utf-8")
    )
    capability["error"] = None

    errors = validate_contract_instance("platform_capability.schema.json", schema, capability)

    assert any("unavailable capability requires an error" in error for error in errors)


def test_document_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "document.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "document"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 2
    assert len(invalid_paths) >= 4
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("document.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("document.schema.json", schema, instance) != [], path.name


def test_document_semantic_gate_requires_snapshot_coverage() -> None:
    schema = json.loads((CONTRACT_ROOT / "document.schema.json").read_text(encoding="utf-8"))
    document = json.loads(
        (CONTRACT_ROOT / "fixtures" / "document" / "valid-project-doc.json").read_text(
            encoding="utf-8"
        )
    )
    document["source_snapshot"]["source_refs"] = []

    errors = validate_contract_instance("document.schema.json", schema, document)

    assert any("source_snapshot.source_refs" in error for error in errors)


def test_document_semantic_gate_protects_user_edited_blocks() -> None:
    schema = json.loads((CONTRACT_ROOT / "document.schema.json").read_text(encoding="utf-8"))
    document = json.loads(
        (CONTRACT_ROOT / "fixtures" / "document" / "valid-project-doc.json").read_text(
            encoding="utf-8"
        )
    )
    document["blocks"][0]["lock_policy"] = "none"

    errors = validate_contract_instance("document.schema.json", schema, document)

    assert any("user edited block must be user_edit_protected" in error for error in errors)


def test_document_revision_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "document_revision.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "document_revision"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 3
    assert len(invalid_paths) >= 4
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("document_revision.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("document_revision.schema.json", schema, instance) != [], path.name


def test_document_revision_semantic_gate_blocks_ai_patch_over_user_block() -> None:
    schema = json.loads((CONTRACT_ROOT / "document_revision.schema.json").read_text(encoding="utf-8"))
    revision = json.loads(
        (
            CONTRACT_ROOT
            / "fixtures"
            / "document_revision"
            / "valid-ai-patch-conflict-detected.json"
        ).read_text(encoding="utf-8")
    )
    revision["conflict"] = {
        "status": "none",
        "conflict_blocks": [],
        "resolution": None,
    }

    errors = validate_contract_instance("document_revision.schema.json", schema, revision)

    assert any("ai_patch touching user protected blocks requires conflict" in error for error in errors)


def test_document_revision_semantic_gate_requires_revision_increment() -> None:
    schema = json.loads((CONTRACT_ROOT / "document_revision.schema.json").read_text(encoding="utf-8"))
    revision = json.loads(
        (CONTRACT_ROOT / "fixtures" / "document_revision" / "valid-ai-patch.json").read_text(
            encoding="utf-8"
        )
    )
    revision["revision"] = revision["parent_revision"]

    errors = validate_contract_instance("document_revision.schema.json", schema, revision)

    assert any("must be greater than parent_revision" in error for error in errors)


def test_atom_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "atom.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "atom"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 2
    assert len(invalid_paths) >= 2
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("atom.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("atom.schema.json", schema, instance) != [], path.name


def test_scenario_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "scenario.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "scenario"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 2
    assert len(invalid_paths) >= 2
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("scenario.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("scenario.schema.json", schema, instance) != [], path.name


def test_persona_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "persona.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "persona"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 2
    assert len(invalid_paths) >= 2
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("persona.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("persona.schema.json", schema, instance) != [], path.name


def test_series_memory_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "series_memory.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "series_memory"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 2
    assert len(invalid_paths) >= 2
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("series_memory.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("series_memory.schema.json", schema, instance) != [], path.name


def test_memory_transition_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "memory_transition.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "memory_transition"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 2
    assert len(invalid_paths) >= 3
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("memory_transition.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("memory_transition.schema.json", schema, instance) != [], path.name


def test_memory_transition_blocks_imported_unverified_l3_auto_promotion() -> None:
    schema = json.loads((CONTRACT_ROOT / "memory_transition.schema.json").read_text(encoding="utf-8"))
    transition = json.loads(
        (
            CONTRACT_ROOT
            / "fixtures"
            / "memory_transition"
            / "valid-confirm-persona.json"
        ).read_text(encoding="utf-8")
    )
    transition["from_trust_status"] = "imported_unverified"
    transition["actor"] = "system"

    errors = validate_contract_instance("memory_transition.schema.json", schema, transition)

    assert any("imported_unverified L3 promotion requires user actor" in error for error in errors)


def test_l3_memory_blocks_imported_unverified_status() -> None:
    schema = json.loads((CONTRACT_ROOT / "persona.schema.json").read_text(encoding="utf-8"))
    persona = json.loads(
        (CONTRACT_ROOT / "fixtures" / "persona" / "valid-user-confirmed.json").read_text(
            encoding="utf-8"
        )
    )
    persona["trust_status"] = "imported_unverified"

    errors = validate_contract_instance("persona.schema.json", schema, persona)

    assert any("imported_unverified cannot enter L3 persona" in error for error in errors)


def test_project_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "project.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "project"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 2
    assert len(invalid_paths) >= 3
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("project.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("project.schema.json", schema, instance) != [], path.name


def test_project_semantic_gate_requires_active_project_skill() -> None:
    schema = json.loads((CONTRACT_ROOT / "project.schema.json").read_text(encoding="utf-8"))
    project = json.loads(
        (CONTRACT_ROOT / "fixtures" / "project" / "valid-active-project.json").read_text(
            encoding="utf-8"
        )
    )
    project["skill_id"] = None

    errors = validate_contract_instance("project.schema.json", schema, project)

    assert any("active project requires a skill" in error for error in errors)


def test_project_skill_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "project_skill.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "project_skill"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 2
    assert len(invalid_paths) >= 5
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("project_skill.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("project_skill.schema.json", schema, instance) != [], path.name


def test_project_skill_semantic_gate_requires_markdown_json_revision_sync() -> None:
    schema = json.loads((CONTRACT_ROOT / "project_skill.schema.json").read_text(encoding="utf-8"))
    skill = json.loads(
        (CONTRACT_ROOT / "fixtures" / "project_skill" / "valid-active-skill.json").read_text(
            encoding="utf-8"
        )
    )
    skill["json_revision"] = skill["revision"] - 1

    errors = validate_contract_instance("project_skill.schema.json", schema, skill)

    assert any("json_revision: must equal revision" in error for error in errors)


def test_project_skill_semantic_gate_blocks_stale_required_context_for_active_skill() -> None:
    schema = json.loads((CONTRACT_ROOT / "project_skill.schema.json").read_text(encoding="utf-8"))
    skill = json.loads(
        (CONTRACT_ROOT / "fixtures" / "project_skill" / "valid-active-skill.json").read_text(
            encoding="utf-8"
        )
    )
    skill["required_context"][0]["stale"] = True

    errors = validate_contract_instance("project_skill.schema.json", schema, skill)

    assert any("active project skill cannot require stale context" in error for error in errors)


def test_project_skill_semantic_gate_blocks_detected_conflict_from_active_publish() -> None:
    schema = json.loads((CONTRACT_ROOT / "project_skill.schema.json").read_text(encoding="utf-8"))
    skill = json.loads(
        (
            CONTRACT_ROOT
            / "fixtures"
            / "project_skill"
            / "valid-conflicted-draft.json"
        ).read_text(encoding="utf-8")
    )
    skill["status"] = "active"

    errors = validate_contract_instance("project_skill.schema.json", schema, skill)

    assert any("detected conflict requires conflicted status" in error for error in errors)


def test_project_skill_semantic_gate_requires_ai_rule_source_refs() -> None:
    schema = json.loads((CONTRACT_ROOT / "project_skill.schema.json").read_text(encoding="utf-8"))
    skill = json.loads(
        (CONTRACT_ROOT / "fixtures" / "project_skill" / "valid-active-skill.json").read_text(
            encoding="utf-8"
        )
    )
    skill["output_rules"][1]["source_refs"] = []

    errors = validate_contract_instance("project_skill.schema.json", schema, skill)

    assert any("ai output rule requires source refs" in error for error in errors)


def test_recall_request_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "recall_request.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "recall_request"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 2
    assert len(invalid_paths) >= 3
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("recall_request.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("recall_request.schema.json", schema, instance) != [], path.name


def test_recall_request_semantic_gate_keeps_project_scope_isolated() -> None:
    schema = json.loads((CONTRACT_ROOT / "recall_request.schema.json").read_text(encoding="utf-8"))
    request = json.loads(
        (CONTRACT_ROOT / "fixtures" / "recall_request" / "valid-project-default.json").read_text(
            encoding="utf-8"
        )
    )
    request["cross_project"] = {
        "allowed": True,
        "grant_id": "grant-unexpected",
        "project_ids": ["project-beta"],
    }

    errors = validate_contract_instance("recall_request.schema.json", schema, request)

    assert any("project scope must keep cross-project disabled" in error for error in errors)
    assert any("project scope must not include a grant" in error for error in errors)


def test_recall_request_semantic_gate_requires_project_skill_first() -> None:
    schema = json.loads((CONTRACT_ROOT / "recall_request.schema.json").read_text(encoding="utf-8"))
    request = json.loads(
        (CONTRACT_ROOT / "fixtures" / "recall_request" / "valid-project-default.json").read_text(
            encoding="utf-8"
        )
    )
    request["layers"] = ["l2_scenario", "l1_atom"]

    errors = validate_contract_instance("recall_request.schema.json", schema, request)

    assert any("recall must start with l4_persona then l3_project_skill" in error for error in errors)


def test_recall_result_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "recall_result.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "recall_result"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 3
    assert len(invalid_paths) >= 4
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("recall_result.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("recall_result.schema.json", schema, instance) != [], path.name


def test_recall_result_semantic_gate_requires_insufficient_evidence_error() -> None:
    schema = json.loads((CONTRACT_ROOT / "recall_result.schema.json").read_text(encoding="utf-8"))
    result = json.loads(
        (CONTRACT_ROOT / "fixtures" / "recall_result" / "valid-insufficient-evidence.json").read_text(
            encoding="utf-8"
        )
    )
    result["errors"] = []

    errors = validate_contract_instance("recall_result.schema.json", schema, result)

    assert any("requires insufficient_evidence error" in error for error in errors)


def test_recall_result_semantic_gate_requires_cross_project_label_and_grant() -> None:
    schema = json.loads((CONTRACT_ROOT / "recall_result.schema.json").read_text(encoding="utf-8"))
    result = json.loads(
        (CONTRACT_ROOT / "fixtures" / "recall_result" / "valid-cross-project-granted.json").read_text(
            encoding="utf-8"
        )
    )
    result["cross_project"] = {
        "used": False,
        "grant_id": None,
        "project_ids": [],
    }

    errors = validate_contract_instance("recall_result.schema.json", schema, result)

    assert any("cross-project hit requires cross_project.used" in error for error in errors)


def test_model_request_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "model_request.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "model_request"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 2
    assert len(invalid_paths) >= 4
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("model_request.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("model_request.schema.json", schema, instance) != [], path.name


def test_model_request_semantic_gate_blocks_remote_pii_without_redaction() -> None:
    schema = json.loads((CONTRACT_ROOT / "model_request.schema.json").read_text(encoding="utf-8"))
    request = json.loads(
        (CONTRACT_ROOT / "fixtures" / "model_request" / "valid-remote-redacted.json").read_text(
            encoding="utf-8"
        )
    )
    request["privacy"]["redaction"] = {
        "applied": False,
        "strategy": "none",
    }

    errors = validate_contract_instance("model_request.schema.json", schema, request)

    assert any("remote request with pii requires redaction" in error for error in errors)


def test_model_request_semantic_gate_requires_structured_schema() -> None:
    schema = json.loads((CONTRACT_ROOT / "model_request.schema.json").read_text(encoding="utf-8"))
    request = json.loads(
        (CONTRACT_ROOT / "fixtures" / "model_request" / "valid-local-structured.json").read_text(
            encoding="utf-8"
        )
    )
    request["response_schema"] = {
        "type": "text",
        "json_schema_uri": None,
        "strict": False,
    }

    errors = validate_contract_instance("model_request.schema.json", schema, request)

    assert any("structured_generation requires json response" in error for error in errors)
    assert any("structured_generation requires schema uri" in error for error in errors)
    assert any("structured_generation requires strict schema" in error for error in errors)


def test_model_result_contract_accepts_valid_and_rejects_invalid_fixtures() -> None:
    schema = json.loads((CONTRACT_ROOT / "model_result.schema.json").read_text(encoding="utf-8"))
    fixture_root = CONTRACT_ROOT / "fixtures" / "model_result"

    valid_paths = sorted(fixture_root.glob("valid-*.json"))
    invalid_paths = sorted(fixture_root.glob("invalid-*.json"))

    assert len(valid_paths) >= 3
    assert len(invalid_paths) >= 4
    for path in valid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("model_result.schema.json", schema, instance) == [], path.name
    for path in invalid_paths:
        instance = json.loads(path.read_text(encoding="utf-8"))
        assert validate_contract_instance("model_result.schema.json", schema, instance) != [], path.name


def test_model_result_semantic_gate_requires_error_status_alignment() -> None:
    schema = json.loads((CONTRACT_ROOT / "model_result.schema.json").read_text(encoding="utf-8"))
    result = json.loads(
        (CONTRACT_ROOT / "fixtures" / "model_result" / "valid-timeout.json").read_text(
            encoding="utf-8"
        )
    )
    result["error"]["code"] = "provider_error"

    errors = validate_contract_instance("model_result.schema.json", schema, result)

    assert any("timeout result requires matching error code" in error for error in errors)


def test_model_result_semantic_gate_blocks_privacy_output() -> None:
    schema = json.loads((CONTRACT_ROOT / "model_result.schema.json").read_text(encoding="utf-8"))
    result = json.loads(
        (CONTRACT_ROOT / "fixtures" / "model_result" / "valid-privacy-blocked.json").read_text(
            encoding="utf-8"
        )
    )
    result["output"] = {
        "kind": "text",
        "content": "privacy blocked result must not include this",
        "structured": None,
        "output_refs": [],
    }

    errors = validate_contract_instance("model_result.schema.json", schema, result)

    assert any("non-completed result must not include generated output" in error for error in errors)


def _ai_contract_validator(filename: str) -> Draft202012Validator:
    schema = json.loads((ROOT / "core-contracts" / "ai" / filename).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_codex_hook_manifest_and_policy_snapshot_pin_revision_and_safe_handler_metadata() -> None:
    manifest = {
        "schema_version": "1.0.0",
        "kind": "codex-hook-manifest",
        "manifest_id": "hook-manifest-default",
        "revision": "revision-7",
        "upstream_revision": "0fe877b4dedc86a29c8bebb3edbd3efcc3580c7d",
        "handlers": [{
            "handler_id": "pre-tool-policy",
            "handler_revision": "revision-7",
            "event": "PreToolUse",
            "order": 0,
            "sync": True,
            "handler_ref": "crp://default/hooks/pre-tool-policy",
            "timeout_ms": 250,
        }],
        "audit_delivery": "async_best_effort",
    }
    snapshot = {
        "schema_version": "1.0.0",
        "kind": "codex-hook-policy-snapshot",
        "snapshot_id": "hook-snapshot-7",
        "revision": "revision-7",
        "upstream_revision": "0fe877b4dedc86a29c8bebb3edbd3efcc3580c7d",
        "manifest_ref": "crp://default/hooks/manifests/hook-manifest-default",
        "manifest_revision": "revision-7",
        "handlers": [{
            "handler_id": "pre-tool-policy",
            "handler_revision": "revision-7",
            "event": "PreToolUse",
            "order": 0,
            "sync": True,
            "enabled": True,
            "handler_ref": "crp://default/hooks/pre-tool-policy",
            "timeout_ms": 250,
        }],
        "local_hard_guard_revision": "guard-revision-3",
        "audit_delivery": "async_best_effort",
    }

    assert _ai_contract_validator("codex-hook-manifest.schema.json").is_valid(manifest)
    assert _ai_contract_validator("codex-hook-policy-snapshot.schema.json").is_valid(snapshot)

    manifest["handlers"][0]["command"] = "C:\\private\\hook.cmd"
    snapshot["upstream_revision"] = "unreviewed"

    assert _ai_contract_validator("codex-hook-manifest.schema.json").is_valid(manifest) is False
    assert _ai_contract_validator("codex-hook-policy-snapshot.schema.json").is_valid(snapshot) is False


def test_codex_hook_invocation_receipt_keeps_only_normalized_event_specific_outcome() -> None:
    receipt = {
        "schema_version": "1.0.0",
        "kind": "codex-hook-invocation-receipt",
        "invocation_id": "hook-invocation-0001",
        "turn_id": "turn-0001",
        "project_id": "project-0001",
        "event": "PreToolUse",
        "upstream_revision": "0fe877b4dedc86a29c8bebb3edbd3efcc3580c7d",
        "policy_snapshot_ref": "crp://default/hooks/snapshots/hook-snapshot-7",
        "policy_snapshot_revision": "revision-7",
        "handler_runs": [{
            "handler_id": "pre-tool-policy",
            "handler_revision": "revision-7",
            "order": 0,
            "sync": True,
            "status": "blocked",
            "control_ignored": False,
            "reason": "restricted capability",
            "duration_ms": 3,
        }],
        "normalized_outcome": {
            "event": "PreToolUse",
            "dispatch_blocked": True,
            "reason": "restricted capability",
            "input_rewrite_applied": False,
        },
        "duration_ms": 4,
        "audit_delivery": "async_best_effort",
    }
    validator = _ai_contract_validator("codex-hook-invocation-receipt.schema.json")

    assert validator.is_valid(receipt)

    receipt["raw_stdout"] = '{"secret":"must-not-record"}'
    assert validator.is_valid(receipt) is False
    del receipt["raw_stdout"]
    receipt["normalized_outcome"]["permission"] = "denied"
    assert validator.is_valid(receipt) is False


def test_codex_hook_invocation_receipt_requires_event_matched_normalized_outcome() -> None:
    receipt = {
        "schema_version": "1.0.0",
        "kind": "codex-hook-invocation-receipt",
        "invocation_id": "hook-invocation-0002",
        "turn_id": None,
        "project_id": None,
        "event": "Stop",
        "upstream_revision": "0fe877b4dedc86a29c8bebb3edbd3efcc3580c7d",
        "policy_snapshot_ref": "crp://default/hooks/snapshots/hook-snapshot-7",
        "policy_snapshot_revision": "revision-7",
        "handler_runs": [],
        "normalized_outcome": {"event": "Stop", "stop_allowed": False, "reason": "finish receipt"},
        "duration_ms": 0,
        "audit_delivery": "async_best_effort",
    }
    validator = _ai_contract_validator("codex-hook-invocation-receipt.schema.json")

    assert validator.is_valid(receipt)
    receipt["event"] = "PreToolUse"
    assert validator.is_valid(receipt) is False
