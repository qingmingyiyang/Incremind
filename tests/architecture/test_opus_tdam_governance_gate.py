from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def test_production_external_effects_freeze_core_recovery_class_and_revision_facts() -> None:
    ai_store = ROOT / "src/core/ai_kernel/sqlite_store.py"
    media_admission = ROOT / "src/core/media_hands/job_admission.py"
    model = ast.unparse(_function(ai_store, "_model_wire_attempt_effect_intent"))
    mcp = ast.unparse(_function(ai_store, "_mcp_side_effect_intent"))
    media = ast.unparse(_function(media_admission, "build"))

    assert "EffectClass.AT_MOST_ONCE" in model
    assert "EffectPurpose.PRIMARY" in model
    assert "routing_snapshot" in model and "provider" in model and "model" in model
    assert "EffectClass.QUERYABLE" in mcp
    assert "protocol_version" in mcp and "tool_id" in mcp
    assert "EffectClass.QUERYABLE" in media
    assert "manifest" in media and "provider" in media and "grant" in media


def test_tdam_governance_contracts_remain_in_their_existing_authorities() -> None:
    skill_catalog = (ROOT / "src/core/application_skill/package_catalog.py").read_text(
        encoding="utf-8"
    )
    skill_binding = (ROOT / "src/core/application_skill/binding_registry.py").read_text(
        encoding="utf-8"
    )
    skill_management = (ROOT / "src/core/application_skill/management.py").read_text(
        encoding="utf-8"
    )
    memory_lifecycle = (ROOT / "src/core/product_core/memory_lifecycle.py").read_text(
        encoding="utf-8"
    )
    for field in ("trigger_boundary", "validation", "maturity"):
        assert field in skill_catalog
    assert 'package.maturity == "deprecated"' in skill_binding
    assert "class ApplicationSkillProposalRegistry" in skill_management
    assert '"application_skill_proposals"' in skill_management
    assert 'action="binding.activate"' in skill_management
    assert 'action="binding.deactivate"' in skill_management
    for operation in (
        "preview_batch_soft_redact",
        "confirm_batch_soft_redact",
        "undo_batch_soft_redact",
    ):
        assert f"def {operation}(" in memory_lifecycle


def test_fixed_memory_gate_keeps_exactly_twenty_versioned_fictional_questions() -> None:
    corpus = json.loads(
        (ROOT / "config/companion/evaluation/memory-recall-gold-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert corpus["version"] == "1.0.0"
    assert len(corpus["queries"]) == 20
    assert len({item["id"] for item in corpus["queries"]}) == 20
    assert all(item["source_id"].startswith("fiction:") for item in corpus["documents"])

    evaluator = (
        ROOT / "src/core/companion_core/memory_vector_evaluation.py"
    ).read_text(encoding="utf-8")
    for metric in (
        "mean_retrieved_items",
        "mean_retrieved_chars",
        "p95_retrieved_chars",
        "retrieved_chars_p95_above_2000",
    ):
        assert metric in evaluator


def test_effect_runtime_is_composed_once_and_legacy_direct_writers_are_frozen() -> None:
    production_root = ROOT / "src"
    python_files = tuple(production_root.rglob("*.py"))
    runner_builders: list[str] = []
    reaper_builders: list[str] = []
    direct_writer_files: set[str] = set()
    for path in python_files:
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "EffectRunner":
                    runner_builders.append(relative)
                elif node.func.id == "EffectReaper":
                    reaper_builders.append(relative)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {
                    "plan", "plan_in_connection", "transition", "transition_in_connection"
                }
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_effect_log"
            ):
                direct_writer_files.add(relative)

    assert runner_builders == ["src/core/effect_log/runtime.py"]
    assert reaper_builders == ["src/core/effect_log/runtime.py"]
    assert direct_writer_files == {
        "src/core/ai_kernel/sqlite_store.py",
    }
