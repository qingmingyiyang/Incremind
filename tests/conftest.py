from __future__ import annotations

import os
from pathlib import Path
import sys

from tests import _path_setup  # noqa: F401


scripts_dir = Path(sys.executable).resolve().parent / "Scripts"
os.environ["PATH"] = os.pathsep.join([str(scripts_dir), os.environ.get("PATH", "")])
