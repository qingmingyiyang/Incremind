"""未执行的合同草案；仅 SQLite 真事务与合成时间/actor，未覆盖授权和模型外发。"""
from datetime import datetime, timezone
import inspect
import sqlite3

import pytest

from backend.shared import memory_sidecars
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


AT = '2026-10-08T01:02:03+00:00'
UPDATED = '2026-10-07T01:02:03+00:00'
REVIEWED = '2026-10-07T02:02:03+00:00'


def _domain_writer():
    # 新领域入口缺失应在节点内失败，不让导入错误中止整份草案的收集。
    from backend.recognition.correction_events import record_correction
    return record_correction


def _seed(records):
    scope = {'user_id': 'local-user', 'project_id': 'alpha'}
    with records.begin() as tx:
        for identity in ('experience-b', 'experience-a'):
            tx.put('recognition_experiences', identity, {'scope': scope, 'content': '合成原文'}, expected_revision=0)
        row = tx.read('recognition_experiences', 'experience-a')
        tx.put(row.collection, row.object_id, {**row.payload, 'content': '合成修订'}, expected_revision=1)
        tx.put('recognitions', 'recognition-source', {'scope': scope, 'content': '合成认识'}, expected_revision=0)
        tx.put('recognition_candidates', 'candidate', {
            'scope': scope, 'content': '修改前', 'conditions': ['适用时'],
            'source_experience_ids': ['experience-b', 'experience-a'],
            'source_experience_revisions': {'experience-b': 1, 'experience-a': 1},
            'source_recognition_ids': ['recognition-source'],
            'source_recognition_revisions': {'recognition-source': 1},
        }, expected_revision=0)
        tx.commit()


@pytest.fixture
def records(tmp_path):
    owner = SQLiteStructuredRecordStore(tmp_path / 'facts.sqlite3')
    _seed(owner)
    return owner


def _change(tx, **changes):
    before = tx.read('recognition_candidates', 'candidate')
    current = tx.put(before.collection, before.object_id,
        {**before.payload, 'content': '修改后', **changes}, expected_revision=before.revision)
    return before, current


def _unused_clock():
    raise AssertionError('已有时间时不应读取 fallback clock')


def test_legacy_entry_resolves_rebound_clock_at_each_call(records, monkeypatch):
    signature = inspect.signature(memory_sidecars.record_correction)
    assert tuple(signature.parameters) == (
        'transaction', 'previous', 'current', 'object_kind', 'event_type', 'after', 'event_id', 'at', 'turn_id')
    assert signature.parameters['object_kind'].kind == inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters['event_type'].default == 'edit'
    assert all(signature.parameters[name].default is None for name in ('after', 'event_id', 'at', 'turn_id'))
    for moment in (datetime(2026, 10, 8, 1, tzinfo=timezone.utc), datetime(2026, 10, 8, 2, tzinfo=timezone.utc)):
        monkeypatch.setattr(memory_sidecars, 'utc_now', lambda value=moment: value)
        with records.begin() as tx:
            before, current = _change(tx)
            memory_sidecars.record_correction(tx, before, current, object_kind='candidate')
            tx.commit()
    events = records.list('v2_correction_events')
    assert len(events) == 2
    assert {row.payload['at'] for row in events} == {'2026-10-08T01:00:00+00:00', '2026-10-08T02:00:00+00:00'}
    assert all(row.revision == 1 for row in events)


@pytest.mark.parametrize('entry', ['legacy', 'domain'])
@pytest.mark.parametrize('case,event_type,explicit_at,expected', [
    ('explicit', 'reject', AT, AT),
    ('review', 'reject', None, REVIEWED),
    ('update', 'edit', None, UPDATED),
    ('fallback', 'edit', None, AT),
])
def test_time_priority_uses_clock_only_for_fallback(records, monkeypatch, entry, case, event_type, explicit_at, expected):
    calls = []
    def clock():
        calls.append(case)
        return datetime.fromisoformat(AT)
    fields = {} if case == 'fallback' else {'updated_at': UPDATED, 'reviewed_at': REVIEWED}
    if entry == 'legacy':
        monkeypatch.setattr(memory_sidecars, 'utc_now', clock)
        writer, clock_argument = memory_sidecars.record_correction, {}
    else:
        writer, clock_argument = _domain_writer(), {'clock': clock}
    with records.begin() as tx:
        before, current = _change(tx, **fields)
        writer(tx, before, current, object_kind='candidate', event_type=event_type, at=explicit_at, **clock_argument)
        tx.commit()
    event, = records.list('v2_correction_events')
    assert event.payload['at'] == expected and event.revision == 1
    assert calls == (['fallback'] if case == 'fallback' else [])


def test_domain_and_legacy_entries_keep_exact_payload_and_source_ref_order(tmp_path, monkeypatch):
    domain = _domain_writer()
    monkeypatch.setattr(memory_sidecars, 'utc_now', _unused_clock)
    stored = []
    for entry, writer in (('legacy', memory_sidecars.record_correction), ('domain', domain)):
        records = SQLiteStructuredRecordStore(tmp_path / (entry + '.sqlite3'))
        _seed(records)
        with records.begin() as tx:
            before, current = _change(tx, conditions=['适用时', '新条件'], updated_at=UPDATED,
                source_experience_revisions={'experience-b': 1, 'experience-a': 2})
            writer(tx, before, current, object_kind='candidate', at=AT, turn_id='turn-a',
                **({'clock': _unused_clock} if entry == 'domain' else {}))
            tx.commit()
        event, = records.list('v2_correction_events')
        stored.append(event)
    assert stored[0] == stored[1]
    assert stored[0].object_id == 'correction-candidate-candidate-2' and stored[0].revision == 1
    assert stored[0].payload == {
        'project_id': 'alpha', 'user_id': 'local-user', 'object_kind': 'candidate',
        'object_id': 'candidate', 'object_revision': 2, 'type': 'edit',
        'before': '{"text":"修改前","conditions":["适用时"]}',
        'after': '{"text":"修改后","conditions":["适用时","新条件"]}',
        'source_refs': [
            {'type': 'experience', 'id': 'experience-b', 'revision': 1, 'project_id': 'alpha'},
            {'type': 'experience', 'id': 'experience-a', 'revision': 1, 'project_id': 'alpha'},
            {'type': 'recognition', 'id': 'recognition-source', 'revision': 1, 'project_id': 'alpha'},
            {'type': 'experience', 'id': 'experience-a', 'revision': 2, 'project_id': 'alpha'},
        ], 'turn_id': 'turn-a', 'at': AT,
    }


def test_domain_writer_insert_failure_rolls_back_the_original_transaction(records):
    writer = _domain_writer()
    old = records.read('recognition_candidates', 'candidate')
    with sqlite3.connect(records.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_writer_correction BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='v2_correction_events' BEGIN SELECT RAISE(ABORT,'synthetic writer conflict'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic writer conflict'):
        with records.begin() as tx:
            before, current = _change(tx)
            writer(tx, before, current, object_kind='candidate', at=AT)
            tx.commit()
    assert records.read('recognition_candidates', 'candidate') == old
    assert records.list('v2_correction_events') == ()


def test_domain_writer_event_cas_conflict_rolls_back_the_original_transaction(records):
    writer = _domain_writer()
    with records.begin() as tx:
        before, current = _change(tx)
        writer(tx, before, current, object_kind='candidate', event_id='fixed-event', at=AT)
        tx.commit()
    old = records.read('recognition_candidates', 'candidate')
    events = records.list('v2_correction_events')
    with pytest.raises(SQLiteUnitOfWorkConflict):
        with records.begin() as tx:
            before, current = _change(tx, content='冲突修订')
            writer(tx, before, current, object_kind='candidate', event_id='fixed-event', at=AT)
            tx.commit()
    assert records.read('recognition_candidates', 'candidate') == old
    assert records.list('v2_correction_events') == events


def test_domain_writer_keeps_the_original_audited_transaction_and_actor_context(records):
    from backend.security.audited_records import AuditedRecordStore
    from backend.security.device_identity import DeviceIdentity
    from backend.security.user_context import USER_ACCESS, UserAccess, user_context

    writer = _domain_writer()
    prior = USER_ACCESS.get()
    access = UserAccess(DeviceIdentity('device-admin', 'admin', 1), 'local-user', 'admin')
    owner = AuditedRecordStore(records, namespace='default', user_id='local-user',
        clock=lambda: datetime.fromisoformat(AT))
    with user_context(access), owner.begin() as tx:
        before, current = _change(tx)
        writer(tx, before, current, object_kind='candidate', at=AT)
        assert tx.closed is False
        event = tx.read('v2_correction_events', 'correction-candidate-candidate-2')
        assert event is not None and event.revision == 1
        tx.commit()
    assert USER_ACCESS.get() is prior
    attribution = owner.writer_for('v2_correction_events', event.object_id, 1)
    assert attribution['by'] == 'admin' and attribution['actor_user_id'] == 'admin'
    assert attribution['actor_device_id'] == 'device-admin' and attribution['target_user_id'] == 'local-user'
    assert attribution['collection'] == 'v2_correction_events' and attribution['object_revision'] == 1
