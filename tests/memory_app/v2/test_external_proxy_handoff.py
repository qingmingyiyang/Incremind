"""原应用的真实查询、交接、来源与回执，不替换被测领域对象。"""
import json
import sqlite3
from types import SimpleNamespace

import pytest
from backend.recognition import WorkScope

from backend.memory_app.kernel.ai_runtime import get_or_build_ai_runtime
from backend.memory_app.v2.external_agent_settings import external_agent_settings, replace_external_agent_settings
from backend.memory_app.v2.external_context import DELIVERIES, delivery_receipts
from backend.memory_app.v2.privacy import set_private_project
from tests.memory_app.v2.test_workbench_ask import env as env, add_document as original_add_document, publish


def add_document(env, **kwargs):
    result = original_add_document(env, **kwargs)
    response = env.http.get('/api/v2/projects')
    assert response.status_code == 200
    assert any(row['id'] == kwargs.get('project', 'alpha') for row in response.json()['items'])
    return result


def enabled(env, **changes):
    current = external_agent_settings(env.records)
    replace_external_agent_settings(env.records,
        {key:value for key,value in current.items() if key != 'revision'} | {'allow_remote':True, **changes},
        expected_revision=current['revision'])


def owner(env):
    from backend.memory_app.v2.external_proxy_handoff import ProxyHandoff
    runtime = get_or_build_ai_runtime(SimpleNamespace(app=env.http.app), SimpleNamespace(root_dir=env.root))
    return ProxyHandoff(env.domains.query, env.http.app.state.external_context,
        runtime=runtime, runner=env.http.app.state.ai_turn_runner)


def test_real_query_tag_original_kernel_handoff_and_receipt(env):
    document, item = add_document(env, project='alpha', summary='alpha 预算约束',
        body='alpha 预算约束的详细规则', original='alpha 预算原件')
    enabled(env)
    result = owner(env).prepare('codex', '#alpha alpha预算是什么？')
    assert result is not None and result.project_id == 'alpha'
    assert '第二大脑上下文' in result.text and 'M1' in result.text
    result.validate()
    turn = env.http.app.state.ai_turn_store.get_request(result.turn_id)
    assert turn['desired_outcome'] == 'external.context'
    assert turn['input']['text'] == 'alpha预算是什么？'
    assert turn['capability_request']['arguments']['budget'] == 2000
    assert turn['capability_request']['arguments']['client'] == 'codex'
    assert len(env.records.list(DELIVERIES)) == 1
    receipts = delivery_receipts(env.records, env.root)
    assert len(receipts) == 1 and set(receipts[0]) == {'at', 'items'}
    assert receipts[0]['items'] > 0
    assert env.model.calls == 0
    assert env.records.list('v2_usage_document') == env.records.list('v2_usage_insight') == ()


@pytest.mark.parametrize('mode', ['off','disabled','private'])
def test_original_external_switch_denies_before_query_or_kernel(env, monkeypatch, mode):
    if mode != 'off':
        enabled(env, **({'clients':{'codex':False,'claude':True}} if mode == 'disabled' else {}))
    if mode == 'private':
        set_private_project(env.records, 'default', True, expected_revision=0)
    calls = []
    def unexpected(*args, **kwargs):
        calls.append(1)
        raise AssertionError('off must not collect')
    monkeypatch.setattr(env.domains.query, 'collect_candidates', unexpected)
    assert owner(env).prepare('codex', '合成问题') is None
    assert calls == [] and env.records.list(DELIVERIES) == ()
    assert env.records.list('v2_external_agent_bindings') == ()


def test_unknown_scope_and_empty_project_do_not_create_delivery(env):
    enabled(env)
    handoff = owner(env)
    assert handoff.prepare('codex', '#不存在的问题 合成问题') is None
    assert handoff.prepare('codex', '空项目问题') is None
    assert env.records.list(DELIVERIES) == ()


def test_late_private_change_invalidates_prepared_context(env):
    add_document(env, project='alpha', summary='alpha 合成摘要', body='alpha正文', original='alpha原文')
    enabled(env)
    prepared = owner(env).prepare('codex', '#alpha alpha？')
    assert prepared is not None
    set_private_project(env.records, 'alpha', True, expected_revision=0)
    with pytest.raises(ValueError, match='^external_proxy_handoff_invalid$'):
        prepared.validate()
    assert len(env.records.list(DELIVERIES)) == 1


def test_query_credentials_are_removed_before_any_frozen_binding(env):
    secret = 'synthetic-' + 'request-private-marker'
    add_document(env, project='alpha', summary='alpha预算', body='alpha预算正文', original='alpha预算原文')
    enabled(env)
    prepared = owner(env).prepare('codex', '#alpha alpha预算 ' + secret, credentials=(secret,))
    assert prepared is not None
    request = env.http.app.state.ai_turn_store.get_request(prepared.turn_id)
    assert secret not in json.dumps(request, ensure_ascii=False)
    assert secret not in prepared.text
    binding = env.records.read('v2_external_agent_bindings', prepared.turn_id)
    assert secret not in json.dumps(binding.payload, ensure_ascii=False)


@pytest.mark.parametrize('layer', ['L2', 'L3'])
@pytest.mark.parametrize('shape', ['opaque', 'shared', 'escaped'])
def test_selected_secret_is_never_copied_into_kernel_archive(env, layer, shape):
    secret = ('synthetic-' + 'source-credential-marker' if shape == 'opaque'
        else 'synthetic-\\request"private-marker' if shape == 'escaped'
        else 'sk-' + 'SYNTHETICNOTAREALKEY' * 2)
    if layer == 'L2':
        identity, _ = add_document(env, project='alpha', summary='alpha预算 ' + secret,
            body='alpha正文', original='alpha原文')
        before = env.documents.markdown(identity)
    else:
        published, _ = publish(env, text='alpha预算 ' + secret)
        identity = published.id
        assert env.http.get('/api/v2/projects').status_code == 200
        before = env.service.get_recognition(scope=WorkScope('local-user', 'alpha'),
            recognition_id=identity).content
    enabled(env)
    assert owner(env).prepare('codex', '#alpha alpha预算是什么？',
        credentials=(secret,) if shape != 'shared' else ()) is None
    assert env.records.list('v2_external_agent_bindings') == ()
    assert env.records.list(DELIVERIES) == ()
    with sqlite3.connect(env.root / '.rebuild-data' / 'ai-turns.sqlite3') as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_turn_immutable_payloads WHERE kind='external-context-handoff-v1'").fetchone()[0] == 0
    if layer == 'L2':
        assert env.documents.markdown(identity) == before
    else:
        assert env.service.get_recognition(scope=WorkScope('local-user', 'alpha'),
            recognition_id=identity).content == before


def test_profile_enabled_and_disabled_use_original_confirmed_profile(env, monkeypatch):
    publish(env, text='本人明确偏好清晰的 alpha 预算说明', project='me')
    enabled(env)
    prepared = owner(env).prepare('claude', '本人偏好是什么？')
    assert prepared is not None and 'P1' in prepared.text
    prepared.validate()
    enabled(env, include_profile=False)
    from backend.memory_app.v2 import external_proxy_handoff
    monkeypatch.setattr(external_proxy_handoff, 'confirmed_profile',
        lambda *args: pytest.fail('disabled profile must not be read'))
    assert owner(env).prepare('claude', '本人偏好是什么？') is None


def test_summary_coordinate_conversion_preserves_exact_original_text(env):
    identity, _ = add_document(env, project='alpha', summary='alpha 摘要独有文字',
        body='无匹配的正文', original='无匹配原件')
    enabled(env)
    prepared = owner(env).prepare('codex', '#alpha 摘要独有文字是什么？')
    assert prepared is not None
    context = env.http.app.state.external_context
    _, archive = context._archive(prepared.turn_id)
    selected = next(row for row in archive['selections'] if row['id'] == identity and row['layer'] == 'L2')
    from backend.memory_app.v2.layers import summary_of
    summary, start, _ = summary_of(env.documents.markdown(identity, revision=selected['revision']))
    assert start > 0 and all(0 <= w['start'] < w['end'] <= len(summary) for w in selected['windows'])
    assert '摘要独有文字' in prepared.text


def test_original_item_l0_identity_uses_original_revision(env):
    _, item = add_document(env, project='alpha', summary='无匹配摘要', body='无匹配正文',
        original='alpha 原文数据唯一数字 94827')
    enabled(env)
    prepared = owner(env).prepare('codex', '#alpha alpha原文数据94827是多少？')
    assert prepared is not None
    _, archive = env.http.app.state.external_context._archive(prepared.turn_id)
    selection = next(row for row in archive['selections'] if row['type'] == 'original_item')
    assert selection['id'] == item and selection['layer'] == 'L0'
    assert selection['revision'] == env.records.read('workspace_items', item).revision


def test_mixed_original_owners_cannot_construct_proxy_handoff(env):
    from backend.memory_app.v2.external_proxy_handoff import ProxyHandoff
    other = SimpleNamespace(records=object())
    runtime = get_or_build_ai_runtime(SimpleNamespace(app=env.http.app), SimpleNamespace(root_dir=env.root))
    with pytest.raises(ValueError, match='^external_proxy_owner_invalid$'):
        ProxyHandoff(other, env.http.app.state.external_context,
            runtime=runtime, runner=env.http.app.state.ai_turn_runner)
