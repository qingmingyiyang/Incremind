import json
from pathlib import Path

from backend.memory_app.v2.policies import override
from tools.memory_eval import evaluate


CORPUS = Path(__file__).resolve().parents[2] / 'fixtures/memory_eval/corpus.json'


def test_six_comparative_cases_have_independent_annotations_and_split_time_coverage():
    corpus = json.loads(CORPUS.read_text(encoding='utf-8'))
    cases = corpus.get('comparative_extraction', [])
    assert len(cases) >= 6
    assert len(corpus['questions']) == 66
    assert sum(len({row['scope_hint'] for row in case['expected']['candidates']}) >= 2 for case in cases) >= 2
    assert any('2026年' in condition for case in cases for row in case['expected']['candidates'] for condition in row['conditions'])
    assert {row['relation'] for case in cases for row in case['expected']['candidates']} | {
        row['relation'] for case in cases for row in case['expected']['supports']} == {
            'new', 'supplement', 'differs', 'may_supersede', 'duplicate_of'}


def test_comparative_eval_runs_real_services_and_synthetic_transport_only(tmp_path):
    corpus = json.loads(CORPUS.read_text(encoding='utf-8'))
    fixture = {'documents': [], 'insights': [], 'questions': [],
        'comparative_extraction': corpus.get('comparative_extraction', [])}
    path = tmp_path / 'comparative.json'
    path.write_text(json.dumps(fixture, ensure_ascii=False), encoding='utf-8')
    reports = []
    for policy in ('@1', '@2'):
        with override(extract=policy):
            reports.append(evaluate(path))
    assert [row['categories']['comparative_extraction']['hits'] for row in reports] == [0, 6]
    assert all(row['synthetic_model_attempts'] == 6 and row['remote_model_attempts'] == 0
        and row['model_attempts'] == 0 for row in reports)
    assert all(row['frozen_neighbors_match'] and row['hit'] for row in reports[1]['questions'])
    assert all(row['frozen_neighbors_match'] is None for row in reports[0]['questions'])
    assert all(row['actual'] == row['expected'] for row in reports[1]['questions'])
