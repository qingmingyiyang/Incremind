import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_world_model_reuses_primary_structured_store_without_new_authorities() -> None:
    runtime = (ROOT / "src/backend/api/personal_world_model_runtime.py").read_text(encoding="utf-8")
    route = (ROOT / "src/backend/api/routes/personal_world_model.py").read_text(encoding="utf-8")

    assert '"jobs.sqlite3"' in runtime
    assert "personal-world.sqlite3" not in runtime
    assert "EffectRunner" not in runtime
    assert "SQLiteAITurnStore(" not in runtime
    assert "from core.memory" not in runtime.lower()
    assert "from core.application_skill" not in runtime.lower()
    assert "feedback_event_draft" in runtime
    assert "FEEDBACK_RECORDED" in route


def test_world_model_router_is_part_of_the_production_router_set() -> None:
    routes = (ROOT / "src/backend/api/routes/__init__.py").read_text(encoding="utf-8")

    assert "personal_world_model_router" in routes
    assert "app.include_router(personal_world_model_router)" in routes


def test_world_state_is_frozen_into_the_production_project_context_manifest() -> None:
    context_contract = (
        ROOT / "src/core/ai_kernel/context_manifest.py"
    ).read_text(encoding="utf-8")
    composition = (ROOT / "src/backend/memory_app/kernel/ai_runtime.py").read_text(encoding="utf-8")
    resolver = (ROOT / "src/backend/api/ai_profile_resolvers.py").read_text(encoding="utf-8")

    assert '"world_state_projection"' in context_contract
    assert "TurnWorldStateSnapshotAuthority.for_root(" in composition
    assert "world_state=world_state_snapshots" in composition
    assert 'kind="world_state_projection"' in resolver
    assert 'disclosure="model"' in resolver
    schema = json.loads(
        (ROOT / "core-contracts/ai/turn-context-manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert "world_state_projection" in schema["properties"]["entries"]["items"][
        "properties"
    ]["kind"]["enum"]


def test_terminal_feedback_learning_reuses_receipt_memory_and_skill_authorities() -> None:
    observer = (
        ROOT / "src/backend/api/personal_world_model_terminal_observer.py"
    ).read_text(encoding="utf-8")
    learning = (
        ROOT / "src/backend/api/personal_world_model_learning.py"
    ).read_text(encoding="utf-8")
    runner = (ROOT / "src/backend/api/ai_turn_runner.py").read_text(encoding="utf-8")
    application = (ROOT / "src/backend/api/app.py").read_text(encoding="utf-8")

    assert "tool.outcome.recorded" in observer
    assert "reported_outcome=OutcomeStatus.UNKNOWN" in observer
    assert "record_feedback(" not in observer
    assert "EffectRunner" not in observer
    assert "ObjectStoreMemoryCandidateRepository" in learning
    assert "ApplicationSkillLearningRuntime" in learning
    assert '"pending_review"' in learning
    assert '"memory_publication": "not_performed"' in learning
    assert '"skill_file_write": "not_performed"' in learning
    assert "ObjectStoreMemoryStore" not in learning
    assert "AutoPublishMemoryCandidate" not in learning
    assert "ApplicationSkillImportService" not in learning
    assert "self._notify_terminal(receipt)" in runner
    assert "PersonalWorldModelTerminalObserver(" in application


def test_user_workflow_consumes_frozen_state_without_creating_parallel_authorities() -> None:
    workflow = (
        ROOT / "src/backend/api/personal_world_model_workflow.py"
    ).read_text(encoding="utf-8")
    planner = (ROOT / "src/backend/api/workbench_ai_runtime.py").read_text(
        encoding="utf-8"
    )
    frontend = (
        ROOT / "src/frontend/src/features/rebuild/WorldIntelligencePanel.jsx"
    ).read_text(encoding="utf-8")

    assert "submitter = self._organization_submitter or self._runner" in workflow
    assert "receipt = submitter.accept_and_submit(request)" in workflow
    assert "ProjectProvenanceRuntime(world=world)" in workflow
    assert "self._world.record_feedback(" in workflow
    assert "frozen_world_state_planning(" in planner
    assert "WORLD_PROJECT_SESSION_ID" in planner
    assert "SQLiteAITurnStore(" not in workflow
    assert "EffectRunner(" not in workflow
    assert "ObjectStoreMemoryStore" not in workflow
    assert "ApplicationSkillImportService" not in workflow
    assert "pending review" in frontend
    assert "正式 Memory 与 Skill 始终需要审核" in frontend
