from pathlib import Path
import shutil
import os
from uuid import uuid4

import pytest

from core.storage_provider import JsonObjectStore, ObjectStorePathError
from core.storage_provider.native_io import native_io_path


@pytest.mark.parametrize('short_layout', [False, True])
def test_original_json_layout_reads_after_long_archive_copy(short_layout):
    allocated = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert not any(path.name == 'runtime' for path in (allocated, *allocated.parents))
    tmp_path = allocated / ('json-' + uuid4().hex[:8])
    tmp_path.mkdir()
    collection = 'source_asset_links'
    identity = 'object-' + 'x' * (121 if short_layout else 80)
    source = tmp_path / 'source'
    if short_layout:
        for length in range(150):
            source = tmp_path / ('s' * length) / 'source'
            if len(str(source / 'objects/default' / collection / (identity + '.meta.json'))) > 259:
                break
    store = JsonObjectStore(source)
    value = {'id': identity, 'content': '原归档的合成附件关联'}
    assert store.write(collection, identity, value, expected_revision=0) == 1
    original_paths = store._object_paths(collection, identity)
    assert original_paths.uses_short_name is short_layout
    target = tmp_path / ('archive-' + 'a' * 90) / ('payload-' + 'b' * 60)
    shutil.copytree(source, native_io_path(target))
    archived = JsonObjectStore(target)
    assert len(str(target / 'objects/default' / collection / original_paths.payload_path.name)) > 295
    assert archived.read(collection, identity) == value
    assert archived.read_including_deleted(collection, identity) == value
    assert archived.list(collection) == (value,)
    assert archived.list_including_deleted(collection) == (value,)
    assert archived.revision(collection, identity) == 1
    assert archived.incarnation(collection, identity) == 'legacy'
    assert archived.attribution(collection, identity, 1) is None
    assert archived.collection_names() == (collection,)
    assert archived.read(collection, 'missing') is None
    assert archived.list('missing') == ()
    assert not str(archived.root).startswith('\\\\?\\')
    assert archived._object_paths(collection, identity).uses_short_name is short_layout
    if short_layout:
        legacy = JsonObjectStore(tmp_path / 'legacy')
        legacy.write(collection, identity, value, expected_revision=0)
        paths = legacy._object_paths(collection, identity)
        assert not paths.uses_short_name
        directory = target / 'objects/default' / collection
        for path in (paths.payload_path, paths.meta_path):
            shutil.copyfile(path, native_io_path(directory / path.name))
        with pytest.raises(ObjectStorePathError, match='ambiguous'):
            archived.read(collection, identity)


def test_native_path_consumers_import_without_cycle():
    from core.storage_provider import vault_backup_restore
    from core.storage_provider import runtime
    assert vault_backup_restore._native_io_path is native_io_path
    assert runtime.native_io_path is native_io_path
    assert Path(native_io_path(Path.cwd())).is_absolute()


@pytest.mark.parametrize('collection', ['sources', 'memory_persona', 'project_skills'])
def test_locked_json_collections_read_retained_long_paths(collection):
    allocated = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert not any(path.name == 'runtime' for path in (allocated, *allocated.parents))
    root = allocated / ('lock-' + uuid4().hex[:8])
    store = JsonObjectStore(root / 'source')
    identity = 'object-' + 'x' * 80
    value = {'id': identity, 'content': '真实长路径读取的合成资料'}
    assert store.write(collection, identity, value, expected_revision=0) == 1
    target = root / ('archive-' + 'a' * 90) / ('payload-' + 'b' * 60)
    shutil.copytree(store.root, native_io_path(target))
    archived = JsonObjectStore(target)
    assert len(str(archived._object_paths(collection, identity).meta_path)) > 295
    assert archived.read(collection, identity) == value
    assert archived.revision(collection, identity) == 1
    assert archived.list(collection) == (value,)
