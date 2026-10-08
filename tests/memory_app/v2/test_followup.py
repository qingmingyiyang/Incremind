import json

from backend.memory_app.v2.followup import read_history
from backend.memory_app.v2.budget import text_tokens
from tests.memory_app.v2.test_workbench_ask import env as _env_fixture, publish, ask

env = _env_fixture


def test_followup_condenses_before_retrieval_but_answers_original_with_history(env):
    insight, _ = publish(env)
    first = ask(env).json()
    original = env.model.complete

    def complete(messages, **kwargs):
        if "condensed_question" in messages[0]["content"]:
            kwargs["validate_current"]()
            env.model.calls += 1
            assert "alpha beta gamma?" in messages[-1]["content"]
            assert "Synthetic answer" in messages[-1]["content"]
            return json.dumps({"condensed_question": "alpha beta gamma?"}), {"usage": {"total_tokens": 7}}
        return original(messages, **kwargs)

    env.model.complete = complete
    response = ask(env, text="它有哪些原则？", thread_id=first["thread_id"])
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["no_match"] is False
    assert receipt["trace"][0]["condensed_question"] == "alpha beta gamma?"
    assert [c["id"] for c in receipt["citations"]] == [insight.id]
    assert "它有哪些原则？" in env.model.messages[-1]["content"]
    assert "Synthetic answer" in env.model.messages[-1]["content"]
    assert receipt["model_usage"] == {"total_tokens": 14}
    part = next(p for p in receipt["context"]["parts"] if p["key"] == "history")
    assert part["count"] == 1 and 0 < part["tokens"] <= 800


def test_history_last_three_same_thread_and_project_and_strips_markers(env):
    with env.records.begin() as tx:
        for i in range(5):
            tx.put(
                "v2_turns",
                "past-" + str(i),
                {
                    "project_id": "alpha",
                    "thread_id": "thread-a",
                    "intent": "ask",
                    "user_text": "question " + str(i),
                    "created_at": str(i),
                    "receipt": {"ask": {"answer": "answer [1]【2】 " + "x" * 1300}},
                },
                expected_revision=0,
            )
        tx.put(
            "v2_turns",
            "other",
            {
                "project_id": "beta",
                "thread_id": "thread-a",
                "intent": "ask",
                "user_text": "foreign",
                "created_at": "9",
                "receipt": {"ask": {"answer": "foreign"}},
            },
            expected_revision=0,
        )
        tx.commit()
    history = read_history(env.records, "alpha", "thread-a", "current", budget=6000)
    assert len(history["turns"]) <= 3
    assert "foreign" not in history["text"] and "question 0" not in history["text"]
    assert "[1]" not in history["text"] and "【2】" not in history["text"]
    assert all(text_tokens(turn["answer"]) <= 300 for turn in history["turns"])
    assert text_tokens(history["text"]) <= 800
    assert read_history(env.records, "alpha", "thread-a", "current", budget=100)["text"] == ""


def test_condense_failure_falls_back_to_original_and_history_is_not_evidence(env):
    publish(env)
    first = ask(env).json()
    original = env.model.complete

    def complete(messages, **kwargs):
        if "condensed_question" in messages[0]["content"]:
            raise RuntimeError("synthetic failure")
        return original(messages, **kwargs)

    env.model.complete = complete
    response = ask(env, text="alpha beta gamma?", thread_id=first["thread_id"])
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["trace"][0]["condensed_question"] is None
    assert len(receipt["citations"]) == 1


def test_condensed_no_match_keeps_known_usage(env):
    publish(env)
    first = ask(env).json()

    def complete(messages, **kwargs):
        kwargs["validate_current"]()
        return json.dumps({"condensed_question": "unfindable absent"}), {"usage": {"total_tokens": 9}}

    env.model.complete = complete
    response = ask(env, text="它呢？", thread_id=first["thread_id"])
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["no_match"] is True and receipt["model_usage"] == {"total_tokens": 9, "observed_only": True}
    assert receipt["trace"][0]["rewrite_status"] == "failed"


def test_changed_private_history_blocks_even_condensed_no_match(env):
    from backend.memory_app.v2.privacy import set_private_project

    publish(env)
    first = ask(env).json()

    def complete(messages, **kwargs):
        set_private_project(env.records, "alpha", True, 0)
        return json.dumps({"condensed_question": "absent"}), {"usage": {"total_tokens": 9}}

    env.model.complete = complete
    response = ask(env, text="它呢？", thread_id=first["thread_id"])
    assert response.status_code == 409
    assert response.json()["detail"] == "source_changed_retry"


def test_manual_forget_drops_history_of_all_sent_sources(env):
    from backend.memory_app.recall_preferences import set_preference
    from backend.recognition import WorkScope

    first_insight, _ = publish(env)
    unused, _ = publish(env, text="alpha beta gamma my preferred tools are notebooks", project="me")
    env.model.numbers = [1]
    first = ask(env).json()
    assert first['turn']['receipt']['ask']['layers']['persona'] == 1
    set_preference(
        env.records,
        WorkScope("local-user", "me"),
        unused.id,
        recognition_revision=1,
        preference_revision=0,
        state="forgotten",
    )
    calls = env.model.calls
    response = ask(env, text="alpha beta gamma?", thread_id=first["thread_id"])
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["trace"][0]["history_turn_ids"] == []
    assert receipt["trace"][0]["condensed_question"] is None
    assert env.model.calls == calls + 1
    assert receipt["citations"][0]["id"] == first_insight.id


def test_recursive_history_dependency_blocks_later_private_persona(env):
    from backend.memory_app.v2.privacy import set_private_project

    publish(env)
    publish(env, text="alpha beta gamma my preferred tools are notebooks", project="me")
    first = ask(env).json()
    assert first['turn']['receipt']['ask']['layers']['persona'] == 1
    original = env.model.complete

    def complete(messages, **kwargs):
        if "condensed_question" in messages[0]["content"]:
            return json.dumps({"condensed_question": "alpha beta gamma?"}), {"usage": {"total_tokens": 1}}
        return original(messages, **kwargs)

    env.model.complete = complete
    second = ask(env, thread_id=first["thread_id"]).json()
    set_private_project(env.records, "me", True, 0)
    response = ask(env, thread_id=first["thread_id"])
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["trace"][0]["history_turn_ids"] == []
    assert second["turn"]["receipt"]["ask"]["trace"][0]["history_turn_ids"]


def test_real_wire_receipt_is_body_free_linked_to_turn_and_usage_is_aggregated(env):
    publish(env)
    first = ask(env).json()
    original = env.model.complete

    def complete(messages, *, max_tokens, validate_current, wire_attempt_sink=None, timeout_seconds=None):
        if "condensed_question" not in messages[0]["content"]:
            return original(messages, max_tokens=max_tokens, validate_current=validate_current, wire_attempt_sink=wire_attempt_sink)
        assert timeout_seconds == 10
        validate_current()
        attempt = wire_attempt_sink.begin_model_wire_attempt()
        def wire():
            output = json.dumps({"condensed_question": "alpha beta gamma?"})
            attempt.succeeded(usage={"input_tokens": 4, "output_tokens": 5}, cache_observation=None)
            return output
        output = attempt.invoke_wire(wire)
        return output, {"usage": {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9}}

    env.model.complete = complete
    response = ask(env, text="它呢？", thread_id=first["thread_id"])
    assert response.status_code == 200, response.text
    saved = response.json()
    receipt = saved["turn"]["receipt"]["ask"]
    ids = receipt["trace"][0]["condense_receipt_ids"]
    assert len(ids) == 1
    wire = kernel_wire_receipt(env, ids[0])
    assert wire["turn_id"] == saved["turn"]["id"] and wire["status"] == "succeeded"
    assert "alpha" not in json.dumps(wire) and "question" not in json.dumps(wire)
    assert receipt["model_usage"]["total_tokens"] == 16
    from backend.memory_app.kernel.receipt_projection import question_receipt
    projected = question_receipt(env.root, saved['turn']['id'], 'alpha', {})
    assert projected['model_usage'] == receipt['model_usage'] == {'total_tokens': 16}


def test_shared_ancestors_are_memoized_and_deduplicated(env, monkeypatch):
    with env.records.begin() as tx:
        for i in range(30):
            parents = [{"id": f"past-{j}", "revision": 1} for j in range(max(0, i - 3), i)]
            tx.put(
                "v2_turns",
                f"past-{i}",
                {
                    "project_id": "alpha",
                    "thread_id": "thread-a",
                    "intent": "ask",
                    "user_text": "alpha",
                    "created_at": f"{i:03}",
                    "receipt": {
                        "ask": {"answer": "unknown", "no_match": True, "trace": [{"history_turn_ids": parents}]}
                    },
                },
                expected_revision=0,
            )
        tx.commit()
    original = env.records.read
    observed = []

    def read(collection, identity):
        observed.append((collection, identity))
        return original(collection, identity)

    monkeypatch.setattr(env.records, "read", read)
    history = read_history(env.records, "alpha", "thread-a", "alpha", budget=6000, query=env.domains.query)
    assert len(history["turns"]) == 3
    assert max(len(dep["turns"]) for dep in history["dependencies"]) == 30
    assert len(observed) < 500


def test_invalid_structured_reply_keeps_actual_wire_usage(env):
    publish(env)
    first = ask(env).json()
    original = env.model.complete

    def complete(messages, *, max_tokens, validate_current, wire_attempt_sink=None):
        if "condensed_question" not in messages[0]["content"]:
            return original(messages, max_tokens=max_tokens, validate_current=validate_current, wire_attempt_sink=wire_attempt_sink)
        attempt = wire_attempt_sink.begin_model_wire_attempt()
        def wire():
            output = "not json"
            attempt.succeeded(usage={"input_tokens": 4, "output_tokens": 5}, cache_observation=None)
            return output
        output = attempt.invoke_wire(wire)
        return output, {"usage": {"total_tokens": 9}}

    env.model.complete = complete
    response = ask(env, thread_id=first["thread_id"])
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["trace"][0]["condense_status"] == "failed"
    assert receipt["trace"][0]["condensed_question"] is None
    assert receipt["model_usage"]["total_tokens"] == 16


def test_timeout_late_wire_usage_never_rewrites_finished_turn(env, monkeypatch):
    import time
    from backend.memory_app.v2 import followup

    publish(env)
    first = ask(env).json()
    original = env.model.complete
    monkeypatch.setattr(followup, "TIMEOUT_SECONDS", 0.5)

    def complete(messages, *, max_tokens, validate_current, wire_attempt_sink=None, timeout_seconds=None):
        if "condensed_question" not in messages[0]["content"]:
            return original(messages, max_tokens=max_tokens, validate_current=validate_current, wire_attempt_sink=wire_attempt_sink)
        attempt = wire_attempt_sink.begin_model_wire_attempt()

        def wire():
            from core.ai_kernel.sqlite_store import SQLiteAITurnStore
            kernel = SQLiteAITurnStore(env.root / ".rebuild-data" / "ai-turns.sqlite3")
            deadline = time.monotonic() + 5
            while not any(e["type"] == "model.completed" and e["data"].get("model_call_purpose") == "primary"
                          for e in kernel.events_after(wire_attempt_sink.turn_id)):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            attempt.succeeded(usage={"input_tokens": 4, "output_tokens": 5}, cache_observation=None)
            return json.dumps({"condensed_question": "late absent"})

        output = attempt.invoke_wire(wire)
        return output, {"usage": {"total_tokens": 9}}

    env.model.complete = complete
    response = ask(env, thread_id=first["thread_id"])
    assert response.status_code == 200, response.text
    turn = response.json()["turn"]
    receipt = turn["receipt"]["ask"]
    assert receipt["trace"][0]["condense_status"] == "timeout"
    assert receipt["trace"][0]["condensed_question"] is None
    assert receipt["model_usage"]["observed_only"] is True
    time.sleep(0.8)
    saved = env.records.read("v2_turns", turn["id"])
    assert saved.revision == 1 and saved.payload["receipt"]["ask"] == receipt
    wire = kernel_wire_receipt(env, receipt["trace"][0]["condense_receipt_ids"][0])
    assert wire["usage"] == {"input_tokens": 4, "output_tokens": 5}
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    kernel = SQLiteAITurnStore(env.root / ".rebuild-data" / "ai-turns.sqlite3")
    events = kernel.events_after(turn["id"])
    aux = [e for e in events if e["type"] in {"model.completed", "model.failed", "model.timed_out"}
           and e["data"].get("model_call_purpose") == "aux"]
    assert len(aux) == 1
    assert aux[0]["type"] != "model.completed"
    assert sum(e["type"] == "model.result.discarded" for e in events) == 1
    primary = next(e for e in events if e["type"] == "model.completed" and e["data"].get("model_call_purpose") == "primary")
    discarded = next(e for e in events if e["type"] == "model.result.discarded")
    assert primary["sequence"] < discarded["sequence"] < events[-1]["sequence"]
    assert events[-1]["type"] == "turn.completed"
    assert kernel.get_immutable_payload(turn["id"], "answer-model-result-followup") is None


def test_source_document_manual_forget_removes_recognition_history(env):
    from tests.memory_app.v2.test_workbench_ask import add_document

    doc, _ = add_document(env)
    publish(env, doc=doc)
    first = ask(env).json()
    with env.records.begin() as tx:
        tx.put(
            "v2_document_recall", doc, {"project_id": "alpha", "state": "forgotten", "by": "user"}, expected_revision=0
        )
        tx.commit()
    history = read_history(env.records, "alpha", first["thread_id"], "它呢？", query=env.domains.query)
    assert history["text"] == "" and history["turns"] == []


def test_history_reserve_exact_boundary_drops_oldest_whole_turn():
    from backend.memory_app.v2.followup import bound_history
    from backend.memory_app.v2.budget import input_tokens, history_tokens

    turns = [{"question": f"question-{i}", "answer": f"answer-{i}"} for i in range(4)]
    text = "\n\n".join(f"问：{turn['question']}\n答：{turn['answer']}" for turn in turns[-3:])
    budget = 5 * (input_tokens([], "current", reserve_refutes=True) + history_tokens(text))
    at = bound_history(turns, "current", budget=budget)
    assert at["turns"] == turns[-3:]
    below = bound_history(turns, "current", budget=budget - 5)
    assert below["turns"] == turns[-2:]
    assert "question-1" not in below["text"] and "question-3" in below["text"]


def test_unconfigured_offline_adapter_skips_condense_without_calling_models(env):
    import asyncio
    from backend.memory_app.v2.followup import condense, bound_history
    from tools.memory_eval import NoModels

    env.domains.query.models = NoModels()
    history = bound_history([{"question": "alpha?", "answer": "alpha"}], "它呢？")
    result = asyncio.run(condense(env.domains.query, "alpha", "它呢？", history, turn_id="eval-test"))
    assert result["status"] == "skipped" and result["question"] is None and result["receipt_ids"] == []


def test_archive_after_history_read_blocks_guard(env):
    from tests.memory_app.v2.test_workbench_ask import add_document
    from backend.memory_app.v2.followup import validate_history
    from backend.recognition import RecognitionError
    import pytest

    doc, _ = add_document(env)
    publish(env, doc=doc)
    first = ask(env).json()
    query = env.domains.query
    history = read_history(env.records, "alpha", first["thread_id"], "它呢？", query=query)
    assert history["text"]
    with env.records.begin() as tx:
        row = tx.read("documents", doc)
        tx.put("documents", doc, {**row.payload, "status": "archived"}, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(RecognitionError):
        validate_history(query, "alpha", history, query.ask_target())


def test_usage_aliases_are_normalized_once_per_call():
    from backend.memory_app.v2.followup import sum_usage

    assert sum_usage(
        {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9},
        {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
    ) == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}


def test_slow_authority_check_is_inside_condense_deadline(env, monkeypatch):
    import asyncio
    import time
    from backend.memory_app.v2 import followup

    history = {"text": "问：alpha?\n答：alpha", "turns": []}
    monkeypatch.setattr(followup, "TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(followup, "validate_history", lambda *args: time.sleep(0.15))

    async def run():
        start = time.monotonic()
        result = await followup.condense(env.domains.query, "alpha", "它呢？", history, turn_id="slow-test")
        assert time.monotonic() - start < 0.1
        return result

    result = asyncio.run(run())
    assert result["status"] == "timeout" and env.model.calls == 0


def test_followup_idempotent_replay_does_not_condense_or_charge_again(env):
    publish(env)
    first = ask(env).json()
    original = env.model.complete

    def complete(messages, **kwargs):
        if "condensed_question" in messages[0]["content"]:
            env.model.calls += 1
            return json.dumps({"condensed_question": "alpha beta gamma?"}), {"usage": {"total_tokens": 3}}
        return original(messages, **kwargs)

    env.model.complete = complete
    body = {"project_id": "alpha", "thread_id": first["thread_id"], "text": "它呢？", "intent": "ask"}
    headers = {"Idempotency-Key": "followup-once"}
    response = env.http.post("/api/v2/workbench/turns", json=body, headers=headers)
    assert response.status_code == 200, response.text
    calls = env.model.calls
    replay = env.http.post("/api/v2/workbench/turns", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == response.json()
    assert env.model.calls == calls == 3


def test_exhausted_history_reserve_has_no_condense_call(env):
    publish(env)
    first = ask(env).json()
    env.model.generation_budget_limits = lambda **kwargs: {"window": 1000, "reserve": 1000}
    calls = env.model.calls
    response = ask(env, text="它呢？", thread_id=first["thread_id"])
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["trace"][0]["condense_status"] == "skipped"
    assert receipt["trace"][0]["history_turn_ids"] == [] and env.model.calls == calls


def test_condensation_preserves_original_detail_minimum_layer(env):
    from tests.memory_app.v2.test_workbench_ask import add_document
    doc, _ = add_document(env, original="alpha beta gamma", body="alpha beta gamma")
    publish(env, doc=doc)
    first = ask(env).json()
    original = env.model.complete
    def complete(messages, **kwargs):
        if "condensed_question" in messages[0]["content"]:
            return json.dumps({"condensed_question": "alpha beta gamma?"}), {"usage": {}}
        return original(messages, **kwargs)
    env.model.complete = complete
    response = ask(env, text="它的原文怎么说？", thread_id=first["thread_id"])
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert any(step["layer"] in {"note", "source"} and step["selected"] > 0 for step in receipt["trace"])


def test_three_history_turns_validate_shared_source_once_per_guard(env, monkeypatch):
    from copy import deepcopy
    from backend.memory_app.v2.followup import validate_history
    from backend.memory_app.source_egress import SourceEgressService

    publish(env)
    first = ask(env).json()
    row = env.records.read("v2_turns", first["turn"]["id"])
    with env.records.begin() as tx:
        for index in range(2):
            payload = deepcopy(row.payload)
            payload["created_at"] = "999" + str(index)
            tx.put("v2_turns", "shared-" + str(index), payload, expected_revision=0)
        tx.commit()
    query = env.domains.query
    history = read_history(env.records, "alpha", first["thread_id"], "current", query=query)
    assert len(history["turns"]) == 3
    plans, snapshots = [], []
    original_plan = query.validate_ask_plan
    original_snapshot = SourceEgressService.validate_snapshot

    def plan(value):
        plans.append(value)
        return original_plan(value)

    def snapshot(self, scope, value):
        snapshots.append(value)
        return original_snapshot(self, scope, value)

    monkeypatch.setattr(query, "validate_ask_plan", plan)
    monkeypatch.setattr(SourceEgressService, "validate_snapshot", snapshot)
    target = query.ask_target()
    validate_history(query, "alpha", history, target)
    assert len(plans) == 1
    assert len(plans[0]["chosen"]) == 1
    assert len(snapshots) == 1
    validate_history(query, "alpha", history, target)
    assert len(plans) == 2 and len(snapshots) == 2


def test_conflicting_frozen_history_dependencies_reject_in_either_order(env):
    from copy import deepcopy
    import pytest
    from backend.memory_app.v2.followup import validate_history
    from backend.recognition import RecognitionError

    publish(env)
    first = ask(env).json()
    query = env.domains.query
    history = read_history(env.records, "alpha", first["thread_id"], "current", query=query)
    target = query.ask_target()
    validate_history(query, "alpha", history, target)
    original = history["dependencies"][0]
    for kind in ("turns", "chosen", "snapshots", "preferences"):
        conflicting = deepcopy(original)
        if kind == "turns":
            conflicting[kind][0]["revision"] += 1
        elif kind == "chosen":
            conflicting[kind][0]["entry"]["revision"] += 1
        elif kind == "snapshots":
            conflicting[kind][0][1]["roots"][0]["revision"] += 1
        else:
            key = next(iter(conflicting[kind]))
            conflicting[kind][key] += 1
        for dependencies in ([original, conflicting], [conflicting, original]):
            with pytest.raises(RecognitionError, match="history_dependency_changed"):
                validate_history(query, "alpha", {**history, "dependencies": dependencies}, target)


def test_shared_history_guard_rereads_manual_forget_and_source_revision(env):
    from copy import deepcopy
    import pytest
    from backend.memory_app.v2.followup import validate_history
    from backend.memory_app.recall_preferences import set_preference
    from backend.recognition import RecognitionError, WorkScope

    insight, experience = publish(env)
    first = ask(env).json()
    row = env.records.read("v2_turns", first["turn"]["id"])
    with env.records.begin() as tx:
        for index in range(2):
            payload = deepcopy(row.payload)
            payload["created_at"] = "999" + str(index)
            tx.put("v2_turns", "shared-" + str(index), payload, expected_revision=0)
        tx.commit()
    query = env.domains.query
    history = read_history(env.records, "alpha", first["thread_id"], "current", query=query)
    assert len(history["turns"]) == 3
    target = query.ask_target()
    validate_history(query, "alpha", history, target)
    set_preference(env.records, WorkScope("local-user", "alpha"), insight.id,
        recognition_revision=1, preference_revision=0, state="forgotten")
    with pytest.raises(RecognitionError):
        validate_history(query, "alpha", history, target)
    set_preference(env.records, WorkScope("local-user", "alpha"), insight.id,
        recognition_revision=1, preference_revision=1, state="normal")
    history = read_history(env.records, "alpha", first["thread_id"], "current", query=query)
    validate_history(query, "alpha", history, target)
    with env.records.begin() as tx:
        source = tx.read("recognition_experiences", experience)
        tx.put("recognition_experiences", experience, source.payload, expected_revision=source.revision)
        tx.commit()
    with pytest.raises(RecognitionError):
        validate_history(query, "alpha", history, target)


def test_document_history_guard_rechecks_source_snapshot_without_document_change(env):
    import pytest
    from tests.memory_app.v2.test_workbench_ask import add_document
    from backend.memory_app.v2.followup import validate_history
    from backend.recognition import RecognitionError

    from backend.memory_app.document_recognition import ensure_document_experience

    document, _ = add_document(env, summary="alpha beta gamma", body="alpha beta gamma")
    experience, _ = ensure_document_experience(env.documents, env.service, "alpha", document)
    first = ask(env).json()
    query = env.domains.query
    history = read_history(env.records, "alpha", first["thread_id"], "current", query=query)
    assert history["text"]
    assert all(candidate["kind"] == "document"
        for dependency in history["dependencies"] for candidate in dependency["chosen"])
    target = query.ask_target()
    validate_history(query, "alpha", history, target)
    frozen_document = env.records.read("documents", document)
    with env.records.begin() as tx:
        source = tx.read("recognition_experiences", experience)
        tx.put("recognition_experiences", experience, source.payload, expected_revision=source.revision)
        tx.commit()
    assert env.records.read("documents", document) == frozen_document
    with pytest.raises(RecognitionError):
        validate_history(query, "alpha", history, target)


def test_history_guard_reads_target_once_and_rejects_later_target_change(env, monkeypatch):
    import pytest
    from backend.memory_app.v2.followup import validate_history
    from backend.recognition import RecognitionError

    publish(env)
    first = ask(env).json()
    query = env.domains.query
    history = read_history(env.records, "alpha", first["thread_id"], "current", query=query)
    target = query.ask_target()
    original = env.model.public
    reads = []
    changed = [False]

    def public():
        reads.append(True)
        result = original()
        if changed[0]:
            result["generation"]["revision"] += 1
        return result

    # Local target isolates the target read from the separate remote consent check.
    def local_public():
        result = public()
        result["generation"]["base_url"] = "http://127.0.0.1:9999/v1"
        return result

    monkeypatch.setattr(env.model, "public", local_public)
    target = query.ask_target()
    reads.clear()
    validate_history(query, "alpha", history, target)
    assert len(reads) == 1
    changed[0] = True
    for dependencies in (history["dependencies"], []):
        with pytest.raises(RecognitionError, match="history_authority_changed"):
            validate_history(query, "alpha", {**history, "dependencies": dependencies}, target)


def test_failed_rewrite_persists_observed_only_usage_in_kernel_receipt(env):
    publish(env)
    first = ask(env).json()
    original = env.model.complete

    def complete(messages, **kwargs):
        if "condensed_question" in messages[0]["content"]:
            raise RuntimeError("synthetic rewrite failure")
        return original(messages, **kwargs)

    env.model.complete = complete
    response = ask(env, thread_id=first["thread_id"])
    assert response.status_code == 200
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["trace"][0]["condense_status"] == "failed"
    assert receipt["model_usage"]["observed_only"] is True
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    store = SQLiteAITurnStore(env.root / '.rebuild-data/ai-turns.sqlite3')
    saved = store.get_immutable_payload(response.json()['turn']['id'], 'product-answer-result-v2')[1]
    assert saved['receipt']['ask']['model_usage'] == receipt['model_usage']


def kernel_wire_receipt(env, reference):
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    store = SQLiteAITurnStore(env.root / ".rebuild-data" / "ai-turns.sqlite3")
    payload = store.get(reference)
    if "status" in payload:
        return payload
    events = store.events_after(payload["turn_id"])
    event = next(event for event in events if event["type"] == "model.attempt.terminal"
        and reference in event["data"]["evidence_refs"])
    return store.get(event["data"]["receipt_ref"])
