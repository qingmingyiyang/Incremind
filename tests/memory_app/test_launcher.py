"""Exercise the real PowerShell startup preparation without launching a server."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("source", ["example", "local", "existing"])
def test_launcher_prepares_config_from_checkout_without_overwriting_runtime(tmp_path, source):
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is required to execute the Windows launcher")
    root = Path(__file__).resolve().parents[2]
    shutil.copyfile(root / "run-web.ps1", tmp_path / "run-web.ps1")
    config = tmp_path / "config"
    config.mkdir()
    example = (root / "config/settings.toml.example").read_bytes()
    (config / "settings.toml.example").write_bytes(example)
    expected = example
    if source == "local":
        expected = b"# existing local settings\n"
        (config / "settings.toml").write_bytes(expected)
    runtime_config = tmp_path / "runtime/config/settings.toml"
    if source == "existing":
        runtime_config.parent.mkdir(parents=True)
        expected = b"# existing runtime settings must win\n"
        runtime_config.write_bytes(expected)
    completed = subprocess.run([shell, "-NoProfile", "-File", str(tmp_path / "run-web.ps1")],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    # The isolated checkout intentionally has no interpreter. The exact launcher
    # performs config setup first and then stops before starting any service.
    assert completed.returncode != 0
    assert "Create the project Python environment" in completed.stderr
    assert runtime_config.read_bytes() == expected
