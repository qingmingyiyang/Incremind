"""资料定位策略、真实目录连接与文件句柄身份的聚焦控制。"""
from dataclasses import replace
import os
from pathlib import Path
import subprocess

import pytest

from backend.memory_app.v2 import external_workspace as workspace
from backend.memory_app.v2.external_adapters import build_launch_plan
from backend.memory_app.v2.policies import ACTIVE, get
from tests.memory_app.v2.test_external_host import setup_host


@pytest.mark.parametrize('executor', ['codex','claude-code'])
def test_folder_policy_is_frozen_and_default_stdin_and_argv_stay_exact(setup_host, executor):
    _, host, turn, original, config, _, root, _ = setup_host(executor)
    assert original.stdin_text == original.input_text == turn['input']['text']
    assert original.input_policy is None and original.material_directory is None
    folder = root / 'selected'; folder.mkdir()
    material = folder / 'agent_workspaces' / turn['turn_id']
    before = dict(ACTIVE)
    base = dict(cli_version=original.cli_version, executable=host.registrations[executor].executable,
        cwd=folder, task=turn['input']['text'], mcp_config=config, preset='folder')
    no_material = build_launch_plan(executor, **base)
    plan = build_launch_plan(executor, **base, material_directory=material)
    assert plan.input_text == turn['input']['text']
    assert plan.stdin_text == get('external_task_input', version='@1')(
        turn['input']['text'], 'agent_workspaces/' + turn['turn_id'])
    assert plan.input_policy == 'external_task_input@1' and plan.material_directory == material
    assert plan.command == no_material.command
    assert str(folder) not in plan.stdin_text and 'CONTEXT.md' in plan.stdin_text
    assert ACTIVE == before and 'external_task_input' not in ACTIVE


@pytest.mark.parametrize('change', ['stdin','policy','directory'])
def test_forged_material_launch_is_refused_before_version_process(setup_host, change):
    module, host, turn, original, config, _, root, _ = setup_host()
    folder = root / 'selected'; folder.mkdir()
    material = folder / 'agent_workspaces' / turn['turn_id']; material.mkdir(parents=True)
    permission = host.permissions.confirm_folder(folder, confirmed=True, expected_revision=0)
    plan = build_launch_plan('codex', cli_version=original.cli_version,
        executable=host.registrations['codex'].executable, cwd=folder, task=turn['input']['text'],
        mcp_config=config, preset='folder', material_directory=material)
    if change == 'stdin': plan = replace(plan, stdin_text='caller-expanded-input')
    elif change == 'policy': plan = replace(plan, input_policy='external_task_input@2')
    else: plan = replace(plan, material_directory=folder / 'agent_workspaces' / 'other-task')
    with pytest.raises(module.ExternalHostError):
        host.prepare(turn, plan, mcp_config=config, host_permission_refs={'folder':permission,'commands':None})
    assert not list(root.rglob('version-called'))


def test_confirmed_folder_replaced_by_junction_cannot_reuse_qualification(setup_host):
    _, host, _, _, _, _, root, _ = setup_host()
    folder, outside = root / 'selected', root / 'outside'
    folder.mkdir(); outside.mkdir()
    (outside / 'user-file').write_bytes(b'user-file')
    host.permissions.confirm_folder(folder, confirmed=True, expected_revision=0)
    folder.rename(root / 'old-selected')
    if os.name == 'nt':
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(folder), str(outside)],
            capture_output=True, text=True)
        assert made.returncode == 0
    else:
        folder.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError): host.permissions.folder_reference(folder)
        assert (outside / 'user-file').read_bytes() == b'user-file'
        assert list(outside.iterdir()) == [outside / 'user-file']
    finally:
        # 只移除本测试创建的连接，保留目标目录及其合成用户文件。
        if os.name == 'nt': os.rmdir(folder)
        else: folder.unlink()


def test_material_reader_rejects_same_bytes_from_replaced_open_file(tmp_path, monkeypatch):
    file = tmp_path / 'material.txt'; file.write_bytes(b'synthetic')
    original_open = workspace.os.open
    observed = []
    def changed(path, flags, *args, **kwargs):
        observed.append(True)
        file.rename(tmp_path / 'old-material.txt')
        file.write_bytes(b'synthetic')
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(workspace.os, 'open', changed)
    with pytest.raises(workspace.ExternalWorkspaceError): workspace.verify_task_material(file, b'synthetic')
    assert observed == [True]


def test_material_reader_has_exact_binary_bytes_and_file_identity(tmp_path):
    file = tmp_path / 'material.bin'; value = b'\x00\xffsynthetic'; file.write_bytes(value)
    assert workspace.verify_task_material(file, value) == workspace.task_path_identity(file)
    with pytest.raises(workspace.ExternalWorkspaceError): workspace.verify_task_material(file, value + b'x')
