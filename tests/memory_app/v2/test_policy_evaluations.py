"""CLI selections exercise real offline routes and restore after success/error."""
import json

import pytest

from backend.memory_app.v2.policies import get, parse_overrides, register, version
from tests.memory_app.v2.test_route_eval import load_evaluator, fixture, part


def test_repeated_policy_options_last_value_wins_without_mutating_active():
    assert parse_overrides(['rank=1', 'rank=@1', 'enough=1']) == {'rank': '@1', 'enough': '@1'}
    with pytest.raises(ValueError):
        parse_overrides(['rank=@1', 'missing=@1'])
    assert version('rank') == '@1'


def test_route_cli_uses_override_and_restores_on_success_and_error(tmp_path):
    register('route', '@9501')(lambda entrypoint, *args, **kwargs: 'do')
    module = load_evaluator()
    cases, output = tmp_path / 'cases.json', tmp_path / 'result.json'
    cases.write_text(json.dumps(fixture([part('do', '今天晴天')], text='今天晴天')), encoding='utf-8')
    assert module.main(['--cases', str(cases), '--output', str(output),
        '--policy', 'route=@1', '--policy', 'route=@9501']) == 0
    result = json.loads(output.read_text(encoding='utf-8'))
    assert result['metrics']['intent_set_accuracy']['accuracy'] == 1
    assert result['cases'][0]['predicted_parts'][0]['intent'] == 'do'
    assert version('route') == '@1'
    with pytest.raises(SystemExit):
        module.main(['--cases', str(tmp_path / 'missing.json'), '--policy', 'route=@9501'])
    assert version('route') == '@1'


def test_memory_cli_repeated_overrides_restore_after_real_evaluation(tmp_path):
    from tools import memory_eval
    output = tmp_path / 'memory.json'
    observed = []

    @register('rank', '@9502')
    def rank(value):
        observed.append(value)
        return get('rank', version='@1')(value)

    @register('rank', '@9503')
    def fail(value):
        raise ValueError('synthetic evaluation policy failure')

    memory_eval.main(['--output', str(output), '--label', 'T12-11-test',
        '--policy', 'rank=@1', '--policy', 'enough=@1', '--policy', 'rank=@9502'])
    result = json.loads(output.read_text(encoding='utf-8'))
    assert result['fixture_counts']['questions'] == 72
    assert result['model_attempts'] == 0
    assert observed
    assert version('rank') == version('enough') == '@1'
    with pytest.raises(SystemExit):
        memory_eval.main(['--label', 'invalid label', '--policy', 'rank=@1'])
    assert version('rank') == '@1'
    with pytest.raises(SystemExit):
        memory_eval.main(['--output', str(tmp_path / 'failed.json'), '--policy', 'rank=@9503'])
    assert version('rank') == '@1'
