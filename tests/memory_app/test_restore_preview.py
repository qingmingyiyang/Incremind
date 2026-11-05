import asyncio
import json
import os
from pathlib import Path
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

from backend.memory_app.backup import fingerprint_runtime_restore_source
from backend.memory_app.v2.daily import DailyJobs
from backend.memory_app.v2.learning_events import COLLECTION
from backend.memory_app.v2.policies import get
from core.storage_provider import SQLiteStructuredRecordStore


def test_real_maintenance_pause_waits_for_actual_callback_and_freezes_checkpoint(tmp_path):
    root = tmp_path / 'r'
    records = SQLiteStructuredRecordStore(root / '.rebuild-data' / 'structured-records.sqlite3')
    with records.begin() as tx:
        tx.put(COLLECTION, 'default', {'score': 0, 'run_seconds': 0, 'seen_event_ids': []}, expected_revision=0)
        tx.commit()

    async def exercise():
        daily = DailyJobs(records=records, initial_delay=.01, check_interval=.02)
        entered, release = threading.Event(), threading.Event()
        def write_original():
            entered.set()
            release.wait()
            (root / 'actual-maintenance.txt').write_text('actual background callback completed')
        callback = asyncio.create_task(daily._run_callback(write_original))
        while not entered.is_set():
            await asyncio.sleep(.005)
        token = object()
        pause = asyncio.create_task(daily.pause_maintenance(token))
        await asyncio.sleep(.03)
        assert not pause.done()
        release.set()
        await callback
        await pause
        assert (root / 'actual-maintenance.txt').is_file()
        await daily.start()
        first = fingerprint_runtime_restore_source(root)
        await asyncio.sleep(.1)
        assert fingerprint_runtime_restore_source(root) == first
        daily.resume_maintenance(token)
        for _ in range(100):
            if records.read(COLLECTION, 'default').revision > 1:
                break
            await asyncio.sleep(.01)
        assert records.read(COLLECTION, 'default').revision > 1
        await daily.stop()
    asyncio.run(exercise())


def _application(tmp_path, monkeypatch):
    root = tmp_path / 'r'
    (root / 'config').mkdir(parents=True)
    shutil.copyfile('config/settings.toml.example', root / 'config/settings.toml')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root.resolve()))
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root.resolve()
    from backend.memory_app.app import create_app
    app = create_app(runtime_root=root)
    records = app.state.recognition_records
    with records.begin() as tx:
        tx.put(COLLECTION, 'default', {'score': 0, 'run_seconds': 0, 'seen_event_ids': []}, expected_revision=0)
        tx.commit()
    app.state.memory_daily_jobs.check_interval = .02
    app.state.memory_daily_jobs.initial_delay = .01
    return root, app


def test_actual_factory_preview_stays_stable_with_clock_then_rejects_real_source_change(tmp_path, monkeypatch):
    root, app = _application(tmp_path, monkeypatch)
    with TestClient(app) as client:
        snap = client.post('/api/rebuild/memory-snapshots', json={})
        assert snap.status_code == 200, snap.text
        endpoint = '/api/rebuild/memory-snapshots/' + snap.json()['snapshot_id']
        plan = client.post(endpoint + '/rollback-plan', json={})
        assert plan.status_code == 200, plan.text
        plan = plan.json()
        assert plan['preview_lifetime_seconds'] == 180
        first = fingerprint_runtime_restore_source(root)
        time.sleep(.15)
        assert fingerprint_runtime_restore_source(root) == first
        (root / 'actual-user-original.md').write_text('actual source mutation after preview')
        rejected = client.post(endpoint + '/rollback', json={'confirm': True, 'rollback_id': plan['rollback_id']})
        assert rejected.status_code == 200 and rejected.json()['status'] == 'rejected'
        assert client.get('/api/rebuild/memory-snapshots').json()['restore_operations'] == []
        assert not app.state.memory_daily_jobs.maintenance_paused
        second = client.post(endpoint + '/rollback-plan', json={})
        assert second.status_code == 200, second.text
        prepared = client.post(endpoint + '/rollback', json={'confirm': True, 'rollback_id': second.json()['rollback_id']})
        assert prepared.status_code == 202, prepared.text
        assert prepared.json()['restore_executed'] is False
        assert app.state.memory_daily_jobs.maintenance_paused
        late = client.post('/api/v2/backups/restore-previews/' + second.json()['preview_id'] + '/release')
        assert late.status_code == 200 and late.json()['status'] == 'restore_accepted'
        assert app.state.memory_daily_jobs.maintenance_paused


def test_actual_factory_cancel_is_per_preview_and_fixed_expiry_resumes_real_jobs(tmp_path, monkeypatch):
    root, app = _application(tmp_path, monkeypatch)
    with TestClient(app) as client:
        snap = client.post('/api/rebuild/memory-snapshots', json={})
        assert snap.status_code == 200, snap.text
        endpoint = '/api/rebuild/memory-snapshots/' + snap.json()['snapshot_id']
        first = client.post(endpoint + '/rollback-plan', json={}).json()
        cancel = '/api/v2/backups/restore-previews/' + first['preview_id'] + '/release'
        assert client.post(cancel).json()['status'] == 'released'
        refused = client.post(endpoint + '/rollback', json={'confirm': True, 'rollback_id': first['rollback_id']})
        assert refused.status_code == 409 and refused.json()['detail'] == 'restore_preview_expired'
        second_response = client.post(endpoint + '/rollback-plan', json={})
        assert second_response.status_code == 200, second_response.text
        second = second_response.json()
        assert second['preview_id'] != first['preview_id']
        assert client.post(cancel).json()['status'] == 'settled'
        assert app.state.memory_daily_jobs.maintenance_paused
        current = app.state.recognition_records.read(COLLECTION, 'default').revision
        lifetime = get('restore_preview', version='@1')()['lifetime_seconds']
        started = time.monotonic()
        while app.state.memory_daily_jobs.maintenance_paused and time.monotonic() - started < lifetime + 5:
            time.sleep(.2)
        assert not app.state.memory_daily_jobs.maintenance_paused
        assert time.monotonic() - started >= lifetime - 2
        expired = client.post(endpoint + '/rollback', json={'confirm': True, 'rollback_id': second['rollback_id']})
        assert expired.status_code == 409 and expired.json()['detail'] == 'restore_preview_expired'
        for _ in range(100):
            if app.state.recognition_records.read(COLLECTION, 'default').revision > current:
                break
            time.sleep(.02)
        assert app.state.recognition_records.read(COLLECTION, 'default').revision > current
        output = root.parent / 'preview-expiry-result.json'
        output.write_text(json.dumps({'elapsed_seconds': time.monotonic() - started,
            'expired_confirmation': expired.json(), 'background_resumed': True}), encoding='utf8')


def test_actual_factory_busy_wait_does_not_claim_callback_finished(tmp_path, monkeypatch):
    root, app = _application(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def actual_callback():
        entered.set()
        release.wait()
        (root / 'completed-maintenance.txt').write_text('actual callback physically completed')
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as executor:
        snap = client.post('/api/rebuild/memory-snapshots', json={})
        assert snap.status_code == 200, snap.text
        task = executor.submit(client.portal.call, app.state.memory_daily_jobs._run_callback, actual_callback)
        assert entered.wait(5)
        try:
            started = time.monotonic()
            endpoint = '/api/rebuild/memory-snapshots/' + snap.json()['snapshot_id'] + '/rollback-plan'
            busy = client.post(endpoint, json={})
            assert busy.status_code == 409 and busy.json()['detail'] == 'restore_preview_busy'
            assert time.monotonic() - started >= get('restore_preview', version='@1')()['drain_seconds']
            assert not task.done()
            assert not app.state.memory_daily_jobs.maintenance_paused
            assert not (root / 'completed-maintenance.txt').exists()
        finally:
            release.set()
            task.result(timeout=5)
        assert (root / 'completed-maintenance.txt').is_file()
        ready = client.post(endpoint, json={})
        assert ready.status_code == 200, ready.text


def test_real_event_wakeup_cannot_enter_a_new_maintenance_pause(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with records.begin() as tx:
        tx.put(COLLECTION, 'default', {'score': 0, 'run_seconds': 0, 'seen_event_ids': []}, expected_revision=0)
        tx.commit()
    async def exercise():
        daily = DailyJobs(records=records)
        first, second = object(), object()
        await daily.pause_maintenance(first)
        waiting = asyncio.create_task(daily._checkpoint())
        await asyncio.sleep(0)
        daily.resume_maintenance(first)
        await daily.pause_maintenance(second)
        await asyncio.sleep(.03)
        assert not waiting.done() and daily._maintenance_active == 0
        assert records.read(COLLECTION, 'default').revision == 1
        daily.resume_maintenance(second)
        await waiting
        assert daily._last_clock is not None
    asyncio.run(exercise())


def test_real_journal_read_race_keeps_confirmed_preview_paused(tmp_path, monkeypatch):
    from backend.shared.deployment import runtime_backup_roots
    from backend.memory_app.runtime_recovery import prepare_runtime_recovery, queue_runtime_recovery
    from core.storage_provider.vault_operational_recovery import _JOURNAL_IO_LOCK, VaultOperationalRecoveryError
    root, app = _application(tmp_path, monkeypatch)
    with TestClient(app) as client:
        snap = client.post('/api/rebuild/memory-snapshots', json={})
        assert snap.status_code == 200, snap.text
        snapshot_id = snap.json()['snapshot_id']
        plan = client.post('/api/rebuild/memory-snapshots/' + snapshot_id + '/rollback-plan', json={}).json()
        _working, snapshots, operations = runtime_backup_roots(root)
        prepare_runtime_recovery(snapshot_root=snapshots / snapshot_id, active_root=root,
            operations_root=operations, operation_id=plan['operation_id'],
            expected_source_fingerprint=fingerprint_runtime_restore_source(root))
        (root / 'changed-original.md').write_text('real source drift before execution')
        with pytest.raises(VaultOperationalRecoveryError):
            queue_runtime_recovery(operations_root=operations, operation_id=plan['operation_id'])
        entered, unblock = threading.Event(), threading.Event()
        def hold_real_journal():
            with _JOURNAL_IO_LOCK:
                entered.set()
                unblock.wait()
        async def exercise():
            owner = app.state.runtime_restore_previews
            request = Request({'type': 'http', 'app': app, 'method': 'POST', 'path': '/', 'headers': []})
            release = asyncio.create_task(owner.release(request, plan['preview_id']))
            await asyncio.sleep(.05)
            assert not release.done()
            preview = await owner.confirm(request, snapshot_id, plan['rollback_id'])
            pending = await asyncio.to_thread(client.post,
                '/api/v2/backups/restore-previews/' + plan['preview_id'] + '/release')
            assert pending.status_code == 200 and pending.json()['status'] == 'confirmation_pending'
            unblock.set()
            result = await release
            assert result['status'] == 'confirmation_pending'
            assert owner.current is preview and owner.daily.maintenance_paused
            await owner.finish(preview)
            assert not owner.daily.maintenance_paused
            (root.parent / 'preview-race-result.json').write_text(json.dumps({
                'concurrent_http_release': pending.json(), 'blocked_release': result,
                'confirmation_retained_pause': True, 'failed_pre_adoption_resumed': True}), encoding='utf8')
        with ThreadPoolExecutor(max_workers=1) as executor:
            held = executor.submit(hold_real_journal)
            assert entered.wait(5)
            try:
                client.portal.call(exercise)
            finally:
                unblock.set()
                held.result(timeout=5)
        next_plan = client.post('/api/rebuild/memory-snapshots/' + snapshot_id + '/rollback-plan', json={})
        assert next_plan.status_code == 200, next_plan.text
        released = client.post('/api/v2/backups/restore-previews/' + next_plan.json()['preview_id'] + '/release')
        assert released.status_code == 200 and released.json()['status'] == 'released'
        stale = client.post('/api/rebuild/memory-snapshots/' + snapshot_id + '/rollback',
            json={'confirm': True, 'rollback_id': next_plan.json()['rollback_id']})
        assert stale.status_code == 409 and stale.json()['detail'] == 'restore_preview_expired'


def test_actual_legacy_api_keeps_original_restore_without_a_daily_scheduler(tmp_path, monkeypatch):
    root = tmp_path / 'l'
    (root / 'config').mkdir(parents=True)
    shutil.copyfile('config/settings.toml.example', root / 'config/settings.toml')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root.resolve()))
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root.resolve()
    from backend.api.app import create_app
    from backend.api.bootstrap import build_api_container
    app = create_app(build_api_container(root))
    assert not hasattr(app.state, 'memory_daily_jobs')
    with TestClient(app) as client:
        snapshot = client.post('/api/rebuild/memory-snapshots', json={})
        assert snapshot.status_code == 200, snapshot.text
        endpoint = '/api/rebuild/memory-snapshots/' + snapshot.json()['snapshot_id']
        plan = client.post(endpoint + '/rollback-plan', json={})
        assert plan.status_code == 200, plan.text
        prepared = client.post(endpoint + '/rollback', json={'rollback_id': plan.json()['rollback_id'], 'confirm': True})
        assert prepared.status_code == 202, prepared.text
        assert prepared.json()['restore_executed'] is False


def test_actual_duplicate_confirm_cannot_finish_the_first_handler_lease(tmp_path, monkeypatch):
    from core.storage_provider.vault_operational_recovery import _JOURNAL_IO_LOCK
    root = tmp_path / 'r'
    (root / 'config').mkdir(parents=True)
    shutil.copyfile('config/settings.toml.example', root / 'config/settings.toml')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root.resolve()))
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root.resolve()
    from backend.memory_app.app import create_app
    from backend.memory_app.runtime_restore_host import RuntimeRestoreHost
    host = RuntimeRestoreHost(root=root, factory=lambda: create_app(runtime_root=root))
    with TestClient(host) as client, ThreadPoolExecutor(max_workers=2) as executor:
        snapshot = client.post('/api/rebuild/memory-snapshots', json={})
        assert snapshot.status_code == 200, snapshot.text
        endpoint = '/api/rebuild/memory-snapshots/' + snapshot.json()['snapshot_id']
        response = client.post(endpoint + '/rollback-plan', json={})
        assert response.status_code == 200, response.text
        plan = response.json()
        daily = host._current.state.memory_daily_jobs
        entered, unblock = threading.Event(), threading.Event()
        def hold_real_journal():
            with _JOURNAL_IO_LOCK:
                entered.set()
                unblock.wait()
        held = executor.submit(hold_real_journal)
        assert entered.wait(5)
        first = executor.submit(client.post, endpoint + '/rollback',
            json={'rollback_id': plan['rollback_id'], 'confirm': True})
        try:
            async def wait_confirming():
                owner = host._current.state.runtime_restore_previews
                while not owner.current.confirming:
                    await asyncio.sleep(.005)
            client.portal.call(wait_confirming)
            results = []
            for extra in ({'confirm': False}, {'confirm': True, 'execute': True, 'operation_id': 'restore-wrong'}):
                duplicate = client.post(endpoint + '/rollback', json={'rollback_id': plan['rollback_id'], **extra})
                assert duplicate.status_code == 409 and duplicate.json()['detail'] == 'restore_confirmation_pending'
                results.append(duplicate.json())
                assert daily.maintenance_paused and not first.done()
                assert not (host.operations / (plan['operation_id'] + '.json')).exists()
        finally:
            unblock.set()
            held.result(timeout=5)
        accepted = first.result(timeout=30)
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()['operation_id'] == plan['operation_id']
        assert daily.maintenance_paused
        status = client.get('/api/rebuild/memory-restores/' + plan['operation_id'])
        assert status.status_code == 200 and status.json()['state'] == 'prepared'
        assert len(list(host.operations.glob('*.json'))) == 1
        (root.parent / 'duplicate-confirm-result.json').write_text(json.dumps({
            'duplicates': results, 'accepted': accepted.json()['status'],
            'operation_id': plan['operation_id'], 'readback': status.json(), 'maintenance_paused': True}), encoding='utf8')
