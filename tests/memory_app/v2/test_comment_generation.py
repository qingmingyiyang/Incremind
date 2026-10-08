"""Registered @3 through real capture/confirmation/MemoryTurn/candidate owners."""
import asyncio
import json

import pytest

from backend.memory_app.v2.insight_generation import generate_insights
from backend.memory_app.v2.policies import ACTIVE, override
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.original_sources import source_store
from tests.memory_app.v2.test_source_sections import source_env, admit
from tests.memory_app.v2.test_xhs_comment_intake import xhs_admitted
from tests.memory_app.v2.test_vision_intake import vision_env


@pytest.mark.parametrize('ordinal, expected', [(1, 1), (2, 0)])
def test_real_xhs_caller_uses_same_owner_quotes_and_does_not_invent_likes(xhs_admitted, ordinal, expected):
    env = xhs_admitted
    owner = env.records.read('workspace_items', env.item_id)
    response = {'insights': [{'origin': 'comment', 'against': 'source_body', 'kind': 'differs',
        'relation': 'differs', 'text': '周末可能关门，需提前确认', 'conditions': ['周末'],
        'target_id': None, 'scope_hint': None,
        'comment': {'source_id': env.item_id, 'revision': owner.revision, 'ordinal': ordinal,
            'quote': '周末已经关门 😀'}, 'body_quote': '营业到晚上。'}], 'supports': []}
    wires = []
    def provider(**request):
        wires.append(request)
        return {'choices': [{'message': {'content': json.dumps(response, ensure_ascii=False)}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 240, 'completion_tokens': 40}}
    env.model._completion_fn = provider
    result = generate(env)
    assert len(result) == expected
    assert len(wires) == 1
    assert env.records.list('recognitions') == () and env.records.list('recognition_relations') == ()
    if expected:
        marker = env.records.read('v2_comment_candidates', result[0]['id']).payload
        assert marker['comment_source']['id'] == env.item_id
        assert marker['comment_source']['ordinal'] == 1
        assert marker['comparison_source']['quote'] == '营业到晚上。'
        assert result[0]['state'] == 'pending'
        sent = json.loads(wires[0]['messages'][-1]['content'])
        assert [comment['ordinal'] for comment in sent['comment_sources'][0]['comments']] == [1]
        assert all(set(comment) == {'ordinal', 'start', 'end'} for comment in sent['comment_sources'][0]['comments'])


def output(env, *, ordinal=1, quote='周末已经关门 😀', target=None):
    row = env.records.read('workspace_items', env.item['id'])
    return {'insights': [{'origin': 'comment', 'against': 'recognition' if target else 'source_body',
        'kind': 'differs', 'relation': 'differs', 'text': '周末可能关门，需提前确认',
        'conditions': ['周末'], 'target_id': target, 'scope_hint': None,
        'comment': {'source_id': row.object_id, 'revision': row.revision,
            'ordinal': ordinal, 'quote': quote},
        'body_quote': None if target else '营业到晚上。'}], 'supports': []}


def generate(env):
    with override(extract='@3'):
        return generate_insights(env.model, env.service, env.documents, 'alpha', env.doc)


def test_real_registered_caller_freezes_l0_and_proposes_pending_without_fake_recognition_link(source_env):
    env = source_env
    admit(env)
    env.model.response = json.dumps(output(env), ensure_ascii=False)
    result = generate(env)
    assert len(result) == 1 and result[0]['state'] == 'pending'
    assert ACTIVE['extract'] == '@3'
    candidate = env.records.read('recognition_candidates', result[0]['id'])
    assert candidate.payload['source_experience_ids'] == [f'experience-legacy-{env.doc}-r1']
    hint = env.records.read('v2_candidate_hints', candidate.object_id).payload
    assert hint == {'project_id': 'alpha', 'relation': 'differs', 'target_id': None, 'scope_hint': None}
    marker = env.records.read('v2_comment_candidates', candidate.object_id).payload
    assert marker['comment_source']['id'] == env.item['id']
    assert marker['comment_source']['quote'] == '周末已经关门 😀'
    assert marker['comparison_source']['quote'] == '营业到晚上。'
    assert env.records.list('recognitions') == () and env.records.list('recognition_relations') == ()
    sent = json.loads(env.model.messages[-1]['content'])
    assert sent['comment_sources'][0]['text'] == env.records.read('workspace_items', env.item['id']).payload['source_text']
    turn = next(row for row in env.records.list('v2_memory_turn_keys')
        if row.payload['identity']['kind'] == 'memory.propose_insights')
    refs = turn.payload['request']['privacy']['material_refs']
    assert {'type': 'original_item', 'id': env.item['id'], 'revision': 6, 'project_id': 'alpha'} in refs
    frozen = env.records.read('v2_comment_extract_inputs', turn.object_id)
    assert frozen is not None and frozen.payload['sources'] == sent['comment_sources']
    assert marker['extract_turn_id'] == turn.object_id
    assert generate(env) == result and env.model.calls == 1


def test_native_comment_recipe_freezes_real_owner_and_proposes_comment_without_override(source_env):
    env = source_env
    admit(env)
    env.model.response = json.dumps(output(env), ensure_ascii=False)
    result = generate_insights(env.model, env.service, env.documents, 'alpha', env.doc)
    assert len(result) == 1 and result[0]['state'] == 'pending'
    assert result[0]['text'] == '周末可能关门，需提前确认'
    assert result[0]['conditions'] == ['周末']
    assert result[0]['hint'] == {'relation': 'differs', 'target_id': None, 'scope_hint': None, 'target': None}
    turn = next(row for row in env.records.list('v2_memory_turn_keys')
        if row.payload['identity']['kind'] == 'memory.propose_insights')
    assert turn.payload['request']['policy_versions']['extract'] == '@3'
    owner = env.records.read('workspace_items', env.item['id'])
    assert owner.revision == 6
    marker = env.records.read('v2_comment_candidates', result[0]['id']).payload
    quote = '周末已经关门 😀'
    start = owner.payload['source_text'].index(quote)
    assert marker['comment_source'] == {'type': 'original_item', 'id': owner.object_id,
        'project_id': 'alpha', 'revision': 6, 'coordinate_space': 'workspace_source_text_v1',
        'ordinal': 1, 'start': start, 'end': start + len(quote), 'quote': quote}
    assert marker['comparison_source']['quote'] == '营业到晚上。'
    frozen = env.records.read('v2_comment_extract_inputs', turn.object_id).payload
    assert frozen['sources'] == json.loads(env.model.messages[-1]['content'])['comment_sources']
    assert marker['extract_turn_id'] == turn.object_id
    assert env.records.list('recognitions') == () and env.records.list('recognition_relations') == ()
    assert generate_insights(env.model, env.service, env.documents, 'alpha', env.doc) == result
    assert env.model.calls == 1 and len(env.calls) == 5


@pytest.mark.parametrize('kind', ['owner', 'proof', 'document', 'alias_recreate', 'private'])
def test_change_during_real_provider_dispatch_discards_output_and_retains_admitted_document(source_env, kind):
    env = source_env
    admit(env)
    env.model.response = json.dumps(output(env), ensure_ascii=False)
    markdown = env.documents.markdown(env.doc)
    def mutate():
        if kind == 'owner':
            env.domains.items.update(env.item['id'], 'alpha', {'confirmed'}, title='迟到变化')
        elif kind == 'proof':
            row = env.records.read('v2_original_sections', env.item['id'])
            assert row is not None
            with env.records.begin() as tx:
                tx.put(row.collection, row.object_id, {**row.payload, 'capture_id': 'f' * 32}, expected_revision=row.revision)
                tx.commit()
        elif kind == 'document':
            env.documents.save_user_edit(env.doc, markdown=markdown + '\n人工改版', expected_revision=1)
        elif kind == 'alias_recreate':
            store = source_store(env.records)
            identity = 'source-' + env.item['id']
            original = store.read('sources', identity)
            assert store.delete('sources', identity)
            store.write('sources', identity, original, expected_revision=0)
        else:
            set_private_project(env.records, 'alpha', True, 0)
    env.model.after = mutate
    assert generate(env) == []
    assert env.model.calls == 1
    assert env.records.list('recognition_candidates') == ()
    assert env.records.list('v2_comment_candidates') == ()
    assert env.documents.read(env.doc) is not None
    if kind != 'document':
        assert env.documents.markdown(env.doc) == markdown


@pytest.mark.parametrize('ordinal,quote', [(True, '周末已经关门 😀'), (2, '周末已经关门 😀'), (1, '营业到晚上。')])
def test_model_cannot_quote_other_range_or_boolean_ordinal(source_env, ordinal, quote):
    env = source_env
    admit(env)
    env.model.response = json.dumps(output(env, ordinal=ordinal, quote=quote), ensure_ascii=False)
    assert generate(env) == [] and env.records.list('recognition_candidates') == ()


def test_comments_against_recognition_keep_the_old_neighbor_requirement(source_env):
    env = source_env
    admit(env)
    env.model.response = json.dumps(output(env, target='nonexistent'), ensure_ascii=False)
    assert generate(env) == [] and env.records.list('recognition_candidates') == ()


def test_private_original_is_refused_before_dispatch(source_env):
    from backend.memory_app.source_egress import SourceEgressService
    from backend.recognition import WorkScope
    env = source_env
    admit(env)
    original = env.records.read('workspace_items', env.item['id'])
    SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'), 'original_item',
        original.object_id, expected_source_revision=original.revision,
        expected_policy_revision=0, allowed_purposes=[])
    env.model.response = json.dumps(output(env), ensure_ascii=False)
    assert generate(env) == [] and env.model.calls == 0
    assert env.records.list('recognition_candidates') == ()


def test_cached_result_cannot_be_reused_after_original_revision_drift(source_env):
    env = source_env
    admit(env)
    env.model.response = json.dumps(output(env), ensure_ascii=False)
    assert len(generate(env)) == 1
    env.domains.items.update(env.item['id'], 'alpha', {'confirmed'}, title='后续变化')
    assert generate(env) == [] and env.model.calls == 1


def test_durable_comment_marker_failure_rolls_back_candidate_and_hint_in_same_tx(source_env):
    env = source_env
    admit(env)
    env.model.response = json.dumps(output(env), ensure_ascii=False)
    with env.records.begin() as tx:
        tx.connection.execute("""CREATE TRIGGER fail_comment_marker BEFORE INSERT ON crp_structured_records
            WHEN NEW.collection = 'v2_comment_candidates' BEGIN
            SELECT RAISE(ABORT, 'synthetic_durable_failure'); END""")
        tx.commit()
    assert generate(env) == [] and env.model.calls == 1
    assert env.records.list('recognition_candidates') == ()
    assert env.records.list('v2_candidate_hints') == ()
    assert env.records.list('v2_comment_candidates') == ()
    assert env.documents.read(env.doc) is not None


def test_body_and_comment_rows_keep_original_order_and_separate_proofs(source_env):
    env = source_env
    admit(env)
    response = output(env)
    response['insights'].insert(0, {'origin': 'body', 'kind': 'new_method', 'relation': 'new',
        'text': '提前核对营业时间', 'conditions': ['出行之前'], 'target_id': None, 'scope_hint': None})
    env.model.response = json.dumps(response, ensure_ascii=False)
    result = generate(env)
    assert [row['text'] for row in result] == ['提前核对营业时间', '周末可能关门，需提前确认']
    assert env.records.read('v2_comment_candidates', result[0]['id']) is None
    assert env.records.read('v2_comment_candidates', result[1]['id']).payload['comment_source']['ordinal'] == 1
    assert [env.records.read('v2_candidate_hints', row['id']).payload['relation'] for row in result] == ['new', 'differs']


def test_comment_against_real_neighbor_keeps_target_without_auto_publication(source_env):
    from tests.memory_app.v2.test_comparative_generation import publish
    env = source_env
    admit(env)
    neighbor = publish(env, 'old')
    env.model.response = json.dumps(output(env, target=neighbor.id), ensure_ascii=False)
    result = generate(env)
    assert len(result) == 1 and result[0]['state'] == 'pending'
    marker = env.records.read('v2_comment_candidates', result[0]['id']).payload
    assert marker['comparison_source'] is None
    assert env.records.read('v2_candidate_hints', result[0]['id']).payload['target_id'] == neighbor.id
    assert len(env.records.list('recognitions')) == 1
    assert env.records.list('recognition_relations') == ()


def test_ordinary_source_without_capture_keeps_real_at3_body_flow(source_env):
    env = source_env
    env.model.intake = True
    item = asyncio.run(env.domains.intake.add_text({'project_id': 'alpha', 'text': '普通原文 ## 评论区 不能猜成评论'}))
    result = asyncio.run(env.domains.intake.process(item['id'], {'project_id': 'alpha'}))
    admitted = asyncio.run(env.domains.review.confirm(item['id'], {'project_id': 'alpha', 'expected_revision': result['revision']}))
    env.doc, env.model.intake = admitted['document_id'], False
    env.model.response = json.dumps({'insights': [{'origin': 'body', 'kind': 'new_method',
        'relation': 'new', 'text': '普通方法', 'conditions': ['普通条件'], 'target_id': None,
        'scope_hint': None}], 'supports': []})
    assert len(generate(env)) == 1
    assert json.loads(env.model.messages[-1]['content'])['comment_sources'] == []
    assert env.records.list('v2_comment_candidates') == ()


def test_explicit_retry_freezes_the_same_source_and_capture_without_recapture(source_env):
    env = source_env
    admit(env)
    env.model.response = 'invalid-json'
    assert generate(env) == [] and env.model.calls == 1
    first = env.records.list('v2_comment_extract_inputs')
    assert len(first) == 1
    env.model.response = json.dumps(output(env), ensure_ascii=False)
    with override(extract='@3'):
        result = generate_insights(env.model, env.service, env.documents, 'alpha', env.doc, retry_token='retry-v1')
    assert len(result) == 1 and env.model.calls == 2
    frozen = env.records.list('v2_comment_extract_inputs')
    assert len(frozen) == 2 and all(row.payload == first[0].payload for row in frozen)
    assert len(env.calls) == 5
