from __future__ import annotations

import json
from pathlib import Path

from core.aggregate_repository_factory import AUTHORITY_DATABASE_NAME, TARGET_IDENTITY
from core.composition import build_project_memory_recall
from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository
from core.memory_core import (
    ObjectStoreMemoryStore,
    SQLiteMemoryReader,
    shared_trust_audit_activation_id,
    shared_trust_audit_activation_payload,
)
from core.product_core import (
    CreateProjectMemoryRecall,
    ObjectStorePersonaRepository,
    PersonaExtractor,
    SourceJobMemoryLoop,
)
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate, SQLiteProjectSkillRepository
from core.search_and_recall import ObjectStoreRecallRepository
from core.storage_provider import AggregateAuthorityEvidence, JsonObjectStore, SQLiteAggregateAuthorityStore, SQLiteStructuredRecordStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _fixture(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / "fixtures" / "project_skill" / name).read_text(encoding="utf-8"))


def _parts(tmp_path: Path) -> tuple[
    CreateProjectMemoryRecall,
    ObjectStoreProjectSkillRepository,
    ObjectStoreMemoryStore,
    ObjectStoreRecallRepository,
]:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    skills = ObjectStoreProjectSkillRepository(object_store)
    memory = ObjectStoreMemoryStore(object_store)
    recalls = ObjectStoreRecallRepository(object_store)
    return CreateProjectMemoryRecall(skills=skills, memory=memory, recalls=recalls), skills, memory, recalls


class _BudgetedRecallRepository:
    def __init__(
        self,
        repository: ObjectStoreRecallRepository,
        *,
        max_hits: int | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self._repository = repository
        self._max_hits = max_hits
        self._max_tokens = max_tokens

    def create_project_default_request(self, **kwargs: object) -> dict[str, object]:
        request = dict(self._repository.create_project_default_request(**kwargs))
        budget = dict(request["budget"])
        if self._max_hits is not None:
            budget["max_hits"] = self._max_hits
        if self._max_tokens is not None:
            budget["max_tokens"] = self._max_tokens
        request["budget"] = budget
        return request

    def save_result(self, result: dict[str, object]) -> dict[str, object]:
        return dict(self._repository.save_result(result))

    def create_insufficient_evidence_result(self, **kwargs: object) -> dict[str, object]:
        return dict(self._repository.create_insufficient_evidence_result(**kwargs))

    def get_request(self, request_id: str) -> dict[str, object] | None:
        request = self._repository.get_request(request_id)
        return dict(request) if request is not None else None

    def get_result(self, result_id: str) -> dict[str, object] | None:
        result = self._repository.get_result(result_id)
        return dict(result) if result is not None else None


def _save_skill(skills: ObjectStoreProjectSkillRepository, structured: dict[str, object]) -> dict[str, object]:
    return dict(
        skills.save(
            ProjectSkillUpdate(
                project_id=str(structured["project_id"]),
                markdown="# Alpha 项目 Skill\n\n沿用旧结构。",
                structured=structured,
                expected_revision=0,
                reason="test project memory recall",
            )
        )
    )


def _publish_project_memory(memory: ObjectStoreMemoryStore, *, project_id: str = "project-alpha") -> None:
    memory.publish(
        "atom",
        {
            "schema_version": "1.0.0",
            "id": f"atom-{project_id}",
            "source_id": f"source-{project_id}",
            "content": f"{project_id} 当前阶段优先关闭 Recall evidence selection。",
            "atom_type": "decision",
            "tags": ["recall", project_id],
            "confidence": 0.9,
            "source_refs": [
                {
                    "source_id": f"source-{project_id}",
                    "locator": "char:0-60",
                    "quote": "优先关闭 Recall evidence selection",
                }
            ],
            "revision": 1,
            "created_at": "2026-06-30T09:00:00+08:00",
            "updated_at": "2026-06-30T09:00:00+08:00",
            "trust_status": "system_generated",
        },
    )
    memory.publish(
        "scenario",
        {
            "schema_version": "1.0.0",
            "id": f"scenario-{project_id}",
            "title": f"{project_id} Recall 阶段",
            "summary": f"{project_id} 已具备 Skill-first 和 no-evidence，现在补同项目记忆证据选择。",
            "atom_ids": [f"atom-{project_id}"],
            "source_refs": [
                {
                    "source_id": f"source-{project_id}",
                    "locator": "char:0-80",
                }
            ],
            "tags": ["recall", project_id],
            "series_id": "series-rebuild",
            "project_id": project_id,
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "created_at": "2026-06-30T09:01:00+08:00",
            "updated_at": "2026-06-30T09:01:00+08:00",
            "trust_status": "system_generated",
        },
    )
    memory.publish(
        "series_memory",
        {
            "schema_version": "1.0.0",
            "id": f"series-memory-{project_id}",
            "series_id": "series-rebuild",
            "scope": "project",
            "overview": f"{project_id} 的 Rebuild 系列正在推进分层召回。",
            "scenario_ids": [f"scenario-{project_id}"],
            "source_refs": [
                {
                    "source_id": f"source-{project_id}",
                    "locator": "char:0-100",
                }
            ],
            "project_ids": [project_id],
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "created_at": "2026-06-30T09:02:00+08:00",
            "updated_at": "2026-06-30T09:02:00+08:00",
            "trust_status": "system_generated",
        },
    )


def _publish_large_series_memory(memory: ObjectStoreMemoryStore, *, project_id: str = "project-alpha") -> None:
    memory.publish(
        "series_memory",
        {
            "schema_version": "1.0.0",
            "id": f"series-memory-large-{project_id}",
            "series_id": "series-rebuild-large",
            "scope": "project",
            "overview": " ".join([f"{project_id} oversized recall context"] * 20),
            "scenario_ids": [f"scenario-{project_id}"],
            "source_refs": [
                {
                    "source_id": f"source-large-{project_id}",
                    "locator": "char:0-600",
                }
            ],
            "project_ids": [project_id],
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "created_at": "2026-06-30T08:59:00+08:00",
            "updated_at": "2026-06-30T08:59:00+08:00",
            "trust_status": "system_generated",
        },
    )


def test_project_memory_recall_returns_skill_then_same_project_memory_layers(
    tmp_path: Path,
) -> None:
    use_case, skills, memory, recalls = _parts(tmp_path)
    skill = _save_skill(skills, _fixture("valid-active-skill.json"))
    _publish_project_memory(memory, project_id="project-alpha")
    _publish_project_memory(memory, project_id="project-beta")

    result = use_case.execute(
        "project-alpha",
        query="这个项目下一版应该先改哪里？",
        created_at="2026-06-30T09:10:00+08:00",
    )
    request = recalls.get_request(result.request_id)
    recall_result = recalls.get_result(result.result_id)

    assert request is not None
    assert recall_result is not None
    assert validate_contract_instance("recall_request.schema.json", _schema("recall_request.schema.json"), request) == []
    assert validate_contract_instance("recall_result.schema.json", _schema("recall_result.schema.json"), recall_result) == []
    assert result.skill_id == skill["id"]
    assert result.hit_count == 4
    assert [hit["layer"] for hit in recall_result["hits"]] == [
        "l3_project_skill",
        "l3_series_memory",
        "l2_scenario",
        "l1_atom",
    ]
    assert recall_result["hits"][0]["object_id"] == skill["id"]
    assert all(hit["project_id"] == "project-alpha" for hit in recall_result["hits"])
    assert all("project-beta" not in hit["object_id"] for hit in recall_result["hits"])
    assert recall_result["coverage"]["covered_layers"] == [
        "l3_project_skill",
        "l3_series_memory",
        "l2_scenario",
        "l1_atom",
    ]
    assert recall_result["coverage"]["missing_layers"] == ["l4_persona", "l0_source"]
    assert recall_result["cross_project"] == {"used": False, "grant_id": None, "project_ids": []}
    assert "answer" not in recall_result
    assert not (tmp_path / "library").exists()


def test_project_memory_recall_includes_persona_as_first_hit(tmp_path: Path) -> None:
    """当 Persona 已确认时，Recall 应将 l4_persona 作为第一条命中插入。"""
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    skills = ObjectStoreProjectSkillRepository(object_store)
    memory = ObjectStoreMemoryStore(object_store)
    recalls = ObjectStoreRecallRepository(object_store)
    persona_repo = ObjectStorePersonaRepository(object_store)
    # 蒸馏并发布 Persona
    persona_repo.save(
        PersonaExtractor().extract(
            scope="global",
            confirmed_entries=[
                {
                    "schema_version": "1.0.0",
                    "id": "atom-persona-evidence",
                    "layer": "atom",
                    "project_id": "project-alpha",
                    "content": "已确认的 Atom。",
                    "source_refs": [{"source_id": "source-persona", "locator": "char:0-50"}],
                    "trust_status": "user_confirmed",
                    "revision": 1,
                    "created_at": "2026-06-30T09:00:00+08:00",
                    "updated_at": "2026-06-30T09:00:00+08:00",
                    "language_style": "克制、温柔",
                    "format_preferences": ["Markdown"],
                    "avoidances": ["不用 emoji"],
                }
            ],
        )
    )
    assert persona_repo.digest("global").ready is False
    persona_repo.update_confirmation(
        "global",
        status="confirmed",
        reason="用户确认稳定画像",
    )
    use_case = CreateProjectMemoryRecall(
        skills=skills,
        memory=memory,
        recalls=recalls,
        persona=persona_repo,
    )
    skill = _save_skill(skills, _fixture("valid-active-skill.json"))
    _publish_project_memory(memory, project_id="project-alpha")

    result = use_case.execute(
        "project-alpha",
        query="这个项目下一版应该先改哪里？",
        created_at="2026-06-30T09:10:00+08:00",
    )
    recall_result = recalls.get_result(result.result_id)

    assert recall_result is not None
    # l4_persona 应为第一条命中
    assert recall_result["hits"][0]["layer"] == "l4_persona"
    assert recall_result["hits"][0]["object_id"] == "persona-global"
    assert recall_result["hits"][0]["score"] == 0.95
    assert "克制、温柔" in recall_result["hits"][0]["snippet"]
    # l4_persona 应在 covered_layers 中，不在 missing_layers 中
    assert "l4_persona" in recall_result["coverage"]["covered_layers"]
    assert "l4_persona" not in recall_result["coverage"]["missing_layers"]
    # 总命中数应为 5（persona + skill + series + scenario + atom）
    assert result.hit_count == 5


def test_project_memory_recall_skips_persona_when_not_published(tmp_path: Path) -> None:
    """当 Persona reader 已配置但未确认 Persona 时，不应产生 l4_persona 命中。"""
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    skills = ObjectStoreProjectSkillRepository(object_store)
    memory = ObjectStoreMemoryStore(object_store)
    recalls = ObjectStoreRecallRepository(object_store)
    persona_repo = ObjectStorePersonaRepository(object_store)
    persona_repo.save(
        PersonaExtractor().extract(
            scope="global",
            confirmed_entries=[
                {
                    "schema_version": "1.0.0",
                    "id": "atom-persona-draft",
                    "layer": "atom",
                    "project_id": "project-alpha",
                    "content": "待确认的稳定画像来源。",
                    "source_refs": [{"source_id": "source-persona", "locator": "char:0-20"}],
                    "trust_status": "user_confirmed",
                    "revision": 1,
                    "created_at": "2026-06-30T09:00:00+08:00",
                    "updated_at": "2026-06-30T09:00:00+08:00",
                    "language_style": "先给结论",
                }
            ],
        )
    )
    use_case = CreateProjectMemoryRecall(
        skills=skills,
        memory=memory,
        recalls=recalls,
        persona=persona_repo,
    )
    _save_skill(skills, _fixture("valid-active-skill.json"))
    _publish_project_memory(memory, project_id="project-alpha")

    result = use_case.execute(
        "project-alpha",
        query="这个项目下一版应该先改哪里？",
        created_at="2026-06-30T09:10:00+08:00",
    )
    recall_result = recalls.get_result(result.result_id)

    assert recall_result is not None
    layers = [hit["layer"] for hit in recall_result["hits"]]
    assert "l4_persona" not in layers
    assert "l4_persona" in recall_result["coverage"]["missing_layers"]


def test_project_memory_recall_skips_stale_and_untrusted_memory(tmp_path: Path) -> None:
    use_case, skills, memory, recalls = _parts(tmp_path)
    _save_skill(skills, _fixture("valid-active-skill.json"))
    _publish_project_memory(memory, project_id="project-alpha")
    stale = dict(memory.get("scenario", "scenario-project-alpha") or {})
    stale["id"] = "scenario-stale-project-alpha"
    stale["stale"] = True
    stale["stale_reason"] = "source changed"
    memory.publish("scenario", stale)
    untrusted = dict(memory.get("atom", "atom-project-alpha") or {})
    untrusted["id"] = "atom-untrusted-project-alpha"
    untrusted["trust_status"] = "imported_unverified"
    memory.publish("atom", untrusted)

    result = use_case.execute(
        "project-alpha",
        query="这个项目下一版应该先改哪里？",
        created_at="2026-06-30T09:10:00+08:00",
    )
    recall_result = recalls.get_result(result.result_id)

    assert recall_result is not None
    object_ids = [hit["object_id"] for hit in recall_result["hits"]]
    assert "scenario-stale-project-alpha" not in object_ids
    assert "atom-untrusted-project-alpha" not in object_ids


def test_project_memory_recall_records_budget_truncation_dropped_hit_ids(
    tmp_path: Path,
) -> None:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    skills = ObjectStoreProjectSkillRepository(object_store)
    memory = ObjectStoreMemoryStore(object_store)
    recalls = ObjectStoreRecallRepository(object_store)
    use_case = CreateProjectMemoryRecall(
        skills=skills,
        memory=memory,
        recalls=_BudgetedRecallRepository(recalls, max_hits=3),
    )
    _save_skill(skills, _fixture("valid-active-skill.json"))
    _publish_project_memory(memory, project_id="project-alpha")

    result = use_case.execute(
        "project-alpha",
        query="这个项目下一版应该先改哪里？",
        created_at="2026-06-30T11:30:00+08:00",
    )
    recall_result = recalls.get_result(result.result_id)

    assert recall_result is not None
    assert validate_contract_instance("recall_result.schema.json", _schema("recall_result.schema.json"), recall_result) == []
    assert result.hit_count == 3
    assert [hit["layer"] for hit in recall_result["hits"]] == [
        "l3_project_skill",
        "l3_series_memory",
        "l2_scenario",
    ]
    truncation = recall_result["truncation"]
    assert truncation["applied"] is True
    assert truncation["reason"] == "hit_limit"
    assert len(truncation["dropped_hit_ids"]) == 1
    assert truncation["dropped_hit_ids"][0] not in [hit["hit_id"] for hit in recall_result["hits"]]
    assert truncation["final_hit_count"] == 3
    assert truncation["final_token_estimate"] == sum(hit["token_estimate"] for hit in recall_result["hits"])
    assert recall_result["explanation"]["warnings"] == ["legacy_fallback_package"]
    assert not (tmp_path / "library").exists()


def test_project_memory_recall_records_token_limit_truncation_dropped_hit_ids(
    tmp_path: Path,
) -> None:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    skills = ObjectStoreProjectSkillRepository(object_store)
    memory = ObjectStoreMemoryStore(object_store)
    recalls = ObjectStoreRecallRepository(object_store)
    max_tokens = 170
    use_case = CreateProjectMemoryRecall(
        skills=skills,
        memory=memory,
        recalls=_BudgetedRecallRepository(recalls, max_tokens=max_tokens),
    )
    _save_skill(skills, _fixture("valid-active-skill.json"))
    _publish_project_memory(memory, project_id="project-alpha")

    result = use_case.execute(
        "project-alpha",
        query="这个项目下一版应该先改哪里？",
        created_at="2026-06-30T11:40:00+08:00",
    )
    recall_result = recalls.get_result(result.result_id)

    assert recall_result is not None
    assert validate_contract_instance("recall_result.schema.json", _schema("recall_result.schema.json"), recall_result) == []
    assert result.hit_count >= 1
    truncation = recall_result["truncation"]
    selected_hit_ids = [hit["hit_id"] for hit in recall_result["hits"]]
    assert truncation["applied"] is True
    assert truncation["reason"] == "token_limit"
    assert truncation["dropped_hit_ids"]
    assert all(hit_id not in selected_hit_ids for hit_id in truncation["dropped_hit_ids"])
    assert truncation["final_hit_count"] == len(recall_result["hits"])
    assert truncation["final_token_estimate"] == sum(hit["token_estimate"] for hit in recall_result["hits"])
    assert truncation["final_token_estimate"] <= max_tokens
    assert recall_result["explanation"]["warnings"] == ["legacy_fallback_package"]
    assert not (tmp_path / "library").exists()


def test_project_memory_recall_records_combined_budget_truncation_reason(
    tmp_path: Path,
) -> None:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    skills = ObjectStoreProjectSkillRepository(object_store)
    memory = ObjectStoreMemoryStore(object_store)
    recalls = ObjectStoreRecallRepository(object_store)
    max_tokens = 170
    use_case = CreateProjectMemoryRecall(
        skills=skills,
        memory=memory,
        recalls=_BudgetedRecallRepository(recalls, max_hits=3, max_tokens=max_tokens),
    )
    _save_skill(skills, _fixture("valid-active-skill.json"))
    _publish_project_memory(memory, project_id="project-alpha")
    _publish_large_series_memory(memory, project_id="project-alpha")

    result = use_case.execute(
        "project-alpha",
        query="这个项目下一版应该先改哪里？",
        created_at="2026-06-30T11:50:00+08:00",
    )
    recall_result = recalls.get_result(result.result_id)

    assert recall_result is not None
    assert validate_contract_instance("recall_result.schema.json", _schema("recall_result.schema.json"), recall_result) == []
    selected_hit_ids = [hit["hit_id"] for hit in recall_result["hits"]]
    selected_object_ids = [hit["object_id"] for hit in recall_result["hits"]]
    truncation = recall_result["truncation"]
    assert result.hit_count == 3
    assert truncation["applied"] is True
    assert truncation["reason"] == "budget"
    assert len(truncation["dropped_hit_ids"]) == 2
    assert all(hit_id not in selected_hit_ids for hit_id in truncation["dropped_hit_ids"])
    assert "series-memory-large-project-alpha" not in selected_object_ids
    assert "atom-project-alpha" not in selected_object_ids
    assert "scenario-project-alpha" in selected_object_ids
    assert truncation["final_hit_count"] == len(recall_result["hits"])
    assert truncation["final_token_estimate"] == sum(hit["token_estimate"] for hit in recall_result["hits"])
    assert truncation["final_token_estimate"] <= max_tokens
    assert recall_result["explanation"]["warnings"] == ["legacy_fallback_package"]
    assert not (tmp_path / "library").exists()


def test_project_memory_recall_composition_uses_runtime_memory_loop(tmp_path: Path) -> None:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    skills = ObjectStoreProjectSkillRepository(object_store)
    skill = _save_skill(skills, _fixture("valid-active-skill.json"))
    sources = ObjectStoreSourceRegistrar(object_store)
    jobs = ObjectStoreJobRepository(object_store)
    memory = ObjectStoreMemoryStore(object_store)
    loop = SourceJobMemoryLoop(
        source_registrar=sources,
        job_repository=jobs,
        memory_reader=memory,
        memory_writer=memory,
    )
    loop.run_text(
        title="Project alpha recall source",
        content="Alpha recall should use same-project memory.\nIt must stay evidence-only.",
        series_id="series-rebuild",
        project_id="project-alpha",
    )
    use_case = build_project_memory_recall(ROOT, runtime_root=tmp_path)

    result = use_case.execute(
        str(skill["project_id"]),
        query="这个项目下一版应该先改哪里？",
        created_at="2026-06-30T09:10:00+08:00",
    )
    recalls = ObjectStoreRecallRepository(object_store)
    recall_result = recalls.get_result(result.result_id)

    assert recall_result is not None
    assert recall_result["hits"][0]["layer"] == "l3_project_skill"
    assert "l3_series_memory" in [hit["layer"] for hit in recall_result["hits"]]
    assert "l2_scenario" in [hit["layer"] for hit in recall_result["hits"]]
    assert "l1_atom" in [hit["layer"] for hit in recall_result["hits"]]
    assert not (tmp_path / "library").exists()


def test_project_memory_recall_composition_uses_one_sqlite_compound_authority(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    members = ("memory_atoms", "memory_publications", "memory_scenarios", "memory_series_memory", "memory_transitions", "project_skills")
    evidence = AggregateAuthorityEvidence("composition-recall-v1", "a" * 64, "b" * 64, TARGET_IDENTITY)
    with records.begin() as transaction:
        for member in members:
            transaction.put("aggregate_authority_targets", f"default~{member}", {"namespace_id": "default", "aggregate": member, "migration_id": evidence.migration_id, "source_fingerprint": evidence.source_fingerprint, "target_fingerprint": evidence.target_fingerprint, "target_identity": evidence.target_identity}, expected_revision=0)
        transaction.put("aggregate_authority_compound_activations", shared_trust_audit_activation_id("default"), shared_trust_audit_activation_payload(namespace_id="default", target_identity=TARGET_IDENTITY, activation_id="composition-recall-v1", member_migrations={member: evidence.migration_id for member in members}, source_fingerprint=evidence.source_fingerprint, target_fingerprint=evidence.target_fingerprint, activated_at="2026-07-12T23:00:00+08:00"), expected_revision=0)
        transaction.commit()
    for member in members:
        initial = authority.create_json_active(namespace_id="default", aggregate=member, reason="test initial")
        staged = authority.transition(namespace_id="default", aggregate=member, expected_revision=initial.revision, to_state="sqlite_staged", evidence=evidence, reason="test staged")
        authority.transition(namespace_id="default", aggregate=member, expected_revision=staged.revision, to_state="sqlite_active", reason="test active")

    use_case = build_project_memory_recall(ROOT, runtime_root=tmp_path)

    assert isinstance(use_case._memory, SQLiteMemoryReader)
    assert isinstance(use_case._skills, SQLiteProjectSkillRepository)


def test_query_driven_layers_keep_atoms_authoritative_without_other_lanes(
    tmp_path: Path,
) -> None:
    use_case, skills, memory, recalls = _parts(tmp_path)
    _save_skill(skills, _fixture("valid-active-skill.json"))
    _publish_project_memory(memory, project_id="project-alpha")
    memory.publish(
        "atom",
        {
            "schema_version": "1.0.0",
            "id": "atom-project-alpha-release-date",
            "source_id": "source-project-alpha-release",
            "content": "正式发布日期是 2026 年 8 月 15 日。",
            "atom_type": "fact",
            "tags": ["release", "date"],
            "confidence": 0.98,
            "source_refs": [
                {
                    "source_id": "source-project-alpha-release",
                    "locator": "section:release-date",
                }
            ],
            "revision": 1,
            "created_at": "2026-07-29T09:00:00+08:00",
            "updated_at": "2026-07-29T09:00:00+08:00",
            "trust_status": "user_confirmed",
        },
    )

    result = use_case.execute(
        "project-alpha",
        query="正式发布日期是什么",
        layers=("l1_atom",),
        created_at="2026-07-29T09:10:00+08:00",
    )
    recall_result = recalls.get_result(result.result_id)

    assert recall_result is not None
    assert {hit["layer"] for hit in recall_result["hits"]} == {"l1_atom"}
    assert recall_result["hits"]
    assert recall_result["coverage"]["requested_layers"] == ["l1_atom"]


def test_atom_only_plan_does_not_read_project_skill_authority(
    tmp_path: Path,
) -> None:
    class FailingSkills:
        def load(self, _project_id: str):
            raise AssertionError("Project Skill authority must not be read")

    object_store = JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )
    memory = ObjectStoreMemoryStore(object_store)
    _publish_project_memory(memory, project_id="project-alpha")
    result = CreateProjectMemoryRecall(
        skills=FailingSkills(),
        memory=memory,
        recalls=ObjectStoreRecallRepository(object_store),
    ).execute(
        "project-alpha",
        query="Recall 决策是什么",
        layers=("l1_atom",),
        created_at="2026-07-29T09:20:00+08:00",
    )

    assert result.skill_id == ""
    assert result.hit_count == 1
    assert result.evidence_hits[0]["layer"] == "l1_atom"
