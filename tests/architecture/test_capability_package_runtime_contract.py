from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE_INTAKE = ROOT / "src" / "core" / "plugin_host" / "package_intake.py"
CAPABILITY_PACKAGES = ROOT / "src" / "core" / "capability_packages"
CORE_ZERO_CHANGE_PATHS = (
    ROOT / "src" / "core" / "ai_kernel",
    ROOT / "src" / "core" / "job_runner",
    ROOT / "src" / "backend" / "security" / "project_boundary_profiles.py",
    ROOT / "src" / "backend" / "security" / "secrets.py",
    ROOT / "src" / "core" / "memory_core",
    ROOT / "src" / "frontend" / "src",
)


def test_normalized_capability_package_declares_core_runtime_ownership() -> None:
    source = PACKAGE_INTAKE.read_text(encoding="utf-8")
    for required in (
        '"core_api": "1"',
        '"execution_state_owner": "core_effect_log"',
        '"recovery_owner": "core_reaper"',
        '"secret_access": "lease_reference_only"',
        '"memory_write": "proposal_only"',
        '"document_write": "draft_only"',
        '"policy_predicates": "closed_core_set"',
    ):
        assert required in source


def test_plugin_package_parsers_cannot_admit_private_runtime_authority_fields() -> None:
    tree = ast.parse(PACKAGE_INTAKE.read_text(encoding="utf-8"))
    string_values = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    forbidden_manifest_fields = {
        "effect_states", "recovery_handler", "secret_value", "memory_writer",
        "document_writer", "policy_predicate", "database_schema",
    }
    assert forbidden_manifest_fields.isdisjoint(string_values)


def test_executable_provider_and_migration_groups_remain_quarantined() -> None:
    source = PACKAGE_INTAKE.read_text(encoding="utf-8")
    assert '_EXECUTABLE_GROUPS = frozenset({"model-providers", "migrations"})' in source
    assert '"executable_contribution_requires_local_review"' in source


def test_new_timeline_package_needs_no_capability_specific_core_branch() -> None:
    package_id = "timeline_preview"
    violations: list[str] = []
    for target in CORE_ZERO_CHANGE_PATHS:
        paths = (target,) if target.is_file() else tuple(target.rglob("*"))
        for path in paths:
            if path.is_file() and path.suffix in {".py", ".js", ".jsx", ".ts", ".tsx"}:
                if package_id in path.read_text(encoding="utf-8", errors="ignore"):
                    violations.append(str(path.relative_to(ROOT)))
    assert violations == []


def test_all_capability_packages_have_no_private_runtime_authority_imports() -> None:
    forbidden_roots = {
        "backend", "core.ai_kernel", "core.effect_log", "core.job_runner",
        "core.mcp_host", "core.model_gateway", "core.memory_core",
        "core.document_engine", "core.project_skill_core",
    }
    violations: list[str] = []
    for path in CAPABILITY_PACKAGES.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if any(name == root or name.startswith(root + ".") for root in forbidden_roots):
                    violations.append(f"{path.relative_to(ROOT)}:{name}")
    assert violations == []
