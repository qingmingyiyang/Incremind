"""Actual SDK stdio -> numeric loopback -> original temporary domain/Kernel."""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
from threading import Thread
import time
from uuid import uuid4

from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
import pytest
import uvicorn

from backend.recognition import WorkScope
from core.effect_log import EffectState
from tests.memory_app.v2.test_external_context import env, settings, setup, request, DAY

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ['projects', 'recall', 'methods', 'read', 'remember', 'propose_insight', 'report_use']


@contextmanager
def loopback(app):
    """Serve that exact fixture app; no replacement routes or lifespan startup."""
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
            lifespan='off', access_log=False, log_level='error'))
        errors = []

        def run():
            try:
                server.run(sockets=[listener])
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and not errors and time.monotonic() < deadline:
                time.sleep(.01)
            assert server.started and not errors, errors
            yield f'http://127.0.0.1:{port}'
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive() and not errors, errors


def originals(env, client):
    settings(env, allow_remote=True)
    scope = WorkScope('local-user', 'alpha')
    experience = env.service.stage_experience(scope=scope, content='合成用户陈述：给朋友挑礼物先问愿望')
    proposal = env.service.propose(scope=scope, content='给朋友挑礼物先问对方愿望',
        conditions=['挑礼物时'], source_experience_ids=[experience])
    method = env.service.publish(scope=scope, candidate_id=proposal.id, expected_revision=proposal.revision,
        reviewer='local-user')
    assert env.service.get_recognition(scope=scope, recognition_id=method.id).authorized
    source = {'id': 'sdk-json-original', 'project_id': 'alpha', 'title': '合成 SDK 原件',
        'metadata': {'content_snapshot': '挑礼物前先问愿望。😀'}}
    env.domains.query.source_store.write('sources', source['id'], source, expected_revision=0)
    api, runtime, runner = setup(env)
    turn = 'turn-' + uuid4().hex
    api.prepare(turn, request(client=client), [{'type': 'original_source', 'id': source['id'],
        'revision': 1, 'project_id': 'alpha', 'layer': 'L0', 'windows': []}],
        session_id='session-sdk-original', operation_id='op-sdk-original', idempotency_key=turn,
        created_at=DAY.isoformat())
    output = api.execute(turn, runtime=runtime, runner=runner)
    assert output['entries'][0]['id'] == 'M1'
    assert api.delivered_proof(turn, 'M1', client=client)[0]['material']['type'] == 'original_source'
    return api, turn, source, method


@pytest.mark.parametrize(('name', 'client'), [('Claude Code', 'claude'), ('Codex', 'codex')])
def test_real_sdk_calls_all_seven_tools_and_proposes_delivered_json_evidence(env, name, client):
    api, origin, source, method = originals(env, client)
    outputs = {}
    with loopback(env.http.app) as endpoint:
        async def operation():
            parameters = StdioServerParameters(command=sys.executable, args=['-m', 'backend.memory_app.mcp'],
                env={**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'CHRIPTMAS_APP_ROOT': str(env.root),
                    'CHRIPTMAS_MCP_BACKEND_URL': endpoint}, cwd=str(ROOT))
            with (env.root / 'real-sdk-stderr.log').open('w', encoding='utf-8') as errors:
                async with stdio_client(parameters, errlog=errors) as (reader, writer):
                    async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=150),
                        client_info=types.Implementation(name=name, version='integration-1')) as session:
                        initialized = await session.initialize()
                        assert initialized.serverInfo.name == 'chriptmas-memory'
                        listed = await session.list_tools()
                        assert [tool.name for tool in listed.tools] == TOOLS
                        arguments = {'projects': {}, 'recall': {'query': '挑礼物先问愿望', 'project': 'alpha'},
                            'methods': {'situation': '给朋友挑生日礼物', 'project': 'alpha'},
                            'read': {'id': {'turn_id': origin, 'id': 'M1'}, 'window': {'start': 0, 'end': 8}},
                            'remember': {'text': 'SDK 合成待核对原件', 'project': 'alpha'}}
                        for index, tool in enumerate(listed.tools):
                            assert tool.annotations.readOnlyHint is (index < 4)
                            assert 'client' not in tool.inputSchema['properties']
                            if tool.name == 'propose_insight':
                                arguments[tool.name] = {'text': 'SDK 基于交付原件提出的合成认识',
                                    'conditions': ['挑礼物时'], 'project': 'alpha', 'evidence_ids': [
                                        {'turn_id': outputs['read']['turn_id'], 'id': 'M1'}]}
                            elif tool.name == 'report_use':
                                arguments[tool.name] = {'turn_id': outputs['read']['turn_id'], 'ids': ['M1']}
                            result = await session.call_tool(tool.name, arguments[tool.name])
                            assert result.isError is False, (tool.name, result)
                            output = result.structuredContent
                            Draft202012Validator(tool.outputSchema).validate(output)
                            assert json.loads(result.content[0].text) == output
                            outputs[tool.name] = output
        asyncio.run(operation())
    assert set(outputs) == set(TOOLS)
    assert any(row['id'] == 'alpha' for row in outputs['projects']['result']['projects'])
    for tool in ('recall', 'methods'):
        assert method.id in {row['object_id'] for row in outputs[tool]['result']['entries']}
    assert outputs['read']['turn_id'] != origin
    assert outputs['read']['result']['entries'][0]['excerpt'] == source['metadata']['content_snapshot'][:8]
    _, child = api._archive(outputs['read']['turn_id'])
    assert child['mapping']['M1']['material']['type'] == 'original_source'
    assert child['mapping']['M1']['material']['id'] == source['id'] and child['origin']['turn_id'] == origin
    for tool in ('projects', 'recall', 'methods', 'read'):
        turn = outputs[tool]['turn_id']
        assert api.turns.get_request(turn)['capability_request']['arguments']['client'] == client
        events = api.turns.events_after(turn)
        assert events[-1]['type'] == 'turn.completed'
        assert not any(event['type'].startswith('model.') for event in events)
        outcomes = [event for event in events if event['type'] == 'tool.outcome.recorded']
        assert len(outcomes) == 1
        assert api.turns.effect_runner.log.get(outcomes[0]['correlation']['tool_call_id']).state is EffectState.SETTLED_OK
    remembered, proposed = outputs['remember'], outputs['propose_insight']
    assert remembered['turn_id'] is proposed['turn_id'] is None
    for output in (remembered, proposed):
        receipt = env.records.read('v2_external_agent_intakes', output['result']['receipt_id'])
        assert receipt.payload['client'] == client and receipt.payload['project_id'] == 'alpha'
    original = env.records.read('workspace_items', remembered['result']['item_id'])
    assert original.payload['status'] == 'staged' and original.payload['source_text'] == 'SDK 合成待核对原件'
    candidate = env.records.read('recognition_candidates', proposed['result']['candidate_id'])
    assert candidate.payload['state'] == 'pending'
    evidence = env.records.read('recognition_experiences', candidate.payload['source_experience_ids'][0])
    marker = env.records.read('v2_external_input_dependencies', evidence.object_id)
    assert marker.payload['client'] == client and marker.payload['references'][0]['turn_id'] == outputs['read']['turn_id']
    assert marker.payload['references'][0]['id'] == 'M1'
    assert len(env.records.list('recognitions')) == 1
    assert outputs['report_use']['turn_id'] == outputs['read']['turn_id']
    assert outputs['report_use']['result']['ids'] == ['M1']
    assert len(env.records.list('v2_external_agent_deliveries')) == 5
    assert len(env.records.list('v2_external_agent_reservations')) == 5
    assert len(env.records.list('v2_external_agent_write_reservations')) == 2
    assert env.records.list('v2_usage_insight') == env.records.list('v2_usage_document') == ()
    with sqlite3.connect((env.root / '.rebuild-data/ai-turns.sqlite3').as_uri() + '?mode=ro', uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        assert connection.execute('SELECT count(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
        assert not any(json.loads(row[0])['type'].startswith('model.') for row in
            connection.execute('SELECT event_json FROM ai_turn_events'))
    assert env.model.calls == 0
