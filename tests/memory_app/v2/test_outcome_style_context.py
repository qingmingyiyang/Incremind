"""原认识、来源与旁路缓存上的写法资格验证。"""
from types import SimpleNamespace

import pytest

from backend.recognition import RecognitionService, RecognitionError, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app.v2.projects import assign_scene
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.recall_preferences import set_preference


@pytest.fixture
def style_env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'styles.sqlite3')
    return SimpleNamespace(records=records, service=RecognitionService(records))


def publish(env, content, *, project='p', conditions=(), scene=None):
    scope = WorkScope('local-user', project)
    source = env.service.stage_experience(scope=scope, content='合成写法来源')
    candidate = env.service.propose(scope=scope, content=content, conditions=conditions,
        source_experience_ids=[source])
    result = env.service.publish(scope=scope, candidate_id=candidate.id,
        expected_revision=1, reviewer='local-user')
    if scene is not None:
        assign_scene(env.records, 'recognition', result.id, project, scene)
    return result


def test_only_confirmed_writing_in_project_scene_and_me_is_selected(style_env):
    from backend.memory_app.v2.style_context import confirmed_style
    env = style_env
    own = publish(env, '段落要简洁。')
    local = publish(env, '标题采用问题式。', scene='访谈')
    mine = publish(env, '开头先列出结论。', project='me')
    publish(env, '表格使用三列。', scene='培训')
    publish(env, '标题要简短。', project='other')
    publish(env, '这座建筑的结构由三层组成。')
    scope = WorkScope('local-user', 'p')
    source = env.service.stage_experience(scope=scope, content='未确认合成来源')
    env.service.propose(scope=scope, content='篇幅要控制在一页。', source_experience_ids=[source])
    block = confirmed_style(env.records, env.service, 'p', scene='访谈')
    assert {item['id'] for item in block['items']} == {own.id, local.id, mine.id}
    assert block['count'] == 3 and block['tokens'] <= 400
    assert '这座建筑' not in block['text'] and '一页' not in block['text']


def test_conditions_can_supply_the_writing_rule(style_env):
    from backend.memory_app.v2.style_context import confirmed_style
    env = style_env
    one = publish(env, '适用于月度总结。', conditions=['开头先给出结论。'])
    block = confirmed_style(env.records, env.service, 'p')
    assert [item['id'] for item in block['items']] == [one.id]
    assert '开头先给出结论。' in block['text']


def test_cache_bytes_survive_usage_changes_and_unrelated_privacy_epoch(style_env):
    from backend.memory_app.v2.style_context import confirmed_style, validate_style, COLLECTION
    from backend.shared.memory_sidecars import utc_now
    env = style_env
    publish(env, '开头先给结论。')
    two = publish(env, '列表使用动词开头。')
    before = confirmed_style(env.records, env.service, 'p')
    with env.records.begin() as tx:
        tx.put('v2_usage_insight', two.id, {'project_id':'p', 'score':50, 'count':8,
            'updated_at':utc_now().isoformat()}, expected_revision=0)
        tx.commit()
    set_private_project(env.records, 'unrelated', True, 0)
    after = confirmed_style(env.records, env.service, 'p')
    assert after['text'].encode() == before['text'].encode()
    assert after['basis'] != before['basis']
    assert len(env.records.list(COLLECTION)) == 1
    validate_style(env.records, env.service, after)
    with pytest.raises(RecognitionError):
        validate_style(env.records, env.service, before)


def test_forgotten_and_private_items_are_excluded_and_frozen_block_rejected(style_env):
    from backend.memory_app.v2.style_context import confirmed_style, validate_style
    env = style_env
    own = publish(env, '标题保持简短。')
    publish(env, '段落要精炼。', project='me')
    before = confirmed_style(env.records, env.service, 'p')
    set_preference(env.records, WorkScope('local-user','p'), own.id,
        recognition_revision=own.revision, preference_revision=0, state='forgotten')
    set_private_project(env.records, 'me', True, 0)
    assert confirmed_style(env.records, env.service, 'p')['text'] == ''
    with pytest.raises(RecognitionError):
        validate_style(env.records, env.service, before)


def test_cache_and_text_tampering_cannot_authorize_a_style_block(style_env):
    from backend.memory_app.v2.style_context import confirmed_style, validate_style, COLLECTION
    env = style_env
    publish(env, '开头先给结论。')
    block = confirmed_style(env.records, env.service, 'p')
    with pytest.raises(RecognitionError):
        validate_style(env.records, env.service, {**block, 'text': '伪造写法'})
    row = env.records.read(COLLECTION, block['cache_id'])
    with env.records.begin() as tx:
        tx.put(COLLECTION, row.object_id, {**row.payload, 'text':'伪造缓存'}, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(RecognitionError):
        validate_style(env.records, env.service, block)


def test_empty_style_has_no_prompt_and_unknown_saved_version_fails(style_env):
    from backend.memory_app.v2.style_context import confirmed_style, style_messages
    env = style_env
    block = confirmed_style(env.records, env.service, 'p')
    messages = [{'role':'user','content':'写一篇总结'}]
    assert block['text'] == '' and block['count'] == block['tokens'] == 0
    assert style_messages(block, messages) == messages
    with pytest.raises(ValueError):
        confirmed_style(env.records, env.service, 'p', version='@unknown')


def test_initial_order_uses_real_usage_strength(style_env):
    from backend.memory_app.v2.style_context import confirmed_style
    from backend.shared.memory_sidecars import utc_now
    env = style_env
    publish(env, '段落要简洁。')
    strong = publish(env, '开头先列出结论。')
    with env.records.begin() as tx:
        tx.put('v2_usage_insight', strong.id, {'project_id':'p', 'score':20, 'count':4,
            'updated_at':utc_now().isoformat()}, expected_revision=0)
        tx.commit()
    block = confirmed_style(env.records, env.service, 'p')
    assert block['items'][0]['id'] == strong.id


def request_for(block):
    import json
    return {'turn_id':'style-turn', 'scope':{'project_id':'p'},
        'input':{'refs':[], 'text':json.dumps({'task':'写总结',
            'style_input':{key:block[key] for key in ('version','text','count','tokens','selected')}},
            ensure_ascii=False)},
        'privacy':{'material_refs':[{'type':'recognition','id':item['id'],
            'revision':item['revision'],'project_id':item['project_id']} for item in block['items']],
            'source_snapshots':[item['snapshot'] for item in block['items']]}}


def test_frozen_style_binds_text_input_and_original_authority(style_env):
    from backend.memory_app.v2.style_context import confirmed_style, freeze_task_style, frozen_task_style
    env = style_env
    publish(env, '开头先列出结论。', project='me')
    block = confirmed_style(env.records, env.service, 'p')
    request = request_for(block)
    freeze_task_style(env.records, request, block)
    frozen = frozen_task_style(env.records, env.service, request)
    assert frozen['text'].encode() == block['text'].encode()
    assert frozen['items'] == block['items']
    before = env.records.list_all()
    frozen_task_style(env.records, env.service, request)
    assert env.records.list_all() == before
    request['privacy']['material_refs'] = []
    with pytest.raises(RecognitionError):
        frozen_task_style(env.records, env.service, request)


def test_frozen_style_rejects_changed_text_and_missing_binding(style_env):
    from backend.memory_app.v2.style_context import confirmed_style, freeze_task_style, frozen_task_style
    env = style_env
    publish(env, '标题要简短。')
    block = confirmed_style(env.records, env.service, 'p')
    request = request_for(block)
    with pytest.raises(RecognitionError):
        frozen_task_style(env.records, env.service, request)
    freeze_task_style(env.records, request, block)
    request['input']['text'] = request['input']['text'].replace('标题要简短。', '标题要夸张。')
    with pytest.raises(RecognitionError):
        frozen_task_style(env.records, env.service, request)


def test_empty_style_can_bind_without_injecting_unselected_facts(style_env):
    from backend.memory_app.v2.style_context import confirmed_style, style_input, freeze_task_style, frozen_task_style
    env = style_env
    block = confirmed_style(env.records, env.service, 'p')
    assert style_input(block) == {'version':'@1', 'text':'', 'tokens':0, 'count':0, 'selected':[]}
    request = request_for(block)
    freeze_task_style(env.records, request, block)
    assert frozen_task_style(env.records, env.service, request)['items'] == []
    publish(env, '合成事实：本周采购预算为三百元。')
    facts = confirmed_style(env.records, env.service, 'p')
    assert '采购预算' not in str(style_input(facts))
    assert facts['items'] == [] and facts['text'] == ''
