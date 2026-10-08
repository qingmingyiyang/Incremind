"""MCP 通过完整本机工厂、真实 lifespan 和原 HTTP 认证边界。"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
from threading import Thread
import time
from types import SimpleNamespace

import httpx
from jsonschema import Draft202012Validator
import pytest
import uvicorn

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.storage_authority import resolve_recognition_document_store
from backend.memory_app.v2.external_agent_settings import (
    external_agent_settings, replace_external_agent_settings,
)
from backend.memory_app.v2.external_context import DELIVERIES, USES
from backend.recognition import WorkScope
from backend.security.secrets import InMemorySecretStore
from tests.memory_app.v2.test_mcp_registered_sdk_consumers import (
    QUERY, SOURCE, assert_public_completed, registered_sdk_session, sdk_success,
)
from tests.memory_app.v2.test_mcp_sdk_backend import TOOLS


ROOT = Path(__file__).resolve().parents[3]
DESKTOP_ENVIRONMENT = (
    'CHRIPTMAS_DESKTOP_SESSION_MODE', 'CHRIPTMAS_DESKTOP_SESSION_SECRET',
    'CHRIPTMAS_DESKTOP_INSTANCE_ID', 'CHRIPTMAS_DESKTOP_NONCE',
    'CHRIPTMAS_DESKTOP_PROTOCOL_VERSION', 'CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT',
    'CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN', 'CHRIPTMAS_WORKER_SECRET',
    'CHRIPTMAS_WORKER_INSTANCE_ID', 'CHRIPTMAS_WORKER_PORT',
    'CHRIPTMAS_RUNTIME_ROOT_VERSION', 'CHRIPTMAS_RUNTIME_ROOT_REVISION',
    'CHRIPTMAS_RUNTIME_VAULT_ROOT', 'CHRIPTMAS_RUNTIME_MODEL_ROOT', 'CHRIPTMAS_RUNTIME_MEDIA_ROOT',
)


@contextmanager
def running_factory(app):
    # 原工厂的全部 startup/shutdown 都由真实 ASGI lifespan 执行。
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', int(os.environ.get('CHRIPTMAS_MCP_TEST_PORT', '0'))))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
            lifespan='on', access_log=False, log_level='error'))
        errors = []

        def run():
            try:
                server.run(sockets=[listener])
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=run, name='mcp-full-factory', daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 45
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            assert server.started and thread.is_alive() and not errors, errors
            assert not server.lifespan.startup_failed
            yield f'http://127.0.0.1:{port}'
        finally:
            server.should_exit = True
            thread.join(timeout=20)
            assert not thread.is_alive() and not errors, errors
            assert not server.lifespan.shutdown_failed and not server.lifespan.error_occurred


@pytest.fixture
def full_factory(tmp_path, monkeypatch):
    # 合成配置独立于安装目录；不继承本机桌面会话或 worker 凭据。
    for name in DESKTOP_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'desktop')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.toml').write_bytes(
        (ROOT / 'config/settings.toml.example').read_bytes())
    records, namespace = resolve_recognition_document_store(tmp_path)
    assert namespace == 'default'
    native_calls = []

    def completion(**request):
        # 唯一隔离的是外部 provider wire，原工厂与领域服务保持真实。
        assert request['messages'][-1]['content'] == SOURCE
        native_calls.append(request['model'])
        draft = {'title': '礼物预算材料', 'summary': SOURCE, 'topics': [QUERY],
            'facts': [{'text': SOURCE, 'evidence': {'quote': SOURCE}}],
            'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}
        return {'choices': [{'message': {'content': json.dumps(draft, ensure_ascii=False)},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 30}}

    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=completion)
    models.update('generation', {'base_url': 'https://provider.invalid/v1', 'model': 'material-writer',
        'api_key': 'synthetic-local-only', 'allow_remote': True, 'expected_revision': 0})
    current = external_agent_settings(records)
    replace_external_agent_settings(records,
        {key: value for key, value in current.items() if key != 'revision'}
        | {'allow_remote': True, 'include_profile': False}, expected_revision=current['revision'])

    from backend.memory_app.app import create_app
    app = create_app(runtime_root=tmp_path, model_configuration=models)
    assert Path(app.state.container.root_dir).resolve() == tmp_path.resolve()
    assert app.state.capability_package_catalog is not None
    actual = SimpleNamespace(root=tmp_path, app=app, records=app.state.recognition_records,
        documents=app.state.recognition_documents, service=app.state.recognition_service,
        models=models, domains=app.state.workspace_domains, context=app.state.external_context,
        native_calls=native_calls)
    assert actual.records.database_path == records.database_path
    with running_factory(app) as endpoint:
        actual.endpoint = endpoint
        actual.runtime, actual.turns, actual.runner = (
            app.state.ai_runtime, app.state.ai_turn_store, app.state.ai_turn_runner)
        actual.execute = lambda turn: actual.context.execute(
            turn, runtime=actual.runtime, runner=actual.runner)
        assert actual.context.runtime is actual.runtime and actual.context.turns is actual.turns
        assert actual.domains.query.answer_turns._runtime() is actual.runtime
        yield actual
        assert actual.runner.active_turn_ids == ()
    assert actual.runner.shutdown(timeout_seconds=5) == ()


def confirmed_material(actual):
    domains = actual.domains
    staged = asyncio.run(domains.intake.add_text({'project_id': 'alpha', 'text': SOURCE}))
    ready = asyncio.run(domains.intake.process(staged['id'], {'project_id': 'alpha'}))
    assert staged['status'] == 'staged' and ready['status'] == 'ready'
    assert len(actual.native_calls) == 1
    confirmed = asyncio.run(domains.review.confirm(staged['id'], {
        'project_id': 'alpha', 'expected_revision': ready['revision']}))
    assert confirmed['status'] == 'confirmed'
    extracted = asyncio.run(domains.review.recognition(staged['id'], {'project_id': 'alpha'}))
    candidate = actual.records.read('recognition_candidates', extracted['candidate_id'])
    assert candidate.payload['state'] == 'pending'
    scope = WorkScope('local-user', 'alpha')
    # 本地人工确认仅准备素材；MCP 调用不发布认识。
    edited = actual.service.edit_candidate(scope=scope, candidate_id=candidate.object_id,
        expected_revision=candidate.revision, content=candidate.payload['content'],
        conditions=[QUERY], editor='local-human-test-reviewer')
    actual.method = actual.service.publish(scope=scope, candidate_id=edited.id,
        expected_revision=edited.revision, reviewer='local-human-test-reviewer')
    actual.material_item = staged['id']


@pytest.mark.parametrize(('name', 'client'), [('Claude Code', 'claude'), ('Codex', 'codex')])
def test_full_factory_lifespan_stdio_seven_tools_and_snapshot(full_factory, name, client):
    actual, outputs = full_factory, {}
    confirmed_material(actual)

    async def operate():
        async with registered_sdk_session(actual, actual.endpoint, name=name, suffix='factory') as session:
            listed = await session.list_tools()
            assert [tool.name for tool in listed.tools] == TOOLS
            for index, tool in enumerate(listed.tools):
                assert tool.annotations.readOnlyHint is (index < 4)
                Draft202012Validator.check_schema(tool.inputSchema)
                Draft202012Validator.check_schema(tool.outputSchema)
            outputs['projects'] = await sdk_success(session, 'projects', {})
            outputs['recall'] = await sdk_success(session, 'recall', {'query': QUERY, 'project': 'alpha'})
            outputs['methods'] = await sdk_success(session, 'methods', {'situation': QUERY, 'project': 'alpha'})
            parent = outputs['recall']
            selected = next(row for row in parent['result']['entries'] if row['object_id'] == actual.method.id)
            outputs['read'] = await sdk_success(session, 'read', {'id': {
                'turn_id': parent['turn_id'], 'id': selected['id']}, 'window': {'start': 0, 'end': 8}})
            child = outputs['read']
            original = next(row for row in child['result']['entries'] if row['object_id'] == actual.material_item)
            assert original['layer'] == 'L0' and original['excerpt'] == SOURCE[:8]
            outputs['remember'] = await sdk_success(session, 'remember', {
                'text': '完整工厂投入\nsk-' + 'A' * 24, 'project': 'alpha'})
            outputs['propose_insight'] = await sdk_success(session, 'propose_insight', {
                'text': '完整工厂待确认认识', 'conditions': [QUERY], 'project': 'alpha',
                'evidence_ids': [{'turn_id': child['turn_id'], 'id': original['id']}]})
            outputs['report_use'] = await sdk_success(session, 'report_use', {
                'turn_id': child['turn_id'], 'ids': [original['id']]})
            for tool in listed.tools:
                Draft202012Validator(tool.outputSchema).validate(outputs[tool.name])

    asyncio.run(operate())
    for tool in ('projects', 'recall', 'methods', 'read'):
        turn = outputs[tool]['turn_id']
        frozen = actual.turns.get_request(turn)
        assert frozen['capability_request']['arguments']['client'] == client
        assert_public_completed(actual, turn, frozen, outputs[tool]['result'])
    remembered = actual.records.read('workspace_items', outputs['remember']['result']['item_id'])
    assert remembered.revision == 1 and remembered.payload['status'] == 'staged'
    assert remembered.payload['source_text'] == '完整工厂投入\n[REDACTED_SECRET]'
    assert outputs['remember']['result']['verified'] is False
    assert remembered.payload['draft'] is None and remembered.payload['document_id'] is None
    candidate = actual.records.read('recognition_candidates', outputs['propose_insight']['result']['candidate_id'])
    assert candidate.payload['state'] == 'pending' and candidate.payload['conditions'] == [QUERY]
    assert len(actual.records.list('recognitions')) == 1
    for tool in ('remember', 'propose_insight'):
        receipt = actual.records.read('v2_external_agent_intakes', outputs[tool]['result']['receipt_id'])
        assert receipt.payload['client'] == client and receipt.payload['tool'] == tool
        assert receipt.payload['project_id'] == 'alpha'
    assert actual.records.read(USES, outputs['read']['turn_id']).payload['ids'] == outputs['report_use']['result']['ids']
    assert len(actual.records.list(DELIVERIES)) == 4

    with httpx.Client(base_url=actual.endpoint, trust_env=False, timeout=150) as http:
        metadata = http.get('/api/v2/settings/external-agent/connection?project_id=alpha')
        assert metadata.status_code == 200, metadata.text
        assert all(actual.endpoint in command for command in metadata.json()['commands'].values())
        snapshot = http.post('/api/v2/settings/external-agent/snapshot', json={
            'client': client, 'project_id': 'alpha', 'budget': 3000})
        assert snapshot.status_code == 200, snapshot.text
    value = snapshot.json()
    assert value['version'] == 'external-snapshot@1' and value['client'] == client
    assert value['text'].startswith('<!-- chriptmas-memory:external-snapshot@1:begin -->\n')
    assert value['text'].endswith('\n<!-- chriptmas-memory:external-snapshot@1:end -->')
    assert value['generated_at'] in value['text'] and value['result']['text'] in value['text']
    assert actual.method.id in {row['object_id'] for row in value['result']['entries']}
    assert_public_completed(actual, value['turn_id'], actual.turns.get_request(value['turn_id']), value['result'])
    assert len(actual.records.list(DELIVERIES)) == 5 and len(actual.native_calls) == 1
    with sqlite3.connect((actual.root / '.rebuild-data/ai-turns.sqlite3').as_uri() + '?mode=ro', uri=True) as database:
        database.execute('PRAGMA query_only=ON')
        assert database.execute('SELECT status,terminal_status FROM ai_model_attempt_reservations').fetchall() == [
            ('terminal', 'succeeded')]


def enable_synthetic_desktop_auth(monkeypatch, endpoint):
    values = {
        'CHRIPTMAS_DESKTOP_SESSION_MODE': 'desktop_production',
        'CHRIPTMAS_DESKTOP_SESSION_SECRET': 'S' * 43,
        'CHRIPTMAS_DESKTOP_INSTANCE_ID': 'synthetic-mcp-session',
        'CHRIPTMAS_DESKTOP_NONCE': 'N' * 43,
        'CHRIPTMAS_DESKTOP_PROTOCOL_VERSION': 'desktop-loopback/1',
        'CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        'CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN': endpoint,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values['CHRIPTMAS_DESKTOP_SESSION_SECRET']


def test_full_factory_original_origin_and_desktop_auth_reject_without_writes(full_factory, monkeypatch):
    actual = full_factory
    before = actual.records.list_all()
    request = {'client': 'codex', 'arguments': {'text': '无权限不能投入', 'project': 'alpha'}}
    with httpx.Client(base_url=actual.endpoint, trust_env=False, timeout=15) as http:
        rejected = http.post('/api/v2/external-agent/mcp/remember', json=request,
            headers={'Origin': 'https://foreign.invalid'})
        assert rejected.status_code == 403 and rejected.json()['detail'] == 'local_origin_required'
        secret = enable_synthetic_desktop_auth(monkeypatch, actual.endpoint)
        for headers in ({}, {'X-Chriptmas-Desktop-Session': 'invalid-synthetic-session'}):
            rejected = http.post('/api/v2/external-agent/mcp/remember', json=request, headers=headers)
            assert rejected.status_code == 403 and rejected.json()['detail'] == 'desktop_session_unauthorized'
        assert actual.records.list_all() == before
        response = http.post('/api/v2/external-agent/mcp/remember', json=request,
            headers={'X-Chriptmas-Desktop-Session': secret})
        assert response.status_code == 200, response.text
    assert response.json()['result']['state'] == 'staged' and actual.native_calls == []


def test_full_factory_stdio_remains_available_with_desktop_auth(full_factory, monkeypatch):
    actual = full_factory
    enable_synthetic_desktop_auth(monkeypatch, actual.endpoint)

    async def operate():
        async with registered_sdk_session(actual, actual.endpoint, suffix='desktop-auth') as session:
            value = await sdk_success(session, 'projects', {})
            assert value['result']['projects']

    asyncio.run(operate())
