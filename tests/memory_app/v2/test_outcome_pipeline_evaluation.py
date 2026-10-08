"""固定人工补丁经过真实 HTTP、协调器、出生与成果链后才形成观测。"""
import asyncio
import json
import os
from pathlib import Path
import re
import time

import pytest

from backend.recognition import WorkScope
from backend.recognition.product_draft_dependencies import product_draft_source
from backend.memory_app.v2.policies import get, override
from tests.memory_app.v2.test_workbench_do import env as do_env
from tests.memory_app.v2.test_outcome_redos import scenario
from tools.outcome_eval import _outline, evaluate, observed_selection, observed_policies

ROOT = Path(__file__).parents[3]
FIXTURE = json.loads((ROOT / 'tests/fixtures/outcome_eval/cases.json').read_text(encoding='utf-8'))
PIPELINE_POLICY = os.environ.get('CHRIPTMAS_OUTCOME_EVAL_POLICY', '@1')
get('continuation', version=PIPELINE_POLICY)
PIPELINE_STYLE = os.environ.get('CHRIPTMAS_OUTCOME_EVAL_STYLE')
if PIPELINE_STYLE is not None:
    get('style', version=PIPELINE_STYLE)
PIPELINE_RUN = os.environ.get('CHRIPTMAS_OUTCOME_EVAL_RUN', '')
if PIPELINE_RUN and re.fullmatch(r'[a-z0-9-]{1,64}', PIPELINE_RUN, flags=re.ASCII) is None:
    raise ValueError('invalid outcome observation run')


def wait_completed(env, response, project):
    assert response.status_code == 200, response.text
    data = response.json()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        read = env.client.get('/api/v2/workbench/threads/'+data['thread_id'], params={'project_id':project})
        assert read.status_code == 200, read.text
        turn = next(item for item in read.json()['turns'] if item['id'] == data['turn']['id'])
        if turn['receipt']['do']['state'] in {'done','partial','failed'} and not env.state.workbench_tasks:
            return data, turn
        time.sleep(.02)
    raise AssertionError('真实成果没有在原夹具的90秒界限内终结')


def checked_birth(env, data, turn, project):
    receipt = turn['receipt']['do']
    assert receipt['state'] == 'done', receipt
    identity = receipt['document_id']
    born = product_draft_source(env.records, WorkScope('local-user',project), identity, 1)
    assert born.revisions['task_execution_id'] == data['turn']['id']
    assert born.revisions['product_turn_id'] == receipt['kernel_turn_id']
    row = env.records.read('v2_outcome_lineage', identity)
    assert row is not None and row.payload['turn_id'] == data['turn']['id']
    return identity


def tagged(project, scene, text):
    return f'#{project}/{scene} {text}' if scene else text


def annotated_completion(case):
    """只构造标注的外部模型响应；不选择成果、不调用补丁应用或文档写入。"""
    expected = case['expected']
    target = expected['continue_from']
    if target is None:
        headings, text = set(), ''
        for place in expected['placements']:
            for level, title in enumerate(place['path'], 1):
                prefix = tuple(place['path'][:level])
                if prefix not in headings:
                    text += '#' * level + ' ' + title + '\n\n'
                    headings.add(prefix)
            text += place['required_text']+'\n\n'
        return {'type':'complete','summary':text.strip()}
    old = next(row for row in case['outcomes'] if row['key'] == target)
    previous = old.get('user_markdown', old['markdown'])
    operations = []
    for place in expected['placements']:
        if place['kind'] == 'updated':
            matches = [row for row in _outline(previous) if row['path'] == place['path']]
            assert len(matches) == 1
            operations.append({'kind':'update','path':place['path'],
                'body':matches[0]['body'].rstrip()+'\n\n'+place['required_text']+'\n\n'})
        else:
            operations.append({'kind':'add','after_path':place['after_path'],
                'level':len(place['path']),'title':place['path'][-1],
                'body':place['required_text']+'\n\n'})
    return {'type':'complete','summary':'','patches':operations}


@pytest.mark.parametrize('case', FIXTURE['cases'], ids=lambda case:case['id'])
def test_annotated_case_through_actual_outcome_pipeline(scenario, case):
    env = scenario
    observation = {'id':case['id']}
    # 各保存版本使用独立观测目录，避免把旧策略和新策略拼成同一份报告。
    name = 'pipeline-observations' if PIPELINE_POLICY == '@1' else 'pipeline-observations-'+PIPELINE_POLICY[1:]
    if PIPELINE_STYLE is not None:
        name += '-style'+PIPELINE_STYLE[1:]
    if PIPELINE_RUN:
        # 不同生产修正的观测各自留存，完整报告只能使用同一轮的全部样本。
        name += '-'+PIPELINE_RUN
    observations_dir = ROOT / 'work/qa/T13.7' / name
    observations_dir.mkdir(parents=True, exist_ok=True)
    try:
        created = env.client.post('/api/v2/projects', json={'name':case['project_id']})
        assert created.status_code == 200, created.text
        project = created.json()['id']
        aliases, preserved = {}, {}
        for prior in case['outcomes']:
            env.summary = prior['markdown'].strip()
            data, turn = wait_completed(env, env.client.post('/api/v2/workbench/turns', json={
                'project_id':project, 'intent':'do', 'text':tagged(project,prior['scene'],prior['task_text']),
                'continue_from':None}), project)
            identity = checked_birth(env, data, turn, project)
            assert env.documents.markdown(identity, revision=1) == env.summary
            assert env.records.read('v2_outcome_lineage', identity).payload['version'] == 1
            body = prior.get('user_markdown',env.summary)
            saved = env.documents.save_user_edit(identity, markdown=body,
                title=prior['title'], expected_revision=env.documents.read(identity)['revision'])
            aliases[prior['key']] = identity
            preserved[identity] = (env.records.read('documents',identity), tuple(
                row for row in env.records.list('document_markdown')
                if row.object_id.startswith(identity+'~r')))
        new_materials = case['next']['new_material']
        intake = env.state.workspace_domains.intake
        materials = [asyncio.run(intake.add_text({'project_id':project,'text':text})) for text in new_materials]
        assert len(materials) == len(new_materials)
        provider = env.models.handler
        answer = annotated_completion(case)
        main_wires = []

        def external_response(messages, **options):
            context = json.loads(messages[-1]['content'])
            if any(row['capability_id'] == 'agent.list' for row in context.get('capabilities',[])):
                main_wires.append(messages)
                return json.dumps(answer, ensure_ascii=False)
            return provider(messages, **options)

        env.models.handler = external_response
        if PIPELINE_STYLE is not None:
            # 真实确认后再运行汇总；只替换外部响应，不替换写法资格或冻结服务。
            scope = WorkScope('local-user', project)
            service = env.state.recognition_service
            source = service.stage_experience(scope=scope, content='合成写法来源：阶段成果')
            candidate = service.propose(scope=scope, content='开头先列出结论。',
                source_experience_ids=[source])
            writing = service.publish(scope=scope, candidate_id=candidate.id,
                expected_revision=1, reviewer='local-user')
        task = case['next']['task_text']+'\n\n新材料：\n'+'\n'.join(new_materials)
        body = {'project_id':project,'intent':'do','text':tagged(project,case['next']['scene'],task)}
        explicit = case['next']['continue_from']
        if explicit is not None:
            body['continue_from'] = aliases[explicit]
        policies = {'continuation':PIPELINE_POLICY}
        if PIPELINE_STYLE is not None:
            policies['style'] = PIPELINE_STYLE
        with override(**policies):
            data, turn = wait_completed(env, env.client.post('/api/v2/workbench/turns',json=body), project)
        identity = checked_birth(env,data,turn,project)
        receipt = turn['receipt']['do']
        execution = env.records.read('v2_task_executions',data['turn']['id'])
        observation.update(document_id=identity, selected_key=observed_selection(execution.payload,aliases),
            markdown=env.documents.markdown(identity), changes=receipt.get('changes',[]),
            continues=receipt.get('continues'), fallback_new=receipt.get('fallback_new',False),
            policies=observed_policies(execution.payload))
        assert observation['policies']['continuation'] == PIPELINE_POLICY
        if PIPELINE_STYLE is not None:
            frozen_style = json.loads(execution.payload['request']['input']['text'])['style_input']
            assert observation['policies']['style'] == PIPELINE_STYLE
            assert frozen_style['selected'] == [{'id':writing.id, 'revision':writing.revision}]
            assert frozen_style['count'] == 1 and 0 < frozen_style['tokens'] <= 400
            assert frozen_style['text'] and all(any(
                message['role'] == 'system' and frozen_style['text'] in message['content']
                for message in messages) for messages in main_wires)
        assert all(text in execution.payload['request']['input']['text'] for text in new_materials)
        assert main_wires
        for old_id,(document,history) in preserved.items():
            assert env.records.read('documents',old_id) == document
            assert tuple(row for row in env.records.list('document_markdown')
                if row.object_id.startswith(old_id+'~r')) == history
    except Exception as error:
        observation['error'] = type(error).__name__
        raise
    finally:
        (observations_dir/(case['id']+'.json')).write_text(
            json.dumps(observation,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    # 质量断言与交付异常分开，落点错误保留已经正确选择的独立得分。
    result = evaluate(FIXTURE,[observation])
    row = next(row for row in result['cases'] if row['id'] == case['id'])
    assert row['selection_hit'] and row['placement_hits'] == row['placement_count'], row
