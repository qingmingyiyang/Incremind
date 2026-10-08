from copy import deepcopy
import json

import pytest

from tools.outcome_eval import evaluate


def annotated():
    return {'schema_version': 1, 'cases': [
        {'id': 'continue', 'partition': 'calibration', 'project_id': 'p',
         'outcomes': [{'key': 'old'}], 'expected': {'continue_from': 'old',
          'protected_paragraphs': ['保留用户改过的句子。'],
          'placements': [{'kind': 'updated', 'path': ['手册', '检查'], 'required_text': '检查电源。'}]}},
        {'id': 'new', 'partition': 'heldout', 'project_id': 'p',
         'outcomes': [], 'expected': {'continue_from': None,
          'placements': [{'kind': 'added', 'path': ['预算', '支出'], 'required_text': '本月物业费。'}]}},
    ]}


def delivered():
    return [
        {'id': 'continue', 'document_id': 'new-1', 'selected_key': 'old',
         'markdown': '# 手册\n\n## 检查\n\n保留用户改过的句子。\n\n检查电源。\n',
         'changes': [{'kind': 'updated', 'path': ['手册', '检查']}]},
        {'id': 'new', 'document_id': 'new-2', 'selected_key': None,
         'markdown': '# 预算\n\n## 支出\n\n本月物业费。\n', 'changes': []},
    ]


def test_missing_or_failed_delivery_stays_in_fixed_denominators():
    fixture = annotated()
    fixture['evaluation_notes'] = '开发已观察本固定集，不作为独立盲测。'
    result = evaluate(fixture, delivered()[:1])
    assert result['evaluation_notes'] == fixture['evaluation_notes']
    assert result['overall']['selection'] == {'hits': 1, 'count': 2, 'accuracy': .5}
    assert result['overall']['placement'] == {'hits': 1, 'count': 2, 'accuracy': .5}
    assert result['partitions']['heldout']['selection']['hits'] == 0
    assert result['partitions']['heldout']['new_placement']['count'] == 1
    failed = delivered()
    failed[1]['error'] = 'timeout'
    assert evaluate(annotated(), failed)['overall']['selection']['hits'] == 1


def test_real_heading_body_and_receipt_both_determine_continuation_credit():
    rows = delivered()
    result = evaluate(annotated(), rows)
    assert result['overall']['placement']['hits'] == 2
    rows[0]['markdown'] = '# 手册\n\n## 检查\n\n保留用户改过的句子。\n\n## 其它\n\n检查电源。\n'
    assert evaluate(annotated(), rows)['overall']['placement']['hits'] == 1
    rows = delivered()
    rows[0]['changes'][0]['path'] = ['手册', '其它']
    assert evaluate(annotated(), rows)['overall']['placement']['hits'] == 1


def test_fenced_fake_heading_and_duplicate_paths_do_not_earn_credit():
    rows = delivered()
    rows[0]['markdown'] = '# 手册\n\n```text\n## 检查\n检查电源。\n```\n\n## 其它\n\n保留用户改过的句子。\n'
    assert evaluate(annotated(), rows)['overall']['placement']['hits'] == 1
    rows[0]['markdown'] = '# 手册\n\n## 检查\n\n保留用户改过的句子。\n\n检查电源。\n\n## 检查\n\n第二处。\n'
    assert evaluate(annotated(), rows)['overall']['placement']['hits'] == 1


def test_wrong_selection_or_lost_user_paragraph_fails_placement():
    rows = delivered()
    rows[0]['selected_key'] = 'other'
    result = evaluate(annotated(), rows)
    assert result['overall']['selection']['hits'] == 1
    assert result['overall']['continuation_placement']['hits'] == 0
    rows = delivered()
    rows[0]['markdown'] = rows[0]['markdown'].replace('保留用户改过的句子。', '改写后的句子。')
    assert evaluate(annotated(), rows)['overall']['continuation_placement']['hits'] == 0


def test_unknown_or_duplicate_observations_are_rejected():
    with pytest.raises(ValueError, match='duplicate'):
        evaluate(annotated(), delivered() + [deepcopy(delivered()[0])])
    extra = deepcopy(delivered()[0])
    extra['id'] = 'unknown'
    with pytest.raises(ValueError, match='unknown'):
        evaluate(annotated(), delivered() + [extra])


def test_added_heading_requires_annotated_predecessor():
    fixture = annotated()
    fixture['cases'][0]['expected']['placements'][0].update(kind='added', after_path=['手册', '准备'])
    rows = delivered()
    rows[0]['changes'][0]['kind'] = 'added'
    rows[0]['markdown'] = '# 手册\n\n## 准备\n\n准备材料。\n\n## 检查\n\n保留用户改过的句子。\n\n检查电源。\n'
    assert evaluate(fixture, rows)['overall']['continuation_placement']['hits'] == 1
    rows[0]['markdown'] = rows[0]['markdown'].replace('## 准备', '## 其它')
    assert evaluate(fixture, rows)['overall']['continuation_placement']['hits'] == 0


def test_cli_scores_observation_file_and_preserves_missing_case(tmp_path, capsys):
    from tools.outcome_eval import main
    cases, observations, output = (tmp_path / name for name in ('cases.json', 'observations.json', 'report.json'))
    cases.write_text(json.dumps(annotated(), ensure_ascii=False), encoding='utf-8')
    observations.write_text(json.dumps(delivered()[:1], ensure_ascii=False), encoding='utf-8')
    assert main(['--cases', str(cases), '--observations', str(observations), '--output', str(output)]) == 0
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['mode'] == 'observations'
    assert report['overall']['selection'] == {'hits': 1, 'count': 2, 'accuracy': .5}
    assert report['overall']['placement']['count'] == 2
    assert json.loads(capsys.readouterr().out)['case_count'] == 2


def test_cli_rejects_unknown_observation_before_writing_report(tmp_path, capsys):
    from tools.outcome_eval import main
    cases, observations, output = (tmp_path / name for name in ('cases.json', 'observations.json', 'report.json'))
    cases.write_text(json.dumps(annotated()), encoding='utf-8')
    observations.write_text(json.dumps([{'id': 'unknown'}]), encoding='utf-8')
    with pytest.raises(SystemExit) as error:
        main(['--cases', str(cases), '--observations', str(observations), '--output', str(output)])
    assert error.value.code == 2
    assert 'unknown observation' in capsys.readouterr().err
    assert not output.exists()


def test_cli_collects_real_per_case_files_and_keeps_failed_case_in_denominator(tmp_path):
    from tools.outcome_eval import main
    cases, output = tmp_path/'cases.json', tmp_path/'report.json'
    observations = tmp_path/'pipeline-observations'
    observations.mkdir()
    cases.write_text(json.dumps(annotated()), encoding='utf-8')
    (observations/'complete.json').write_text(json.dumps(delivered()[0]), encoding='utf-8')
    failed = {'id':annotated()['cases'][1]['id'], 'error':'AssertionError'}
    (observations/'failed.json').write_text(json.dumps(failed), encoding='utf-8')
    assert main(['--cases',str(cases),'--observations',str(observations),'--output',str(output)]) == 0
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['overall']['selection'] == {'hits':1, 'count':2, 'accuracy':.5}
    assert report['overall']['placement']['count'] == 2
    assert report['cases'][1]['delivery_available'] is False


def test_cli_empty_observation_directory_keeps_all_expected_cases(tmp_path):
    from tools.outcome_eval import main
    cases, output = tmp_path/'cases.json', tmp_path/'report.json'
    observations = tmp_path/'empty'
    observations.mkdir()
    cases.write_text(json.dumps(annotated()), encoding='utf-8')
    assert main(['--cases',str(cases),'--observations',str(observations),'--output',str(output)]) == 0
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['overall']['selection'] == {'hits':0, 'count':2, 'accuracy':0}


def test_final_fallback_does_not_erase_wrong_original_selection():
    from tools.outcome_eval import observed_selection
    selection = {'document_id':'previous'}
    execution = {'outcome_selection':selection,
        'request':{'input':{'text':json.dumps({'outcome_selection':selection})}}}
    row = delivered()[1]
    row.update(selected_key=observed_selection(execution, {'old':'previous'}),
        continues=None, fallback_new=True)
    report = evaluate(annotated(), [row])
    assert report['cases'][1]['delivery_available'] is True
    assert report['cases'][1]['selection_hit'] is False


def test_observed_selection_requires_saved_request_agreement_and_known_alias():
    from tools.outcome_eval import observed_selection
    selection = {'document_id':'previous'}
    execution = {'outcome_selection':selection,
        'request':{'input':{'text':json.dumps({'outcome_selection':selection})}}}
    with pytest.raises(ValueError):
        observed_selection(execution, {'other':'different'})
    execution['outcome_selection'] = None
    with pytest.raises(ValueError):
        observed_selection(execution, {'old':'previous'})


def test_policy_evidence_comes_from_saved_request_even_for_new_write():
    from tools.outcome_eval import observed_policies
    execution = {'request':{'input':{'text':json.dumps({'outcome_input':{
        'continuation_policy':'@1', 'document_id':None}})}}}
    assert observed_policies(execution) == {'continuation':'@1'}
    execution['request']['input']['text'] = json.dumps({'outcome_input':{
        'continuation_policy':'@unknown','document_id':None}})
    with pytest.raises(ValueError):
        observed_policies(execution)


def test_cli_policy_requires_actual_observation_evidence_and_keeps_missing_case(tmp_path):
    from tools.outcome_eval import main
    cases, observations, output = (tmp_path/name for name in ('cases.json','observations.json','report.json'))
    cases.write_text(json.dumps(annotated()), encoding='utf-8')
    row = {**delivered()[0], 'policies':{'continuation':'@1'}}
    observations.write_text(json.dumps([row]), encoding='utf-8')
    assert main(['--cases',str(cases),'--observations',str(observations),
        '--policy','continuation=@1','--output',str(output)]) == 0
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['policy_versions'] == {'continuation':'@1'}
    assert report['overall']['selection'] == {'hits':1,'count':2,'accuracy':.5}


@pytest.mark.parametrize('evidence', [None, {'continuation':'@wrong'}])
def test_cli_policy_refuses_missing_or_different_producer_version(tmp_path, evidence, capsys):
    from tools.outcome_eval import main
    cases, observations, output = (tmp_path/name for name in ('cases.json','observations.json','report.json'))
    cases.write_text(json.dumps(annotated()), encoding='utf-8')
    observations.write_text(json.dumps([{**delivered()[0], 'policies':evidence}]), encoding='utf-8')
    with pytest.raises(SystemExit) as error:
        main(['--cases',str(cases),'--observations',str(observations),
            '--policy','continuation=@1','--output',str(output)])
    assert error.value.code == 2
    assert 'policy evidence' in capsys.readouterr().err
    assert not output.exists()


def test_added_heading_after_anchor_subtree_matches_real_patch_position():
    from backend.memory_app.v2.outcome_patches import apply_patch
    fixture, rows = annotated(), delivered()
    previous = '# 手册\n\n## 准备\n\n准备材料。\n\n### 工具\n\n备好工具。\n\n## 后续\n\n再检查。\n'
    fixture['cases'][0]['outcomes'][0]['markdown'] = previous
    fixture['cases'][0]['expected'].update(protected_paragraphs=[], placements=[{
        'kind': 'added', 'path': ['手册', '检查'], 'after_path': ['手册', '准备'], 'required_text': '检查电源。'}])
    result = apply_patch(previous, previous, [{'kind': 'add', 'after_path': ['手册', '准备'],
                                              'level': 2, 'title': '检查', 'body': '检查电源。'}])
    rows[0].update(result)
    assert evaluate(fixture, rows)['overall']['continuation_placement']['hits'] == 1


@pytest.mark.parametrize('rewrite', ['inline', 'other_section'])
def test_protected_paragraph_requires_original_boundary_and_title_path(rewrite):
    fixture, rows = annotated(), delivered()
    previous = '# 手册\n\n## 检查\n\n保留用户改过的句子。\n\n## 其它\n\n普通段落。\n'
    fixture['cases'][0]['outcomes'][0].update(markdown=previous, user_markdown=previous)
    if rewrite == 'inline':
        rows[0]['markdown'] = '# 手册\n\n## 检查\n\n保留用户改过的句子。但实际需要反过来。\n\n检查电源。\n'
    else:
        rows[0]['markdown'] = '# 手册\n\n## 检查\n\n检查电源。\n\n## 其它\n\n保留用户改过的句子。\n'
    assert evaluate(fixture, rows)['overall']['continuation_placement']['hits'] == 0


def test_added_child_before_existing_subtree_does_not_count_as_after_anchor():
    fixture, rows = annotated(), delivered()
    previous = '# 手册\n\n## 准备\n\n### 工具\n\n备好工具。\n\n## 后续\n\n再检查。\n'
    fixture['cases'][0]['outcomes'][0]['markdown'] = previous
    fixture['cases'][0]['expected'].update(protected_paragraphs=[], placements=[{
        'kind': 'added', 'path': ['手册', '准备', '检查'], 'after_path': ['手册', '准备'],
        'required_text': '检查电源。'}])
    rows[0].update(markdown=previous.replace('### 工具', '### 检查\n\n检查电源。\n\n### 工具'),
                   changes=[{'kind': 'added', 'path': ['手册', '准备', '检查']}])
    assert evaluate(fixture, rows)['overall']['continuation_placement']['hits'] == 0


def test_protected_paragraphs_keep_their_original_order_in_one_section():
    fixture, rows = annotated(), delivered()
    previous = '# 手册\n\n## 检查\n\n用户先写的依据。\n\n用户后写的结论。\n'
    fixture['cases'][0]['outcomes'][0].update(markdown=previous, user_markdown=previous)
    fixture['cases'][0]['expected']['protected_paragraphs'] = ['用户先写的依据。', '用户后写的结论。']
    rows[0]['markdown'] = '# 手册\n\n## 检查\n\n用户后写的结论。\n\n用户先写的依据。\n\n检查电源。\n'
    assert evaluate(fixture, rows)['overall']['continuation_placement']['hits'] == 0
