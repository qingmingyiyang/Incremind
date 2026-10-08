from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_hands_outcome_projector_has_no_execution_runtime_dependency() -> None:
    source = (
        ROOT / "src" / "backend" / "api" / "plugin_hands_outcome_projection.py"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "PluginHandsCapabilityProvider",
        "WindowsContainedPluginHandsHost",
        "PluginHandsWorkspaceManager",
        ".invoke(",
        ".execute(",
        "recover_completed_invocation",
    ):
        assert forbidden not in source


def test_turn_recovery_projects_external_facts_before_registry_resolution() -> None:
    source = (ROOT / "src" / "core" / "ai_kernel" / "runtime.py").read_text(
        encoding="utf-8"
    )
    start = source.index("    def _resume_incomplete_tool(")
    end = source.index("    def _project_external_tool_completion(", start)
    body = source[start:end]

    assert body.index("self._project_external_tool_completion(intent, effect)") < body.index(
        "self._registry.resolve(intent.capability_id)"
    )


def test_production_ai_runtime_injects_only_read_only_hands_projector() -> None:
    source = (ROOT / "src" / "backend" / "memory_app" / "kernel" / "ai_runtime.py").read_text(
        encoding="utf-8"
    )

    assert "PluginHandsToolOutcomeProjector(" in source
    assert (
        "external_tool_outcome_projector="
        "plugin_hands_outcome_projector.project_for_runtime"
    ) in source
