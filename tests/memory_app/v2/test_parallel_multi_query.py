"""Real retrieval entry points overlap while preserving deterministic fusion."""
import asyncio
import json
from threading import Barrier, Event, get_ident

import pytest

from backend.memory_app.v2.multi_query import _collect_variants

from tests.memory_app.v2.test_workbench_ask import env as _env_fixture, publish, ask

env = _env_fixture


def test_three_variants_collect_concurrently_and_keep_real_citations(env):
    insight, _ = publish(env)
    variants = [f"alpha beta gamma {word}" for word in ("first", "second", "third")]
    original_model = env.model.complete
    original_collect = env.domains.query.collect_candidates
    barrier = Barrier(3, timeout=2)
    collectors = {}

    def complete(messages, **kwargs):
        if '"queries"' in messages[0]["content"]:
            kwargs["validate_current"]()
            return json.dumps({"queries": variants}), {"usage": {"total_tokens": 5}}
        return original_model(messages, **kwargs)

    def collect(project, question, **kwargs):
        if question in variants:
            collectors[question] = get_ident()
            barrier.wait()
        return original_collect(project, question, **kwargs)

    env.model.complete = complete
    env.domains.query.collect_candidates = collect
    response = ask(env, text="equivalent terminology?")
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["trace"][0]["rewrite"] == {"queries": variants, "used": True}
    assert len(set(collectors.values())) == 3
    assert [row["id"] for row in receipt["citations"]] == [insight.id]
    assert receipt["model_usage"] == {"total_tokens": 12}


def test_repeated_cancellation_settles_real_query_workers(env):
    publish(env)
    original_collect = env.domains.query.collect_candidates
    entered, release = Event(), Event()
    barrier = Barrier(3, action=entered.set, timeout=3)
    finished = []

    def collect(project, question, **kwargs):
        barrier.wait()
        assert release.wait(3)
        result = original_collect(project, question, **kwargs)
        finished.append(question)
        return result

    env.domains.query.collect_candidates = collect

    async def run():
        pending = asyncio.create_task(_collect_variants(env.domains.query, "alpha", ["alpha", "beta", "gamma"]))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert sorted(finished) == ["alpha", "beta", "gamma"]

    asyncio.run(run())


def test_failed_variant_waits_for_other_real_collectors(env):
    publish(env)
    original_collect = env.domains.query.collect_candidates
    entered, release = Event(), Event()
    barrier = Barrier(3, action=entered.set, timeout=3)
    finished = []

    def collect(project, question, **kwargs):
        barrier.wait()
        if question == "alpha":
            raise ValueError("synthetic_retrieval_failure")
        assert release.wait(3)
        result = original_collect(project, question, **kwargs)
        finished.append(question)
        return result

    env.domains.query.collect_candidates = collect

    async def run():
        pending = asyncio.create_task(_collect_variants(env.domains.query, "alpha", ["alpha", "beta", "gamma"]))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            await asyncio.sleep(0)
            assert not pending.done()
        finally:
            release.set()
            with pytest.raises(ValueError, match="synthetic_retrieval_failure"):
                await pending
        assert sorted(finished) == ["beta", "gamma"]

    asyncio.run(run())
