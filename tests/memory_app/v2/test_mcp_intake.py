"""External writes enlist the real original and pending-recognition owners."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import copyfile
import socket
from threading import Thread

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.memory_app.v2.external_agent_settings import (
    external_agent_settings, replace_external_agent_settings,
)
from backend.memory_app.v2.privacy import set_private_project


INTAKES = 'v2_external_agent_intakes'
PREFIX = '/api/v2/external-agent/mcp/'


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / 'config').mkdir()
    copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example',
             tmp_path / 'config/settings.toml')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'desktop')
    from backend.memory_app.app import create_app
    app = create_app(runtime_root=tmp_path / 'runtime', legacy_app=FastAPI())
    records = app.state.recognition_records
    with records.begin() as tx:
        tx.put('v2_projects', 'alpha', {'name': '合成项目', 'scenes': ['reading']}, expected_revision=0)
        tx.commit()
    current = external_agent_settings(records)
    replace_external_agent_settings(records,
        {key: value for key, value in current.items() if key != 'revision'} | {'allow_remote': True},
        expected_revision=current['revision'])
    return app, TestClient(app), records


def post(env, tool, arguments, client='codex'):
    return env[1].post(PREFIX + tool, json={'client': client, 'arguments': arguments})


def configure(env, **changes):
    current = external_agent_settings(env[2])
    return replace_external_agent_settings(env[2],
        {key: value for key, value in current.items() if key != 'revision'} | changes,
        expected_revision=current['revision'])


def ledger(records, result, kind):
    row = records.read(INTAKES, result['receipt_id'])
    assert row.revision == 1
    assert row.payload['owner_id'] == 'local-user'
    assert row.payload['client'] == result['client']
    assert row.payload['tool'] == kind
    assert row.payload['project_id'] == result['project_id']
    assert row.payload['object_id'] == result['item_id' if kind == 'remember' else 'candidate_id']
    assert row.payload['object_revision'] == result['revision']
    return row


def assert_no_business_writes(records):
    for collection in ('workspace_items', 'recognition_experiences', 'recognition_candidates',
                       'recognitions', INTAKES):
        assert records.list(collection) == ()


def test_remember_defaults_to_inbox_redacts_before_original_and_keeps_revision(env):
    secret = 'sk-' + 'A' * 24
    response = post(env, 'remember', {'text': '合成原件\n' + secret})
    assert response.status_code == 200
    value = response.json()
    assert value['turn_id'] is None
    result = value['result']
    assert result['project_id'] == 'inbox' and result['state'] == 'staged'
    assert result['verified'] is False and result['client'] == 'codex'
    row = env[2].read('workspace_items', result['item_id'])
    assert row.revision == result['revision'] == 1
    assert row.payload['source_text'] == '合成原件\n[REDACTED_SECRET]'
    assert row.payload['status'] == 'staged' and row.payload['draft'] is None
    assert row.payload['document_id'] is None
    assert 'client' not in row.payload and 'source_client' not in row.payload
    ledger(env[2], result, 'remember')
    assert env[2].list('documents') == env[2].list('recognition_candidates') == ()


def test_propose_without_evidence_uses_real_client_statement_and_pending_queue(env):
    response = post(env, 'propose_insight', {
        'text': '合成待确认认识', 'conditions': ['阅读时'], 'project': 'alpha', 'scene': 'reading'},
        client='claude')
    assert response.status_code == 200
    result = response.json()['result']
    assert result['state'] == 'pending' and result['client'] == 'claude'
    candidate = env[2].read('recognition_candidates', result['candidate_id'])
    assert candidate.revision == result['revision'] == 1
    assert candidate.payload['state'] == 'pending'
    assert candidate.payload['scope'] == {'user_id': 'local-user', 'project_id': 'alpha'}
    assert candidate.payload['conditions'] == ['阅读时']
    assert candidate.payload['source_recognition_ids'] == []
    assert len(candidate.payload['source_experience_ids']) == 1
    source = env[2].read('recognition_experiences', candidate.payload['source_experience_ids'][0])
    assert source.payload['content'] == '合成待确认认识'
    assert source.payload['provenance']['kind'] == 'user_statement'
    assert source.payload['provenance']['actor'] == 'claude'
    assert source.payload['provenance']['source_refs'] == []
    assert env[2].list('recognitions') == env[2].list('workspace_items') == ()
    scene = env[2].read('v2_scene_assignments_candidate', candidate.object_id)
    assert scene.revision == 1 and scene.payload == {'project_id': 'alpha', 'scene': 'reading'}
    ledger(env[2], result, 'propose_insight')


@pytest.mark.parametrize('tool,arguments', [
    ('remember', {'text': '合成原件', 'project': 'alpha'}),
    ('propose_insight', {'text': '合成认识', 'project': 'alpha'}),
])
@pytest.mark.parametrize('blocked', ['off', 'client', 'private'])
def test_current_client_switch_and_privacy_block_both_writes(env, tool, arguments, blocked):
    if blocked == 'off':
        configure(env, allow_remote=False)
    elif blocked == 'client':
        configure(env, clients={'claude': True, 'codex': False})
    else:
        set_private_project(env[2], 'alpha', True, 0)
    response = post(env, tool, arguments)
    assert response.status_code == 409
    assert response.json()['detail'] in {'external_agent_disabled', 'external_agent_client_disabled',
                                         'external_agent_private'}
    assert_no_business_writes(env[2])


def test_write_tools_share_original_daily_quota_and_exhaustion_writes_nothing(env):
    configure(env, daily_limit=1)
    first = post(env, 'remember', {'text': '首个原件', 'project': 'alpha'})
    assert first.status_code == 200
    second = post(env, 'propose_insight', {'text': '不得写入', 'project': 'alpha'})
    assert second.status_code == 409
    assert second.json() == {'detail': 'external_agent_quota_exhausted'}
    assert len(env[2].list('workspace_items')) == len(env[2].list(INTAKES)) == 1
    assert env[2].list('recognition_experiences') == env[2].list('recognition_candidates') == ()


@pytest.mark.parametrize('tool,arguments', [
    ('remember', {'text': '合成', 'url': 'http://127.0.0.1/'}),
    ('remember', {'text': '合成', 'project': '../foreign'}),
    ('propose_insight', {'text': '合成', 'project': 'alpha', 'confirm': True}),
    ('propose_insight', {'text': '合成', 'project': 'alpha', 'conditions': '阅读时'}),
])
def test_http_schema_cannot_bypass_sdk_or_owner_scope(env, tool, arguments):
    response = post(env, tool, arguments)
    assert response.status_code == 400
    assert response.json() == {'detail': 'external_agent_request_invalid'}
    assert_no_business_writes(env[2])


def test_evidence_branch_is_explicit_safe_refusal_until_qualified_consumer_is_installed(env):
    response = post(env, 'propose_insight', {'text': '合成', 'project': 'alpha',
        'evidence_ids': [{'turn_id': 'turn-real-number-required', 'id': 'M1'}]})
    assert response.status_code == 409
    assert response.json() == {'detail': 'external_context_selection_invalid'}
    assert_no_business_writes(env[2])


def test_remember_private_network_url_uses_existing_network_guard(env):
    response = post(env, 'remember', {'url': 'http://127.0.0.1/', 'project': 'alpha'})
    assert response.status_code == 400
    assert response.json() == {'detail': 'external_agent_request_invalid'}
    assert_no_business_writes(env[2])


@contextmanager
def public_link(monkeypatch, body):
    requests = []
    class Provider(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(body.encode('utf-8'))
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    from backend.memory_app import workspace_links
    original_resolve = socket.getaddrinfo
    monkeypatch.setattr(workspace_links.socket, 'getaddrinfo', lambda host, port, *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port))]
        if host == 'public-fixture.invalid' else original_resolve(host, port, *args, **kwargs))
    def connect(address, timeout=None, source_address=None):
        assert address == ('8.8.8.8', 80)
        channel = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        channel.settimeout(timeout)
        channel.connect(server.server_address)
        return channel
    monkeypatch.setattr(workspace_links.socket, 'create_connection', connect)
    try:
        yield requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_remember_public_link_redacts_body_before_real_original_birth(env, monkeypatch):
    secret = 'sk-' + 'B' * 24
    with public_link(monkeypatch, '合成链接正文 ' + secret) as calls:
        response = post(env, 'remember', {'url': 'http://public-fixture.invalid/article',
            'project': 'alpha', 'scene': 'reading'})
    assert response.status_code == 200 and calls == ['/article']
    result = response.json()['result']
    item = env[2].read('workspace_items', result['item_id'])
    assert item.revision == 1 and item.payload['input_kind'] == 'link'
    assert item.payload['source_text'] == '合成链接正文 [REDACTED_SECRET]'
    assert item.payload['url'] == 'http://public-fixture.invalid/article'
    assert item.payload['status'] == 'staged' and item.payload['document_id'] is None
    scene = env[2].read('v2_scene_assignments_item', item.object_id)
    assert scene.revision == 1 and scene.payload == {'project_id': 'alpha', 'scene': 'reading'}
    ledger(env[2], result, 'remember')


def test_disabled_client_cannot_start_even_public_link_acquisition(env, monkeypatch):
    configure(env, clients={'claude': True, 'codex': False})
    with public_link(monkeypatch, '不得取得的合成正文') as calls:
        response = post(env, 'remember', {'url': 'http://public-fixture.invalid/article', 'project': 'alpha'})
    assert response.status_code == 409 and calls == []
    assert_no_business_writes(env[2])


def test_disabled_profile_cannot_start_me_link_acquisition(env, monkeypatch):
    configure(env, include_profile=False)
    with public_link(monkeypatch, '不得取得的画像正文') as calls:
        response = post(env, 'remember', {'url': 'http://public-fixture.invalid/article', 'project': 'me'})
    assert response.status_code == 409
    assert response.json() == {'detail': 'external_agent_profile_disabled'}
    assert calls == []
    assert_no_business_writes(env[2])


@pytest.mark.parametrize('tool', ['remember', 'propose_insight'])
def test_client_receipt_failure_rolls_back_all_actual_domain_writes(env, tool):
    with env[2].begin() as tx:
        tx.connection.execute("CREATE TRIGGER reject_client_receipt BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='v2_external_agent_intakes' BEGIN SELECT RAISE(ABORT,'derived_fixture'); END")
        tx.commit()
    response = post(env, tool, {'text': '必须回滚的合成输入', 'project': 'alpha', 'scene': 'reading'})
    assert response.status_code == 409
    assert response.json() == {'detail': 'external_context_unavailable'}
    assert_no_business_writes(env[2])
    assert env[2].list('v2_scene_assignments_item') == env[2].list('v2_scene_assignments_candidate') == ()
    with env[2].begin() as tx:
        assert tx.connection.execute('SELECT count(*) FROM crp_structured_records WHERE collection LIKE ?',
            ('v2_external_agent_quota_%',)).fetchone()[0] == 0
        tx.commit()
