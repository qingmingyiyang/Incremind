from __future__ import annotations

import subprocess
import sys
from pathlib import Path


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
