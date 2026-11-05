import argparse
import asyncio
import os
from pathlib import Path
import shutil
import sys
import subprocess
from threading import RLock, Event, getprofile, setprofile_all_threads
from uuid import uuid4

import pytest
import httpx


def check(base):
    workspace, base = Path(__file__).resolve().parents[1], Path(base).resolve()
    if base == workspace or any(part.name == 'runtime' for part in (base, *base.parents)):
        raise ValueError('shared_check_requires_isolated_root')
    root = base / ('l' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root
    from backend.memory_app.app import create_app
    from backend.memory_app.document_recognition import ensure_document_experience
    from backend.memory_app.processing_lease import ProcessingLease
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.source_egress import recognition_service
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.privacy import set_private_project
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_library import SharedLibrary
    from backend.memory_app.v2.shared_library_routes import _SharedOriginal
    from backend.memory_app.v2.shared_library_files import SourceFile
    from backend.memory_app.v2.shared_projects import SharedProjectError
    from backend.memory_app.workspace_confirmation import WorkspaceConfirmation
    from backend.memory_app.workspace_generation import _draft
    from backend.memory_app.workspace_items import WorkspaceItems
    from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider.connection_scope import connection_scope

    devices = DeviceRegistry(root / 'server')
    users = ServerUsers(root, records=devices.records)
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin = devices.authenticate(devices.exchange(pairing['code'], name='共享资料域核验')['key'])
    actors, tokens = [], {}
    for label in ('所有者', '成员', '只看成员', '非成员'):
        account = users.create(admin, name=label)
        pairing = devices.issue_pairing(user_id=account['user_id'], actor=admin.device_id)
        token = devices.exchange(pairing['code'], name=label)['key']
        actors.append(devices.authenticate(token))
        tokens[actors[-1].user_id] = token
    owner, member, viewer, outsider = actors

    def child(path, user, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8013)
    registry = server.state.shared_projects
    project = registry.create(owner, project_name='共享资料实际领域', operation_id='create-' + uuid4().hex)['project']
    for actor, role in ((member, 'member'), (viewer, 'viewer')):
        registry.invite(owner, project['id'], user_id=actor.user_id, role=role, authority=project['authority'],
            operation_id='invite-' + uuid4().hex)
        invitation = registry.invitations(actor)[0]
        registry.accept(actor, invitation['id'], expected_revision=invitation['revision'],
            operation_id='accept-' + uuid4().hex)
    data = registry.roots._data(project['id'])
    items = WorkspaceItems(data.records, ProcessingLease(data.records, 'workspace_items', 'library-real'), RLock())
    text = '共享资料原始正文。私密资料仍可由实际共享成员阅读。'
    (data.root / 'workspace').mkdir(exist_ok=True)
    original = data.root / 'workspace' / 'library-item.txt'
    original.write_text(text, encoding='utf8')
    draft = _draft({'title': '共享资料', 'summary': '共享摘要', 'topics': [], 'facts': [], 'todos': [],
        'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, text)
    uploaded = items.create_upload({'id': 'library-item', 'project_id': project['id'], 'input_kind': 'file',
        'title': '共享资料', 'source_text': text, 'status': 'ready', 'draft': draft, 'error': None,
        'document_id': None, 'created_at': '2026-11-05T00:00:00Z',
        'original_path': str(original), 'original_name': '共享资料.txt'})
    document = WorkspaceConfirmation(data.root, data.records, data.documents).confirm(
        uploaded['id'], project['id'], uploaded['revision'], lambda value: '# 共享资料\n\n## 摘要\n\n共享摘要\n\n## 内容\n\n' + text)
    service = recognition_service(data.records)
    experience, _ = ensure_document_experience(data.documents, service, project['id'], document['document_id'])
    scope = WorkScope('local-user', project['id'])
    candidates = [service.propose(scope=scope, content=content, source_experience_ids=[experience])
        for content in ('需要成员审核的认识', '需要丢弃的认识')]
    set_private_project(data.records, project['id'], True, 0)
    image_id, image_files = 'library-images', []
    for ordinal in (1, 2):
        path = data.root / 'workspace' / (image_id + '-' + str(ordinal) + '.png')
        shutil.copyfile(workspace / 'src/frontend/public/capture-192.png', path)
        image_files.append({'path': str(path), 'name': '原图' + str(ordinal) + '.png'})
    items.create_upload({'id': image_id, 'project_id': project['id'], 'input_kind': 'image',
        'title': '共享图片原件', 'source_text': '', 'status': 'queued', 'draft': None, 'error': None,
        'document_id': None, 'created_at': '2026-11-05T00:00:00Z',
        'original_path': image_files[0]['path'], 'original_name': image_files[0]['name']})
    with data.records.begin() as tx:
        tx.put('v2_image_groups', image_id, {'project_id': project['id'], 'item_id': image_id,
            'images': image_files}, expected_revision=0)
        tx.commit()
    apps = {actor.user_id: child(users.root_for(actor.user_id), actor.user_id, server.state.server_context)
        for actor in (viewer, outsider)}
    library = SharedLibrary(server.state.shared_query_factory)
    unchanged = data.records.list('recognition_candidates')
    for invalid in ([], {}, None, 1):
        with pytest.raises(SharedProjectError, match='shared_library_review_invalid'):
            library.review(member, project['id'], candidates[0].id, action=invalid,
                expected_revision=candidates[0].revision,
                authority=registry.detail(member, project['id'])['project']['authority'])
    assert data.records.list('recognition_candidates') == unchanged

    async def file_body(query, response):
        messages = []
        async def send(message):
            messages.append(message)
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await _SharedOriginal(query, response)({'type': 'http', 'method': 'GET',
            'asgi': {'spec_version': '2.4'}, 'headers': []}, receive, send)
        return b''.join(message.get('body', b'') for message in messages)

    with connection_scope():
        with user_context(authorize_user(registry.users, viewer, viewer.user_id)):
            app = apps[viewer.user_id]
            for layer in ('insights', 'summaries', 'notes', 'sources'):
                result = library.list(app, viewer, project['id'], layer=layer)
                assert result['items'] and not result['can_write']
                assert result['namespace'].startswith('shared/' + project['id'] + '/')
            result = library.drill(app, viewer, project['id'], layer='note', identity=document['document_id'])
            assert text in result['note']['markdown'] and not result['can_write']
            result = library.drill(app, viewer, project['id'], layer='source', identity=uploaded['id'])
            assert result['source']['download_url'] == ('/api/v2/server/shared-projects/' + project['id']
                + '/library/originals/' + uploaded['id'])
            query, response = library.original(app, viewer, project['id'], uploaded['id'])
            assert asyncio.run(file_body(query, response)).decode('utf8') == text
            authority = registry.detail(viewer, project['id'])['project']['authority']
            with pytest.raises(SharedProjectError) as denied:
                library.review(viewer, project['id'], candidates[0].id, action='confirm',
                    expected_revision=candidates[0].revision, authority=authority)
            assert denied.value.status == 403
        with user_context(authorize_user(registry.users, outsider, outsider.user_id)):
            with pytest.raises(SharedProjectError) as denied:
                library.list(apps[outsider.user_id], outsider, project['id'], layer='insights')
            assert denied.value.status == 403
        with user_context(authorize_user(registry.users, member, member.user_id)):
            authority = registry.detail(member, project['id'])['project']['authority']
            confirmed = library.review(member, project['id'], candidates[0].id, action='confirm',
                expected_revision=candidates[0].revision, authority=authority)
            assert confirmed['insight']['state'] == 'active'
            assert confirmed['insight']['confirmed_by'] == member.user_id
            assert confirmed['insight']['by'] == member.user_id
            recognition = data.records.read('recognitions', confirmed['insight']['id'])
            assert recognition.payload['published_by'] == member.user_id
            with pytest.raises((RecognitionConflict, RecognitionError)):
                library.review(member, project['id'], candidates[0].id, action='confirm',
                    expected_revision=candidates[0].revision, authority=authority)
            discarded = library.review(member, project['id'], candidates[1].id, action='discard',
                expected_revision=candidates[1].revision, authority=authority)
            assert discarded['insight'] is None
            assert data.records.read('recognition_candidates', candidates[1].id).payload['state'] == 'rejected'
        activities = registry.activity(viewer, project['id'])
        assert sum(row['action'] == 'confirm' for row in activities) == 1
        assert sum(row['action'] == 'discard' for row in activities) == 1
        async def full_source_http():
            async with server.router.lifespan_context(server):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),
                        base_url='http://127.0.0.1:8013') as client:
                    path = '/api/v2/server/shared-projects/' + project['id'] + '/library/sources/' + uploaded['id'] + '/text'
                    response = await client.get(path, headers={'Authorization': 'Bearer ' + tokens[viewer.user_id]})
                    assert response.status_code == 200, response.text
                    value = response.json()
                    assert value['text'] == text and value['coordinate_space'] == 'workspace_source_text_v1'
                    assert value['namespace'] == 'shared/' + project['id'] + '/' + data.namespace
                    assert value['project_id'] == project['id'] and value['can_write'] is False
                    assert value['download_url'] == ('/api/v2/server/shared-projects/' + project['id']
                        + '/library/originals/' + uploaded['id'])
                    response = await client.get(path, headers={'Authorization': 'Bearer ' + tokens[outsider.user_id]})
                    assert response.status_code == 403, response.text
                    images_path = '/api/v2/server/shared-projects/' + project['id'] + '/library/sources/' + image_id + '/images'
                    response = await client.get(images_path, headers={'Authorization': 'Bearer ' + tokens[viewer.user_id]})
                    assert response.status_code == 200, response.text
                    catalog = response.json()
                    assert catalog['namespace'] == value['namespace'] and len(catalog['images']) == 2
                    assert [entry['name'] for entry in catalog['images']] == [entry['name'] for entry in image_files]
                    image_url = catalog['images'][1]['url']
                    assert image_url == images_path + '/2'
                    response = await client.get(image_url, headers={'Authorization': 'Bearer ' + tokens[viewer.user_id]})
                    assert response.status_code == 200 and response.content == Path(image_files[1]['path']).read_bytes()
                    response = await client.get(image_url, headers={'Authorization': 'Bearer ' + tokens[viewer.user_id],
                        'Range': 'bytes=0-7'})
                    assert response.status_code == 206 and response.content == Path(image_files[1]['path']).read_bytes()[:8]
                    response = await client.get(image_url, headers={'Authorization': 'Bearer ' + tokens[outsider.user_id]})
                    assert response.status_code == 403, response.text
                    with user_context(authorize_user(registry.users, viewer, viewer.user_id)):
                        guarded_query, guarded_response = library.original(apps[viewer.user_id], viewer,
                            project['id'], uploaded['id'])
                        prepared = _SharedOriginal(guarded_query, guarded_response)
                    managed, retained, outside = data.root / 'workspace', data.root / 'workspace-retained', root / 'outside-media'
                    outside.mkdir()
                    (outside / 'library-item.txt').write_text('共享根外的检查资料', encoding='utf8')
                    shutil.copyfile(workspace / 'src/frontend/public/capture-192.png', outside / (image_id + '-2.png'))
                    assert managed.absolute().is_relative_to(root) and retained.absolute().is_relative_to(root)
                    managed.rename(retained)
                    try:
                        subprocess.run(['cmd', '/c', 'mklink', '/J', str(managed), str(outside)],
                            check=True, capture_output=True)
                        assert managed.is_junction() and managed.resolve() == outside.resolve()
                        for blocked_path in (images_path, image_url, value['download_url']):
                            refused = await client.get(blocked_path,
                                headers={'Authorization': 'Bearer ' + tokens[viewer.user_id]})
                            assert refused.status_code == 409, refused.text
                        sent = []
                        async def send(message):
                            sent.append(message)
                        async def receive():
                            return {'type': 'http.request', 'body': b'', 'more_body': False}
                        with user_context(authorize_user(registry.users, viewer, viewer.user_id)):
                            with pytest.raises(SharedProjectError, match='shared_library_file_changed'):
                                await prepared({'type': 'http', 'method': 'GET', 'headers': [],
                                    'asgi': {'spec_version': '2.4'}}, receive, send)
                        assert not sent
                    finally:
                        if managed.is_junction():
                            assert managed.absolute().parent == data.root and managed.resolve() == outside.resolve()
                            managed.rmdir()
                        retained.rename(managed)
                    entered, released, observed = Event(), Event(), {}
                    target = SourceFile._open_windows.__code__ if os.name == 'nt' else SourceFile._open_posix.__code__
                    def observe_open(frame, event, result):
                        if (event == 'return' and frame.f_code is target and isinstance(result, int)
                                and frame.f_locals['self'].path == Path(image_files[1]['path']).absolute()):
                            observed.update(file=frame.f_locals['self'], descriptor=result)
                            entered.set()
                            if not released.wait(10):
                                raise TimeoutError('source_file_cancel_observer_timeout')
                    previous = getprofile()
                    setprofile_all_threads(observe_open)
                    cancelled = asyncio.create_task(client.get(image_url,
                        headers={'Authorization': 'Bearer ' + tokens[viewer.user_id]}))
                    try:
                        assert await asyncio.to_thread(entered.wait, 10)
                        cancelled.cancel()
                        await asyncio.sleep(0.05)
                        assert not cancelled.done()
                        released.set()
                        with pytest.raises(asyncio.CancelledError):
                            await cancelled
                        assert observed['file'].stream is None
                        try:
                            remaining = os.fstat(observed['descriptor'])
                        except OSError:
                            remaining = None
                        assert remaining is None or (remaining.st_dev, remaining.st_ino) != observed['file'].expected[1][:2]
                    finally:
                        released.set()
                        setprofile_all_threads(previous)
                        if not cancelled.done():
                            cancelled.cancel()
                            try:
                                await cancelled
                            except asyncio.CancelledError:
                                pass
                    print('shared-library actual cancellation waited for real in-flight open and closed its file descriptor; cancellation propagated', flush=True)
                    print('shared-library real NTFS workspace junction rejected by catalog/image/original and prepared response; no outside bytes sent', flush=True)
                    print('shared-library actual private image group catalog200 and original bytes200; outsider403; server-provided shared URLs', flush=True)
                    print('shared-library actual public private-source full text200/coordinate/namespace/download; outsider403; normal server lifecycle', flush=True)
        asyncio.run(full_source_http())
        detail = registry.detail(owner, project['id'])
        row = next(row for row in detail['members'] if row['user_id'] == viewer.user_id)
        registry.change_member(owner, project['id'], user_id=viewer.user_id, action='remove',
            expected_revision=row['revision'], authority=detail['project']['authority'],
            operation_id='remove-' + uuid4().hex)
        with user_context(authorize_user(registry.users, viewer, viewer.user_id)):
            with pytest.raises(SharedProjectError) as denied:
                library.drill(apps[viewer.user_id], viewer, project['id'], layer='note', identity=document['document_id'])
            assert denied.value.status == 403
            with pytest.raises(SharedProjectError):
                asyncio.run(file_body(query, response))
        assert data.records.list('v2_turns') == () and data.records.list('workspace_ask_receipts') == ()
        for app in apps.values():
            assert app.state.recognition_records.list('v2_turns') == ()
        print('shared-library domain: private four-layer reads, actual source original, viewer denial, member confirmation/discard, CAS and loss of access passed', flush=True)
        print('shared-library domain: actor/source records remain separate; no model calls started', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    check(args.root)
