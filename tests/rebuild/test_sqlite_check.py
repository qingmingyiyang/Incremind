import subprocess
import sys
from pathlib import Path

from tools.sqlite_check import sqlite_version_supported


def test_fixed_sqlite_release_branches():
    for version in ((3, 50, 7), (3, 50, 8), (3, 51, 3), (3, 54, 1)):
        assert sqlite_version_supported(version)
    for version in ((3, 44, 6), (3, 49, 1), (3, 50, 6), (3, 51, 0), (3, 51, 2)):
        assert not sqlite_version_supported(version)


def test_check_reports_actual_loaded_library():
    import sqlite3

    script = Path(__file__).resolve().parents[2] / 'tools' / 'sqlite_check.py'
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)
    assert result.stdout.strip() == sqlite3.sqlite_version
    assert result.returncode == (0 if sqlite_version_supported(sqlite3.sqlite_version_info) else 1)
