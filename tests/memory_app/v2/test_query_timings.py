import asyncio
from contextlib import contextmanager

from tests.memory_app.v2.test_workbench_ask import env, publish
from backend.memory_app import workspace_query


def test_query_measures_real_preparation_stages(env, monkeypatch):
    seen = []
    @contextmanager
    def stage(name):
        seen.append(name)
        yield
    monkeypatch.setattr(workspace_query, "stage", stage)
    publish(env)
    plan = env.domains.query.prepare_ask("alpha", "alpha beta gamma?")
    assert plan["chosen"]
    assert {"build_entries", "keyword", "ladder"} <= set(seen)
    preview = env.domains.query.store_ask_preview(plan)
    answer = asyncio.run(env.domains.query.execute_ask(preview, "alpha", "alpha beta gamma?", True))
    assert answer["answer"] == "Synthetic answer"
    assert {"prompt_build"} <= set(seen)
    assert "vector" in seen
    assert "timings" not in answer


def test_stream_first_token_is_observed_but_legacy_final_callback_is_not(env):
    import time
    from core.storage_provider.observability import Observation, observation_scope
    from backend.memory_app.structured_generation import AskOutput

    publish(env)
    query = env.domains.query
    def run(identity):
        observation = Observation("ask", identity)
        chunks = []
        with observation_scope(observation):
            plan = query.prepare_ask("alpha", "alpha beta gamma?")
            preview = query.store_ask_preview(plan)
            result = asyncio.run(query.execute_ask(preview, "alpha", "alpha beta gamma?", True,
                                                  on_delta=chunks.append))
        assert result["model_used"] and chunks
        return observation.snapshot()

    legacy = run("legacy")
    assert legacy["stages_ms"]["first_token"] == 0
    assert legacy["stages_ms"]["generation"] > 0

    def stream(messages, *, response_model, max_tokens, validate_current, on_delta):
        validate_current()
        time.sleep(0.012)
        on_delta("")
        on_delta("Synthetic")
        time.sleep(0.012)
        on_delta(" answer")
        return AskOutput(answer="Synthetic answer", citations=[1]), {}
    env.model.complete_stream = stream
    streamed = run("streamed")
    timings = streamed["stages_ms"]
    assert 10 <= timings["first_token"] < timings["generation"]
    assert timings["generation"] >= 20
    assert timings["read_records"] > 0
    assert timings["vector"] > 0
    assert streamed["stage_observations"]["vector"] > 0
    assert streamed["connection_count"] > 0 and streamed["statement_count"] > 0


def test_elapsed_recorder_failure_does_not_interrupt_generated_answer(env, monkeypatch):
    publish(env)
    def broken():
        raise RuntimeError("observer unavailable")
    monkeypatch.setattr(workspace_query, "current_observation", broken)
    query = env.domains.query
    plan = query.prepare_ask("alpha", "alpha beta gamma?")
    preview = query.store_ask_preview(plan)
    answer = asyncio.run(query.execute_ask(preview, "alpha", "alpha beta gamma?", True))
    assert answer["answer"] == "Synthetic answer"
    assert env.model.calls == 1


def test_retrieval_observes_actual_fts_and_optional_vector_work(tmp_path):
    from core.storage_provider.observability import Observation, observation_scope
    from backend.recognition_retrieval import retrieve, SQLiteEmbeddingCache
    from tests.recognition_retrieval.test_service import _entry, _CountingEmbedding

    observation = Observation("ask", "retrieval")
    with observation_scope(observation):
        cache = SQLiteEmbeddingCache(str(tmp_path / "vectors.sqlite3"))
        try:
            result = retrieve("project-a", "alpha beta", [_entry("one", "alpha beta gamma")],
                              embedding_provider=_CountingEmbedding(), embedding_cache=cache)
        finally:
            cache.close()
    measured = observation.snapshot()
    assert result.hits and result.trace["vector"]["status"] == "used"
    assert measured["stages_ms"]["keyword"] > 0
    assert measured["stages_ms"]["vector"] > 0
    assert measured["connection_count"] == 2
    assert measured["statement_count"] > 0
