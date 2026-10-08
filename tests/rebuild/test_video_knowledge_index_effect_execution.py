from __future__ import annotations

import sqlite3

import pytest

from core.effect_log import EffectLog, EffectState
from core.product_core.video_knowledge_index_effect_admission import (
    SQLiteVideoKnowledgeIndexEffectAdmission, VideoKnowledgeIndexEffectAdmissionFactory,
)
from core.product_core.video_knowledge_index_effect_execution import (
    RECEIPT_TABLE, RESERVATION_TABLE, VideoKnowledgeIndexEffectExecutionHandler,
    VideoKnowledgeIndexEffectExecutionProbe, VideoKnowledgeIndexQueryOutcome,
)


def _request() -> dict[str, str]:
    return {"id": "video-index-request-1", "operation": "upsert_video", "series_id": "series-1", "video_id": "video-1", "source_revision": "source-r1", "workspace_revision": "workspace-r1", "embedding_profile_revision": "embedding-r1", "lancedb_schema_revision": "schema-r1", "index_generation": "generation-1"}


def _admit(path):
    log = EffectLog(path)
    admission = VideoKnowledgeIndexEffectAdmissionFactory(admitted_at=100).build(request=_request())
    with sqlite3.connect(log.database, isolation_level=None) as c:
        c.execute("PRAGMA foreign_keys=ON"); c.execute("BEGIN IMMEDIATE")
        effect, _ = SQLiteVideoKnowledgeIndexEffectAdmission(log).admit_in_connection(c, admission, now=100)
        c.commit()
    return log, effect


def _result(): return {"artifact_ref": "lancedb-artifact-1", "artifact_revision": "artifact-r1", "entry_count": 3}
def _not_completed(*_): return VideoKnowledgeIndexQueryOutcome("not_completed")
def _unknown(*_): return VideoKnowledgeIndexQueryOutcome("unknown")
def _completed(*_): return VideoKnowledgeIndexQueryOutcome("completed", _result())


def test_handler_writes_immutable_receipt_and_replay_does_not_execute_twice(tmp_path) -> None:
    log, effect = _admit(tmp_path / "effects.sqlite")
    calls, checkpoints = [], []
    def checkpoint(): checkpoints.append("checkpoint")
    def execute(operation_id, request, controlled_checkpoint):
        calls.append((operation_id, dict(request))); controlled_checkpoint(); return _result()
    handler = VideoKnowledgeIndexEffectExecutionHandler(log.database, execute, _not_completed, lambda _: checkpoint)
    assert handler(effect).receipt_ref.endswith(effect.operation_id)
    assert handler(effect).receipt_ref.endswith(effect.operation_id)
    assert len(calls) == 1 and len(checkpoints) >= 3
    assert VideoKnowledgeIndexEffectExecutionProbe(log.database, _not_completed)(effect) == (EffectState.SETTLED_OK, f"receipt:video-knowledge-index/{effect.operation_id}")


def test_probe_materializes_exact_receipt_after_domain_completed_before_receipt(tmp_path) -> None:
    log, effect = _admit(tmp_path / "effects.sqlite")
    probe = VideoKnowledgeIndexEffectExecutionProbe(log.database, _not_completed)
    assert probe(effect)[0] is EffectState.PLANNED
    handler = VideoKnowledgeIndexEffectExecutionHandler(log.database, lambda *_: _result(), _not_completed, lambda _: lambda: None, after_reservation_write=lambda: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"): handler(effect)
    def query_without_effect_lock(*_):
        # The external query may need its own write transaction.  This proves
        # Probe released the short Effect read/verify transaction first.
        with sqlite3.connect(log.database, isolation_level=None, timeout=0) as concurrent:
            concurrent.execute("BEGIN IMMEDIATE")
            concurrent.rollback()
        return _completed()
    first_probe = VideoKnowledgeIndexEffectExecutionProbe(log.database, query_without_effect_lock)
    second_probe = VideoKnowledgeIndexEffectExecutionProbe(log.database, _completed)
    expected = (EffectState.SETTLED_OK, f"receipt:video-knowledge-index/{effect.operation_id}")
    assert first_probe(effect) == expected
    # A second Probe observes the first immutable winner rather than treating
    # a unique-key race as ambiguous recovery evidence.
    assert second_probe(effect) == expected


def test_reserved_not_completed_allows_safe_resume_and_second_worker_never_duplicates(tmp_path) -> None:
    log, effect = _admit(tmp_path / "effects.sqlite")
    calls = []
    first = VideoKnowledgeIndexEffectExecutionHandler(log.database, lambda *_: _result(), _not_completed, lambda _: lambda: None, after_reservation_write=lambda: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError): first(effect)
    second = VideoKnowledgeIndexEffectExecutionHandler(log.database, lambda *_: calls.append(1) or _result(), _not_completed, lambda _: lambda: None)
    second(effect)
    assert calls == [1]
    with sqlite3.connect(log.database) as c:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            c.execute(f"UPDATE {RESERVATION_TABLE} SET reservation_json='{{}}' WHERE operation_id=?", (effect.operation_id,))


def test_receipt_is_immutable_and_authority_drift_blocks_domain_write(tmp_path) -> None:
    log, effect = _admit(tmp_path / "effects.sqlite")
    calls = []
    handler = VideoKnowledgeIndexEffectExecutionHandler(log.database, lambda *_: calls.append(1) or _result(), _not_completed, lambda _: lambda: None, authority_validator=lambda *_: (_ for _ in ()).throw(ValueError("drift")))
    with pytest.raises(ValueError, match="drift"): handler(effect)
    assert calls == []
    good = VideoKnowledgeIndexEffectExecutionHandler(log.database, lambda *_: _result(), _not_completed, lambda _: lambda: None)
    good(effect)
    with sqlite3.connect(log.database) as c:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            c.execute(f"DELETE FROM {RECEIPT_TABLE} WHERE operation_id=?", (effect.operation_id,))


def test_unknown_or_conflicting_receipt_evidence_fails_closed(tmp_path) -> None:
    log, effect = _admit(tmp_path / "effects.sqlite")
    crash = VideoKnowledgeIndexEffectExecutionHandler(log.database, lambda *_: _result(), _not_completed, lambda _: lambda: None, after_reservation_write=lambda: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError): crash(effect)
    assert VideoKnowledgeIndexEffectExecutionProbe(log.database, _unknown)(effect) == (EffectState.UNKNOWN, "error:video-knowledge-index-query-unknown")
    with sqlite3.connect(log.database) as c:
        c.execute(f"DROP TRIGGER {RECEIPT_TABLE}_deny_update")
        c.execute(f"INSERT INTO {RECEIPT_TABLE}(operation_id,receipt_json,recorded_at) VALUES(?,?,?)", (effect.operation_id, '{"operation_id":"conflict"}', 100))
    assert VideoKnowledgeIndexEffectExecutionProbe(log.database, _completed)(effect) == (EffectState.UNKNOWN, "error:video-knowledge-index-evidence-drift")
