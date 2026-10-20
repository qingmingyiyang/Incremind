"""Freeze the current memory_app -> backend.api reverse-import debt.

The budget only ratchets down: adding a new reverse import fails this test,
while removing one passes. Regenerate the reviewed baseline with
``tools/structure_audit.py`` when the debt is deliberately reduced.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tools.python_imports import collect_package_imports

_REPO_ROOT = Path(__file__).parents[3]
_MEMORY_APP = _REPO_ROOT / "src" / "backend" / "memory_app"
_BASELINE = Path(__file__).with_name("reverse_dependency_baseline.json")


def _current() -> set[str]:
    rows: set[str] = set()
    for path in sorted(_MEMORY_APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(_REPO_ROOT).as_posix()
        for _, module in collect_package_imports(path, _REPO_ROOT / "src", "backend.api"):
            rows.add(f"{relative} -> {module}")
    return rows


def test_no_new_reverse_imports() -> None:
    baseline = set(json.loads(_BASELINE.read_text(encoding="utf-8"))["baseline"])
    added = sorted(_current() - baseline)
    assert not added, (
        "memory_app gained reverse imports into backend.api; move the dependency "
        "into memory_app.kernel/core or extend the reviewed baseline:\n"
        + "\n".join(added)
    )


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("from backend import api\n", "backend.api"),
        ("from ..api import job_runtime\n", "backend.api"),
        ("import os, backend.api.job_runtime\n", "backend.api.job_runtime"),
    ],
)
def test_budget_rejects_new_edges_in_each_import_form(
    monkeypatch, tmp_path, source, module
):
    memory_app = tmp_path / "src/backend/memory_app"
    memory_app.mkdir(parents=True)
    (memory_app / "probe.py").write_text(source, encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"baseline": []}', encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "_MEMORY_APP", memory_app)
    monkeypatch.setattr(sys.modules[__name__], "_BASELINE", baseline)

    edge = f"src/backend/memory_app/probe.py -> {module}"
    with pytest.raises(AssertionError, match="memory_app gained reverse imports"):
        test_no_new_reverse_imports()
    assert _current() == {edge}

    baseline.write_text(json.dumps({"baseline": [edge]}), encoding="utf-8")
    test_no_new_reverse_imports()


def test_budget_ignores_import_text_and_unrelated_packages(monkeypatch, tmp_path):
    memory_app = tmp_path / "src/backend/memory_app"
    memory_app.mkdir(parents=True)
    (memory_app / "probe.py").write_text(
        '# from backend.api import job_runtime\n'
        'text = """\nimport backend.api.job_runtime\n"""\n'
        'import backend.apiculture, other.backend.api\n'
        'from backend import apiculture\n'
        'from . import api\n',
        encoding="utf-8",
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"baseline": []}', encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "_MEMORY_APP", memory_app)
    monkeypatch.setattr(sys.modules[__name__], "_BASELINE", baseline)

    assert _current() == set()
    test_no_new_reverse_imports()
