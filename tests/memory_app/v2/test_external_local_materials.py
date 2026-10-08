"""原运行器目录与附件接线；仅隔离尚未部署的 MCP 传输资格。"""
import io
import json
from copy import deepcopy
from pathlib import Path
import sys
import zipfile

import pytest

from backend.memory_app.v2.external_host import ExecutorRegistration
from backend.memory_app.v2.external_runner import ARCHIVE, ExternalRunnerError
from tests.memory_app.v2.test_external_app_composition import host_env, install
from tests.memory_app.v2.test_external_context import prepare, TURN, DAY
from tests.memory_app.v2.test_external_host import native_cli


@pytest.fixture
def local(host_env, monkeypatch):
    from backend.memory_app.v2 import external_memory_admission as memory
    env = host_env
    app, runtime, owner = install(env)
    probes = []
    def transport(*args, **kwargs):
        probes.append(True)
        return {'test': 'transport-only'}
    monkeypatch.setattr(memory, 'require_memory_service', transport)
    monkeypatch.setattr(memory, 'check_memory_configuration', lambda *args, **kwargs: None)
    monkeypatch.setattr(memory, 'revalidate_memory_service', lambda *args, **kwargs: None)
    auth = env.root / 'empty-auth'; auth.mkdir()
    executable = native_cli(env.root / 'synthetic-codex.exe', 'codex')
    owner.host.registrations['codex'] = ExecutorRegistration('codex', executable, auth)
    context, _, turn_runner, _ = prepare(env)
    context.execute(TURN, runtime=runtime, runner=turn_runner)
    identity = 'turn-' + '8' * 32
    config = {'mcpServers': {'chriptmas-memory': {'command': sys.executable,
        'args': ['-I', '-m', 'backend.memory_app.mcp']}}}
    kw = dict(delivery_turn_id=TURN, executor='codex', cli_version='0.156.1',
        task='核查合成资料', mcp_config=config, session_id='session-local',
        operation_id='operation-local', idempotency_key=identity, created_at=DAY.isoformat())
    return env, owner, identity, kw, probes


def test_default_workspace_freezes_attachment_and_replay_without_overwrite(local):
    env, owner, identity, kw, probes = local
    supplied = {'合成证据.bin': b'\x00\x01\xffsynthetic'}
    request = owner.prepare(identity, **kw, attachments=supplied)
    path = env.root / 'agent_workspaces' / identity
    assert (path / '合成证据.bin').read_bytes() == supplied['合成证据.bin']
    saved = owner.turns.get_immutable_payload(identity, ARCHIVE)
    supplied['合成证据.bin'] = b'caller-changed'
    with pytest.raises(ExternalRunnerError): owner.prepare(identity, **kw, attachments=supplied)
    assert owner.prepare(identity, **kw, attachments={'合成证据.bin': b'\x00\x01\xffsynthetic'}) == request
    assert probes == [True] and owner.turns.get_immutable_payload(identity, ARCHIVE) == saved
    assert env.records.list('v2_external_runs') == () and env.model.calls == 0


def test_folder_preserves_actual_cwd_and_user_files_in_control_names(local):
    env, owner, identity, kw, probes = local
    folder = env.root / '用户选定目录'; folder.mkdir()
    (folder / 'TASK.md').write_bytes(b'user-task')
    (folder / 'CONTEXT.md').write_bytes(b'user-context')
    permission = owner.host.permissions.confirm_folder(folder, confirmed=True, expected_revision=0)
    refs = {'folder': permission, 'commands': None}
    request = owner.prepare(identity, **kw, preset='folder', folder=folder,
        attachments={'资料.txt': '合成附件'.encode()}, host_permission_refs=refs)
    materials = folder / 'agent_workspaces' / identity
    assert (materials / '资料.txt').read_bytes() == '合成附件'.encode()
    assert not (env.root / 'agent_workspaces' / identity).exists()
    assert (folder / 'TASK.md').read_bytes() == b'user-task'
    assert (folder / 'CONTEXT.md').read_bytes() == b'user-context'
    saved = owner.turns.get_immutable_payload(identity, ARCHIVE)
    assert saved[1]['launch']['cwd'] == str(folder)
    assert owner.prepare(identity, **kw, preset='folder', folder=folder,
        attachments={'资料.txt': '合成附件'.encode()}, host_permission_refs=refs) == request
    assert probes == [True]
    assert owner.turns.get_immutable_payload(identity, ARCHIVE) == saved


@pytest.mark.parametrize('name', ['../escape', 'sub/file.txt', 'sub\\file.txt', 'TASK.md', 'task.MD'])
def test_invalid_attachment_before_memory_or_turn_acceptance(local, name):
    env, owner, identity, kw, probes = local
    with pytest.raises(ExternalRunnerError): owner.prepare(identity, **kw, attachments={name:b'bad'})
    assert probes == [] and owner.turns.get_request(identity) is None
    assert env.records.list('v2_external_task_preparations') == ()
    assert not (env.root / 'agent_workspaces' / identity).exists()


def test_case_collision_and_size_limit_before_external_probe(local):
    env, owner, identity, kw, probes = local
    for attachments in ({'资料.txt': b'a', '资料.TXT': b'b'}, {'large.bin': b'x' * (16 * 1024 * 1024)}):
        with pytest.raises(ExternalRunnerError): owner.prepare(identity, **kw, attachments=attachments)
    assert probes == [] and owner.turns.get_request(identity) is None
    assert env.records.list('v2_external_task_preparations') == ()


@pytest.mark.parametrize('change', ['bytes', 'identity', 'directory', 'missing'])
def test_prepared_replay_revalidates_attachment_and_directory_identity(local, change):
    env, owner, identity, kw, probes = local
    attached = {'资料.txt': b'synthetic-original'}
    owner.prepare(identity, **kw, attachments=attached)
    directory = env.root / 'agent_workspaces' / identity
    file = directory / '资料.txt'
    saved = owner.turns.get_immutable_payload(identity, ARCHIVE)
    if change == 'bytes': file.write_bytes(b'synthetic-modified')
    elif change == 'identity': file.rename(directory / 'old-attachment'); file.write_bytes(attached['资料.txt'])
    elif change == 'missing': file.rename(directory / 'old-attachment')
    else:
        directory.rename(directory.with_name('old-task'))
        directory.mkdir()
        for source in directory.with_name('old-task').iterdir():
            (directory / source.name).write_bytes(source.read_bytes())
    with pytest.raises(ExternalRunnerError): owner.prepare(identity, **kw, attachments=attached)
    assert probes == [True] and owner.turns.get_immutable_payload(identity, ARCHIVE) == saved
    assert env.records.list('v2_external_runs') == ()


def test_folder_without_confirmation_rejects_before_memory_and_existing_file_write(local):
    env, owner, identity, kw, probes = local
    folder = env.root / 'selected'; folder.mkdir()
    marker = folder / 'TASK.md'; marker.write_bytes(b'user-file')
    with pytest.raises(ExternalRunnerError): owner.prepare(identity, **kw, preset='folder', folder=folder)
    assert marker.read_bytes() == b'user-file' and list(folder.iterdir()) == [marker]
    assert probes == [] and owner.turns.get_request(identity) is None


def test_existing_unbound_task_tree_is_never_adopted_or_overwritten(local):
    env, owner, identity, kw, probes = local
    path = env.root / 'agent_workspaces' / identity; path.mkdir(parents=True)
    marker = path / 'user-file'; marker.write_bytes(b'keep-user-data')
    with pytest.raises(ExternalRunnerError): owner.prepare(identity, **kw, attachments={'资料.txt': b'synthetic'})
    assert marker.read_bytes() == b'keep-user-data' and list(path.iterdir()) == [marker]
    assert owner.turns.get_immutable_payload(identity, ARCHIVE) is None
    assert env.records.list('v2_external_runs') == ()


def test_attachment_secret_is_rejected_before_write_probe_or_archive(local):
    env, owner, identity, kw, probes = local
    secret = ('sk-' + 'Q' * 26).encode()
    with pytest.raises(ExternalRunnerError): owner.prepare(identity, **kw, attachments={'资料.txt': secret})
    assert probes == [] and owner.turns.get_request(identity) is None
    assert not (env.root / 'agent_workspaces' / identity).exists()


def test_replay_cannot_omit_original_attachments(local):
    _, owner, identity, kw, probes = local
    owner.prepare(identity, **kw, attachments={'资料.txt': b'synthetic'})
    with pytest.raises(ExternalRunnerError): owner.prepare(identity, **kw)
    assert probes == [True]


def test_directory_replaced_during_sdk_probe_is_not_written_or_accepted(local, monkeypatch):
    from backend.memory_app.v2 import external_memory_admission as memory
    env, owner, identity, kw, probes = local
    folder = env.root / 'selected'; folder.mkdir()
    ref = owner.host.permissions.confirm_folder(folder, confirmed=True, expected_revision=0)
    original = memory.require_memory_service
    def changed(*args, **kwargs):
        folder.rename(env.root / 'old-selected'); folder.mkdir()
        marker = folder / 'user-file'; marker.write_bytes(b'user-file')
        return original(*args, **kwargs)
    monkeypatch.setattr(memory, 'require_memory_service', changed)
    with pytest.raises(ExternalRunnerError):
        owner.prepare(identity, **kw, preset='folder', folder=folder,
            host_permission_refs={'folder':ref,'commands':None})
    assert list(folder.iterdir()) == [folder / 'user-file']
    assert owner.turns.get_request(identity) is None
    assert owner.turns.get_immutable_payload(identity, ARCHIVE) is None
    assert env.records.list('v2_external_runs') == ()
    assert not list(env.root.rglob('version-called'))


def test_known_host_secret_attachment_is_rejected_before_any_disk_or_probe(local):
    env, owner, identity, kw, probes = local
    secret = '-'.join(('synthetic','owned','runtime','value'))
    owner.host._secret_environment = {'APPROVED_DEVICE_KEY':secret}
    with pytest.raises(ExternalRunnerError): owner.prepare(identity, **kw, attachments={'资料.txt': secret.encode()})
    assert probes == [] and owner.turns.get_request(identity) is None
    assert not (env.root / 'agent_workspaces' / identity).exists()
    assert not list(env.root.rglob('version-called'))


def synthetic_task_cli(executable):
    from pip._vendor.distlib.resources import finder
    source = '''import json,sys,re
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
if sys.argv[1:]==['--version']:
 print('codex-cli 0.156.1'); sys.exit(0)
text=sys.stdin.buffer.read().decode('utf-8')
relative=re.search(r'agent_workspaces/[A-Za-z0-9._-]+',text).group(0)
materials=Path(relative)
assert (materials/'资料.txt').read_bytes()==b'synthetic-attachment'
assert json.loads((materials/'CONTEXT.md').read_text(encoding='utf-8'))['entries'][0]['id']=='M1'
assert (materials/'TASK.md').read_text(encoding='utf-8')=='核查合成资料'
assert Path('user-file').read_bytes()==b'user-file'
Path('spawned').write_text(text,encoding='utf-8')
print(json.dumps({'type':'turn.started'}),flush=True)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'合成完成'}}),flush=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':1}}),flush=True)
'''
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive: archive.writestr('__main__.py', source)
    executable.write_bytes(finder('pip._vendor.distlib').find('t64.exe').bytes
        + ('#!"' + sys._base_executable + '" -I -S\n').encode() + buffer.getvalue())


@pytest.mark.parametrize('change', ['none','attachment','directory','permission'])
def test_real_launch_seam_locates_folder_materials_and_rejects_late_change(local, change):
    from backend.memory_app.v2.external_execution import execute_external
    from core.ai_kernel.dispatcher import ToolProviderFailure
    from tests.memory_app.v2.test_external_dispatch import context
    env, owner, identity, kw, _ = local
    folder = env.root / 'selected'; folder.mkdir()
    (folder / 'user-file').write_bytes(b'user-file')
    synthetic_task_cli(owner.host.registrations['codex'].executable)
    ref = owner.host.permissions.confirm_folder(folder, confirmed=True, expected_revision=0)
    owner.prepare(identity, **kw, preset='folder', folder=folder, attachments={'资料.txt':b'synthetic-attachment'},
        host_permission_refs={'folder':ref, 'commands':None})
    archive = owner.turns.get_immutable_payload(identity, ARCHIVE)[1]
    plan = owner._archive_plan(archive)
    assert plan.cwd == folder and plan.input_text == kw['task']
    assert plan.input_policy == archive['launch']['input_policy'] == 'external_task_input@1'
    assert plan.stdin_text == archive['launch']['stdin_text']
    lease = owner.host.prepare(archive['request'], plan, mcp_config=kw['mcp_config'],
        host_permission_refs=archive['host_permission_refs'])
    def before_launch():
        if change == 'attachment': (plan.material_directory / '资料.txt').write_bytes(b'late-changed')
        elif change == 'directory':
            plan.material_directory.rename(plan.material_directory.with_name('old-materials'))
            plan.material_directory.mkdir()
        elif change == 'permission':
            row = env.records.read('v2_external_host_permissions', ref['id'])
            with env.records.begin() as tx:
                tx.put('v2_external_host_permissions', ref['id'], row.payload, expected_revision=1); tx.commit()
        owner._materials(lease, archive['delivery'], archive['mcp_config'], materials=archive['launch']['materials'])
    try:
        if change == 'none':
            dto = execute_external(lease, context(), records=env.records, turns=owner.turns,
                turn_id=identity, owner_id=owner.owner_id, before_launch=before_launch)
            assert set(dto) == {'summary','payload_ref','receipt_ref','evidence_refs'}
            assert (folder / 'spawned').read_text(encoding='utf-8') == plan.stdin_text
            assert env.records.read('v2_external_runs', identity).payload['status'] == 'completed'
        else:
            with pytest.raises(ToolProviderFailure) as caught:
                execute_external(lease, context(), records=env.records, turns=owner.turns,
                    turn_id=identity, owner_id=owner.owner_id, before_launch=before_launch)
            assert caught.value.effect_certainty == 'confirmed_none'
            assert not (folder / 'spawned').exists()
            assert env.records.read('v2_external_runs', identity).payload['status'] == 'failed'
        assert (folder / 'user-file').read_bytes() == b'user-file'
    finally: lease.close()


def historical_workspace(local):
    from core.ai_kernel.turn_kinds import freeze_turn_request
    from backend.memory_app.v2.external_adapters import build_launch_plan
    from backend.memory_app.v2.external_workspace import create_task_workspace
    env, owner, identity, kw, _ = local
    delivery = owner.context.qualified_delivery(TURN)
    frozen = freeze_turn_request('project.task', template_version=2, turn_id=identity,
        session_id=kw['session_id'], operation_id=kw['operation_id'], idempotency_key=kw['idempotency_key'],
        project_id=delivery['project_id'], created_at=kw['created_at'], text=kw['task'],
        privacy=delivery['privacy'], refs=delivery['refs'],
        capability_request={'mode':'execute_exact_v1','capability_id':'external.task.execute',
            'arguments':{'binding_ref':'crp://session/' + identity + '/' + ARCHIVE}})
    frozen['policy_versions'] = delivery['policy_versions']
    assert owner.runtime.accept_turn(frozen).status == 'accepted'
    cwd = create_task_workspace(env.root, identity, task=kw['task'], handoff=delivery['handoff'], mcp_config=kw['mcp_config'])
    plan = build_launch_plan('codex', cli_version='0.156.1', executable=owner.host.registrations['codex'].executable,
        cwd=cwd, task=kw['task'], mcp_config=kw['mcp_config'])
    # 原五字段 launch 是上一版真实 writer 的持久格式，不把升级后的字段补写回去。
    archive = {'schema_version':'1.0.0','owner_id':owner.owner_id,'turn_id':identity,
        'request':frozen,'delivery':delivery,'mcp_config':kw['mcp_config'],'host_permission_refs':None,
        'memory_proof':owner._require_memory(kw['mcp_config'], 'codex'),
        'launch':{'executor':'codex','cli_version':'0.156.1','preset':'workspace','commands':'disabled','cwd':str(cwd)}}
    owner.turns.get_or_create_immutable_payload(identity, ARCHIVE, archive)
    return frozen, archive, plan


def test_original_five_field_archive_replays_without_rewriting_it(local):
    env, owner, identity, kw, probes = local
    frozen, archive, original_plan = historical_workspace(local)
    saved = owner.turns.get_immutable_payload(identity, ARCHIVE)
    events = owner.turns.events_after(identity)
    assert owner.prepare(identity, **kw) == frozen
    assert owner._archive_plan(archive) == original_plan
    assert owner.turns.get_immutable_payload(identity, ARCHIVE) == saved
    assert owner.turns.events_after(identity) == events and probes == [True]
    assert env.records.list('v2_external_runs') == ()


@pytest.mark.parametrize('change', ['partial','folder','context','unfrozen_attachment'])
def test_legacy_archive_does_not_relax_new_shape_or_original_materials(local, change):
    _, owner, identity, kw, probes = local
    _, archive, plan = historical_workspace(local)
    saved = owner.turns.get_immutable_payload(identity, ARCHIVE)
    if change in {'partial','folder'}:
        supplied = deepcopy(archive)
        if change == 'partial': supplied['launch']['stdin_text'] = kw['task']
        else: supplied['launch']['preset'] = 'folder'
        with pytest.raises(ExternalRunnerError): owner._archive_plan(supplied)
    elif change == 'context':
        (plan.cwd / 'CONTEXT.md').write_bytes(b'changed-original-context')
        with pytest.raises(ExternalRunnerError): owner.prepare(identity, **kw)
    else:
        (plan.cwd / 'unfrozen.txt').write_bytes(b'unfrozen')
        with pytest.raises(ExternalRunnerError): owner.prepare(identity, **kw, attachments={'unfrozen.txt':b'unfrozen'})
    assert owner.turns.get_immutable_payload(identity, ARCHIVE) == saved
    assert probes == [True]


def test_new_full_launch_cannot_use_nullable_material_snapshot(local):
    _, owner, identity, kw, _ = local
    owner.prepare(identity, **kw)
    archive = deepcopy(owner.turns.get_immutable_payload(identity, ARCHIVE)[1])
    archive['launch']['materials'] = None
    with pytest.raises(ExternalRunnerError): owner._archive_materials(archive)
