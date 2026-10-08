from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_effect_v2_contract_keeps_the_closed_authority_and_receipt_boundaries() -> None:
    tree = ast.parse((ROOT / "src/core/effect_log/core.py").read_text(encoding="utf-8"))
    names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    assert {"GateDecisionFact", "EffectReceipt", "derive_v2_operation_id"} <= names

    source = (ROOT / "src/core/effect_log/core.py").read_text(encoding="utf-8")
    for revision in (
        "policy", "boundary", "capability", "context_manifest", "provider", "model_route",
        "bundle", "handler", "secret", "budget", "workflow",
    ):
        assert f'"{revision}"' in source
    assert "v2 Effect planning requires a durable GateDecisionFact" in source
    assert "effect-v2 SETTLED_OK requires Receipt binding API" in source
    assert "v2 reference-only payload envelope" in source

    effect_log = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "EffectLog")
    methods = {node.name: node for node in effect_log.body if isinstance(node, ast.FunctionDef)}
    assert {"plan_v2", "plan_v2_in_connection", "_plan_v2_in_connection", "settle_ok_with_receipt_in_connection"} <= methods.keys()
    public_v2_calls = [node for node in ast.walk(methods["plan_v2"])
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert any(node.func.attr == "plan_v2_in_connection" for node in public_v2_calls)
    caller_owned = methods["plan_v2_in_connection"]
    caller_owned_calls = [node for node in ast.walk(caller_owned)
                          if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert not any(node.func.attr in {"commit", "rollback"} for node in caller_owned_calls)
    assert not any(
        node.func.attr == "execute"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.startswith("BEGIN")
        for node in caller_owned_calls
    )
    assert any(node.func.attr == "_record_gate_fact_in_connection" for node in caller_owned_calls)
    assert any(node.func.attr == "_plan_v2_in_connection" for node in caller_owned_calls)
    assert any(isinstance(node, ast.Raise) for node in ast.walk(methods["plan_in_connection"]))
    settle_calls = [node for node in ast.walk(methods["settle_ok_with_receipt_in_connection"])
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert any(node.func.attr == "_settle_ok_after_receipt_binding_in_connection" for node in settle_calls)

    assignments = [node for node in tree.body if isinstance(node, ast.Assign)]
    schema = next(node for node in assignments if any(isinstance(target, ast.Name) and target.id == "EFFECT_SCHEMA_VERSION" for target in node.targets))
    assert isinstance(schema.value, ast.Constant) and schema.value.value == 3

    identity = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "derive_v2_operation_id")
    canonical_dict = next(node for node in ast.walk(identity) if isinstance(node, ast.Dict))
    assert {key.value for key in canonical_dict.keys if isinstance(key, ast.Constant)} == {
        "session_id", "root_id", "step_key", "intent_digest",
    }
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "blake3" for node in ast.walk(identity))
