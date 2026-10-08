"""Recovery and receipt contract for auxiliary memory Turns."""
import pytest
from backend.memory_app.v2.memory_turn import MemoryTurn
from tests.memory_app.v2.test_insight_generation import env, generate


def test_insight_generation_uses_aux_turn_and_kernel_receipts(env):
    result = generate(env)
    assert len(result) == 2
    turns = MemoryTurn.store_for(env.records)
    entries = env.records.list("v2_memory_turn_keys")
    assert len(entries) == 1
    identity = entries[0].object_id
    request = turns.get_request(identity)
    assert request["desired_outcome"] == "memory.propose_insights"
    assert request["execution_policy"]["purpose"] == "aux"
    events = tuple(turns.events_after(identity))
    terminal = next(e for e in events if e["type"] == "model.completed")
    receipt = turns.get(terminal["data"]["receipt_ref"])
    assert receipt["model_call_purpose"] == "aux"
    assert env.records.list("v2_egress_receipts") == ()
    assert turns.get_immutable_payload(identity, "memory-generation-output-v1")


def test_partial_candidate_commit_recovers_all_without_regeneration(env, monkeypatch):
    original = MemoryTurn.propose
    count = 0
    def crash(self, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise SystemExit("synthetic process death")
        return original(self, **kwargs)
    monkeypatch.setattr(MemoryTurn, "propose", crash)
    with pytest.raises(SystemExit):
        generate(env)
    assert len(env.records.list("recognition_candidates")) == 1
    monkeypatch.setattr(MemoryTurn, "propose", original)
    assert len(generate(env)) == 2
    assert env.model.calls == 1


def test_observed_wire_uses_kernel_receipt(env):
    from tests.memory_app.v2.test_consolidation import PatternModel
    from backend.memory_app.v2.consolidation import PatternOutput
    from backend.memory_app.document_recognition import ensure_document_experience
    identity, _ = ensure_document_experience(env.documents, env.service, "alpha", env.doc)
    row = env.records.read("recognition_experiences", identity)
    turn = MemoryTurn(env.records, PatternModel(), kind="memory.consolidate", project="alpha", key="wire",
        materials=[{"type": "experience", "id": identity, "revision": row.revision, "project_id": "alpha"}],
        validate=lambda: None)
    result, _ = turn.generate([], response_model=PatternOutput, max_tokens=1000)
    assert result.text


@pytest.mark.parametrize("point", ["during_call", "before_proposals"])
def test_real_process_death_preserves_durable_input_or_output(env, tmp_path, point):
    import os
    import subprocess
    import sys
    code = r"""
import os
from pathlib import Path
from core.storage_provider import SQLiteStructuredRecordStore
from core.document_engine import SQLiteDocumentRepository
from backend.recognition import RecognitionService
from backend.memory_app.v2.insight_generation import generate_insights
from backend.memory_app.v2.memory_turn import MemoryTurn
from tests.memory_app.v2.test_insight_generation import Model
records = SQLiteStructuredRecordStore(Path(os.environ['TEST_RECORDS']))
model = Model()
model.intake = False
if os.environ['TEST_POINT'] == 'during_call':
    model.after = lambda: os._exit(42)
else:
    MemoryTurn.propose = lambda *a, **k: os._exit(42)
generate_insights(model, RecognitionService(records), SQLiteDocumentRepository(records), 'alpha', os.environ['TEST_DOCUMENT'])
"""
    result = subprocess.run([sys.executable, "-c", code], env={**os.environ,
        "TEST_RECORDS": str(env.records.database_path), "TEST_DOCUMENT": env.doc,
        "TEST_POINT": point, "PYTHONPATH": str(__import__('pathlib').Path.cwd() / "src")},
        capture_output=True, text=True, timeout=45)
    assert result.returncode == 42, result.stderr
    rows = env.records.list("v2_memory_turn_keys")
    assert len(rows) == 1
    store = MemoryTurn.store_for(env.records)
    request = store.get_request(rows[0].object_id)
    assert request["privacy"]["material_refs"]
    recovered = generate(env)
    assert env.model.calls == 0
    if point == "before_proposals":
        assert len(recovered) == 2
        assert len(env.records.list("recognition_candidates")) == 2
    else:
        assert recovered == []
        from core.ai_kernel.recovery import classify_recovery
        assert classify_recovery(rows[0].object_id, 1,
            tuple(store.events_after(rows[0].object_id)), payload_loader=store.get).disposition == "quarantine"


@pytest.mark.parametrize("after_write", [False, True])
def test_expired_proposal_effect_recovers_domain_write_gap(env, monkeypatch, after_write):
    import time
    from backend.memory_app.kernel import memory_turn
    original = MemoryTurn.propose
    def interrupted(self, *, key, write, existing):
        def crash():
            if after_write:
                write()
            raise SystemExit("synthetic proposal interruption")
        return original(self, key=key, write=crash, existing=existing)
    monkeypatch.setattr(MemoryTurn, "propose", interrupted)
    with pytest.raises(SystemExit):
        generate(env)
    assert len(env.records.list("recognition_candidates")) == int(after_write)
    assert len(env.records.list("v2_candidate_hints")) == int(after_write)
    monkeypatch.setattr(MemoryTurn, "propose", original)
    later = time.time() + 300
    monkeypatch.setattr(memory_turn.time, "time", lambda: later)
    result = generate(env)
    assert len(result) == 2 and env.model.calls == 1
    store = MemoryTurn.store_for(env.records)
    identity = env.records.list("v2_memory_turn_keys")[0].object_id
    assert all(store.get_immutable_payload(identity, "memory-proposal-candidate-" + str(n)) for n in (1, 2))
    generations = {r.payload["generation"]["id"] for r in env.records.list("recognition_candidates")}
    assert len(generations) == 1


def test_generation_remote_disabled_never_dispatches(env, monkeypatch):
    monkeypatch.setattr(env.model, "public", lambda: {
        "generation": {"base_url": "https://example.com/v1", "allow_remote": False}})
    assert generate(env) == []
    assert env.model.calls == 0
    assert env.records.list("recognition_candidates") == ()
    assert env.records.list("v2_memory_turn_keys") == ()
