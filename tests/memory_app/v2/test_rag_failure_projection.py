import os
from pathlib import Path
from shutil import copyfile
import socket
import sqlite3

from fastapi.testclient import TestClient
import pytest


def test_actual_failed_rag_decision_is_recoverable_and_never_publishes_no_match(tmp_path_factory):
    root = tmp_path_factory.mktemp('r').resolve(strict=True)
    assert root.is_absolute()
    config = root / 'config/settings.toml'
    config.parent.mkdir()
    copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', config)
    previous = {key: os.environ.get(key) for key in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY', 'NO_PROXY')}
    os.environ.update(CHRIPTMAS_APP_ROOT=str(root), CHRIPTMAS_DEPLOY='desktop', NO_PROXY='127.0.0.1,localhost')
    try:
        from backend.memory_app.app import create_app
        from backend.memory_app.v2.turn_frames import STREAMS
        from backend.memory_app.v2.privacy import egress_allowed
        from backend.recognition import WorkScope
        from backend.recognition.external_turn_facts import completed_answer_facts
        from openai._streaming import SSEDecoder

        application = create_app(runtime_root=root)
        models, records = application.state.recognition_models, application.state.recognition_records
        async def pause():
            await application.state.memory_daily_jobs.pause_maintenance('rag-failure-isolated-check')
        application.router.on_startup.insert(0, pause)
        with socket.socket() as unavailable:
            unavailable.bind(('127.0.0.1', 0))
            port = unavailable.getsockname()[1]
            models.update('generation', {'expected_revision': models.public()['generation']['revision'],
                'base_url': f'http://127.0.0.1:{port}/v1', 'model': 'gpt-4o-mini',
                'api_key': 'isolated-loopback-unreachable', 'enabled': True, 'allow_remote': True})
            with TestClient(application) as client:
                created = client.post('/api/v2/projects', json={'name': '检索失败真实恢复检查'})
                assert created.status_code == 200
                project = created.json()['id']
                scope = WorkScope('local-user', project)
                service = application.state.recognition_service
                experience = service.stage_experience(scope=scope, content='核对会议日期为十一月十日。',
                    provenance={'kind': 'user_statement', 'actor': 'local-user'})
                candidate = service.propose(scope=scope, content='会议日期为十一月十日。',
                    source_experience_ids=[experience], conditions=['会议日期'])
                published = service.publish(scope=scope, candidate_id=candidate.id,
                    expected_revision=candidate.revision, reviewer='local-user')
                assert published.authorized and published.source_evidence_complete
                body = {'project_id': project, 'intent': 'ask', 'text': '请根据会议资料说明会议日期并列出准备步骤。'}
                headers = {'Idempotency-Key': 'actual-rag-refused-connection'}
                response = client.post('/api/v2/workbench/turns', json=body, headers=headers)
                assert response.status_code == 502, response.text
                assert response.json() == {'detail': 'answer_generation_failed'}
                failed = records.read('v2_turn_requests', headers['Idempotency-Key'])
                assert failed.payload['state'] == 'failed' and failed.payload['status_code'] == 502
                assert failed.payload['error'] == 'answer_generation_failed'
                store = application.state.ai_turn_store
                with sqlite3.connect(Path(store._path).resolve().as_uri() + '?mode=ro', uri=True) as connection:
                    identities = [row[0] for row in connection.execute(
                        "SELECT turn_id FROM ai_turns WHERE json_extract(request_json,'$.scope.project_id')=?", (project,))]
                assert len(identities) == 1
                identity = identities[0]
                events = store.events_after(identity)
                assert events[-1]['type'] == 'turn.failed'
                assert any(row['type'] == 'tool.failed' for row in events)
                assert any(row['type'] == 'model.failed' for row in events)
                assert not any(row['type'] == 'turn.completed' for row in events)
                assert not any(row['type'] == 'model.requested' and row['data'].get('model_call_purpose') == 'primary'
                    for row in events)
                assert store.get_immutable_payload(identity, 'product-answer-result-v2') is None
                assert not records.list('v2_turns')
                stream = records.read(STREAMS, identity)
                assert stream.payload['terminal'] is None
                resumed = client.get('/api/v2/workbench/turns/' + identity + '/stream',
                    params={'project_id': project, 'after': 0})
                assert resumed.status_code == 200
                frames = list(SSEDecoder().iter_bytes([resumed.content]))
                assert [frame.json() for frame in frames if frame.event == 'error'] == [
                    {'code': 'answer_generation_failed'}]
                history_path = '/api/v2/workbench/threads/' + stream.payload['thread_id']
                history = client.get(history_path, params={'project_id': project})
                assert history.status_code == 200
                assert len(history.json()['turns']) == 1
                retained = history.json()['turns'][0]
                assert retained['id'] == identity and retained['user_text'] == body['text']
                assert retained['receipt']['ask']['error'] == 'answer_generation_failed'
                assert retained['receipt']['ask']['no_match'] is False
                assert 'interruption' not in retained['receipt']['ask']
                refreshed = client.get(history_path, params={'project_id': project})
                assert refreshed.status_code == 200 and refreshed.json() == history.json()
                assert store.events_after(identity) == events
                with pytest.raises(ValueError):
                    completed_answer_facts(records, project_id=project, turn_id=identity)
                for event in events:
                    if event['type'] == 'model.attempt.terminal':
                        receipt = store.get(event['data']['receipt_ref'])
                        assert receipt['status'] == 'failed_transport'
                        assert receipt['usage'] is None and receipt['usage_status'] == 'unavailable'
                replay = client.post('/api/v2/workbench/turns', json=body, headers=headers)
                assert replay.status_code == 502 and replay.json() == response.json()
                assert store.events_after(identity) == events
                assert records.read('v2_turn_requests', headers['Idempotency-Key']) == failed

                models.update('generation', {'expected_revision': models.public()['generation']['revision'],
                    'base_url': 'https://api.openai.com/v1', 'model': 'gpt-4o-mini',
                    'enabled': True, 'allow_remote': False})
                empty = client.post('/api/v2/projects', json={'name': '真实空资料范围'}).json()['id']
                assert application.state.workspace_domains.query.ask_target()['execution_location'] == 'remote'
                assert not egress_allowed(records, models, empty, 'generation')
                no_match = client.post('/api/v2/workbench/turns', json={
                    'project_id': empty, 'intent': 'ask', 'text': '这个空项目中有什么资料？'},
                    headers={'Idempotency-Key': 'actual-empty-project'})
                assert no_match.status_code == 200
                result = no_match.json()['turn']
                assert result['receipt']['ask']['no_match'] is True
                assert store.events_after(result['id'])[-1]['type'] == 'turn.completed'
                assert not any(row['type'].startswith('model.') for row in store.events_after(result['id']))
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
