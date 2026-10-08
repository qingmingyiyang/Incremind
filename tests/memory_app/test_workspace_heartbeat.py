from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio.to_thread
import pytest
from fastapi import HTTPException

import backend.memory_app.workspace_intake as intake_module
from backend.memory_app.processing_lease import ProcessingLease
from backend.memory_app.workspace_items import WorkspaceItems
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore


@asynccontextmanager
async def processing(tmp_path, monkeypatch, heartbeat_error=None):
    """Real item/lease storage, fake model, and an explicitly advanced lease clock."""
    monkeypatch.setattr(intake_module, "_PROCESSING_HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(intake_module, "_PROCESSING_HEARTBEAT_RETRY_INTERVAL", 0.01)
    create_task = asyncio.create_task
    maintenance_tasks = []

    def track_task(coro, *args, **kwargs):
        task = create_task(coro, *args, **kwargs)
        if coro.__qualname__.endswith("keep_lease_alive"):
            maintenance_tasks.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", track_task)
    loop = asyncio.get_running_loop()
    entered, finished = asyncio.Event(), asyncio.Event()
    draft_built = asyncio.Event()
    original_draft = intake_module.generation._draft

    def draft(*args):
        result = original_draft(*args)
        draft_built.set()
        return result

    monkeypatch.setattr(intake_module.generation, "_draft", draft)
    release = threading.Event()
    beats = asyncio.Queue()
    clock = [100.0]
    records = SQLiteStructuredRecordStore(tmp_path / "heartbeat.sqlite3")
    lease = ProcessingLease(records, "workspace_items", "instance-a", clock=lambda: clock[0])
    original_heartbeat = lease.heartbeat
    attempts = []

    def heartbeat(*args):
        attempts.append(args)
        try:
            if heartbeat_error:
                heartbeat_error(len(attempts))
            renewed = original_heartbeat(*args)
            loop.call_soon_threadsafe(beats.put_nowait, renewed)
            return renewed
        except Exception as exc:
            loop.call_soon_threadsafe(beats.put_nowait, exc)
            raise

    monkeypatch.setattr(lease, "heartbeat", heartbeat)

    class Model:
        def complete(self, messages, *, max_tokens, validate_current=None):
            try:
                if validate_current:
                    validate_current()
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(10), "test did not release model"
                return json.dumps({
                    "title": "Title", "summary": "Summary", "topics": [],
                    "facts": [{"text": "original", "evidence": {"quote": "original"}}],
                    "todos": [], "uncertainties": [], "people": [], "dates": [], "suggestions": [],
                }), {}
            finally:
                loop.call_soon_threadsafe(finished.set)

    items = WorkspaceItems(records, lease, threading.RLock())
    intake = intake_module.WorkspaceIntake(tmp_path, items, Model())
    item = items.create("default", "text", "Title", "original source")
    task = asyncio.create_task(intake.process(item["id"], {}))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        maintenance, = maintenance_tasks
        yield SimpleNamespace(
            clock=clock, records=records, lease=lease, task=task, maintenance=maintenance,
            release=release, beats=beats, attempts=attempts, item_id=item["id"],
            draft_built=draft_built,
            row=lambda: lease._with_heartbeat(records, records.read("workspace_items", item["id"])),
        )
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await asyncio.wait_for(finished.wait(), 5)


def test_shared_model_pool_saturation_does_not_block_heartbeat(tmp_path, monkeypatch):
    async def scenario():
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        try:
            async with processing(tmp_path, monkeypatch) as run:
                assert limiter.borrowed_tokens == 1  # The blocked model owns every shared slot.
                run.clock[0] = 150.0
                while run.row().payload["processing_lease_expires_at"] <= 160:
                    assert await asyncio.wait_for(run.beats.get(), 5) is True
                run.clock[0] = 161.0  # Beyond initial expiry; only the heartbeat can save this run.
                assert run.lease.recover_expired() == 0
                run.release.set()
                result = await asyncio.wait_for(run.task, 5)
                assert result["status"] == "ready"
                assert result["source_text"] == "original source"
        finally:
            limiter.total_tokens = previous
    asyncio.run(scenario())


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                                  sqlite3.SQLITE_BUSY | (2 << 8), None])
def test_one_lock_failure_is_retried_and_processing_finishes(tmp_path, monkeypatch, code):
    def fail_once(attempt):
        if attempt == 1:
            error = sqlite3.OperationalError("database is locked")
            if code is not None:
                error.sqlite_errorcode = code
            raise error

    async def scenario():
        async with processing(tmp_path, monkeypatch, fail_once) as run:
            run.clock[0] = 150.0
            assert isinstance(await asyncio.wait_for(run.beats.get(), 5), sqlite3.OperationalError)
            while run.row().payload["processing_lease_expires_at"] <= 160:
                assert await asyncio.wait_for(run.beats.get(), 5) is True
            run.clock[0] = 161.0
            run.release.set()
            assert (await asyncio.wait_for(run.task, 5))["status"] == "ready"
            assert len(run.attempts) >= 2
    asyncio.run(scenario())


def test_continuous_busy_has_bounded_retries_and_original_expiry(tmp_path, monkeypatch):
    def always_busy(attempt):
        raise sqlite3.OperationalError("database is locked")

    async def scenario():
        async with processing(tmp_path, monkeypatch, always_busy) as run:
            await asyncio.wait_for(asyncio.shield(run.maintenance), 5)
            assert len(run.attempts) == 1 + intake_module._PROCESSING_HEARTBEAT_MAX_RETRIES
            assert run.row().payload["processing_lease_expires_at"] == 160.0
            run.clock[0] = 161.0
            run.release.set()
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(run.task, 5)
            assert exc.value.detail == "processing_lease_expired"
            assert run.lease.recover_expired() == 1
            assert run.row().payload["error"] == "processing_interrupted"
            assert run.row().payload["draft"] is None
    asyncio.run(scenario())


@pytest.mark.parametrize("superseded", [False, True])
def test_cancel_uses_reserved_capacity_and_cannot_interrupt_new_run(tmp_path, monkeypatch, superseded):
    async def scenario():
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        try:
            async with processing(tmp_path, monkeypatch) as run:
                assert limiter.borrowed_tokens == 1
                old_run = run.row().payload["processing_run_id"]
                if superseded:
                    assert run.lease.interrupt(run.item_id, "default", old_run)
                    run.lease.claim(run.item_id, "default", run.row().revision, "new-run", None, {})
                    before = run.row()
                run.task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(run.task, 5)
                assert not run.release.is_set()  # Cancellation did not wait for the occupied model pool.
                if superseded:
                    assert run.row() == before
                    assert run.lease.heartbeat(run.item_id, "default", old_run) is False
                    assert run.row() == before
                else:
                    assert run.row().payload["status"] == "failed"
                    assert run.row().payload["error"] == "processing_interrupted"
                    assert run.row().payload["processing_run_id"] is None
                assert run.row().payload["draft"] is None
        finally:
            limiter.total_tokens = previous
    asyncio.run(scenario())


def test_inflight_heartbeat_after_cancel_cannot_modify_replacement_run(tmp_path, monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        heartbeat_entered = asyncio.Event()
        release_heartbeat = threading.Event()

        def delay_heartbeat(attempt):
            loop.call_soon_threadsafe(heartbeat_entered.set)
            assert release_heartbeat.wait(5)

        async with processing(tmp_path, monkeypatch, delay_heartbeat) as run:
            try:
                await asyncio.wait_for(heartbeat_entered.wait(), 5)
                old_run = run.row().payload["processing_run_id"]
                assert run.lease.interrupt(run.item_id, "default", old_run)
                run.lease.claim(run.item_id, "default", run.row().revision, "replacement", None, {})
                before = run.row()
                run.task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(run.task, 5)
            finally:
                release_heartbeat.set()
                outcome = await asyncio.wait_for(run.beats.get(), 5)
            assert outcome is False
            assert run.row() == before
    asyncio.run(scenario())


@pytest.mark.parametrize("error", [sqlite3.OperationalError("disk I/O error"), RuntimeError("unexpected")])
def test_unknown_heartbeat_error_is_visible_and_cannot_publish_ready(tmp_path, monkeypatch, error):
    def fail(attempt):
        raise error

    async def scenario():
        async with processing(tmp_path, monkeypatch, fail) as run:
            with pytest.raises(type(error), match=str(error)):
                await asyncio.wait_for(asyncio.shield(run.maintenance), 5)
            assert len(run.attempts) == 1
            run.release.set()
            with pytest.raises(type(error), match=str(error)):
                await asyncio.wait_for(run.task, 5)
            assert run.row().payload["status"] == "failed"
            assert run.row().payload["error"] == "processing_failed"
            assert run.row().payload["draft"] is None
    asyncio.run(scenario())


@pytest.mark.parametrize("late_error", [False, True])
def test_ready_waits_for_inflight_heartbeat_and_propagates_its_error(tmp_path, monkeypatch, late_error):
    async def scenario():
        loop = asyncio.get_running_loop()
        heartbeat_entered = asyncio.Event()
        release_heartbeat = threading.Event()

        def delayed_heartbeat(attempt):
            loop.call_soon_threadsafe(heartbeat_entered.set)
            assert release_heartbeat.wait(5)
            if late_error:
                raise RuntimeError("late heartbeat failure")

        async with processing(tmp_path, monkeypatch, delayed_heartbeat) as run:
            try:
                await asyncio.wait_for(heartbeat_entered.wait(), 5)
                run.release.set()
                await asyncio.wait_for(run.draft_built.wait(), 5)
                assert not run.task.done()
                assert run.row().payload["status"] == "processing"
                assert run.row().payload["draft"] is None
                release_heartbeat.set()
                if late_error:
                    with pytest.raises(RuntimeError, match="late heartbeat failure"):
                        await asyncio.wait_for(run.task, 5)
                    assert run.row().payload["status"] == "failed"
                    assert run.row().payload["draft"] is None
                else:
                    assert (await asyncio.wait_for(run.task, 5))["status"] == "ready"
                assert len(run.attempts) == 1  # Stopping also prevents scheduling another heartbeat.
            finally:
                release_heartbeat.set()
                await asyncio.wait_for(run.beats.get(), 5)
    asyncio.run(scenario())


@pytest.mark.parametrize("superseded", [False, True])
def test_cancel_during_final_heartbeat_wait_interrupts_only_owned_run(tmp_path, monkeypatch, superseded):
    async def scenario():
        loop = asyncio.get_running_loop()
        heartbeat_entered = asyncio.Event()
        release_heartbeat = threading.Event()

        def delayed_heartbeat(attempt):
            loop.call_soon_threadsafe(heartbeat_entered.set)
            assert release_heartbeat.wait(5)

        async with processing(tmp_path, monkeypatch, delayed_heartbeat) as run:
            try:
                await asyncio.wait_for(heartbeat_entered.wait(), 5)
                run.release.set()
                await asyncio.wait_for(run.draft_built.wait(), 5)
                if superseded:
                    old_run = run.row().payload["processing_run_id"]
                    assert run.lease.interrupt(run.item_id, "default", old_run)
                    run.lease.claim(run.item_id, "default", run.row().revision, "replacement", None, {})
                    before = run.row()
                run.task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(run.task, 5)
                assert not release_heartbeat.is_set()
                if superseded:
                    assert run.row() == before
                else:
                    assert run.row().payload["status"] == "failed"
                    assert run.row().payload["error"] == "processing_interrupted"
                after_cancel = run.row()
            finally:
                release_heartbeat.set()
                assert await asyncio.wait_for(run.beats.get(), 5) is False
            assert run.row() == after_cancel
    asyncio.run(scenario())


def test_final_heartbeat_wait_is_bounded_and_late_renewal_cannot_revive_run(tmp_path, monkeypatch):
    monkeypatch.setattr(intake_module, "_PROCESSING_HEARTBEAT_DRAIN_TIMEOUT", 0.01)

    async def scenario():
        loop = asyncio.get_running_loop()
        heartbeat_entered = asyncio.Event()
        release_heartbeat = threading.Event()

        def delayed_heartbeat(attempt):
            loop.call_soon_threadsafe(heartbeat_entered.set)
            assert release_heartbeat.wait(5)

        async with processing(tmp_path, monkeypatch, delayed_heartbeat) as run:
            try:
                await asyncio.wait_for(heartbeat_entered.wait(), 5)
                run.release.set()
                result = await asyncio.wait_for(run.task, 5)
                assert result["status"] == "failed"
                assert result["error"] == "processing_failed"
                assert result["draft"] is None
                assert not release_heartbeat.is_set()
                after_timeout = run.row()
            finally:
                release_heartbeat.set()
                assert await asyncio.wait_for(run.beats.get(), 5) is False
            assert run.row() == after_timeout
    asyncio.run(scenario())
