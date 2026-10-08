"""原生外部执行可并行，普通不可逆能力仍须独占。"""

from dataclasses import asdict, is_dataclass, replace
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from core.ai_tooling import ToolContractError, ToolConnectionIdentity, ToolRetryPolicy
from tests.rebuild.test_external_execution_manifest import external_capability


def parallel_tool(**changes):
    tool = external_capability().tool_definition
    return replace(tool, **{"execution_mode": "parallel", **changes})


def test_exact_external_native_tool_can_use_parallel_without_claiming_reversibility():
    tool = parallel_tool()
    assert tool.execution_mode == "parallel"
    assert tool.mutability == "irreversible"
    assert tool.effect == "external"
    assert tool.operation_semantics == "receipt_required"
    assert tool.idempotency == "never_retry"
    assert tool.timeout_ms == 1_230_000


NATIVE_REJECTIONS = [
    ("tool_id", "external.other.execute"),
    ("version", 2),
    ("version", True),
    ("source", "plugin"),
    ("owner_id", "another-runner"),
    ("effect", "write"),
    ("effect", "delete"),
    ("destination", "platform"),
    ("destination", "local"),
    ("operation_semantics", "none"),
    ("receipt_schema_uri", None),
    ("egress_class", "none"),
    ("idempotency", "idempotent"),
    ("retry_policy", ToolRetryPolicy(2, 0, ())),
    ("retry_policy", ToolRetryPolicy(1, 1, ())),
    ("retry_policy", ToolRetryPolicy(True, 0, ())),
    ("boundary_requirements", ()),
    ("boundary_requirements", ("external_execute", "draft_create_only")),
    ("timeout_ms", 60_000),
    ("timeout_ms", 1_230_001),
    ("data_classes", ()),
    ("network_scope", ()),
    ("data_egress_scope", ()),
    ("available", False),
    ("nested_model_handle_budget", 2),
    ("verification_tool_id", "verify-external"),
    ("compensation_tool_id", "compensate-external"),
    ("connection_identity", ToolConnectionIdentity(
        "mcp", "another-server", "2025-11-25", 1,
        "another-endpoint", "another-owner", 1, 1, 1,
    )),
]


@pytest.mark.parametrize("field,value", NATIVE_REJECTIONS)
def test_parallel_irreversible_exception_does_not_admit_other_native_contracts(field, value):
    with pytest.raises(ToolContractError):
        parallel_tool(**{field: value})


def test_other_irreversible_tool_keeps_original_exclusive_contract():
    original = external_capability().tool_definition
    ordinary = replace(original, tool_id="ordinary.external", execution_mode="exclusive", boundary_requirements=())
    assert ordinary.execution_mode == "exclusive"
    with pytest.raises(ToolContractError, match="must execute exclusively"):
        replace(ordinary, execution_mode="parallel")


def test_external_parallel_native_projection_matches_public_tool_schema():
    schema_path = Path(__file__).resolve().parents[2] / "core-contracts/ai/tool-definition.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    payload = json.loads(json.dumps({"schema_version": "2.0.0", **asdict(parallel_tool())}))
    errors = tuple(Draft202012Validator(schema).iter_errors(payload))
    assert errors == (), [error.message for error in errors]


@pytest.mark.parametrize("field,value", NATIVE_REJECTIONS)
def test_public_schema_rejects_expanded_parallel_irreversible_contract(field, value):
    schema_path = Path(__file__).resolve().parents[2] / "core-contracts/ai/tool-definition.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    payload = json.loads(json.dumps({"schema_version": "2.0.0", **asdict(parallel_tool())}))
    payload[field] = json.loads(json.dumps(asdict(value) if is_dataclass(value) else value))
    assert tuple(Draft202012Validator(schema).iter_errors(payload))
