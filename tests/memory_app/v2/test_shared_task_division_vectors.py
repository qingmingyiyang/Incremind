import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pytest


def test_actual_shared_task_division_native_embedding_admission(tmp_path_factory):
    allocated = tmp_path_factory.mktemp('shared-task-division').resolve(strict=True)
    assert all(path.name != 'runtime' for path in (allocated, *allocated.parents))
    base = allocated / 'server'
    (base / 'config').mkdir(parents=True)
    repository = Path(__file__).resolve().parents[3]
    shutil.copyfile(repository / 'config/settings.toml.example', base / 'config/settings.toml')
    previous = {name: os.environ.get(name) for name in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(base.resolve(strict=True)), 'server'
    try:
        _actual_case(base)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _actual_case(base):
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.privacy_policy import set_private_project, is_private_project
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_projects import SharedProjectError
    from backend.memory_app.v2.shared_task_materials import freeze_task_request, task_material_item
    from backend.memory_app.v2.shared_task_division_vectors import DivisionVectorProducer
    from backend.memory_app.v2.shared_rendered_preparation import RenderedEmbeddingBatch
    from backend.memory_app.v2.task_divisions import TaskDivisions
    from backend.memory_app.workbench_identity_binding import capture_identity, prepare_admission
    from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
    from backend.recognition_retrieval import SQLiteEmbeddingCache
    from backend.recognition_retrieval.service import _entry
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context

    devices = DeviceRegistry(base / 'server')
    users = ServerUsers(base, records=devices.records)
    pair = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='分工材料管理员')
    admin = devices.authenticate(pair['key'])
    actors = []
    for name in ('分工材料所有者', '分工材料成员'):
        account = users.create(admin, name=name)
        pair = devices.exchange(devices.issue_pairing(user_id=account['user_id'], actor=admin.device_id)['code'], name=name)
        actors.append(devices.authenticate(pair['key']))
    owner, member = actors

    def child(path, user_id, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, base), server_context=context)

    server = create_server_application(DeploymentLayout('server', base / 'users/local-user', base), child_factory=child, port=8015)
    registry = server.state.shared_projects
    project = registry.create(owner, project_name='分工材料检查', operation_id='create-' + uuid4().hex)['project']
    registry.invite(owner, project['id'], user_id=member.user_id, role='viewer', authority=project['authority'],
        operation_id='invite-' + uuid4().hex)
    invitation = registry.invitations(member)[0]
    registry.accept(member, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + uuid4().hex)
    data = registry.roots._data(project['id'])
    service = RecognitionService(data.records)
    scope = WorkScope('local-user', project['id'])
    source = service.stage_experience(scope=scope, content='实际分工资料要求逐项列出。')
    proposed = service.propose(scope=scope, content='实际分工资料要求逐项列出。',
        source_experience_ids=[source], candidate_id='candidate-division-source')
    recognition = service.publish(scope=scope, candidate_id=proposed.id,
        expected_revision=proposed.revision, reviewer=owner.user_id)

    async def scenario():
        async with server.router.lifespan_context(server):
            pool = server.state.server_user_pool
            async with pool.lease(member.user_id) as app:
                access = authorize_user(registry.users, member, member.user_id)
                with user_context(access), pool.request_scope(member.user_id, app, access):
                    query = server.state.shared_query_factory.query(app, member, project['id'])
                    query.answer_turns._runtime()
                    records, models = query.receipt_records, query.models
                    models.update('embedding', {'expected_revision': models.public()['embedding']['revision'],
                        'base_url': 'http://127.0.0.1:9/v1', 'model': 'receipt-only-embedding',
                        'allow_remote': True, 'enabled': True})
                    app.state.memory_embedding_settings.update_mode(mode='remote', expected_revision=0)
                    set_private_project(records, project['id'], True, 0)
                    assert is_private_project(records, project['id'])
                    assert not is_private_project(query.records, project['id'])
                    materials = [{'type': 'recognition', 'id': recognition.id, 'revision': recognition.revision,
                        'project_id': project['id'], 'namespace': query.source_namespace}]
                    text = '按实际资料逐项编写方案。'
                    native = app.state.ai_turn_store
                    divisions = TaskDivisions(records, models=models)
                    samples, requests = [], []
                    for _ in range(2):
                        public, turn = 'public-' + uuid4().hex, 'task-' + uuid4().hex
                        request, proof = freeze_task_request(query, records=records, models=models,
                            project_id=project['id'], materials=materials, load_text=lambda item: '',
                            text=json.dumps({'task': text}, ensure_ascii=False), local_only=True,
                            turn_id=turn, session_id='session-' + public, operation_id='operation-' + turn,
                            idempotency_key='key-' + turn, created_at=datetime.now(timezone.utc).isoformat(), template_version=3)
                        identity = capture_identity(app, records, app.state.recognition_documents,
                            project=project['id'], scene=None, thread_id='thread-' + uuid4().hex, turn_id=public)

                        def prepare(actual, binding):
                            return (*query.origin_admission.prepare(actual, binding), task_material_item(query, actual, proof))

                        bundle = prepare_admission(app, request, identity, native, admission_prepare=prepare)
                        with native.admission_bundle(request, bundle):
                            assert native.claim_turn(request) == (turn, True)
                        actual = native.get_request(turn)
                        requests.append(actual)
                        with records.begin() as transaction:
                            transaction.put('v2_task_executions', public,
                                {'project_id': project['id'], 'request': actual, 'task_text': text}, expected_revision=0)
                            transaction.commit()
                        divisions.complete(public, project=project['id'], text=text,
                            items=[{'goal': text, 'deliverable': '逐项方案', 'capabilities': [], 'depends_on': []}], outcome='done')
                        samples.append(records.read('v2_task_divisions', public))
                    row = samples[1]
                    producer = DivisionVectorProducer(query, samples, '继续逐项编写方案。')
                    assert producer.allowed(project['id']) is True
                    entries = [{'id': 'task-division-' + sample.object_id, 'revision': sample.revision,
                        'project_id': project['id'], 'content': sample.payload['task_text'], 'source_refs': []}
                        for sample in samples]
                    batch = RenderedEmbeddingBatch(entries)
                    cache = SQLiteEmbeddingCache(str(records.database_path.parent / 'recognition-vectors.sqlite3'))
                    try:
                        cache.write(model_id='identity-cache-fixture', entry=_entry(entries[0]), vector=(1.0, 0.0))
                        observed = []
                        for entry in entries:
                            normalized = _entry(entry)
                            value = cache.read(model_id='identity-cache-fixture', entry=normalized)
                            observed.append(value)
                            batch.observe(normalized, value)
                        assert observed[0] is not None and observed[1] is None
                    finally:
                        cache.close()
                    missing_ids = batch.missing_ids
                    assert missing_ids == ('task-division-' + row.object_id,)
                    payload = {'model': models.public()['embedding']['model'], 'input': [producer.question, text]}
                    with producer.prepare(payload, missing_ids=missing_ids) as prepared:
                        auxiliary = producer.turn(prepared, key={'case': 'actual-division-wire'})
                        assert auxiliary.models is app.state.recognition_models
                        assert auxiliary.qualified_materials == materials
                        assert auxiliary.request['privacy']['material_refs']
                        assert auxiliary.store.claim_turn(auxiliary.request) == (auxiliary.turn_id, True)
                        origin = query.origin_admission.restore(auxiliary.request).auxiliary
                        capsule = origin['rendered_input']
                        assert capsule['wire_payload'] == payload
                        binding = capsule['items'][1]['rendering']['candidate']
                        assert binding['root_request'] == actual
                        assert binding['root_request'] != requests[0]
                        assert binding['sample']['id'] == row.object_id != samples[0].object_id
                        assert binding['sample']['payload'] == dict(row.payload)
                        assert binding['materials'] == materials
                        assert origin['selection']['sql_identities']
                    auxiliary.validate()
                    with producer.prepare(payload, missing_ids=missing_ids) as prepared:
                        reopened = producer.turn(prepared, key={'case': 'actual-division-wire'})
                        assert reopened.turn_id == auxiliary.turn_id
                        assert reopened.store.claim_turn(reopened.request) == (auxiliary.turn_id, False)
                    with pytest.raises(RecognitionConflict, match='shared_task_division_wire_changed'):
                        with producer.prepare({**payload, 'input': [producer.question, '未绑定的正文。']},
                                missing_ids=missing_ids):
                            pass
                    set_private_project(data.records, project['id'], True, 0)
                    with pytest.raises(RecognitionConflict):
                        auxiliary.validate()
                    details = registry.detail(owner, project['id'])
                    membership = next(item for item in details['members'] if item['user_id'] == member.user_id)
                    registry.change_member(owner, project['id'], user_id=member.user_id, action='remove',
                        expected_revision=membership['revision'], authority=details['project']['authority'],
                        operation_id='remove-' + uuid4().hex)
                    with pytest.raises(SharedProjectError) as denied:
                        auxiliary.validate()
                    assert denied.value.status == 403
                    assert not any(event['type'].startswith('model.') for event in native.events_after(auxiliary.turn_id))

    asyncio.run(scenario())
