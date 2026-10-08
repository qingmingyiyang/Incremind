from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_grant_command_does_not_own_runtime_dispatch_or_secret_material() -> None:
    text = (ROOT / "src/backend/security/project_boundary_grant_command.py").read_text(encoding="utf-8")
    for forbidden in (
        "BoundaryPolicyEngine", ".dispatch(", ".invoke(", "api_key",
        "request_body", "credential_subject_id", "endpoint_identity",
    ):
        assert forbidden not in text
    assert "prepared" in text and "boundary_updated" in text
    assert "requires_repair" in text and "ProjectBoundaryMutationReservation" in text


def test_grant_create_route_reads_snapshot_but_never_builds_runtime() -> None:
    text = (ROOT / "src/backend/api/routes/ai.py").read_text(encoding="utf-8")
    start = text.index("async def create_project_boundary_grant(")
    end = text.index("\n\n@router.post(\"/projects/{project_id}/boundary-grants/{grant_id}/revoke\"", start)
    endpoint = text[start:end]
    assert "_local_request" in endpoint
    assert "capability_registry_snapshot" in endpoint
    assert "get_or_build_ai_runtime" not in endpoint


def test_grant_revoke_route_has_no_runtime_dependency() -> None:
    text = (ROOT / "src/backend/api/routes/ai.py").read_text(encoding="utf-8")
    start = text.index("async def revoke_project_boundary_grant(")
    end = text.index("\n\n@router.get(\"/projects/{project_id}/boundary-grant-commands", start)
    endpoint = text[start:end]
    assert "ProjectBoundaryGrantCommandService" in endpoint
    assert "ai_runtime" not in endpoint
