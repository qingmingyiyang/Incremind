"""Real capture/confirmation/generation DTOs; only external providers are fake."""
import asyncio
from copy import deepcopy
import json

import pytest

from backend.memory_app.v2.library import LibraryRead
from backend.memory_app.v2.insights import insight_view
from backend.memory_app.v2.image_read import comment_section_for_document
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.original_sources import source_store
from backend.recognition import WorkScope
from tests.memory_app.v2.test_source_sections import source_env, admit
from tests.memory_app.v2.test_comment_generation import generate, output
from tests.memory_app.v2.test_comment_document_section import prepare_xhs, confirm
from tests.memory_app.v2.test_vision_intake import vision_env


def note(env):
    return LibraryRead(env.records, env.service, env.documents, env.domains).drill(
        'alpha', 'note', env.doc)['note']


def comment_candidate(env):
    admit(env)
    env.model.response = json.dumps(output(env), ensure_ascii=False)
    result = generate(env)
    assert len(result) == 1 and result[0]['state'] == 'pending'
    return result[0]['id']


def test_real_confirmation_projects_count_at_only_the_generated_heading(source_env):
    env = source_env
    summary = '😀\n\n## 评论区\n\n伪造的正文'
    saved = asyncio.run(env.domains.review.save_draft(env.item['id'], {
        'project_id': 'alpha', 'expected_revision': env.ready['revision'],
        **env.ready['draft'], 'summary': summary}))
    assert saved['revision'] == 5
    admit(env)
    base = '# 整理稿\n\n## 摘要\n\n' + summary + '\n'
    view = note(env)
    assert view['markdown'].startswith(base)
    assert view.get('comment_section') == {'document_id': env.doc, 'document_revision': 1,
        'coordinate_space': 'document_markdown_v1', 'count': 2,
        'heading': {'start': len(base) + 2, 'end': len(base) + 2 + len('## 评论区')}}
    assert env.records.list('recognitions') == ()


def test_real_at3_pending_projects_the_verified_comment_quote(source_env):
    env = source_env
    identity = comment_candidate(env)
    row = env.records.read('workspace_items', env.item['id'])
    quote = '周末已经关门 😀'
    start = row.payload['source_text'].index(quote)
    expected = {'type': 'original_item', 'id': env.item['id'], 'project_id': 'alpha',
        'revision': 6, 'coordinate_space': 'workspace_source_text_v1',
        'ordinal': 1, 'start': start, 'end': start + len(quote), 'quote': quote}
    view = insight_view(env.records, WorkScope('local-user', 'alpha'), identity, service=env.service)
    assert view.get('comment_source') == expected
    assert view['hint'] == {'relation': 'differs', 'target_id': None, 'scope_hint': None, 'target': None}
    assert env.records.list('recognitions') == ()


def put(env, collection, identity, payload):
    with env.records.begin() as tx:
        old = tx.read(collection, identity)
        tx.put(collection, identity, payload, expected_revision=old.revision)
        tx.commit()


def candidate_view(env, identity):
    return insight_view(env.records, WorkScope('local-user', 'alpha'), identity, service=env.service)


def test_local_private_and_egress_off_do_not_hide_verified_display(source_env):
    env = source_env
    identity = comment_candidate(env)
    before = candidate_view(env, identity)['comment_source']
    count = note(env)['comment_section']
    calls = env.model.calls
    owner = env.records.read('workspace_items', env.item['id'])
    SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'), 'original_item',
        owner.object_id, expected_source_revision=owner.revision, expected_policy_revision=0,
        allowed_purposes=[])
    set_private_project(env.records, 'alpha', True, 0)
    # This is only the external provider configuration; no domain validator is replaced.
    env.model.public = lambda: {'generation': {'base_url': 'https://example.com/v1', 'allow_remote': False}}
    assert note(env)['comment_section'] == count
    assert candidate_view(env, identity)['comment_source'] == before
    assert env.model.calls == calls


def test_pending_marker_cannot_replace_frozen_quote_identity_or_actual_request(source_env):
    env = source_env
    identity = comment_candidate(env)
    proof = deepcopy(env.records.read('v2_comment_candidates', identity).payload)
    variants = []
    for key, value in [('ordinal', True), ('ordinal', 2), ('quote', '营业到晚上。'),
            ('project_id', 'beta'), ('type', 'original_source'), ('revision', True),
            ('coordinate_space', 'document_markdown_v1'), ('start', True)]:
        forged = deepcopy(proof)
        forged['comment_source'][key] = value
        variants.append((key, forged))
    forged = deepcopy(proof)
    forged['comparison_source'] = None
    variants.append(('comparison', forged))
    forged = deepcopy(proof)
    forged['source_bindings'][0]['aliases'][0]['revision'] = True
    variants.append(('binding_boolean', forged))
    for name, forged in variants:
        put(env, 'v2_comment_candidates', identity, forged)
        view = candidate_view(env, identity)
        assert view is not None and view['state'] == 'pending'
        assert 'comment_source' not in view, name
        put(env, 'v2_comment_candidates', identity, proof)
        assert 'comment_source' in candidate_view(env, identity), name
    turn_id = proof['extract_turn_id']
    index = deepcopy(env.records.read('v2_memory_turn_keys', turn_id).payload)
    forged = deepcopy(index)
    forged['request']['policy_versions']['extract'] = '@2'
    put(env, 'v2_memory_turn_keys', turn_id, forged)
    assert 'comment_source' not in candidate_view(env, identity)
    put(env, 'v2_memory_turn_keys', turn_id, index)
    assert candidate_view(env, identity)['comment_source'] == proof['comment_source']
    candidate = deepcopy(env.records.read('recognition_candidates', identity).payload)
    put(env, 'recognition_candidates', identity, {**candidate, 'content': '人工改写后的认识'})
    assert 'comment_source' not in candidate_view(env, identity)


def test_actual_source_alias_recreation_revokes_pending_comment_display(source_env):
    env = source_env
    identity = comment_candidate(env)
    assert 'comment_source' in candidate_view(env, identity)
    store = source_store(env.records)
    source_id = 'source-' + env.item['id']
    payload = store.read('sources', source_id)
    assert store.delete('sources', source_id)
    store.write('sources', source_id, payload, expected_revision=0)
    assert 'comment_source' not in candidate_view(env, identity)


def test_actual_owner_recreation_does_not_reuse_old_count_or_comment_birth(source_env):
    env = source_env
    identity = comment_candidate(env)
    assert note(env)['comment_section']['count'] == 2
    assert 'comment_source' in candidate_view(env, identity)
    owner = env.records.read('workspace_items', env.item['id'])
    old_birth = env.records.read('v2_original_sections', owner.object_id).payload['owner_birth']
    with env.records.begin() as tx:
        tx.delete('workspace_items', owner.object_id, expected_revision=owner.revision)
        tx.commit()
    env.domains.items.create_upload(owner.payload)
    new = env.records.read('v2_original_sections', owner.object_id)
    assert new.payload['owner_birth'] != old_birth and new.payload['state'] == 'unbound'
    assert 'comment_section' not in note(env)
    assert 'comment_source' not in candidate_view(env, identity)


def test_bad_capture_and_edit_hide_only_optional_count_without_changing_note(source_env):
    env = source_env
    admit(env)
    original = note(env)['markdown']
    proof = deepcopy(env.records.read('v2_original_sections', env.item['id']).payload)
    for name in ('like_boolean', 'wrong_project', 'invalidated'):
        forged = deepcopy(proof)
        if name == 'like_boolean':
            forged['comments'][0]['like_count'] = True
        elif name == 'wrong_project':
            forged['project_id'] = 'beta'
        else:
            forged['state'] = 'invalidated'
        put(env, 'v2_original_sections', env.item['id'], forged)
        current = note(env)
        assert current['markdown'] == original and 'comment_section' not in current, name
        put(env, 'v2_original_sections', env.item['id'], proof)
        assert note(env)['comment_section']['count'] == 2
    owner = env.records.read('workspace_items', env.item['id'])
    assert comment_section_for_document(env.records, env.documents, owner, revision=True) is None
    assert comment_section_for_document(env.records, env.documents, owner, revision=2) is None
    env.documents.save_user_edit(env.doc, markdown=original + '\n人工改写', expected_revision=1)
    assert 'comment_section' not in note(env)
    assert note(env)['markdown'] == original + '\n人工改写'


def test_xhs_count_uses_nonempty_screenshots_and_empty_comments_have_no_projection(vision_env, monkeypatch):
    env = vision_env
    identity, ready = prepare_xhs(env, monkeypatch, ['甲 😀\n## 评论区\n仍是截图文字', '', '乙补充'], ['推断', '', ''])
    env.doc = confirm(env, identity, ready['revision'])['document_id']
    view = note(env)
    section = view['comment_section']
    assert section['count'] == 2 and section['document_revision'] == 1
    assert view['markdown'][section['heading']['start']:section['heading']['end']] == '## 评论区'
    assert '### 第1张' in view['markdown'] and '### 第3张' in view['markdown']
    assert '## 看图' in view['markdown']
    identity, ready = prepare_xhs(env, monkeypatch, [''], ['一个推断'])
    env.doc = confirm(env, identity, ready['revision'])['document_id']
    empty = note(env)
    assert 'comment_section' not in empty and '## 看图' in empty['markdown']


@pytest.mark.parametrize('version', ['@2', '@3'])
def test_ordinary_body_candidate_does_not_gain_comment_mark_from_words(source_env, version):
    from backend.memory_app.v2.insight_generation import generate_insights
    from backend.memory_app.v2.policies import override
    env = source_env
    admit(env)
    body = {'kind': 'new_method', 'relation': 'new', 'text': '评论区评只是正文文字',
        'conditions': ['仅限测试'], 'target_id': None, 'scope_hint': None}
    if version == '@3':
        body['origin'] = 'body'
    env.model.response = json.dumps({'insights': [body], 'supports': []}, ensure_ascii=False)
    with override(extract=version):
        views = generate_insights(env.model, env.service, env.documents, 'alpha', env.doc)
    assert len(views) == 1 and views[0]['state'] == 'pending'
    assert 'comment_source' not in views[0]
    assert env.records.list('v2_comment_candidates') == ()
    assert env.records.list('recognitions') == ()
