from __future__ import annotations

import subprocess
import sys
import shutil
import os
from importlib.metadata import distribution
from pathlib import Path

import pytest
from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[2]
VERIFIER = ROOT / "tools" / "scripts" / "verify-runtime-dependencies.py"


def _run_verifier(requirements_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), "--requirements", str(requirements_path)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_runtime_dependency_verifier_accepts_declared_docx_and_pdf_dependencies(
    tmp_path: Path,
) -> None:
    requirements_path = tmp_path / "requirements.txt"
    requirements_path.write_text("python-docx>=1.1,<2\npypdf>=4.0,<6\n", encoding="utf-8")

    completed = _run_verifier(requirements_path)

    assert completed.returncode == 0, completed.stderr
    assert "2 requirements" in completed.stdout


def test_runtime_dependency_verifier_rejects_unsatisfied_versions(tmp_path: Path) -> None:
    requirements_path = tmp_path / "requirements.txt"
    requirements_path.write_text("fastapi>=99,<100\n", encoding="utf-8")

    completed = _run_verifier(requirements_path)

    assert completed.returncode == 1
    assert "does not satisfy" in completed.stderr


def test_runtime_dependency_verifier_rejects_unknown_distributions(tmp_path: Path) -> None:
    requirements_path = tmp_path / "requirements.txt"
    requirements_path.write_text("not-a-real-runtime-package==1.0\n", encoding="utf-8")

    completed = _run_verifier(requirements_path)

    assert completed.returncode == 1
    assert "no explicit import target" in completed.stderr


def _compile(source: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), "--requirements", str(source), "--compile-output", str(output)],
        capture_output=True, text=True, check=False,
    )


def test_compiler_preserves_nested_pep508_and_runtime_markers(tmp_path: Path) -> None:
    source = tmp_path / "requirements.txt"
    child = tmp_path / "child.txt"
    output = tmp_path / "flat.txt"
    source.write_text("# 本地包含\n-r child.txt\npython-docx>=1.1,<2\n", encoding="utf-8")
    child.write_text('uvicorn[standard]>=0.30,\\\n<1\nnot-installed; python_version < "1"\npython-docx>=1.1,<2 # 保留重复\n', encoding="utf-8")
    completed = _compile(source, output)
    assert completed.returncode == 0, completed.stderr
    requirements = [Requirement(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(requirements) == 4
    assert requirements[0].extras == {"standard"}
    assert str(requirements[0].specifier) == "<1,>=0.30"
    assert requirements[1].marker is not None
    assert requirements[2] == requirements[3]
    checked = _run_verifier(output)
    assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize("content", [
    "-r https://127.0.0.1:1/never.txt\n", "-r ../outside.txt\n",
    "-r requirements.txt\n", "-c child.txt\n", "-e .\n",
    "--index-url https://127.0.0.1:1\n", "pypdf --hash=sha256:abcd\n",
    "pypdf @ https://127.0.0.1:1/pypdf.whl\n",
    "--unknown-requirements-option\n", "-r missing.txt\n",
])
def test_compiler_rejects_unsupported_or_unsafe_semantics(tmp_path: Path, content: str) -> None:
    source = tmp_path / "requirements.txt"
    source.write_text(content, encoding="utf-8")
    output = tmp_path / "flat.txt"
    completed = _compile(source, output)
    assert completed.returncode == 2, completed.stderr
    assert not output.exists()


def test_compiler_rejects_real_linked_include_directory(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "child.txt").write_text("pypdf>=4,<6\n", encoding="utf-8")
    linked = tmp_path / "linked"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(actual)], check=True, capture_output=True)
    else:
        linked.symlink_to(actual, target_is_directory=True)
    source = tmp_path / "requirements.txt"
    source.write_text("-r linked/child.txt\n", encoding="utf-8")
    completed = _compile(source, tmp_path / "flat.txt")
    assert completed.returncode == 2, completed.stderr
    assert "linked requirements path" in completed.stderr


def test_ordinary_verifier_runs_without_pip_on_import_path(tmp_path: Path) -> None:
    source = tmp_path / "flat.txt"
    source.write_text("python-docx>=1.1,<2\npypdf>=4,<6\n", encoding="utf-8")
    isolated_site = tmp_path / "site"
    for name in ("packaging", "python-docx", "pypdf", "lxml", "typing-extensions"):
        installed = distribution(name)
        for entry in installed.files:
            if ".." in entry.parts or not installed.locate_file(entry).is_file():
                continue
            destination = isolated_site / entry
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(installed.locate_file(entry), destination)
    assert not (isolated_site / "pip").exists()
    result = subprocess.run([
        sys.executable, "-S", "-c",
        "import importlib.util,runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
        "assert importlib.util.find_spec('pip') is None; "
        "sys.argv=sys.argv[1:]; namespace=runpy.run_path(sys.argv[0],run_name='verifier_without_pip'); "
        "status=namespace['main'](); assert 'pip' not in sys.modules; raise SystemExit(status)",
        str(isolated_site), str(VERIFIER), "--requirements", str(source),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
