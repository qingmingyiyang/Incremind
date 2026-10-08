from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.routes import application_skill_learning as learning_routes
from core.application_skill import (
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillConsumerRuntime,
    ApplicationSkillProposalRegistry,
    ApplicationSkillResolver,
    ApplicationSkillSource,
    ObjectStoreApplicationSkillTraceRepository,
)
from core.storage_provider import JsonObjectStore


def _write_skill(root: Path, skill_id: str, marker: str = "PRIVATE-METHOD-BODY") -> Path:
    package = root / skill_id
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        f"description: Use {skill_id} for project document review.\n"
        "---\n\n"
        f"# {marker}\n",
        encoding="utf-8",
    )
    return package


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def test_application_skill_import_binding_preview_and_project_summary_api(
    tmp_path: Path,
) -> None:
    source = _write_skill(tmp_path / "selected", "document-review")
    with _client(tmp_path) as client:
        preview_response = client.post(
            "/api/rebuild/developer-studio/application-skills/imports/preview",
            json={"source_path": str(source)},
        )
        assert preview_response.status_code == 200
        assert preview_response.headers["cache-control"] == "no-store"
        preview = preview_response.json()
        assert preview["write_effect"] == "none"
        assert not (tmp_path / "skills").exists()

        import_response = client.post(
            "/api/rebuild/developer-studio/application-skills/imports/confirm",
            json={
                "source_path": str(source),
                "expected_fingerprint": preview["package"]["fingerprint"],
                "preview_token": preview["preview_token"],
                "proposal_id": preview["proposal_id"],
                "confirm": True,
                "reason": "用户确认导入本地方法。",
            },
        )
        assert import_response.status_code == 200
        assert import_response.json()["status"] == "imported"

        status_response = client.get(
            "/api/rebuild/developer-studio/application-skills"
        )
        assert status_response.status_code == 200
        status = status_response.json()
        serialized = status_response.text
        assert status["catalog"]["packages"][0]["skill_id"] == "document-review"
        assert status["catalog"]["packages"][0]["trigger_boundary"] == (
            "Use document-review for project document review."
        )
        assert status["catalog"]["packages"][0]["validation"] == "manual-review-required"
        assert status["catalog"]["packages"][0]["maturity"] == "draft"
        assert str((tmp_path / "skills").resolve()) not in serialized
        assert "PRIVATE-METHOD-BODY" not in serialized

        bind_preview_response = client.post(
            "/api/rebuild/developer-studio/application-skills/bindings/preview",
            json={
                "skill_id": "document-review",
                "project_id": "project-alpha",
                "allowed_consumers": ["document.generate"],
                "priority": 700,
                "trigger_terms": ["项目文档"],
            },
        )
        assert bind_preview_response.status_code == 200
        bind_preview = bind_preview_response.json()
        assert bind_preview["write_effect"] == "none"

        activate_response = client.post(
            "/api/rebuild/developer-studio/application-skills/bindings/activate",
            json={
                "skill_id": "document-review",
                "project_id": "project-alpha",
                "allowed_consumers": ["document.generate"],
                "priority": 700,
                "trigger_terms": ["项目文档"],
                "expected_registry_revision": bind_preview["registry_revision"],
                "preview_token": bind_preview["preview_token"],
                "proposal_id": bind_preview["proposal_id"],
                "confirm": True,
                "reason": "用户确认当前项目使用文档方法。",
            },
        )
        assert activate_response.status_code == 200
        assert activate_response.json()["status"] == "activated"

        resolver_response = client.post(
            "/api/rebuild/developer-studio/application-skills/resolver-preview",
            json={
                "project_id": "project-alpha",
                "consumer": "document.generate",
                "task_kind": "project-document",
                "task_text": "生成项目文档。",
            },
        )
        assert resolver_response.status_code == 200
        resolver = resolver_response.json()
        assert resolver["selected"][0]["skill_id"] == "document-review"
        assert resolver["write_effect"] == "none"
        assert "PRIVATE-METHOD-BODY" not in resolver_response.text

        summary_response = client.get(
            "/api/rebuild/projects/project-alpha/application-skills"
        )
        assert summary_response.status_code == 200
        summary = summary_response.json()
        assert summary["methods"][0]["name"] == "document-review"
        assert summary["methods"][0]["status"] == "active"
        assert summary["methods"][0]["maturity"] == "draft"
        assert summary["methods"][0]["last_used_at"] is None


def test_application_skill_invocation_api_is_redacted_and_project_isolated(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path / "skills", "document-review")
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    catalog = ApplicationSkillCatalog().discover(
        [ApplicationSkillSource("user", tmp_path / "skills", "user")]
    )
    bindings = ApplicationSkillBindingRegistry(store, now="2026-07-18T17:30:00+00:00")
    package = catalog.get("document-review")
    bind_preview = bindings.preview_bind(
        package,
        project_id="project-alpha",
        allowed_consumers=("document.generate",),
        priority=700,
        trigger_terms=("项目文档",),
    )
    bindings.activate(
        package,
        project_id="project-alpha",
        allowed_consumers=("document.generate",),
        priority=700,
        trigger_terms=("项目文档",),
        expected_registry_revision=bind_preview["registry_revision"],
        preview_token=bind_preview["preview_token"],
        confirm=True,
        reason="用户确认当前项目使用文档方法。",
    )
    ApplicationSkillConsumerRuntime(
        catalog=ApplicationSkillCatalog(),
        sources=(ApplicationSkillSource("user", tmp_path / "skills", "user"),),
        resolver=ApplicationSkillResolver(
            bindings,
            trace_store=ObjectStoreApplicationSkillTraceRepository(store),
            now="2026-07-18T17:31:00+00:00",
        ),
    ).resolve_context(
        project_id="project-alpha",
        consumer="document.generate",
        task_kind="project-document",
        task_text="PRIVATE-TASK-TEXT 项目文档",
        invocation_id="model-request-document-api1234",
    )

    with _client(tmp_path) as client:
        alpha = client.get(
            "/api/rebuild/developer-studio/application-skills/invocations",
            params={"project_id": "project-alpha"},
        )
        beta = client.get(
            "/api/rebuild/developer-studio/application-skills/invocations",
            params={"project_id": "project-beta"},
        )

    assert alpha.status_code == 200
    assert len(alpha.json()["invocations"]) == 1
    assert "PRIVATE-TASK-TEXT" not in alpha.text
    assert "PRIVATE-METHOD-BODY" not in alpha.text
    assert beta.status_code == 200
    assert beta.json()["invocations"] == []


def test_application_skill_api_rejects_secret_package_and_stale_activation(
    tmp_path: Path,
) -> None:
    source = _write_skill(tmp_path / "selected", "unsafe-package")
    (source / "SKILL.md").write_text(
        "---\nname: unsafe-package\ndescription: unsafe package.\n---\n\napi_key=sk-abcdefghijklmnop\n",
        encoding="utf-8",
    )
    with _client(tmp_path) as client:
        rejected = client.post(
            "/api/rebuild/developer-studio/application-skills/imports/preview",
            json={"source_path": str(source)},
        )
        assert rejected.status_code == 400
        assert rejected.headers["cache-control"] == "no-store"
        assert "sk-abcdefghijklmnop" not in rejected.text

        stale = client.post(
            "/api/rebuild/developer-studio/application-skills/bindings/activate",
            json={
                "skill_id": "missing",
                "project_id": "project-alpha",
                "allowed_consumers": ["document.generate"],
                "priority": 700,
                "trigger_terms": [],
                "expected_registry_revision": 99,
                "preview_token": "stale",
                "confirm": True,
                "reason": "确认。",
            },
        )
        assert stale.status_code == 400
        assert stale.json()["actionable"] is True


def test_learning_proposal_list_detail_and_review_are_project_scoped_and_proposal_only(
    tmp_path: Path,
) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    proposal = ApplicationSkillProposalRegistry(store).propose("skill.update", {
        "turn_id": "turn-alpha",
        "receipt_id": "receipt-alpha",
        "project_id": "project-alpha",
        "resolution_id": "skill-resolution-" + "a" * 32,
        "skill_id": "document-review",
        "skill_fingerprint": "a" * 64,
        "source_kind": "user",
        "target_source_kind": "user",
        "reusable_signal": {"kind": "explicit_correction", "evidence": "用户明确纠正了重复的排序错误。"},
        "proposed_content": {"summary": "先核对来源。", "instructions": "先读取 trace，再提出待审建议。"},
        "safety_diagnostics": [{"code": "credential_path_command_and_scope_scan", "status": "passed"}],
        "mutation_mode": "update_existing_user_skill",
        "write_effect": "proposal_only",
        "private_task": "PRIVATE-TASK-TEXT",
    })
    # The endpoint uses an allow-list projection: unrelated durable fields are
    # never returned to the Developer Studio.
    with _client(tmp_path) as client:
        listed = client.get(
            "/api/rebuild/developer-studio/application-skills/learning-proposals",
            params={"project_id": "project-alpha"},
        )
        isolated = client.get(
            "/api/rebuild/developer-studio/application-skills/learning-proposals",
            params={"project_id": "project-beta"},
        )
        detail = client.get(
            f"/api/rebuild/developer-studio/application-skills/learning-proposals/{proposal['proposal_id']}",
            params={"project_id": "project-alpha"},
        )
        reviewed = client.post(
            f"/api/rebuild/developer-studio/application-skills/learning-proposals/{proposal['proposal_id']}/review",
            json={
                "project_id": "project-alpha", "decision": "approve",
                "reason": "用户已确认候选仅进入待应用状态。", "confirm": True,
            },
        )
        conflicting_reject = client.post(
            f"/api/rebuild/developer-studio/application-skills/learning-proposals/{proposal['proposal_id']}/review",
            json={
                "project_id": "project-alpha", "decision": "reject",
                "reason": "批准后不能改为拒绝。", "confirm": True,
            },
        )

    assert listed.status_code == 200
    assert listed.json()["proposals"][0]["status"] == "pending_review"
    assert "PRIVATE-TASK-TEXT" not in listed.text
    assert isolated.status_code == 200 and isolated.json()["proposals"] == []
    assert detail.status_code == 200
    assert detail.json()["safety_diagnostics"][0]["status"] == "passed"
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "approved"
    assert reviewed.json()["write_effect"] == "proposal_only"
    assert conflicting_reject.status_code == 409
    assert conflicting_reject.json()["detail"] == "application_skill_learning_rejected"


def test_learning_eligible_route_requires_completed_same_project_turn(
    tmp_path: Path, monkeypatch,
) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    ObjectStoreApplicationSkillTraceRepository(store).save_trace({
        "schema_version": "1.0.0", "resolution_id": "skill-resolution-" + "a" * 32,
        "invocation_id": "turn-alpha", "project_id": "project-alpha",
        "consumer": "turn.workbench-question", "task_kind": "workbench.question.answer",
        "task_fingerprint": "b" * 64,
        "matched": [{"skill_id": "document-review", "skill_fingerprint": "a" * 64, "binding_id": "binding-a", "binding_revision": 1, "score": 100, "priority": 700, "reasons": ["selected"], "selected": True}],
        "selected": [{"skill_id": "document-review", "skill_fingerprint": "a" * 64, "binding_id": "binding-a", "binding_revision": 1, "score": 100, "priority": 700, "instruction_bytes": 40, "resource_count": 0}],
        "budget_excluded_skill_ids": [], "loaded_instruction_bytes": 40,
        "context_size_bytes": 80, "fallback": "none", "recorded_at": "2026-09-01T01:00:00+00:00",
    })
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    app.state.ai_turn_effect_store = SimpleNamespace(get_request=lambda turn_id: {
        "turn_id": turn_id, "scope": {"kind": "project", "project_id": "project-alpha"},
    })
    monkeypatch.setattr(learning_routes, "get_or_build_ai_runtime", lambda _request, _container: SimpleNamespace(
        receipt_for=lambda turn_id: SimpleNamespace(turn_id=turn_id, operation_id="operation-alpha", status="completed"),
    ))
    params = {"project_id": "project-alpha", "turn_id": "turn-alpha", "resolution_id": "skill-resolution-" + "a" * 32}
    with TestClient(app) as client:
        completed = client.get("/api/rebuild/developer-studio/application-skills/learning-eligible-invocations", params=params)
        isolated = client.get("/api/rebuild/developer-studio/application-skills/learning-eligible-invocations", params={**params, "project_id": "project-beta"})
        monkeypatch.setattr(learning_routes, "get_or_build_ai_runtime", lambda _request, _container: SimpleNamespace(
            receipt_for=lambda turn_id: SimpleNamespace(turn_id=turn_id, operation_id="operation-alpha", status="failed"),
        ))
        incomplete = client.get("/api/rebuild/developer-studio/application-skills/learning-eligible-invocations", params=params)

    assert completed.status_code == 200
    assert completed.json()["selected"][0]["skill_id"] == "document-review"
    assert "task_fingerprint" not in completed.text
    assert isolated.status_code == 404
    assert incomplete.status_code == 404
