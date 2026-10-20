from __future__ import annotations

import pytest

from tools.python_imports import collect_package_imports


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import backend.api\n", [(1, "backend.api")]),
        ("import backend.api.jobs as jobs\n", [(1, "backend.api.jobs")]),
        ("import os, backend.api.jobs as jobs\n", [(1, "backend.api.jobs")]),
        ("from backend.api import job_runtime, models\n", [(1, "backend.api")]),
        ("from backend.api.jobs import Job\n", [(1, "backend.api.jobs")]),
        ("from backend import api as legacy, other\n", [(1, "backend.api")]),
        ("from ..api import job_runtime\n", [(1, "backend.api")]),
        ("from .. import api\n", [(1, "backend.api")]),
        (
            "from backend import (\n    other,\n    api as legacy,\n)\n",
            [(1, "backend.api")],
        ),
        ("from backend.api \\\n    import job_runtime\n", [(1, "backend.api")]),
        ("def deferred():\n    import backend.api.jobs\n", [(2, "backend.api.jobs")]),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    from backend import api\n",
            [(3, "backend.api")],
        ),
        ("class Consumer:\n    from backend.api import Job\n", [(2, "backend.api")]),
        ("from backend.api import *\n", [(1, "backend.api")]),
    ],
)
def test_collect_package_imports_covers_static_python_syntax(tmp_path, source, expected):
    path = tmp_path / "backend/memory_app/probe.py"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")

    assert collect_package_imports(path, tmp_path, "backend.api") == expected


@pytest.mark.parametrize(
    ("relative_path", "source", "expected"),
    [
        ("backend/memory_app/__init__.py", "from ..api import jobs\n", [(1, "backend.api")]),
        ("backend/memory_app/v2/__init__.py", "from ... import api\n", [(1, "backend.api")]),
        ("backend/memory_app/__init__.py", "from .api import jobs\n", []),
    ],
)
def test_relative_imports_use_the_package_directory(tmp_path, relative_path, source, expected):
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")

    assert collect_package_imports(path, tmp_path, "backend.api") == expected


def test_collector_ignores_comments_strings_and_other_package_boundaries(tmp_path):
    path = tmp_path / "backend/memory_app/probe.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        '# import backend.api\n'
        'text = """\nfrom backend.api import job_runtime\n"""\n'
        'import backend.apiculture, other.backend.api\n'
        'from backend import apiculture\n'
        'from backend.memory_app import api\n'
        'from . import api\n',
        encoding="utf-8",
    )

    assert collect_package_imports(path, tmp_path, "backend.api") == []


@pytest.mark.parametrize("relative_path", ["probe.py", "backend/memory_app/probe.py"])
def test_excessive_relative_import_levels_report_the_file(tmp_path, relative_path):
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("from ...api import jobs\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid relative import") as captured:
        collect_package_imports(path, tmp_path, "backend.api")
    assert f"{path}:1:" in str(captured.value)


def test_invalid_python_is_reported_instead_of_silently_ignored(tmp_path):
    path = tmp_path / "probe.py"
    path.write_text("from backend import (\n", encoding="utf-8")

    with pytest.raises(SyntaxError) as captured:
        collect_package_imports(path, tmp_path, "backend.api")
    assert captured.value.filename == str(path)


def test_collector_obeys_python_source_encoding(tmp_path):
    path = tmp_path / "probe.py"
    path.write_bytes(b'# coding: latin-1\nlabel = "caf\xe9"\nimport backend.api\n')

    assert collect_package_imports(path, tmp_path, "backend.api") == [(3, "backend.api")]
