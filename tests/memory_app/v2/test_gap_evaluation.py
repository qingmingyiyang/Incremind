"""固定标注验证已登记的 gap 策略操作，覆盖范围为纯策略协议。"""
import json
from pathlib import Path

import pytest

from backend.memory_app.v2.policies import ACTIVE, override
from tools.memory_eval import evaluate


CASES = Path(__file__).parents[2] / 'fixtures/memory_eval/gaps.json'
COUNTS = {'insufficient': 18, 'related': 10, 'selection': 10}


def test_registered_inactive_gap_has_disabled_null_accuracy(monkeypatch):
    before = dict(ACTIVE)
    assert ACTIVE['gap'] == '@1'
    with monkeypatch.context() as configuration:
        configuration.delitem(ACTIVE, 'gap')
        report = evaluate(CASES)
    assert ACTIVE == before
    result = report['categories']['gap_policy']
    assert result == {
        'state': 'disabled', 'policy_version': None,
        'metric_kind': 'pure_policy_protocol_controls',
        'total': 38, 'evaluated': 0, 'hits': None, 'accuracy': None,
        'by_operation': {name: {'total': count, 'evaluated': 0, 'hits': None,
                              'accuracy': None} for name, count in COUNTS.items()},
    }
    assert len(report['questions']) == 38
    assert all(row['actual'] is None and row['hit'] is None
               and row['policy_version'] is None for row in report['questions'])
    assert report['fixture_counts'] == {'documents': 0, 'sources': 0, 'insights': 0, 'questions': 38}
    assert report['model_attempts'] == report['synthetic_model_attempts'] == report['remote_model_attempts'] == 0


def test_gap_override_compares_all_three_real_operations_with_annotations():
    labels = json.loads(CASES.read_text(encoding='utf-8'))['gap_cases']
    with override(gap='@1'):
        report = evaluate(CASES)
    assert report['categories']['gap_policy'] == {
        'state': 'enabled', 'policy_version': '@1',
        'metric_kind': 'pure_policy_protocol_controls',
        'total': 38, 'evaluated': 38, 'hits': 38, 'accuracy': 1.0,
        'by_operation': {name: {'total': count, 'evaluated': count, 'hits': count,
                              'accuracy': 1.0} for name, count in COUNTS.items()},
    }
    assert report['questions'] == [
        {'id': case['id'], 'category': 'gap_policy', 'operation': case['operation'],
         'policy_version': '@1', 'expected': case['expected'], 'actual': case['expected'],
         'hit': True} for case in labels
    ]
    assert 'not model semantic quality' in report['metric_definitions']['gap_policy']
    assert report['model_attempts'] == report['synthetic_model_attempts'] == report['remote_model_attempts'] == 0
    assert ACTIVE['gap'] == '@1'


def test_gap_override_rejects_unknown_version():
    with pytest.raises(ValueError, match='unknown_policy_version'):
        with override(gap='@999'):
            evaluate(CASES)


def test_fixture_without_gap_keeps_original_report_shape(tmp_path):
    fixture = tmp_path / 'empty.json'
    fixture.write_text(json.dumps({'documents': [], 'insights': [], 'questions': []}), encoding='utf-8')
    report = evaluate(fixture)
    assert set(report) == {'schema_version', 'repository_revision', 'forgetting_mode',
        'token_measure', 'evaluation_time', 'model_attempts', 'synthetic_model_attempts',
        'remote_model_attempts', 'metric_definitions', 'fixture_counts', 'categories', 'questions'}
    assert report['schema_version'] == 2 and report['categories'] == {} and report['questions'] == []
    assert set(report['metric_definitions']) == {'hit_rate', 'citation_correct_rate',
        'average_recall_count', 'false_recall_rate', 'comparative_extraction'}
    assert report['fixture_counts'] == {'documents': 0, 'sources': 0, 'insights': 0, 'questions': 0}
