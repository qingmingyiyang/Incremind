from __future__ import annotations

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
from core.composition import build_answer_model_request_from_recall
from core.model_gateway import ObjectStoreModelRequestRepository
from core.product_core import AnswerModelRequestError, CreateAnswerModelRequestFromRecallResult
from core.search_and_recall import ObjectStoreRecallRepository
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _write_skill(source: Path, skill_id: str, *, body: str) -> None:
    root = source / skill_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        f"description: Use {skill_id} for architecture review work.\n"
        "---\n\n"
        f"{body}",
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
    term: str = "架构审计",
) -> None:
    preview = registry.preview_bind(
        package,
        project_id=project_id,
        allowed_consumers=("answer.model-request",),
        priority=700,
        trigger_terms=(term,),
    )
    registry.activate(
        package,
        project_id=project_id,
        allowed_consumers=("answer.model-request",),
        priority=700,
        trigger_terms=(term,),
        expected_registry_revision=preview["registry_revision"],
        preview_token=preview["preview_token"],
        confirm=True,
        reason="Bind the reviewed answer method.",
    )


def _evidence(recalls: ObjectStoreRecallRepository, project_id: str, query: str):
    request = recalls.create_project_default_request(
        project_id=project_id,
        query=query,
        project_skill_id=f"skill-{project_id}",
        created_at="2026-07-18T15:00:00+08:00",
    )
    return recalls.save_result(
        {
            "schema_version": "1.0.0",
            "id": f"recall-result-{project_id}",
            "request_id": request["id"],
            "project_id": project_id,
            "status": "evidence_found",
            "hits": [
                {
                    "hit_id": f"hit-{project_id}",
                    "layer": "l3_project_skill",
                    "object_id": f"skill-{project_id}",
                    "project_id": project_id,
                    "source_project_label": None,
                    "trust_status": "user_confirmed",
                    "score": 0.99,
                    "token_estimate": 100,
                    "source_refs": [
                        {
                            "source_id": f"source-{project_id}",
                            "locator": "char:0-80",
                        }
                    ],
                    "snippet": f"{project_id} 已发布的项目规则与证据。",
                    "explanation": "Project Skill evidence.",
                }
            ],
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
                "final_token_estimate": 100,
            },
            "explanation": {
                "summary": "Evidence ready.",
                "layer_order": request["layers"],
                "warnings": [],
            },
            "cross_project": {"used": False, "grant_id": None, "project_ids": []},
            "errors": [],
            "created_at": "2026-07-18T15:01:00+08:00",
        }
    )


def _runtime(tmp_path: Path, store: JsonObjectStore, *, now: str):
    trace_repository = ObjectStoreApplicationSkillTraceRepository(store)
    return ApplicationSkillConsumerRuntime(
        catalog=ApplicationSkillCatalog(),
        sources=(ApplicationSkillSource("user", tmp_path / "skills", "user"),),
        resolver=ApplicationSkillResolver(
            ApplicationSkillBindingRegistry(store, now=now),
            trace_store=trace_repository,
            now=now,
        ),
    )


def test_two_projects_with_same_question_receive_isolated_skill_contexts(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, "alpha-review", body="# ALPHA-ONLY-METHOD\n\nUse Alpha evidence order.\n")
    _write_skill(source, "beta-review", body="# BETA-ONLY-METHOD\n\nUse Beta decision matrix.\n")
    catalog = _catalog(source)
    store = _store(tmp_path)
    registry = ApplicationSkillBindingRegistry(store, now="2026-07-18T15:02:00+00:00")
    _bind(registry, catalog.get("alpha-review"), project_id="project-alpha")
    _bind(registry, catalog.get("beta-review"), project_id="project-beta")
    recalls = ObjectStoreRecallRepository(store)
    query = "请对当前方案进行架构审计。"
    alpha_recall = _evidence(recalls, "project-alpha", query)
    beta_recall = _evidence(recalls, "project-beta", query)
    use_case = build_answer_model_request_from_recall(ROOT, runtime_root=tmp_path)

    alpha = use_case.execute(str(alpha_recall["id"]), created_at="2026-07-18T15:03:00+08:00")
    beta = use_case.execute(str(beta_recall["id"]), created_at="2026-07-18T15:04:00+08:00")
    alpha_replay = build_answer_model_request_from_recall(ROOT, runtime_root=tmp_path).execute(
        str(alpha_recall["id"]), created_at="2026-07-18T15:03:00+08:00"
    )
    requests = ObjectStoreModelRequestRepository(store)
    alpha_request = requests.get_request(alpha.model_request_id)
    beta_request = requests.get_request(beta.model_request_id)
    assert alpha_request is not None and beta_request is not None
    alpha_prompt = str(alpha_request["payload"]["content"])
    beta_prompt = str(beta_request["payload"]["content"])

    assert "ALPHA-ONLY-METHOD" in alpha_prompt and "BETA-ONLY-METHOD" not in alpha_prompt
    assert "BETA-ONLY-METHOD" in beta_prompt and "ALPHA-ONLY-METHOD" not in beta_prompt
    for prompt, marker in (
        (alpha_prompt, "ALPHA-ONLY-METHOD"),
        (beta_prompt, "BETA-ONLY-METHOD"),
    ):
        assert prompt.index("请只根据以下已召回证据") < prompt.index("# Application Skill Context")
        assert prompt.index("# Application Skill Context") < prompt.index(marker)
        assert prompt.index(marker) < prompt.index("用户问题：") < prompt.index("召回证据：")

    assert alpha.application_skill_resolution_id != beta.application_skill_resolution_id
    assert alpha_replay.model_request_id == alpha.model_request_id
    assert alpha_replay.application_skill_resolution_id == alpha.application_skill_resolution_id
    assert len(requests.list_requests("project-alpha")) == 1
    trace_repository = ObjectStoreApplicationSkillTraceRepository(store)
    alpha_trace = trace_repository.get_trace(str(alpha.application_skill_resolution_id))
    beta_trace = trace_repository.get_trace(str(beta.application_skill_resolution_id))
    assert alpha_trace is not None and beta_trace is not None
    assert alpha_trace["invocation_id"] == alpha.model_request_id
    assert beta_trace["invocation_id"] == beta.model_request_id
    assert [item["skill_id"] for item in alpha_trace["selected"]] == ["alpha-review"]
    assert [item["skill_id"] for item in beta_trace["selected"]] == ["beta-review"]
    assert "ALPHA-ONLY-METHOD" not in json.dumps(alpha_trace)
    assert "BETA-ONLY-METHOD" not in json.dumps(beta_trace)
    assert len(tuple(store.list("application_skill_resolution_traces"))) == 2

    for request, result, recall in (
        (alpha_request, alpha, alpha_recall),
        (beta_request, beta, beta_recall),
    ):
        assert request["provider_preference"]["allow_remote"] is False
        assert request["provider_preference"]["mode"] == "local_only"
        assert request["privacy"]["allow_remote"] is False
        assert request["budget"]["max_cost_usd"] == 0
        assert request["payload"]["source_refs"] == recall["hits"][0]["source_refs"]
        skill_refs = [
            item
            for item in request["payload"]["input_refs"]
            if item["kind"] == "application_skill_resolution"
        ]
        assert skill_refs == [
            {
                "kind": "application_skill_resolution",
                "object_id": result.application_skill_resolution_id,
                "uri": (
                    "crp://default/application-skill-resolutions/"
                    f"{result.application_skill_resolution_id}.json"
                ),
            }
        ]
        schema = json.loads((CONTRACT_ROOT / "model_request.schema.json").read_text(encoding="utf-8"))
        assert validate_contract_instance("model_request.schema.json", schema, request) == []


def test_unmatched_composed_answer_records_fallback_without_changing_prompt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recalls = ObjectStoreRecallRepository(store)
    recall = _evidence(recalls, "project-alpha", "没有匹配方法的普通问题。")

    result = build_answer_model_request_from_recall(ROOT, runtime_root=tmp_path).execute(
        str(recall["id"]),
        created_at="2026-07-18T15:05:00+08:00",
    )
    request = ObjectStoreModelRequestRepository(store).get_request(result.model_request_id)
    trace = ObjectStoreApplicationSkillTraceRepository(store).get_trace(
        str(result.application_skill_resolution_id)
    )

    assert request is not None and trace is not None
    assert "# Application Skill Context" not in request["payload"]["content"]
    assert trace["selected"] == []
    assert trace["fallback"] == "default_consumer_flow"
    assert request["payload"]["input_refs"][-1]["object_id"] == trace["resolution_id"]


def test_fingerprint_drift_falls_back_without_loading_changed_body(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, "drift-review", body="# ORIGINAL-METHOD\n")
    catalog = _catalog(source)
    store = _store(tmp_path)
    registry = ApplicationSkillBindingRegistry(store, now="2026-07-18T15:06:00+00:00")
    _bind(registry, catalog.get("drift-review"), project_id="project-alpha")
    _write_skill(source, "drift-review", body="# CHANGED-MUST-NOT-LOAD\n")
    recalls = ObjectStoreRecallRepository(store)
    recall = _evidence(recalls, "project-alpha", "请进行架构审计。")

    result = build_answer_model_request_from_recall(ROOT, runtime_root=tmp_path).execute(
        str(recall["id"]),
        created_at="2026-07-18T15:07:00+08:00",
    )
    request = ObjectStoreModelRequestRepository(store).get_request(result.model_request_id)
    trace = ObjectStoreApplicationSkillTraceRepository(store).get_trace(
        str(result.application_skill_resolution_id)
    )

    assert request is not None and trace is not None
    assert "ORIGINAL-METHOD" not in request["payload"]["content"]
    assert "CHANGED-MUST-NOT-LOAD" not in request["payload"]["content"]
    assert trace["fallback"] == "default_consumer_flow"


def test_skill_resolution_failure_prevents_model_request_persistence(tmp_path: Path) -> None:
    class FailingRuntime:
        def resolve_context(self, **_kwargs):
            raise RuntimeError("controlled resolver failure with private detail")

    store = _store(tmp_path)
    recalls = ObjectStoreRecallRepository(store)
    recall = _evidence(recalls, "project-alpha", "请进行架构审计。")
    model_requests = ObjectStoreModelRequestRepository(store)
    use_case = CreateAnswerModelRequestFromRecallResult(
        recalls=recalls,
        model_requests=model_requests,
        application_skills=FailingRuntime(),
    )

    with pytest.raises(AnswerModelRequestError, match="Application Skill resolution failed") as raised:
        use_case.execute(str(recall["id"]))

    assert "private detail" not in str(raised.value)
    assert model_requests.list_requests("project-alpha") == ()


def test_skill_runtime_cannot_replace_missing_recall_evidence(tmp_path: Path) -> None:
    class RecordingRuntime:
        calls = 0

        def resolve_context(self, **_kwargs):
            self.calls += 1
            return {}

    store = _store(tmp_path)
    recalls = ObjectStoreRecallRepository(store)
    request = recalls.create_project_default_request(
        project_id="project-alpha",
        query="请进行架构审计。",
        project_skill_id="skill-project-alpha",
    )
    insufficient = recalls.create_insufficient_evidence_result(request_id=str(request["id"]))
    runtime = RecordingRuntime()
    model_requests = ObjectStoreModelRequestRepository(store)
    use_case = CreateAnswerModelRequestFromRecallResult(
        recalls=recalls,
        model_requests=model_requests,
        application_skills=runtime,
    )

    with pytest.raises(AnswerModelRequestError, match="not evidence-bearing"):
        use_case.execute(str(insufficient["id"]))

    assert runtime.calls == 0
    assert model_requests.list_requests("project-alpha") == ()


def test_invalid_skill_resolution_identity_cannot_enter_model_request_ref(tmp_path: Path) -> None:
    class InvalidRuntime:
        def resolve_context(self, **kwargs):
            return {
                "resolution_id": "skill-resolution-../../escape",
                "project_id": kwargs["project_id"],
                "consumer": kwargs["consumer"],
                "context_markdown": "",
                "selected": [],
                "fallback": "default_consumer_flow",
            }

    store = _store(tmp_path)
    recalls = ObjectStoreRecallRepository(store)
    recall = _evidence(recalls, "project-alpha", "请进行架构审计。")
    model_requests = ObjectStoreModelRequestRepository(store)

    with pytest.raises(AnswerModelRequestError, match="resolution id is invalid"):
        CreateAnswerModelRequestFromRecallResult(
            recalls=recalls,
            model_requests=model_requests,
            application_skills=InvalidRuntime(),
        ).execute(str(recall["id"]))

    assert model_requests.list_requests("project-alpha") == ()


def test_trace_first_request_failure_can_replay_with_original_trace_timestamp(tmp_path: Path) -> None:
    class FailingRequests:
        def save_request(self, _request):
            raise RuntimeError("controlled request persistence failure")

    source = tmp_path / "skills"
    _write_skill(source, "replay-review", body="# REPLAY-METHOD\n")
    catalog = _catalog(source)
    store = _store(tmp_path)
    registry = ApplicationSkillBindingRegistry(store, now="2026-07-18T15:08:00+00:00")
    _bind(registry, catalog.get("replay-review"), project_id="project-alpha")
    recalls = ObjectStoreRecallRepository(store)
    recall = _evidence(recalls, "project-alpha", "请进行架构审计。")
    first_runtime = _runtime(tmp_path, store, now="2026-07-18T15:09:00+00:00")
    first = CreateAnswerModelRequestFromRecallResult(
        recalls=recalls,
        model_requests=FailingRequests(),
        application_skills=first_runtime,
    )

    with pytest.raises(RuntimeError, match="request persistence failure"):
        first.execute(str(recall["id"]), created_at="2026-07-18T15:09:00+08:00")

    stored_traces = store.list("application_skill_resolution_traces")
    assert len(stored_traces) == 1
    first_trace = dict(stored_traces[0])
    second_runtime = _runtime(tmp_path, store, now="2026-07-18T15:10:00+00:00")
    replay = CreateAnswerModelRequestFromRecallResult(
        recalls=recalls,
        model_requests=ObjectStoreModelRequestRepository(store),
        application_skills=second_runtime,
    ).execute(str(recall["id"]), created_at="2026-07-18T15:10:00+08:00")
    replay_trace = ObjectStoreApplicationSkillTraceRepository(store).get_trace(
        str(replay.application_skill_resolution_id)
    )

    assert replay_trace is not None
    assert replay_trace["resolution_id"] == first_trace["resolution_id"]
    assert replay_trace["recorded_at"] == first_trace["recorded_at"]
    assert ObjectStoreModelRequestRepository(store).get_request(replay.model_request_id) is not None
