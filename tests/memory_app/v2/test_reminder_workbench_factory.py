"""真实应用工厂的提醒快速通道；仅隔离外部 completion 和提醒时钟。"""
from contextlib import closing, contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from functools import partial, wraps
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace


def _assert_no_ordinary_memory_facts(records, documents):
    from backend.memory_app.original_sources import source_store

    for collection in ('workspace_items', 'documents', 'document_revisions',
                       'document_markdown', 'recognition_experiences',
                       'recognition_candidates', 'recognitions'):
        assert tuple(records.list(collection)) == (), collection
    assert tuple(documents.list(include_archived=True)) == ()
    # 使用真实原件主人，避免把外发查询门面中的空发布列表当成原件为空。
    assert tuple(source_store(records).list_including_deleted('sources')) == ()


def _assert_no_route_auxiliary_turns(records):
    assert tuple(records.list('v2_route_turn_keys')) == ()
    assert tuple(records.list('v2_memory_turn_keys')) == ()
    # 路径规则来自 MemoryTurn.store_for；只读实际内核库，不创建替代存储。
    kernel_root = records.database_path.parent
    if records.database_path.name == 'recognition.sqlite3':
        kernel_root = kernel_root / '.rebuild-data'
    kernel_path = kernel_root / 'ai-turns.sqlite3'
    assert kernel_path.is_file()
    with closing(sqlite3.connect(kernel_path.as_uri() + '?mode=ro', uri=True)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_turns WHERE turn_id LIKE 'route-%'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_turns WHERE session_id LIKE 'aux-%'"
        ).fetchone()[0] == 0


def test_full_factory_auto_reminder_keeps_raw_fact_and_bypasses_route_and_intake(tmp_path, monkeypatch):
    """原样观察真实 route 调用；零外部调用与零辅助记录分别断言。"""
    from fastapi.testclient import TestClient
    from backend.memory_app.app import create_app
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    from backend.memory_app.v2.policies import ACTIVE, override
    from backend.memory_app.v2.reminders import ReminderService
    from backend.memory_app.v2.route import RouteService
    from backend.security.secrets import InMemorySecretStore

    root = tmp_path / 'factory'
    (root / 'config').mkdir(parents=True)
    (root / 'config' / 'settings.toml').write_bytes(
        (Path(__file__).parents[3] / 'config' / 'settings.toml.example').read_bytes())
    records, _ = resolve_recognition_document_store(root)
    raw = '#提醒合同/健康\r\n提醒我明天下午三点喝水\r\n请带蓝色水杯\r\n'
    wire_calls, route_calls = [], []

    def provider(**request):
        wire_calls.append({'messages': deepcopy(request['messages'])})
        # 错误路径仍返回合法外部路由协议响应，不固定或替换任何领域返回值。
        content = json.dumps({'parts': [{'intent': 'remember', 'span': raw}]}, ensure_ascii=False)
        return {'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}

    original_route = RouteService.route

    @wraps(original_route)
    def observed_route(owner, *args, **kwargs):
        route_calls.append(1)
        return original_route(owner, *args, **kwargs)

    monkeypatch.setattr(RouteService, 'route', observed_route)
    models = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=provider)
    models.update('generation', {'base_url': 'https://example.test/v1', 'model': 'test-model',
        'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})
    reference = datetime(2026, 10, 8, tzinfo=timezone.utc)
    active_before = dict(ACTIVE)
    try:
        with override(remind='@1', nudge='@2'):
            app = create_app(runtime_root=root, model_configuration=models)
            reminder_owner = getattr(app.state, 'memory_reminders', None)
            if reminder_owner is not None:
                # 只注入实际装配服务的公开时钟，不手动补建服务掩盖装配缺口。
                monkeypatch.setattr(reminder_owner, 'now', lambda: reference)
            with TestClient(app, raise_server_exceptions=False) as http:
                domains = app.state.workspace_domains
                _assert_no_ordinary_memory_facts(records, domains.query.documents)
                project_response = http.post('/api/v2/projects', json={'name': '提醒合同'})
                assert project_response.status_code == 200
                project = project_response.json()
                scene_response = http.patch('/api/v2/projects/' + project['id'],
                    json={'scenes': ['健康'], 'expected_revision': project['revision']})
                assert scene_response.status_code == 200
                assert scene_response.json()['scenes'] == ['健康']
                assert wire_calls == [] and route_calls == []
                body = {'project_id': project['id'], 'text': raw, 'intent': 'auto'}
                headers = {'accept': 'application/json', 'idempotency-key': 'reminder-raw-once'}
                response = http.post('/api/v2/workbench/turns', json=body, headers=headers)
                assert route_calls == [], 'auto reminder executed the real RouteService.route'
                assert response.status_code == 200, response.text
                result = response.json()
                turn = result['turn']
                assert turn['intent'] == 'remember'
                assert isinstance(reminder_owner, ReminderService)
                assert reminder_owner.records.database_path == records.database_path
                expected_day = reference.astimezone(reminder_owner.local_timezone).date() + timedelta(days=1)
                expected_at = datetime.combine(expected_day, time(15), reminder_owner.local_timezone)
                row = records.read('v2_reminders', turn['id'])
                assert row is not None
                assert set(row.payload) == {'project_id', 'scene', 'at', 'text', 'turn_id', 'state'}
                assert row.payload == {'project_id': project['id'], 'scene': '健康',
                    'at': expected_at.astimezone(timezone.utc).isoformat(), 'text': raw,
                    'turn_id': turn['id'], 'state': 'active'}
                assert row.revision == 1
                persisted = records.read('v2_turns', turn['id'])
                assert persisted is not None and persisted.payload['project_id'] == project['id']
                assert persisted.payload['thread_id'] == result['thread_id'] == turn['thread_id']
                assert persisted.payload['intent'] == 'remember'
                assert persisted.payload['item_id'] is None
                assert turn['receipt']['remember']['item_id'] is None
                assert persisted.payload['receipt'] == turn['receipt']
                assert records.read('v2_threads', result['thread_id']).payload['project_id'] == project['id']
                request_row = records.read('v2_turn_requests', headers['idempotency-key'])
                assert request_row.payload['state'] == 'completed'
                assert request_row.payload['body'] == body
                assert request_row.payload['result'] == result
                _assert_no_ordinary_memory_facts(records, domains.query.documents)
                _assert_no_route_auxiliary_turns(records)
                assert tuple(records.list('workspace_ask_receipts')) == ()
                assert tuple(records.list('v2_workbench_item_states')) == ()
                assert wire_calls == []
                replay = http.post('/api/v2/workbench/turns', json=body, headers=headers)
                assert replay.status_code == 200 and replay.json() == result
                replay_row = records.read('v2_reminders', turn['id'])
                assert replay_row.payload == row.payload and replay_row.revision == row.revision
                assert len(records.list('v2_reminders')) == len(records.list('v2_turns')) == 1
                _assert_no_ordinary_memory_facts(records, domains.query.documents)
                _assert_no_route_auxiliary_turns(records)
                assert tuple(records.list('workspace_ask_receipts')) == ()
                assert wire_calls == [] and route_calls == []
                assert app.state.workbench_turn_execution.tasks == {}
                assert not app.state.workbench_tasks
    finally:
        assert ACTIVE == active_before


@contextmanager
def _factory_reminder_controls(tmp_path, monkeypatch, *, selected=True):
    """负控同样使用完整工厂，只合成 provider 协议回复和公开时钟。"""
    from fastapi.testclient import TestClient
    from backend.memory_app.app import create_app
    from backend.memory_app.model_config import ModelConfiguration
    from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    from backend.memory_app.v2.policies import ACTIVE, override
    from backend.memory_app.v2.reminders import ReminderService
    from backend.memory_app.v2.route import RouteService
    from backend.security.secrets import InMemorySecretStore

    root = tmp_path / 'control-factory'
    (root / 'config').mkdir(parents=True)
    (root / 'config' / 'settings.toml').write_bytes(
        (Path(__file__).parents[3] / 'config' / 'settings.toml.example').read_bytes())
    records, _ = resolve_recognition_document_store(root)
    wire_calls, route_calls = [], []

    def provider(**request):
        messages = request['messages']
        wire_calls.append({'messages': deepcopy(messages)})
        system = '\n'.join(message['content'] for message in messages if message['role'] == 'system')
        if 'span必须逐字截取原文' in system:
            inputs = json.loads(messages[-1]['content'])
            value = {'parts': [{'intent': 'remember', 'span': inputs['original_text']}]}
        elif '"insights"' in system:
            value = {'insights': []}
            inputs = json.loads(messages[-1]['content'])
            if isinstance(inputs.get('neighbors'), list) and isinstance(inputs.get('projects'), list):
                # 按实际比较协议返回完整空结果，不替换认识生成服务。
                value['supports'] = []
        elif 'title,summary,topics,facts,todos' in system:
            value = {'title': '合成整理稿', 'summary': '合成摘要', 'topics': [], 'facts': [],
                     'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}
        else:
            raise AssertionError('unexpected_synthetic_provider_contract')
        return {'choices': [{'message': {'content': json.dumps(value, ensure_ascii=False)},
                             'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}

    async def async_provider(**request):
        # 异步外部边界复用同一协议，不改变真实 Gateway 或领域执行。
        return provider(**request)

    original_route = RouteService.route

    @wraps(original_route)
    def observed_route(owner, *args, **kwargs):
        route_calls.append(1)
        return original_route(owner, *args, **kwargs)

    monkeypatch.setattr(RouteService, 'route', observed_route)
    models = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=provider,
        gateway_factory=partial(LiteLLMCompletionGateway, acompletion_fn=async_provider))
    models.update('generation', {'base_url': 'https://example.test/v1', 'model': 'test-model',
        'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})
    active_before = dict(ACTIVE)
    if not selected:
        # 此控制属于候选尚未激活的阶段，不修改全局 ACTIVE 来伪造这个前提。
        assert 'remind' not in ACTIVE
    try:
        with override(remind='@1', nudge='@2') if selected else nullcontext():
            app = create_app(runtime_root=root, model_configuration=models)
            reminders = app.state.memory_reminders
            assert isinstance(reminders, ReminderService)
            monkeypatch.setattr(reminders, 'now', lambda: datetime(2026, 10, 8, tzinfo=timezone.utc))
            with TestClient(app, raise_server_exceptions=False) as http:
                project = _control_project(http, '提醒负控')
                domains = app.state.workspace_domains
                _assert_no_ordinary_memory_facts(records, domains.query.documents)
                assert route_calls == [] and wire_calls == []
                yield SimpleNamespace(app=app, http=http, records=records, domains=domains,
                    project=project, route_calls=route_calls, wire_calls=wire_calls)
    finally:
        assert ACTIVE == active_before


def _control_project(http, name):
    created = http.post('/api/v2/projects', json={'name': name})
    assert created.status_code == 200
    project = created.json()
    patched = http.patch('/api/v2/projects/' + project['id'],
        json={'scenes': ['健康'], 'expected_revision': project['revision']})
    assert patched.status_code == 200
    return patched.json()


def _control_post(env, raw, *, key, intent='auto', project=None, **fields):
    response = env.http.post('/api/v2/workbench/turns',
        json={'project_id': (project or env.project)['id'], 'text': raw, 'intent': intent, **fields},
        headers={'accept': 'application/json', 'idempotency-key': key})
    return response


def _assert_real_ordinary_intake(env, result, expected_source):
    from backend.memory_app.original_sources import source_store
    from tests.memory_app.v2.test_workbench_remember import wait

    # 复用原公开 HTTP 等待函数的原期限，不替换后台整理或认识生成。
    turn = wait(env, result, env.project['id'])
    memory = turn['receipt']['remember']
    assert memory['state'] == 'done'
    assert memory['item_id'] is not None
    owner = env.records.read('workspace_items', memory['item_id'])
    assert owner is not None and owner.payload['project_id'] == env.project['id']
    assert owner.payload['source_text'] == expected_source
    assert owner.payload['status'] == 'confirmed'
    assert memory['document_id'] == owner.payload['document_id']
    document = env.domains.query.documents.read(memory['document_id'])
    assert document['source_refs']
    assert len(env.records.list('workspace_items')) == 1
    assert len(source_store(env.records).list_including_deleted('sources')) == 1
    assert tuple(env.records.list('v2_reminders')) == ()
    assert tuple(env.records.list('recognitions')) == ()
    assert tuple(env.records.list('recognition_candidates')) == ()
    assert tuple(env.records.list('workspace_ask_receipts')) == ()
    assert env.wire_calls
    assert env.app.state.workbench_turn_execution.tasks == {}
    return memory


def test_unparseable_auto_reminder_uses_the_real_ordinary_intake(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        text = '提醒我有空时喝水'
        response = _control_post(env, '#提醒负控/健康\r\n' + text, key='unparseable-auto')
        assert response.status_code == 200, response.text
        assert env.route_calls == []
        _assert_real_ordinary_intake(env, response.json(), text)
        assert tuple(env.records.list('v2_route_turn_keys')) == ()
        kernel_root = env.records.database_path.parent
        if env.records.database_path.name == 'recognition.sqlite3':
            kernel_root = kernel_root / '.rebuild-data'
        kernel_path = kernel_root / 'ai-turns.sqlite3'
        assert kernel_path.is_file()
        with closing(sqlite3.connect(kernel_path.as_uri() + '?mode=ro', uri=True)) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM ai_turns WHERE turn_id LIKE 'route-%'"
            ).fetchone()[0] == 0


def test_auto_date_without_reminder_prefix_uses_the_real_ordinary_intake(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        text = '明天下午三点喝水'
        response = _control_post(env, '#提醒负控/健康\r\n' + text, key='ordinary-auto')
        assert response.status_code == 200, response.text
        assert env.route_calls == [1]
        _assert_real_ordinary_intake(env, response.json(), text)


def test_explicit_remember_preserves_the_real_ordinary_intake(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        text = '明确记住的原始资料'
        response = _control_post(env, '#提醒负控/健康\r\n' + text, key='explicit-remember', intent='remember')
        assert response.status_code == 200, response.text
        assert env.route_calls == []
        _assert_real_ordinary_intake(env, response.json(), text)


def test_auto_reminder_with_uploaded_item_reuses_the_real_intake_item(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        source = '原始上传资料的正文不可改写'
        uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': env.project['id']},
            files={'file': ('reminder.txt', source.encode('utf-8'), 'text/plain')})
        assert uploaded.status_code == 200, uploaded.text
        item = uploaded.json()
        assert env.records.read('workspace_items', item['id']) is not None
        response = _control_post(env, '#提醒负控/健康\r\n提醒我明天下午三点喝水',
            key='uploaded-auto', item_id=item['id'])
        assert response.status_code == 200, response.text
        assert env.route_calls == [1]
        memory = _assert_real_ordinary_intake(env, response.json(), source)
        assert memory['item_id'] == item['id']


def test_inactive_remind_candidate_keeps_auto_on_the_original_intake_path(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch, selected=False) as env:
        text = '提醒我明天下午三点喝水'
        response = _control_post(env, '#提醒负控/健康\r\n' + text, key='inactive-auto')
        assert response.status_code == 200, response.text
        assert env.route_calls == [1]
        _assert_real_ordinary_intake(env, response.json(), text)


def _first_reminder(env):
    raw = '#提醒负控/健康\r\n提醒我明天下午三点喝水\r\n原话必须保持\r\n'
    response = _control_post(env, raw, key='first-reminder')
    assert response.status_code == 200, response.text
    row = env.records.read('v2_reminders', response.json()['turn']['id'])
    assert row is not None and row.payload['text'] == raw and row.revision == 1
    assert env.route_calls == [] and env.wire_calls == []
    return response.json(), raw


def _control_fact_snapshot(env):
    collections = ('v2_reminders', 'v2_turns', 'v2_threads', 'workspace_items', 'documents',
                   'document_revisions', 'document_markdown', 'recognitions',
                   'recognition_candidates', 'recognition_experiences')
    return {name: sorted((row.object_id, row.revision, deepcopy(row.payload))
                        for row in env.records.list(name)) for name in collections}


def _assert_rejected_request_keeps_reminder_facts(env, before, original_request):
    assert _control_fact_snapshot(env) == before
    current = env.records.read('v2_turn_requests', 'first-reminder')
    assert (current.revision, current.payload) == (original_request.revision, original_request.payload)
    _assert_no_ordinary_memory_facts(env.records, env.domains.query.documents)
    _assert_no_route_auxiliary_turns(env.records)
    assert tuple(env.records.list('workspace_ask_receipts')) == ()
    assert env.route_calls == [] and env.wire_calls == []


def test_same_reminder_key_with_changed_raw_is_rejected_without_fact_changes(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        _, raw = _first_reminder(env)
        before = _control_fact_snapshot(env)
        original_request = env.records.read('v2_turn_requests', 'first-reminder')
        response = _control_post(env, raw + '改变原话', key='first-reminder')
        assert response.status_code == 409
        assert response.json()['detail'] == 'idempotency_key_conflict'
        _assert_rejected_request_keeps_reminder_facts(env, before, original_request)


def test_reminder_cross_project_thread_is_rejected_without_fact_changes(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        result, _ = _first_reminder(env)
        other = _control_project(env.http, '另一提醒项目')
        before = _control_fact_snapshot(env)
        original_request = env.records.read('v2_turn_requests', 'first-reminder')
        response = _control_post(env, '#另一提醒项目/健康\r\n提醒我明天下午三点喝水',
            key='cross-project-reminder', project=other, thread_id=result['thread_id'])
        assert response.status_code == 404
        assert response.json()['detail'] == 'workbench_not_found'
        _assert_rejected_request_keeps_reminder_facts(env, before, original_request)


def test_reminder_unknown_tag_is_rejected_without_fact_changes(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        _first_reminder(env)
        before = _control_fact_snapshot(env)
        original_request = env.records.read('v2_turn_requests', 'first-reminder')
        response = _control_post(env, '#未登记提醒项目/健康\r\n提醒我明天下午三点喝水', key='unknown-tag-reminder')
        assert response.status_code == 404
        assert response.json()['detail'] == 'project_tag_not_found'
        _assert_rejected_request_keeps_reminder_facts(env, before, original_request)


def _assert_selected_reminder_entry_contract(env, *, explicit):
    """两种公开意图入口分别验证完整事实、归属、幂等与零外发。"""
    from backend.memory_app.v2.reminders import ReminderService

    raw = '#提醒负控/健康\r\n提醒我明天下午三点喝水\r\n请带蓝色水杯\r\n'
    body = {'project_id': env.project['id'], 'text': raw}
    if explicit:
        body['intent'] = 'remember'
    else:
        # 省略公开字段，不能以会被原输入验证拒绝的 null 代替。
        assert 'intent' not in body
    headers = {'accept': 'application/json', 'idempotency-key': 'selected-reminder-once'}
    assert env.project['scenes'] == ['健康']
    assert env.route_calls == [] and env.wire_calls == []
    response = env.http.post('/api/v2/workbench/turns', json=body, headers=headers)
    assert env.route_calls == [], 'selected reminder executed the real RouteService.route'
    assert response.status_code == 200, response.text
    result = response.json()
    turn = result['turn']
    assert turn['intent'] == 'remember'
    reminder_owner = env.app.state.memory_reminders
    assert isinstance(reminder_owner, ReminderService)
    assert reminder_owner.records.database_path == env.records.database_path
    reference = datetime(2026, 10, 8, tzinfo=timezone.utc)
    expected_day = reference.astimezone(reminder_owner.local_timezone).date() + timedelta(days=1)
    expected_at = datetime.combine(expected_day, time(15), reminder_owner.local_timezone)
    row = env.records.read('v2_reminders', turn['id'])
    assert row is not None
    assert set(row.payload) == {'project_id', 'scene', 'at', 'text', 'turn_id', 'state'}
    assert row.payload == {'project_id': env.project['id'], 'scene': '健康',
        'at': expected_at.astimezone(timezone.utc).isoformat(), 'text': raw,
        'turn_id': turn['id'], 'state': 'active'}
    assert row.revision == 1
    persisted = env.records.read('v2_turns', turn['id'])
    assert persisted is not None and persisted.payload['project_id'] == env.project['id']
    assert persisted.payload['thread_id'] == result['thread_id'] == turn['thread_id']
    assert persisted.payload['intent'] == 'remember'
    assert persisted.payload['item_id'] is None
    assert turn['receipt']['remember']['item_id'] is None
    assert persisted.payload['receipt'] == turn['receipt']
    assert env.records.read('v2_threads', result['thread_id']).payload['project_id'] == env.project['id']
    request_row = env.records.read('v2_turn_requests', headers['idempotency-key'])
    assert request_row is not None
    assert request_row.payload['state'] == 'completed'
    assert request_row.payload['body'] == body
    assert request_row.payload['result'] == result
    _assert_no_ordinary_memory_facts(env.records, env.domains.query.documents)
    _assert_no_route_auxiliary_turns(env.records)
    assert tuple(env.records.list('workspace_ask_receipts')) == ()
    assert tuple(env.records.list('v2_workbench_item_states')) == ()
    assert env.wire_calls == []
    replay = env.http.post('/api/v2/workbench/turns', json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == result
    replay_row = env.records.read('v2_reminders', turn['id'])
    assert replay_row.payload == row.payload and replay_row.revision == row.revision
    assert len(env.records.list('v2_reminders')) == len(env.records.list('v2_turns')) == 1
    _assert_no_ordinary_memory_facts(env.records, env.domains.query.documents)
    _assert_no_route_auxiliary_turns(env.records)
    assert tuple(env.records.list('workspace_ask_receipts')) == ()
    assert env.wire_calls == [] and env.route_calls == []
    assert env.app.state.workbench_turn_execution.tasks == {}
    assert not env.app.state.workbench_tasks


def test_explicit_remember_parsed_reminder_keeps_full_fact_without_intake(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        _assert_selected_reminder_entry_contract(env, explicit=True)


def test_omitted_intent_parsed_reminder_keeps_full_fact_without_intake(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        _assert_selected_reminder_entry_contract(env, explicit=False)
