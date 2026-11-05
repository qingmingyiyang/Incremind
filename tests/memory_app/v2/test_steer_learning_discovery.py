import os
from pathlib import Path
from shutil import copyfile
from uuid import uuid4

import pytest

from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.steer_learning_sources import learning_recognition_identity
from core.storage_provider import SQLiteStructuredRecordStore


def test_legacy_ordinary_discovery_preserves_original_source_chain_limit(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'legacy.sqlite3')
    scope = WorkScope('u', 'p')
    with records.begin() as tx:
        for index in range(257):
            identity, following = f'legacy-{index}', f'legacy-{index + 1}'
            tx.put('recognitions', identity, {'id': identity,
                'scope': {'user_id': scope.user_id, 'project_id': scope.project_id}, 'state': 'active',
                'source_experience_ids': [], 'source_experience_revisions': {},
                'source_recognition_ids': [] if index == 256 else [following],
                'source_recognition_revisions': {} if index == 256 else {following: 1}}, expected_revision=0)
        tx.commit()
    with records.begin() as tx:
        assert learning_recognition_identity(records, tx, scope, tx.read('recognitions', 'legacy-0')) is False
    with pytest.raises(RecognitionConflict, match='too large'):
        SourceEgressService(records).snapshot(scope, [{'type': 'recognition', 'id': 'legacy-0', 'revision': 1}])


@pytest.fixture(scope='module')
def actual_discovery_owner():
    allocated = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    root = allocated / 'discovery'
    (root / 'config').mkdir(parents=True)
    copyfile(Path(__file__).parents[3] / 'config/settings.toml.example', root / 'config/settings.toml')
    previous = os.environ['CHRIPTMAS_APP_ROOT']
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root.resolve(strict=True))
    try:
        from backend.memory_app.app import create_app
        from backend.memory_app.v2.steer_learning import SteerLearning
        app = create_app(runtime_root=root)
        yield app.state.recognition_records, SteerLearning(app, app.state.product_outcome_freshness)
    finally:
        os.environ['CHRIPTMAS_APP_ROOT'] = previous


@pytest.mark.parametrize('change,code', [
    ('missing_project', 'lineage_row_changed'), ('missing_parents', 'lineage_birth_changed'),
    ('missing_version', 'lineage_version_unavailable'),
])
def test_known_learning_root_requires_strict_current_domain_versions(actual_discovery_owner, change, code):
    records, learning = actual_discovery_owner
    service, scope = RecognitionService(records), WorkScope('local-user', 'strict-discovery')
    original = service.stage_experience(scope=scope, content='严格根来源检查原件-' + uuid4().hex)
    candidate = service.propose(scope=scope, content='人工认识检查', source_experience_ids=[original])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer='local-user')
    with records.begin() as tx:
        marker = tx.read('recognition_candidates', candidate.id)
        tx.put('recognition_candidates', candidate.id, {**marker.payload,
            'generation': {'id': str(uuid4()), 'step_version': 'candidate-from-steer-v1'}}, expected_revision=marker.revision)
        current = tx.read('recognitions', recognition.id)
        if change == 'missing_version':
            version = tx.read('recognition_versions', recognition.id + '~v1')
            tx.delete('recognition_versions', version.object_id, expected_revision=version.revision)
        else:
            payload = dict(current.payload)
            payload.pop('project_id' if change == 'missing_project' else 'parent_ids')
            current = tx.put('recognitions', current.object_id, payload, expected_revision=current.revision)
        assert learning_recognition_identity(records, tx, scope, current)
        with pytest.raises(RecognitionConflict, match=code):
            learning.sources.recognition_authority(records, tx, scope, current)
