from __future__ import annotations

import ast
from configparser import ConfigParser
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = "core_must_not_import_backend"
CONTRACT_SECTION = f"importlinter:contract:{CONTRACT}"


def _run_contract(root: Path, config: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports_command; lint_imports_command()",
            "--config",
            str(config),
            "--contract",
            CONTRACT,
            "--no-cache",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def _assert_core_import_boundary(root: Path, config: Path) -> None:
    # import-linter 的全局类型导入例外不适用于 core 的依赖方向约束。
    paths = sorted((root / "src/core").rglob("*.py"))
    assert paths, "core source tree is missing"
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules = [node.module] if node.module else []
            else:
                continue
            for module in modules:
                if module == "backend" or module.startswith("backend."):
                    relative = path.relative_to(root / "src").as_posix()
                    violations.append(
                        f"{relative}:{node.lineno}: forbidden backend import {module}"
                    )
    assert not violations, "\n".join(violations)

    result = _run_contract(root, config)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 kept, 0 broken" in result.stdout


@pytest.fixture
def isolated_source_tree(tmp_path: Path) -> tuple[Path, Path]:
    configured = ConfigParser()
    configured.read(ROOT / ".importlinter", encoding="utf-8")
    assert configured.has_section(CONTRACT_SECTION), "core/backend contract is missing"

    # 复用真实契约，隔离的只有源码图，避免负例碰触生产文件或其他历史契约。
    isolated = ConfigParser()
    for section in ("importlinter", CONTRACT_SECTION):
        isolated[section] = dict(configured[section])
    config = tmp_path / ".importlinter"
    with config.open("w", encoding="utf-8") as handle:
        isolated.write(handle)

    for package in ("core", "backend"):
        package_dir = tmp_path / "src" / package
        package_dir.mkdir(parents=True)
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src/core/helper.py").write_text("VALUE = 42\n", encoding="utf-8")
    (tmp_path / "src/core/backend.py").write_text("VALUE = 42\n", encoding="utf-8")
    (tmp_path / "src/backend/adapter.py").write_text(
        "from core.helper import VALUE\n", encoding="utf-8"
    )
    return tmp_path, config


def test_real_core_and_backend_keep_the_registered_dependency_contract() -> None:
    _assert_core_import_boundary(ROOT, ROOT / ".importlinter")


@pytest.mark.parametrize(
    "source",
    [
        "from .helper import VALUE\n",
        "import json\nfrom json import loads as parse\n",
        'TEXT = "from backend import adapter"\n',
        "from .backend import VALUE\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import backend\n",
    ],
    ids=[
        "core-relative-import",
        "standard-library",
        "import-looking-string",
        "relative-backend-module",
        "relative-type-import",
    ],
)
def test_core_contract_allows_internal_imports_and_backend_composition(
    isolated_source_tree: tuple[Path, Path], source: str
) -> None:
    root, config = isolated_source_tree
    (root / "src/core/policy.py").write_text(source, encoding="utf-8")
    _assert_core_import_boundary(root, config)


@pytest.mark.parametrize(
    "source",
    [
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import backend\n",
        "from typing import TYPE_CHECKING as TYPES\nif TYPES:\n    import backend.adapter as transport\n",
        "import typing as types\nif types.TYPE_CHECKING:\n    from backend import adapter as transport\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from backend.adapter import VALUE as answer\n",
    ],
    ids=["direct-type-guard", "guard-alias", "typing-alias-parent-import", "member-alias"],
)
def test_core_boundary_rejects_backend_imports_under_type_checking(
    isolated_source_tree: tuple[Path, Path], source: str,
) -> None:
    root, config = isolated_source_tree
    _assert_core_import_boundary(root, config)
    (root / "src/core/policy.py").write_text(source, encoding="utf-8")

    with pytest.raises(AssertionError, match=r"core/policy.py:\d+: forbidden backend import"):
        _assert_core_import_boundary(root, config)


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        ("core/policy.py", "import backend\n"),
        ("core/policy.py", "import backend.adapter\n"),
        ("core/policy.py", "import backend.adapter as transport\n"),
        ("core/policy.py", "from backend import adapter\n"),
        ("core/policy.py", "from backend import adapter as transport\n"),
        ("core/policy.py", "from backend.adapter import VALUE as answer\n"),
        ("core/policy.py", "def resolve():\n    from backend import adapter\n"),
        ("core/__init__.py", "from backend import adapter\n"),
    ],
    ids=[
        "root-import",
        "module-import",
        "module-alias",
        "parent-imports-child",
        "parent-imports-child-alias",
        "member-alias",
        "deferred-import",
        "package-initializer",
    ],
)
def test_core_contract_rejects_backend_imports_in_the_actual_tool(
    isolated_source_tree: tuple[Path, Path], relative: str, source: str
) -> None:
    root, config = isolated_source_tree
    baseline = _run_contract(root, config)
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr

    (root / "src" / relative).write_text(source, encoding="utf-8")
    result = _run_contract(root, config)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "0 kept, 1 broken" in result.stdout
    assert "core is not allowed to import backend" in result.stdout


def test_core_contract_rejects_backend_imports_through_an_internal_module(
    isolated_source_tree: tuple[Path, Path],
) -> None:
    root, config = isolated_source_tree
    (root / "src/core/policy.py").write_text(
        "from .helper import VALUE\n", encoding="utf-8"
    )
    baseline = _run_contract(root, config)
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr

    (root / "src/core/helper.py").write_text(
        "from backend.adapter import VALUE\n", encoding="utf-8"
    )
    result = _run_contract(root, config)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "0 kept, 1 broken" in result.stdout
    assert "core.helper -> backend.adapter" in result.stdout
