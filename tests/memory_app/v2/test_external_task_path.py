"""宿主复用目录边界，检查发生在路径解析之前。"""
from pathlib import Path

import pytest

from backend.memory_app.v2 import external_workspace


def test_absolute_new_task_path_is_allowed(tmp_path):
    assert external_workspace.validate_task_path(tmp_path / 'new-task') is None


@pytest.mark.parametrize('path', [Path('relative'), Path('relative/../task'), 'C:/task', None])
def test_ambiguous_path_is_rejected(path):
    with pytest.raises(external_workspace.ExternalWorkspaceError, match='external_workspace_invalid'):
        external_workspace.validate_task_path(path)


def test_absolute_parent_traversal_is_rejected(tmp_path):
    with pytest.raises(external_workspace.ExternalWorkspaceError, match='external_workspace_invalid'):
        external_workspace.validate_task_path(tmp_path / '..' / 'task')


def test_existing_directory_is_not_modified(tmp_path):
    marker = tmp_path / 'marker.txt'
    marker.write_text('已有材料', encoding='utf-8')
    external_workspace.validate_task_path(tmp_path)
    assert marker.read_text(encoding='utf-8') == '已有材料'
