from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_boundary_mode_command_is_narrow_and_does_not_own_policy_or_runtime() -> None:
    text = (ROOT / "src/backend/security/project_boundary_mode_command.py").read_text(encoding="utf-8")
    for forbidden in ("BoundaryPolicyEngine", ".grant", ".dispatch(", ".invoke(", "api_key", "request_body"):
        assert forbidden not in text
    assert "prepared" in text and "boundary_updated" in text and "requires_repair" in text


def test_boundary_mode_command_api_is_local_and_no_runtime_build() -> None:
    text = (ROOT / "src/backend/api/routes/ai.py").read_text(encoding="utf-8")
    start = text.index("async def set_project_boundary_mode(")
    end = text.index("\n\n@router.get(\"/projects/{project_id}/boundary-mode-commands", start)
    endpoint = text[start:end]
    assert "_local_request" in endpoint
    assert "ProjectBoundaryModeCommandService" in endpoint
    assert "get_or_build_ai_runtime" not in endpoint
