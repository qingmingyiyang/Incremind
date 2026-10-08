import json
from pathlib import Path
from collections import Counter

from backend.memory_app.v2.policies import override
from tools.memory_eval import evaluate

FIXTURE = Path(__file__).resolve().parents[2] / 'fixtures/memory_eval/inspiration.json'


def test_new_original_questions_keep_the_existing_annotations(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    original = json.loads(FIXTURE.with_name('corpus.json').read_text(encoding='utf-8'))
    questions = fixture['inspiration_questions']
    assert len(questions) >= 6
    assert sum(any(word in row['question'] for word in ('点子', '创意')) for row in questions) >= 2
    assert len({row['id'] for row in [*original['questions'], *questions]}) == len(original['questions']) + len(questions)
    assert all(set(row['expected_evidence']) == set(row['expected_ids']) for row in questions)
    assert 'questions' not in fixture and 'documents' not in fixture and 'insights' not in fixture


def test_real_inspiration_retrieval_against_scope2_preserves_old_categories():
    reports = []
    for scope, compose in (('@2', '@3'), ('@3', '@4')):
        with override(scope=scope, compose=compose):
            reports.append(evaluate(FIXTURE))
    before, after = reports
    assert before['categories']['inspiration']['hits'] == 0
    assert after['categories']['inspiration']['hits'] == 6
    assert all(report['model_attempts'] == report['remote_model_attempts'] == 0 for report in reports)
    assert all(report['inspiration_capture_http_requests'] == 6 for report in reports)
    for category, baseline in before['categories'].items():
        if category == 'inspiration':
            continue
        current = after['categories'][category]
        assert current['hits'] >= baseline['hits'], category
        if 'average_tokens' in baseline:
            assert current['average_tokens'] <= baseline['average_tokens'] * 1.1, category
            assert current['stop_layers'] == baseline['stop_layers'], category
    prior = {row['id']: row for row in before['questions']}
    for row in after['questions']:
        if row['category'] != 'inspiration':
            assert row['hit'] == prior[row['id']]['hit']
            assert row.get('stop_layer') == prior[row['id']].get('stop_layer')
    assert Counter(row['category'] for row in before['questions']) == Counter(row['category'] for row in after['questions'])
