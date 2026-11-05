import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from uuid import uuid4

import httpx


def check(root):
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_backup import COMMANDS as BACKUPS
    from backend.memory_app.v2.shared_backup_qualification import FIELD
    from backend.memory_app.v2.shared_merge_privacy import PRIVACY, pending_merge_private
    from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context

    devices = DeviceRegistry(root / 'server')
    users = ServerUsers(root, records=devices.records)
    admin_key = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'],
        name='合并验收管理设备')['key']
    admin = devices.authenticate(admin_key)

    def account(name):
        user = users.create(admin, name=name)
        pairing = devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)
        key = devices.exchange(pairing['code'], name='合并验收设备')['key']
        return devices.authenticate(key), {'Authorization': 'Bearer ' + key}

    member, member_headers = account('合并验收成员')
    viewer, viewer_headers = account('合并验收只看成员')

    def child(path, user_id, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root), server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root), child_factory=child, port=8096)
    registry = server.state.shared_projects
    project = registry.create(admin, project_name='共享认识合并验收', operation_id='merge-project')['project']['id']
    for actor, role in ((member, 'member'), (viewer, 'viewer')):
        current = registry.detail(admin, project)['project']
        registry.invite(admin, project, user_id=actor.user_id, role=role, authority=current['authority'],
            operation_id='merge-invite-' + role)
        invitation, = registry.invitations(actor)
        registry.accept(actor, invitation['id'], expected_revision=invitation['revision'], operation_id='merge-accept-' + role)
    data, scope = registry.roots._data(project), WorkScope('local-user', project)
    service, egress = RecognitionService(data.records), SourceEgressService(data.records)

    def seed(text, *, pending=False, private_input=False, source_recognition_ids=()):
        evidence = service.stage_experience(scope=scope, content=text + '的原始材料')
        if private_input:
            egress.set_policy(scope, 'experience', evidence, 1, 0, [])
        candidate = service.propose(scope=scope, content=text, source_experience_ids=[evidence],
            source_recognition_ids=source_recognition_ids)
        return candidate if pending else service.publish(scope=scope, candidate_id=candidate.id,
            expected_revision=candidate.revision, reviewer=admin.user_id)

    first, second = seed('已生效甲', private_input=True), seed('已生效乙')
    target = seed('已有流程')
    candidate = seed('待确认流程', pending=True, source_recognition_ids=[target.id])
    browser = [seed('浏览器合并甲'), seed('浏览器合并乙')]
    browser_pending, browser_target = seed('浏览器待确认内容', pending=True), seed('浏览器合并已有认识')
    earlier, later = seed('早期合并目标'), seed('后续合并目标')
    ancestor = seed('连续合并原候选', pending=True)
    egress.set_policy(scope, 'recognition', target.id, target.revision, 0, [])
    base = f'/api/v2/server/shared-projects/{project}/library'
    merges = base + '/insight-merges'
    before = {row.object_id: row for row in data.records.list('recognition_versions')}

    def expect(response, status):
        assert response.status_code == status, (response.request.url.path, response.status_code, response.json())
        return response.json()

    async def scenario():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://test') as client:
                listing = expect(await client.get(base + '/insights', headers=member_headers), 200)
                access = {key: listing[key] for key in ('namespace', 'authority')}
                query = {'namespace': access['namespace']}
                active = {**access, 'operation_id': str(uuid4()),
                    'expected_revisions': {first.id: first.revision, second.id: second.revision},
                    'content': '人工合并后的认识', 'conditions': ['先核对原始材料']}
                expect(await client.post(merges + '/active', headers=viewer_headers, json=active), 403)
                expect(await client.post(merges + '/active', headers=member_headers, json={**active,
                    'expected_revisions': {first.id: first.revision + 1, second.id: second.revision}}), 409)
                active_result = expect(await client.post(merges + '/active', headers=member_headers, json=active), 200)
                active_id = active_result['result_id']
                assert expect(await client.post(merges + '/active', headers=member_headers, json=active), 200) == active_result
                assert expect(await client.get(merges + '/' + active['operation_id'], headers=member_headers,
                    params=query), 200) == active_result
                assert all(data.records.read('recognitions', row.id).payload['state'] == 'superseded' for row in (first, second))
                assert data.records.read('recognitions', active_id).payload['published_by'] == member.user_id
                assert egress.policy(scope, 'recognition', active_id)['allowed_purposes'] == []
                assert all(data.records.read('recognition_versions', identity) == row for identity, row in before.items())
                targets = expect(await client.get(merges + '/targets', headers=member_headers, params=query), 200)['items']
                assert not {first.id, second.id} & {row['id'] for row in targets}

                pending = {**access, 'project_id': project, 'operation_id': str(uuid4()), 'candidate_id': candidate.id,
                    'candidate_revision': candidate.revision, 'target_id': target.id, 'target_revision': target.revision,
                    'content': '人工合并待确认内容', 'conditions': []}
                prepared = expect(await client.post(merges + '/prepare', headers=member_headers, json=pending), 200)
                assert prepared['proposal']['state'] == 'pending'
                assert expect(await client.get(merges + '/' + pending['operation_id'], headers=member_headers,
                    params=query), 200) == prepared
                action = {**access, 'decision': 'approve', 'expected_revision': prepared['proposal']['revision']}
                review_path = merges + '/' + pending['operation_id'] + '/review'
                reviewed = expect(await client.post(review_path, headers=member_headers, json=action), 200)['proposal']
                assert reviewed['state'] == 'approved' and reviewed['reviewed_by'] == member.user_id
                result_id, = reviewed['result_candidate_ids']
                assert data.records.read('recognition_candidates', result_id).payload['state'] == 'pending'
                assert data.records.read('recognitions', result_id) is None
                assert data.records.read('recognitions', target.id).payload['state'] == 'active'
                repeated = expect(await client.post(review_path, headers=member_headers, json=action), 200)
                assert repeated['proposal'] == reviewed
                confirmed = expect(await client.post(base + '/insights/' + result_id + '/review',
                    headers=member_headers, json={'action': 'confirm', 'expected_revision': 1, 'authority': access['authority']}), 200)
                new = confirmed['insight']
                assert new['confirmed_by'] == member.user_id and new['state'] == 'active'
                assert egress.policy(scope, 'recognition', new['id'])['allowed_purposes'] == []
                expect(await client.get(merges + '/' + pending['operation_id'], headers=viewer_headers, params=query), 403)
                assert data.records.read('v2_candidate_merges', candidate.id) is not None
                expect(await client.post(merges + '/active', headers=member_headers, json={**access,
                    'operation_id': str(uuid4()), 'expected_revisions': {target.id: target.revision, new['id']: new['revision']},
                    'content': '取代候选的原认识', 'conditions': []}), 200)
                invalidated = data.records.read('recognition_candidates', candidate.id)
                assert invalidated.revision == candidate.revision + 1 and invalidated.payload['state'] == 'invalidated'

                async def combine(input_id, input_revision, selected, text):
                    operation = str(uuid4())
                    values = {**access, 'project_id': project, 'operation_id': operation,
                        'candidate_id': input_id, 'candidate_revision': input_revision,
                        'target_id': selected.id, 'target_revision': selected.revision, 'content': text, 'conditions': []}
                    saved = expect(await client.post(merges + '/prepare', headers=member_headers, json=values), 200)
                    accepted = expect(await client.post(merges + '/' + operation + '/review', headers=member_headers,
                        json={**access, 'decision': 'approve', 'expected_revision': saved['proposal']['revision']}), 200)
                    return accepted['proposal']['result_candidate_ids'][0]

                first_candidate = await combine(ancestor.id, ancestor.revision, earlier, '连续合并第一条')
                second_candidate = await combine(first_candidate, 1, later, '连续合并第二条')
                records = data.records
                final_candidate = records.read('recognition_candidates', second_candidate)
                assert records.read(PRIVACY, second_candidate).payload['private'] is False
                assert not pending_merge_private(records, egress, scope, final_candidate)
                egress.set_policy(scope, 'recognition', earlier.id, earlier.revision, 0, [])
                assert pending_merge_private(records, egress, scope, final_candidate)
                backup_id = 'merge-privacy-backup'
                backup = expect(await client.post('/api/v2/server/shared-projects/' + project + '/backups',
                    headers={**member_headers, 'Idempotency-Key': backup_id}, json={}), 200)
                assert backup['state'] == 'ready'
                personal = server.state.server_user_pool._children[member.user_id].application.state.recognition_records
                evidence = personal.read(BACKUPS, backup_id).payload[FIELD]['objects']
                assert next(item for item in evidence if item['collection'] == 'recognition_candidates'
                    and item['id'] == second_candidate)['private'] is True
                historical = next(item for item in evidence if item['collection'] == 'recognition_candidates'
                    and item['id'] == invalidated.object_id)
                assert historical['status'] == 'historical' and historical['private'] is True
                restored = expect(await client.post('/api/v2/server/shared-projects/backups/' + backup_id + '/projects',
                    headers={**member_headers, 'Idempotency-Key': 'merge-privacy-restore'},
                    json={'name': '共享合并备份恢复验收', 'approved': True, 'batch_size': 32}), 200)
                assert restored['state'] == 'completed'
                restored_project = restored['project_id']
                restored_candidates = {row.payload['content']: row
                    for row in personal.list_matching('recognition_candidates', project_id=restored_project)}
                restored_candidate = restored_candidates[final_candidate.payload['content']]
                personal_scope = WorkScope('local-user', restored_project)
                assert restored_candidate.payload['state'] == 'pending'
                assert all(not SourceEgressService(personal).policy(personal_scope, 'experience', identity)['allowed_purposes']
                    for identity in restored_candidate.payload['source_experience_ids'])
                for identity in (ancestor.id, first_candidate, invalidated.object_id):
                    original = records.read('recognition_candidates', identity)
                    copied = restored_candidates[original.payload['content']]
                    assert copied.payload['state'] == 'rejected' and copied.payload['conditions'] == original.payload['conditions']
                    try:
                        RecognitionService(personal).publish(scope=personal_scope, candidate_id=copied.object_id,
                            expected_revision=copied.revision, reviewer=member.user_id)
                    except RecognitionConflict as error:
                        assert str(error) == 'only a pending candidate can be published'
                    else:
                        raise AssertionError('merged_archive_candidate_published')

    with resource_context(server.state.server_context.resources):
        asyncio.run(scenario())
    for path in root.rglob('ai-turns.sqlite3'):
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as connection:
            assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone() == (0,)
    print(json.dumps({'root': str(root), 'project': project, 'project_name': '共享认识合并验收',
        'member': member.user_id, 'viewer': viewer.user_id, 'browser_active': [row.id for row in browser],
        'browser_pending': browser_pending.id, 'browser_target': browser_target.id,
        'active_merge': True, 'pending_merge_and_confirm': True, 'private_provenance': True,
        'recursive_backup_privacy': True, 'archived_merge_ancestors_stopped': 2,
        'archived_invalidated_merge_ancestor': True, 'model_attempts': 0}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('isolated_root')
    options = parser.parse_args()
    code = Path(__file__).resolve().parents[1]
    root = Path(options.isolated_root).resolve()
    if root.exists() or root == code or any(part.name.lower() == 'runtime' for part in (root, *root.parents)):
        raise ValueError('merge_check_requires_new_isolated_root')
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(code / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ.update(CHRIPTMAS_APP_ROOT=str(root), CHRIPTMAS_DEPLOY='server')
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == root
    sys.path.insert(0, str(code / 'src'))
    check(root)
