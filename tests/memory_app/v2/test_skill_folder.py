"""在真实临时目录验证显式导出、不覆盖和重解析点边界。"""
from pathlib import Path
import subprocess

import pytest

from backend.memory_app.v2.skill_folder import write_reviewed_folder


@pytest.fixture
def files():
    return {'SKILL.md': '---\nname: choose-gift\ndescription: 挑礼物时使用\n---\n'.encode(),
        'references/methods.md': '认识及修订：合成事实\n'.encode()}


def test_writes_exact_bytes_under_existing_selected_directory(tmp_path, files):
    root = tmp_path / '用户选定目录'
    root.mkdir()
    result = write_reviewed_folder(str(root), 'choose-gift', files)
    assert result == str(root / 'choose-gift')
    assert {str(path.relative_to(root / 'choose-gift')).replace('\\', '/'): path.read_bytes()
        for path in (root / 'choose-gift').rglob('*') if path.is_file()} == files
    assert files['SKILL.md'].startswith(b'---\n')


def test_identical_existing_tree_is_idempotent_and_unchanged(tmp_path, files):
    first = write_reviewed_folder(str(tmp_path), 'choose-gift', files)
    before = {name: ((Path(first) / name).read_bytes(), (Path(first) / name).stat().st_mtime_ns)
        for name in files}
    assert write_reviewed_folder(str(tmp_path), 'choose-gift', dict(files)) == first
    assert {name: ((Path(first) / name).read_bytes(), (Path(first) / name).stat().st_mtime_ns)
        for name in files} == before


@pytest.mark.parametrize('change', ['same_size', 'extra_file', 'missing_file', 'extra_directory'])
def test_existing_different_tree_is_rejected_without_overwrite(tmp_path, files, change):
    target = Path(write_reviewed_folder(str(tmp_path), 'choose-gift', files))
    if change == 'same_size':
        (target / 'SKILL.md').write_bytes(b'x' * len(files['SKILL.md']))
    elif change == 'extra_file':
        (target / 'user-note.txt').write_bytes(b'preserve')
    elif change == 'missing_file':
        (target / 'references/methods.md').unlink()
    else:
        (target / 'user-directory').mkdir()
    before = {str(path.relative_to(target)): path.read_bytes()
        for path in target.rglob('*') if path.is_file()}
    with pytest.raises(ValueError, match='^skill_folder_exists$'):
        write_reviewed_folder(str(tmp_path), 'choose-gift', files)
    assert {str(path.relative_to(target)): path.read_bytes()
        for path in target.rglob('*') if path.is_file()} == before
    if change == 'extra_directory':
        assert (target / 'user-directory').is_dir()


@pytest.mark.parametrize('name', ['../escape', 'other/name', 'other\\name', '', 'CON', 'con',
    'bad--name', 'trailing.', 'Bad-Name', 'name:stream'])
def test_invalid_product_names_do_not_create_any_files(tmp_path, files, name):
    with pytest.raises(ValueError, match='^invalid_skill_folder$'):
        write_reviewed_folder(str(tmp_path), name, files)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'references/../outside',
    'references\\outside', 'references//duplicate', 'references/./duplicate',
    'references/CON', 'references/name:stream', 'references/trailing.',
    'references/bad?name', '.hidden/file', 'outside/file'])
def test_unsafe_package_paths_do_not_write(tmp_path, files, path):
    with pytest.raises(ValueError, match='^invalid_skill_folder$'):
        write_reviewed_folder(str(tmp_path), 'choose-gift', {**files, path: b'body'})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('bad_files', [None, {}, {'references/a.md': b'body'},
    {'SKILL.md': 'not bytes'}, {'SKILL.md': b'body', 'references/A.md': b'1', 'references/a.md': b'2'},
    {'SKILL.md': b'body', 'references/a.md': b'1', 'references/a.md/b': b'2'}])
def test_invalid_mapping_is_rejected_before_io(tmp_path, bad_files):
    with pytest.raises(ValueError, match='^invalid_skill_folder$'):
        write_reviewed_folder(str(tmp_path), 'choose-gift', bad_files)
    assert list(tmp_path.iterdir()) == []


def test_root_must_exist_and_be_absolute_and_a_directory(tmp_path, files):
    missing = tmp_path / 'not-created'
    regular = tmp_path / 'ordinary-file'
    regular.write_bytes(b'user contents')
    for value in ('relative-folder', str(missing), str(regular)):
        with pytest.raises(ValueError, match='^invalid_skill_folder$'):
            write_reviewed_folder(value, 'choose-gift', files)
    assert not missing.exists()
    assert regular.read_bytes() == b'user contents'


def test_existing_regular_file_is_not_replaced(tmp_path, files):
    target = tmp_path / 'choose-gift'
    target.write_bytes(b'user file')
    with pytest.raises(ValueError, match='^skill_folder_exists$'):
        write_reviewed_folder(str(tmp_path), 'choose-gift', files)
    assert target.read_bytes() == b'user file'


def _junction(link, target):
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(target)],
        capture_output=True, check=False)
    assert result.returncode == 0, 'ENV: temporary directory junction creation unavailable'


@pytest.mark.parametrize('where', ['root', 'target', 'resource'])
def test_real_junction_cannot_redirect_writes_or_idempotent_reads(tmp_path, files, where):
    root, outside = tmp_path / 'selected', tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    (outside / 'marker').write_bytes(b'preserve')
    if where == 'root':
        link = tmp_path / 'selected-link'
        _junction(link, root)
        directory = link
    elif where == 'target':
        _junction(root / 'choose-gift', outside)
        directory = root
    else:
        target = root / 'choose-gift'
        target.mkdir()
        (target / 'SKILL.md').write_bytes(files['SKILL.md'])
        (outside / 'methods.md').write_bytes(files['references/methods.md'])
        _junction(target / 'references', outside)
        directory = root
    before = {path.name: path.read_bytes() for path in outside.iterdir()}
    with pytest.raises(ValueError) as failure:
        write_reviewed_folder(str(directory), 'choose-gift', files)
    assert str(failure.value) in {'invalid_skill_folder', 'skill_folder_exists', 'skill_folder_write_failed'}
    assert str(directory) not in str(failure.value)
    assert {path.name: path.read_bytes() for path in outside.iterdir()} == before
    if where == 'root':
        assert list(root.iterdir()) == []


@pytest.mark.parametrize('existing', [False, True])
def test_ancestor_junction_with_ordinary_selected_child_is_rejected(tmp_path, files, existing):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'marker').write_bytes(b'external marker remains unchanged')
    actual_root = outside / 'ordinary-child'
    actual_root.mkdir()
    target = actual_root / 'choose-gift'
    if existing:
        for name, body in files.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
    alias = tmp_path / 'selected-ancestor'
    _junction(alias, outside)
    selected = alias / 'ordinary-child'
    before = {str(path.relative_to(outside)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in outside.rglob('*') if path.is_file()}
    with pytest.raises(ValueError) as failure:
        write_reviewed_folder(str(selected), 'choose-gift', files)
    assert str(failure.value) in {'invalid_skill_folder', 'skill_folder_exists', 'skill_folder_write_failed'}
    assert str(selected) not in str(failure.value)
    assert {str(path.relative_to(outside)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in outside.rglob('*') if path.is_file()} == before
    assert target.exists() is existing
    assert set(actual_root.iterdir()) == ({target} if existing else set())
