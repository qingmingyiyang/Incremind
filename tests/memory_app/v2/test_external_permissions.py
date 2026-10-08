"""目录许可用真实临时目录与 CAS 记录；不替代用户确认界面。"""
import importlib
from pathlib import Path

import pytest

from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict
from tests.memory_app.v2.test_external_host import setup_host


def permissions(host):
    module = importlib.import_module('backend.memory_app.v2.external_permissions')
    return module.HostPermissions(host.records, owner_id=host.owner_id, deployment=host.deployment)


def test_first_folder_requires_confirmation_and_same_value_keeps_revision(setup_host):
    _, host, _, _, _, records, root, _ = setup_host()
    path = root / '用户选定目录'; path.mkdir()
    owner = permissions(host)
    assert owner.folder_reference(path) is None
    with pytest.raises(ValueError, match='^external_host_confirmation_required$'):
        owner.confirm_folder(path, confirmed=False, expected_revision=0)
    assert records.list('v2_external_host_permissions') == ()
    ref = owner.confirm_folder(path, confirmed=True, expected_revision=0)
    before = records.list('v2_external_host_permissions')
    assert set(ref) == {'id', 'revision'} and ref['revision'] == 1
    assert owner.folder_reference(path) == ref
    assert owner.confirm_folder(path, confirmed=True, expected_revision=1) == ref
    assert records.list('v2_external_host_permissions') == before
    with pytest.raises(SQLiteUnitOfWorkConflict):
        owner.confirm_folder(path, confirmed=True, expected_revision=0)
    assert records.list('v2_external_host_permissions') == before


def test_two_store_confirmation_has_one_cas_winner(setup_host):
    from concurrent.futures import ThreadPoolExecutor
    _, host, _, _, _, records, root, _ = setup_host()
    path = root / 'parallel-folder'; path.mkdir()
    first = permissions(host)
    module = importlib.import_module('backend.memory_app.v2.external_permissions')
    second = module.HostPermissions(SQLiteStructuredRecordStore(records.database_path),
        owner_id=host.owner_id, deployment=host.deployment)
    def save(owner):
        try:
            return owner.confirm_folder(path, confirmed=True, expected_revision=0)
        except SQLiteUnitOfWorkConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(save, (first, second)))
    assert values.count('conflict') == 1
    assert len(records.list('v2_external_host_permissions')) == 1


@pytest.mark.parametrize('change', ['owner', 'path', 'replacement'])
def test_saved_qualification_cannot_follow_other_owner_or_replaced_directory(setup_host, change):
    module, host, turn, _, config, records, root, _ = setup_host()
    path = root / 'selected'; path.mkdir()
    other = root / 'other'; other.mkdir()
    owner = permissions(host)
    ref = owner.confirm_folder(path, confirmed=True, expected_revision=0)
    if change == 'replacement':
        path.rename(root / 'old-selected'); path.mkdir()
    else:
        row = records.read(module.PERMISSIONS, ref['id'])
        with records.begin() as tx:
            body = dict(row.payload)
            body['owner_id' if change == 'owner' else 'path'] = 'other-user' if change == 'owner' else str(other)
            tx.put(module.PERMISSIONS, ref['id'], body, expected_revision=1); tx.commit()
    if change == 'replacement':
        with pytest.raises(ValueError): owner.folder_reference(path)
    else:
        assert owner.folder_reference(path) is None
    from backend.memory_app.v2.external_adapters import build_launch_plan
    plan = build_launch_plan('codex', cli_version='0.156.1',
        executable=host.registrations['codex'].executable, cwd=path, task=turn['input']['text'],
        mcp_config=config, preset='folder')
    with pytest.raises(module.ExternalHostError, match='^external_host_permission_unavailable$'):
        host.prepare(turn, plan, mcp_config=config, host_permission_refs={'folder':ref, 'commands':None})
    assert not list(root.rglob('version-called'))


@pytest.mark.parametrize('change', ['server', 'relative', 'traversal', 'missing', 'file', 'boolean_revision'])
def test_invalid_confirmation_never_writes(setup_host, change):
    _, host, _, _, _, records, root, _ = setup_host(mode='server' if change == 'server' else 'desktop')
    path = root / 'selected'; path.mkdir()
    if change == 'relative': path = Path('relative')
    elif change == 'traversal': path = root / '..' / root.name / 'selected'
    elif change == 'missing': path = root / 'missing'
    elif change == 'file': path = root / 'file'; path.write_bytes(b'file')
    with pytest.raises(ValueError):
        permissions(host).confirm_folder(path, confirmed=True, expected_revision=True if change == 'boolean_revision' else 0)
    assert records.list('v2_external_host_permissions') == ()


def test_live_lease_revalidates_permission_after_directory_replacement(setup_host):
    from backend.memory_app.v2.external_adapters import build_launch_plan
    module, host, turn, _, config, _, root, _ = setup_host()
    path = root / 'selected'; path.mkdir()
    ref = permissions(host).confirm_folder(path, confirmed=True, expected_revision=0)
    plan = build_launch_plan('codex', cli_version='0.156.1', executable=host.registrations['codex'].executable,
        cwd=path, task=turn['input']['text'], mcp_config=config, preset='folder')
    lease = host.prepare(turn, plan, mcp_config=config, host_permission_refs={'folder':ref, 'commands':None})
    try:
        path.rename(root / 'old-selected'); path.mkdir()
        with pytest.raises(module.ExternalHostError): lease.validate()
    finally:
        lease.close()
