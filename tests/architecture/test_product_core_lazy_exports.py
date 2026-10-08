from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_runtime_modules_import_product_core_features_from_concrete_modules() -> None:
    offenders: list[str] = []
    for relative in (
        "src/backend",
        "src/core/composition.py",
    ):
        target = ROOT / relative
        paths = target.rglob("*.py") if target.is_dir() else (target,)
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if any(
                isinstance(node, ast.ImportFrom)
                and node.module in {"core.product_core", "product_core"}
                for node in ast.walk(tree)
            ):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_product_core_compatibility_exports_are_lazy() -> None:
    source = (ROOT / "src/core/product_core/__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    eager_relative_imports = [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.level > 0
    ]

    assert eager_relative_imports == []
    assert "def __getattr__" in source
