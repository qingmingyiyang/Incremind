from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "src" / "core" / "ai_tooling" / "project_capability_catalog.py"
API = ROOT / "src" / "backend" / "api" / "routes" / "ai.py"


def test_catalog_projection_stays_pure_and_never_owns_a_runtime() -> None:
    text = CATALOG.read_text(encoding="utf-8")
    for forbidden in (".register(", ".invoke(", ".dispatch(", "build_ai_runtime", "ProviderRegistry", "secret_store"):
        assert forbidden not in text
    assert "tool_from_capability" in text
    assert "EffectiveToolPolicyResolver" in text
    assert 'supported_kinds=("tool",)' in text
    assert "excluded_reason_counts=resolution.excluded_reason_counts" in text


def test_catalog_api_uses_existing_runtime_and_never_builds_or_dispatches() -> None:
    text = API.read_text(encoding="utf-8")
    start = text.index('async def get_project_capability_catalog(')
    end = text.index('\n\n@router.post("/turns")', start)
    endpoint = text[start:end]
    for forbidden in ("get_or_build_ai_runtime", "build_ai_runtime", ".register(", ".dispatch(", ".invoke("):
        assert forbidden not in endpoint
    assert "capability_registry_snapshot" in endpoint
    assert "ProjectCapabilityProfileStore" in endpoint
    assert "ProjectBoundaryProfileStore" in endpoint
