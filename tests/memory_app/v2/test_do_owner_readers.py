from datetime import datetime, timedelta, timezone
from inspect import getclosurevars, unwrap
import os
from pathlib import Path
from shutil import copyfile
from uuid import uuid4

import pytest


@pytest.fixture(scope='module')
def installed(tmp_path_factory):
    root = tmp_path_factory.mktemp('do-owner-readers').resolve(strict=True)
    (root / 'config').mkdir()
    copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example',
        root / 'config/settings.toml')
    previous = {key: os.environ.get(key) for key in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ.update(CHRIPTMAS_APP_ROOT=str(root), CHRIPTMAS_DEPLOY='desktop')
    try:
        from fastapi.testclient import TestClient
        from backend.memory_app.app import create_app
        app = create_app(runtime_root=root)
        app.state.workspace_domains.query.answer_turns._runtime()
        with TestClient(app) as client:
            response = client.post('/api/v2/projects', json={'name': '干活事务读取检查'})
            assert response.status_code == 200, response.text
            yield app, response.json()['id']
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def prepared_task(app, project):
    from backend.memory_app.workbench_identity_binding import capture_identity
    records, documents = app.state.recognition_records, app.state.recognition_documents
    service = getclosurevars(app.state.workbench_task_observer).nonlocals['research']
    identity, thread = 'turn-' + uuid4().hex, 'thread-' + uuid4().hex
    text, now = '列出三项会议准备工作', datetime.now(timezone.utc).isoformat()
    binding = capture_identity(app, records, documents, project=project, scene=None,
        thread_id=thread, turn_id=identity)
    receipt, execution = service.initial(identity, project, text, None,
        workbench_identity=binding, continuation_version='@2')
    turn = {'id': identity, 'thread_id': thread, 'intent': 'do', 'user_text': text, 'created_at': now}
    with records.begin() as tx:
        tx.put('v2_threads', thread, {'project_id': project, 'title': text, 'updated_at': now}, expected_revision=0)
        tx.put('v2_task_executions', identity, execution, expected_revision=0)
        tx.put('v2_turns', identity, {**{key: value for key, value in turn.items() if key != 'id'},
            'project_id': project, 'receipt': {'do': receipt}, 'updated_at': now,
            'instance': 'owner-reader-check', 'item_id': None, 'run_id': None}, expected_revision=0)
        tx.commit()
    return turn, execution


def test_unsealed_do_started_uses_the_original_active_sql_reader(installed):
    from fastapi.routing import _iter_routes_with_context
    from backend.recognition import RecognitionConflict
    app, project = installed
    records, native = app.state.recognition_records, app.state.ai_turn_store
    turn, execution = prepared_task(app, project)
    endpoint = next(route.endpoint for route, _ in _iter_routes_with_context(app.routes)
        if getattr(route, 'path', None) == '/api/v2/workbench/turns' and 'POST' in route.methods)
    scope_for_turn = getclosurevars(unwrap(endpoint)).nonlocals['stream_scope_for_turn']
    before = records.read('v2_turns', turn['id'])
    assert native.get_request(execution['request']['turn_id']) is None
    assert scope_for_turn(turn, None) == 'personal'
    with pytest.raises(RecognitionConflict, match='turn_logical_scope_changed'):
        scope_for_turn({**turn, 'user_text': '另一请求'}, None)
    assert records.read('v2_turns', turn['id']) == before
    assert native.get_request(execution['request']['turn_id']) is None
    assert native.events_after(execution['request']['turn_id']) == ()
    assert native.model_attempt_bindings(execution['request']['turn_id']) == ()


def test_task_input_checkpoint_uses_owner_reads_and_keeps_the_original_uow(installed):
    from backend.memory_app.kernel.task_planner import ProductTaskPlanner
    from backend.memory_app.research_sources import ReadControl
    from backend.memory_app.transaction_records import TransactionRecords
    from backend.memory_app.v2.task_model_inputs import TaskModelInput
    from backend.recognition import RecognitionConflict
    from core.ai_kernel.product_admission import bundle_from_payload
    from tests.rebuild.test_steer_admission import event
    app, project = installed
    records, native, runtime = app.state.recognition_records, app.state.ai_turn_store, app.state.ai_runtime
    _, execution = prepared_task(app, project)
    request = execution['request']
    with native.admission_bundle(request, bundle_from_payload(execution['admission_bundle'])):
        native.claim_turn(request)
    native.append(event(native, request, 'turn.accepted'), expected_sequence=0)
    now = datetime.now(timezone.utc)
    lease = native.try_acquire_run_lease(request['turn_id'], 'task-input-reader-check', now=now,
        stale_after=now + timedelta(minutes=5))
    assert lease is not None
    token = runtime._run_lease_context.set(lease)
    try:
        planner = runtime._planner.inner
        assert isinstance(planner, ProductTaskPlanner)
        route = planner.acquire(request=request, project_id=project, project_profile_id='default',
            project_profile_revision=1, boundary_profile_id='default', boundary_profile_revision=1,
            capability_ids=())
        step, model = 'step-' + uuid4().hex, 'model-request-' + uuid4().hex
        runtime._append(request['turn_id'], 'model.requested', 'running', '真实发送前读取检查',
            step_id=step, model_request_id=model, model_call_purpose='primary')
        control = ReadControl(runtime._begin_planner_control(request['turn_id'], step_id=step,
            model_request_id=model, timeout_ms=60000), records, native, planner.composition.store, request)
        prepared = TaskModelInput(planner, request=request, source_request=request,
            messages=[{'role': 'user', 'content': request['input']['text']}], response_model=None,
            control=control, routing_ref=route.payload_ref, call_index=1, sink=control,
            validate=control.checkpoint)
        before = native.events_after(request['turn_id'])
        prepared.execution_control.checkpoint()
        with records.begin() as tx:
            reader = TransactionRecords(tx)
            prepared.execution_control.validator(reader, request)
            profile = tx.read('v2_task_profiles', request['turn_id'])
            tx.put(profile.collection, profile.object_id, dict(profile.payload), expected_revision=profile.revision)
            with pytest.raises(RecognitionConflict, match='outcome_task_profile_changed'):
                prepared.execution_control.validator(reader, request)
            tx.rollback()
        with pytest.raises(RecognitionConflict, match='task model input source reader changed'):
            prepared.execution_control.validator(native, request)
        prepared.execution_control.checkpoint()
        assert native.events_after(request['turn_id']) == before
        assert native.model_attempt_bindings(request['turn_id']) == ()
        assert not any(item['type'] in {'model.completed', 'turn.completed'} for item in before)
    finally:
        runtime._run_lease_context.reset(token)
        native.release_run_lease(request['turn_id'], lease.owner_id, lease.generation)


def test_actual_history_gets_use_default_reader_without_dispatch_or_fact_changes(installed):
    import asyncio
    import sqlite3
    import httpx
    from backend.memory_app.v2.turn_logical_scope import TurnLogicalScope
    from backend.recognition import RecognitionConflict
    from core.ai_kernel.product_admission import bundle_from_payload
    from tests.rebuild.test_steer_admission import event
    app, project = installed
    records, native = app.state.recognition_records, app.state.ai_turn_store
    turn, execution = prepared_task(app, project)
    request = execution['request']
    with native.admission_bundle(request, bundle_from_payload(execution['admission_bundle'])):
        native.claim_turn(request)
    native.append(event(native, request, 'turn.accepted'), expected_sequence=0)

    def facts():
        connection = sqlite3.connect(Path(native._path).as_uri() + '?mode=ro', uri=True)
        try:
            tables = ('ai_turns', 'ai_turn_events', 'ai_turn_immutable_payloads', 'ai_model_attempt_reservations')
            rows = [connection.execute('SELECT * FROM ' + table + ' WHERE turn_id=? ORDER BY 1,2',
                (request['turn_id'],)).fetchall() for table in tables]
        finally:
            connection.close()
        return rows, [records.read(collection, identity) for collection, identity in (
            ('v2_turns', turn['id']), ('v2_task_executions', turn['id']), ('v2_threads', turn['thread_id']))]

    before = facts()
    async def read_history():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            params = {'project_id': project, 'scope_kind': 'personal'}
            listing = await client.get('/api/v2/workbench/threads', params=params)
            assert listing.status_code == 200, listing.text
            assert turn['thread_id'] in {item['id'] for item in listing.json()['items']}
            detail = await client.get('/api/v2/workbench/threads/' + turn['thread_id'], params=params)
            assert detail.status_code == 200, detail.text
            saved = next(item for item in detail.json()['turns'] if item['id'] == turn['id'])
            assert saved['scope_kind'] == 'personal' and saved['user_text'] == turn['user_text']
            assert saved['receipt']['do']['state'] != 'done'
    asyncio.run(read_history())
    assert facts() == before
    assert native.model_attempt_bindings(request['turn_id']) == ()
    row = records.read('v2_turns', turn['id'])
    with records.begin() as tx:
        tx.put(row.collection, row.object_id, {**row.payload, 'updated_at': 'another-current-revision'},
            expected_revision=row.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict, match='turn_logical_scope_changed'):
        TurnLogicalScope(app).read(row)
