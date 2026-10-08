"""Versioned decisions keep current contracts and isolate temporary overrides."""
from datetime import datetime, timedelta, timezone
import asyncio
import json

import pytest

from backend.memory_app.v2 import policies
from backend.memory_app.v2.policies import pipelines
from backend.memory_app.v2.policies.types import (
    EnoughInput, ForgetInput, PlaceInput, RankInput, ScopeInput,
    StrengthOutput, TriggerInput,
)


def test_all_active_versions_and_pipeline_steps_are_registered():
    expected = {'organize', 'place', 'extract', 'route', 'scope', 'retrieve',
                'rank', 'strength', 'forget', 'enough', 'compose', 'trigger', 'consolidate', 'image_read', 'handoff', 'reask', 'outcome_correction', 'review', 'elsewhere', 'gap', 'search', 'retry', 'continuation', 'style'}
    assert set(policies.ACTIVE) == expected
    for name, version in policies.ACTIVE.items():
        assert policies.get(name, version=version) is policies.get(name)
    assert pipelines.REMEMBER == ('organize', 'place', 'extract')
    assert pipelines.ASK_DO == ('route', 'scope', 'retrieve', 'rank', 'enough', 'compose')
    assert pipelines.LEARN == ('strength', 'forget', 'trigger', 'consolidate')
    for recipe in pipelines.PIPELINES.values():
        assert set(recipe) <= expected


def test_registration_and_nested_override_restore_after_exception():
    @policies.register('rank', '@9101')
    def alternative(request):
        return -request.score

    request = RankInput(score=3, weight=.5)
    original = policies.get('rank')
    assert original(request) == 2
    with policies.override(rank='@9101'):
        assert policies.version('rank') == '@9101'
        assert policies.get('rank')(request) == -3
        with pytest.raises(RuntimeError), policies.override(rank='@1'):
            assert policies.get('rank')(request) == 2
            raise RuntimeError('synthetic override exit')
        assert policies.get('rank') is alternative
    assert policies.get('rank') is original
    assert policies.ACTIVE['rank'] == '@2'


def test_duplicate_registration_is_rejected_without_replacing_original():
    original = policies.get('rank')
    with pytest.raises(ValueError, match='already_registered'):
        policies.register('rank', '@1')(lambda request: 999)
    assert policies.get('rank') is original


@pytest.mark.parametrize('options', [{'missing_policy': '@1'}, {'rank': '@999999'}, {'rank': 'invalid'}])
def test_invalid_override_does_not_change_other_versions(options):
    before = dict(policies.ACTIVE)
    with pytest.raises(ValueError):
        with policies.override(**options):
            pytest.fail('invalid override entered')
    assert policies.ACTIVE == before
    assert policies.version('rank') == '@2'


def test_async_overrides_are_local_to_each_evaluation():
    @policies.register('enough', '@9102')
    def never(_request):
        return False

    request = EnoughInput(evidence='synthetic evidence', coverage=.7, detail=False, layers=('L3',))

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def alternative():
            with policies.override(enough='@9102'):
                entered.set()
                await release.wait()
                assert policies.get('enough')(request) is False

        task = asyncio.create_task(alternative())
        await entered.wait()
        assert policies.get('enough')(request) is True
        release.set()
        await task

    asyncio.run(exercise())
    assert policies.version('enough') == '@1'


def test_strength_original_half_life_and_invalid_timestamp_fallback():
    from backend.shared.memory_sidecars import decayed_score, half_life_days
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    assert half_life_days(0, 'default') == 30
    assert half_life_days(0, 'me') == 90
    assert decayed_score({'score': 2, 'count': 0, 'project_id': 'default',
                          'updated_at': (now - timedelta(days=30)).isoformat()}, now) == 1
    for value in ('invalid', None, '2026-10-02'):
        assert decayed_score({'score': 2, 'count': 0, 'project_id': 'default', 'updated_at': value}, now) == 2
    @policies.register('strength', '@9103')
    def constant(_request):
        return StrengthOutput(half_life_days=77, score=9)
    with policies.override(strength='@9103'):
        assert half_life_days(3, 'me') == 77
        assert decayed_score({'score': 2, 'count': 0, 'project_id': 'default', 'updated_at': None}, now) == 9


@pytest.mark.parametrize('score,old,kind,project,recover,expected', [
    (.0624, 'normal', 'insight', 'default', True, 'forgotten'),
    (.0625, 'normal', 'insight', 'default', True, 'cooled'),
    (.0624, 'normal', 'insight', 'me', True, 'cooled'),
    (.0624, 'normal', 'document', 'default', True, 'cooled'),
    (.25, 'normal', 'insight', 'default', True, 'normal'),
    (.4999, 'cooled', 'insight', 'default', True, 'cooled'),
    (.5, 'cooled', 'insight', 'default', True, 'normal'),
    (.5, 'cooled', 'insight', 'default', False, 'cooled'),
])
def test_forget_original_boundaries(score, old, kind, project, recover, expected):
    assert policies.get('forget')(ForgetInput(score, old, kind, project, recover)) == expected


@policies.override(scope='@1')
def test_scope_place_enough_and_trigger_preserve_current_decisions():
    assert policies.get('scope')(ScopeInput(None, 'A')) is True
    assert policies.get('scope')(ScopeInput('A', 'A')) is True
    assert policies.get('scope')(ScopeInput('A', None)) is False
    assert policies.get('scope')(ScopeInput('A', 'B')) is False
    assert policies.get('place')(PlaceInput('tagged', 'inspiration', 'current')) == 'tagged'
    assert policies.get('place')(PlaceInput(None, 'inspiration', 'current')) == 'inbox'
    assert policies.get('place')(PlaceInput(None, 'remember', 'current')) == 'current'
    assert policies.get('enough')(EnoughInput('evidence', .6, False, ('L3',))) is True
    assert policies.get('enough')(EnoughInput('evidence', .5999, False, ('L3',))) is False
    assert policies.get('enough')(EnoughInput('', 1, False, ())) is False
    assert policies.get('enough')(EnoughInput('evidence', .8, True, ('L3',))) is False
    assert policies.get('enough')(EnoughInput('evidence', .8, True, ('L1',))) is True
    assert policies.get('trigger')(TriggerInput(initial=True, initial_delay=60, interval=86400)) == 60
    assert policies.get('trigger')(TriggerInput(initial=False, initial_delay=60, interval=.03)) == .03


@pytest.mark.parametrize('kind,pipeline', [
    ('memory.organize', 'remember'), ('memory.propose_insights', 'remember'),
    ('project.answer', 'ask_do'), ('project.task', 'ask_do'), ('workbench.route', 'ask_do'),
    ('memory.consolidate', 'learn'), ('memory.link_suggest', 'learn'), ('memory.overview', 'learn'),
])
@policies.override(scope='@1', extract='@1', place='@1')
def test_frozen_versions_are_deterministic_and_capture_temporary_selection(kind, pipeline):
    selected = '@9110'
    try:
        policies.get('rank', version=selected)
    except ValueError:
        policies.register('rank', selected)(lambda value: -value.score)
    versions = pipelines.versions_for_turn(kind)
    names = sorted((*pipelines.PIPELINES[pipeline], *pipelines.DEPENDENCIES[pipeline]))
    if kind in {'project.answer', 'workbench.route'}:
        names.append('search')
    assert tuple(versions) == tuple(names)
    assert versions == {name: '@1' if name in {'scope', 'extract'} else policies.ACTIVE[name]
                        for name in names}
    first = json.dumps(versions, ensure_ascii=False, separators=(',', ':')).encode()
    assert first == json.dumps(pipelines.versions_for_turn(kind), ensure_ascii=False, separators=(',', ':')).encode()
    if pipeline == 'ask_do':
        with policies.override(rank=selected):
            assert pipelines.versions_for_turn(kind)['rank'] == selected
        assert pipelines.versions_for_turn(kind) == versions


def test_frozen_placement_uses_actual_default_and_restores_historical_override():
    assert policies.ACTIVE['place'] == '@2'
    assert policies.version('place') == '@2'
    for kind in ('memory.organize', 'memory.propose_insights', 'project.answer',
                 'project.task', 'workbench.route'):
        current = pipelines.versions_for_turn(kind)
        assert current['place'] == '@2'
        with policies.override(place='@1'):
            assert pipelines.versions_for_turn(kind) == {**current, 'place': '@1'}
        assert pipelines.versions_for_turn(kind) == current
        assert policies.version('place') == '@2'
