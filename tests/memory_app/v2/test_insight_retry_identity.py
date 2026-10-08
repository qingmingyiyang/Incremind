"""Explicit user attempts preserve unknown isolation and reusable results."""

import json
from uuid import UUID

from backend.memory_app.v2.insight_generation import generate_insights
from backend.memory_app.v2.memory_turn import MemoryTurn
from tests.memory_app.v2.test_insight_generation import env


def set_run(env, run_id):
    with env.records.begin() as tx:
        old = tx.read("v2_turns", "user-turn")
        tx.put("v2_turns", "user-turn", {
            "project_id": "alpha", "intent": "remember", "run_id": run_id,
            "receipt": {"remember": {"state": "processing", "document_id": env.doc}},
        }, expected_revision=old.revision if old else 0)
        tx.commit()


def generate(env, run_id):
    return generate_insights(env.model, env.service, env.documents, "alpha", env.doc,
        retry_token=run_id, attempt=("user-turn", run_id))


def test_unknown_replay_and_explicit_retry_never_call_again(env):
    set_run(env, "first")
    env.model.error = RuntimeError("synthetic failure")
    assert generate(env, "first") == []
    original = env.records.list("v2_memory_turn_keys")[-1]
    frozen = MemoryTurn.store_for(env.records).get_request(original.object_id)
    env.model.error = None
    assert generate(env, "first") == []
    assert env.model.calls == 1
    set_run(env, "retry")
    assert generate(env, "retry") == []
    assert env.model.calls == 1
    assert env.records.read("v2_memory_turn_keys", original.object_id) == original
    assert MemoryTurn.store_for(env.records).get_request(original.object_id) == frozen


def test_completed_output_is_reused_across_explicit_attempts(env):
    set_run(env, "first")
    original = generate(env, "first")
    assert len(original) == 2
    set_run(env, "next")
    assert generate(env, "next") == original
    assert env.model.calls == 1


def test_completed_empty_output_is_reused_across_explicit_attempts(env):
    env.model.response = json.dumps({"insights": []})
    set_run(env, "first")
    assert generate(env, "first") == []
    set_run(env, "next")
    assert generate(env, "next") == []
    assert env.model.calls == 1


def test_replaced_run_cannot_publish_its_late_output(env, monkeypatch):
    original = env.model.complete
    def observed(messages, *, max_tokens, validate_current, wire_attempt_sink):
        wire_attempt = wire_attempt_sink.begin_model_wire_attempt()
        def wire():
            result = original(messages, max_tokens=max_tokens, validate_current=validate_current)
            wire_attempt.succeeded(usage={}, cache_observation=None)
            return result
        return wire_attempt.invoke_wire(wire)
    monkeypatch.setattr(env.model, "complete", observed)
    set_run(env, "first")
    env.model.after = lambda: set_run(env, "next")
    assert generate(env, "first") == []
    assert env.records.list("recognition_candidates") == ()
    env.model.after = lambda: None
    assert len(generate(env, "next")) == 2
    assert env.model.calls == 2


def test_partial_candidate_commit_recovers_original_output_on_retry(env, monkeypatch):
    set_run(env, "first")
    original = MemoryTurn.propose
    def interrupted(self, **kwargs):
        result = original(self, **kwargs)
        if kwargs["key"].split(":", 1)[0] == "candidate-1":
            raise RuntimeError("synthetic interruption after committed candidate")
        return result
    monkeypatch.setattr(MemoryTurn, "propose", interrupted)
    assert generate(env, "first") == []
    assert len(env.records.list("recognition_candidates")) == 1
    monkeypatch.setattr(MemoryTurn, "propose", original)
    set_run(env, "retry")
    assert len(generate(env, "retry")) == 2
    assert env.model.calls == 1
    assert len({r.payload["generation"]["id"] for r in env.records.list("recognition_candidates")}) == 1


def test_replaced_run_cannot_write_candidates_after_durable_output(env, monkeypatch):
    set_run(env, "first")
    original = MemoryTurn.propose
    def replaced(self, **kwargs):
        set_run(env, "next")
        return original(self, **kwargs)
    monkeypatch.setattr(MemoryTurn, "propose", replaced)
    assert generate(env, "first") == []
    assert env.records.list("recognition_candidates") == ()
    monkeypatch.setattr(MemoryTurn, "propose", original)
    assert len(generate(env, "next")) == 2
    assert env.model.calls == 1


def test_transaction_fence_blocks_replacement_at_the_final_write_window(env, monkeypatch):
    from core.effect_log import EffectLog, EffectState

    set_run(env, "first")
    original = MemoryTurn.propose
    plan = EffectLog.plan
    effects = []
    def observe_plan(self, intent, **kwargs):
        result = plan(self, intent, **kwargs)
        if intent.step_key == "candidate-1:turn:user-turn:run:first":
            effects.append((self, result[0].operation_id))
        return result
    monkeypatch.setattr(EffectLog, "plan", observe_plan)
    def replace_at_write(self, **kwargs):
        write = kwargs["write"]
        def replaced_write():
            set_run(env, "next")
            return write()
        return original(self, **{**kwargs, "write": replaced_write})
    monkeypatch.setattr(MemoryTurn, "propose", replace_at_write)
    assert generate(env, "first") == []
    assert env.records.list("recognition_candidates") == ()
    assert effects
    log, operation_id = effects[0]
    failed = log.get(operation_id)
    # Handler exceptions retain their lease for the existing core Reaper.
    assert failed.state == EffectState.INFLIGHT
    store = MemoryTurn.store_for(env.records)
    owner = env.records.list("v2_memory_turn_keys")[-1].object_id
    frozen = store.get_request(owner)
    monkeypatch.setattr(MemoryTurn, "propose", original)
    assert len(generate(env, "next")) == 2
    assert env.model.calls == 1
    assert log.get(operation_id) == failed
    assert store.get_request(owner) == frozen


def test_partial_candidates_recover_their_generation_over_another_empty_output(env, monkeypatch):
    from backend.memory_app.kernel import memory_turn as kernel_memory_turn
    from backend.memory_app.document_recognition import ensure_document_experience
    from backend.memory_app.structured_generation import InsightOutput
    monkeypatch.setattr(kernel_memory_turn, "uuid4", lambda: UUID("11111111-1111-4111-8111-111111111111"))
    set_run(env, "first")
    original = MemoryTurn.propose
    def interrupted(self, **kwargs):
        result = original(self, **kwargs)
        if kwargs["key"].split(":", 1)[0] == "candidate-1":
            raise RuntimeError("synthetic interrupted candidate batch")
        return result
    monkeypatch.setattr(MemoryTurn, "propose", interrupted)
    assert generate(env, "first") == []
    assert len(env.records.list("recognition_candidates")) == 1
    monkeypatch.setattr(MemoryTurn, "propose", original)
    monkeypatch.setattr(kernel_memory_turn, "uuid4", lambda: UUID("00000000-0000-4000-8000-000000000002"))
    identity, revision = ensure_document_experience(env.documents, env.service, "alpha", env.doc)
    other = MemoryTurn(env.records, env.model, kind="memory.propose_insights", project="alpha",
        key=f"candidate-v2-{env.doc}-r{revision}-turn:parallel:run:other",
        materials=[{"type": "experience", "id": identity, "revision": revision, "project_id": "alpha"}],
        validate=lambda: None)
    env.model.response = json.dumps({"insights": []})
    from backend.memory_app.v2.comparative_insights import ComparativeOutput
    from backend.memory_app.v2.comment_insights import CommentOutput
    response_model = {'@3': CommentOutput, '@2': ComparativeOutput}.get(
        other.request['policy_versions']['extract'], InsightOutput)
    output, _ = other.generate(env.model.messages, response_model=response_model, max_tokens=1800)
    assert output.insights == []
    other.store.get_or_create_immutable_payload(other.turn_id, "memory-insights-applied-v1", {"candidate_ids": []})
    set_run(env, "retry")
    assert len(generate(env, "retry")) == 2
    assert env.model.calls == 2
    assert len({r.payload["generation"]["id"] for r in env.records.list("recognition_candidates")}) == 1
