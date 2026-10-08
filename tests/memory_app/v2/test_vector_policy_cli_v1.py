"""实际离线 CLI 必须消费候选协议，不加载模型或修改默认语料。"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]


def vector_cases():
    model = 'google/embeddinggemma-2'
    def request(identity, inputs, expected, input_type='document', chosen=model):
        return {'id': identity, 'operation': 'request',
            'input': {'model': chosen, 'input': inputs, 'input_type': input_type}, 'expected': expected}
    return [
        {'id': 'query-prefix', 'operation': 'encode',
         'input': {'texts': ['合成查询'], 'input_type': 'query'},
         'expected': {'inputs': ['task: search result | query: 合成查询'],
                      'dims': 256, 'batch_size': 8, 'normalize_embeddings': True}},
        {'id': 'document-prefix', 'operation': 'encode',
         'input': {'texts': ['合成标题\n合成正文'], 'input_type': 'document'},
         'expected': {'inputs': ['title: 合成标题 | text: 合成正文'],
                      'dims': 256, 'batch_size': 8, 'normalize_embeddings': True}},
        request('exact-characters', '合' * 24000, {'accepted': True, 'items': 1, 'input_type': 'document'}),
        request('too-many-characters', '合' * 24001, {'error': 'local_vector_input_too_large'}),
        request('exact-items', ['合'] * 128, {'accepted': True, 'items': 128, 'input_type': 'document'}),
        request('too-many-items', ['合'] * 129, {'error': 'local_vector_input_too_large'}),
        request('model-key', '合成查询', {'accepted': True, 'items': 1, 'input_type': 'query'},
                input_type='query', chosen='google/embeddinggemma-2:256:vector@1'),
        request('wrong-dim-key', '合成查询', {'error': 'invalid_local_vector_request'},
                chosen='google/embeddinggemma-2:768:vector@1'),
        request('wrong-version-key', '合成查询', {'error': 'invalid_local_vector_request'},
                chosen='google/embeddinggemma-2:256:vector@2'),
        {'id': 'queue-boundary', 'operation': 'queue', 'input': {'items': 65},
         'expected': {'accepted': 64, 'error': 'local_vector_queue_full', 'closed': True}},
    ]


@pytest.mark.parametrize('selected', [False, True], ids=['baseline', 'candidate'])
def test_memory_eval_cli_consumes_vector_cases_with_active_default_and_explicit_override(tmp_path, selected):
    fixture = tmp_path / 'vector-corpus.json'
    cases = vector_cases()
    fixture.write_text(json.dumps({'documents': [], 'insights': [], 'questions': [],
        'vector_cases': cases}, ensure_ascii=False), encoding='utf-8')
    output = tmp_path / 'report.json'
    command = [sys.executable, '-B', str(ROOT / 'tools/memory_eval.py'),
        '--cases', str(fixture), '--output', str(output)]
    if selected:
        command.extend(['--policy', 'vector=@1'])
    environment = {**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
    result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
        text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding='utf-8'))
    category = report['categories']['vector_policy']
    assert category['metric_kind'] == 'pure_policy_protocol_controls'
    assert category['total'] == len(cases)
    assert report['model_attempts'] == 0 and report['remote_model_attempts'] == 0
    rows = report['questions']
    assert [row['id'] for row in rows] == [case['id'] for case in cases]
    assert [row['expected'] for row in rows] == [case['expected'] for case in cases]
    assert category['state'] == 'enabled' and category['policy_version'] == '@1'
    assert category['evaluated'] == category['hits'] == len(cases) == 10
    assert all(row['hit'] is True and row['actual'] == row['expected'] for row in rows)
