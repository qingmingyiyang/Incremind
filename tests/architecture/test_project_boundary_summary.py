from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROJECTOR = ROOT / "src" / "backend" / "security" / "project_boundary_summary.py"
API = ROOT / "src" / "backend" / "api" / "routes" / "ai.py"
SCHEMA = ROOT / "core-contracts" / "ai" / "project-boundary-summary.schema.json"


def test_boundary_summary_projector_is_read_only_and_owns_no_authority() -> None:
    text = PROJECTOR.read_text(encoding="utf-8")
    for forbidden in ("ProjectBoundaryProfileStore", "ProjectCapabilityProfileStore", ".update(", ".register(", ".dispatch(", ".invoke(", "secret_store"):
        assert forbidden not in text
    assert "unmapped_target" in text
    assert "machine_mcp_approval" in text


def test_boundary_summary_dto_has_a_stable_schema_for_ui_consumers() -> None:
    import json

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    required = set(schema["required"])
    assert {"schema_version", "semantics_version", "expiring_window_hours", "persistent_grants", "authorities"} <= required
    grant = schema["properties"]["persistent_grants"]["items"]
    assert {"static_match_eligible", "invocation_dependent", "redaction_required"} <= set(grant["required"])


def test_boundary_summary_api_uses_existing_runtime_without_build_or_execution() -> None:
    text = API.read_text(encoding="utf-8")
    start = text.index("async def get_project_boundary_summary(")
    end = text.index("\n\n@router.post(\"/turns\")", start)
    endpoint = text[start:end]
    for forbidden in ("get_or_build_ai_runtime", "build_ai_runtime", ".register(", ".dispatch(", ".invoke("):
        assert forbidden not in endpoint
    assert "capability_registry_snapshot" in endpoint
    assert "ProjectBoundarySummaryProjector" in endpoint
