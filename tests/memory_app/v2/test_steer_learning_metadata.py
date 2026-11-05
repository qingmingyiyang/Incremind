from copy import deepcopy

import pytest

from backend.memory_app.source_egress import _frozen_packet_authority
from backend.memory_app.source_graph import SourceGraph, validate_graph
from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.external_evidence_json import encoded


def _birth(identity, collection='recognitions'):
    return {'collection': collection, 'object_id': identity, 'fact_id': 'metadata-fact-' + identity}


def _metadata():
    # 本文件只验证无正文元数据语法，不创建原生执行或发布回执。
    owner = {'owner_user_id': 'local-user', 'namespace_id': 'default', 'origin_kind': 'personal'}
    original = {'owner': owner, 'candidate_id': 'steer-metadata-aux',
        'candidate_birth': _birth('steer-metadata-aux', 'recognition_candidates'),
        'recognition_birth': _birth('metadata-root'), 'auxiliary_turn_id': 'metadata-aux',
        'generation_ref': 'metadata-only-generation', 'proposal_ref': 'metadata-only-proposal',
        'candidate_origin_ref': 'metadata-only-origin', 'publication': {'id': 'metadata-root~v1', 'revision': 1},
        'feedback_ids': ['metadata-feedback'],
        'input_ref': {'collection': 'v2_steer_learning_inputs', 'id': 'metadata-aux', 'revision': 1},
        'model_input_refs': ['metadata-only-input'], 'publication_ref': 'metadata-only-publication'}
    child = {'id': 'metadata-child', 'revision': 1, 'birth': _birth('metadata-child'),
        'birth_version': {'id': 'metadata-child~v1', 'revision': 1}, 'parent_ids': ['metadata-root'],
        'transitions': [{'parent_id': 'metadata-root', 'id': 'metadata-root~v2', 'revision': 1}]}
    root = {'id': 'metadata-root', 'revision': 2, 'birth': _birth('metadata-root'),
        'birth_version': {'id': 'metadata-root~v1', 'revision': 1}, 'parent_ids': [], 'transitions': []}
    return {'owner': owner, 'recognition_id': child['id'], 'recognition_revision': 1,
        'recognition_birth': child['birth'], 'nodes': [child, root],
        'roots': [{'id': root['id'], 'revision': 2, 'birth': root['birth'], 'origin_identity': encoded(original)}]}


def _snapshots(value):
    scope = WorkScope('local-user', 'metadata-project')
    node = {'type': 'recognition', 'id': 'metadata-child', 'source_revision': 1,
        'policy_revision': 0, 'effective_purposes': ['embedding', 'generation', 'rerank'],
        'dependency_revisions': {'steer_learning_lineage_identity': encoded(value)}}
    snapshot = {'schema_version': 1, 'scope': {'user_id': scope.user_id, 'project_id': scope.project_id},
        'privacy_revision': 0, 'roots': [{'type': 'recognition', 'id': node['id'], 'revision': 1}], 'nodes': [node]}
    graph = SourceGraph()
    graph.snapshot(snapshot)
    return scope, snapshot, graph.result()


def test_lineage_metadata_grammar_is_identical_in_graph_and_frozen_packet_consumers():
    scope, snapshot, graph = _snapshots(_metadata())
    assert validate_graph(graph, scope.user_id) == graph
    assert _frozen_packet_authority(scope, {'source_egress': snapshot},
        [('recognition', 'metadata-child', 1)])['nodes'] == snapshot['nodes']


@pytest.mark.parametrize('change', ['revision', 'birth', 'parent', 'cycle', 'missing_node',
    'duplicate_node', 'duplicate_root', 'owner', 'original_identity', 'boolean_version', 'extra_key'])
def test_lineage_metadata_grammar_rejects_changed_frozen_identity_in_both_consumers(change):
    value = deepcopy(_metadata())
    if change == 'revision':
        value['recognition_revision'] = 2
    elif change == 'birth':
        value['recognition_birth'] = _birth('another-birth')
    elif change == 'parent':
        value['nodes'][0]['transitions'][0]['parent_id'] = 'another-parent'
    elif change == 'cycle':
        value['nodes'][1]['parent_ids'] = ['metadata-child']
        value['nodes'][1]['transitions'] = [{'parent_id': 'metadata-child', 'id': 'metadata-child~v2', 'revision': 1}]
    elif change == 'missing_node':
        value['nodes'].pop()
    elif change == 'duplicate_node':
        value['nodes'].append(deepcopy(value['nodes'][-1]))
    elif change == 'duplicate_root':
        value['roots'].append(deepcopy(value['roots'][0]))
    elif change == 'owner':
        value['owner']['owner_user_id'] = 'another-user'
    elif change == 'original_identity':
        value['roots'][0]['origin_identity'] = '{}'
    elif change == 'boolean_version':
        value['nodes'][0]['birth_version']['revision'] = True
    else:
        value['nodes'][0]['new_sources'] = []
    scope, snapshot, graph = _snapshots(value)
    with pytest.raises(RecognitionConflict):
        validate_graph(graph, scope.user_id)
    with pytest.raises(RecognitionConflict):
        _frozen_packet_authority(scope, {'source_egress': snapshot}, [('recognition', 'metadata-child', 1)])
