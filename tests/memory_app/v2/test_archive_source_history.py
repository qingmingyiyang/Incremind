import os
from pathlib import Path
from uuid import uuid4

import pytest


def test_archive_history_keeps_active_egress_closed_and_rejects_missing_native_birth():
    allocated = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert not any(path.name == 'runtime' for path in (allocated, *allocated.parents))
    root = allocated / ('history-' + uuid4().hex[:8])
    from core.storage_provider import SQLiteStructuredRecordStore
    from backend.memory_app.source_egress import SourceEgressService, recognition_service
    from backend.recognition import WorkScope, RecognitionError

    records = SQLiteStructuredRecordStore(root / '.rebuild-data/structured-records.sqlite3')
    service, scope = recognition_service(records), WorkScope('local-user', 'history-project')
    experience = service.stage_experience(scope=scope, content='已停止的真实领域合成依据',
        provenance={'kind': 'user_statement', 'source_refs': []})
    candidate = service.propose(scope=scope, content='停止后的待确认内容', source_experience_ids=[experience])
    service.revoke_experience(scope=scope, experience_id=experience, expected_revision=1)
    assert records.read('recognition_candidates', candidate.id).payload['state'] == 'invalidated'
    retained = service.archive_source_history(scope=scope, source_type='candidate', source_id=candidate.id)
    assert retained['archive_only'] is True and retained['private'] is True
    egress = SourceEgressService(records)
    with pytest.raises(RecognitionError):
        egress.validate_snapshot(scope, retained)
    with pytest.raises(RecognitionError):
        egress.snapshot(scope, [{'type': 'experience', 'id': experience, 'revision': 2}])
    with pytest.raises(RecognitionError):
        service.publish(scope=scope, candidate_id=candidate.id, expected_revision=2, reviewer='local-user')
    unknown = service.stage_experience(scope=scope, content='没有任务证明的合成停止条目', provenance={
        'kind': 'model_generated_artifact', 'source_refs': [
            {'type': 'task', 'id': 'missing-task', 'revision': 1},
            {'type': 'document', 'id': 'missing-document', 'revision': 1},
            {'type': 'context_packet', 'id': 'missing-packet', 'revision': 1}]})
    service.revoke_experience(scope=scope, experience_id=unknown, expected_revision=1)
    with pytest.raises(RecognitionError):
        service.archive_source_history(scope=scope, source_type='experience', source_id=unknown)


def test_stopped_history_retains_lifecycle_and_unresolved_import_veto():
    allocated = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert not any(path.name == 'runtime' for path in (allocated, *allocated.parents))
    root = allocated / ('history-gates-' + uuid4().hex[:8])
    from core.storage_provider import SQLiteStructuredRecordStore
    from backend.memory_app.source_egress import recognition_service
    from backend.recognition import WorkScope, RecognitionError

    records = SQLiteStructuredRecordStore(root / '.rebuild-data/structured-records.sqlite3')
    service, scope = recognition_service(records), WorkScope('local-user', 'history-project')
    basis = service.stage_experience(scope=scope, content='合成依据',
        provenance={'kind': 'user_statement', 'source_refs': []})
    stopped = service.stage_experience(scope=scope, content='保留停止状态的合成资料', provenance={
        'kind': 'user_statement', 'source_refs': [{'type': 'experience', 'id': basis, 'revision': 1}]})
    candidate = service.propose(scope=scope, content='合成历史认识', source_experience_ids=[stopped])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision,
        reviewer='local-user')
    service.revoke(scope=scope, recognition_id=recognition.id, expected_revision=recognition.revision, reason='停止历史检查')
    service.revoke_experience(scope=scope, experience_id=stopped, expected_revision=1)
    assert service.archive_source_history(scope=scope, source_type='experience', source_id=stopped)['archive_only'] is True
    with records.begin() as tx:
        tx.put('recognition_migration_imports', 'unresolved-history', {
            'scope': {'user_id': scope.user_id, 'project_id': scope.project_id},
            'mapping': {'experiences': {'portable-input': stopped}},
            'receipt': {'issues': [{'location': 'provenance', 'code': 'external_provenance_reference',
                'record_id': 'portable-input'}]}}, expected_revision=0)
        tx.commit()
    with pytest.raises(RecognitionError, match='migration source'):
        service.archive_source_history(scope=scope, source_type='experience', source_id=stopped)
    with pytest.raises(RecognitionError, match='migration source'):
        service.archive_source_history(scope=scope, source_type='recognition', source_id=recognition.id)
    clean = service.stage_experience(scope=scope, content='生命周期修订检查',
        provenance={'kind': 'user_statement', 'source_refs': []})
    service.revoke_experience(scope=scope, experience_id=clean, expected_revision=1)
    with records.begin() as tx:
        row = tx.read('recognition_experiences', clean)
        tx.put('recognition_experiences', clean, dict(row.payload), expected_revision=row.revision)
        tx.commit()
    with pytest.raises(RecognitionError, match='lifecycle'):
        service.archive_source_history(scope=scope, source_type='experience', source_id=clean)
