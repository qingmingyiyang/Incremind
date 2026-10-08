from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.plugin_hands.windows_acl import PluginHandsAclError, grant_appcontainer_workspace_acl
from core.plugin_hands.windows_appcontainer import AppContainerLaunchSpec, AppContainerProfile, AppContainerResourceLimits, launch_in_appcontainer

LIMITS = AppContainerResourceLimits(256 * 1024 * 1024, 2500)


def _system_environment(workspace: Path, executable: Path) -> dict[str, str]:
    names = ("ALLUSERSPROFILE", "APPDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATHEXT", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "PUBLIC", "SystemDrive", "SYSTEMROOT", "USERPROFILE", "WINDIR")
    environment = {name: os.environ[name] for name in names if name in os.environ}
    environment.update({"PATH": str(executable.parent), "TEMP": str(workspace / "tmp"), "TMP": str(workspace / "tmp")})
    return environment


def test_acl_requires_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("core.plugin_hands.windows_acl.os.name", "posix")
    with pytest.raises(PluginHandsAclError, match="requires Windows"):
        grant_appcontainer_workspace_acl(tmp_path.resolve(), 123)


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer ACL probe")
def test_real_appcontainer_acl_allows_only_lease_input_output_and_tmp(tmp_path: Path) -> None:
    parent_sentinel = tmp_path / "parent-sentinel.txt"
    parent_sentinel.write_text("parent", encoding="utf-8")
    workspace = tmp_path / "lease-workspace"
    input_dir = workspace / "input"
    output_dir = workspace / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir()
    (workspace / "code" / "payload").mkdir(parents=True)
    (workspace / "code" / "payload" / "main.py").write_text("# staged", encoding="utf-8")
    (input_dir / "source.txt").write_text("input-ok", encoding="utf-8")
    comspec = Path(os.environ["COMSPEC"]).resolve()
    with AppContainerProfile.create() as profile:
        acl = grant_appcontainer_workspace_acl(workspace.resolve(), profile.sid, allowed_resources=("workspace_input", "workspace_output"))
        assert acl.tmp_dir == workspace / "tmp"
        command = "type input\\source.txt > output\\seen.txt & echo tmp-ok > tmp\\seen.txt & echo input-denied > input\\worker-write.txt 2>nul & echo root-denied > root-write.txt 2>nul & echo parent-denied > ..\\parent-sentinel.txt 2>nul & exit /b 0"
        process = launch_in_appcontainer(AppContainerLaunchSpec(comspec, ("/d", "/c", command), workspace.resolve(), _system_environment(workspace, comspec)), profile, resource_limits=LIMITS)
        try:
            assert process.is_appcontainer() is True
            assert process.wait(10) == 0
        finally:
            process.close()
    assert (output_dir / "seen.txt").read_text(encoding="utf-8").strip() == "input-ok"
    assert (acl.tmp_dir / "seen.txt").read_text(encoding="utf-8").strip() == "tmp-ok"
    assert not (input_dir / "worker-write.txt").exists()
    assert not (workspace / "root-write.txt").exists()
    assert parent_sentinel.read_text(encoding="utf-8") == "parent"


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer ACL probe")
def test_read_scope_does_not_create_or_grant_output_or_tmp(tmp_path: Path) -> None:
    workspace = tmp_path / "lease-workspace"
    input_dir = workspace / "input"
    input_dir.mkdir(parents=True)
    (workspace / "code" / "payload").mkdir(parents=True)
    (workspace / "code" / "payload" / "main.py").write_text("# staged", encoding="utf-8")
    (input_dir / "source.txt").write_text("input-ok", encoding="utf-8")
    comspec = Path(os.environ["COMSPEC"]).resolve()
    with AppContainerProfile.create() as profile:
        acl = grant_appcontainer_workspace_acl(workspace.resolve(), profile.sid, allowed_resources=("workspace_input",))
        assert acl.output_dir is None and acl.tmp_dir is None
        assert not (workspace / "output").exists() and not (workspace / "tmp").exists()
        command = "type input\\source.txt > output\\blocked.txt 2>nul & echo temp > tmp\\blocked.txt 2>nul & exit /b 0"
        process = launch_in_appcontainer(AppContainerLaunchSpec(comspec, ("/d", "/c", command), workspace.resolve(), _system_environment(workspace, comspec)), profile, resource_limits=LIMITS)
        try:
            assert process.wait(10) == 0
        finally:
            process.close()
    assert not (workspace / "output" / "blocked.txt").exists()
    assert not (workspace / "tmp" / "blocked.txt").exists()
