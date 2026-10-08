from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
KERNEL = ROOT / "src" / "core" / "ai_kernel"
AGENT_MODULES = (
    KERNEL / "agent_contracts.py",
    KERNEL / "agent_profiles.py",
    KERNEL / "agent_store.py",
)
COORDINATOR = ROOT / "src" / "backend" / "memory_app" / "kernel" / "agent_coordinator.py"
NATIVE_CAPABILITIES = ROOT / "src" / "backend" / "api" / "agent_capabilities.py"


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.append(node.module)
    return tuple(values)


def _annotated_fields(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                child.target.id
                for child in node.body
                if isinstance(child, ast.AnnAssign)
                and isinstance(child.target, ast.Name)
            }
    raise AssertionError(f"missing class {class_name}")


def test_internal_agent_kernel_does_not_reactivate_legacy_or_provider_authority() -> None:
    for path in AGENT_MODULES:
        imports = _imports(path)
        assert not any(name.startswith("backend.agent_graph") for name in imports)
        assert not any(name.startswith("backend.security") for name in imports)
        assert not any(name.startswith("core.model_gateway") for name in imports)
        assert "fastapi" not in imports


def test_agent_profiles_and_runs_store_only_canonical_model_tier() -> None:
    forbidden = {"provider_id", "model_name", "endpoint", "secret", "api_key"}
    contracts = KERNEL / "agent_contracts.py"
    profiles = KERNEL / "agent_profiles.py"
    assert not (_annotated_fields(contracts, "AgentProfile") & forbidden)
    assert not (_annotated_fields(contracts, "AgentRun") & forbidden)
    assert not (_annotated_fields(profiles, "AgentProfileTierResolution") & forbidden)


def test_agent_store_shares_but_never_mutates_turn_authority() -> None:
    source = (KERNEL / "agent_store.py").read_text(encoding="utf-8").lower()
    assert "select 1 from ai_turns" in source
    for forbidden in (
        "create table if not exists ai_turns",
        "insert into ai_turns",
        "update ai_turns",
        "delete from ai_turns",
    ):
        assert forbidden not in source
    for required in (
        "pragma foreign_keys=on",
        "pragma journal_mode=wal",
        "pragma synchronous=full",
        "begin immediate",
    ):
        assert required in source


def test_builtin_agent_capability_templates_use_real_governed_ids() -> None:
    source = (KERNEL / "agent_profiles.py").read_text(encoding="utf-8")
    assert "library.search" not in source
    assert "library.write" not in source
    for required in (
        '"memory.recall"',
        '"source.evidence.read"',
        '"document.draft.propose"',
        '"agent.spawn"',
        '"agent.fan_in"',
    ):
        assert required in source


def test_agent_coordinator_reuses_turn_authority_and_never_owns_execution() -> None:
    imports = _imports(COORDINATOR)
    assert not any(name.startswith("backend.agent_graph") for name in imports)
    assert not any(name.startswith("backend.security") for name in imports)
    assert not any(name.startswith("core.model_gateway") for name in imports)
    assert "fastapi" not in imports
    assert "sqlite3" not in imports
    source = COORDINATOR.read_text(encoding="utf-8")
    for forbidden in (
        "ThreadPoolExecutor",
        "CREATE TABLE",
        "INSERT INTO ai_turns",
        "UPDATE ai_turns",
        "DELETE FROM ai_turns",
    ):
        assert forbidden not in source
    for required in (
        "self._runtime.accept_turn",
        "self._runner.accept_and_submit",
        "self._store.reserve_spawn",
        "self._store.finalize_spawn",
    ):
        assert required in source

    tree = ast.parse(source)
    child_request = next(node for node in ast.walk(tree)
                         if isinstance(node, ast.FunctionDef)
                         and node.name == "_child_turn_request")
    outcome_values = [value for node in ast.walk(child_request)
                      if isinstance(node, ast.Dict)
                      for key, value in zip(node.keys, node.values)
                      if isinstance(key, ast.Constant) and key.value == "desired_outcome"]
    assert len(outcome_values) == 1
    outcome = outcome_values[0]
    assert isinstance(outcome, ast.IfExp)
    assert ast.unparse(outcome.test) == "parent_request['desired_outcome'] == 'project.task'"
    assert isinstance(outcome.body, ast.Constant) and outcome.body.value == "project.task"
    assert isinstance(outcome.orelse, ast.Constant) and outcome.orelse.value == "agent.child.execute"


def test_native_agent_capabilities_are_thin_host_governed_adapters() -> None:
    imports = _imports(NATIVE_CAPABILITIES)
    assert not any(name.startswith("backend.agent_graph") for name in imports)
    assert "fastapi" not in imports
    assert "sqlite3" not in imports
    source = NATIVE_CAPABILITIES.read_text(encoding="utf-8")
    for required in (
        '"agent.spawn"',
        '"agent.message"',
        '"agent.interrupt"',
        '"agent.wait"',
        '"agent.fan_in"',
        '"agent.list"',
        'effect_certainty="confirmed_none"',
    ):
        assert required in source
    assert "ProviderRegistry" not in source
    assert "SecretStore" not in source
