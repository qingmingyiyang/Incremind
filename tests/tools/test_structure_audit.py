from __future__ import annotations

import pytest

from tools import structure_audit as audit


@pytest.fixture
def source_root(monkeypatch, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(audit, "SRC", source)
    return source


@pytest.mark.parametrize(
    ("size", "lines", "included"),
    [
        pytest.param(51201, 1, True, id="size-only"),
        pytest.param(2001, 1001, True, id="lines-only"),
        pytest.param(51201, 1001, True, id="both-exceeded"),
        pytest.param(51200, 1, False, id="exact-size-boundary"),
        pytest.param(1999, 1000, False, id="exact-line-boundary"),
        pytest.param(51200, 1000, False, id="both-exact-boundaries"),
        pytest.param(51200, 1001, True, id="size-boundary-lines-exceeded"),
        pytest.param(51201, 1000, True, id="line-boundary-size-exceeded"),
        pytest.param(10, 1, False, id="small-file"),
    ],
)
def test_large_files_include_either_strictly_exceeded_threshold(
    source_root, size, lines, included
):
    content = b"x\n" * (lines - 1) + b"x" * (size - 2 * (lines - 1))
    (source_root / "example.py").write_bytes(content)

    expected = [{"file": "src/example.py", "bytes": size, "lines": lines}]
    assert audit.scan_large_files() == (expected if included else [])


@pytest.mark.parametrize(
    "relative_path",
    [
        "example.txt",
        "node_modules/example.py",
        "frontend/node_modules/example.js",
        "dist/example.py",
        "frontend/dist/example.js",
    ],
)
def test_large_files_keep_suffix_and_generated_directory_exclusions(
    source_root, relative_path
):
    path = source_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x\n" * 60000)

    assert audit.scan_large_files() == []


@pytest.mark.parametrize("suffix", [".py", ".js", ".jsx", ".ts", ".tsx"])
def test_large_files_scan_each_supported_code_suffix(source_root, suffix):
    (source_root / f"example{suffix}").write_bytes(b"x" * 51201)

    assert audit.scan_large_files() == [
        {"file": f"src/example{suffix}", "bytes": 51201, "lines": 1}
    ]


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("from backend import api\n", "backend.api"),
        ("from ..api import job_runtime\n", "backend.api"),
        ("import os, backend.api.job_runtime\n", "backend.api.job_runtime"),
    ],
)
def test_reverse_imports_cover_each_import_form(source_root, source, module):
    path = source_root / "backend/memory_app/probe.py"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")

    assert audit.scan_reverse_imports() == [
        {"file": "src/backend/memory_app/probe.py", "line": 1, "module": module}
    ]


def test_reverse_imports_ignore_text_and_unrelated_packages(source_root):
    path = source_root / "backend/memory_app/probe.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        '# from backend.api import job_runtime\n'
        'text = """\nimport backend.api.job_runtime\n"""\n'
        'import backend.apiculture, other.backend.api\n'
        'from backend import apiculture\n'
        'from . import api\n',
        encoding="utf-8",
    )

    assert audit.scan_reverse_imports() == []


def test_reverse_imports_report_actual_lines_and_package_relative_imports(source_root):
    path = source_root / "backend/memory_app/__init__.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        '\n\nfrom ..api import job_runtime\n'
        'def deferred():\n    from backend.api.models import Model\n',
        encoding="utf-8",
    )

    assert audit.scan_reverse_imports() == [
        {"file": "src/backend/memory_app/__init__.py", "line": 3, "module": "backend.api"},
        {
            "file": "src/backend/memory_app/__init__.py",
            "line": 5,
            "module": "backend.api.models",
        },
    ]
