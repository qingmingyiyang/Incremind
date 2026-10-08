"""Placement suggestions are replaceable without changing frozen @1 routing."""
from copy import deepcopy

import pytest

from backend.memory_app.v2.policies import get, override, version
from backend.memory_app.v2.policies.types import PlaceInput


@pytest.mark.parametrize('routing_input, expected', [
    (PlaceInput(None, 'remember', 'alpha'), 'alpha'),
    (PlaceInput(None, 'inspiration', 'alpha'), 'inbox'),
    (PlaceInput('beta', 'remember', 'alpha'), 'beta'),
    (PlaceInput('me', 'inspiration', 'alpha'), 'me'),
])
def test_frozen_v1_routing_stays_identical(routing_input, expected):
    assert get('place', version='@1')(routing_input) == expected


def inputs(*, terms=(('tea', 3), ('gift', 2)), examples=(), projects=None):
    from backend.memory_app.v2.policies.types import PlaceProject, PlaceSuggestionInput
    if projects is None:
        projects = (
            PlaceProject('alpha', frozenset({'reading'}), {'books': frozenset({'reading'})}),
            PlaceProject('life', frozenset({'tea', 'gift', 'travel'}), {
                'mother': frozenset({'tea', 'gift'}), 'friend': frozenset({'travel'})}),
            PlaceProject('me', frozenset({'tea', 'gift'}), {}),
        )
    return PlaceSuggestionInput('alpha', terms, projects, examples)


def test_v2_preserves_existing_string_routing():
    policy = get('place', version='@2')
    for request in (PlaceInput(None, 'remember', 'alpha'), PlaceInput(None, 'inspiration', 'alpha'),
                    PlaceInput('beta', 'ask', 'alpha')):
        assert policy(request) == get('place', version='@1')(request)


def test_v2_scores_local_vocabulary_and_selects_scene_without_mutation():
    policy = get('place', version='@2')
    request = inputs()
    before = deepcopy(request)
    hint = policy(request)
    assert (hint.project_id, hint.scene, hint.score) == ('life', 'mother', 5)
    assert request == before


@pytest.mark.parametrize('terms', [(), (('unrelated', 4),)])
def test_v2_zero_overlap_returns_no_suggestion(terms):
    policy = get('place', version='@2')
    assert policy(inputs(terms=terms)) is None


def test_v2_ambiguous_project_and_scene_are_not_guessed():
    policy = get('place', version='@2')
    from backend.memory_app.v2.policies.types import PlaceProject
    projects = (PlaceProject('alpha', frozenset({'tea'}), {}),
                PlaceProject('beta', frozenset({'tea'}), {}))
    assert policy(inputs(terms=(('tea', 3),), projects=projects)) is None
    projects = (PlaceProject('life', frozenset({'tea'}), {
        'mother': frozenset({'tea'}), 'friend': frozenset({'tea'})}),)
    hint = policy(inputs(terms=(('tea', 3),), projects=projects))
    assert (hint.project_id, hint.scene, hint.score) == ('life', None, 3)


def test_v2_prioritizes_same_project_correction_and_never_suggests_me():
    policy = get('place', version='@2')
    from backend.memory_app.v2.policies.types import PlaceExample
    examples = (PlaceExample('alpha', frozenset({'tea', 'gift'}), 'alpha', 'books'),
                PlaceExample('beta', frozenset({'tea', 'gift'}), 'life', 'friend'))
    hint = policy(inputs(examples=examples))
    assert (hint.project_id, hint.scene) == ('alpha', 'books')
    examples = (PlaceExample('alpha', frozenset({'tea', 'gift'}), 'me', None),)
    assert policy(inputs(examples=examples)).project_id == 'life'


def test_v2_override_restores_after_exception():
    get('place', version='@2')
    before = version('place')
    with pytest.raises(RuntimeError), override(place='@2'):
        assert version('place') == '@2'
        assert get('place')(inputs()).project_id == 'life'
        raise RuntimeError('synthetic context exit')
    assert version('place') == before
