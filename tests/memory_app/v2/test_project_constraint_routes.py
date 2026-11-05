from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.v2.project_constraint_routes import install_project_constraint_routes
from backend.memory_app.v2.project_constraints import ProjectConstraintScopes
from backend.recognition import WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


def setup_routes(path):
    records = SQLiteStructuredRecordStore(path / 'records.sqlite3')
    with records.begin() as tx:
        tx.put('v2_projects', 'project-a', {'name': '约束验证', 'scenes': ['会议']}, expected_revision=0)
        tx.commit()
    service = ProjectConstraintScopes(records, namespace='route-constraints', owner_user_id='qa-user', origin_kind='personal')
    application = FastAPI()
    install_project_constraint_routes(application, service=service)
    return application, records, service


def body(**changes):
    return {'content': '输出周报格式', 'enabled': True, 'valid_from': None, 'valid_until': None,
            'activation': {'mode': 'mention', 'scene': None, 'keywords': ['周报']},
            'expected_revision': 0, 'expected_scope_revision': 0, 'expected_project_revision': 1, **changes}


def test_real_http_route_saves_actual_scope_and_hides_internal_origin(tmp_path):
    application, records, service = setup_routes(tmp_path)
    with TestClient(application) as client:
        response = client.put('/api/v2/projects/project-a/constraints/rule', json=body())
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved['activation']['keywords'] == ['周报']
        assert saved['revision'] == 1 and saved['scope_revision'] == 1
        listing = client.get('/api/v2/projects/project-a/constraints')
        assert listing.status_code == 200
        assert set(listing.json()) == {'items', 'scenes', 'project_revision'}
        assert listing.json()['scenes'] == ['会议']
        assert 'database_path' not in listing.text and 'qa-user' not in listing.text
        assert records.read('project_constraints', 'rule').payload['content'] == '输出周报格式'
        assert service.prepare_bundle(WorkScope('local-user', 'project-a'), scene=None, text='写周报')['items'][0]['id'] == 'rule'


def test_stale_scope_or_project_revision_returns_conflict_and_keeps_current_body(tmp_path):
    application, records, _ = setup_routes(tmp_path)
    with TestClient(application) as client:
        path = '/api/v2/projects/project-a/constraints/rule'
        assert client.put(path, json=body()).status_code == 200
        stale_scope = client.put(path, json=body(content='过期请求', expected_revision=1))
        assert stale_scope.status_code == 409
        stale_project = client.put(path, json=body(content='过期项目', expected_revision=1,
            expected_scope_revision=1, expected_project_revision=2))
        assert stale_project.status_code == 409
        assert records.read('project_constraints', 'rule').payload['content'] == '输出周报格式'


def test_client_cannot_supply_owner_namespace_or_unknown_activation(tmp_path):
    application, records, _ = setup_routes(tmp_path)
    with TestClient(application) as client:
        path = '/api/v2/projects/project-a/constraints/rule'
        for changes in ({'owner_user_id': 'other-user'}, {'namespace': 'other'},
                        {'activation': {'mode': 'unknown', 'scene': None, 'keywords': []}},
                        {'expected_revision': True}):
            response = client.put(path, json=body(**changes))
            assert response.status_code == 400, response.text
        assert records.read('project_constraints', 'rule') is None
        assert client.get('/api/v2/projects/missing-project/constraints').status_code == 404


def test_reverse_date_and_unregistered_scene_are_rejected_before_persisting(tmp_path):
    application, records, _ = setup_routes(tmp_path)
    with TestClient(application) as client:
        path = '/api/v2/projects/project-a/constraints/rule'
        reverse = client.put(path, json=body(valid_from='2026-11-04T12:00:00Z', valid_until='2026-11-03T12:00:00Z'))
        assert reverse.status_code == 400, reverse.text
        scene = client.put(path, json=body(activation={'mode': 'scene', 'scene': '已移除', 'keywords': []}))
        assert scene.status_code == 400, scene.text
        assert records.read('project_constraints', 'rule') is None


def test_activation_preview_uses_the_same_unicode_casefold_and_nfc_rules_without_writing(tmp_path):
    application, records, _ = setup_routes(tmp_path)
    original = records.read('v2_projects', 'project-a')
    with TestClient(application) as client:
        path = '/api/v2/projects/project-a/constraints/activation-preview'
        for words in (['API', 'api'], ['straße', 'STRASSE'], ['cafe\u0301', 'café'], ['σ', 'ς']):
            response = client.post(path, json={'mode': 'mention', 'scene': None, 'keywords': words})
            assert response.status_code == 400, response.text
            assert response.json()['detail'] == 'constraint_keyword_duplicate'
        valid = client.post(path, json={'mode': 'mention', 'scene': None, 'keywords': ['cafe\u0301']})
        assert valid.status_code == 200, valid.text
        assert valid.json()['activation']['keywords'] == ['café']
        assert valid.json()['limits'] == {'max_keywords': 32, 'max_keyword_chars': 128}
        for extra in ({'owner_user_id': 'other-user'}, {'namespace': 'other-namespace'}):
            assert client.post(path, json={'mode': 'always', 'scene': None, 'keywords': [], **extra}).status_code == 400
    assert records.read('v2_projects', 'project-a') == original
    assert records.list('v2_constraint_scopes') == ()
    assert records.list('project_constraints') == ()


def test_activation_preview_enforces_real_policy_keyword_count_and_character_bounds(tmp_path):
    application, records, _ = setup_routes(tmp_path)
    with TestClient(application) as client:
        path = '/api/v2/projects/project-a/constraints/activation-preview'
        maximum = [f'关键词{index}' for index in range(32)]
        assert client.post(path, json={'mode': 'mention', 'scene': None, 'keywords': maximum}).status_code == 200
        many = client.post(path, json={'mode': 'mention', 'scene': None, 'keywords': [*maximum, '第33个']})
        assert many.status_code == 400
        assert many.json() == {'detail': 'constraint_keywords_too_many', 'limits': {'max_keywords': 32, 'max_keyword_chars': 128}}
        assert client.post(path, json={'mode': 'mention', 'scene': None, 'keywords': ['字' * 128]}).status_code == 200
        long_word = client.post(path, json={'mode': 'mention', 'scene': None, 'keywords': ['字' * 129]})
        assert long_word.status_code == 400
        assert long_word.json()['detail'] == 'constraint_keyword_invalid'
        assert long_word.json()['limits']['max_keyword_chars'] == 128
    assert records.list('project_constraints') == ()
