import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from uuid import uuid4

import httpx


def test_real_shared_insight_maintenance_http():
    repo = Path(__file__).resolve().parents[3]
    base = Path(os.environ.get('CHRIPTMAS_MAINTENANCE_CHECK_ROOT', repo / 'work')) / ('im-' + uuid4().hex[:7])
    (base / 'config').mkdir(parents=True)
    base = base.resolve(strict=True)
    assert 'work' in base.parts and 'runtime' not in base.parts
    shutil.copyfile(repo / 'config/settings.toml.example', base / 'config/settings.toml')
    prior = {key: os.environ.get(key) for key in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(base), 'server'
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == base
    try:
        _actual_case(base)
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _actual_case(base):
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.recognition import RecognitionService, WorkScope
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context

    devices = DeviceRegistry(base / 'server')
    users = ServerUsers(base, records=devices.records)
    pair = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='维护管理员')
    admin, tokens = devices.authenticate(pair['key']), {'local-user': pair['key']}
    actors = []
    for name in ('维护所有者', '维护成员', '维护只看成员'):
        account = users.create(admin, name=name)
        paired = devices.exchange(devices.issue_pairing(user_id=account['user_id'], actor=admin.device_id)['code'], name=name)
        actor = devices.authenticate(paired['key'])
        actors.append(actor)
        tokens[actor.user_id] = paired['key']
    owner, member, viewer = actors

    def child(path, user_id, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, base), server_context=context)

    server = create_server_application(DeploymentLayout('server', base / 'users/local-user', base), child_factory=child, port=8015)
    admin_config = users.root_for(admin.user_id) / 'config'
    admin_config.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', admin_config / 'settings.toml')
    registry = server.state.shared_projects
    project = registry.create(owner, project_name='共享维护核验', operation_id='create-' + uuid4().hex)['project']
    for actor, role in ((member, 'member'), (viewer, 'viewer')):
        registry.invite(owner, project['id'], user_id=actor.user_id, role=role, authority=project['authority'], operation_id='invite-' + uuid4().hex)
        invitation = registry.invitations(actor)[0]
        registry.accept(actor, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + uuid4().hex)
    data = registry.roots._data(project['id'])
    service, scope = RecognitionService(data.records), WorkScope('local-user', project['id'])
    experience = service.stage_experience(scope=scope, content='人工资料要求保留认识正文和历史。')
    proposed = service.propose(scope=scope, content='保留正文和历史', source_experience_ids=[experience])
    recognition = service.publish(scope=scope, candidate_id=proposed.id, expected_revision=proposed.revision, reviewer=owner.user_id)
    pending = service.propose(scope=scope, content='待核验的人工认识', source_experience_ids=[experience])
    with data.records.begin() as tx:
        tx.put('v2_candidate_fade', pending.id, {'faded_at': datetime.now(timezone.utc).isoformat()}, expected_revision=0)
        tx.commit()

    async def scenario():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://test') as client:
                path = '/api/v2/server/shared-projects/' + project['id'] + '/library'
                headers = lambda actor: {'Authorization': 'Bearer ' + tokens[actor.user_id]}
                async def listing(actor=member):
                    result = await client.get(path + '/insights', headers=headers(actor))
                    assert result.status_code == 200, result.text
                    return result.json()
                first = await listing()
                print('shared_library_warm=1', flush=True)
                async with server.state.server_user_pool.lease(member.user_id) as personal:
                    actor_records = personal.state.recognition_records
                    personal_before = {kind: actor_records.list(kind) for kind in
                        ('recognitions', 'recognition_candidates', 'recognition_versions', 'recognition_recall_preferences')}
                users.update(admin, member.user_id, expected_revision=users.get(member.user_id)['revision'], storage_limit_mb=0)
                first = await listing()
                namespace, authority = first['namespace'], first['authority']
                row = next(item for item in first['items'] if item['id'] == recognition.id)
                assert row['recall_preference_revision'] == 0
                target = path + '/insights/' + recognition.id
                edit = {'namespace': namespace, 'authority': authority, 'expected_revision': row['revision'],
                    'text': '维护后的正文仍保留历史', 'conditions': ['适用于共同项目']}
                try:
                    changed = await client.patch(target, headers=headers(member), json=edit)
                finally:
                    users.update(admin, member.user_id, expected_revision=users.get(member.user_id)['revision'], storage_limit_mb=None)
                assert changed.status_code == 200, changed.text
                edit['authority'] = (await listing())['authority']
                authority = edit['authority']
                print('shared_edit_source_capacity=1', flush=True)
                saved = changed.json()['insight']
                assert saved['text'] == edit['text'] and saved['conditions'] == edit['conditions']
                assert next(item for item in (await listing(viewer))['items'] if item['id'] == recognition.id) == saved
                stale = await client.patch(target, headers=headers(member), json=edit)
                assert stale.status_code == 409, stale.text
                edit['expected_revision'] = saved['revision']
                wrong = await client.patch(target, headers=headers(member), json={**edit, 'namespace': 'personal/other/default'})
                assert wrong.status_code == 409, wrong.text
                recall = {key: edit[key] for key in ('namespace', 'authority', 'expected_revision')}
                recall.update(forgotten=True, preference_revision=0)
                forgotten = await client.post(target + '/forget', headers=headers(member), json=recall)
                assert forgotten.status_code == 200, forgotten.text
                assert forgotten.json()['insight']['state'] == 'forgotten'
                from backend.memory_app.recall_state import is_recall_excluded
                assert is_recall_excluded(data.records, scope, recognition.id)
                assert data.records.read('recognitions', recognition.id).revision == saved['revision']
                restore = {**recall, 'forgotten': False}
                conflict = await client.post(target + '/forget', headers=headers(member), json=restore)
                assert conflict.status_code == 409, conflict.text
                restore['preference_revision'] = forgotten.json()['insight']['recall_preference_revision']
                restored = await client.post(target + '/forget', headers=headers(member), json=restore)
                assert restored.status_code == 200, restored.text
                assert restored.json()['insight']['state'] == 'active'
                assert not is_recall_excluded(data.records, scope, recognition.id)
                observed = next(item for item in (await listing())['items'] if item['id'] == pending.id)
                assert observed['state'] == 'forgotten' and observed['recall_preference_revision'] == 1
                pending_body = {**restore, 'expected_revision': observed['revision'], 'preference_revision': 1}
                recovered = await client.post(path + '/insights/' + pending.id + '/forget', headers=headers(member), json=pending_body)
                assert recovered.status_code == 200, recovered.text
                assert recovered.json()['insight']['state'] == 'pending'
                assert data.records.read('recognitions', pending.id) is None
                pending_edit = await client.patch(path + '/insights/' + pending.id, headers=headers(member),
                    json={**edit, 'expected_revision': recovered.json()['insight']['revision'], 'text': '人工修改待确认认识'})
                assert pending_edit.status_code == 200, pending_edit.text
                assert pending_edit.json()['insight']['state'] == 'pending'
                assert data.records.read('recognition_candidates', pending.id).payload['updated_by'] == member.user_id
                print('forget_restore_and_pending_edit=1', flush=True)
                denied = await client.patch(target, headers=headers(viewer), json={**edit, 'authority': (await listing(viewer))['authority']})
                assert denied.status_code == 403, denied.text
                denied_recall = await client.post(target + '/forget', headers=headers(viewer),
                    json={**restore, 'authority': (await listing(viewer))['authority']})
                assert denied_recall.status_code == 403, denied_recall.text
                privileged = {**headers(admin), 'X-Chriptmas-Target-User': member.user_id}
                admin_read = await client.get(path + '/insights', headers=privileged)
                assert admin_read.status_code == 200, admin_read.text
                admin_edit = {**edit, 'text': '管理员维护共同项目正文', 'authority': admin_read.json()['authority']}
                audited = await client.patch(target, headers=privileged, json=admin_edit)
                assert audited.status_code == 200, audited.text
                from backend.memory_app.v2.shared_projects import ATTRIBUTION
                writes = [entry for entry in data.records.list(ATTRIBUTION) if entry.payload.get('collection') == 'recognitions'
                    and entry.payload.get('object_id') == recognition.id and entry.payload.get('object_revision') == audited.json()['insight']['revision']]
                assert len(writes) == 1 and writes[0].payload['by'] == 'admin' and writes[0].payload['actor_user_id'] == admin.user_id
                users.update(admin, owner.user_id, expected_revision=users.get(owner.user_id)['revision'], storage_limit_mb=0)
                try:
                    capacity = await client.patch(target, headers=privileged, json={**admin_edit, 'expected_revision': audited.json()['insight']['revision']})
                finally:
                    users.update(admin, owner.user_id, expected_revision=users.get(owner.user_id)['revision'], storage_limit_mb=None)
                assert capacity.status_code == 429, capacity.text
                obsolete_source = service.stage_experience(scope=scope, content='该人工来源随后停止生效。')
                obsolete = service.propose(scope=scope, content='该主张需要当前来源', source_experience_ids=[obsolete_source])
                service.revoke_experience(scope=scope, experience_id=obsolete_source, expected_revision=1)
                obsolete_row = data.records.read('recognition_candidates', obsolete.id)
                changed_source = await client.patch(path + '/insights/' + obsolete.id, headers=privileged,
                    json={**admin_edit, 'expected_revision': obsolete_row.revision})
                assert changed_source.status_code == 409, changed_source.text
                detail = registry.detail(owner, project['id'])
                membership = next(item for item in detail['members'] if item['user_id'] == member.user_id)
                registry.change_member(owner, project['id'], user_id=member.user_id, action='role', role='viewer',
                    expected_revision=membership['revision'], authority=detail['project']['authority'], operation_id='role-' + uuid4().hex)
                revoked = await client.patch(target, headers=headers(member), json=edit)
                assert revoked.status_code == 403, revoked.text
                versions = [entry.payload for entry in data.records.list('recognition_versions') if entry.payload.get('recognition_id') == recognition.id]
                assert any(version.get('snapshot', {}).get('content') == '保留正文和历史' for version in versions)
                assert data.records.read('recognitions', recognition.id).payload['content'] == admin_edit['text']
                assert all(actor_records.list(kind) == rows for kind, rows in personal_before.items())
                actions = [entry['action'] for entry in registry.activity(viewer, project['id'])]
                assert 'edit' in actions and 'forget' in actions and 'restore' in actions
                for database in base.rglob('ai-turns.sqlite3'):
                    import sqlite3
                    with sqlite3.connect(database) as connection:
                        assert connection.execute('SELECT COUNT(*) FROM ai_turn_events WHERE json_extract(event_json,\'$.type\') LIKE \'model.%\'').fetchone()[0] == 0
                print('edit_get_persisted=1; forget_restore=1; pending_restore_only=1; source_capacity=1; by_admin=1; viewer_revocation=1; model_events=0')
    asyncio.run(scenario())
