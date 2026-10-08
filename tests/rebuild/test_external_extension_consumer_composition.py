from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from core.application_skill import (
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillVerifiedContent,
)
from core.composition import (
    build_answer_model_request_from_recall,
    build_project_document_model_request,
)
from core.model_gateway import ObjectStoreModelRequestRepository
from core.product_core import AnswerModelRequestError
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate
from core.search_and_recall import ObjectStoreRecallRepository
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _external_package(skill_id: str, marker: str):
    content = ApplicationSkillVerifiedContent.from_mapping({
        "SKILL.md": (
            "---\n"
            f"name: {skill_id}\n"
            f"description: Use {skill_id} for external consumer review.\n"
            "---\n\n"
            f"# {marker}\n"
        ).encode("utf-8"),
    })
    return ApplicationSkillCatalog().package_from_verified_content(
        content,
        source_id="external-project-alpha-review-1",
        source_kind="external",
        package_root=Path("retired-managed-tree") / skill_id,
    )


def _activate(registry: ApplicationSkillBindingRegistry, package, *, consumer: str, term: str) -> None:
    values = {
        "project_id": "project-alpha",
        "allowed_consumers": (consumer,),
        "priority": 700,
        "trigger_terms": (term,),
    }
    preview = registry.preview_bind(package, **values)
    registry.activate(
        package,
        **values,
        expected_registry_revision=int(preview["registry_revision"]),
        preview_token=str(preview["preview_token"]),
        confirm=True,
        reason="Activate reviewed immutable external consumer package.",
    )


def _recall(recalls: ObjectStoreRecallRepository, *, identifier: str, query: str):
    request = recalls.create_project_default_request(
        project_id="project-alpha",
        query=query,
        project_skill_id="project-skill-alpha",
        created_at="2026-08-30T12:00:00+08:00",
    )
    return recalls.save_result({
        "schema_version": "1.0.0",
        "id": identifier,
        "request_id": request["id"],
        "project_id": "project-alpha",
        "status": "evidence_found",
        "hits": [{
            "hit_id": f"hit-{identifier}",
            "layer": "l3_project_skill",
            "object_id": "project-skill-alpha",
            "project_id": "project-alpha",
            "source_project_label": None,
            "trust_status": "user_confirmed",
            "score": 0.99,
            "token_estimate": 32,
            "source_refs": [{"source_id": "source-alpha", "locator": "char:0-80"}],
            "snippet": "已验证的项目证据。",
            "explanation": "Project Skill evidence.",
        }],
        "coverage": {
            "status": "sufficient",
            "requested_layers": request["layers"],
            "covered_layers": ["l3_project_skill"],
            "missing_layers": [],
            "low_trust": False,
            "source_ref_count": 1,
        },
        "truncation": {
            "applied": False,
            "reason": "none",
            "dropped_hit_ids": [],
            "final_hit_count": 1,
            "final_token_estimate": 32,
        },
        "explanation": {"summary": "Evidence ready.", "layer_order": request["layers"], "warnings": []},
        "cross_project": {"used": False, "grant_id": None, "project_ids": []},
        "errors": [],
        "created_at": "2026-08-30T12:00:01+08:00",
    })


def _project_skill() -> dict[str, object]:
    skill = json.loads((CONTRACT_ROOT / "fixtures" / "project_skill" / "valid-active-skill.json").read_text(encoding="utf-8"))
    skill["id"] = "project-skill-alpha"
    skill["project_id"] = "project-alpha"
    skill["name"] = "项目 Alpha"
    skill["purpose"] = "项目文档生成。"
    skill["markdown_uri"] = "crp://default/projects/project-alpha/project-skill.md"
    skill["json_uri"] = "crp://default/projects/project-alpha/project-skill.json"
    skill["source_refs"] = [{"source_id": "source-alpha", "locator": "char:0-80"}]
    skill["evidence_refs"] = []
    skill["output_rules"] = [{
        "rule_id": "rule-alpha", "priority": "must", "rule": "PROJECT-ALPHA-RULE",
        "source_refs": [{"source_id": "source-alpha", "locator": "char:0-80"}],
    }]
    return skill


def test_composed_answer_uses_active_external_package_then_ignores_disabled_and_drifted_provider(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    package = _external_package("external-answer", "EXTERNAL-ANSWER-METHOD")
    registry = ApplicationSkillBindingRegistry(store, now="2026-08-30T12:00:00+00:00")
    _activate(registry, package, consumer="answer.model-request", term="外部审核")
    packages = (package,)

    def active_packages(_project_id: str):
        return packages

    recalls = ObjectStoreRecallRepository(store)
    active = _recall(recalls, identifier="recall-external-active", query="请做外部审核。")
    use_case = build_answer_model_request_from_recall(
        ROOT, runtime_root=tmp_path, external_packages=active_packages,
    )
    result = use_case.execute(str(active["id"]), created_at="2026-08-30T12:01:00+08:00")
    request = ObjectStoreModelRequestRepository(store).get_request(result.model_request_id)
    assert request is not None
    assert "EXTERNAL-ANSWER-METHOD" in str(request["payload"]["content"])

    packages = ()
    disabled = _recall(recalls, identifier="recall-external-disabled", query="请做外部审核。")
    disabled_result = build_answer_model_request_from_recall(
        ROOT, runtime_root=tmp_path, external_packages=active_packages,
    ).execute(str(disabled["id"]), created_at="2026-08-30T12:02:00+08:00")
    disabled_request = ObjectStoreModelRequestRepository(store).get_request(disabled_result.model_request_id)
    assert disabled_request is not None
    assert "EXTERNAL-ANSWER-METHOD" not in str(disabled_request["payload"]["content"])

    packages = (replace(package, fingerprint="0" * 64),)
    drifted = _recall(recalls, identifier="recall-external-drifted", query="请做外部审核。")
    with pytest.raises(AnswerModelRequestError, match="Application Skill resolution failed"):
        build_answer_model_request_from_recall(
            ROOT, runtime_root=tmp_path, external_packages=active_packages,
        ).execute(str(drifted["id"]), created_at="2026-08-30T12:03:00+08:00")
    persisted = ObjectStoreModelRequestRepository(store).list_requests("project-alpha")
    assert {item["id"] for item in persisted} == {request["id"], disabled_request["id"]}


def test_composed_document_uses_active_external_immutable_package_after_managed_path_is_gone(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    package = _external_package("external-document", "EXTERNAL-DOCUMENT-METHOD")
    registry = ApplicationSkillBindingRegistry(store, now="2026-08-30T12:00:00+00:00")
    _activate(registry, package, consumer="document.generate", term="项目文档")
    project_skills = ObjectStoreProjectSkillRepository(store)
    structured = _project_skill()
    project_skills.save(ProjectSkillUpdate(
        project_id="project-alpha",
        markdown="# 项目 Alpha",
        structured=copy.deepcopy(structured),
        expected_revision=0,
        reason="Persist project authority before external consumer composition.",
    ))

    result = build_project_document_model_request(
        ROOT,
        runtime_root=tmp_path,
        external_packages=lambda _project_id: (package,),
    ).execute(
        "project-alpha",
        brief="请生成项目文档。",
        created_at="2026-08-30T12:04:00+08:00",
    )
    request = ObjectStoreModelRequestRepository(store).get_request(result.model_request_id)
    assert request is not None
    assert "EXTERNAL-DOCUMENT-METHOD" in str(request["payload"]["content"])
