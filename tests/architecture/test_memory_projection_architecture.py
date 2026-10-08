from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_MODULE = (
    ROOT
    / "src"
    / "core"
    / "product_core"
    / "memory_projection_contract.py"
)
BUILDER_MODULE = (
    ROOT
    / "src"
    / "core"
    / "product_core"
    / "memory_projection_builder.py"
)
AUTHORITY_CONTRACT_MODULE = (
    ROOT
    / "src"
    / "core"
    / "product_core"
    / "memory_projection_authority_contract.py"
)
LEGACY_JOB_MODULE = "core.product_core.memory_projection_rebuild_job"
PRODUCTION_CONSUMERS = (
    ROOT / "src" / "core" / "product_core" / "memory_projection_authority.py",
    ROOT / "src" / "core" / "product_core" / "progressive_direct_question_recall.py",
    ROOT / "src" / "core" / "product_core" / "project_brain_projection_status.py",
    ROOT / "src" / "core" / "product_core" / "memory_projection_observability.py",
    ROOT / "src" / "core" / "product_core" / "progressive_memory_scale_benchmark.py",
    ROOT / "src" / "core" / "product_core" / "memory_projection_rebuild_effect_admission.py",
    ROOT / "src" / "core" / "product_core" / "memory_projection_rebuild_effect_execution.py",
    ROOT / "src" / "backend" / "api" / "memory_projection_effect_runtime.py",
    ROOT / "src" / "backend" / "api" / "routes" / "rebuild.py",
)


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_projection_contract_and_builder_have_no_runtime_or_storage_dependency() -> None:
    assert _imported_roots(CONTRACT_MODULE) <= {
        "__future__",
        "dataclasses",
    }
    assert _imported_roots(BUILDER_MODULE) <= {
        "__future__",
        "collections",
        "datetime",
        "hashlib",
        "json",
        "re",
        "core",
        "typing",
    }


def test_authority_snapshot_contract_is_pure_domain_code() -> None:
    assert _imported_roots(AUTHORITY_CONTRACT_MODULE) <= {
        "__future__",
        "collections",
        "dataclasses",
        "core",
        "typing",
    }


def test_production_consumers_do_not_depend_on_legacy_projection_job() -> None:
    for consumer in PRODUCTION_CONSUMERS:
        tree = ast.parse(consumer.read_text(encoding="utf-8"))
        assert all(
            not (
                isinstance(node, ast.ImportFrom)
                and node.module == LEGACY_JOB_MODULE
            )
            and not (
                isinstance(node, ast.Import)
                and any(alias.name == LEGACY_JOB_MODULE for alias in node.names)
            )
            for node in ast.walk(tree)
        ), consumer


def test_projection_builder_cannot_write_or_call_external_authority() -> None:
    source = BUILDER_MODULE.read_text(encoding="utf-8")

    for forbidden in (
        "sqlite3",
        "sqlalchemy",
        "httpx",
        "requests",
        "subprocess",
        "socket",
        "from backend",
        "import backend",
        "team_memory",
        "project_skills.save",
        "memory_publications",
        "memory_transitions",
        "open(",
        "write_text(",
        "write_bytes(",
        "mkdir(",
        "unlink(",
    ):
        assert forbidden not in source


def test_projection_contract_marks_output_as_derived_and_non_authoritative() -> None:
    source = CONTRACT_MODULE.read_text(encoding="utf-8")

    assert '"derived": True' in source
    assert '"business_authority": False' in source
    assert '"rebuildable": True' in source
    assert '"project_skill_body_included": False' in source
    assert '"business_writes_allowed": False' in source
