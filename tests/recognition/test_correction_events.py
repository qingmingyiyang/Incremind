"""Correction fact writes share the actual recognition owner transaction."""
import json
import sqlite3

import pytest

from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def domain(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'facts.sqlite3')
    service = RecognitionService(records)
    scope = WorkScope('local-user', 'alpha')
    eid = service.stage_experience(scope=scope, content='Synthetic authoritative source')
    candidate = service.propose(scope=scope, content='Initial', conditions=['Condition'], source_experience_ids=[eid])
    return records, service, scope, candidate


def test_repeated_candidate_edits_keep_each_exact_before_and_after(domain):
    records, service, scope, candidate = domain
    first = service.edit_candidate(scope=scope, candidate_id=candidate.id, expected_revision=1, content='First')
    service.edit_candidate(scope=scope, candidate_id=candidate.id, expected_revision=first.revision, content='Second')
    events = records.list('v2_correction_events')
    assert len(events) == 2
    assert {json.loads(event.payload['before'])['text'] for event in events} == {'Initial', 'First'}
    assert {json.loads(event.payload['after'])['text'] for event in events} == {'First', 'Second'}
    assert all(event.payload['project_id'] == 'alpha' and event.payload['type'] == 'edit' for event in events)
    assert all(event.payload['source_refs'] and event.revision == 1 for event in events)


def test_reject_and_revise_write_once_and_noop_or_conflict_writes_nothing(domain):
    records, service, scope, candidate = domain
    active = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer='local-user')
    service.revise(scope=scope, recognition_id=active.id, expected_revision=1, content='Initial', conditions=['Condition'])
    assert records.list('v2_correction_events') == ()
    service.revise(scope=scope, recognition_id=active.id, expected_revision=1, content='Revised')
    other = service.propose(scope=scope, content='Rejected', source_experience_ids=candidate.source_experience_ids)
    service.reject_candidate(scope=scope, candidate_id=other.id, expected_revision=1, reviewer='local-user')
    assert len(records.list('v2_correction_events')) == 2
    before = records.list('v2_correction_events')
    with pytest.raises(RecognitionConflict):
        service.revise(scope=scope, recognition_id=active.id, expected_revision=1, content='Stale')
    assert records.list('v2_correction_events') == before


def test_correction_failure_rolls_back_actual_candidate_and_version(domain):
    records, service, scope, candidate = domain
    with sqlite3.connect(records.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_correction BEFORE INSERT ON crp_structured_records WHEN NEW.collection = 'v2_correction_events' BEGIN SELECT RAISE(ABORT, 'synthetic correction conflict'); END")
    old = records.read('recognition_candidates', candidate.id)
    with pytest.raises(sqlite3.IntegrityError, match='synthetic correction conflict'):
        service.edit_candidate(scope=scope, candidate_id=candidate.id, expected_revision=1, content='New')
    assert records.read('recognition_candidates', candidate.id) == old
    assert records.list('v2_correction_events') == ()
    active = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer='local-user')
    original = records.read('recognitions', active.id)
    versions = records.list('recognition_versions')
    with pytest.raises(sqlite3.IntegrityError, match='synthetic correction conflict'):
        service.revise(scope=scope, recognition_id=active.id, expected_revision=1, content='New')
    assert records.read('recognitions', active.id) == original
    assert records.list('recognition_versions') == versions


def test_rejection_timestamp_is_its_review_time_after_an_earlier_edit(domain):
    records, service, scope, candidate = domain
    edited = service.edit_candidate(scope=scope, candidate_id=candidate.id, expected_revision=1, content='Edited earlier')
    old = records.read('recognition_candidates', edited.id)
    rejected = service.reject_candidate(scope=scope, candidate_id=edited.id, expected_revision=edited.revision, reviewer='local-user')
    current = records.read('recognition_candidates', rejected.id)
    event = next(row for row in records.list('v2_correction_events') if row.payload['type'] == 'reject')
    assert current.payload['updated_at'] == old.payload['updated_at']
    assert event.payload['at'] == current.payload['reviewed_at']
    assert event.payload['at'] != old.payload['updated_at']


def test_candidate_and_recognition_with_same_id_keep_distinct_correction_facts(domain):
    records, service, scope, candidate = domain
    edited = service.edit_candidate(scope=scope, candidate_id=candidate.id, expected_revision=1, content='Edited')
    active = service.publish(scope=scope, candidate_id=edited.id, expected_revision=edited.revision,
                             reviewer='local-user', recognition_id=edited.id)
    revised = service.revise(scope=scope, recognition_id=active.id, expected_revision=1, content='Revised')
    events = records.list('v2_correction_events')
    assert revised.revision == 2 and len(events) == 2
    assert {row.payload['object_kind'] for row in events} == {'candidate', 'recognition'}
    assert len({row.object_id for row in events}) == 2
