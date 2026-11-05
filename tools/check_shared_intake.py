import argparse
import asyncio
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import httpx


def check(base):
    workspace, base = Path(__file__).resolve().parents[1], Path(base).resolve()
    if base == workspace or any(part.name == 'runtime' for part in (base, *base.parents)):
        raise ValueError('shared_check_requires_isolated_root')
    root = base / ('i' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.material_origin_readers import _readonly_connection
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_intake import COMMANDS
    from backend.memory_app.v2.shared_projects import ATTRIBUTION
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider.connection_scope import connection_scope
    from core.storage_provider.record_lineage import verify_lineage

    devices = DeviceRegistry(root / 'server')
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin_token = devices.exchange(pairing['code'], name='投喂管理设备')['key']
    admin = devices.authenticate(admin_token)
    users, actors, tokens = ServerUsers(root, records=devices.records), [], []
    for label in ('来源所有者', '共同投喂成员', '只看成员'):
        user = users.create(admin, name=label)
        pairing = devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)
        token = devices.exchange(pairing['code'], name=label)['key']
        actors.append(devices.authenticate(token))
        tokens.append(token)
    owner, member, viewer = actors

    def child(path, user, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8013)
    registry, jobs = server.state.shared_projects, server.state.server_context.jobs
    project = registry.create(owner, project_name='共同投喂', operation_id='create-intake')['project']
    for actor, role in ((member, 'member'), (viewer, 'viewer')):
        registry.invite(owner, project['id'], user_id=actor.user_id, role=role,
            authority=registry.detail(owner, project['id'])['project']['authority'],
            operation_id='invite-' + actor.user_id)
        invitation = registry.invitations(actor)[0]
        registry.accept(actor, invitation['id'], expected_revision=invitation['revision'],
            operation_id='accept-' + actor.user_id)
    prefix = '/api/v2/server/shared-projects/' + project['id']
    key, body = 'input-' + uuid4().hex, {'text': '成员共同投喂的原始文字。保留完整原件与单独的人审。'}

    async def run():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),
                    base_url='http://127.0.0.1:8013') as client:
                headers = {'Authorization': 'Bearer ' + tokens[1], 'Idempotency-Key': key}
                missing = await client.get(prefix + '/intake/commands/' + key, headers=headers)
                assert missing.status_code == 404 and missing.json()['detail'] == 'shared_intake_not_received', missing.text
                response = await client.post(prefix + '/intake/text', headers=headers, json=body)
                assert response.status_code == 200, response.text
                value = response.json()
                assert value['state'] == 'accepted' and value['scope_kind'] == 'shared'
                assert value['item']['source_text'] == body['text'] and value['item']['status'] == 'staged'
                data = registry.roots._data(project['id'])
                assert value['namespace'] == 'shared/' + project['id'] + '/' + data.namespace
                item_id = value['item']['id']
                commands = data.records.list(COMMANDS)
                assert len(commands) == 1 and commands[0].payload['actor_user_id'] == member.user_id
                verify_lineage(data.records, commands[0].payload['birth'])
                assert data.records.read('original_text_retrieval_index', item_id) is not None
                assert data.records.read('v2_original_sections', item_id) is not None
                assert any(row.payload['actor_user_id'] == member.user_id and row.payload['object_id'] == item_id
                    for row in data.records.list(ATTRIBUTION))
                before = {name: data.records.list(name) for name in
                    ('workspace_items', COMMANDS, ATTRIBUTION, 'v2_shared_activity')}
                recovered = await client.get(prefix + '/intake/commands/' + key, headers=headers)
                assert recovered.status_code == 200 and recovered.json() == value, recovered.text
                replay = await client.post(prefix + '/intake/text', headers=headers, json=body)
                assert replay.status_code == 200 and replay.json() == value, replay.text
                changed = await client.post(prefix + '/intake/text', headers=headers, json={'text': '改变原提交'})
                assert changed.status_code == 409 and changed.json()['detail'] == 'shared_intake_command_conflict', changed.text
                viewer_headers = {'Authorization': 'Bearer ' + tokens[2], 'Idempotency-Key': 'viewer-input'}
                refused = await client.post(prefix + '/intake/text', headers=viewer_headers, json=body)
                assert refused.status_code == 403, refused.text
                viewed = await client.get(prefix + '/intake/items/' + item_id, headers=viewer_headers)
                assert viewed.status_code == 200, viewed.text
                assert viewed.json()['item']['source_text'] == body['text'] and viewed.json()['scope_kind'] == 'shared'
                assert {name: data.records.list(name) for name in before} == before
                async with server.state.server_context.pool.lease(member.user_id) as app:
                    assert app.state.recognition_records.read('workspace_items', item_id) is None
                    assert app.state.recognition_records.list('v2_turns') == ()
                    assert app.state.ai_turn_store.get_request(item_id) is None
                assert not tuple(data.root.rglob('ai-turns.sqlite3'))
                proposed_key = 'thought-' + uuid4().hex
                proposed_headers = {**headers, 'Idempotency-Key': proposed_key}
                proposed_body = {'text': '共同建设认识需要每位成员明确审核。'}
                proposal = await client.post(prefix + '/intake/inspiration',
                    headers=proposed_headers, json=proposed_body)
                assert proposal.status_code == 200, proposal.text
                proposed = proposal.json()
                assert proposed['kind'] == 'inspiration' and proposed['insight']['state'] == 'pending'
                candidate_id = proposed['insight']['id']
                assert not data.records.list('recognitions')
                recovered = await client.get(prefix + '/intake/commands/' + proposed_key, headers=headers)
                assert recovered.status_code == 200 and recovered.json() == proposed, recovered.text
                replay = await client.post(prefix + '/intake/inspiration', headers=proposed_headers, json=proposed_body)
                assert replay.status_code == 200 and replay.json() == proposed, replay.text
                refused = await client.post(prefix + '/intake/inspiration', headers=viewer_headers, json=proposed_body)
                assert refused.status_code == 403, refused.text
                authority = registry.detail(member, project['id'])['project']['authority']
                review_body = {'action': 'confirm', 'expected_revision': proposed['insight']['revision'], 'authority': authority}
                refused = await client.post(prefix + '/library/insights/' + candidate_id + '/review',
                    headers=viewer_headers, json=review_body)
                assert refused.status_code == 403, refused.text
                assert not data.records.list('recognitions')
                reviewed = await client.post(prefix + '/library/insights/' + candidate_id + '/review',
                    headers=headers, json=review_body)
                assert reviewed.status_code == 200, reviewed.text
                assert reviewed.json()['insight']['confirmed_by'] == member.user_id
                assert len(data.records.list('recognitions')) == 1
                recovered = await client.get(prefix + '/intake/commands/' + proposed_key, headers=headers)
                assert recovered.status_code == 200 and recovered.json()['insight']['state'] == 'published', recovered.text
                changed_kind = await client.post(prefix + '/intake/inspiration', headers=headers, json=body)
                assert changed_kind.status_code == 409 and changed_kind.json()['detail'] == 'shared_intake_command_conflict', changed_kind.text
                before = {name: data.records.list(name) for name in before}
                async with server.state.server_context.pool.lease(member.user_id) as app:
                    assert app.state.recognition_records.read('recognition_candidates', candidate_id) is None
                    connection = _readonly_connection(app.state.ai_turn_store._path)
                    try:
                        assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
                    finally:
                        connection.close()
                proxy_key = 'admin-proxy-' + uuid4().hex
                proxy_headers = {'Authorization': 'Bearer ' + admin_token,
                    'x-chriptmas-target-user': member.user_id, 'Idempotency-Key': proxy_key}
                proxy_body = {'text': '管理员在已授权的成员空间提交共享原件。'}
                proxied = await client.post(prefix + '/intake/text', headers=proxy_headers, json=proxy_body)
                assert proxied.status_code == 200, proxied.text
                recovered = await client.get(prefix + '/intake/commands/' + proxy_key, headers=headers)
                assert recovered.status_code == 200 and recovered.json() == proxied.json(), recovered.text
                replay = await client.post(prefix + '/intake/text',
                    headers={**headers, 'Idempotency-Key': proxy_key}, json=proxy_body)
                assert replay.status_code == 200 and replay.json() == proxied.json(), replay.text
                assert any(row.payload['actor_user_id'] == admin.user_id and row.payload['by'] == 'admin'
                    and row.payload['object_id'] == proxied.json()['item']['id'] for row in data.records.list(ATTRIBUTION))
                denied_target = await client.post(prefix + '/intake/text',
                    headers={**headers, 'x-chriptmas-target-user': owner.user_id, 'Idempotency-Key': 'wrong-target'}, json=body)
                assert denied_target.status_code == 403, denied_target.text
                actor_current = jobs.storage_bytes(member.user_id)
                actor_limit = (actor_current // (1024 * 1024) + 1) * 1024 * 1024
                users.update(admin, member.user_id, expected_revision=users.get(member.user_id)['revision'],
                    storage_limit_mb=actor_limit // (1024 * 1024))
                (users.root_for(member.user_id) / 'full-personal.bin').write_bytes(b'F' * (actor_limit - actor_current + 1))
                accepted = await client.post(prefix + '/intake/text',
                    headers={**headers, 'Idempotency-Key': 'source-only-storage'}, json={'text': '个人空间已满仍可向有容量的共享来源提交。'})
                assert accepted.status_code == 200, accepted.text
                assert jobs.storage_bytes(member.user_id) >= actor_limit
                before = {name: data.records.list(name) for name in before}
                current = jobs.storage_bytes(owner.user_id)
                limit = (current // (1024 * 1024) + 1) * 1024 * 1024
                users.update(admin, owner.user_id, expected_revision=users.get(owner.user_id)['revision'],
                    storage_limit_mb=limit // (1024 * 1024))
                (data.root / 'quota-check.bin').write_bytes(b'Q' * (limit - current - 256))
                refused = await client.post(prefix + '/intake/text',
                    headers={**headers, 'Idempotency-Key': 'over-owner-cap'}, json={'text': '容量检查。' * 4000})
                assert refused.status_code == 429, refused.text
                assert {name: data.records.list(name) for name in before} == before
                print('shared-intake actual HTTP member original birth/index/section/attribution; '
                    'command GET/replay recovery and changed-key conflict; viewer read200/write403; '
                    'shared proposal pending/replay and member explicit confirmation/attribution; '
                    'viewer proposal/review403; admin authorized target/own command recovery/by admin, ordinary cross-target403; '
                    'full actor space accepts source-only write; owner finite quota429 rolled back; actor/source separation; model dispatch0', flush=True)
        print('shared-intake real server lifecycle closed', flush=True)

    with resource_context(server.state.server_context.resources), connection_scope():
        asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    check(args.root)
