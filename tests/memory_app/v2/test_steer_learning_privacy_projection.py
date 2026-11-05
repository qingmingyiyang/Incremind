from copy import deepcopy

import pytest

from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.source_graph import SourceGraph
from backend.memory_app.v2.steer_learning_sources import _published_privacy_qualifications
from core.storage_provider import SQLiteStructuredRecordStore


def test_frozen_private_floor_does_not_reintroduce_old_revision_into_current_graph(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'recognition.sqlite3')
    service, scope = RecognitionService(records), WorkScope('local-user', 'projection-check')
    original = service.stage_experience(scope=scope, content='人工隐私图检查原件')
    candidate = service.propose(scope=scope, content='第一版认识', source_experience_ids=[original])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer='local-user')
    egress = SourceEgressService(records)
    egress.set_policy(scope, 'recognition', recognition.id, recognition.revision, 0, [])
    previous = egress.snapshot(scope, [{'type': 'recognition', 'id': recognition.id, 'revision': recognition.revision}])
    current = service.revise(scope=scope, recognition_id=recognition.id, expected_revision=recognition.revision, content='第二版认识')
    egress.set_policy(scope, 'recognition', current.id, current.revision, 1, ['generation', 'embedding', 'rerank'])
    now = egress.snapshot(scope, [{'type': 'recognition', 'id': current.id, 'revision': current.revision}])
    graph = SourceGraph()
    graph.snapshot(previous)
    with pytest.raises(RecognitionConflict):
        graph.snapshot(now)
    frozen = [{'complete': True, 'unknown': [], 'current_sources': [], 'recognitions': [],
        'privacy_ceiling': {'effective_purposes': [], 'sources': [{'previous': previous, 'current': previous, 'history_current': []}]}}]
    live = [{'complete': True, 'unknown': [], 'current_sources': [{'id': current.id, 'revision': current.revision}],
        'recognitions': [{'id': current.id, 'revision': current.revision}],
        'privacy_ceiling': {'effective_purposes': ['generation', 'embedding', 'rerank'],
            'sources': [{'previous': previous, 'current': now, 'history_current': []}]}}]
    before = deepcopy((live, frozen))
    projected = _published_privacy_qualifications(live, frozen)
    assert (live, frozen) == before
    graph, purposes = SourceGraph(), {'generation', 'embedding', 'rerank'}
    for qualified in projected:
        assert qualified['current_sources'] == [] and qualified['recognitions'] == []
        purposes.intersection_update(qualified['privacy_ceiling']['effective_purposes'])
        for source in qualified['privacy_ceiling']['sources']:
            graph.snapshot(source['current'])
            for retained in source['history_current']:
                graph.snapshot(retained)
    assert purposes == set()
    assert any(not value['privacy_ceiling']['sources'] and not value['privacy_ceiling']['effective_purposes'] for value in projected)
    assert frozen[0]['privacy_ceiling']['sources'][0]['current'] == previous
