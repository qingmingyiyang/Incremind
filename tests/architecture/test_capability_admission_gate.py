from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.capability_admission import (
    CapabilityAdmissionError,
    RuntimeCapabilityAdmission,
    reviewed_core_capability_ids,
)
from core.ai_kernel import CapabilityDefinition, ScopedCapabilityRegistry


ROOT = Path(__file__).resolve().parents[2]


class _Provider:
    def invoke(self, _request: object) -> dict[str, object]:
        return {"ok": True}


def _definition(capability_id: str, version: int = 1) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id, version, "read", False, "read_only",
        "crp://test/in.schema.json", "crp://test/out.schema.json",
    )


def test_reviewed_core_inventory_is_explicit_unique_and_versioned() -> None:
    capability_ids = reviewed_core_capability_ids()

    assert len(capability_ids) == len(set(capability_ids))
    assert capability_ids == (
        "memory.recall", "recognition.task.execute", "recognition.task.execute.local",
        "presentation.pptx.fixed", "analyze_source", "image.generate",
        "memory.candidate.evidence.read", "series.intake.organize.commit",
        "memory.candidate.propose.write", "workbench.input.classification.context.read",
        "developer_studio.test_lab.execute", "workbench.input.classification.enhance.write",
        "workbench.question.answer", "workbench.answer.execute", "companion.vision.context.read",
        "companion.vision.analyze.write", "project_skill.evidence.read",
        "project_skill.draft.propose", "source.evidence.read", "document.draft.propose",
        "companion.chat.context.read", "companion.chat.message.write",
        "agent.spawn", "agent.message", "agent.interrupt", "agent.wait",
        "agent.fan_in", "agent.list", "agent.plan", "external.context.execute", "external.task.execute",
    )


def test_manual_capability_registration_fails_closed_outside_reviewed_core() -> None:
    admission = RuntimeCapabilityAdmission(ScopedCapabilityRegistry())

    with pytest.raises(CapabilityAdmissionError, match="not_in_reviewed_core_inventory"):
        admission.register_core(_definition("unreviewed.extension"), _Provider())
    with pytest.raises(CapabilityAdmissionError, match="version_drift"):
        admission.register_core(_definition("memory.recall", version=2), _Provider())


def test_non_core_registration_requires_active_package_declaration_and_contribution() -> None:
    registry = ScopedCapabilityRegistry()
    manifest = SimpleNamespace(
        capability_id="fixture_package",
        capability_revision="1.0.0",
        tools=({
            "id": "fixture.preview", "exposure": "model",
            "contributes": "reader", "handler": "preview.py:preview",
        },),
    )
    admission = RuntimeCapabilityAdmission(
        registry,
        packages=SimpleNamespace(active=lambda: (manifest,)),
        contributions=SimpleNamespace(tools={"fixture.preview": lambda: None}),
    )

    admission.register_package_tool(
        _definition("fixture.preview"), _Provider(),
        package_id="fixture_package", package_revision="1.0.0", contribution_id="reader",
    )
    assert registry.get("fixture.preview") is not None
    with pytest.raises(CapabilityAdmissionError, match="contribution_mismatch"):
        admission.register_package_tool(
            _definition("fixture.preview"), _Provider(),
            package_id="fixture_package", package_revision="1.0.0",
            contribution_id="writer",
        )
    with pytest.raises(CapabilityAdmissionError, match="revision_not_active"):
        admission.register_package_tool(
            _definition("fixture.changed"), _Provider(),
            package_id="fixture_package", package_revision="2.0.0",
        )


def test_static_ai_runtime_has_only_reviewed_core_registration_view() -> None:
    rendered = (ROOT / "src/backend/memory_app/kernel/ai_runtime.py").read_text(encoding="utf-8")
    builder = next(
        node for node in ast.parse(rendered).body
        if isinstance(node, ast.FunctionDef) and node.name == "build_ai_runtime"
    )
    composition = ast.unparse(builder)

    assert "dispatch_registry = ScopedCapabilityRegistry()" in composition
    assert "registry = ReviewedCoreCapabilityRegistry(capability_admission)" in composition
    assert "dispatch_registry.register(" not in composition
    assert "registry=dispatch_registry" in composition
