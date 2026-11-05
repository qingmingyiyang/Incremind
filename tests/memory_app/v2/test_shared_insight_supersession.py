import asyncio
import os
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

import httpx


def test_real_shared_insight_supersession_http(tmp_path_factory):
    root = tmp_path_factory.mktemp('shared-supersession').resolve(strict=True)
    assert root.is_absolute() and 'runtime' not in root.parts
    (root / 'config').mkdir()
    settings = Path(__file__).resolve().parents[3] / 'config/settings.toml.example'
    shutil.copyfile(settings, root / 'config/settings.toml')
    prior = {key: os.environ.get(key) for key in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ.update(CHRIPTMAS_APP_ROOT=str(root), CHRIPTMAS_DEPLOY='server')
    try:
        _case(root)
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _case(root):
    from backend.memory_app.app import create_app
    from backend.memory_app.privacy_policy import set_private_project
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.recall_preferences import set_preference
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.recognition import RecognitionService, WorkScope
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context

    devices = DeviceRegistry(root / 'server')
    users = ServerUsers(root, records=devices.records)
    owner_token = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'],
        name='取代检查所有者')['key']
    owner = devices.authenticate(owner_token)
    account = users.create(owner, name='取代检查只看成员')
    viewer_token = devices.exchange(devices.issue_pairing(user_id=account['user_id'], actor=owner.device_id)['code'],
        name='取代检查只看设备')['key']
    viewer = devices.authenticate(viewer_token)

    def child(path, user_id, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root), server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root), child_factory=child)
    owner_config = users.root_for(owner.user_id) / 'config'
    owner_config.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / 'config/settings.toml', owner_config / 'settings.toml')
    registry = server.state.shared_projects
    project = registry.create(owner, project_name='认识取代检查', operation_id='create-' + uuid4().hex)['project']
    identity = project['id']
    registry.invite(owner, identity, user_id=viewer.user_id, role='viewer', authority=project['authority'],
        operation_id='invite-' + uuid4().hex)
    invitation, = registry.invitations(viewer)
    registry.accept(viewer, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + uuid4().hex)
    data = registry.roots._data(identity)
    service, scope = RecognitionService(data.records), WorkScope('local-user', identity)
    evidence = service.stage_experience(scope=scope, content='新人工资料核对后纠正旧认识，原历史应保存。')
    old_candidate = service.propose(scope=scope, content='旧认识正文', source_experience_ids=[evidence])
    old = service.publish(scope=scope, candidate_id=old_candidate.id, expected_revision=old_candidate.revision,
        reviewer=owner.user_id)
    target = service.revise(scope=scope, recognition_id=old.id, expected_revision=old.revision, content='已编辑的旧认识正文')
    set_preference(data.records, scope, target.id, recognition_revision=target.revision,
        preference_revision=0, state='cooled')
    pending = service.propose(scope=scope, content='新认识正文', source_experience_ids=[evidence])
    following = service.propose(scope=scope, content='下一条待确认认识', source_experience_ids=[evidence])
    set_private_project(data.records, identity, True, 0)
    prior_target = data.records.read('recognitions', target.id)
    prior_history = data.records.list('recognition_versions')
    insights_path = f'/api/v2/server/shared-projects/{identity}/library/insights'
    path = f'{insights_path}/{pending.id}/supersede'
    targets_path = f'{insights_path}/{pending.id}/supersession-targets'

    async def scenario():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://test') as client:
                headers = {'Authorization': 'Bearer ' + owner_token}
                listing = await client.get(insights_path, headers=headers)
                assert listing.status_code == 200, listing.text
                listing = listing.json()
                query = {'namespace': listing['namespace'], 'expected_revision': pending.revision,
                    'target_id': target.id, 'target_revision': target.revision}
                body = {**query, 'authority': listing['authority']}
                denied = await client.post(path, headers={'Authorization': 'Bearer ' + viewer_token}, json=body)
                assert denied.status_code == 403, denied.text
                target_query = {'namespace': listing['namespace'], 'expected_revision': pending.revision}
                denied_targets = await client.get(targets_path, headers={'Authorization': 'Bearer ' + viewer_token}, params=target_query)
                assert denied_targets.status_code == 403, denied_targets.text
                selectable = await client.get(targets_path, headers=headers, params=target_query)
                assert selectable.status_code == 200, selectable.text
                assert [(row['id'], row['revision']) for row in selectable.json()['items']] == [(target.id, target.revision)]
                stale = await client.post(path, headers=headers, json={**body, 'target_revision': old.revision})
                assert stale.status_code == 409, stale.text
                changed = await client.get(path, headers=headers, params={**query, 'target_revision': old.revision})
                assert changed.status_code == 200 and changed.json()['state'] == 'changed', changed.text
                waiting = await client.get(path, headers=headers, params=query)
                assert waiting.status_code == 200 and waiting.json()['state'] == 'pending', waiting.text
                assert not data.records.list('recognition_relation_proposals')
                committed = await client.post(path, headers=headers, json=body)
                assert committed.status_code == 200, committed.text
                result = committed.json()
                new_id = result['insight']['id']
                assert result['insight']['confirmed_by'] == owner.user_id
                assert result['target'] == {'id': target.id, 'revision': target.revision, 'superseded_by': new_id}
                assert result['relation']['from_id'] == new_id and result['relation']['to_id'] == target.id
                applied = await client.get(path, headers=headers, params=query)
                assert applied.status_code == 200 and applied.json()['state'] == 'applied', applied.text
                assert applied.json()['insight_id'] == new_id and applied.json()['relation'] == result['relation']
                remaining = await client.get(f'{insights_path}/{following.id}/supersession-targets', headers=headers,
                    params={**target_query, 'expected_revision': following.revision})
                assert remaining.status_code == 200, remaining.text
                assert [row['id'] for row in remaining.json()['items']] == [new_id]
                repeated = await client.post(path, headers=headers, json=body)
                assert repeated.status_code == 409, repeated.text
                proposal, = data.records.list('recognition_relation_proposals')
                relation, = data.records.list('recognition_relations')
                assert proposal.payload['state'] == 'approved' and proposal.payload['source'] == 'manual'
                assert relation.object_id == result['relation']['id']
                assert data.records.read('recognitions', target.id) == prior_target
                assert set(row.object_id for row in prior_history).issubset(
                    row.object_id for row in data.records.list('recognition_versions'))
                assert all(data.records.read('recognition_versions', row.object_id) == row for row in prior_history)
                validity = data.records.read('v2_insight_validity', target.id).payload
                assert validity['superseded_by'] == new_id and validity['valid_until'] == relation.payload['created_at']
                assert data.records.read('v2_insight_interference', target.id).payload['superseding_id'] == new_id
                assert data.records.read('recognition_recall_preferences', target.id).payload['state'] == 'cooled'

    with resource_context(server.state.server_context.resources):
        asyncio.run(scenario())
    for database in root.glob('users/*/.rebuild-data/ai-turns.sqlite3'):
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
            assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone() == (0,)
