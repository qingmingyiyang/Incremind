"""Edited filing facts stay separate from copied facts and frozen permission."""
from copy import deepcopy

import pytest

from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.source_egress import SourceEgressService, _frozen_packet_authority
from backend.memory_app.source_graph import SourceGraph, validate_graph
from backend.recognition import RecognitionError, WorkScope
from tests.memory_app.v2.test_document_filing_sources import filed, runtime


def edited(filed):
    runtime, source, target, copied = filed
    runtime.documents.save_user_edit(target['id'], markdown='用户补充的事实', expected_revision=1)
    experience, _ = ensure_document_experience(runtime.documents, runtime.domains.query.service, 'beta', target['id'])
    scope = WorkScope('local-user', 'beta')
    snapshot = SourceEgressService(runtime.records).snapshot(scope,
        [{'type': 'experience', 'id': experience, 'revision': 1}])
    return runtime, scope, experience, snapshot


@pytest.mark.parametrize('damage', ['extra', 'missing', 'bool', 'float', 'long_id',
    'scope', 'root', 'root_bool', 'nested_extra', 'cycle', 'budget'])
def test_frozen_edit_closure_rejects_malformed_or_forged_facts_without_writes(filed, damage):
    runtime, scope, experience, snapshot = edited(filed)
    bad = deepcopy(snapshot)
    dependency = bad['nodes'][0]['dependency_revisions']
    nested = dependency['source_snapshot']
    if damage == 'extra': dependency['forged'] = 1
    elif damage == 'missing': del dependency['document_revision']
    elif damage == 'bool': dependency['document_revision'] = True
    elif damage == 'float': dependency['filing_revision'] = 1.0
    elif damage == 'long_id': dependency['filing_document_id'] += 'a' * 129
    elif damage == 'scope': nested['scope']['project_id'] = 'alpha'
    elif damage == 'root': nested['roots'][0]['id'] = 'experience-other'
    elif damage == 'root_bool': nested['roots'][0]['revision'] = True
    elif damage == 'nested_extra': nested['untrusted'] = []
    elif damage == 'cycle': nested['nodes'].append(deepcopy(bad['nodes'][0]))
    elif damage == 'budget':
        base = nested['nodes'][0]
        nested['nodes'] = [{**base, 'id': f'experience-test-{index}'} for index in range(257)]
    before = runtime.records.list_all()
    with pytest.raises(RecognitionError):
        _frozen_packet_authority(scope, {'source_egress': bad}, [('experience', experience, 1)])
    assert runtime.records.list_all() == before


@pytest.mark.parametrize('damage', ['extra', 'bool', 'float', 'id'])
def test_flat_graph_parses_only_the_exact_seven_edit_owner_scalars(filed, damage):
    runtime, scope, experience, snapshot = edited(filed)
    graph = SourceGraph(); graph.snapshot(snapshot)
    bad = graph.result()
    dependency = next(row['dependency_revisions'] for row in bad['nodes']
        if row['type'] == 'experience' and row['id'] == experience)
    assert len(dependency) == 7 and 'source_snapshot' not in dependency
    if damage == 'extra': dependency['source_snapshot'] = snapshot
    elif damage == 'bool': dependency['filing_revision'] = True
    elif damage == 'float': dependency['document_markdown_revision'] = 1.0
    else: dependency['copied_experience_id'] = 'experience-arbitrary'
    with pytest.raises(RecognitionError):
        validate_graph(bad, scope.user_id)


@pytest.mark.parametrize('damage', ['missing_provenance', 'wrong_id', 'wrong_document_ref'])
def test_reserved_real_edit_identity_cannot_fall_back_to_generic_user_statement(filed, damage):
    runtime, scope, experience, snapshot = edited(filed)
    with runtime.records.begin() as tx:
        row = tx.read('recognition_experiences', experience)
        value = deepcopy(row.payload)
        if damage == 'missing_provenance': value['provenance'] = {'kind': 'user_statement', 'actor': 'local-user'}
        elif damage == 'wrong_id': value['id'] = 'experience-other'
        else: value['provenance']['source_refs'][0]['id'] = 'document-other'
        tx.put(row.collection, row.object_id, value, expected_revision=row.revision); tx.commit()
    before = runtime.records.list_all()
    with pytest.raises(RecognitionError):
        SourceEgressService(runtime.records).snapshot(scope,
            [{'type': 'experience', 'id': experience, 'revision': 2}])
    with pytest.raises(RecognitionError):
        runtime.domains.query.service.read_candidate_experiences(scope=scope, experience_ids=[experience])
    assert runtime.records.list_all() == before
