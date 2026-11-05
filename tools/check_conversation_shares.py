import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import httpx


def _isolate(base):
    workspace, base = Path(__file__).resolve().parents[1], Path(base).resolve()
    if base == workspace or any(part.name == 'runtime' for part in (base, *base.parents)):
        raise ValueError('conversation_share_check_requires_isolated_root')
    return workspace, base


def _configure(root):
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root


def check(base, *, shared_project_only=False, same_project_only=False, archive_privacy_only=False,
          archive_derived_privacy_only=False, revoked_answer_only=False):
    workspace, base = _isolate(base)
    root = base / ('s' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    _configure(root)
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.conversation_shares import COPIES, SHARES, conversation_originals
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.policies import override
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context

    devices = DeviceRegistry(root / 'server')
    install = devices.issue_pairing(user_id='local-user', actor='install')
    admin_token = devices.exchange(install['code'], name='管理设备')['key']
    admin = devices.authenticate(admin_token)
    users = ServerUsers(root, records=devices.records)

    def account(name):
        account_user = users.create(admin, name=name)
        pairing = devices.issue_pairing(user_id=account_user['user_id'], actor=admin.device_id)
        return account_user, devices.exchange(pairing['code'], name='实际验收设备')['key']

    sender, sender_token = account('对话发送方')
    recipient, recipient_token = account('对话接收方')

    def child(path, user, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8013)
    shares = '/api/v2/server/conversation-shares'

    def device(token, key=None):
        return {'Authorization': 'Bearer ' + token, **({'Idempotency-Key': key} if key else {})}

    def ensure(response, status):
        assert response.status_code == status, (response.request.url.path, response.status_code,
            response.json().get('detail') if response.headers.get('Content-Type', '').startswith('application/json') else None)
        return response.json()

    async def run():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://127.0.0.1:8013') as client:
                sender_headers, receiver_headers = device(sender_token), device(recipient_token)
                ensure(await client.get('/api/v2/projects', headers=sender_headers), 200)
                pool = server.state.server_user_pool
                sender_app = pool._children[sender['user_id']].application
                models = sender_app.state.recognition_models
                models.update('generation', {'expected_revision': models.public()['generation']['revision'],
                    'allow_remote': False})
                if revoked_answer_only:
                    await revoked_answer(client, server, admin, devices.authenticate(sender_token), sender_app)
                    return
                if same_project_only:
                    await same_project(client, server, devices, sender_token, sender_app)
                    return
                question = '空白的个人项目中保存了哪些对话资料？'
                with override(compose='@1'):
                    completed = ensure(await client.post('/api/v2/workbench/turns',
                        headers=device(sender_token, 'conversation-source-ask'),
                        json={'project_id': 'inbox', 'scope_kind': 'personal', 'intent': 'ask', 'text': question}), 200)
                turn = completed['turn']
                assert turn['receipt']['ask']['no_match'] is True
                assert not any(event['type'].startswith('model.') for event in sender_app.state.ai_turn_store.events_after(turn['id']))
                preview_path = shares + '/preview/' + turn['id']
                params = {'project_id': 'inbox', 'scope_kind': 'personal'}
                preview = ensure(await client.get(preview_path, params=params, headers=sender_headers), 200)
                snapshot = preview['snapshot']
                assert snapshot == {'question': question, 'answer': turn['receipt']['ask']['answer'], 'citations': []}
                if shared_project_only:
                    await shared_project(client, server, devices, admin, sender, sender_token, recipient,
                        recipient_token, turn, preview, params, sender_app)
                    return
                ensure(await client.get(preview_path, params=params, headers=receiver_headers), 404)
                directory = ensure(await client.get('/api/v2/server/shared-projects/users', headers=sender_headers), 200)
                assert any(row['user_id'] == recipient['user_id'] for row in directory)
                send = {**params, 'turn_id': turn['id'], 'source_revision': preview['source_revision'],
                    'to_user_id': recipient['user_id'], 'include_citations': []}
                sender_command = device(sender_token, 'first-share')
                shared = ensure(await client.post(shares, json=send, headers=sender_command), 200)
                identity = shared['id']
                detail = shares + '/' + identity
                assert ensure(await client.post(shares, json=send, headers=sender_command), 200) == shared
                assert ensure(await client.get(shares + '/commands/first-share', headers=sender_headers), 200)['share'] == shared
                ensure(await client.post(shares, json={**send, 'answer': '客户端正文'}, headers=sender_command), 400)
                ensure(await client.post(shares, json={**send, 'include_citations': [0]},
                    headers=device(sender_token, 'unknown-citation')), 400)
                ensure(await client.get(detail, headers=device(admin_token)), 403)
                inbox = ensure(await client.get(shares, headers=receiver_headers), 200)['items']
                assert [row['id'] for row in inbox] == [identity] and inbox[0]['user_name'] == sender['name']
                received = ensure(await client.get(detail, headers=receiver_headers), 200)
                assert received['snapshot'] == snapshot and 'source' not in received
                ensure(await client.get(detail + '/citations/0', headers=receiver_headers), 403)
                print('completed native answer, two accounts, whitelist snapshot, delivery and outsider gates verified', flush=True)

                archive_input = {'project_id': 'inbox', 'expected_revision': received['revision']}
                file_headers = device(recipient_token, 'file-original')
                if archive_privacy_only or archive_derived_privacy_only:
                    await personal_archive_privacy(client, pool, recipient, receiver_headers, file_headers,
                        detail, archive_input, snapshot, derived=archive_derived_privacy_only)
                    return
                with devices.records.begin() as central:
                    central.connection.execute(
                        "CREATE TRIGGER check_conversation_file_rollback BEFORE UPDATE ON crp_structured_records "
                        "WHEN NEW.collection='shared_turns' AND json_extract(NEW.payload_json, '$.filed_at') IS NOT NULL "
                        "BEGIN SELECT RAISE(ABORT, 'isolated archive settlement interruption'); END")
                    central.commit()
                interrupted = await client.post(detail + '/file', headers=file_headers, json=archive_input)
                ensure(interrupted, 503)
                receiver_app = pool._children[recipient['user_id']].application
                receiver_records = receiver_app.state.recognition_records
                assert len(receiver_records.list(COPIES)) == 1
                assert devices.records.read(SHARES, identity).payload['filed_at'] is None
                pending = ensure(await client.get(detail, headers=sender_headers), 200)
                assert pending['state'] == 'filing'
                with devices.records.begin() as central:
                    central.connection.execute('DROP TRIGGER check_conversation_file_rollback')
                    central.commit()
                blocked = await client.post(detail + '/revoke', headers=device(sender_token, 'already-copied-revoke'),
                    json={'expected_revision': pending['revision']})
                assert ensure(blocked, 409)['detail'] == 'conversation_share_already_filed'
                archived = ensure(await client.get(shares + '/commands/file-original', headers=receiver_headers), 200)['share']
                assert archived['state'] == 'filed'
                assert ensure(await client.post(detail + '/file', headers=file_headers, json=archive_input), 200) == archived
                assert len(receiver_records.list(COPIES)) == 1
                item_id = archived['archive']['item_id']
                original = conversation_originals(receiver_records, 'inbox',
                    receiver_app.state.recognition_documents.namespace_id, identity=item_id)[item_id]
                assert original.payload['input_kind'] == 'conversation'
                assert question in original.payload['source_text']
                assert snapshot['answer'] in original.payload['source_text']
                print('actual recipient original committed before central failure, recovered same copy and rejected revocation', flush=True)

                unsaved = ensure(await client.post(shares, json=send, headers=device(sender_token, 'unfiled-share')), 200)
                undone = shares + '/' + unsaved['id']
                revoked = ensure(await client.post(undone + '/revoke',
                    headers=device(sender_token, 'unfiled-revoke'), json={'expected_revision': unsaved['revision']}), 200)
                assert revoked['state'] == 'revoked'
                ensure(await client.get(undone, headers=receiver_headers), 404)
                assert ensure(await client.get(shares, headers=receiver_headers), 200)['items'] == []
                assert receiver_records.read('workspace_items', item_id) == original
                print('unfiled share revoked, inbox refreshed, archived original retained, zero model calls', flush=True)

    async def personal_archive_privacy(client, pool, recipient, receiver_headers, file_headers,
            detail, archive_input, snapshot, *, derived):
        from backend.memory_app.source_egress import POLICY_COLLECTIONS
        from backend.memory_app.v2.conversation_share_snapshot import snapshot_markdown
        import sqlite3

        result = ensure(await client.post(detail + '/file', headers=file_headers, json=archive_input), 200)
        item_id = result['archive']['item_id']
        path = '/api/v2/library/sources/' + item_id + '/text'
        params = {'project_id': 'inbox'}
        current = ensure(await client.get(path, params=params, headers=receiver_headers), 200)
        assert current['text'] == snapshot_markdown(snapshot)
        app = pool._children[recipient['user_id']].application
        records = app.state.recognition_records
        collection = POLICY_COLLECTIONS['original_item']
        policy = records.read(collection, item_id)
        assert policy.payload['allowed_purposes'] == []
        # 在隔离副本中还原旧归档只有原件与出生凭证的实际保存形状。
        with records.begin() as transaction:
            transaction.delete(collection, item_id, expected_revision=policy.revision)
            transaction.commit()
        assert ensure(await client.get(path, params=params, headers=receiver_headers), 200) == current
        if derived:
            await legacy_archive_dependencies(client, app, receiver_headers, path, item_id, current['text'])
            return
        models = app.state.recognition_models
        config = models.public()['generation']
        models.update('generation', {'expected_revision': config['revision'], 'base_url': 'https://127.0.0.2:9/v1',
            'model': 'gpt-4o-mini', 'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': True})
        denied = ensure(await client.post('/api/workspace/v1/items/' + item_id + '/process',
            json=params, headers=receiver_headers), 409)
        assert denied['detail'] == 'private_source_remote_blocked'
        assert records.read(collection, item_id).payload['allowed_purposes'] == []
        assert records.read('workspace_items', item_id).payload['status'] == 'staged'
        for database in root.rglob('ai-turns.sqlite3'):
            with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
                assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone() == (0,)
        print('actual recipient archive readable, legacy privacy restored before remote processing, model attempts=0', flush=True)

    async def legacy_archive_dependencies(client, app, headers, path, item_id, text):
        import sqlite3
        from backend.memory_app.document_recognition import ensure_document_experience
        from backend.memory_app.source_egress import POLICY_COLLECTIONS, SourceEgressService, recognition_service
        from backend.memory_app.v2.conversation_shares import ConversationShares
        from backend.memory_app.v2.material_origin_sql import _identity
        from backend.memory_app.v2.shared_projects import SharedProjectError
        from backend.memory_app.workspace_confirmation import WorkspaceConfirmation
        from backend.memory_app.workspace_generation import _draft
        from backend.recognition import RecognitionConflict, WorkScope

        records, documents = app.state.recognition_records, app.state.recognition_documents
        project = 'inbox'
        scope, authority = WorkScope('local-user', project), SourceEgressService(records)
        item = records.read('workspace_items', item_id)
        namespace = '/'.join(('personal', records.user_id, documents.namespace_id))
        original_material = {'type': 'original_item', 'id': item_id, 'revision': item.revision, 'project_id': project}
        frozen = {'material': original_material, 'project_id': project, 'namespace': namespace,
            'sql_identity': _identity(records, namespace, 'workspace_items', item_id)}

        def blocked(kind, identity, revision):
            graph = authority.snapshot(scope, [{'type': kind, 'id': identity, 'revision': revision}])
            original = next(node for node in graph['nodes'] if node['type'] == 'original_item' and node['id'] == item_id)
            assert original['policy_revision'] == 0 and original['effective_purposes'] == []
            try:
                authority.require(graph, 'generation')
            except RecognitionConflict:
                return
            raise AssertionError('legacy conversation dependency admitted to remote generation')

        draft = _draft({'title': '对话整理', 'summary': '保留个人归档的问答原文。', 'topics': [], 'facts': [],
            'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, text)
        ready = app.state.workspace_domains.items.update(item_id, project, {'staged'}, status='ready', draft=draft)
        blocked('original_item', item_id, ready['revision'])
        try:
            ConversationShares._require_citation(records, project, namespace, frozen)
        except SharedProjectError as error:
            assert error.status == 409 and str(error) == 'conversation_citation_changed'
        else:
            raise AssertionError('stale selected citation remained eligible')
        confirmed = WorkspaceConfirmation(app.state.recognition_runtime_root, records, documents).confirm(
            item_id, project, ready['revision'], lambda _: '# 对话整理\n\n' + text)
        assert confirmed['status'] == 'confirmed'
        blocked('original_source', confirmed['source_id'], 1)
        experience_id, _ = ensure_document_experience(documents, recognition_service(records), project,
            confirmed['document_id'])
        experience = records.read('recognition_experiences', experience_id)
        blocked('experience', experience_id, experience.revision)
        assert records.read(POLICY_COLLECTIONS['original_item'], item_id) is None
        assert ensure(await client.get(path, params={'project_id': project}, headers=headers), 200)['text'] == text
        for database in root.rglob('ai-turns.sqlite3'):
            with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
                assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone() == (0,)
        print('legacy ready original, confirmed source and derived experience deny egress with no policy row; local original readable; models=0', flush=True)

    async def revoked_answer(client, server, owner, actor, sender_app):
        registry = server.state.shared_projects
        target = registry.create(owner, project_name='对话权限核对', operation_id='answer-qualification')['project']
        project = target['id']
        registry.invite(owner, project, user_id=actor.user_id, role='viewer', authority=target['authority'],
            operation_id='answer-invite')
        invitation, = registry.invitations(actor)
        registry.accept(actor, invitation['id'], expected_revision=invitation['revision'], operation_id='answer-accept')
        params = {'project_id': project, 'scope_kind': 'shared'}
        body = {**params, 'intent': 'ask', 'text': '这个空项目有哪些资料？'}
        headers = device(sender_token, 'shared-answer-original')
        with override(compose='@1', retrieve='@1'):
            completed = ensure(await client.post('/api/v2/workbench/turns', json=body, headers=headers), 200)
        turn = completed['turn']
        original = turn['receipt']['ask']
        assert turn['scope_kind'] == 'shared' and original['no_match'] is True and original['context']['egress']
        path = '/api/v2/workbench/threads/' + turn['thread_id']
        current, = ensure(await client.get(path, params=params, headers=headers), 200)['turns']
        assert current['receipt']['ask']['context']['egress']
        native = sender_app.state.ai_turn_store
        events = tuple(native.events_after(turn['id']))
        assert not any(event['type'].startswith('model.') for event in events)

        preview_path = shares + '/preview/' + turn['id']
        preview = ensure(await client.get(preview_path, params=params, headers=headers), 200)
        command = {**params, 'turn_id': turn['id'], 'source_revision': preview['source_revision'],
            'to_user_id': recipient['user_id'], 'include_citations': []}
        share_headers = device(sender_token, 'shared-answer-before-leave')
        existing = ensure(await client.post(shares, json=command, headers=share_headers), 200)
        authority = registry.detail(actor, project)['project']['authority']
        registry.change_member(actor, project, user_id=actor.user_id, action='leave',
            expected_revision=authority['member_revision'], authority=authority, operation_id='answer-leave')

        def redacted(value, previous):
            ask = value['receipt']['ask']
            assert value['id'] == turn['id'] and value['scope_kind'] == 'shared'
            assert ask['answer'] == original['answer'] and ask['no_match'] is True
            assert ask['model_usage'] == previous['model_usage'] and ask.get('model_cost') == previous.get('model_cost')
            assert ask['sources_available'] is False and ask['context'] is None and ask['citations'] == []
            assert ask['trace'] == ask['excluded_sources'] == [] and ask['egress_receipt_id'] is None

        saved, = ensure(await client.get(path, params=params, headers=headers), 200)['turns']
        redacted(saved, current['receipt']['ask'])
        redacted(ensure(await client.post('/api/v2/workbench/turns', json=body, headers=headers), 200)['turn'], original)
        stream = await client.get('/api/v2/workbench/turns/' + turn['id'] + '/stream',
            params={'project_id': project, 'after': 0}, headers=headers)
        assert stream.status_code == 200
        done, = [json.loads(block.split('data: ', 1)[1]) for block in stream.text.split('\n\n')
            if block.startswith('event: done\n')]
        redacted(done['turn'], current['receipt']['ask'])
        assert tuple(native.events_after(turn['id'])) == events

        assert ensure(await client.get(preview_path, params=params, headers=headers), 200) == preview
        assert ensure(await client.post(shares, json=command, headers=share_headers), 200) == existing
        sent = ensure(await client.post(shares, json=command, headers=device(sender_token, 'shared-answer-after-leave')), 200)
        received = ensure(await client.get(shares + '/' + sent['id'], headers=device(recipient_token)), 200)
        assert received['snapshot'] == preview['snapshot'] and received['snapshot']['citations'] == []
        print('shared NoMatch retained body and usage after leave in history, replay and SSE; body-only share and existing replay preserved; models=0', flush=True)

    async def same_project(client, server, devices, sender_token, sender_app):
        from backend.memory_app.privacy_policy import set_private_project
        from backend.memory_app.source_egress import POLICY_COLLECTIONS, SourceEgressService
        from backend.recognition import RecognitionConflict, WorkScope

        registry = server.state.shared_projects
        actor = devices.authenticate(sender_token)
        project = registry.create(actor, project_name='共享对话原项目', operation_id='share-same-project')['project']['id']
        key = 'same-project-conversation'
        headers = device(sender_token, key)
        params = {'project_id': project, 'scope_kind': 'shared'}
        with override(compose='@1', retrieve='@1'):
            answered = ensure(await client.post('/api/v2/workbench/turns',
                headers=device(sender_token, 'same-project-answer'),
                json={**params, 'intent': 'ask', 'text': '这个共享项目有哪些资料？'}), 200)
        turn = answered['turn']
        assert turn['scope_kind'] == 'shared' and turn['receipt']['ask']['no_match'] is True
        preview = ensure(await client.get(shares + '/preview/' + turn['id'], params=params, headers=headers), 200)
        target = shares + '/projects/' + project
        scope = ensure(await client.get(target, headers=headers), 200)
        source = {'source_revision': preview['source_revision'], 'turn_id': turn['id'], 'include_citations': []}
        body = {**params, **source, 'namespace': scope['namespace'], 'authority': scope['authority']}
        archived = ensure(await client.post(target, headers=headers, json=body), 200)
        assert archived['project_id'] == project and archived['state'] == 'accepted'
        assert ensure(await client.get(target + '/commands/' + key, headers=headers,
            params={'namespace': scope['namespace']}), 200) == archived
        store = sender_app.state.ai_turn_store
        assert not any(event['type'].startswith('model.') for event in store.events_after(turn['id']))
        records = registry.roots._data(project).records
        assert records.read(POLICY_COLLECTIONS['original_item'], archived['item']['id']) is None
        private = set_private_project(records, project, True, 0)
        restricted = ensure(await client.post(target, headers=device(sender_token, 'same-project-private'), json=body), 200)
        restricted_id = restricted['item']['id']
        assert records.read(POLICY_COLLECTIONS['original_item'], restricted_id).payload['allowed_purposes'] == []
        set_private_project(records, project, False, private.revision)
        original_path = '/api/v2/server/shared-projects/' + project + '/library/sources/' + restricted_id + '/text'
        original = ensure(await client.get(original_path, headers=headers), 200)
        from backend.memory_app.v2.conversation_share_snapshot import snapshot_markdown
        assert original['text'] == snapshot_markdown(preview['snapshot'])
        authority = SourceEgressService(records)
        work_scope = WorkScope('local-user', project)
        assert authority.policy(work_scope, 'original_item', archived['item']['id'])['allowed_purposes']
        closed = authority.snapshot(work_scope, [{'type': 'original_item', 'id': restricted_id, 'revision': 1}])
        try:
            authority.require(closed, 'generation')
        except RecognitionConflict:
            assert all(not node['effective_purposes'] for node in closed['nodes'])
        else:
            raise AssertionError('private conversation admitted to remote generation')
        print('same physical project accepted native shared conversation and retained original privacy after the project reopened; models=0', flush=True)

    async def shared_project(client, server, devices, admin, sender, sender_token, viewer, viewer_token,
            turn, preview, params, sender_app):
        from backend.memory_app.v2.shared_intake import COMMANDS
        from backend.memory_app.v2.shared_projects import ACTIVITY

        registry = server.state.shared_projects
        target = registry.create(admin, project_name='对话共享目的地', operation_id='share-project-target')['project']
        target_id = target['id']
        for account, token, role in ((sender, sender_token, 'member'), (viewer, viewer_token, 'viewer')):
            actor = devices.authenticate(token)
            current = registry.detail(admin, target_id)['project']
            registry.invite(admin, target_id, user_id=account['user_id'], role=role,
                authority=current['authority'], operation_id='share-target-invite-' + role)
            invitation, = registry.invitations(actor)
            registry.accept(actor, invitation['id'], expected_revision=invitation['revision'],
                operation_id='share-target-accept-' + role)
        sender_actor = devices.authenticate(sender_token)
        other = registry.create(sender_actor, project_name='不同的共享目的地', operation_id='share-project-other')['project']
        pairing = devices.issue_pairing(user_id=sender['user_id'], actor=admin.device_id)
        alternate_token = devices.exchange(pairing['code'], name='同账号另一设备')['key']
        target_path = shares + '/projects/' + target_id
        key = 'shared-conversation-original'
        sender_command = device(sender_token, key)
        target_scope = ensure(await client.get(target_path, headers=sender_command), 200)
        namespace = {'namespace': target_scope['namespace']}
        recovery = target_path + '/commands/' + key
        assert ensure(await client.get(recovery, params=namespace, headers=sender_command), 200) == {'state': 'not_accepted'}
        ensure(await client.get(shares + '/projects/' + other['id'] + '/commands/' + key,
            params=namespace, headers=sender_command), 409)
        body = {**params, **namespace, 'authority': target_scope['authority'], 'turn_id': turn['id'],
            'source_revision': preview['source_revision'], 'include_citations': []}
        ensure(await client.post(target_path, json=body, headers=device(viewer_token, key)), 403)
        ensure(await client.post(target_path, json={**body, 'include_citations': [0]}, headers=sender_command), 400)
        accepted = ensure(await client.post(target_path, json=body, headers=sender_command), 200)
        assert accepted == {'command_id': key, 'state': 'accepted', 'project_id': target_id,
            'scope_kind': 'shared', **namespace, 'kind': 'conversation', 'item': accepted['item']}
        item_id = accepted['item']['id']
        assert accepted['item'] == {'id': item_id, 'input_kind': 'conversation'}
        assert ensure(await client.get(recovery, params=namespace, headers=sender_command), 200) == accepted
        assert ensure(await client.post(target_path, json=body, headers=sender_command), 200) == accepted
        ensure(await client.get(recovery, params=namespace, headers=device(alternate_token)), 409)
        ensure(await client.post(target_path, json={**body, 'include_citations': [0]}, headers=sender_command), 409)
        source_path = '/api/v2/server/shared-projects/' + target_id + '/library/sources'
        source_headers = device(viewer_token)
        sources = ensure(await client.get(source_path, headers=source_headers), 200)
        assert any(item['id'] == item_id and item['kind'] == 'conversation' for item in sources['items'])
        full = ensure(await client.get(source_path + '/' + item_id + '/text', headers=source_headers), 200)
        from backend.memory_app.v2.conversation_share_snapshot import snapshot_markdown
        assert full['text'] == snapshot_markdown(preview['snapshot'])
        source = registry.roots._data(target_id).records
        original, = source.list('workspace_items')
        recorded, = source.list(COMMANDS)
        activity, = source.list_matching(ACTIVITY, action='remember', object_id=item_id)
        assert original.object_id == recorded.payload['item_id'] == activity.payload['object_id'] == item_id
        assert recorded.payload['device_id'] == sender_actor.device_id
        assert activity.payload['actor_user_id'] == sender['user_id'] and activity.payload['action'] == 'remember'
        assert not sender_app.state.recognition_records.list('workspace_items')
        assert not devices.records.list(SHARES)
        assert not any(event['type'].startswith('model.') for event in sender_app.state.ai_turn_store.events_after(turn['id']))
        print('shared conversation persisted once, recovered by original device, read by viewer, and verified target attribution', flush=True)

    asyncio.run(run())


def read_archive(existing, *, check_admin=False):
    _, root = _isolate(existing)
    if not (root / 'server' / 'devices.sqlite3').is_file() or not (root / 'config' / 'settings.toml').is_file():
        raise ValueError('conversation_share_check_root_unavailable')
    _configure(root)
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.conversation_shares import COMMANDS, SHARES
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context

    devices = DeviceRegistry(root / 'server')
    with devices.records.begin_read() as transaction:
        commands = [row for row in transaction.list(COMMANDS) if row.payload.get('key') == 'first-share'
            and row.payload.get('action') == 'send']
        assert len(commands) == 1
        share = transaction.read(SHARES, commands[0].payload['share_id'])
        sender, recipient = share.payload['from_user_id'], share.payload['to_user_id']
        assert transaction.read('server_users', sender).payload['name'] == '对话发送方'
        assert transaction.read('server_users', recipient).payload['name'] == '对话接收方'
        assert share.payload['filed_at'] and share.payload['revoked_at'] is None
    issuers = [row['device_id'] for row in devices.list_devices('local-user') if row['revoked_at'] is None]
    assert issuers and devices.identity_for_device(issuers[0], 'local-user') is not None
    pairing = devices.issue_pairing(user_id=recipient, actor=issuers[0])
    token = devices.exchange(pairing['code'], name='归档原件读取设备')['key']
    assert devices.authenticate(token).user_id == recipient

    def child(path, user, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8013)

    async def admin_proxy(client):
        from backend.memory_app.server_audit import AUDIT

        pairing = devices.issue_pairing(user_id='local-user', actor=issuers[0])
        admin_token = devices.exchange(pairing['code'], name='分享代办检查设备')['key']
        actor = devices.authenticate(admin_token)
        sender_headers = {'Authorization': 'Bearer ' + admin_token, 'X-Chriptmas-Target-User': sender}
        recipient_headers = {**sender_headers, 'X-Chriptmas-Target-User': recipient}
        path = '/api/v2/server/conversation-shares'
        source = {key: share.payload['source'][key] for key in ('turn_id', 'project_id', 'scope_kind')}
        preview_path = path + '/preview/' + source['turn_id']
        params = {key: source[key] for key in ('project_id', 'scope_kind')}
        denied = await client.get(preview_path, params=params, headers={'X-Chriptmas-Target-User': sender})
        assert denied.status_code == 403
        preview = await client.get(preview_path, params=params, headers=sender_headers)
        assert preview.status_code == 200, preview.status_code
        assert preview.json()['snapshot'] == share.payload['snapshot']
        sender_app = server.state.server_user_pool._children[sender].application
        events_before = sender_app.state.ai_turn_store.events_after(source['turn_id'])
        send_key = 'admin-send-' + uuid4().hex
        send = await client.post(path, headers={**sender_headers, 'Idempotency-Key': send_key}, json={
            **source, 'source_revision': preview.json()['source_revision'], 'to_user_id': recipient, 'include_citations': []})
        assert send.status_code == 200, (send.status_code, send.json().get('detail'))
        created = send.json()
        identity, detail = created['id'], path + '/' + created['id']
        assert devices.records.read(SHARES, identity).payload['from_user_id'] == sender
        assert devices.records.read(COMMANDS, sender + '--' + send_key).payload['actor_device_id'] == actor.device_id
        outsider = await client.get(detail, headers={'Authorization': 'Bearer ' + admin_token})
        assert outsider.status_code == 403
        inbox = await client.get(detail, headers=recipient_headers)
        assert inbox.status_code == 200 and inbox.json()['direction'] == 'inbox' and inbox.json()['user_id'] == sender
        citation = await client.get(detail + '/citations/0', headers=recipient_headers)
        assert citation.status_code == 403 and citation.json()['detail'] == 'conversation_citation_static'
        file_key = 'admin-file-' + uuid4().hex
        archived = await client.post(detail + '/file', headers={**recipient_headers, 'Idempotency-Key': file_key}, json={
            'project_id': 'inbox', 'expected_revision': inbox.json()['revision']})
        assert archived.status_code == 200, (archived.status_code, archived.json().get('detail'))
        original = archived.json()['archive']['item_id']
        recovered = await client.get(path + '/commands/' + file_key)
        assert recovered.status_code == 200 and recovered.json()['share']['archive']['item_id'] == original
        recipient_app = server.state.server_user_pool._children[recipient].application
        writer = recipient_app.state.recognition_records.writer_for('workspace_items', original, 1)
        assert writer['by'] == 'admin' and writer['actor_device_id'] == actor.device_id and writer['target_user_id'] == recipient
        commands = [devices.records.read(COMMANDS, owner + '--' + key).payload
            for owner, key in ((sender, send_key), (recipient, file_key))]
        assert all(command['by'] == 'admin' and command['actor_user_id'] == actor.user_id for command in commands)
        assert any(row.payload['phase'] == 'outcome' and row.payload['target_user_id'] == recipient
            and row.payload['actor_device_id'] == actor.device_id and row.payload['status'] == 200
            for row in devices.records.list(AUDIT))
        events_after = sender_app.state.ai_turn_store.events_after(source['turn_id'])
        assert events_before == events_after and not any(event['type'].startswith('model.') for event in events_after)
        print('admin real target send/archive, recipient command replay, current static citation and unrelated target denied, zero model calls')

    async def run():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://127.0.0.1:8013',
                    headers={'Authorization': 'Bearer ' + token}) as client:
                if check_admin:
                    await admin_proxy(client)
                    return
                archive = share.payload['archive']
                params = {'project_id': archive['project_id']}
                listed = await client.get('/api/v2/library/sources', params=params)
                assert listed.status_code == 200, listed.status_code
                assert any(row['id'] == archive['item_id'] and row['kind'] == 'conversation'
                    for row in listed.json()['items'])
                full = await client.get('/api/v2/library/sources/' + archive['item_id'] + '/text', params=params)
                assert full.status_code == 200, full.status_code
                original = full.json()
                assert original['kind'] == 'conversation'
                assert share.payload['snapshot']['question'] in original['text']
                assert share.payload['snapshot']['answer'] in original['text']
                print('archived conversation listed and original text loaded through the real personal library', flush=True)
                print('share=' + share.object_id + ' sender=' + sender + ' recipient=' + recipient
                    + ' turn=' + share.payload['source']['turn_id'] + ' original=' + archive['item_id'], flush=True)

    asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('isolated_root')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--read-archive', action='store_true')
    modes.add_argument('--check-admin', action='store_true')
    modes.add_argument('--shared-project-only', action='store_true')
    modes.add_argument('--same-project-only', action='store_true')
    modes.add_argument('--archive-privacy-only', action='store_true')
    modes.add_argument('--archive-derived-privacy-only', action='store_true')
    modes.add_argument('--revoked-answer-only', action='store_true')
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    if args.read_archive or args.check_admin:
        read_archive(args.isolated_root, check_admin=args.check_admin)
    else:
        check(args.isolated_root, shared_project_only=args.shared_project_only, same_project_only=args.same_project_only,
            archive_privacy_only=args.archive_privacy_only, archive_derived_privacy_only=args.archive_derived_privacy_only,
            revoked_answer_only=args.revoked_answer_only)
