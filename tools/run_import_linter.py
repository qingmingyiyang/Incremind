from __future__ import annotations

import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
sys.path.insert(0, SRC)
existing = os.environ.get("PYTHONPATH", "")
os.environ["PYTHONPATH"] = SRC if not existing else os.pathsep.join([SRC, existing])

from importlinter.cli import lint_imports_command  # noqa: E402


if __name__ == "__main__":
    lint_imports_command()
