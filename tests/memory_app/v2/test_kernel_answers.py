from tests.memory_app.v2.test_workbench_ask import env as _env_fixture, publish, ask
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
import asyncio
import json

env = _env_fixture


def test_answer_uses_real_kernel_and_replays_the_same_completed_request(env):
    publish(env)
    response = env.http.post("/api/v2/workbench/turns", headers={"Idempotency-Key": "answer-once"},
        json={"project_id": "alpha", "text": "alpha beta gamma?"})
    assert response.status_code == 200, response.text
    identity = response.json()["turn"]["id"]
    store = SQLiteAITurnStore(env.root / ".rebuild-data" / "ai-turns.sqlite3")
    assert store.get_request(identity)["desired_outcome"] == "project.answer"
    events = store.events_after(identity)
    completed = [e for e in events if e["type"] == "model.completed"]
    assert len(completed) == 1
    assert completed[0]["data"]["model_call_purpose"] == "primary"
    assert not any(e["type"] == "approval.required" for e in events)
    from core.effect_log import EffectClass, EffectState
    tool = next(e for e in events if e["type"] == "tool.outcome.recorded")
    effect = store.effect_runner.log.get(tool["correlation"]["tool_call_id"])
    assert effect.effect_class is EffectClass.AT_MOST_ONCE
    assert effect.state is EffectState.SETTLED_OK
    again = env.http.post("/api/v2/workbench/turns", headers={"Idempotency-Key": "answer-once"},
        json={"project_id": "alpha", "text": "alpha beta gamma?"})
    assert again.json() == response.json()
    assert env.model.calls == 1


def test_followup_aux_and_answer_share_one_kernel_turn(env):
    publish(env)
    first = ask(env).json()
    previous = env.model.complete
    def complete(messages, **kwargs):
        if "condensed_question" in messages[0]["content"]:
            kwargs["validate_current"]()
            env.model.calls += 1
            return json.dumps({"condensed_question": "alpha beta gamma?"}), {"usage": {"total_tokens": 3}}
        return previous(messages, **kwargs)
    env.model.complete = complete
    answer = ask(env, text="它的原则？", thread_id=first["thread_id"])
    assert answer.status_code == 200, answer.text
    identity = answer.json()["turn"]["id"]
    store = SQLiteAITurnStore(env.root / ".rebuild-data" / "ai-turns.sqlite3")
    completed = [e for e in store.events_after(identity) if e["type"] == "model.completed"]
    assert [e["data"]["model_call_purpose"] for e in completed] == ["aux", "primary"]
    assert store.get_immutable_payload(identity, "answer-model-result-followup") is not None
    assert store.get_immutable_payload(identity, "answer-model-result-answer") is not None


def test_completed_kernel_result_replays_after_configuration_changes(env):
    publish(env)
    response = ask(env)
    assert response.status_code == 200, response.text
    identity = response.json()["turn"]["id"]
    from backend.memory_app.kernel.answer_turns import RESULT_KIND
    from backend.memory_app.kernel.receipt_projection import question_receipt
    store = SQLiteAITurnStore(env.root / ".rebuild-data" / "ai-turns.sqlite3")
    saved = store.get_immutable_payload(identity, RESULT_KIND)
    assert saved is not None
    env.model.allowed = False
    async def unexpected():
        raise AssertionError("completed answer must not run again")
    result = asyncio.run(env.domains.query.answer_turns.run(turn_id=identity, project="alpha",
        question="alpha beta gamma?", operation=unexpected))
    assert result == saved[1]
    assert store.get_immutable_payload(identity, RESULT_KIND) == saved
    receipt = {**result["receipt"], "ask": question_receipt(env.root, identity, "alpha",
        result["receipt"]["ask"], records=env.records)}
    assert receipt == response.json()["turn"]["receipt"]
    assert env.model.calls == 1


def test_answer_local_dispatch_latency(env):
    import time
    publish(env)
    samples = []
    for index in range(5):
        started = time.perf_counter()
        env.model.before = lambda: samples.append((time.perf_counter() - started) * 1000)
        response = env.http.post("/api/v2/workbench/turns", headers={"Idempotency-Key": f"latency-{index}"},
            json={"project_id": "alpha", "text": "alpha beta gamma?"})
        assert response.status_code == 200, response.text
    print("local dispatch milliseconds", samples)
    from statistics import median
    assert len(samples) == 5
    assert median(samples) <= 250



def test_frozen_inputs_are_revalidated_at_each_answer_wire(env, monkeypatch):
    from backend.memory_app.v2 import turn_requests
    publish(env)
    checked = []
    original = turn_requests.validate_frozen_inputs
    def observe(*args, **kwargs):
        original(*args, **kwargs)
        checked.append(True)
    monkeypatch.setattr(turn_requests, "validate_frozen_inputs", observe)
    env.model.before = lambda: None
    old = env.model.complete
    def complete(messages, **kwargs):
        # The injected transport uses the same live wire callback as the gateway.
        kwargs["validate_current"]()
        assert len(checked) >= 2
        return old(messages, **kwargs)
    env.model.complete = complete
    response = ask(env)
    assert response.status_code == 200, response.text
    assert env.model.calls == 1
