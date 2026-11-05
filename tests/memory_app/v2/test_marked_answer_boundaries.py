import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sqlite3
from threading import Thread

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.storage_authority import resolve_recognition_document_store
from backend.memory_app.v2.policies import override
from backend.recognition import RecognitionError, WorkScope
from backend.security.secrets import InMemorySecretStore
from backend.shared.deployment import DeploymentLayout
from backend.shared.job_runtime import build_rebuild_job_repository
from core.ingestion_core import ObjectStoreSourceRegistrar
from core.product_core.workbench_source_intake import CaptureWorkbenchTextSource


@pytest.fixture
def actual(tmp_path):
    requests = []

    class DeniedEndpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get('Content-Length', '0'))
            self.rfile.read(size)
            requests.append(self.path)
            # 真实HTTP拒绝用于核对运输失败，端点不返回任何模型成果。
            self.send_response(401)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"local test endpoint denied","type":"authentication_error"}}')

        def log_message(self, *args):
            pass

    with ExitStack() as resources:
        (tmp_path / 'config').mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', tmp_path / 'config/settings.toml')
        values = {'CHRIPTMAS_APP_ROOT': str(tmp_path), 'CHRIPTMAS_DEPLOY': 'server',
            'LITELLM_LOCAL_MODEL_COST_MAP': 'true', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
        prior = {key: os.environ.get(key) for key in values}
        def restore_environment():
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        resources.callback(restore_environment)
        os.environ.update(values)
        from backend.memory_app.app import create_app
        server = ThreadingHTTPServer(('127.0.0.1', 0), DeniedEndpoint)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        resources.callback(worker.join, 2)
        resources.callback(server.server_close)
        resources.callback(server.shutdown)
        records, namespace = resolve_recognition_document_store(tmp_path)
        models = ModelConfiguration(records, tmp_path, InMemorySecretStore())
        models.update('generation', {'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'model': 'local-control',
            'api_key': 'synthetic-only', 'allow_remote': False, 'enabled': True, 'expected_revision': 0})
        app = create_app(runtime_root=tmp_path, model_configuration=models,
            deployment_layout=DeploymentLayout('desktop', tmp_path))
        query = app.state.workspace_domains.query
        captured = CaptureWorkbenchTextSource(source_registrar=ObjectStoreSourceRegistrar(
            query.source_store, namespace_id=namespace, project_id='alpha', created_at=datetime.now(timezone.utc).isoformat()),
            job_repository=build_rebuild_job_repository(tmp_path, query.source_store), namespace_id=namespace,
            now=datetime.now(timezone.utc).isoformat()).execute(title='Maya painting record',
                content='Maya painted a self-portrait last week.')
        query.retrieval_index.bootstrap()
        def shutdown_runner():
            runner = getattr(app.state, 'ai_turn_runner', None)
            if runner is not None:
                runner.shutdown()
        resources.callback(shutdown_runner)
        yield app, query, models, captured.source_id, requests


@pytest.mark.parametrize('change', ['source_revision', 'private_source', 'model_revision', 'budget'])
def test_fresh_owner_qualification_still_rejects_before_publication(actual, change):
    app, query, models, source_id, requests = actual
    with override(compose='@37', retrieve='@1'):
        plan = query.prepare_ask('alpha', 'What did Maya paint?')
    assert plan['policy_versions']['compose'] == '@37' and plan['chosen']
    query.validate_ask_plan(plan)
    if change == 'source_revision':
        current = query.source_store.read('sources', source_id)
        query.source_store.write('sources', source_id, {**current, 'title': 'Later revision'},
            expected_revision=query.source_store.revision('sources', source_id))
    elif change == 'private_source':
        SourceEgressService(query.records).set_policy(WorkScope('local-user', 'alpha'), 'original_source',
            source_id, query.source_store.revision('sources', source_id), 0, [])
    elif change == 'model_revision':
        config = models.public_for_purpose('generation')
        models.update('generation', {'base_url': config['base_url'], 'model': 'later-control',
            'allow_remote': False, 'enabled': True, 'expected_revision': config['revision']})
    else:
        plan = deepcopy(plan)
        plan['answer_output_budget']['reserve'] -= 1
    with pytest.raises((RecognitionError, ModelConfigurationError, ValueError)):
        query.validate_ask_plan(plan)
    assert requests == []


def test_real_failed_wire_and_failed_native_turn_are_not_published_as_unverified(actual):
    app, query, models, source_id, requests = actual
    with override(compose='@37', retrieve='@1'):
        with TestClient(app) as client:
            response = client.post('/api/v2/workbench/turns', headers={'Idempotency-Key': 'r6-native-failure'},
                json={'project_id': 'alpha', 'text': 'What did Maya paint?', 'intent': 'ask'})
    assert response.status_code == 502 and response.json() == {'detail': 'answer_generation_failed'}
    assert requests
    store = app.state.ai_turn_store
    with sqlite3.connect(query.answer_turns.root / '.rebuild-data/ai-turns.sqlite3') as connection:
        frozen = [json.loads(row[0]) for row in connection.execute('SELECT request_json FROM ai_turns')]
    request = next(value for value in frozen if value['desired_outcome'] == 'project.answer')
    identity = request['turn_id']
    assert request['policy_versions']['compose'] == '@37'
    events = tuple(store.events_after(identity))
    attempts = [store.get(event['data']['receipt_ref']) for event in events if event['type'] == 'model.attempt.terminal']
    assert attempts and all(value['status'] != 'succeeded' for value in attempts)
    assert any(event['type'] == 'model.failed' for event in events)
    assert not any(event['type'] == 'model.completed' for event in events)
    assert app.state.ai_runtime.receipt_for(identity).status == 'failed'
    assert store.get_immutable_payload(identity, 'product-answer-result-v2') is None
    assert store.get_immutable_payload(identity, 'answer-model-result-answer') is None
    for row in query.records.list('workspace_ask_receipts'):
        assert row.payload['status'] == 'failed' and 'unverified_claims' not in row.payload

    # 即便事后插入公开结果，原失败终态仍拒绝重复提交，不产生新的运输。
    store.get_or_create_immutable_payload(identity, 'product-answer-result-v2',
        {'answer': 'Tampered public answer【未核实】', 'sources': [], 'unverified_claims': 1})
    count = len(requests)
    async def forbidden_operation():
        raise AssertionError('failed turn cannot restart')
    with pytest.raises(HTTPException) as raised:
        asyncio.run(query.answer_turns.run(turn_id=identity, project='alpha', question=request['input']['text'],
            operation=forbidden_operation, policy_versions=request['policy_versions']))
    assert raised.value.status_code == 409 and raised.value.detail == 'turn_interrupted'
    assert len(requests) == count
