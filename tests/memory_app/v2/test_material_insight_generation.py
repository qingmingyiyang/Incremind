"""隔离模型传输和材料分析读取，验证真实 Turn、资格和候选事务。"""
import json
from copy import deepcopy

import pytest

from backend.memory_app.v2 import insight_generation
from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.memory_app.v2.policies import ACTIVE, get, override, register
from backend.memory_app.v2.policies.extract_material import (
    MaterialExtractInput, MaterialExtractDecodeInput, v6, MAX_OUTPUT_CHARS, MAX_OUTPUT_ITEMS,
)
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import normalize_conditions
from tests.memory_app.v2.test_insight_generation import env
from tests.memory_app.v2.test_insight_retry_identity import set_run
from tests.memory_app.v2.test_source_sections import source_env, admit


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    try:
        get('extract', version='@6')
    except ValueError:
        register('extract', '@6')(v6)
    # 材料分析另一个实现任务负责；此处只隔离其读取边界。
    monkeypatch.setattr(insight_generation, '_read_material_analysis', lambda *args, **kwargs: None)
    monkeypatch.setattr(insight_generation, '_read_material_sources', source_proofs)


@pytest.fixture(autouse=True)
def adapter_capacity(monkeypatch):
    from tests.memory_app.v2.test_insight_generation import Model
    monkeypatch.setattr(Model, 'generation_budget_limits',
        lambda self, **kwargs: {'window': 128_000, 'reserve': 2000}, raising=False)


def row(number, *, claim_ids=(), text=None):
    return {'origin': 'body', 'kind': 'new_method', 'relation': 'new',
            'text': text or f'在证据不足时保留第{number}个角度及其限制，不要为了简短省略影响判断的适用条件与反例，并明确哪些情境尚未验证。',
            'conditions': ['遇到同类判断时'], 'target_id': None, 'scope_hint': None,
            'claim_ids': list(claim_ids)}


def generate(env, *, version='@6', **kwargs):
    with override(extract=version):
        return insight_generation.generate_insights(env.model, env.service, env.documents,
            'alpha', env.doc, **kwargs)


def answer(env, rows):
    env.model.response = json.dumps({'complete': True, 'insights': rows, 'supports': []}, ensure_ascii=False)


def source_proofs(records, documents, project_id, document_id, *, revision=None, reader=None):
    # 此处隔离实际分析owner；正文/评论范围取自真实捕获记录，生成stage身份只作合同输入。
    from backend.memory_app.v2.source_sections import resolve_comment_sources
    reader = reader or records
    document = reader.read('documents', document_id).payload
    item_id = next(ref['source_id'] for ref in document['source_refs']
        if ref.get('locator') == 'workspace://' + str(ref.get('source_id')))
    item = reader.read('workspace_items', item_id)
    section = reader.read('v2_original_sections', item_id)
    section = section if section is not None and section.payload.get('state') != 'unbound' else None
    body = {'start': 0, 'end': len(item.payload['source_text'])}
    if section is not None:
        body = resolve_comment_sources(records, documents, project_id, document_id,
            revision=revision, reader=reader)['sources'][0]['body']
    return {'body_sources': [{'source_type': 'original_item', 'source_id': item_id,
        'project_id': project_id, 'revision': item.revision, 'generation_revision': item.revision,
        'coordinate_space': 'workspace_source_text_v1', 'text': item.payload['source_text'], 'body': body,
        'section_binding': {'id': section.object_id, 'revision': section.revision} if section else None}],
        'analysis_generation_refs': [{'collection': 'workspace_organize_steps',
            'id': 'isolated-material-analysis', 'revision': 1, 'turn_id': 'isolated-analysis-turn'}],
        'original_material_refs': [{'type': 'original_item', 'id': item_id,
            'revision': item.revision, 'project_id': project_id}]}


def analysis_for(env):
    owner = env.records.list('workspace_items')[0]
    return {'document': {'id': env.doc, 'revision': 1},
        'analysis': {'plan': {'angles': [{'id': 'angle-1', 'goal': '适用条件'}]},
            'claims': [{'id': 'claim-1', 'angle_ids': ['angle-1'], 'text': '原文证据',
                'epistemic_status': 'reported_fact', 'actors': [], 'entities': [], 'event_status': 'unknown',
                'evidence': [{'quote': '原文证据', 'start': 0, 'end': 4,
                    'source_type': 'original_item', 'source_id': owner.object_id,
                    'revision': owner.revision, 'coordinate_space': 'workspace_source_text_v1'}]}],
            'temporal': []},
        'bindings': [{'document_id': env.doc, 'document_revision': 1,
            'publication_id': 'confirm-' + owner.object_id, 'publication_revision': 1,
            'origin_item_id': owner.object_id, 'origin_item_revision': owner.revision}],
        'analysis_ref': {'collection': 'v2_material_analysis_publications',
            'id': 'confirm-' + owner.object_id, 'revision': 1},
        **source_proofs(env.records, env.documents, 'alpha', env.doc, revision=1)}


def test_dynamic_candidates_keep_more_than_three_complete_bodies_and_ordered_replay(env):
    answer(env, [row(n) for n in range(1, 13)])
    original_active = dict(ACTIVE)
    result = generate(env)
    assert len(result) == 12 and all(len(value['text']) > 40 for value in result)
    assert [value['id'] for value in result] == [f'candidate-v2-{env.doc}-r1-{n}' for n in range(1, 13)]
    assert all(value['state'] == 'pending' and value['scene'] == '阅读' for value in result)
    assert env.records.list('recognitions') == ()
    assert generate(env) == result and env.model.calls == 1
    assert ACTIVE == original_active
    turn = env.records.list('v2_memory_turn_keys')[0]
    marker = MemoryTurn.store_for(env.records).get_immutable_payload(turn.object_id, 'memory-insights-applied-v1')[1]
    assert marker['candidate_ids'] == [value['id'] for value in result]
    assert marker['proposal_keys'] == [f'candidate-{n}' for n in range(1, 13)]
    sent = json.loads(env.model.messages[-1]['content'])
    assert sent['output_budget']['max_tokens'] >= 4096
    assert '一次性事实、事件' in env.model.messages[0]['content']


def test_empty_completed_batch_is_replayed_without_new_model_or_manifest_alias(env):
    answer(env, [])
    assert generate(env) == generate(env) == []
    assert env.model.calls == 1 and env.records.list('recognition_candidates') == ()
    turn = env.records.list('v2_memory_turn_keys')[0]
    store = MemoryTurn.store_for(env.records)
    assert store.get_immutable_payload(turn.object_id, 'memory-insights-plan-v2')[1]['candidate_ids'] == []
    assert store.get_immutable_payload(turn.object_id, 'memory-insights-applied-v1')[1]['proposal_keys'] == []


def test_incomplete_output_is_preserved_without_candidates_applied_or_automatic_retry(env):
    output = {'complete': False, 'insights': [row(1)], 'supports': []}
    env.model.response = json.dumps(output, ensure_ascii=False)
    errors = []
    assert generate(env, retry_token='first', on_error=errors.append) == []
    assert 'insight_extraction_incomplete' in errors
    turn = env.records.list('v2_memory_turn_keys')[0]
    store = MemoryTurn.store_for(env.records)
    assert store.get_immutable_payload(turn.object_id, 'memory-generation-output-v1')[1]['output'] == output
    assert store.get_immutable_payload(turn.object_id, 'memory-insights-applied-v1') is None
    assert store.get_immutable_payload(turn.object_id, 'memory-insights-plan-v2') is None
    assert env.records.list('recognition_candidates') == ()
    assert generate(env) == generate(env, retry_token='explicit-retry') == []
    assert env.model.calls == 1


@pytest.mark.parametrize('value', [None, 'true', 1])
def test_complete_must_be_explicit_boolean(env, value):
    output = {'insights': [row(1)], 'supports': []}
    if value is not None:
        output['complete'] = value
    env.model.response = json.dumps(output, ensure_ascii=False)
    assert generate(env) == [] and env.records.list('recognition_candidates') == ()


def test_analysis_claims_and_original_qualification_are_frozen_in_real_turn(env, monkeypatch):
    snapshot = analysis_for(env)
    monkeypatch.setattr(insight_generation, '_read_material_analysis', lambda *a, **k: deepcopy(snapshot))
    answer(env, [row(n, claim_ids=['claim-1']) for n in range(1, 6)])
    result = generate(env)
    assert len(result) == 5 and env.records.list('recognitions') == ()
    turn = env.records.list('v2_memory_turn_keys')[0]
    inputs = env.records.read('v2_extract_inputs', turn.object_id).payload
    assert inputs['material_analysis'] == snapshot
    refs = turn.payload['request']['privacy']['material_refs']
    assert snapshot['original_material_refs'][0] in refs
    assert all(value['type'] != 'v2_material_analysis_publications' for value in refs)
    proof = env.records.read('v2_material_candidate_evidence', result[-1]['id']).payload
    assert proof['analysis_ref'] == snapshot['analysis_ref']
    assert proof['evidence'] == snapshot['analysis']['claims'][0]['evidence']
    assert proof['claim_ids'] == ['claim-1']
    assert generate(env) == result and env.model.calls == 1


def test_comment_evidence_keeps_original_proofs_and_deduplicates_analysis_material_refs(source_env, monkeypatch):
    env = source_env
    admit(env)
    snapshot = analysis_for(env)
    monkeypatch.setattr(insight_generation, '_read_material_analysis', lambda *a, **k: deepcopy(snapshot))
    owner = env.records.read('workspace_items', env.item['id'])
    rows = [{'origin': 'comment', 'against': 'source_body', 'kind': 'differs',
        'relation': 'differs', 'text': f'根据第{n}个对照角度，周末营业状态可能与正文介绍不同，应按实际出行日期重新确认，不把评论当成已经核实的营业承诺。',
        'conditions': ['周末出行之前'], 'target_id': None, 'scope_hint': None,
        'comment': {'source_id': owner.object_id, 'revision': owner.revision, 'ordinal': 1,
            'quote': '周末已经关门 😀'}, 'body_quote': '营业到晚上。'} for n in range(1, 5)]
    answer(env, rows)
    result = generate(env)
    assert len(result) == 4 and all(value['state'] == 'pending' for value in result)
    turn = env.records.list('v2_memory_turn_keys')[0]
    refs = turn.payload['request']['privacy']['material_refs']
    assert refs.count(snapshot['original_material_refs'][0]) == 1
    proof = env.records.read('v2_comment_candidates', result[-1]['id']).payload
    assert proof['comment_source']['quote'] == '周末已经关门 😀'
    assert proof['comparison_source']['quote'] == '营业到晚上。'
    assert generate(env) == result and env.model.calls == 1


@pytest.mark.parametrize('change', ['body', 'binding', 'missing'])
def test_analysis_change_after_wire_never_proposes_a_candidate(env, monkeypatch, change):
    snapshot = analysis_for(env)
    current = [snapshot]
    monkeypatch.setattr(insight_generation, '_read_material_analysis', lambda *a, **k: deepcopy(current[0]))
    answer(env, [row(1, claim_ids=['claim-1'])])
    def modify():
        if change == 'body':
            current[0]['analysis']['claims'][0]['text'] = '变化后的正文'
        elif change == 'binding':
            current[0]['bindings'][0]['publication_revision'] = 2
        else:
            current[0] = None
    env.model.after = modify
    assert generate(env) == [] and env.model.calls == 1
    assert env.records.list('recognition_candidates') == ()


def test_final_candidate_transaction_rechecks_analysis_with_same_reader(env, monkeypatch):
    snapshot, changed, wire_finished = analysis_for(env), analysis_for(env), [False]
    changed['analysis_ref']['revision'] = 2
    def read(*args, reader=None, **kwargs):
        return deepcopy(changed if reader is not None and wire_finished[0] else snapshot)
    monkeypatch.setattr(insight_generation, '_read_material_analysis', read)
    env.model.after = lambda: wire_finished.__setitem__(0, True)
    answer(env, [row(1, claim_ids=['claim-1'])])
    assert generate(env) == []
    assert env.records.list('recognition_candidates') == ()


@pytest.mark.parametrize('ids', [[], ['invented'], ['claim-1', 'claim-1']])
def test_unverified_or_missing_claim_reference_cannot_become_completed_batch(env, monkeypatch, ids):
    snapshot = analysis_for(env)
    monkeypatch.setattr(insight_generation, '_read_material_analysis', lambda *a, **k: deepcopy(snapshot))
    answer(env, [row(1, claim_ids=ids)])
    assert generate(env) == [] and env.records.list('recognition_candidates') == ()
    turn = env.records.list('v2_memory_turn_keys')[0]
    assert MemoryTurn.store_for(env.records).get_immutable_payload(turn.object_id, 'memory-insights-applied-v1') is None


def test_partial_seven_candidate_commit_recovers_frozen_ids_and_proposal_keys(env, monkeypatch):
    answer(env, [row(n) for n in range(1, 8)])
    set_run(env, 'first')
    original = MemoryTurn.propose
    def interrupted(self, **kwargs):
        value = original(self, **kwargs)
        if kwargs['key'].split(':', 1)[0] == 'candidate-4':
            raise RuntimeError('synthetic interruption')
        return value
    monkeypatch.setattr(MemoryTurn, 'propose', interrupted)
    assert generate(env, retry_token='first', attempt=('user-turn', 'first')) == []
    assert len(env.records.list('recognition_candidates')) == 4
    monkeypatch.setattr(MemoryTurn, 'propose', original)
    set_run(env, 'retry')
    result = generate(env, retry_token='retry', attempt=('user-turn', 'retry'))
    assert len(result) == 7 and env.model.calls == 1
    turn = env.records.list('v2_memory_turn_keys')[0]
    store = MemoryTurn.store_for(env.records)
    manifest = store.get_immutable_payload(turn.object_id, 'memory-insights-plan-v2')[1]
    assert manifest['proposal_keys'] == [f'candidate-{n}:turn:user-turn:run:first' for n in range(1, 8)]
    assert all(store.get_immutable_payload(turn.object_id, 'memory-proposal-' + key) for key in manifest['proposal_keys'])
    assert generate(env, retry_token='retry', attempt=('user-turn', 'retry')) == result


def test_published_alias_never_replaces_manifest_candidate_identity(env):
    answer(env, [row(n) for n in range(1, 6)])
    original = generate(env)
    published = env.service.publish(scope=env.scope, candidate_id=original[-1]['id'], expected_revision=1, reviewer='local-user')
    replay = generate(env)
    assert len(replay) == 5 and replay[-1]['id'] == published.id
    assert replay[-1]['kind'] == 'recognition' and env.model.calls == 1
    turn = env.records.list('v2_memory_turn_keys')[0]
    marker = MemoryTurn.store_for(env.records).get_immutable_payload(turn.object_id, 'memory-insights-applied-v1')[1]
    assert marker['candidate_ids'][-1] == original[-1]['id']


@pytest.mark.parametrize('kind', ['privacy', 'source'])
def test_existing_source_and_privacy_guards_still_fence_new_policy(env, kind):
    answer(env, [row(1)])
    def change():
        if kind == 'privacy':
            set_private_project(env.records, 'alpha', True, 0)
        else:
            source = env.records.list('recognition_experiences')[0]
            env.service.revoke_experience(scope=env.scope, experience_id=source.object_id, expected_revision=source.revision)
    env.model.after = change
    assert generate(env) == [] and env.records.list('recognition_candidates') == ()


@pytest.mark.parametrize('kind', ['characters', 'items', 'truncated'])
def test_resource_or_truncated_output_is_not_applied_and_can_retry_known_rejection(env, kind):
    if kind == 'characters':
        answer(env, [row(1, text='文' * MAX_OUTPUT_CHARS)])
    elif kind == 'items':
        answer(env, [row(n) for n in range(MAX_OUTPUT_ITEMS + 1)])
    else:
        env.model.response = '{"insights":['
    assert generate(env, retry_token='first') == []
    turn = env.records.list('v2_memory_turn_keys')[0]
    assert MemoryTurn.store_for(env.records).get_immutable_payload(turn.object_id, 'memory-insights-applied-v1') is None
    assert env.records.list('recognition_candidates') == ()
    answer(env, [row(n) for n in range(1, 5)])
    assert len(generate(env, retry_token='retry')) == 4 and env.model.calls == 2


@pytest.mark.parametrize('old_version', ['@1', '@2', '@3', '@4', '@5'])
def test_old_episode_keeps_original_three_row_behavior_when_new_version_requested(env, monkeypatch, old_version):
    rows = [{'text': '旧' * 41, 'conditions': ['实际需要时']}] + [
        {'text': f'方法{n}', 'conditions': ['实际需要时']} for n in range(1, 6)]
    env.model.response = json.dumps({'insights': rows})
    result = generate(env, version=old_version)
    assert len(result) == 3
    assert [value['text'] for value in result] == ['方法1', '方法2', '方法3']
    def forbidden(*args, **kwargs):
        raise AssertionError('旧轮次不应读取新增分析')
    monkeypatch.setattr(insight_generation, '_read_material_analysis', forbidden)
    assert generate(env) == result and env.model.calls == 1
    turn = env.records.list('v2_memory_turn_keys')[0]
    assert turn.payload['request']['policy_versions']['extract'] == old_version
    if old_version != '@1':
        assert 'material_analysis' not in env.records.read('v2_extract_inputs', turn.object_id).payload


def test_pure_budget_grows_with_material_and_resource_overflow_is_explicit():
    short = MaterialExtractInput([{'content': '短材料'}], '约束')
    long = MaterialExtractInput([{'content': '很长材料' * 3000}], '约束')
    assert v6.budget(short)['max_tokens'] < v6.budget(long)['max_tokens'] <= 16000
    output = {'complete': True, 'insights': [row(1, text='文' * MAX_OUTPUT_CHARS)], 'supports': []}
    decoded = v6.decide(MaterialExtractDecodeInput(output, normalize_conditions))
    assert not decoded.valid and decoded.errors == ('insight_resource_limit',)


def test_reaching_item_safety_ceiling_is_not_claimed_as_complete_coverage():
    output = {'complete': True, 'insights': [row(1)] * MAX_OUTPUT_ITEMS, 'supports': []}
    decoded = v6.decide(MaterialExtractDecodeInput(output, normalize_conditions))
    assert not decoded.valid and decoded.errors == ('insight_resource_limit',)


def test_actual_fast_model_capacity_limits_complete_structured_input(env, monkeypatch):
    import litellm
    from backend.memory_app.model_config import ModelConfiguration
    from backend.security.secrets import InMemorySecretStore
    from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
    from backend.shared.llm.message_metadata import _estimate_input_tokens, _build_prompt_fallback_messages
    from backend.memory_app.v2.material_insights import MaterialOutput
    from tests.memory_app.test_fast_generation import choose
    calls, projections = [], []
    snapshot = analysis_for(env)
    snapshot['analysis']['claims'][0]['text'] = '材料中的完整分析结论。' * 1800
    monkeypatch.setattr(insight_generation, '_read_material_analysis', lambda *a, **k: deepcopy(snapshot))
    output = {'complete': True, 'insights': [row(1, claim_ids=['claim-1'])], 'supports': []}
    def wire(**request):
        calls.append(request)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}],
            'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}
    def gateway(**kwargs):
        # 仅设置模型容量，仍由实际网关执行硬预算、结构化协议和回执。
        kwargs['context_window_tokens'] = 32768 if kwargs['model'] == 'quick' else 128000
        return LiteLLMCompletionGateway(**kwargs)
    env.model = ModelConfiguration(env.records, env.records.database_path.parent,
        InMemorySecretStore(), completion_fn=wire, gateway_factory=gateway)
    env.model.update('generation', {'base_url': 'https://synthetic.invalid/v1', 'model': 'main',
        'api_key': 'synthetic-only', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    choose(env.model, 'quick')
    reader = ModelConfiguration.generation_budget_limits
    def observe(self, **kwargs):
        projections.append(self.public()['generation']['model'])
        return reader(self, **kwargs)
    monkeypatch.setattr(ModelConfiguration, 'generation_budget_limits', observe)
    result = generate(env)
    assert len(result) == 1 and len(calls) == 1 and calls[0]['model'] == 'openai/quick'
    assert set(projections) == {'quick'}
    turn = env.records.list('v2_memory_turn_keys')[0]
    store = MemoryTurn.store_for(env.records)
    frozen = env.records.read('v2_material_extract_budgets', turn.object_id).payload
    assert frozen['binding']['model'] == frozen['configuration']['model'] == 'quick'
    assert frozen['limits'] == {'window': 32768, 'reserve': 2000}
    assert frozen['output_budget']['max_tokens'] == calls[0]['max_tokens'] < 16000
    assert frozen['input_tokens'] + calls[0]['max_tokens'] <= 32768
    actual = calls[0]['messages']
    sent = next(json.loads(message['content']) for message in actual if message['content'].startswith('{"experiences"'))
    assert sent['output_budget'] == frozen['output_budget']
    framed = _build_prompt_fallback_messages(messages=actual[-2:], response_model=MaterialOutput, validation_error=None)
    assert frozen['input_tokens'] >= _estimate_input_tokens(framed)
    assert generate(env) == result and len(calls) == 1


@pytest.mark.parametrize('limits', [None, {'window': 0, 'reserve': 2000}, {'window': 32768, 'reserve': True}])
def test_missing_or_invalid_model_capacity_never_dispatches(env, monkeypatch, limits):
    monkeypatch.setattr(env.model, 'generation_budget_limits', lambda **kwargs: limits)
    answer(env, [row(1)])
    errors = []
    assert generate(env, on_error=errors.append) == [] and env.model.calls == 0
    assert 'insight_budget_unavailable' in errors and env.records.list('recognition_candidates') == ()


def test_full_framing_budget_insufficiency_is_frozen_without_wire(env, monkeypatch):
    monkeypatch.setattr(env.model, 'generation_budget_limits', lambda **kwargs: {'window': 2500, 'reserve': 2000})
    answer(env, [row(1)])
    errors = []
    assert generate(env, on_error=errors.append) == [] and env.model.calls == 0
    assert 'insight_budget_insufficient' in errors
    turn = env.records.list('v2_memory_turn_keys')[0]
    store = MemoryTurn.store_for(env.records)
    frozen = env.records.read('v2_material_extract_budgets', turn.object_id).payload
    assert frozen['error'] == 'insight_budget_insufficient'
    assert frozen['input_tokens'] > frozen['limits']['window'] - frozen['limits']['reserve']
    assert store.get_immutable_payload(turn.object_id, 'memory-generation-output-v1') is None
    assert store.get_immutable_payload(turn.object_id, 'memory-insights-applied-v1') is None
    assert generate(env) == [] and env.model.calls == 0


def test_capacity_change_after_dispatch_cannot_propose(env, monkeypatch):
    limits = {'window': 32768, 'reserve': 2000}
    monkeypatch.setattr(env.model, 'generation_budget_limits', lambda **kwargs: dict(limits))
    env.model.after = lambda: limits.update(window=64000)
    answer(env, [row(1)])
    assert generate(env) == [] and env.model.calls == 1
    assert env.records.list('recognition_candidates') == ()


def test_pure_policy_uses_remaining_capacity_without_model_dependency():
    request = MaterialExtractInput([{'content': '材料' * 10000}], '约束')
    projected = v6.budget(request, limits={'window': 32768, 'reserve': 2000}, input_tokens=30000)
    assert projected['max_tokens'] == 2768
    with pytest.raises(ValueError, match='insight_budget_insufficient'):
        v6.budget(request, limits={'window': 32768, 'reserve': 2000}, input_tokens=31000)


def test_comment_claim_cannot_become_body_new_method(source_env, monkeypatch):
    env = source_env
    admit(env)
    snapshot = analysis_for(env)
    source = snapshot['body_sources'][0]
    quote = '周末已经关门 😀'
    start = source['text'].index(quote)
    snapshot['analysis']['claims'][0]['evidence'][0].update(
        quote=quote, start=start, end=start + len(quote))
    monkeypatch.setattr(insight_generation, '_read_material_analysis', lambda *a, **k: deepcopy(snapshot))
    answer(env, [row(1, claim_ids=['claim-1'])])
    errors = []
    assert generate(env, on_error=errors.append) == [] and env.model.calls == 1
    assert 'insight_invalid_output' in errors and env.records.list('recognition_candidates') == ()
    turn = env.records.list('v2_memory_turn_keys')[0]
    assert MemoryTurn.store_for(env.records).get_immutable_payload(turn.object_id, 'memory-insights-applied-v1') is None


def test_comment_document_without_analysis_cannot_promote_body(source_env):
    env = source_env
    admit(env)
    answer(env, [row(1)])
    assert generate(env) == [] and env.model.calls == 1
    assert env.records.list('recognition_candidates') == ()


@pytest.mark.parametrize('field', ['body', 'generation_revision', 'generation_refs', 'section_binding', 'bool_revision'])
def test_self_declared_source_proof_does_not_replace_independent_owner_read(env, monkeypatch, field):
    snapshot = analysis_for(env)
    source = snapshot['body_sources'][0]
    if field == 'body':
        source['body']['start'] += 1
    elif field == 'generation_refs':
        snapshot['analysis_generation_refs'][0]['turn_id'] = 'unrelated-turn'
    elif field == 'section_binding':
        source['section_binding'] = {'id': 'unrelated-section', 'revision': 1}
    elif field == 'bool_revision':
        snapshot['analysis_generation_refs'][0]['revision'] = True
    else:
        source['generation_revision'] -= 1
    monkeypatch.setattr(insight_generation, '_read_material_analysis', lambda *a, **k: deepcopy(snapshot))
    answer(env, [row(1, claim_ids=['claim-1'])])
    assert generate(env) == [] and env.model.calls == 0
    assert env.records.list('recognition_candidates') == ()


@pytest.mark.parametrize('field', ['quote', 'start', 'revision', 'source_id', 'coordinate_space'])
def test_claim_proof_must_match_exact_source_generation_and_coordinates(env, monkeypatch, field):
    snapshot = analysis_for(env)
    proof = snapshot['analysis']['claims'][0]['evidence'][0]
    proof[field] = {'quote': '不在原文', 'start': 1, 'revision': proof['revision'] - 1,
        'source_id': 'unrelated-item', 'coordinate_space': 'document_markdown_v1'}[field]
    monkeypatch.setattr(insight_generation, '_read_material_analysis', lambda *a, **k: deepcopy(snapshot))
    answer(env, [row(1, claim_ids=['claim-1'])])
    assert generate(env) == [] and env.model.calls == 1
    assert env.records.list('recognition_candidates') == ()


def test_exact_verified_generation_revision_survives_later_item_state_revisions(env, monkeypatch):
    snapshot = analysis_for(env)
    snapshot['body_sources'][0]['generation_revision'] -= 1
    snapshot['analysis']['claims'][0]['evidence'][0]['revision'] -= 1
    proofs = {key: deepcopy(snapshot[key]) for key in
        ('body_sources', 'analysis_generation_refs', 'original_material_refs')}
    monkeypatch.setattr(insight_generation, '_read_material_sources', lambda *a, **k: deepcopy(proofs))
    monkeypatch.setattr(insight_generation, '_read_material_analysis', lambda *a, **k: deepcopy(snapshot))
    answer(env, [row(1, claim_ids=['claim-1'])])
    assert len(generate(env)) == 1 and env.model.calls == 1


def test_independent_source_proof_is_revalidated_in_candidate_transaction(env, monkeypatch):
    snapshot, wire_finished = analysis_for(env), [False]
    original = insight_generation._read_material_sources
    def read(*args, reader=None, **kwargs):
        value = original(*args, reader=reader, **kwargs)
        if wire_finished[0] and reader is not None:
            value['analysis_generation_refs'][0]['revision'] += 1
        return value
    monkeypatch.setattr(insight_generation, '_read_material_sources', read)
    monkeypatch.setattr(insight_generation, '_read_material_analysis', lambda *a, **k: deepcopy(snapshot))
    env.model.after = lambda: wire_finished.__setitem__(0, True)
    answer(env, [row(1, claim_ids=['claim-1'])])
    assert generate(env) == [] and env.model.calls == 1
    assert env.records.list('recognition_candidates') == ()


@pytest.mark.parametrize('change', [None, 'manifest_id', 'manifest_key', 'generation', 'experience', 'source_proof'])
def test_material_comment_consumer_helper_uses_frozen_generation_and_manifest(source_env, monkeypatch, change):
    from backend.memory_app.v2.material_insights import resolve_material_comment_candidate_inputs
    from backend.recognition import WorkScope
    env = source_env
    admit(env)
    snapshot = analysis_for(env)
    monkeypatch.setattr(insight_generation, '_read_material_analysis', lambda *a, **k: deepcopy(snapshot))
    owner = env.records.read('workspace_items', env.item['id'])
    comment = {'origin': 'comment', 'against': 'source_body', 'kind': 'differs', 'relation': 'differs',
        'text': '周末营业情况与正文不同，需要提前确认。', 'conditions': ['周末出行时'],
        'target_id': None, 'scope_hint': None, 'comment': {'source_id': owner.object_id,
            'revision': owner.revision, 'ordinal': 1, 'quote': '周末已经关门 😀'}, 'body_quote': '营业到晚上。'}
    answer(env, [comment])
    result = generate(env)
    assert len(result) == 1
    candidate = env.records.read('recognition_candidates', result[0]['id'])
    turn = env.records.list('v2_memory_turn_keys')[0]
    store = MemoryTurn.store_for(env.records)
    arguments = {'turn_id': turn.object_id, 'request': turn.payload['request'],
        'generated': store.get_immutable_payload(turn.object_id, 'memory-generation-output-v1')[1],
        'comment_inputs': env.records.read('v2_comment_extract_inputs', turn.object_id).payload,
        'comparative_inputs': env.records.read('v2_extract_inputs', turn.object_id).payload,
        'manifest': store.get_immutable_payload(turn.object_id, 'memory-insights-plan-v2')[1]}
    if change == 'manifest_id':
        arguments['manifest']['candidate_ids'] = ['unrelated-candidate']
    elif change == 'manifest_key':
        arguments['manifest']['proposal_keys'] = ['candidate-2']
    elif change == 'generation':
        arguments['manifest']['generation_id'] = 'unrelated-generation'
    elif change == 'experience':
        arguments['request']['privacy']['material_refs'] = [ref for ref in
            arguments['request']['privacy']['material_refs'] if ref['type'] != 'experience']
    elif change == 'source_proof':
        snapshot['analysis_generation_refs'][0]['turn_id'] = 'unrelated-analysis-turn'
    scope = WorkScope('local-user', 'alpha')
    if change is not None:
        with pytest.raises(ValueError):
            resolve_material_comment_candidate_inputs(env.records, scope, candidate, **arguments)
    else:
        decoded = v6.decide(resolve_material_comment_candidate_inputs(env.records, scope, candidate, **arguments))
        assert decoded.valid and decoded.hints[0]['comment_source']['quote'] == '周末已经关门 😀'
        with env.records.begin() as tx:
            assert v6.decide(resolve_material_comment_candidate_inputs(tx, scope, candidate, **arguments)).valid
            tx.commit()
    assert env.model.calls == 1
