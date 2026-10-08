import asyncio
import threading

from fastapi import FastAPI

from backend.memory_app.v2.daily import DailyJobs, install_daily_jobs


def test_startup_does_not_wait_for_slow_daily_job():
    app = FastAPI()
    jobs = install_daily_jobs(app)
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        release.wait(2)

    jobs.register("slow", slow)

    async def exercise():
        lifespan = app.router.lifespan_context(app)
        try:
            await asyncio.wait_for(lifespan.__aenter__(), 0.1)
            assert not entered.is_set()
            assert jobs.task is not None
        finally:
            release.set()
            await lifespan.__aexit__(None, None, None)

    asyncio.run(exercise())


def test_first_run_waits_for_delay_then_repeats_and_stops():
    async def exercise():
        jobs = DailyJobs(initial_delay=0.03, interval=0.03)
        calls, first, repeated = [], asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()

        def callback():
            calls.append(loop.time())
            loop.call_soon_threadsafe((first if len(calls) == 1 else repeated).set)

        jobs.register("tick", callback)
        started = loop.time()
        await jobs.start()
        assert calls == []
        await asyncio.wait_for(first.wait(), 1)
        assert calls[0] - started >= 0.03
        await asyncio.wait_for(repeated.wait(), 1)
        assert calls[1] - calls[0] >= 0.03
        await jobs.stop()
        count = len(calls)
        await asyncio.sleep(0.05)
        assert len(calls) == count
        assert jobs.task.done()

    asyncio.run(exercise())


def test_shutdown_before_first_run_cancels_pending_job():
    async def exercise():
        jobs, calls = DailyJobs(), []
        jobs.register("tick", lambda: calls.append(True))
        await jobs.start()
        await jobs.stop()
        assert calls == []
        assert jobs.task.done()

    asyncio.run(exercise())
