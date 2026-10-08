from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "src" / "core" / "mcp_host" / "host.py"
RUNTIME = ROOT / "src" / "backend" / "api" / "mcp_runtime.py"
ADAPTER = ROOT / "src" / "backend" / "api" / "mcp_recovery_probe.py"
APP = ROOT / "src" / "backend" / "api" / "app.py"


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _call_name(node: ast.Call) -> str:
    parts: list[str] = []
    value: ast.AST = node.func
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return ".".join(reversed(parts))


def test_mcp_core_probe_is_status_only_and_cannot_replay_target_tool() -> None:
    tree = ast.parse(HOST.read_text(encoding="utf-8"))
    probe = _function(tree, "probe_completed_invocation")
    calls = [node for node in ast.walk(probe) if isinstance(node, ast.Call)]
    remote = next(
        node for node in calls if _call_name(node) == "self._recover_remote_status"
    )

    assert any(
        keyword.arg == "replay_confirmed_none"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in remote.keywords
    )
    assert "self._invoke_guarded" not in {_call_name(node) for node in calls}


def test_mcp_wire_intent_freezes_recovery_authority_without_secret_values() -> None:
    tree = ast.parse(HOST.read_text(encoding="utf-8"))
    builder = _function(tree, "_side_effect_intent")
    constants = {
        node.value for node in ast.walk(builder)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert {
        "capability_id", "capability_version", "authorization_facts_ref",
        "authorization_facts_revision", "approval_fact_ref", "execution_mode",
        "resource_locks", "timeout_ms", "tool_contract",
    }.issubset(constants)
    assert not {"secret", "token", "api_key", "authorization_header"}.intersection(
        constants
    )


def test_one_shot_recovery_session_has_no_manager_or_ai_runtime_dependency() -> None:
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    probe = _function(tree, "probe")
    calls = {_call_name(node) for node in ast.walk(probe) if isinstance(node, ast.Call)}

    assert "ScopedCapabilityRegistry" in calls
    assert "_build_approved_mcp_connection" in calls
    assert not {
        "MCPConnectionManager", "MCPConnectionManager.start",
        "build_ai_runtime", "get_or_build_ai_runtime",
    }.intersection(calls)

    source = RUNTIME.read_text(encoding="utf-8")
    builder = source[source.index("def _build_approved_mcp_connection("):]
    builder = builder[:builder.index("\n\ndef ", 1)]
    assert "_MCPSecretInjector(" in builder
    assert "credential_generation_current=injector.generations_current" in builder


def test_core_reaper_adapter_cannot_enter_target_or_full_runtime_recovery() -> None:
    source = ADAPTER.read_text(encoding="utf-8")

    assert "self._session.probe(" in source
    assert "EffectState.UNKNOWN" in source
    for forbidden in (
        "recover_completed_invocation", "_invoke_guarded", "build_ai_runtime",
        "get_or_build_ai_runtime", "MCPConnectionManager",
    ):
        assert forbidden not in source


def test_production_mcp_recovery_uses_remote_adapter_for_probe_and_verify() -> None:
    source = APP.read_text(encoding="utf-8")
    registration = source[source.index('kind="mcp_call"'):]
    registration = registration[:registration.index("))") + 2]

    assert "probe=mcp_remote_recovery_probe" in registration
    assert "verify=mcp_remote_recovery_probe" in registration
    assert "verify_mcp_call_effect" not in registration
