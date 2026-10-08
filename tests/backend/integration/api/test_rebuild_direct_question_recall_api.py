from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from fastapi.testclient import TestClient

from core.memory_core import (
    ObjectStoreMemoryCandidateRepository,
    ObjectStoreMemoryStore,
    build_manual_publication_context,
)
from core.aggregate_repository_factory import AUTHORITY_DATABASE_NAME, STRUCTURED_DATABASE_NAME, TARGET_IDENTITY
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate, SQLiteProjectSkillRepository
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
)


ROOT = Path(__file__).resolve().parents[4]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _skill_fixture(*, source_id: str = "source-default") -> dict[str, object]:
    payload = json.loads(
        (CONTRACT_ROOT / "fixtures" / "project_skill" / "valid-active-skill.json").read_text(
            encoding="utf-8"
        )
    )
    # Adapt fixture to the "default" project so it matches the route's recall_project_id.
    payload["project_id"] = "default"
    payload["id"] = "skill-default"
    payload["name"] = "默认项目 Skill"
    payload["purpose"] = "固定默认项目的目标和输出结构，用于直接问答系列召回。"
    payload["markdown_uri"] = "crp://default/projects/default/project-skill.md"
    payload["json_uri"] = "crp://default/projects/default/project-skill.json"
    source_refs = [{"source_id": source_id, "locator": "source:metadata"}]
    payload["source_refs"] = source_refs
    payload["evidence_refs"] = source_refs
    for rule in payload.get("output_rules", []):
        if isinstance(rule, dict):
            rule["source_refs"] = source_refs
    return payload


def _save_default_skill(store: JsonObjectStore, *, source_id: str = "source-default") -> dict[str, object]:
    skills = ObjectStoreProjectSkillRepository(store)
    structured = _skill_fixture(source_id=source_id)
    return dict(
        skills.save(
            ProjectSkillUpdate(
                project_id="default",
                markdown="# 默认项目 Skill\n\n用于直接问答召回测试。",
                structured=structured,
                expected_revision=0,
                reason="test direct question recall",
            )
        )
    )


def _save_project_skill(
    store: JsonObjectStore,
    *,
    project_id: str,
    source_id: str,
) -> dict[str, object]:
    skills = ObjectStoreProjectSkillRepository(store)
    structured = _skill_fixture(source_id=source_id)
    structured["project_id"] = project_id
    structured["id"] = f"skill-{project_id}"
    structured["name"] = f"{project_id} 项目 Skill"
    structured["markdown_uri"] = (
        f"crp://default/projects/{project_id}/project-skill.md"
    )
    structured["json_uri"] = (
        f"crp://default/projects/{project_id}/project-skill.json"
    )
    return dict(
        skills.save(
            ProjectSkillUpdate(
                project_id=project_id,
                markdown=f"# {project_id} 项目 Skill\n\n用于全局项目路由测试。",
                structured=structured,
                expected_revision=0,
                reason="test global project routing",
            )
        )
    )


def _save_sqlite_project_skill(
    records: SQLiteStructuredRecordStore,
    *,
    project_id: str,
    source_id: str,
) -> dict[str, object]:
    skills = SQLiteProjectSkillRepository(records)
    structured = _skill_fixture(source_id=source_id)
    structured["project_id"] = project_id
    structured["id"] = f"skill-{project_id}"
    structured["name"] = f"{project_id} 项目 Skill"
    structured["markdown_uri"] = (
        f"crp://default/projects/{project_id}/project-skill.md"
    )
    structured["json_uri"] = (
        f"crp://default/projects/{project_id}/project-skill.json"
    )
    return dict(
        skills.save(
            ProjectSkillUpdate(
                project_id=project_id,
                markdown=f"# {project_id} 项目 Skill\n\n用于全局项目路由测试。",
                structured=structured,
                expected_revision=0,
                reason="test global project routing",
            )
        )
    )


def _project_series_payloads(
    *,
    project_id: str,
    series_id: str,
    source_id: str,
    overview: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    source_refs = [{"source_id": source_id, "locator": "source:metadata"}]
    atom_id = f"atom-{project_id}"
    scenario_id = f"scenario-{project_id}"
    return (
        {
            "schema_version": "1.0.0",
            "id": atom_id,
            "source_id": source_id,
            "content": overview,
            "atom_type": "fact",
            "tags": [project_id],
            "confidence": 0.9,
            "source_refs": source_refs,
            "revision": 1,
            "created_at": "2026-07-29T09:00:00+08:00",
            "updated_at": "2026-07-29T09:00:00+08:00",
            "trust_status": "user_confirmed",
        },
        {
            "schema_version": "1.0.0",
            "id": scenario_id,
            "title": f"{series_id} 场景",
            "summary": overview,
            "atom_ids": [atom_id],
            "source_refs": source_refs,
            "tags": [project_id],
            "series_id": series_id,
            "project_id": project_id,
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "created_at": "2026-07-29T09:01:00+08:00",
            "updated_at": "2026-07-29T09:01:00+08:00",
            "trust_status": "user_confirmed",
        },
        {
            "schema_version": "1.0.0",
            "id": f"series-memory-{project_id}",
            "series_id": series_id,
            "scope": "project",
            "overview": overview,
            "scenario_ids": [scenario_id],
            "source_refs": source_refs,
            "project_ids": [project_id],
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "created_at": "2026-07-29T09:02:00+08:00",
            "updated_at": "2026-07-29T09:02:00+08:00",
            "trust_status": "user_confirmed",
        },
    )


def _publish_project_series(
    store: JsonObjectStore,
    *,
    project_id: str,
    series_id: str,
    source_id: str,
    overview: str,
) -> None:
    memory = ObjectStoreMemoryStore(store)
    for layer, payload in zip(
        ("atom", "scenario", "series_memory"),
        _project_series_payloads(
            project_id=project_id,
            series_id=series_id,
            source_id=source_id,
            overview=overview,
        ),
        strict=True,
    ):
        memory.publish(layer, payload)


def _publish_project_series_sqlite(
    records: SQLiteStructuredRecordStore,
    *,
    project_id: str,
    series_id: str,
    source_id: str,
    overview: str,
) -> None:
    payloads = _project_series_payloads(
        project_id=project_id,
        series_id=series_id,
        source_id=source_id,
        overview=overview,
    )
    with records.begin() as transaction:
        for collection, payload in zip(
            (
                "memory_atoms",
                "memory_scenarios",
                "memory_series_memory",
            ),
            payloads,
            strict=True,
        ):
            transaction.put(
                collection,
                str(payload["id"]),
                payload,
                expected_revision=0,
            )
        transaction.commit()


def _review_candidate(
    candidate_id: str,
    target_layer: str,
    content: str,
    source_id: str,
) -> dict[str, object]:
    timestamp = "2026-07-04T09:00:00+08:00"
    return {
        "schema_version": "1.0.0",
        "id": candidate_id,
        "project_id": "default",
        "target_layer": target_layer,
        "candidate_type": "answer_summary",
        "status": "pending_review",
        "proposed_content": content,
        "source_refs": [{"source_id": source_id, "locator": "source:metadata"}],
        "provenance": {
            "model_result_id": None,
            "model_request_id": None,
            "recall_result_id": None,
            "document_id": None,
            "document_revision": None,
            "source_content_read_id": f"content-read-{source_id}",
            "input_refs": [
                {
                    "kind": "source",
                    "object_id": source_id,
                    "uri": f"crp://default/sources/{source_id}.json",
                },
                {
                    "kind": "source_content_read",
                    "object_id": f"content-read-{source_id}",
                    "uri": f"crp://default/source-content-reads/content-read-{source_id}.json",
                },
            ],
        },
        "review": {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
            "reason": "等待用户确认。",
            "reviewed_by": None,
            "reviewed_at": None,
        },
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _publish_default_memory(
    store: JsonObjectStore,
    *,
    source_id: str = "source-default",
    stage_series: bool = False,
) -> str | None:
    memory = ObjectStoreMemoryStore(store)
    source_refs = [{"source_id": source_id, "locator": "source:metadata"}]
    memory.publish(
        "atom",
        {
            "schema_version": "1.0.0",
            "id": "atom-default",
            "source_id": source_id,
            "content": "默认项目当前阶段优先推进直接问答系列召回。",
            "atom_type": "decision",
            "tags": ["recall", "default"],
            "confidence": 0.9,
            "source_refs": source_refs,
            "revision": 1,
            "created_at": "2026-07-04T09:00:00+08:00",
            "updated_at": "2026-07-04T09:00:00+08:00",
            "trust_status": "system_generated",
        },
    )
    memory.publish(
        "scenario",
        {
            "schema_version": "1.0.0",
            "id": "scenario-default",
            "title": "默认项目召回场景",
            "summary": "默认项目通过已确认事实形成直接问答场景。",
            "atom_ids": ["atom-default"],
            "source_refs": source_refs,
            "tags": ["recall", "default"],
            "series_id": "series-default",
            "project_id": "default",
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "created_at": "2026-07-04T09:01:00+08:00",
            "updated_at": "2026-07-04T09:01:00+08:00",
            "trust_status": "user_confirmed",
        },
    )
    series = {
        "schema_version": "1.0.0",
        "id": "series-memory-default",
        "series_id": "series-default",
        "scope": "project",
        "overview": "默认项目的系列总览，用于直接问答上下文召回。",
        "scenario_ids": ["scenario-default"],
        "source_refs": source_refs,
        "project_ids": ["default"],
        "stale": False,
        "stale_reason": None,
        "revision": 1,
        "created_at": "2026-07-04T09:02:00+08:00",
        "updated_at": "2026-07-04T09:02:00+08:00",
        "trust_status": "system_generated",
    }
    if stage_series:
        context = build_manual_publication_context(
            namespace_id="default",
            layer="series_memory",
            draft_id="series-memory-default",
            candidate_id="candidate-series-default",
            reviewed_at="2026-07-04T09:02:00+08:00",
            review_reason="用户确认默认项目总览。",
            source_refs=source_refs,
            evidence_refs=source_refs,
        )
        memory.save_candidate("series_memory", series, publication_context=context)
        return "series-memory-default"
    memory.publish("series_memory", series)
    return None


def _seed_deep_authority(store: JsonObjectStore) -> tuple[str, str]:
    source_body = "R3-API-CANARY 默认项目原始来源正文，说明系列召回的真实依据。"
    structured_body = "R2-API-CANARY 默认项目结构化详细资料，包含召回阶段和证据边界。"
    source_hash = hashlib.sha256(source_body.encode("utf-8")).hexdigest()
    store.write(
        "sources",
        "source-default",
        {
            "id": "source-default",
            "project_id": "default",
            "capture_mode": "inline",
            "processing_state": "captured",
            "trust_status": "user_confirmed",
            "content_hash": source_hash,
            "metadata": {"content": source_body},
        },
        expected_revision=0,
    )
    store.write(
        "documents",
        "document-default-deep",
        {
            "id": "document-default-deep",
            "project_id": "default",
            "status": "published",
            "revision": 1,
            "content_hash": hashlib.sha256(
                structured_body.encode("utf-8")
            ).hexdigest(),
            "series_id": "series-default",
            "blocks": [
                {
                    "id": "block-deep-1",
                    "content": structured_body,
                    "source_refs": [
                        {
                            "source_id": "source-default",
                            "locator": "source:metadata",
                        }
                    ],
                }
            ],
        },
        expected_revision=0,
    )
    return structured_body, source_body


def _activate_sqlite_skills(tmp_path: Path, records: SQLiteStructuredRecordStore) -> None:
    evidence = AggregateAuthorityEvidence(
        migration_id="direct-question-skill-v1",
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
        target_identity=TARGET_IDENTITY,
    )
    with records.begin() as uow:
        uow.put("aggregate_authority_targets", "default~project_skills", {
            "namespace_id": "default", "aggregate": "project_skills",
            "migration_id": evidence.migration_id,
            "source_fingerprint": evidence.source_fingerprint,
            "target_fingerprint": evidence.target_fingerprint,
            "target_identity": TARGET_IDENTITY,
        }, expected_revision=0)
        uow.commit()
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    initial = authority.create_json_active(namespace_id="default", aggregate="project_skills", reason="initial")
    staged = authority.transition(
        namespace_id="default", aggregate="project_skills", expected_revision=initial.revision,
        to_state="sqlite_staged", evidence=evidence, reason="staged",
    )
    authority.transition(
        namespace_id="default", aggregate="project_skills", expected_revision=staged.revision,
        to_state="sqlite_active", evidence=evidence, reason="active",
    )


def test_direct_question_on_fresh_library_returns_skipped_recall(tmp_path: Path) -> None:
    """Fresh library has no Project Skill, so recall degrades to 'skipped' but QA still answers."""
    client = _client(tmp_path)
    with client:
        response = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "全新的库，还没有任何项目 Skill。"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["recall_status"] == "skipped"
    assert body["recall_request_id"] is None
    assert body["recall_result_id"] is None
    assert body["evidence_refs"] == []
    assert body["evidence_count"] == 0
    # Still does not create knowledge records.
    assert body["source_created"] is False
    assert body["library_item_created"] is False
    assert body["memory_publication_state"] == "not_published"
    assert getattr(client.app.state, "rebuild_job_lifecycle", None) is None
    assert getattr(client.app.state, "media_hands_runtime_resolution", None) is None
    assert getattr(client.app.state, "plugin_hands_runtime", None) is None


def test_direct_question_routes_missing_project_over_current_r0_catalog(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        records = SQLiteStructuredRecordStore(
            tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
        )
        _save_sqlite_project_skill(
            records,
            project_id="memory-os",
            source_id="source-memory-os",
        )
        _save_sqlite_project_skill(
            records,
            project_id="brand-rice",
            source_id="source-brand-rice",
        )
        _publish_project_series_sqlite(
            records,
            project_id="memory-os",
            series_id="四层记忆系统",
            source_id="source-memory-os",
            overview="渐进召回、系列投影和原始证据。",
        )
        _publish_project_series_sqlite(
            records,
            project_id="brand-rice",
            series_id="桥米品牌",
            source_id="source-brand-rice",
            overview="区域公用品牌、包装与消费者价值。",
        )
        memory_seed = client.post(
            "/api/rebuild/workbench/direct-question",
            json={
                "project_id": "memory-os",
                "question": "四层记忆系统怎么召回？",
            },
        )
        brand_seed = client.post(
            "/api/rebuild/workbench/direct-question",
            json={
                "project_id": "brand-rice",
                "question": "桥米品牌怎么定位？",
            },
        )
        automatic = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "请给我桥米品牌整体方案"},
        )

    assert memory_seed.status_code == brand_seed.status_code == 200
    assert memory_seed.json()["recall_trace"] is not None, memory_seed.json()
    assert brand_seed.json()["recall_trace"] is not None, brand_seed.json()
    assert automatic.status_code == 200
    body = automatic.json()
    assert body["recall_trace"] is not None, body
    assert body["recall_trace"]["project_id"] == "brand-rice"
    evidence_ids = [
        str(item.get("object_id") or "")
        for item in body["evidence_items"]
    ]
    assert "series-memory-brand-rice" in evidence_ids
    assert all("memory-os" not in object_id for object_id in evidence_ids)


def test_direct_question_requires_human_project_selection_for_a_real_r0_tie(tmp_path: Path) -> None:
    query = "PRIVATE-PROJECT-ROUTE-CANARY shared knowledge"
    with _client(tmp_path) as client:
        records = SQLiteStructuredRecordStore(
            tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
        )
        for project_id in ("alpha", "beta"):
            _save_sqlite_project_skill(
                records,
                project_id=project_id,
                source_id=f"source-{project_id}",
            )
            _publish_project_series_sqlite(
                records,
                project_id=project_id,
                series_id="shared knowledge",
                source_id=f"source-{project_id}",
                overview="shared knowledge planning and retrieval",
            )
        for project_id in ("alpha", "beta"):
            seeded = client.post(
                "/api/rebuild/workbench/direct-question",
                json={
                    "project_id": project_id,
                    "question": "shared knowledge planning",
                },
            )
            assert seeded.status_code == 200, seeded.text

        automatic = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": query},
        )

    assert automatic.status_code == 409, automatic.text
    body = automatic.json()
    assert body["project_route"]["status"] == "ambiguous"
    assert body["project_route"]["reason_code"] == "ambiguous_project_scope"
    assert body["project_route"]["ai_assist"]["status"] == "disabled"
    assert query not in str(body["project_route"])
    assert [
        candidate["project_id"]
        for candidate in body["project_route"]["candidates"]
    ] == ["alpha", "beta"]


def test_direct_question_recalls_published_series_context(tmp_path: Path) -> None:
    """With a Project Skill + published memory for 'default', direct question returns evidence refs."""
    _save_default_skill(_store(tmp_path))
    _publish_default_memory(_store(tmp_path))

    with _client(tmp_path) as client:
        response = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "默认项目下一版应该先改哪里？"},
        )
        overview = client.get("/api/rebuild/library/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["recall_status"] == "recalled"
    assert body["recall_request_id"] is not None
    assert body["recall_result_id"] is not None
    assert body["evidence_count"] >= 1
    assert isinstance(body["evidence_refs"], list)
    assert len(body["evidence_refs"]) >= 1

    layers = [ref.get("layer") for ref in body["evidence_refs"]]
    assert "l3_project_skill" in layers
    # Each evidence ref carries the compact, privacy-safe shape only.
    for ref in body["evidence_refs"]:
        assert set(ref.keys()) <= {
            "layer",
            "object_id",
            "explanation",
                "source_refs",
                "snippet",
                "score",
                "quality",
            }
        assert isinstance(ref["source_refs"], list)

    # Still does not create knowledge records.
    assert body["knowledge_base_write"] is False
    assert body["source_created"] is False
    assert body["job_created"] is False
    assert body["library_item_created"] is False
    assert body["memory_publication_state"] == "not_published"
    assert "source_creation" in body["blocked_operations"]
    assert "long_term_memory_publication" in body["blocked_operations"]

    # Library overview must not include the direct question record.
    assert overview.status_code == 200
    assert all(
        item["item_id"] != body["question_id"] for item in overview.json()["items"]
    )


def test_direct_question_builds_projection_after_first_answer_and_uses_it_after_restart(
    tmp_path: Path,
) -> None:
    _save_default_skill(_store(tmp_path))
    _publish_default_memory(_store(tmp_path))

    with _client(tmp_path) as client:
        first = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"project_id": "default", "question": "默认项目召回怎么做？"},
        )
        second = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"project_id": "default", "question": "series-default 的召回场景是什么？"},
        )
    with _client(tmp_path) as restarted:
        third = restarted.post(
            "/api/rebuild/workbench/direct-question",
            json={"project_id": "default", "question": "series-default 的召回结构是什么？"},
        )
        performance = restarted.get(
            "/api/rebuild/developer-studio/memory-retrieval/performance?project_id=default"
        )

    assert first.status_code == second.status_code == third.status_code == performance.status_code == 200
    first_trace = first.json()["recall_trace"]
    second_trace = second.json()["recall_trace"]
    third_trace = third.json()["recall_trace"]
    assert first_trace["fallback"]["used"] is True
    assert first_trace["rebuild"]["scheduled"] is True
    assert second_trace["fallback"]["used"] is False
    assert second_trace["layers_read"] == [
        "r0_series_router",
        "r1_series_digest",
    ]
    assert third_trace["fallback"]["used"] is False
    assert third_trace["authority_fingerprint"] == second_trace["authority_fingerprint"]
    assert [item["object_id"] for item in second.json()["evidence_items"][:2]] == [
        "series-memory-default",
        "atom-default",
    ]
    assert [item["object_id"] for item in third.json()["evidence_items"][:2]] == [
        "series-memory-default",
        "atom-default",
    ]
    performance_payload = performance.json()
    assert performance_payload["window"]["timed_sample_count"] == 3
    assert performance_payload["stages"][0]["attempt_count"] == 3
    assert performance_payload["stages"][1]["used_count"] == 2
    performance_json = json.dumps(performance_payload, ensure_ascii=False)
    assert "默认项目召回怎么做" not in performance_json
    assert "series-memory-default" not in performance_json
    jobs = [
        job
        for job in _store(tmp_path).list("jobs")
        if job.get("job_type") == "rebuild_memory_projection"
    ]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "completed"


def test_direct_question_progressive_projection_can_answer_without_project_skill(
    tmp_path: Path,
) -> None:
    _publish_default_memory(_store(tmp_path))

    with _client(tmp_path) as client:
        first = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "series-default"},
        )
        second = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "series-default 的项目概况是什么？"},
        )

    assert first.status_code == second.status_code == 200
    assert first.json()["recall_status"] == "skipped"
    assert second.json()["recall_status"] == "recalled"
    assert second.json()["recall_request_id"].startswith("recall-request-default-")
    request = _store(tmp_path).read(
        "recall_requests",
        second.json()["recall_request_id"],
    )
    assert request is not None
    assert request["layers"] == ["l4_persona", "l1_atom"]
    assert request["required_context_refs"] == []
    assert second.json()["recall_trace"]["fallback"]["used"] is False
    assert second.json()["evidence_items"][0]["object_id"] == "series-memory-default"


def test_direct_question_r2_r3_are_ephemeral_in_production_api(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _publish_default_memory(store)
    r2_canary, r3_canary = _seed_deep_authority(store)

    with _client(tmp_path) as client:
        warmup = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "series-default"},
        )
        response = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "series-default 原文出处"},
        )

    assert warmup.status_code == response.status_code == 200
    body = response.json()
    assert body["deep_evidence"] == {
        "count": 2,
        "layers": ["r2_structured_content", "r3_source_evidence"],
        "ephemeral": True,
        "content_persisted": False,
        "provider_egress_authorized": False,
    }
    assert [item["layer"] for item in body["evidence_items"][-2:]] == [
        "r2_structured_content",
        "r3_source_evidence",
    ]
    response_json = json.dumps(body, ensure_ascii=False, sort_keys=True)
    assert r2_canary in response_json
    assert r3_canary in response_json
    record = store.read("workbench_direct_questions", body["question_id"])
    persisted = json.dumps(record, ensure_ascii=False, sort_keys=True)
    trace = json.dumps(body["recall_trace"], ensure_ascii=False, sort_keys=True)
    assert r2_canary not in persisted
    assert r3_canary not in persisted
    assert r2_canary not in trace
    assert r3_canary not in trace
    assert "source:metadata" not in trace
    assert record["deep_evidence"]["content_persisted"] is False
    non_authority_records = json.dumps(
        {
            collection: store.list(collection)
            for collection in (
                "workbench_direct_questions",
                "memory_retrieval_projection_manifests",
                "memory_retrieval_projection_items",
                "memory_retrieval_projection_failures",
                "jobs",
            )
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    assert r2_canary not in non_authority_records
    assert r3_canary not in non_authority_records


def test_direct_question_uses_l3_to_l1_evidence_and_falls_back_after_l3_rollback(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/text-source-intake",
            json={
                "title": "默认项目分层问答来源",
                "content": "默认项目应按系列、场景、事实逐层回答，并在系列撤回后安全降级。",
            },
        )
        assert intake.status_code == 201
        source_id = intake.json()["source_id"]
        store = _store(tmp_path)
        candidates = ObjectStoreMemoryCandidateRepository(store)
        candidates.save(
            _review_candidate(
                "candidate-atom-default",
                "atom",
                "默认项目当前阶段优先推进直接问答系列召回。",
                source_id,
            )
        )
        candidates.save(
            _review_candidate(
                "candidate-scenario-default",
                "scenario",
                "默认项目通过已确认事实形成直接问答场景。",
                source_id,
            )
        )
        candidates.save(
            _review_candidate(
                "candidate-series-default",
                "series_memory",
                "默认项目的系列总览，用于直接问答上下文召回。",
                source_id,
            )
        )
        candidates.save(
            _review_candidate(
                "candidate-skill-default",
                "project_skill",
                "默认项目应使用系列、场景和事实组织直接问答证据。",
                source_id,
            )
        )
        atom_review = client.post(
            "/api/rebuild/memory-candidates/candidate-atom-default/review",
            json={"action": "promote_to_atom", "reason": "确认默认项目事实。"},
        )
        atom_id = atom_review.json()["promoted_object_id"]
        assert client.post(
            f"/api/rebuild/staging-atoms/{atom_id}/publication",
            json={"confirm": True, "reason": "发布默认项目事实。"},
        ).status_code == 200
        scenario_review = client.post(
            "/api/rebuild/memory-candidates/candidate-scenario-default/review",
            json={
                "action": "promote_to_scenario",
                "reason": "确认默认项目场景。",
                "series_id": "default",
                "atom_ids": [atom_id],
            },
        )
        scenario_id = scenario_review.json()["promoted_object_id"]
        assert client.post(
            f"/api/rebuild/staging-scenarios/{scenario_id}/publication",
            json={"confirm": True, "reason": "发布默认项目场景。"},
        ).status_code == 200
        series_review = client.post(
            "/api/rebuild/memory-candidates/candidate-series-default/review",
            json={
                "action": "promote_to_series_memory",
                "reason": "确认默认项目总览。",
                "series_id": "series-default",
                "scenario_ids": [scenario_id],
            },
        )
        staged_series_id = series_review.json()["promoted_object_id"]
        publication = client.post(
            f"/api/rebuild/staging-series-memory/{staged_series_id}/publication",
            json={"confirm": True, "reason": "发布默认项目分层问答总览。"},
        )
        assert publication.status_code == 200
        publication_id = publication.json()["publication_id"]
        skill_review = client.post(
            "/api/rebuild/memory-candidates/candidate-skill-default/review",
            json={"action": "promote_to_project_skill", "reason": "确认默认项目 Skill。"},
        )
        skill_id = skill_review.json()["promoted_object_id"]
        assert client.post(
            f"/api/rebuild/staging-project-skills/{skill_id}/publication",
            json={"confirm": True, "reason": "发布默认项目 Skill。"},
        ).status_code == 200

        before_rollback = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "默认项目应如何组织下一步？"},
        )
        assert before_rollback.status_code == 200
        before_body = before_rollback.json()
        before_layers = [item["layer"] for item in before_body["evidence_items"]]
        assert before_layers == ["l3_project_skill", "l3_series_memory"]
        assert "L3 Series Memory：默认项目的系列总览" in before_body["answer"]["text"]
        assert "L2 Scenario" not in before_body["answer"]["text"]
        assert "L1 Atom" not in before_body["answer"]["text"]
        assert before_body["source_links"] == [
            {
                "source_id": source_id,
                "locator": "source:metadata",
                "ref": f"crp://default/sources/{source_id}",
            }
        ]

        rollback = client.post(
            f"/api/rebuild/memory-publications/{publication_id}/rollback",
            json={"confirm": True, "reason": "撤回过期项目总览并验证下层降级。"},
        )
        assert rollback.status_code == 200
        after_rollback = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "默认项目应如何组织下一步？"},
        )
    with _client(tmp_path) as restarted:
        after_restart = restarted.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "默认项目应如何组织下一步？"},
        )

    assert after_rollback.status_code == 200
    after_body = after_rollback.json()
    after_layers = [item["layer"] for item in after_body["evidence_items"]]
    assert after_layers == ["l3_project_skill"]
    assert "L3 Series Memory" not in after_body["answer"]["text"]
    assert "L2 Scenario" not in after_body["answer"]["text"]
    assert "L1 Atom" not in after_body["answer"]["text"]
    assert after_body["recall_request_id"] == before_body["recall_request_id"]
    assert after_body["recall_result_id"] != before_body["recall_result_id"]
    assert after_body["source_links"][0]["source_id"] == source_id
    assert store.read("sources", source_id) is not None
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    )
    assert records.read("memory_series_memory", staged_series_id) is None
    assert records.read("memory_scenarios", scenario_id) is not None
    assert records.read("memory_atoms", atom_id) is not None
    assert len(store.list("sources")) == 1
    assert len(records.list("memory_scenarios")) == 1
    assert len(records.list("memory_atoms")) == 1
    assert after_body["recall_trace"]["fallback"]["used"] is True
    assert not {
        "r2_structured_content",
        "r3_source_evidence",
    } & set(after_body["recall_trace"]["layers_read"])
    assert after_restart.status_code == 200
    restart_body = after_restart.json()
    assert "L3 Series Memory" not in restart_body["answer"]["text"]
    assert not {
        "r2_structured_content",
        "r3_source_evidence",
    } & set(restart_body["recall_trace"]["layers_read"])


def test_direct_question_identical_query_replays_across_restart_without_duplicate_recall_records(
    tmp_path: Path,
) -> None:
    _save_default_skill(_store(tmp_path))
    _publish_default_memory(_store(tmp_path))
    question = "默认项目下一版应该先改哪里？"

    with _client(tmp_path) as client:
        first = client.post("/api/rebuild/workbench/direct-question", json={"question": question})
        replay = client.post("/api/rebuild/workbench/direct-question", json={"question": question})
    with _client(tmp_path) as restarted:
        after_restart = restarted.post("/api/rebuild/workbench/direct-question", json={"question": question})

    assert first.status_code == replay.status_code == after_restart.status_code == 200
    first_body = first.json()
    replay_body = replay.json()
    restart_body = after_restart.json()
    assert first_body["recall_status"] == "recalled"
    assert replay_body["recall_request_id"] == first_body["recall_request_id"]
    assert replay_body["recall_result_id"] == first_body["recall_result_id"]
    assert replay_body["evidence_items"] == first_body["evidence_items"]
    assert restart_body["recall_request_id"] == first_body["recall_request_id"]
    assert restart_body["recall_result_id"] == first_body["recall_result_id"]
    assert restart_body["evidence_items"] == first_body["evidence_items"]
    store = _store(tmp_path)
    assert len(store.list("recall_requests")) == 1
    assert len(store.list("recall_results")) == 1


def test_direct_question_rejects_partial_sqlite_project_skill_authority(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    skills = SQLiteProjectSkillRepository(records)
    structured = _skill_fixture()
    structured["purpose"] = "SQLite authority directs the next answer toward durable recovery."
    skills.save(ProjectSkillUpdate(
        project_id="default",
        markdown="# SQLite Project Skill\n\nUse durable recovery evidence.",
        structured=structured,
        expected_revision=0,
        reason="active sqlite direct question fixture",
    ))
    _activate_sqlite_skills(tmp_path, records)
    json_store = _store(tmp_path)
    _publish_default_memory(json_store)

    with _client(tmp_path) as client:
        response = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "默认项目下一步需要关注什么？"},
        )

    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == "workbench direct question rejected"
    assert "partially SQLite active" in body["reason"]
    assert skills.load("default")["revision"] == 1
    assert len(skills.revisions("default")) == 1
    assert ObjectStoreProjectSkillRepository(json_store).load("default") is None


def test_direct_question_recall_does_not_leak_secrets(tmp_path: Path) -> None:
    """Direct question response (including evidence_refs) must not carry secret-like values."""
    _save_default_skill(_store(tmp_path))
    _publish_default_memory(_store(tmp_path))

    with _client(tmp_path) as client:
        response = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "隐私检查：确保不泄露密钥。"},
        )

    assert response.status_code == 200
    def string_values(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [
                item
                for nested in value.values()
                for item in string_values(nested)
            ]
        if isinstance(value, list):
            return [item for nested in value for item in string_values(nested)]
        return []

    lowered = "\n".join(string_values(response.json())).lower()
    assert "sk-" not in lowered
    assert "bearer " not in lowered
    assert "authorization" not in lowered
    assert "cookie" not in lowered
    assert "password" not in lowered
    assert "token" not in lowered


def test_direct_question_still_answers_without_recall_dependencies(tmp_path: Path) -> None:
    """Even when recall finds nothing usable, the direct QA path must still answer."""
    with _client(tmp_path) as client:
        response = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "只问答不入库，无论召回如何。"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["qa_mode"] == "direct_local_answer"
    assert body["recall_status"] in {"skipped", "insufficient_evidence", "disabled", "recalled"}
    assert body["knowledge_base_write"] is False
    assert body["memory_publication_state"] == "not_published"
