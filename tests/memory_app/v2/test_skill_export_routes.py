"""真实 HTTP 入口保持审阅 CAS、项目隔离及无模型手写。"""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tests.memory_app.v2.test_skill_exports import env, method, draft
from backend.memory_app.v2.skill_export_routes import install_skill_export_routes


@pytest.fixture
def client(env):
    app = FastAPI()
    app.state.deployment = SimpleNamespace(mode='desktop')
    install_skill_export_routes(app, records=env[0], service=env[1], models=None)
    with TestClient(app) as client:
        yield client


BASE = '/api/v2/projects/alpha/skill-exports'


def created(env, client):
    one = method(env)
    response = client.post(BASE, json={'sources': [{'id': one.id, 'revision': 1}],
        'document': draft(), 'scene': None})
    assert response.status_code == 200
    return one, response.json()


def test_handwritten_http_review_and_download_without_models(env, client):
    one, result = created(env, client)
    assert client.get(BASE + '/methods').json()['items'][0]['id'] == one.id
    assert client.get(BASE).json()['items'][0]['id'] == result['id']
    path = BASE + '/' + result['id']
    assert client.post(path + '/download', json={'expected_revision': 1}).status_code == 409
    reviewed = client.post(path + '/review', json={'expected_revision': 1})
    assert reviewed.status_code == 200 and reviewed.json()['reviewed'] is True
    downloaded = client.post(path + '/download', json={'expected_revision': 2})
    assert downloaded.status_code == 200 and downloaded.content.startswith(b'PK')
    assert downloaded.headers['content-type'] == 'application/zip'
    assert 'choose-gift.zip' in downloaded.headers['content-disposition']
    current = client.get(path).json()
    assert current['revision'] == 3 and current['exported_revision'] == 2


def test_http_edit_requires_exact_revision_and_clears_review(env, client):
    _, result = created(env, client)
    path = BASE + '/' + result['id']
    client.post(path + '/review', json={'expected_revision': 1})
    assert client.patch(path, json={'expected_revision': 1, 'document': draft()}).status_code == 409
    response = client.patch(path, json={'expected_revision': 2, 'document': draft()})
    assert response.status_code == 200 and response.json()['reviewed'] is False


def test_other_project_cannot_read_review_edit_or_download(env, client):
    _, result = created(env, client)
    path = '/api/v2/projects/beta/skill-exports/' + result['id']
    assert client.get(path).status_code == 404
    for suffix in ('review', 'download'):
        assert client.post(path + '/' + suffix, json={'expected_revision': 1}).status_code == 404
    assert client.patch(path, json={'expected_revision': 1, 'document': draft()}).status_code == 404


@pytest.mark.parametrize('body', [{}, {'sources': [], 'document': draft(), 'scene': None, 'extra': True},
    {'sources': [], 'document': draft(), 'scene': 3}])
def test_invalid_http_fields_do_not_write(env, client, body):
    assert client.post(BASE, json=body).status_code == 400
    assert len(env[0].list('v2_skill_exports')) == 0


def test_handwritten_regeneration_tracks_new_source_and_requires_review_again(env, client):
    one, result = created(env, client)
    path = BASE + '/' + result['id']
    client.post(path + '/review', json={'expected_revision': 1})
    from backend.recognition import WorkScope
    changed = env[1].revise(scope=WorkScope('local-user', 'alpha'), recognition_id=one.id,
        expected_revision=1, content='先核实预算，再询问近期愿望')
    assert client.get(path).json()['needs_update'] is True
    response = client.post(path + '/regenerate', json={'expected_revision': 2,
        'sources': [{'id': changed.id, 'revision': changed.revision}], 'document': draft(), 'scene': None})
    assert response.status_code == 200
    assert response.json()['needs_update'] is False and response.json()['reviewed'] is False


def test_server_cannot_fall_back_to_desktop_domain(env):
    app = FastAPI()
    app.state.deployment = SimpleNamespace(mode='server')
    install_skill_export_routes(app, records=env[0], service=env[1], models=None)
    with TestClient(app) as client:
        assert client.get(BASE).status_code == 503
        assert client.get(BASE + '/methods').status_code == 503
    assert len(env[0].list('v2_skill_exports')) == 0


def test_generated_http_draft_binds_actual_aux_output_and_is_unreviewed(env):
    from tests.memory_app.v2.test_skill_generation import Model
    models, one = Model(), method(env)
    app = FastAPI()
    app.state.deployment = SimpleNamespace(mode='desktop')
    install_skill_export_routes(app, records=env[0], service=env[1], models=models)
    with TestClient(app) as client:
        response = client.post(BASE + '/generate', json={'sources': [{'id': one.id, 'revision': 1}],
            'scene': None, 'key': 'synthetic-http-origin'})
    assert response.status_code == 200
    saved = response.json()
    assert saved['reviewed'] is False and models.calls == 1
    binding = env[0].read('v2_memory_turn_keys', saved['generation_turn_id'])
    assert binding.payload['identity']['kind'] == 'memory.skill_export'
    assert binding.payload['request']['execution_policy']['purpose'] == 'aux'


def test_disabled_http_generation_leaves_handwriting_available(env):
    from tests.memory_app.v2.test_skill_generation import Model
    models, one = Model(), method(env)
    models.allowed = False
    app = FastAPI()
    install_skill_export_routes(app, records=env[0], service=env[1], models=models)
    with TestClient(app) as client:
        sources = [{'id': one.id, 'revision': 1}]
        response = client.post(BASE + '/generate', json={'sources': sources, 'key': 'synthetic-off'})
        assert response.status_code == 403 and response.json()['detail'] == 'skill_generation_disabled'
        assert client.post(BASE, json={'sources': sources, 'document': draft()}).status_code == 200
    assert models.calls == 0 and len(env[0].list('v2_memory_turn_keys')) == 0


def test_foreign_regeneration_rejects_before_any_model_call(env):
    from tests.memory_app.v2.test_skill_generation import Model
    models, one = Model(), method(env)
    app = FastAPI()
    install_skill_export_routes(app, records=env[0], service=env[1], models=models)
    with TestClient(app) as client:
        response = client.post(BASE + '/absent/regenerate', json={'expected_revision': 1,
            'sources': [{'id': one.id, 'revision': 1}], 'key': 'synthetic-absent'})
    assert response.status_code == 404 and models.calls == 0


def test_folder_export_requires_first_confirmation_and_exact_versions(env, client, tmp_path):
    _, created_row = created(env, client)
    path = BASE + '/' + created_row['id']
    client.post(path + '/review', json={'expected_revision': 1})
    assert client.get(BASE).json()['local_folder'] == {'available': True, 'confirmed': False, 'revision': 0}
    body = {'expected_revision': 2, 'directory': str(tmp_path),
        'confirm_first_export': False, 'expected_confirmation_revision': 0}
    rejected = client.post(path + '/folder', json=body)
    assert rejected.status_code == 409 and rejected.json()['detail'] == 'skill_folder_confirmation_required'
    assert not (tmp_path / 'choose-gift').exists()
    stale = client.post(path + '/folder', json={**body,
        'confirm_first_export': True, 'expected_confirmation_revision': 1})
    assert stale.status_code == 409 and not (tmp_path / 'choose-gift').exists()
    exported = client.post(path + '/folder', json={**body, 'confirm_first_export': True})
    assert exported.status_code == 200
    assert exported.json()['folder_path'] == str(tmp_path / 'choose-gift')
    assert (tmp_path / 'choose-gift' / 'SKILL.md').is_file()
    assert client.get(BASE).json()['local_folder']['confirmed'] is True
    repeated = client.post(path + '/folder', json={**body, 'expected_revision': 3,
        'expected_confirmation_revision': 1})
    assert repeated.status_code == 200


def test_missing_review_or_private_source_cannot_write_selected_folder(env, client, tmp_path):
    one, result = created(env, client)
    path = BASE + '/' + result['id']
    body = {'expected_revision': 1, 'directory': str(tmp_path),
        'confirm_first_export': True, 'expected_confirmation_revision': 0}
    assert client.post(path + '/folder', json=body).status_code == 409
    assert not (tmp_path / 'choose-gift').exists()
    from backend.memory_app.source_egress import SourceEgressService
    from backend.recognition import WorkScope
    SourceEgressService(env[0]).set_policy(WorkScope('local-user', 'alpha'), 'recognition', one.id, 1, 0, [])
    assert client.post(path + '/folder', json=body).status_code == 409
    assert not (tmp_path / 'choose-gift').exists()
    assert client.get(BASE).json()['local_folder']['confirmed'] is False


def test_failed_aux_terminal_cannot_be_exported_from_cached_output(env):
    import sqlite3
    from backend.memory_app.v2.memory_turn import MemoryTurn
    from tests.memory_app.v2.test_skill_generation import Model
    models, one = Model(), method(env)
    MemoryTurn.store_for(env[0])
    with sqlite3.connect(env[0].database_path.parent / 'ai-turns.sqlite3') as connection:
        connection.execute("""CREATE TRIGGER reject_skill_terminal BEFORE INSERT ON ai_turn_events
            WHEN json_extract(NEW.event_json, '$.type') = 'turn.completed'
            BEGIN SELECT RAISE(ABORT, 'synthetic terminal failure'); END""")
    app = FastAPI()
    install_skill_export_routes(app, records=env[0], service=env[1], models=models)
    body = {'sources': [{'id': one.id, 'revision': 1}], 'key': 'synthetic-failed-terminal'}
    with TestClient(app, raise_server_exceptions=False) as client:
        first = client.post(BASE + '/generate', json=body)
        repeated = client.post(BASE + '/generate', json=body)
        assert first.status_code == repeated.status_code == 409
        assert first.json()['detail'] == repeated.json()['detail'] == 'skill_generation_origin_invalid'
    assert models.calls == 1 and len(env[0].list('v2_skill_exports')) == 0


def test_same_generation_key_reuses_export_identity_without_overwriting_edits(env):
    from tests.memory_app.v2.test_skill_generation import Model
    models, one = Model(), method(env)
    app = FastAPI()
    install_skill_export_routes(app, records=env[0], service=env[1], models=models)
    body = {'sources': [{'id': one.id, 'revision': 1}], 'key': 'synthetic-repeat-http'}
    with TestClient(app) as client:
        first = client.post(BASE + '/generate', json=body)
        assert first.status_code == 200
        changed = {**draft(), 'description': '用户审阅后的描述。'}
        path = BASE + '/' + first.json()['id']
        assert client.patch(path, json={'expected_revision': 1, 'document': changed}).status_code == 200
        repeated = client.post(BASE + '/generate', json=body)
        assert repeated.status_code == 200 and repeated.json()['id'] == first.json()['id']
        assert repeated.json()['document'] == changed
    assert models.calls == 1 and len(env[0].list('v2_skill_exports')) == 1


def test_old_generation_key_retains_identity_after_regeneration_and_edit(env):
    from tests.memory_app.v2.test_skill_generation import Model
    models, one = Model(), method(env)
    app = FastAPI()
    install_skill_export_routes(app, records=env[0], service=env[1], models=models)
    body = {'sources': [{'id': one.id, 'revision': 1}], 'key': 'synthetic-origin-history-a'}
    with TestClient(app) as client:
        first = client.post(BASE + '/generate', json=body)
        assert first.status_code == 200
        path = BASE + '/' + first.json()['id']
        newer = client.post(path + '/regenerate', json={**body, 'expected_revision': 1,
            'key': 'synthetic-origin-history-b'})
        assert newer.status_code == 200
        changed = {**draft(), 'description': '重新生成后用户保存的描述。'}
        edited = client.patch(path, json={'expected_revision': 2, 'document': changed})
        assert edited.status_code == 200
        repeated = client.post(BASE + '/generate', json=body)
        assert repeated.status_code == 200 and repeated.json()['id'] == first.json()['id']
        assert repeated.json()['document'] == changed and repeated.json()['revision'] == 3
    assert models.calls == 2 and len(env[0].list('v2_skill_exports')) == 1


def test_missing_model_adapter_keeps_handwriting_and_rejects_generation(env, client):
    one, saved = created(env, client)
    body = {'sources': [{'id': one.id, 'revision': 1}], 'key': 'synthetic-no-model'}
    assert client.get(BASE).json()['generation_available'] is False
    for path, payload in ((BASE + '/generate', body),
            (BASE + '/' + saved['id'] + '/regenerate', {**body, 'expected_revision': 1})):
        response = client.post(path, json=payload)
        assert response.status_code == 403 and response.json()['detail'] == 'skill_generation_disabled'
    assert env[2].get('alpha', saved['id'])['revision'] == 1
    assert len(env[0].list('v2_memory_turn_keys')) == 0
