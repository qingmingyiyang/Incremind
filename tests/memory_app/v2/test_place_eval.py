"""The placement corpus goes through the production local vocabulary adapter."""
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]


def test_complete_place_corpus_and_repeated_policy_override_are_offline(tmp_path):
    report = tmp_path / 'place.json'
    command = [sys.executable, str(ROOT / 'tools/place_eval.py'),
        '--fixtures', str(ROOT / 'tests/fixtures/place_eval/corpus.json'),
        '--policy', 'place=@1', '--policy', 'place=@2', '--report', str(report)]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(report.read_text(encoding='utf-8'))
    assert result['fixture'] == {'projects': 5, 'materials': 26, 'life_projects': 1, 'quick_notes': 6}
    assert result['policy_versions']['place'] == '@2'
    assert result['model_attempts'] == 0 and result['remote_model_attempts'] == 0
    assert len(result['materials']) == 26 and len({row['id'] for row in result['materials']}) == 26
    assert all(row['actual'] == row['expected'] and row['hit'] for row in result['materials'])
    assert result['correct'] == 26 and result['accuracy'] == 1


def test_place_one_reports_actual_no_suggestion_baseline(tmp_path):
    from tools.place_eval import evaluate
    result = evaluate(ROOT / 'tests/fixtures/place_eval/corpus.json', {'place': '@1'})
    assert result['correct'] == 0 and result['accuracy'] == 0
    assert result['model_attempts'] == 0 and result['remote_model_attempts'] == 0
    assert all(row['actual'] is None and not row['hit'] for row in result['materials'])
