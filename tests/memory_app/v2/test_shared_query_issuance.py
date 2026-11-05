import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import gc
import os
from pathlib import Path
import shutil
import socket
import sys
from uuid import uuid4
import weakref

import pytest


def test_real_issued_query_survives_lru_eviction_and_preserves_scope():
    repo = Path(__file__).resolve().parents[3]
    allocated = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert not any(path.name == 'runtime' for path in (allocated, *allocated.parents))
    base = allocated / ('q' + uuid4().hex[:8])
    assert not base.exists()
    (base / 'config').mkdir(parents=True)
    base = base.resolve(strict=True)
    assert base.is_relative_to(allocated)
    shutil.copyfile(repo / 'config/settings.toml.example', base / 'config/settings.toml')
    prior = {key: os.environ.get(key) for key in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(base), 'server'
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == base
    active, outgoing = [True], []

    def prohibit_network(event, arguments):
        if not active[0] or event != 'socket.connect':
            return
        frame = sys._getframe(1)
        while frame is not None:
            if (frame.f_code.co_name == '_fallback_socketpair'
                    and Path(frame.f_code.co_filename).resolve() == Path(socket.__file__).resolve()):
                return
            frame = frame.f_back
        outgoing.append(arguments[1])
        raise RuntimeError('issued_query_check_disallows_network')

    sys.addaudithook(prohibit_network)
    try:
        _check_real_factory(base)
        assert not outgoing
    finally:
        active[0] = False
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _check_real_factory(base):
    # 应用只在专属目录完成解析后导入，权限与资料均由原服务器工厂建立。
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.material_origin_admission import BoundMaterialOrigins, origin_reference
    from backend.memory_app.v2.material_origin_sql import freeze_sql_identities, validate_sql_identities
    from backend.memory_app.v2.privacy import set_private_project
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_projects import SharedProjectError
    from backend.memory_app.v2.shared_query import SharedQueryFactory
    from backend.memory_app.v2.shared_rendered_preparation import recognition_wire_input
    from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context

    devices = DeviceRegistry(base / 'server')
    users = ServerUsers(base, records=devices.records)
    paired = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='发行检查管理员')
    admin = devices.authenticate(paired['key'])
    actors = []
    for name in ('发行检查所有者', '发行检查只看成员'):
        user = users.create(admin, name=name)
        pair = devices.exchange(devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)['code'], name=name)
        actors.append(devices.authenticate(pair['key']))
    owner, viewer = actors

    def child(path, user_id, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, base),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', base / 'users/local-user', base), child_factory=child, port=8015)
    registry = server.state.shared_projects
    project = registry.create(owner, project_name='共享发行资格QA', operation_id='create-' + uuid4().hex)['project']
    registry.invite(owner, project['id'], user_id=viewer.user_id, role='viewer', authority=project['authority'],
        operation_id='invite-' + uuid4().hex)
    invitation = registry.invitations(viewer)[0]
    registry.accept(viewer, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + uuid4().hex)
    data = registry.roots._data(project['id'])
    source = RecognitionService(data.records)
    scope = WorkScope('local-user', project['id'])
    experience = source.stage_experience(scope=scope, content='QA手工资料：逐笔核对金额和单位。')
    candidate = source.propose(scope=scope, content='逐笔核对金额和单位', source_experience_ids=[experience])
    insight = source.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer=owner.user_id)

    async def scenario():
        async with server.router.lifespan_context(server):
            factory, pool = server.state.shared_query_factory, server.state.server_user_pool
            async with pool.lease(viewer.user_id) as app:
                access = authorize_user(registry.users, viewer, viewer.user_id)
                with user_context(access), pool.request_scope(viewer.user_id, app, access):
                    query = factory.query(app, viewer, project['id'])
                    assert query.execution_owner is app.state.workspace_domains.query
                    assert factory.query(app, viewer, project['id'], lease=query.origin_admission.lease) is query
                    query.answer_turns._runtime()
                    entry = query.service.get_recognition(scope=scope, recognition_id=insight.id).retrieval_projection()
                    material = {'type': 'recognition', 'id': insight.id, 'revision': insight.revision,
                        'project_id': project['id'], 'namespace': query.source_namespace}
                    question = '如何核对金额？'
                    payload = {'model': 'text-embedding-3-small', 'input': [question, entry['content']]}
                    local = query.local_reader()
                    with pytest.raises(RecognitionConflict, match='shared_rendered_input_owner_changed'):
                        with recognition_wire_input(local, payload=payload, question=question, entries=[entry]):
                            raise AssertionError('unissued_local_reader_was_accepted')
                    other = SharedQueryFactory(registry, factory.devices)
                    foreign = other.query(app, viewer, project['id'], lease=query.origin_admission.lease)
                    with pytest.raises(RecognitionConflict, match='shared_rendered_input_owner_changed'):
                        with recognition_wire_input(foreign, payload=payload, question=question, entries=[entry]):
                            raise AssertionError('other_factory_query_was_accepted')
                    with recognition_wire_input(query, payload=payload, question=question, entries=[entry]) as prepared:
                        reference = origin_reference(viewer.user_id, query.origin_admission.destination.namespace,
                            'memory-' + uuid4().hex)
                        prepared.capsule(turn_id=reference['object_id'], reference=reference, materials=[material])
                        origins = BoundMaterialOrigins(query.origin_admission, query.origin_admission.lease, reference)
                        assert prepared.public_payload == payload
                        with factory._lock:
                            key, removed = factory._queries.popitem(last=False)
                        assert removed is query
                        del removed
                        # 仅淘汰真实缓存条目，活动发送仍须通过原资格和正文复验。
                        assert prepared.public_payload == payload
                        identities = freeze_sql_identities(origins, [material], query=query, prepared_input=prepared)
                        assert identities
                        validate_sql_identities(origins, identities)
                        captured = copy_context()
                        with ThreadPoolExecutor(max_workers=1) as worker:
                            with pytest.raises(RecognitionConflict, match='shared_rendered_input_preparation_expired'):
                                worker.submit(captured.run, lambda: prepared.public_payload).result()
                        assert prepared.public_payload == payload
                    with pytest.raises(RecognitionConflict, match='shared_rendered_input_preparation_expired'):
                        prepared.public_payload
                    newer = factory.query(app, viewer, project['id'], lease=query.origin_admission.lease)
                    assert newer is not query and newer.origin_factory is factory
                    with recognition_wire_input(query, payload=payload, question=question, entries=[entry]) as active_input:
                        reference = origin_reference(viewer.user_id, query.origin_admission.destination.namespace,
                            'memory-' + uuid4().hex)
                        active_input.capsule(turn_id=reference['object_id'], reference=reference, materials=[material])
                        changed_origins = BoundMaterialOrigins(query.origin_admission, query.origin_admission.lease, reference)
                        assert active_input.public_payload == payload
                        assert freeze_sql_identities(changed_origins, [material], query=query, prepared_input=active_input)
                        source.revise(scope=scope, recognition_id=insight.id, expected_revision=insight.revision,
                            content='已经修订的QA手工方法')
                        with pytest.raises(RecognitionConflict):
                            freeze_sql_identities(changed_origins, [material], query=query, prepared_input=active_input)
                    refreshed = query.service.get_recognition(scope=scope, recognition_id=insight.id)
                    authority = SourceEgressService(query.records)
                    refs = [{'type': 'recognition', 'id': refreshed.id, 'revision': refreshed.revision}]
                    authority.require(authority.snapshot(scope, refs), 'generation')
                    set_private_project(data.records, project['id'], True, 0)
                    with pytest.raises(RecognitionConflict):
                        authority.require(authority.snapshot(scope, refs), 'generation')
                    updated_entry = refreshed.retrieval_projection()
                    with recognition_wire_input(query,
                            payload={'model': 'text-embedding-3-small', 'input': [question, updated_entry['content']]},
                            question=question, entries=[updated_entry]) as revoked_input:
                        detail = registry.detail(owner, project['id'])
                        membership = next(row for row in detail['members'] if row['user_id'] == viewer.user_id)
                        registry.change_member(owner, project['id'], user_id=viewer.user_id, action='remove',
                            expected_revision=membership['revision'], authority=detail['project']['authority'],
                            operation_id='remove-' + uuid4().hex)
                        with pytest.raises(SharedProjectError) as denied:
                            revoked_input.public_payload
                        assert denied.value.status == 403
                        del denied
                    reference = weakref.ref(query)
                    del query, local, origins, changed_origins
                    gc.collect()
                    assert reference() is None
                    with app.state.ai_turn_store._connect() as connection:
                        assert connection.execute('SELECT COUNT(*) FROM ai_turns').fetchone()[0] == 0

    asyncio.run(scenario())
