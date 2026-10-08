from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _called_attributes(nodes) -> list[str]:
    return [
        node.func.attr
        for parent in nodes
        for node in ast.walk(parent)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]


def test_hook_enabled_tool_path_never_calls_dynamic_boundary_evaluator() -> None:
    function = _function(ROOT / "src/core/ai_kernel/runtime.py", "_execute_tool_decision")
    branch = next(
        node for node in function.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and ast.unparse(node.test) == "self._hook_host is not None"
    )
    assert "_evaluate_execution_boundary" not in _called_attributes(branch.body)
    assert _called_attributes(branch.orelse).count("_evaluate_execution_boundary") == 1


def test_frozen_candidate_hot_path_cannot_reenter_boundary_evaluate() -> None:
    function = _function(
        ROOT / "src/backend/security/turn_frozen_authorization.py",
        "authorize_candidate",
    )
    calls = _called_attributes(function.body)
    assert "evaluate" not in calls
    assert "sanitize_candidate_arguments" in calls
    assert not {"read", "get_immutable_payload", "list"}.intersection(calls)


def test_packaged_hook_policy_is_explicitly_enabled() -> None:
    config = (ROOT / "config/codex-hooks.toml").read_text(encoding="utf-8")
    assert "enabled = true" in config
    assert "upstream_revision = \"0fe877b4dedc86a29c8bebb3edbd3efcc3580c7d\"" in config
