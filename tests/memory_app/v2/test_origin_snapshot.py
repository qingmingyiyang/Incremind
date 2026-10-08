import pytest

from backend.memory_app.source_egress import _frozen_packet_authority
from backend.memory_app.source_snapshot import _closure_identity
from backend.recognition import RecognitionConflict, WorkScope


def node(identity, dependencies=None):
    return {'type': 'experience', 'id': identity, 'source_revision': 1, 'policy_revision': 0,
        'effective_purposes': ['generation', 'embedding', 'rerank'],
        **({'dependency_revisions': dependencies} if dependencies else {})}


def packet(own, identity, entry):
    return {'source_egress': {'schema_version': 1, 'scope': {'user_id': 'local-user', 'project_id': own},
        'privacy_revision': 0, 'roots': [{'type': 'experience', 'id': identity, 'revision': 1}], 'nodes': [entry]}}


def dependencies():
    return {'origin_marker_revision': 1, 'source_project_id': 'alpha',
        'source_experience_id': 'original', 'source_revision': 1,
        'source_snapshot': packet('alpha', 'original', node('original'))['source_egress']}


def test_strict_origin_node_survives_retained_artifact_packet_read():
    value = packet('beta', 'copy', node('copy', dependencies()))
    result = _frozen_packet_authority(WorkScope('local-user', 'beta'), value, [('experience', 'copy', 1)])
    assert result['nodes'][0]['dependency_revisions'] == dependencies()


def test_origin_closure_is_hashable_and_keeps_privacy_separate_from_identity():
    first = node('copy', dependencies())
    before = {_closure_identity(first)}
    first['dependency_revisions']['source_snapshot']['privacy_revision'] = 2
    first['dependency_revisions']['source_snapshot']['nodes'][0]['effective_purposes'] = []
    assert {_closure_identity(first)} == before
    first['dependency_revisions']['origin_marker_revision'] = 2
    assert {_closure_identity(first)} != before


@pytest.mark.parametrize('change', ['unknown', 'bool', 'scope', 'roots', 'nested_unknown'])
def test_origin_dependency_is_not_a_general_dictionary(change):
    value = dependencies()
    if change == 'unknown':
        value['anything'] = 1
    elif change == 'bool':
        value['origin_marker_revision'] = True
    elif change == 'scope':
        value['source_snapshot']['scope']['user_id'] = 'other-user'
    elif change == 'roots':
        value['source_snapshot']['roots'][0]['id'] = 'wrong'
    else:
        value['source_snapshot']['nodes'][0]['anything'] = 1
    with pytest.raises(RecognitionConflict):
        _frozen_packet_authority(WorkScope('local-user', 'beta'),
            packet('beta', 'copy', node('copy', value)), [('experience', 'copy', 1)])
