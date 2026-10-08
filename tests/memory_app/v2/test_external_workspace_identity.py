"""首次原生打开目录的身份绑定；保留两个合成目录的用户文件。"""
import os

import pytest

from backend.memory_app.v2 import external_runner as runner_module
from backend.memory_app.v2 import external_workspace as workspace
from tests.memory_app.v2.test_external_local_materials import local, host_env
from tests.memory_app.v2.test_external_workspace import handoff, mcp


def test_before_open_folder_swap_never_writes_the_unconfirmed_new_directory(local, monkeypatch):
    env, owner, identity, kw, _ = local
    folder = env.root / 'selected'; folder.mkdir()
    (folder / 'user-file').write_bytes(b'original-user-file')
    ref = owner.host.permissions.confirm_folder(folder, confirmed=True, expected_revision=0)
    original = runner_module.create_task_workspace
    opened = []
    def changed(*args, **kwargs):
        opened.append(True)
        folder.rename(env.root / 'old-selected'); folder.mkdir()
        (folder / 'new-user-file').write_bytes(b'new-user-file')
        return original(*args, **kwargs)
    monkeypatch.setattr(runner_module, 'create_task_workspace', changed)
    with pytest.raises(runner_module.ExternalRunnerError):
        owner.prepare(identity, **kw, preset='folder', folder=folder,
            host_permission_refs={'folder':ref,'commands':None}, attachments={'资料.txt':b'synthetic'})
    assert opened == [True]
    assert list(folder.iterdir()) == [folder / 'new-user-file']
    assert (folder / 'new-user-file').read_bytes() == b'new-user-file'
    assert (env.root / 'old-selected' / 'user-file').read_bytes() == b'original-user-file'
    assert owner.turns.get_immutable_payload(identity, runner_module.ARCHIVE) is None
    assert env.records.list('v2_external_runs') == ()
    assert not list(env.root.rglob('version-called'))


def test_after_open_folder_swap_writes_only_the_original_approved_handle(tmp_path, monkeypatch):
    root = tmp_path / 'selected'; root.mkdir()
    (root / 'user-file').write_bytes(b'original-user-file')
    expected = workspace.task_path_identity(root, directory=True)
    changed = []
    def move_root():
        changed.append(True)
        root.rename(tmp_path / 'old-selected'); root.mkdir()
        (root / 'new-user-file').write_bytes(b'new-user-file')
    if os.name == 'nt':
        original = workspace.WindowsHandleTreeIo
        class RootSwap(original):
            def _after_root_open(self, handle):
                move_root()
        monkeypatch.setattr(workspace, 'WindowsHandleTreeIo', RootSwap)
    else:
        original = workspace.os.open
        def opened(path, flags, *args, **kwargs):
            descriptor = original(path, flags, *args, **kwargs)
            if path == root: move_root()
            return descriptor
        monkeypatch.setattr(workspace.os, 'open', opened)
    workspace.create_task_workspace(root, 'turn-handle', task='合成任务', handoff=handoff(),
        mcp_config=mcp(), expected_root_identity=expected)
    assert changed == [True]
    assert list(root.iterdir()) == [root / 'new-user-file']
    assert (root / 'new-user-file').read_bytes() == b'new-user-file'
    original_root = tmp_path / 'old-selected'
    assert (original_root / 'user-file').read_bytes() == b'original-user-file'
    assert (original_root / 'agent_workspaces' / 'turn-handle' / 'TASK.md').read_text(encoding='utf-8') == '合成任务'
