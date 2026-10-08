from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.application_skill import (
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillConsumerRuntime,
    ApplicationSkillResolver,
    ApplicationSkillSource,
    ObjectStoreApplicationSkillTraceRepository,
)
from core.composition import build_project_document_model_request
from core.document_engine import ObjectStoreDocumentRepository
from core.model_gateway import ObjectStoreModelRequestRepository, ObjectStoreModelResultRepository
from core.product_core import (
    CreateDocumentFromModelResult,
    CreateProjectDocumentModelRequest,
    ProjectDocumentModelRequestError,
)
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _fixture() -> dict[str, object]:
    path = CONTRACT_ROOT / "fixtures" / "project_skill" / "valid-active-skill.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _project_skill(project_id: str, marker: str) -> dict[str, object]:
    skill = copy.deepcopy(_fixture())
    skill["id"] = f"skill-{project_id}"
    skill["project_id"] = project_id
    skill["name"] = f"{project_id} 项目"
    skill["purpose"] = f"{project_id} 的项目文档目标。"
    skill["markdown_uri"] = f"crp://default/projects/{project_id}/project-skill.md"
    skill["json_uri"] = f"crp://default/projects/{project_id}/project-skill.json"
    skill["source_refs"] = [
        {"source_id": f"source-{project_id}", "locator": "char:0-80"}
    ]
    skill["evidence_refs"] = []
    skill["output_rules"] = [
        {
            "rule_id": f"rule-{project_id}",
            "priority": "must",
            "rule": marker,
            "source_refs": [
                {"source_id": f"source-{project_id}", "locator": "char:0-80"}
            ],
        }
    ]
    return skill


def _save_skill(
    repository: ObjectStoreProjectSkillRepository,
    structured: dict[str, object],
    *,
    expected_revision: int = 0,
) -> dict[str, object]:
    return dict(
        repository.save(
            ProjectSkillUpdate(
                project_id=str(structured["project_id"]),
                markdown=f"# {structured['name']}",
                structured=structured,
                expected_revision=expected_revision,
                reason="Persist document consumer Project Skill evidence.",
            )
        )
    )


def _write_application_skill(source: Path, skill_id: str, marker: str) -> None:
    root = source / skill_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        f"description: Use {skill_id} for project document planning.\n"
        "---\n\n"
        f"# {marker}\n\nApply this document method without changing project rules.\n",
        encoding="utf-8",
    )


def _catalog(source: Path):
    return ApplicationSkillCatalog().discover(
        [ApplicationSkillSource("user", source, "user")]
    )


def _bind(
    registry: ApplicationSkillBindingRegistry,
    package,
    *,
    project_id: str,
) -> None:
    values = {
        "project_id": project_id,
        "allowed_consumers": ("document.generate",),
        "priority": 700,
        "trigger_terms": ("项目文档",),
    }
    preview = registry.preview_bind(package, **values)
    registry.activate(
        package,
        **values,
        expected_registry_revision=preview["registry_revision"],
        preview_token=preview["preview_token"],
        confirm=True,
        reason="Bind reviewed document method.",
    )


def _runtime(tmp_path: Path, store: JsonObjectStore) -> ApplicationSkillConsumerRuntime:
    traces = ObjectStoreApplicationSkillTraceRepository(store)
    return ApplicationSkillConsumerRuntime(
        catalog=ApplicationSkillCatalog(),
        sources=(ApplicationSkillSource("user", tmp_path / "skills", "user"),),
        resolver=ApplicationSkillResolver(
            ApplicationSkillBindingRegistry(store, now="2026-07-18T16:00:00+00:00"),
            trace_store=traces,
            now="2026-07-18T16:00:00+00:00",
        ),
    )


def _use_case(tmp_path: Path, store: JsonObjectStore) -> CreateProjectDocumentModelRequest:
    return CreateProjectDocumentModelRequest(
        project_skills=ObjectStoreProjectSkillRepository(store),
        model_requests=ObjectStoreModelRequestRepository(store),
        application_skills=_runtime(tmp_path, store),
    )


def test_two_projects_apply_isolated_methods_under_project_skill_authority(
    tmp_path: Path,
) -> None:
    source = tmp_path / "skills"
    _write_application_skill(source, "alpha-document", "ALPHA-ONLY-DOCUMENT-METHOD")
    _write_application_skill(source, "beta-document", "BETA-ONLY-DOCUMENT-METHOD")
    catalog = _catalog(source)
    store = _store(tmp_path)
    registry = ApplicationSkillBindingRegistry(store, now="2026-07-18T16:00:00+00:00")
    _bind(registry, catalog.get("alpha-document"), project_id="project-alpha")
    _bind(registry, catalog.get("beta-document"), project_id="project-beta")
    project_skills = ObjectStoreProjectSkillRepository(store)
    _save_skill(project_skills, _project_skill("project-alpha", "ALPHA-PROJECT-STRUCTURE"))
    _save_skill(project_skills, _project_skill("project-beta", "BETA-PROJECT-STRUCTURE"))
    use_case = _use_case(tmp_path, store)

    alpha = use_case.execute(
        "project-alpha",
        brief="请生成项目文档并保留项目结构。",
        created_at="2026-07-18T16:01:00+08:00",
    )
    beta = use_case.execute(
        "project-beta",
        brief="请生成项目文档并保留项目结构。",
        created_at="2026-07-18T16:02:00+08:00",
    )
    alpha_replay = _use_case(tmp_path, store).execute(
        "project-alpha",
        brief="请生成项目文档并保留项目结构。",
        created_at="2026-07-18T16:01:00+08:00",
    )
    requests = ObjectStoreModelRequestRepository(store)
    alpha_request = requests.get_request(alpha.model_request_id)
    beta_request = requests.get_request(beta.model_request_id)
    assert alpha_request is not None and beta_request is not None
    alpha_prompt = str(alpha_request["payload"]["content"])
    beta_prompt = str(beta_request["payload"]["content"])

    assert "ALPHA-ONLY-DOCUMENT-METHOD" in alpha_prompt
    assert "BETA-ONLY-DOCUMENT-METHOD" not in alpha_prompt
    assert "ALPHA-PROJECT-STRUCTURE" in alpha_prompt
    assert "BETA-PROJECT-STRUCTURE" not in alpha_prompt
    assert "BETA-ONLY-DOCUMENT-METHOD" in beta_prompt
    assert "ALPHA-ONLY-DOCUMENT-METHOD" not in beta_prompt
    assert "BETA-PROJECT-STRUCTURE" in beta_prompt
    assert "ALPHA-PROJECT-STRUCTURE" not in beta_prompt
    assert alpha_prompt.index("项目工作规则是项目事实") < alpha_prompt.index(
        "ALPHA-ONLY-DOCUMENT-METHOD"
    ) < alpha_prompt.index("## 项目工作规则")
    assert alpha_replay.model_request_id == alpha.model_request_id
    assert alpha_replay.application_skill_resolution_id == alpha.application_skill_resolution_id
    assert len(requests.list_requests("project-alpha")) == 1
    assert len(tuple(store.list("application_skill_resolution_traces"))) == 2
    for request, result in ((alpha_request, alpha), (beta_request, beta)):
        payload = request["payload"]
        assert payload["kind"] == "document_draft"
        assert payload["recall_result_id"] is None
        assert [ref["kind"] for ref in payload["input_refs"]] == [
            "project_skill",
            "application_skill_resolution",
        ]
        assert result.application_skill_resolution_id == payload["input_refs"][1]["object_id"]
        assert request["provider_preference"]["mode"] == "local_only"
        assert request["privacy"]["allow_remote"] is False
        assert validate_contract_instance(
            "model_request.schema.json",
            json.loads((CONTRACT_ROOT / "model_request.schema.json").read_text(encoding="utf-8")),
            request,
        ) == []
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "documents").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / "library").exists()


def test_project_skill_revision_changes_new_request_without_mutating_old_request(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    skills = ObjectStoreProjectSkillRepository(store)
    first = _save_skill(skills, _project_skill("project-alpha", "FIRST-STRUCTURE"))
    use_case = _use_case(tmp_path, store)
    first_result = use_case.execute(
        "project-alpha",
        brief="生成项目文档。",
        created_at="2026-07-18T16:03:00+08:00",
    )
    requests = ObjectStoreModelRequestRepository(store)
    first_request = copy.deepcopy(requests.get_request(first_result.model_request_id))
    updated = copy.deepcopy(first)
    updated["output_rules"].append(
        {
            "rule_id": "rule-new",
            "priority": "must",
            "rule": "SECOND-REVISION-STRUCTURE",
            "source_refs": [
                {"source_id": "source-project-alpha-r2", "locator": "char:10-90"}
            ],
        }
    )
    updated["source_refs"].append(
        {"source_id": "source-project-alpha-r2", "locator": "char:10-90"}
    )
    _save_skill(skills, updated, expected_revision=1)

    second = use_case.execute(
        "project-alpha",
        brief="生成项目文档。",
        created_at="2026-07-18T16:04:00+08:00",
    )
    second_request = requests.get_request(second.model_request_id)

    assert second.project_skill_revision == 2
    assert second.model_request_id != first_result.model_request_id
    assert second_request is not None
    assert "SECOND-REVISION-STRUCTURE" in str(second_request["payload"]["content"])
    assert requests.get_request(first_result.model_request_id) == first_request
    assert "SECOND-REVISION-STRUCTURE" not in str(first_request["payload"]["content"])


class _UnexpectedRuntime:
    def resolve_context(self, **_kwargs):
        raise AssertionError("unsafe Project Skill must fail before Skill resolution")


class _SkillStub:
    def __init__(self, skill: dict[str, object]) -> None:
        self.skill = skill

    def load(self, project_id: str):
        return dict(self.skill) if self.skill.get("project_id") == project_id else None

    def save(self, _update):
        raise AssertionError("document request does not write Project Skill")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(status="conflicted"), "active"),
        (lambda value: value.update(trust_status="imported_unverified"), "trust status"),
        (
            lambda value: value.update(
                conflict={"status": "detected", "conflict_refs": ["rule"], "resolution": None}
            ),
            "conflict",
        ),
        (lambda value: value["required_context"][0].update(stale=True), "stale"),
        (lambda value: value.update(source_refs=[], evidence_refs=[], output_rules=[]), "source refs"),
    ],
)
def test_unsafe_project_skill_fails_before_skill_resolution_and_writes(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    skill = _project_skill("project-alpha", "PROJECT-STRUCTURE")
    mutation(skill)
    store = _store(tmp_path)
    use_case = CreateProjectDocumentModelRequest(
        project_skills=_SkillStub(skill),
        model_requests=ObjectStoreModelRequestRepository(store),
        application_skills=_UnexpectedRuntime(),
    )

    with pytest.raises(ProjectDocumentModelRequestError, match=message):
        use_case.execute("project-alpha", brief="生成项目文档。")

    assert ObjectStoreModelRequestRepository(store).list_requests("project-alpha") == ()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "application_skill_resolution_traces").exists()


def test_document_request_can_complete_into_existing_document_handoff_without_publication(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    skills = ObjectStoreProjectSkillRepository(store)
    _save_skill(skills, _project_skill("project-alpha", "PROJECT-STRUCTURE"))
    request = _use_case(tmp_path, store).execute(
        "project-alpha",
        brief="生成一份可编辑项目文档。",
        created_at="2026-07-18T16:05:00+08:00",
    )
    results = ObjectStoreModelResultRepository(store)
    model_result = results.create_completed_local_result(
        request_id=request.model_request_id,
        output_text="# 生成结果\n\n受项目规则约束的正文。",
        input_tokens=120,
        output_tokens=40,
        completed_at="2026-07-18T16:06:00+08:00",
    )
    documents = ObjectStoreDocumentRepository(store)
    handoff = CreateDocumentFromModelResult(
        model_requests=ObjectStoreModelRequestRepository(store),
        model_results=results,
        documents=documents,
    ).execute(str(model_result["id"]), title="项目文档草稿")

    assert handoff.project_id == "project-alpha"
    assert documents.read(handoff.document_id) is not None
    assert "受项目规则约束的正文" in str(documents.markdown(handoff.document_id))
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / "library").exists()


class _FailOnceModelRequests:
    def __init__(self, delegate: ObjectStoreModelRequestRepository) -> None:
        self.delegate = delegate
        self.failed = False

    def save_request(self, request):
        if not self.failed:
            self.failed = True
            raise ValueError("simulated request persistence interruption")
        return self.delegate.save_request(request)


def test_trace_then_request_interruption_is_diagnosable_and_same_id_retry_converges(
    tmp_path: Path,
) -> None:
    source = tmp_path / "skills"
    _write_application_skill(source, "document-method", "DOCUMENT-METHOD")
    store = _store(tmp_path)
    catalog = _catalog(source)
    registry = ApplicationSkillBindingRegistry(store, now="2026-07-18T16:08:00+00:00")
    _bind(registry, catalog.get("document-method"), project_id="project-alpha")
    skills = ObjectStoreProjectSkillRepository(store)
    _save_skill(skills, _project_skill("project-alpha", "PROJECT-STRUCTURE"))
    requests = ObjectStoreModelRequestRepository(store)
    interrupted = CreateProjectDocumentModelRequest(
        project_skills=skills,
        model_requests=_FailOnceModelRequests(requests),
        application_skills=_runtime(tmp_path, store),
    )

    with pytest.raises(ValueError, match="interruption"):
        interrupted.execute(
            "project-alpha",
            brief="生成项目文档。",
            created_at="2026-07-18T16:09:00+08:00",
        )

    traces = tuple(store.list("application_skill_resolution_traces"))
    assert len(traces) == 1
    assert requests.list_requests("project-alpha") == ()

    replay = CreateProjectDocumentModelRequest(
        project_skills=skills,
        model_requests=requests,
        application_skills=_runtime(tmp_path, store),
    ).execute(
        "project-alpha",
        brief="生成项目文档。",
        created_at="2026-07-18T16:09:00+08:00",
    )

    assert replay.application_skill_resolution_id == traces[0]["resolution_id"]
    assert requests.get_request(replay.model_request_id) is not None
    assert len(tuple(store.list("application_skill_resolution_traces"))) == 1


def test_application_skill_failure_blocks_document_request_without_side_effects(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    skills = ObjectStoreProjectSkillRepository(store)
    _save_skill(skills, _project_skill("project-alpha", "PROJECT-STRUCTURE"))
    use_case = CreateProjectDocumentModelRequest(
        project_skills=skills,
        model_requests=ObjectStoreModelRequestRepository(store),
        application_skills=_UnexpectedRuntime(),
    )

    with pytest.raises(ProjectDocumentModelRequestError, match="resolution failed"):
        use_case.execute("project-alpha", brief="生成项目文档。")

    assert ObjectStoreModelRequestRepository(store).list_requests("project-alpha") == ()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "documents").exists()


def test_fingerprint_drift_falls_back_without_loading_changed_instructions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "skills"
    _write_application_skill(source, "document-method", "REVIEWED-DOCUMENT-METHOD")
    store = _store(tmp_path)
    catalog = _catalog(source)
    registry = ApplicationSkillBindingRegistry(store, now="2026-07-18T16:10:00+00:00")
    _bind(registry, catalog.get("document-method"), project_id="project-alpha")
    _write_application_skill(source, "document-method", "CHANGED-UNREVIEWED-METHOD")
    skills = ObjectStoreProjectSkillRepository(store)
    _save_skill(skills, _project_skill("project-alpha", "PROJECT-STRUCTURE"))

    result = _use_case(tmp_path, store).execute(
        "project-alpha",
        brief="生成项目文档。",
        created_at="2026-07-18T16:11:00+08:00",
    )
    request = ObjectStoreModelRequestRepository(store).get_request(result.model_request_id)
    trace = ObjectStoreApplicationSkillTraceRepository(store).get_trace(
        str(result.application_skill_resolution_id)
    )

    assert request is not None and trace is not None
    prompt = str(request["payload"]["content"])
    assert "REVIEWED-DOCUMENT-METHOD" not in prompt
    assert "CHANGED-UNREVIEWED-METHOD" not in prompt
    assert trace["selected"] == []
    assert trace["fallback"] == "default_consumer_flow"


def test_composition_reads_current_project_skill_authority_and_records_fallback(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    skills = ObjectStoreProjectSkillRepository(store)
    _save_skill(skills, _project_skill("project-alpha", "COMPOSED-PROJECT-STRUCTURE"))
    use_case = build_project_document_model_request(ROOT, runtime_root=tmp_path)

    result = use_case.execute(
        "project-alpha",
        brief="生成项目文档。",
        created_at="2026-07-18T16:07:00+08:00",
    )
    request = ObjectStoreModelRequestRepository(store).get_request(result.model_request_id)
    trace = ObjectStoreApplicationSkillTraceRepository(store).get_trace(
        str(result.application_skill_resolution_id)
    )

    assert request is not None and trace is not None
    assert "COMPOSED-PROJECT-STRUCTURE" in str(request["payload"]["content"])
    assert trace["consumer"] == "document.generate"
    assert trace["fallback"] == "default_consumer_flow"
    assert trace["selected"] == []
    assert not (tmp_path / "library").exists()
