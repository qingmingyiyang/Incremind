"""真实画像构建与缓存命中在复制前校验，不替换被测领域对象。"""
from copy import deepcopy
import sqlite3

import pytest

from backend.memory_app.v2.external_context import DELIVERIES
from backend.memory_app.v2.external_proxy_handoff import _validate_copy
from backend.memory_app.v2.profile import COLLECTION, confirmed_profile
from backend.recognition import WorkScope
from tests.memory_app.v2.test_external_proxy_handoff import enabled, owner
from tests.memory_app.v2.test_profile import profile_env as profile_env, publish as publish_profile
from tests.memory_app.v2.test_workbench_ask import env as env, publish


@pytest.mark.parametrize('shape', ['opaque', 'escaped'])
def test_profile_published_after_preflight_does_not_copy_credentials(env, monkeypatch, shape):
    credential = ('synthetic-' + 'profile-private-marker' if shape == 'opaque'
        else 'synthetic-\\profile"private-marker')
    content = '本人明确偏好清晰说明 ' + credential
    enabled(env)
    original_collect = env.domains.query.collect_candidates
    published = []
    before_blocks = env.records.list(COLLECTION)

    def collect_then_publish(*args, **kwargs):
        collected = original_collect(*args, **kwargs)
        recognition, source = publish(env, text=content, project='me')
        published.append((recognition.id, source,
            deepcopy(env.records.read('recognition_experiences', source))))
        return collected

    monkeypatch.setattr(env.domains.query, 'collect_candidates', collect_then_publish)
    prepared = owner(env).prepare('codex', '本人偏好是什么？', credentials=(credential,))
    assert prepared is None and len(published) == 1
    identity, source, source_before = published[0]
    assert env.service.get_recognition(scope=WorkScope('local-user', 'me'),
        recognition_id=identity).content == content
    assert env.records.read('recognition_experiences', source) == source_before
    assert env.records.list(COLLECTION) == before_blocks
    assert env.records.list('v2_external_agent_bindings') == ()
    assert env.records.list(DELIVERIES) == () and env.model.calls == 0
    with sqlite3.connect(env.root / '.rebuild-data' / 'ai-turns.sqlite3') as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_turn_immutable_payloads WHERE kind='external-context-handoff-v1'").fetchone()[0] == 0


def test_guard_rejection_rolls_back_real_first_profile_block(profile_env):
    credential = 'synthetic-' + 'guard-private-marker'
    published = publish_profile(profile_env, '本人偏好 ' + credential)
    source_before = profile_env.records.read('recognitions', published.id)
    with pytest.raises(ValueError, match='^external_proxy_selected_secret$'):
        confirmed_profile(profile_env.records, profile_env.service,
            validate_input=lambda value: _validate_copy(value, (credential,)))
    assert profile_env.records.list(COLLECTION) == ()
    assert profile_env.records.read('recognitions', published.id) == source_before


def test_cache_hit_validates_real_payload_without_rewriting_original_fact(profile_env):
    credential = 'synthetic-' + 'cached-private-marker'
    publish_profile(profile_env, '本人偏好 ' + credential)
    original = confirmed_profile(profile_env.records, profile_env.service)
    rows = profile_env.records.list(COLLECTION)
    captured = []

    def reject(value):
        captured.append(deepcopy(value))
        _validate_copy(value, (credential,))

    with pytest.raises(ValueError, match='^external_proxy_selected_secret$'):
        confirmed_profile(profile_env.records, profile_env.service, validate_input=reject)
    assert len(captured) == 1 and captured[0] == original
    assert profile_env.records.list(COLLECTION) == rows


def test_validator_receives_detached_complete_payload_on_build_and_cache_hit(profile_env):
    published = publish_profile(profile_env, '本人偏好清晰的合成说明')
    seen = []

    def mutate_copy(value):
        seen.append(deepcopy(value))
        value['text'] = '不可写回的合成替换'
        value['projection_basis']['items'][0]['content'] = '不可写回的内容'
        value['items'][0]['snapshot']['roots'].clear()
        value['basis']['items'].clear()

    built = confirmed_profile(profile_env.records, profile_env.service, validate_input=mutate_copy)
    rows = profile_env.records.list(COLLECTION)
    cached = confirmed_profile(profile_env.records, profile_env.service, validate_input=mutate_copy)
    assert len(seen) == 2 and seen[0] == built and seen[1] == cached
    assert cached == built and profile_env.records.list(COLLECTION) == rows
    assert built['items'][0]['id'] == published.id
    assert built['items'][0]['snapshot']['roots'] and built['basis']['items']
    assert built['projection_basis']['items'][0]['content'] in built['text']
    assert '不可写回' not in built['text']


def test_none_guard_preserves_original_cache_bytes_and_return_shape(profile_env):
    publish_profile(profile_env, '本人偏好稳定的合成说明')
    original = confirmed_profile(profile_env.records, profile_env.service)
    rows = profile_env.records.list(COLLECTION)
    repeated = confirmed_profile(profile_env.records, profile_env.service, validate_input=None)
    assert repeated == original and repeated['text'].encode() == original['text'].encode()
    assert profile_env.records.list(COLLECTION) == rows


def test_empty_profile_is_validated_without_header_or_cache(profile_env):
    captured = []
    original = confirmed_profile(profile_env.records, profile_env.service)
    result = confirmed_profile(profile_env.records, profile_env.service, validate_input=captured.append)
    assert captured == [result] and result == original
    assert result['text'] == '' and result['count'] == result['tokens'] == 0
    assert result['items'] == [] and profile_env.records.list(COLLECTION) == ()
