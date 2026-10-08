from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "core" / "external_extensions"


def test_external_extension_contract_has_no_runtime_or_authority_dependencies() -> None:
    forbidden_modules = {
        "subprocess",
        "socket",
        "requests",
        "httpx",
        "urllib.request",
        "core.effect_log",
        "core.storage_provider",
        "core.secret_store",
        "core.ai_kernel",
        "core.mcp_host",
        "core.plugin_hands",
    }
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)
        assert not any(
            imported_name == forbidden or imported_name.startswith(f"{forbidden}.")
            for imported_name in imported
            for forbidden in forbidden_modules
        ), path
        assert calls.isdisjoint({"open", "connect", "run", "Popen", "system", "exec", "eval"}), path


def test_external_manifest_forbids_private_runtime_authority_vocabulary() -> None:
    contracts = (PACKAGE / "contracts.py").read_text(encoding="utf-8")
    assert 'execution_state_owner: str = "core_effect_log"' in contracts
    assert 'recovery_owner: str = "core_reaper"' in contracts
    assert 'secret_access: str = "lease_reference_only"' in contracts
    assert 'memory_write: str = "proposal_only"' in contracts
    assert 'document_write: str = "draft_only"' in contracts
    for forbidden in (
        '"capability_package"',
        '"effect_state_machine"',
        '"recovery_scheduler"',
        '"secret_reader"',
        '"memory_writer"',
        '"document_writer"',
        '"gate_predicate"',
        '"planner_prompt_patch"',
    ):
        assert forbidden not in contracts
