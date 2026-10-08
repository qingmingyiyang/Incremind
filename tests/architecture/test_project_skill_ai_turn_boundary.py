from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROUTES = ROOT / "src/backend/api/routes/product/project_skills.py"
AI_RUNTIME = ROOT / "src/backend/memory_app/kernel/ai_runtime.py"
PROJECT_SKILL_RUNTIME = ROOT / "src/backend/api/project_skill_ai_runtime.py"


def test_project_skill_ai_legacy_endpoint_is_only_a_turn_action_adapter() -> None:
    routes = LEGACY_ROUTES.read_text(encoding="utf-8")
    endpoint = routes[
        routes.index("async def project_skill_ai_draft_generate("):
        routes.index("async def project_skill_ai_draft_edit(")
    ]

    assert "get_or_build_ai_runtime" in endpoint
    assert "runtime.submit_turn(turn_request)" in endpoint
    assert 'event.get("type") == "approval.required"' in endpoint
    assert "runtime.apply_action" in endpoint
    assert '"type": "approve"' in endpoint
    assert "runtime.presentation_for" in endpoint
    for forbidden in (
        "_resolve_deepseek_provider_record",
        "_build_provider_from_record",
        "ObjectStoreMemoryCandidateRepository",
        "build_project_skill_evidence_bundle",
    ):
        assert forbidden not in endpoint
    assert "def _project_skill_ai_provider_call" not in routes


def test_project_skill_draft_model_and_write_are_owned_by_ai_kernel_capabilities() -> None:
    installation = AI_RUNTIME.read_text(encoding="utf-8")
    runtime = PROJECT_SKILL_RUNTIME.read_text(encoding="utf-8")

    assert "PROJECT_SKILL_DRAFT_OUTCOME: ProjectSkillDraftPlanner" in installation
    assert 'egress_purpose="memory_candidate"' in installation
    assert "ProjectSkillEvidenceCapability" in installation
    assert "ProjectSkillDraftProposalCapability" in installation
    assert '"receipt_required"' in installation
    assert "self._gateway.invoke(ModelRequest(" in runtime
    assert '{"role": "system", "content": project_skill_ai_system_prompt()}' in runtime
    assert '"status": "pending_review"' in runtime
    assert "ObjectStoreMemoryCandidateRepository(self._store).save(candidate)" in runtime
    assert '"active_project_skill_changed": False' in runtime
