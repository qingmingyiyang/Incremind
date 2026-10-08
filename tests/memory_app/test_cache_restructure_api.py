"""The real reviewed route retains the application lifecycle dependency."""
from backend.recognition import WorkScope
from backend.recognition.restructuring import RestructureProposalService
from tests.memory_app.test_api import _client, _shutdown
from tests.memory_app.v2.test_cache_sources import put_vectors, vector_rows


def test_real_review_approval_prepares_then_applies_shared_cache_plan(tmp_path):
    client, model = _client(tmp_path)
    service = client.app.state.recognition_service
    scope = WorkScope('local-user', 'project-a')
    calls = []
    shared = service.cache_invalidation
    def prepare(tx, own, identities):
        calls.append(('prepare', own, identities))
        apply = shared(tx, own, identities)
        def complete():
            calls.append(('apply', own, identities))
            return apply()
        return complete
    service.cache_invalidation = prepare
    try:
        experience = service.stage_experience(scope=scope, content='Evidence')
        pending = service.propose(scope=scope, content='Before', source_experience_ids=[experience])
        parent = service.publish(scope=scope, candidate_id=pending.id, expected_revision=1, reviewer='local-user')
        authority = RestructureProposalService(service)
        snapshot = authority.capture(scope=scope, recognition_ids=[parent.id], expected_revisions={parent.id: 1})
        proposal = authority.save(scope=scope, proposal_id='cache-reviewed', snapshot=snapshot,
            operation='revise', outputs=[{'content': 'After', 'conditions': [],
                'source_experience_ids': [experience], 'source_recognition_ids': []}],
            reason='Clarify', step_metadata={'source': 'manual', 'implementation_version': 'manual-restructure-v1'})
        path = service.records.database_path.parent / 'recognition-vectors.sqlite3'
        put_vectors(path, {(scope.project_id, parent.id)})
        body = {'project_id': scope.project_id, 'expected_revision': proposal['revision'], 'decision': 'approved'}
        approved = client.patch('/api/recognition/restructure-proposals/cache-reviewed', json=body)
        assert approved.status_code == 200, approved.text
        assert calls == [('prepare', scope, (('recognition', parent.id),)),
                         ('apply', scope, (('recognition', parent.id),))]
        assert vector_rows(path) == ()
        replay = client.patch('/api/recognition/restructure-proposals/cache-reviewed', json=body)
        assert replay.status_code == 200 and replay.json() == approved.json()
        assert len(calls) == 2
        assert model.calls == []
    finally:
        _shutdown(client)
