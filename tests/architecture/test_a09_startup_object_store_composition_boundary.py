from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STARTUP_OBJECT_STORE_CONSUMERS = (
    "fresh_vault_shared_trust_audit_startup.py",
    "shared_trust_audit_activation_startup.py",
    "memory_publication_review_staging_startup.py",
    "external_series_candidate_startup.py",
    "project_skill_review_staging_startup.py",
    "external_series_apply_startup.py",
)


def test_a09_startup_object_store_consumers_depend_on_storage_composition() -> None:
    for filename in STARTUP_OBJECT_STORE_CONSUMERS:
        startup_path = ROOT / "src/backend/api" / filename
        tree = ast.parse(startup_path.read_text(encoding="utf-8"))
        imported_modules = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }

        assert "backend.api.routes.rebuild" not in imported_modules
        assert "backend.api.rebuild_storage_runtime" in imported_modules
