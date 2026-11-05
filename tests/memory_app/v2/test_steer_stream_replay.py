import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from shutil import copyfile
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest


@pytest.fixture(scope='module')
def live_stream():
    base = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    root = base / ('s-' + uuid4().hex[:6])
    root.mkdir()
    config = root / 'config/settings.toml'
    config.parent.mkdir()
    copyfile(Path('config/settings.toml.example'), config)
    previous = os.environ['CHRIPTMAS_APP_ROOT']
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    from backend.memory_app.app import create_app
    from backend.memory_app.kernel.turn_requests import freeze_turn_request
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.workbench_identity_binding import capture_identity, prepare_admission
    from backend.memory_app.v2.privacy import freeze_turn_materials
    from backend.memory_app.v2.policies import get
    from backend.memory_app.v2.turn_frames import FrameProducerOwner, TurnFrames, STREAMS
    from backend.memory_app.v2.steer import visible_snapshot
    from tests.rebuild.test_steer_admission import event
    app = create_app(runtime_root=root)
    app.state.workspace_domains.query.answer_turns._runtime()
    records, store = app.state.recognition_records, app.state.ai_turn_store
    assert Path(store._path).resolve() == root / '.rebuild-data/ai-turns.sqlite3'
    try:
        with TestClient(app) as client:
            response = client.post('/api/v2/projects', json={'name': '真实插话续读检查'})
            assert response.status_code == 200
            project, identity = response.json()['id'], 'turn-' + uuid4().hex
            _, privacy = freeze_turn_materials(records, app.state.recognition_models, project, [],
                authority=SourceEgressService(records), local_only=True)
            request = freeze_turn_request('project.answer', template_version=2, project_id=project,
                turn_id=identity, session_id='session-' + identity, operation_id='op-' + uuid4().hex,
                idempotency_key='read-' + uuid4().hex, text='实际派生传输检查', refs=[], privacy=privacy,
                created_at=datetime.now(timezone.utc).isoformat())
            binding = capture_identity(app, records, app.state.recognition_documents, project=project,
                scene=None, thread_id='thread-' + uuid4().hex, turn_id=identity)
            bundle = prepare_admission(app, request, binding, store)
            with store.admission_bundle(request, bundle):
                store.claim_turn(request)
            store.append(event(store, request, 'turn.accepted'), expected_sequence=0)
            now = datetime.now(timezone.utc)
            lease = store.try_acquire_run_lease(identity, 'actual-stream-reader', now=now,
                stale_after=now + timedelta(minutes=10))
            assert lease is not None
            runtime, service = app.state.ai_runtime, app.state.product_steers
            token = runtime._run_lease_context.set(lease)
            try:
                with service.run_scope(identity):
                    turn = {'id': identity, 'thread_id': binding['thread_id'], 'intent': 'ask',
                        'user_text': request['input']['text'], 'created_at': request['created_at']}
                    frames = TurnFrames(records, turn_id=identity, project_id=project, turn=turn,
                        request=request, recipe=get('retry', version='@1'), schedule=False,
                        producer_owner=lambda: FrameProducerOwner(turn_id=identity, lease=lease, store=store))
                    assert frames.start()
                    frames.delta('旧' * 400)
                    head = records.read(STREAMS, identity)
                    snapshot = visible_snapshot(records, binding, request, lease, head.payload['sequence'], limit=300)
                    received = event(store, request, 'turn.steer.received')
                    store.accept_steer_bundle(received, expected_sequence=received['sequence'] - 1, run_lease=lease,
                        submission_key='actual-steer', text='这次采用当前要求', snapshot=snapshot,
                        visible_sequence=snapshot['visible_sequence'], maximum_count=5, policy_version='@2')
                    owner = service._owners[identity]
                    service.bind_delivery(identity, frames.deliver_control)
                    service._publish(owner, service._rows(identity))
                    frames.reset_for_steer()
                    frames.delta('新' * 400)
                    head = records.read(STREAMS, identity)
                    assert [item['kind'] for item in head.payload['events']] == ['started', 'delta', 'steer', 'reset', 'delta']
                    assert head.payload['terminal'] is None
                    before = store.events_after(identity)
                    assert not any(item['type'].startswith('model.') for item in before)
                    yield [app, records, store, request, lease, head, before]
            finally:
                runtime._run_lease_context.reset(token)
                store.release_run_lease(identity, lease.owner_id, lease.generation)
    finally:
        os.environ['CHRIPTMAS_APP_ROOT'] = previous


async def read_asgi(app, identity, project, after, count, *, header=None):
    disconnected = asyncio.Event()
    body, status = [], []
    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'},
        'http_version': '1.1', 'method': 'GET', 'scheme': 'http',
        'path': f'/api/v2/workbench/turns/{identity}/stream', 'raw_path': b'', 'root_path': '',
        'query_string': f'project_id={project}&after={after}'.encode(),
        'headers': [(b'host', b'127.0.0.1'), (b'last-event-id', str(after if header is None else header).encode())],
        'client': ('127.0.0.1', 1234), 'server': ('127.0.0.1', 80)}
    async def receive():
        await disconnected.wait()
        return {'type': 'http.disconnect'}
    async def send(message):
        if message['type'] == 'http.response.start':
            status.append(message['status'])
        elif message['type'] == 'http.response.body' and message.get('body'):
            body.append(message['body'])
            if len(body) >= count:
                disconnected.set()
    await asyncio.wait_for(app(scope, receive, send), 10)
    return status[0], body


@pytest.mark.parametrize('after', [0, 2, 3, 4])
def test_actual_steer_and_reset_replay_after_original_cursor_without_dispatch(live_stream, after):
    from backend.memory_app.v2.workbench_transport import sse_event
    app, records, store, request, lease, head, before = live_stream
    identity, project = request['turn_id'], request['scope']['project_id']
    steer = {key: head.payload['events'][2][key] for key in ('id', 'text', 'at', 'state', 'applied_to', 'part')}
    values = [(1, 'started', {'thread_id': head.payload['thread_id'], 'turn':
        {key: head.payload['turn'][key] for key in ('id', 'intent', 'user_text')}}),
        (2, 'delta', {'text': '旧' * 400}), (3, 'steer', steer),
        (4, 'reset', {'text': ''}), (5, 'delta', {'text': '新' * 400})]
    expected = [sse_event(kind, value, sequence).encode() for sequence, kind, value in values if sequence > after]
    status, body = asyncio.run(read_asgi(app, identity, project, after, len(expected)))
    assert status == 200 and body == expected
    assert records.read(head.collection, head.object_id) == head
    assert store.get_request(identity) == request and store.events_after(identity) == before
    store.assert_active_run_lease(lease)


@pytest.mark.parametrize('change', ['steer_extra', 'steer_part', 'steer_time', 'unknown_kind', 'reset_extra',
    'reset_source', 'reset_generation', 'delta_extra', 'delta_generation', 'binding'])
def test_malformed_derived_control_rows_remain_rejected(live_stream, change):
    app, records, store, request, lease, original, before = live_stream
    payload = deepcopy(original.payload)
    if change == 'steer_extra':
        payload['events'][2]['extra'] = True
    elif change == 'steer_part':
        payload['events'][2]['part'] = 1
    elif change == 'steer_time':
        payload['events'][2]['at'] = 'invalid-time'
    elif change == 'unknown_kind':
        payload['events'][2]['kind'] = 'unknown-kind'
    elif change == 'reset_extra':
        payload['events'][3]['extra'] = True
    elif change == 'reset_source':
        payload['events'][3]['source'] = 'unknown-source'
    elif change == 'reset_generation':
        payload['events'][3]['run_generation'] = True
    elif change == 'delta_extra':
        payload['events'][4]['extra'] = True
    elif change == 'delta_generation':
        payload['events'][4]['run_generation'] = True
    else:
        payload['binding']['request']['input']['text'] = '其他请求'
    with records.begin() as tx:
        altered = tx.put(original.collection, original.object_id, payload, expected_revision=original.revision)
        tx.commit()
    try:
        status, _ = asyncio.run(read_asgi(app, request['turn_id'], request['scope']['project_id'], 2, 1))
        assert status == 404
        assert records.read(original.collection, original.object_id) == altered
        assert store.events_after(request['turn_id']) == before
        store.assert_active_run_lease(lease)
    finally:
        with records.begin() as tx:
            restored = tx.put(original.collection, original.object_id, original.payload, expected_revision=altered.revision)
            tx.commit()
        live_stream[5] = restored


@pytest.mark.parametrize('after,header,other_project,status', [(6, None, False, 400),
    (2, 3, False, 400), (2, None, True, 404)])
def test_cursor_and_project_qualification_remain_closed(live_stream, after, header, other_project, status):
    app, records, store, request, lease, head, before = live_stream
    project = 'different-project' if other_project else request['scope']['project_id']
    actual, _ = asyncio.run(read_asgi(app, request['turn_id'], project, after, 1, header=header))
    assert actual == status
    assert records.read(head.collection, head.object_id) == head
    assert store.events_after(request['turn_id']) == before
    store.assert_active_run_lease(lease)


@pytest.mark.parametrize('source', ['frame_prefix', 'terminal_partial'])
def test_existing_reset_sources_keep_exact_field_contract(source):
    from backend.memory_app.v2.turn_frames import append_stream_event
    from backend.memory_app.v2.workbench_stream import _reset_source
    payload = {'sequence': 0, 'events': []}
    event = append_stream_event(payload, 'reset', source=source)
    assert _reset_source(event) == source
    with pytest.raises(ValueError, match='invalid_stream_reset'):
        _reset_source({**event, 'run_generation': 1})
