import pytest

from backend.memory_app.v2.policies import ACTIVE, get, override
from backend.memory_app.v2.overviews import ScopeOverviews
from backend.memory_app.v2.privacy import set_private_project
from tests.memory_app.v2.test_overviews import OverviewModel
from tests.memory_app.v2.test_workbench_ask import env, add_document, ask, publish


def projects(env, **names):
    with env.records.begin() as tx:
        for identity, name in names.items():
            old = tx.read('v2_projects', identity)
            tx.put('v2_projects', identity, {'name': name, 'scenes': [], 'builtin': None},
                   expected_revision=old.revision if old else 0)
        tx.commit()


def test_local_hint_is_immutable_without_model_or_other_project_evidence(env):
    projects(env, alpha='厨房 食谱', beta='旅行 出行 路线')
    env.model.allowed = False
    with override(elsewhere='@1'):
        response = ask(env, '旅行 出行 路线', intent='ask')
    assert response.status_code == 200, response.text
    value = response.json()
    hint = value['turn']['receipt']['ask']
    assert hint['elsewhere'] == {'project_id': 'beta', 'scene': None}
    assert hint['answer'] == '当前项目中没有匹配且可用于回答的资料。'
    assert hint['citations'] == [] and hint['model_usage'] == {} and 'model_cost' not in hint
    assert env.model.calls == 0 and env.records.list('workspace_ask_receipts') == ()
    projects(env, beta='完全不相关')
    for _ in range(2):
        result = env.http.get(f"/api/v2/workbench/threads/{value['thread_id']}?project_id=alpha")
        assert result.status_code == 200
        assert result.json()['turns'] == [value['turn']]
    assert env.model.calls == 0


@pytest.mark.parametrize('text,sufficient', [('#alpha 旅行 出行 路线', False), ('旅行 出行 路线', True)])
def test_tagged_or_sufficient_ask_keeps_original_receipt(env, text, sufficient):
    projects(env, alpha='厨房 食谱', beta='旅行 出行 路线')
    env.model.allowed = sufficient
    if sufficient:
        publish(env, '旅行 出行 路线')
    with override(elsewhere='@1'):
        result = ask(env, text, intent='ask')
    assert result.status_code == 200, result.text
    receipt = result.json()['turn']['receipt']['ask']
    assert 'elsewhere' not in receipt
    assert env.model.calls == int(sufficient)
    assert receipt['answer'] == ('Synthetic answer' if sufficient else '当前项目中没有匹配且可用于回答的资料。')


def test_bookshelf_restored_sufficient_answer_does_not_suggest_another_project(env):
    from backend.memory_app.v2.elsewhere import suggest_elsewhere
    from backend.memory_app.v2.links import similarity
    from tests.memory_app.v2.test_bookshelf import forgotten
    projects(env, alpha='厨房', beta='alpha beta gamma')
    insight, _ = publish(env)
    forgotten(env, insight, by='auto')
    question = 'alpha beta gamma'
    assert similarity(question, 'alpha beta gamma') == 1
    assert suggest_elsewhere(env.domains.query, 'alpha', question, coverage=0,
        has_hits=True, policy_version='@1') == {'project_id': 'beta', 'scene': None}
    assert env.model.calls == 0
    with override(elsewhere='@1'):
        response = ask(env, question, intent='ask')
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    assert receipt['answer'] == 'Synthetic answer' and receipt['no_match'] is False
    assert len(receipt['citations']) == 1
    citation = receipt['citations'][0]
    assert citation['id'] == insight.id and citation['layer'] == 'insight'
    assert citation['bookshelf'] is True and citation['persona'] is False
    assert receipt['trace'][0]['bookshelf'] == {'hits': 1, 'used': 1}
    assert receipt['trace'][-1]['coverage'] == 0
    assert 'elsewhere' not in receipt


@pytest.mark.parametrize('wordings,evidence,expected', [
    (['alpha beta'], 'ALPHA BETA', 1),
    (['absent', 'alpha beta'], 'alpha beta', 9 / 14),
    (['alpha beta', 'alpha beta'], 'alpha', 2 / 9),
    (['alpha beta', 'beta gamma'], 'alpha beta', 9 / 16),
    ([''], 'alpha beta', 0),
])
def test_navigation_coverage_preserves_original_weighted_term_union(wordings, evidence, expected):
    from core.search_and_recall.evidence_windows import query_terms
    assert get('elsewhere', version='@1')(operation='coverage', coverage_questions=wordings,
        evidence=evidence, terms_for=query_terms) == expected


@pytest.mark.parametrize('identity', ['alpha', 'inbox', 'me'])
def test_excluded_scope_never_becomes_a_hint(env, identity):
    projects(env, alpha='厨房 食谱', **({identity: '旅行 出行 路线'} if identity != 'alpha' else {}))
    env.model.allowed = False
    with override(elsewhere='@1'):
        result = ask(env, '旅行 出行 路线', intent='ask')
    assert result.status_code == 200, result.text
    assert 'elsewhere' not in result.json()['turn']['receipt']['ask']
    assert env.model.calls == 0


def test_private_name_is_only_local_and_unknown_shared_scope_is_deferred(env):
    from backend.memory_app.v2.elsewhere import suggest_elsewhere
    projects(env, alpha='厨房', beta='旅行 出行 路线')
    set_private_project(env.records, 'beta', True, 0)
    with override(elsewhere='@1'):
        assert suggest_elsewhere(env.domains.query, 'alpha', '旅行 出行 路线', coverage=0, has_hits=False) == {
            'project_id': 'beta', 'scene': None}
    old = env.records.read('v2_projects', 'beta')
    with env.records.begin() as tx:
        tx.put('v2_projects', 'beta', {**old.payload, 'shared': True}, expected_revision=old.revision)
        tx.commit()
    with override(elsewhere='@1'):
        assert suggest_elsewhere(env.domains.query, 'alpha', '旅行 出行 路线', coverage=0, has_hits=False) is None
    assert env.model.calls == 0 and env.records.list('v2_memory_turn_keys') == ()


def test_valid_overview_is_read_using_metadata_only_and_stales_on_edit(env):
    doc, _ = add_document(env, project='beta', summary='桥梁预算已确认', body='禁止读取正文', original='禁止读取原件')
    model = OverviewModel()
    owner = ScopeOverviews(env.records, env.documents, model)
    overview = owner.update('beta')
    assert overview
    class MetadataAudit:
        def __getattr__(self, name):
            return getattr(env.records, name)
        def read(self, collection, identity):
            assert collection not in {'documents', 'document_markdown', 'document_revisions',
                'workspace_items', 'recognition_experiences', 'recognitions', 'sources'}
            return env.records.read(collection, identity)
        def list(self, collection):
            assert collection not in {'documents', 'document_markdown', 'document_revisions',
                'workspace_items', 'recognition_experiences', 'recognitions', 'sources'}
            return env.records.list(collection)
    audited = ScopeOverviews(MetadataAudit(), env.documents, model)
    assert audited.current_metadata('beta') == overview
    env.documents.save_user_edit(doc, expected_revision=2, markdown='# changed\n\n## 摘要\n不同摘要')
    assert audited.current_metadata('beta') is None
    assert model.calls == 1


def test_stale_overview_cannot_create_a_false_hint(env):
    from backend.memory_app.v2.elsewhere import suggest_elsewhere
    projects(env, alpha='厨房', beta='无关')
    doc, _ = add_document(env, project='beta', summary='桥梁预算已确认')
    owner = ScopeOverviews(env.records, env.documents, OverviewModel())
    owner.update('beta')
    env.documents.save_user_edit(doc, expected_revision=2, markdown='# changed\n\n## 摘要\n无关')
    with override(elsewhere='@1'):
        assert suggest_elsewhere(env.domains.query, 'alpha', '桥梁预算已确认', coverage=0, has_hits=False) is None


def test_policy_registration_keeps_active_and_rejects_near_ties():
    from backend.memory_app.v2.links import similarity
    policy = get('elsewhere', version='@1')
    assert ACTIVE['elsewhere'] == '@1'
    candidates = [{'project_id': p, 'scene': None, 'texts': ('旅行 出行 路线',)} for p in ('alpha', 'beta')]
    assert policy(question='旅行 出行 路线', project_id='alpha', coverage=0, has_hits=False,
                  tagged=False, candidates=candidates, score=similarity) is None
    with pytest.raises(ValueError):
        get('elsewhere', version='@99')


@pytest.mark.parametrize('change', ['malformed', 'malformed_snapshot', 'malformed_node', 'source', 'scene', 'privacy', 'invalid_index'])
def test_metadata_failures_only_discard_navigation_prose(env, change):
    from backend.memory_app.original_sources import source_store
    from backend.memory_app.v2.projects import assign_scene
    from core.storage_provider.source_retrieval_index import source_mutation, invalidate_source
    doc, item_id = add_document(env, project='beta', summary='桥梁预算已确认')
    owner = ScopeOverviews(env.records, env.documents, OverviewModel())
    assert owner.update('beta') and owner.current_metadata('beta')
    item = env.records.read('workspace_items', item_id)
    if change in {'malformed', 'malformed_snapshot', 'malformed_node'}:
        from copy import deepcopy
        row = env.records.read('v2_scope_overviews', 'beta')
        payload = deepcopy(dict(row.payload))
        if change == 'malformed':
            payload['input_revision'] = []
        elif change == 'malformed_snapshot':
            payload['input_revision']['members'][0]['source_snapshot'] = []
        else:
            payload['input_revision']['members'][0]['source_snapshot']['nodes'] = ['invalid']
        with env.records.begin() as tx:
            tx.put(row.collection, row.object_id, payload, expected_revision=row.revision)
            tx.commit()
    elif change == 'source':
        store = source_store(env.records)
        body = store.read('sources', item.payload['source_id'])
        store.write('sources', item.payload['source_id'], {**body, 'metadata': {'content': '新的原件'}}, expected_revision=1)
    elif change == 'scene':
        assign_scene(env.records, 'document', doc, 'beta', '新场景')
    elif change == 'privacy':
        set_private_project(env.records, 'beta', True, 0)
    else:
        store = source_store(env.records)
        with source_mutation(store, item.payload['source_id']) as tx:
            invalidate_source(tx, store, item.payload['source_id'], 'beta')
    assert owner.current_metadata('beta') is None
    assert owner.models.calls == 1


def test_offline_elsewhere_has_held_out_cases_and_zero_model_calls():
    from tools.memory_eval import evaluate
    from pathlib import Path
    path = Path(__file__).parents[2] / 'fixtures/memory_eval/elsewhere.json'
    with override(elsewhere='@1'):
        report = evaluate(path)
    result = report['categories']['elsewhere']
    assert result['held_out_wrong_place_total'] >= 6
    assert result['calibration'] == {'total': 18, 'correct': 18, 'threshold': .1, 'margin': .07, 'production_matches': True}
    assert result['hint_accuracy'] == 1 and result['false_suggestion_rate'] <= .1
    assert report['model_attempts'] == report['remote_model_attempts'] == 0
    assert len(report['questions']) >= 66 + 16
