"""Explicit retry may replace a known failed attempt, never unknown egress."""
import json

import pytest

from backend.memory_app.v2.insight_generation import generate_insights
from backend.memory_app.v2.memory_turn import MemoryTurn
from tests.memory_app.v2.test_insight_generation import env


def retry(env, token):
    return generate_insights(env.model, env.service, env.documents,
                             "alpha", env.doc, retry_token=token)


def test_rejected_output_retries_once_per_execution_and_preserves_candidates(env):
    original = env.model.response
    env.model.response = '{"insights":"invalid"}'
    assert retry(env, "initial") == []
    assert retry(env, "retry-one") == []
    assert env.model.calls == 2
    env.model.response = original
    assert retry(env, "retry-one") == []
    assert env.model.calls == 2
    result = retry(env, "retry-two")
    assert len(result) == 2 and env.model.calls == 3
    assert [row["id"] for row in result] == [f"candidate-v2-{env.doc}-r1-{n}" for n in (1, 2)]
    assert retry(env, "another-request") == result and env.model.calls == 3
    rows = env.records.list("v2_memory_turn_keys")
    assert len(rows) == 3
    store = MemoryTurn.store_for(env.records)
    assert sorted(store.events_after(row.object_id)[-1]["type"] for row in rows) == [
        "turn.completed", "turn.failed", "turn.failed"]
    assert env.records.list("recognitions") == ()


@pytest.mark.parametrize("failed_predecessor", [False, True])
@pytest.mark.parametrize("other_token", ["owner", "other"])
def test_concurrent_execution_does_not_borrow_running_attempt(env, failed_predecessor, other_token):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    original = env.model.response
    if failed_predecessor:
        env.model.response = "invalid JSON"
        assert retry(env, "failed") == []
        env.model.response = original
    entered, release = Event(), Event()
    def blocked_wire():
        entered.set()
        assert release.wait(20)
    env.model.after = blocked_wire
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(retry, env, "owner")
        try:
            assert entered.wait(20)
            assert retry(env, other_token) == []
            assert env.model.calls == 1 + int(failed_predecessor)
        finally:
            release.set()
        assert len(first.result(timeout=20)) == 2
    assert len(retry(env, other_token)) == 2
    assert env.model.calls == 1 + int(failed_predecessor)
    assert len(env.records.list("v2_memory_turn_keys")) == 1 + int(failed_predecessor)


@pytest.mark.parametrize("failed_predecessor", [False, True])
def test_constructor_race_only_winner_can_execute_attempt(env, monkeypatch, failed_predecessor):
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    from threading import Barrier, Event
    from backend.memory_app.v2 import memory_turn
    from backend.memory_app.document_recognition import ensure_document_experience
    # Prepare the admitted source once so this race isolates attempt creation,
    # rather than concurrently creating the source experience before it.
    ensure_document_experience(env.documents, env.service, "alpha", env.doc)
    original = env.model.response
    if failed_predecessor:
        env.model.response = "invalid JSON"
        assert retry(env, "failed") == []
        env.model.response = original
    barrier, entered, release = Barrier(2), Event(), Event()
    freeze = memory_turn.freeze_product_turn
    def simultaneous_freeze(*args, **kwargs):
        frozen = freeze(*args, **kwargs)
        # Both constructors have observed the missing identity before either
        # enters the real transaction that must choose a single durable owner.
        barrier.wait(timeout=30)
        return frozen
    monkeypatch.setattr(memory_turn, "freeze_product_turn", simultaneous_freeze)
    def blocked_wire():
        entered.set()
        assert release.wait(30)
    env.model.after = blocked_wire
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = {pool.submit(retry, env, token): token for token in ("a", "b")}
        try:
            assert entered.wait(30)
            finished, pending = wait(attempts, timeout=30, return_when=FIRST_COMPLETED)
            assert len(finished) == 1 and len(pending) == 1
            loser = next(iter(finished))
            assert loser.result() == []
            assert retry(env, attempts[loser]) == []
            assert env.model.calls == 1 + int(failed_predecessor)
            assert len(env.records.list("v2_memory_turn_keys")) == 1 + int(failed_predecessor)
        finally:
            release.set()
        assert sorted(len(future.result(timeout=30)) for future in attempts) == [0, 2]


def test_unknown_call_cannot_be_reissued_by_any_retry_token(env):
    def interrupted():
        raise SystemExit("synthetic process interruption")
    env.model.after = interrupted
    with pytest.raises(SystemExit):
        retry(env, "initial")
    env.model.after = lambda: None
    assert retry(env, "retry-one") == []
    assert retry(env, "retry-two") == []
    assert env.model.calls == 1
    assert len(env.records.list("v2_memory_turn_keys")) == 1


def test_unobserved_remote_transport_failure_is_not_retryable(env):
    env.model.error = RuntimeError("synthetic provider failure")
    assert retry(env, "initial") == []
    env.model.error = None
    assert retry(env, "retry-one") == []
    assert env.model.calls == 1


def test_remote_unknown_cannot_be_retried_by_switching_to_local(env, monkeypatch):
    env.model.error = RuntimeError("synthetic remote transport failure")
    assert retry(env, "initial") == []
    env.model.error = None
    monkeypatch.setattr(env.model, "public", lambda: {
        "generation": {"base_url": "http://localhost/v1", "allow_remote": True}})
    assert retry(env, "retry-one") == []
    assert env.model.calls == 1
    assert len(env.records.list("v2_memory_turn_keys")) == 1


def test_retry_revalidates_original_material_revision(env):
    env.model.response = "invalid JSON"
    assert retry(env, "initial") == []
    row = env.records.list("recognition_experiences")[0]
    with env.records.begin() as tx:
        tx.put("recognition_experiences", row.object_id,
               dict(row.payload), expected_revision=row.revision)
        tx.commit()
    env.model.response = json.dumps({"insights": []})
    assert retry(env, "retry-one") == []
    assert env.model.calls == 1
    assert len(env.records.list("v2_memory_turn_keys")) == 1


@pytest.mark.parametrize("transport_failed", [False, True])
def test_observed_wire_receipt_controls_explicit_retry(env, monkeypatch, transport_failed):
    from tests.memory_app.v2.test_insight_generation import response_for
    def complete(messages, *, max_tokens, validate_current, wire_attempt_sink):
        validate_current()
        attempt = wire_attempt_sink.begin_model_wire_attempt()
        def wire():
            env.model.calls += 1
            if transport_failed:
                attempt.failed_transport(error_code="synthetic_transport_failure")
                raise RuntimeError("synthetic transport failure")
            attempt.succeeded(usage={}, cache_observation=None)
            return response_for(messages, env.model.response)
        response = attempt.invoke_wire(wire)
        # Native providers may wrap schema rejection as a generic model error.
        if env.model.calls == 1:
            raise RuntimeError("synthetic rejected structured response")
        return response, {"model": "fake", "configuration_revision": 1}
    monkeypatch.setattr(env.model, "complete", complete)
    assert retry(env, "initial") == []
    result = retry(env, "retry-one")
    assert len(result) == (0 if transport_failed else 2)
    assert env.model.calls == (1 if transport_failed else 2)
    rows = env.records.list("v2_memory_turn_keys")
    assert len(rows) == (1 if transport_failed else 2)
    assert env.records.list("recognitions") == ()
