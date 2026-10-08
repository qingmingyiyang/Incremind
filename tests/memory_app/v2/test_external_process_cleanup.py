"""真实 Windows Job 的失败关闭与原生产宿主归属，不替换被测 owner。"""
from copy import copy
import ctypes
from ctypes import wintypes as w
import gc
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import weakref

import pytest

from backend.memory_app.v2 import external_process
from backend.memory_app.v2.external_execution import execute_external
from core.ai_kernel.dispatcher import ToolProviderFailure
from tests.memory_app.v2.test_external_host import setup_host
from tests.memory_app.v2.test_external_execution_limits import configured, claims
from tests.memory_app.v2.test_external_process import CLI, environment


class _Function:
    def __init__(self, function, call):
        object.__setattr__(self, 'function', function)
        object.__setattr__(self, 'call', call)

    def __call__(self, *args):
        return self.call(self.function, args)

    def __getattr__(self, name):
        return getattr(self.function, name)

    def __setattr__(self, name, value):
        setattr(self.function, name, value)


class _NativeFailures:
    """只注入原生 API 失败，记录真实创建代次和成功关闭回执。"""
    def __init__(self, monkeypatch, *, configuration=False, close=True, api_failure=None, query_class=None,
            process_close=False, member_close=False):
        assert os.name == 'nt'
        self.original = ctypes.WinDLL
        self.api = self.original('kernel32', use_last_error=True)
        self.api.GetHandleInformation.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        self.api.GetHandleInformation.restype = w.BOOL
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.CloseHandle.restype = w.BOOL
        self.jobs, self.owners, self.receipts, self.failures = [], [], [], []
        self.active_jobs, self.generation_receipts = {}, []
        self.configuration, self.enabled = configuration, True
        self.close_failure, self.api_failure = close, api_failure
        self.query_class = query_class
        self.process_close, self.process_receipts = process_close, []
        self.member_close, self.member_receipts = member_close, []
        self.process_generations, self.member_generations = [], []
        original_init = external_process._Owner.__init__

        def observe(owner):
            self.owners.append(owner)
            original_init(owner)

        monkeypatch.setattr(external_process._Owner, '__init__', observe)
        monkeypatch.setattr(ctypes, 'WinDLL', self.library)

    @staticmethod
    def handle(value):
        return int(getattr(value, 'value', value) or 0)

    def library(self, *args, **kwargs):
        native = self.original(*args, **kwargs)
        fault = self

        class Library:
            def __getattr__(self, name):
                function = getattr(native, name)
                if name not in {'CreateJobObjectW', 'SetInformationJobObject', 'CloseHandle',
                        'AssignProcessToJobObject', 'TerminateJobObject', 'QueryInformationJobObject'}:
                    return function

                def call(original, arguments):
                    if name == 'CreateJobObjectW':
                        value = original(*arguments)
                        if value:
                            handle = fault.handle(value)
                            fault.active_jobs[handle] = len(fault.jobs)
                            fault.jobs.append(handle)
                        return value
                    target = fault.handle(arguments[0])
                    process_owner = next((owner for owner in fault.owners if owner.process is not None
                        and not owner.process_handle_closed and target == int(owner.process._handle)), None)
                    member = next(((owner, owner.job, pid, handle) for owner in fault.owners if owner.job is not None
                        for pid, (handle, _) in owner.job.members.items() if target == handle), None)
                    if fault.enabled and fault.member_close and member is not None and name == 'CloseHandle':
                        print('实际原成员 CloseFalse:', {'handle': target, 'valid': fault.valid(target)})
                        ctypes.set_last_error(5)
                        return False
                    if fault.enabled and fault.process_close and process_owner is not None and name == 'CloseHandle':
                        print('实际原 process CloseFalse:', {'handle': target, 'valid': fault.valid(target)})
                        ctypes.set_last_error(5)
                        return False
                    generation = fault.active_jobs.get(target)
                    if fault.enabled and generation is not None:
                        if (name == 'CloseHandle' and fault.close_failure
                                or fault.configuration and name == 'SetInformationJobObject'
                                or name == fault.api_failure and (name != 'QueryInformationJobObject'
                                    or fault.query_class is None or arguments[1] == fault.query_class)):
                            if (name, target) not in fault.failures:
                                fault.failures.append((name, target))
                                print('真实 native 失败:', {'api': name, 'handle': target,
                                    'valid': fault.valid(target), 'close_receipt': target in fault.receipts})
                            ctypes.set_last_error(5 if name == 'CloseHandle' else 87)
                            return False
                    result = original(*arguments)
                    if name == 'CloseHandle' and generation is not None and result:
                        fault.receipts.append(target)
                        fault.generation_receipts.append((generation, target))
                        del fault.active_jobs[target]
                    if name == 'CloseHandle' and process_owner is not None and result:
                        fault.process_receipts.append(target)
                        fault.process_generations.append((process_owner, process_owner.process._handle, target))
                    if name == 'CloseHandle' and member is not None and result:
                        fault.member_receipts.append(target)
                        fault.member_generations.append(member)
                    return result
                return _Function(function, call)
        return Library()

    def valid(self, handle):
        return bool(self.api.GetHandleInformation(handle, ctypes.byref(w.DWORD())))

    def rescue(self):
        # RED 后只收本次仍有效且没有成功关闭回执的资源，不计入实现成功证据。
        self.enabled = False
        for handle in tuple(self.active_jobs):
            if self.valid(handle):
                assert self.api.CloseHandle(handle)
                del self.active_jobs[handle]


@pytest.mark.parametrize('configuration', [False, True])
def test_native_job_close_failure_retains_same_owner_and_allows_retry(monkeypatch, configuration):
    fault = _NativeFailures(monkeypatch, configuration=configuration)
    owner = None
    try:
        if configuration:
            with pytest.raises(OSError, match='^external_process_owner_failed$') as caught:
                external_process._Owner()
            owner = getattr(caught.value, '_cleanup_owner', None)
            assert owner is fault.owners[0]
        else:
            owner = external_process._Owner()
            with pytest.raises(OSError, match='^external_process_cleanup_incomplete$'):
                owner.close()
        handle = fault.jobs[0]
        assert owner.job.handle == handle and fault.valid(handle)
        assert owner.closed is False and fault.receipts == []
        print('实际 Job 保留:', {'handle': handle, 'configuration': configuration, 'closed': owner.closed})
        fault.enabled = False
        owner.close()
        assert owner.closed is True and owner.job.handle is None
        assert not fault.valid(handle) and fault.receipts == [handle]
        owner.close()
        assert fault.receipts == [handle]
    finally:
        fault.rescue()


@pytest.mark.parametrize('lock_name', ['lock', 'cleanup_lock'])
def test_original_owner_lock_contention_is_inside_cleanup_deadline(monkeypatch, lock_name):
    fault = _NativeFailures(monkeypatch, close=False)
    owner = external_process._Owner()
    acquired = threading.Event()
    def hold():
        with getattr(owner, lock_name):
            acquired.set()
            time.sleep(.3)
    thread = threading.Thread(target=hold)
    thread.start()
    assert acquired.wait(1)
    try:
        beginning = time.monotonic()
        with pytest.raises(OSError, match='^external_process_cleanup_incomplete$'):
            owner.close(deadline=beginning + .05)
        assert time.monotonic() - beginning < .15
        assert owner.closed is False and owner.job.handle == fault.jobs[0]
        assert fault.valid(fault.jobs[0]) and fault.receipts == []
    finally:
        thread.join(timeout=1)
        owner.close()
        fault.rescue()


def test_unstarted_reader_failure_closes_original_real_bootstrap(monkeypatch, tmp_path):
    fault = _NativeFailures(monkeypatch, close=False)
    original_start = threading.Thread.start
    def fail_first_reader(thread):
        if thread.name == 'external-process-stdout':
            raise RuntimeError('synthetic-start-failure')
        return original_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', fail_first_reader)
    try:
        result = external_process.run_process([sys._base_executable, '-c', ''], cwd=tmp_path,
            environment={key:value for key,value in os.environ.items()
                if key.upper() in {'SYSTEMROOT','WINDIR','TEMP','TMP'}}, timeout=1)
        assert result == external_process.ProcessResult('failed', None, 0, ('external_process_start_failed',))
        owner = fault.owners[0]
        assert owner.closed and owner.process.poll() is not None
        assert all(stream.closed for stream in owner.streams)
        assert all(not thread.is_alive() for thread in owner.threads)
        assert fault.receipts == [fault.jobs[0]] and not fault.valid(fault.jobs[0])
    finally:
        fault.rescue()


def test_native_popen_handle_close_failure_preserves_original_handle_and_retry(monkeypatch, tmp_path):
    fault = _NativeFailures(monkeypatch, close=False, process_close=True)
    close_api = fault.library('kernel32', use_last_error=True).CloseHandle
    close_api.argtypes, close_api.restype = [w.HANDLE], w.BOOL
    def winapi_close(handle):
        if not close_api(handle):
            raise OSError('synthetic-native-close-failure')
    # 原 Handle.Close 的默认参数已绑定 C API，仅替换该 API 边界，保留原包装器。
    monkeypatch.setattr(subprocess.Handle.Close, '__defaults__', (winapi_close,))
    handle = None
    try:
        with pytest.raises(external_process.ProcessCleanupError) as caught:
            external_process.run_process([sys._base_executable, '-c', ''], cwd=tmp_path,
                environment=environment(), timeout=1)
        owner = caught.value._cleanup_owner
        handle = int(owner.process._handle)
        assert owner is fault.owners[0] and owner.process.poll() is not None
        assert owner.closed is False and owner.process_handle_closed is False
        assert fault.valid(handle) and owner.process._handle.closed is False
        assert fault.process_receipts == []
        fault.enabled = False
        owner.close()
        assert owner.closed and owner.process_handle_closed and owner.process._handle.closed
        assert not fault.valid(handle) and fault.process_receipts == [handle]
        assert fault.process_generations == [(owner, owner.process._handle, handle)]
        owner.close()
        assert fault.process_receipts == [handle]
    finally:
        fault.enabled = False
        if handle is not None and fault.valid(handle):
            assert fault.api.CloseHandle(handle)
        fault.rescue()


def test_native_tree_member_close_failure_keeps_same_generation_handles_for_retry(monkeypatch, tmp_path):
    fault = _NativeFailures(monkeypatch, close=False, member_close=True)
    script = tmp_path / 'native-member-tree.py'
    script.write_text(CLI, encoding='utf-8')
    cancel = threading.Event()
    def observe(line):
        if line == 'tree_ready': cancel.set()
    try:
        with pytest.raises(external_process.ProcessCleanupError) as caught:
            external_process.run_process([sys._base_executable, '-u', str(script), 'tree'],
                cwd=tmp_path, environment=environment(), timeout=3, cancel=cancel, on_line=observe)
        owner = caught.value._cleanup_owner
        generations = dict(owner.job.members)
        assert owner is fault.owners[0] and owner.closed is False
        assert owner.job.handle is None and owner.job.tree_exited
        assert fault.receipts == [fault.jobs[0]] and not fault.valid(fault.jobs[0])
        assert len(generations) >= 3 and fault.member_receipts == []
        assert all(fault.valid(handle) for handle, _ in generations.values())
        assert all(owner.job.api.WaitForSingleObject(handle, 0) == 0 for handle, _ in generations.values())
        fault.enabled = False
        owner.close()
        assert owner.closed and owner.job.members == {}
        assert all(not fault.valid(handle) for handle, _ in generations.values())
        assert sorted(fault.member_receipts) == sorted(handle for handle, _ in generations.values())
        assert set(fault.member_generations) == {(owner, owner.job, pid, handle)
            for pid, (handle, _) in generations.items()}
        owner.close()
        assert len(fault.member_receipts) == len(generations) and fault.receipts == [fault.jobs[0]]
    finally:
        fault.enabled = False
        if fault.owners:
            fault.owners[0].close()
        fault.rescue()


@pytest.mark.parametrize('api_failure', ['TerminateJobObject', 'QueryInformationJobObject'])
def test_tree_exit_api_failure_keeps_original_job_until_native_exit_proved(monkeypatch, tmp_path, api_failure):
    fault = _NativeFailures(monkeypatch, close=False, api_failure=api_failure, query_class=1)
    fault.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    fault.api.OpenProcess.restype = w.HANDLE
    fault.api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    fault.api.WaitForSingleObject.restype = w.DWORD
    script = tmp_path / 'native-tree.py'
    script.write_text(CLI, encoding='utf-8')
    cancel, generations = threading.Event(), []
    def observe(line):
        if line == 'tree_ready':
            for name in ('tree', 'child', 'grand'):
                pid = int((tmp_path / (name + '.pid')).read_text())
                handle = fault.api.OpenProcess(0x00100000, False, pid)
                assert handle and fault.api.WaitForSingleObject(handle, 0) == 258
                generations.append((pid, handle))
            cancel.set()
    try:
        beginning = time.monotonic()
        with pytest.raises(external_process.ProcessCleanupError) as caught:
            external_process.run_process([sys._base_executable, '-u', str(script), 'tree'],
                cwd=tmp_path, environment=environment(), timeout=3, cancel=cancel, on_line=observe)
        owner = caught.value._cleanup_owner
        assert time.monotonic() - beginning < 3 and len(generations) == 3
        assert owner is fault.owners[0] and owner.closed is False
        assert owner.job.handle == fault.jobs[0] and fault.valid(fault.jobs[0])
        assert fault.receipts == [] and (api_failure, fault.jobs[0]) in fault.failures
        exits = [fault.api.WaitForSingleObject(handle, 0) for _, handle in generations]
        if api_failure == 'TerminateJobObject':
            assert any(value == 258 for value in exits)
        else:
            # Query 失败不能证明终止请求已完成；实际代次可能仍在退出中。
            assert all(value in {0, 258} for value in exits)
        print('实际树证明失败保留:', {'api': api_failure, 'job': fault.jobs[0],
            'process_generations': generations, 'wait_receipts': exits})
        fault.enabled = False
        owner.close()
        assert owner.closed and owner.job.handle is None
        assert [fault.api.WaitForSingleObject(handle, 0) for _, handle in generations] == [0, 0, 0]
        assert fault.receipts == [fault.jobs[0]] and not fault.valid(fault.jobs[0])
    finally:
        fault.enabled = False
        if fault.owners:
            fault.owners[0].close()
        for _, handle in generations:
            assert fault.api.CloseHandle(handle)
        fault.rescue()


@pytest.mark.parametrize('configuration', [False, True])
def test_version_probe_failure_is_strongly_retained_without_issued_lease(setup_host, monkeypatch, configuration):
    module, host, turn, plan, config, *_ = setup_host()
    fault = _NativeFailures(monkeypatch, configuration=configuration)
    try:
        beginning = time.monotonic()
        with pytest.raises(module.ExternalHostError, match='^external_host_cleanup_incomplete$'):
            host.prepare(turn, plan, mcp_config=config)
        assert time.monotonic() - beginning < 3
        assert len(host._issued_leases) == 1
        pending = next(iter(host._issued_leases))
        owner = pending._process_owner
        assert owner is fault.owners[0] and owner.job.handle == fault.jobs[0]
        assert fault.valid(fault.jobs[0]) and owner.closed is False
        with pytest.raises(module.ExternalHostError):
            pending.validate()
        copied = copy(pending)
        copied.close()
        assert owner.job.handle == fault.jobs[0] and fault.receipts == []
        reference = weakref.ref(pending)
        del pending, copied
        gc.collect()
        assert reference() is not None
        print('实际预签发保留:', {'handle': fault.jobs[0], 'pending': len(host._issued_leases)})
        fault.enabled = False
        host.close()
        assert owner.closed and host._issued_leases == {}
        assert fault.receipts == [fault.jobs[0]] and not fault.valid(fault.jobs[0])
        host.close()
        assert fault.receipts == [fault.jobs[0]]
    finally:
        fault.rescue()


@pytest.mark.parametrize('configuration,api_failure', [(False, None), (True, None),
    (False, 'AssignProcessToJobObject')])
def test_real_execution_cleanup_failure_keeps_original_owner_and_slot(configured, monkeypatch, configuration, api_failure):
    env = configured
    host = env.host()
    task = env.task(host, 1, released=True)
    accepted, original_plan = task.lease.accepted_turn, task.lease.plan
    fault = _NativeFailures(monkeypatch, configuration=configuration, api_failure=api_failure)
    unopened = configuration or api_failure == 'AssignProcessToJobObject'
    events = []
    try:
        beginning = time.monotonic()
        with pytest.raises(ToolProviderFailure) as caught:
            execute_external(task.lease, task.context, records=env.records, turns=env.turns,
                turn_id=task.identity, owner_id='local-user', event_sink=events.append)
        assert caught.value.error_code == 'external_task_cleanup_incomplete'
        assert caught.value.effect_certainty == 'unknown' and time.monotonic() - beginning < 3
        assert not [event for event in events if event['kind'] == 'finished']
        owner = task.lease._process_owner
        assert owner is fault.owners[0] and owner.job.handle == fault.jobs[0]
        assert fault.valid(fault.jobs[0]) and owner.closed is False
        assert owner.process is None if configuration else owner.process.poll() is not None
        assert [claim['turn_id'] for claim in claims(env)] == [task.identity]
        row = env.records.read('v2_external_runs', task.identity)
        assert row.payload['status'] == ('reserved' if unopened else 'running')
        assert row.payload['ended_at'] is None
        assert env.turns.get_immutable_payload(task.identity, 'external-task-result-v1') is None
        assert env.turns.get_immutable_payload(task.identity, 'external-task-receipt-v1') is None
        with pytest.raises(ToolProviderFailure) as replay:
            execute_external(task.lease, task.context, records=env.records, turns=env.turns,
                turn_id=task.identity, owner_id='local-user')
        assert replay.value.effect_certainty == 'unknown'
        if unopened:
            assert not (task.cwd / 'spawn-count').exists()
        else:
            assert (task.cwd / 'spawn-count').read_text() == '1'
        assert env.records.read('v2_external_runs', task.identity) == row
        copied = copy(task.lease)
        copied.close()
        assert owner.job.handle == fault.jobs[0] and fault.receipts == []
        with pytest.raises(OSError, match='^external_process_cleanup_incomplete$'):
            task.lease.close()
        assert task.lease in host._issued_leases and owner.job.handle == fault.jobs[0]
        print('实际执行保留:', {'handle': fault.jobs[0],
            'pid': None if configuration else owner.process.pid,
            'exit': None if configuration else owner.process.returncode, 'slot': claims(env)})
        fault.enabled = False
        host.close()
        assert owner.closed and host._issued_leases == {}
        assert fault.receipts == [fault.jobs[0]] and not fault.valid(fault.jobs[0])
        ended = env.records.read('v2_external_runs', task.identity)
        assert ended.payload['status'] == 'failed' and ended.payload['ended_at'] is not None
        assert ended.revision == row.revision + 1 and claims(env) == []
        body = env.turns.get_immutable_payload(task.identity, 'external-task-result-v1')[1]
        proof = env.turns.get_immutable_payload(task.identity, 'external-task-receipt-v1')[1]
        assert body['status'] == 'failed' and body['exit_code'] == (None if unopened else 0)
        assert body['tail'] == ['external_process_cleanup_incomplete']
        assert proof['status'] == 'failed' and proof['effect_certainty'] == 'unknown'
        assert proof['run_revision'] == ended.revision
        config = {'mcpServers':{'chriptmas-memory':{'command':sys.executable,
            'args':['-I','-m','backend.memory_app.mcp']}}}
        with host.prepare(accepted, original_plan, mcp_config=config) as replay_lease:
            with pytest.raises(ToolProviderFailure) as replay:
                execute_external(replay_lease, task.context, records=env.records, turns=env.turns,
                    turn_id=task.identity, owner_id='local-user')
            assert replay.value.error_code == 'external_task_failed'
            assert replay.value.effect_certainty == 'unknown'
        assert not (task.cwd / 'spawn-count').exists() if unopened else (task.cwd / 'spawn-count').read_text() == '1'
        next_task = env.task(host, 2, released=True)
        assert execute_external(next_task.lease, next_task.context, records=env.records, turns=env.turns,
            turn_id=next_task.identity, owner_id='local-user')['receipt_ref']
        assert claims(env) == [] and (next_task.cwd / 'spawn-count').read_text() == '1'
        assert env.records.read('v2_external_runs', task.identity) == ended
        assert fault.generation_receipts.count((0, fault.jobs[0])) == 1
        print('实际创建代次回执:', {'created': list(enumerate(fault.jobs)),
            'close_true': fault.generation_receipts, 'original_once': (0, fault.jobs[0])})
    finally:
        fault.rescue()


def test_original_cleanup_completion_retry_keeps_pending_lease_after_receipt_failure(configured, monkeypatch):
    env = configured
    host = env.host()
    task = env.task(host, 1, released=True)
    fault = _NativeFailures(monkeypatch)
    original_save = env.turns.get_or_create_immutable_payload
    interrupted = []
    def fail_receipt_once(turn_id, kind, body):
        if kind == 'external-task-receipt-v1' and not interrupted:
            interrupted.append(True)
            raise OSError('synthetic-private-save-detail')
        return original_save(turn_id, kind, body)
    try:
        with pytest.raises(ToolProviderFailure) as caught:
            execute_external(task.lease, task.context, records=env.records, turns=env.turns,
                turn_id=task.identity, owner_id='local-user')
        assert caught.value.effect_certainty == 'unknown' and len(claims(env)) == 1
        owner = task.lease._process_owner
        fault.enabled = False
        monkeypatch.setattr(env.turns, 'get_or_create_immutable_payload', fail_receipt_once)
        with pytest.raises(Exception, match='^external_host_cleanup_incomplete$'):
            host.close()
        assert owner.closed and task.lease in host._issued_leases
        assert fault.receipts == [fault.jobs[0]] and not fault.valid(fault.jobs[0])
        ended = env.records.read('v2_external_runs', task.identity)
        assert ended.payload['status'] == 'failed' and claims(env) == []
        assert env.turns.get_immutable_payload(task.identity, 'external-task-result-v1') is not None
        assert env.turns.get_immutable_payload(task.identity, 'external-task-receipt-v1') is None
        host.close()
        assert host._issued_leases == {} and fault.receipts == [fault.jobs[0]]
        assert env.records.read('v2_external_runs', task.identity) == ended
        assert env.turns.get_immutable_payload(task.identity, 'external-task-receipt-v1')[1]['effect_certainty'] == 'unknown'
    finally:
        fault.enabled = False
        host.close()
        fault.rescue()


def test_cleanup_registration_uses_original_resource_authority_after_limit_revocation(configured, monkeypatch):
    env = configured
    host = env.host()
    task = env.task(host, 1, released=True)
    fault = _NativeFailures(monkeypatch)
    changed = []
    def revoke(event):
        if event == {'kind': 'step', 'stage': 'turn'}:
            host.concurrency_limit = 2
            changed.append(True)
    try:
        with pytest.raises(ToolProviderFailure) as caught:
            execute_external(task.lease, task.context, records=env.records, turns=env.turns,
                turn_id=task.identity, owner_id='local-user', event_sink=revoke)
        assert changed == [True] and caught.value.error_code == 'external_task_cleanup_incomplete'
        assert caught.value.effect_certainty == 'unknown' and len(claims(env)) == 1
        owner = task.lease._process_owner
        assert not owner.closed and fault.valid(fault.jobs[0])
        fault.enabled = False
        host.close()
        assert owner.closed and host._issued_leases == {} and claims(env) == []
        assert env.records.read('v2_external_runs', task.identity).payload['status'] == 'failed'
        assert env.turns.get_immutable_payload(task.identity, 'external-task-receipt-v1')[1]['effect_certainty'] == 'unknown'
    finally:
        fault.enabled = False
        host.close()
        fault.rescue()


@pytest.mark.parametrize('entry', ['host', 'lease'])
def test_active_host_or_lease_close_returns_original_collector_and_releases_its_slot(configured, monkeypatch, entry):
    env = configured
    host = env.host()
    task = env.task(host, 1)
    fault = _NativeFailures(monkeypatch, close=False)
    done, results = threading.Event(), []
    reader_gate = threading.Event()
    original_run = threading.Thread.run
    def deferred_reader(thread):
        if thread.name in {'external-process-stdout', 'external-process-stderr'}:
            assert reader_gate.wait(3)
        original_run(thread)
    # 真实线程调度边界：两个 reader 在 owner 发出停止后才继续原读取函数。
    monkeypatch.setattr(threading.Thread, 'run', deferred_reader)
    release_thread = None
    def execute():
        try:
            execute_external(task.lease, task.context, records=env.records, turns=env.turns,
                turn_id=task.identity, owner_id='local-user')
        except Exception as error:
            results.append(error)
        finally:
            done.set()
    thread = threading.Thread(target=execute, name='native-active-close-consumer')
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            row = env.records.read('v2_external_runs', task.identity)
            if row is not None and row.payload['status'] == 'running' and (task.cwd / 'spawn-count').exists():
                break
            time.sleep(.01)
        assert row.payload['status'] == 'running' and (task.cwd / 'spawn-count').read_text() == '1'
        owner = task.lease._process_owner
        def release_readers():
            assert owner.finished.wait(3)
            reader_gate.set()
        release_thread = threading.Thread(target=release_readers, name='native-reader-schedule-consumer')
        release_thread.start()
        beginning = time.monotonic()
        (host if entry == 'host' else task.lease).close()
        assert owner.closed and fault.receipts == [fault.jobs[0]]
        assert done.wait(1.5), '原 owner 已关闭但原 collector 未收口'
        assert time.monotonic() - beginning < 3
        assert len(results) == 1 and isinstance(results[0], ToolProviderFailure)
        assert results[0].error_code == 'external_task_failed' and results[0].effect_certainty == 'unknown'
        assert claims(env) == [] and env.records.read('v2_external_runs', task.identity).payload['status'] == 'failed'
        assert (task.cwd / 'spawn-count').read_text() == '1'
        assert not [worker for worker in threading.enumerate() if worker.name.startswith('external-process-')]
    finally:
        reader_gate.set()
        if not done.is_set():
            # RED 安全收尾只唤醒真实 producer 的原期限；该救援不计为产品通过。
            frame = sys._current_frames().get(thread.ident)
            while frame is not None:
                if frame.f_code.co_name == 'run_process' and frame.f_code.co_filename.endswith('external_process.py'):
                    frame.f_locals['state']['cleanup_deadline'] = time.monotonic()
                    print('RED 安全唤醒原 collector，不计成功')
                    break
                frame = frame.f_back
        thread.join(timeout=2)
        assert not thread.is_alive()
        if release_thread is not None:
            release_thread.join(timeout=2)
            assert not release_thread.is_alive()
        host.close()
        fault.rescue()


@pytest.mark.parametrize('entry', ['host', 'lease'])
def test_close_after_real_owner_adoption_never_allocates_into_closed_owner(configured, monkeypatch, entry):
    env = configured
    host = env.host()
    task = env.task(host, 1, released=True)
    fault = _NativeFailures(monkeypatch, close=False)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    original_adopt = type(task.lease)._adopt_owner
    def hold_adopt(lease, owner):
        original_adopt(lease, owner)
        if lease is task.lease:
            entered.set()
            assert release.wait(3)
    monkeypatch.setattr(type(task.lease), '_adopt_owner', hold_adopt)
    results = []
    def execute():
        try:
            execute_external(task.lease, task.context, records=env.records, turns=env.turns,
                turn_id=task.identity, owner_id='local-user')
        except Exception as error:
            results.append(error)
        finally:
            done.set()
    thread = threading.Thread(target=execute, name='native-startup-close-consumer')
    thread.start()
    owner = None
    try:
        assert entered.wait(3)
        owner = task.lease._process_owner
        assert owner is fault.owners[0] and owner.process is None and not owner.descriptors
        (host if entry == 'host' else task.lease).close()
        assert owner.closed and fault.generation_receipts == [(0, fault.jobs[0])]
        release.set()
        assert done.wait(2)
        assert owner.process is None or owner.process.poll() is not None
        assert all(stream.closed for stream in owner.streams) and not owner.descriptors
        assert owner.closed and not (task.cwd / 'spawn-count').exists()
        assert len(results) == 1 and isinstance(results[0], ToolProviderFailure)
        assert claims(env) == []
    finally:
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        if owner is not None and (owner.descriptors or any(not stream.closed for stream in owner.streams)
                or owner.process is not None and owner.process.poll() is None):
            print('RED 救援原关闭后被追加资源，不计成功')
            owner.closed = False
            owner.close()
        host.close()
        fault.rescue()


def test_close_before_original_completion_registration_keeps_same_lease_for_retry(configured, monkeypatch):
    env = configured
    host = env.host()
    task = env.task(host, 1, released=True)
    fault = _NativeFailures(monkeypatch)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    original_retain = type(task.lease)._retain_cleanup
    def hold_retain(lease, owner, complete):
        if lease is task.lease:
            entered.set()
            assert release.wait(3)
        original_retain(lease, owner, complete)
    monkeypatch.setattr(type(task.lease), '_retain_cleanup', hold_retain)
    results = []
    def execute():
        try:
            execute_external(task.lease, task.context, records=env.records, turns=env.turns,
                turn_id=task.identity, owner_id='local-user')
        except Exception as error:
            results.append(error)
        finally:
            done.set()
    thread = threading.Thread(target=execute, name='native-registration-close-consumer')
    thread.start()
    try:
        assert entered.wait(3)
        owner = task.lease._process_owner
        fault.enabled = False
        host.close()
        assert owner.closed and fault.generation_receipts == [(0, fault.jobs[0])]
        assert task.lease in host._issued_leases and len(claims(env)) == 1
        release.set()
        assert done.wait(2)
        assert len(results) == 1 and isinstance(results[0], ToolProviderFailure)
        assert results[0].error_code == 'external_task_cleanup_incomplete'
        assert results[0].effect_certainty == 'unknown'
        host.close()
        assert host._issued_leases == {} and claims(env) == []
        assert env.records.read('v2_external_runs', task.identity).payload['status'] == 'failed'
        assert env.turns.get_immutable_payload(task.identity, 'external-task-receipt-v1')[1]['effect_certainty'] == 'unknown'
        assert (task.cwd / 'spawn-count').read_text() == '1'
    finally:
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        fault.enabled = False
        host.close()
        fault.rescue()


@pytest.mark.parametrize('entry', ['host', 'lease'])
def test_close_during_real_owner_constructor_retains_new_job_in_original_lease(configured, monkeypatch, entry):
    env = configured
    host = env.host()
    task = env.task(host, 1, released=True)
    fault = _NativeFailures(monkeypatch)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    original_init = external_process._Owner.__init__
    def hold_created(owner):
        original_init(owner)
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(external_process._Owner, '__init__', hold_created)
    results = []
    def execute():
        try:
            execute_external(task.lease, task.context, records=env.records, turns=env.turns,
                turn_id=task.identity, owner_id='local-user')
        except Exception as error:
            results.append(error)
        finally:
            done.set()
    thread = threading.Thread(target=execute, name='native-constructor-close-consumer')
    thread.start()
    try:
        assert entered.wait(3)
        owner = fault.owners[0]
        assert owner.job.handle == fault.jobs[0] and fault.valid(fault.jobs[0])
        (host if entry == 'host' else task.lease).close()
        assert task.lease in host._issued_leases
        release.set()
        assert done.wait(2)
        assert len(results) == 1 and isinstance(results[0], ToolProviderFailure)
        assert results[0].error_code == 'external_task_cleanup_incomplete' and results[0].effect_certainty == 'unknown'
        assert task.lease._process_owner is owner and host._issued_leases[task.lease][5] is owner
        assert not owner.closed and owner.process is None and not owner.descriptors
        assert fault.valid(fault.jobs[0]) and fault.generation_receipts == []
        assert len(claims(env)) == 1 and not (task.cwd / 'spawn-count').exists()
        fault.enabled = False
        host.close()
        assert owner.closed and host._issued_leases == {} and claims(env) == []
        assert fault.generation_receipts == [(0, fault.jobs[0])]
        assert env.turns.get_immutable_payload(task.identity, 'external-task-receipt-v1')[1]['effect_certainty'] == 'unknown'
    finally:
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        fault.enabled = False
        host.close()
        if fault.owners and not fault.owners[0].closed:
            print('RED 救援未被原 Host 接住的真实 owner，不计成功')
            fault.owners[0].close()
        fault.rescue()


def test_probe_close_during_real_owner_constructor_keeps_unissued_pending_job(setup_host, monkeypatch):
    module, host, turn, plan, config, *_ = setup_host()
    fault = _NativeFailures(monkeypatch)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    original_init = external_process._Owner.__init__
    def hold_created(owner):
        original_init(owner)
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(external_process._Owner, '__init__', hold_created)
    results = []
    def prepare():
        try:
            results.append(host.prepare(turn, plan, mcp_config=config))
        except Exception as error:
            results.append(error)
        finally:
            done.set()
    thread = threading.Thread(target=prepare, name='native-probe-constructor-close-consumer')
    thread.start()
    try:
        assert entered.wait(3)
        pending = next(iter(host._issued_leases))
        host.close()
        assert pending in host._issued_leases and host._issued_leases[pending][4] is False
        release.set()
        assert done.wait(2)
        assert len(results) == 1 and isinstance(results[0], module.ExternalHostError)
        assert str(results[0]) == 'external_host_cleanup_incomplete'
        owner = fault.owners[0]
        assert pending._process_owner is owner and host._issued_leases[pending][5] is owner
        assert fault.valid(fault.jobs[0]) and not owner.closed and owner.process is None
        with pytest.raises(module.ExternalHostError): pending.validate()
        fault.enabled = False
        host.close()
        assert owner.closed and host._issued_leases == {} and fault.generation_receipts == [(0, fault.jobs[0])]
    finally:
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        fault.enabled = False
        host.close()
        if fault.owners and not fault.owners[0].closed:
            print('RED 救援未签发原 owner，不计成功')
            fault.owners[0].close()
        fault.rescue()


@pytest.mark.parametrize('entry', ['host', 'lease'])
def test_unobserved_private_start_reader_never_claims_no_effect_for_real_cli(configured, monkeypatch, entry):
    env = configured
    host = env.host()
    task = env.task(host, 1)
    fault = _NativeFailures(monkeypatch, close=False)
    gate, done = threading.Event(), threading.Event()
    original_run = threading.Thread.run
    def deferred_control(thread):
        if thread.name == 'external-process-control':
            assert gate.wait(3)
        original_run(thread)
    monkeypatch.setattr(threading.Thread, 'run', deferred_control)
    results = []
    def execute():
        try:
            execute_external(task.lease, task.context, records=env.records, turns=env.turns,
                turn_id=task.identity, owner_id='local-user')
        except Exception as error:
            results.append(error)
        finally:
            done.set()
    thread = threading.Thread(target=execute, name='native-unobserved-start-consumer')
    thread.start()
    release_thread = None
    try:
        deadline = time.monotonic() + 3
        while not (task.cwd / 'spawn-count').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (task.cwd / 'spawn-count').read_text() == '1'
        before = env.records.read('v2_external_runs', task.identity)
        assert before.payload['status'] == 'reserved' and before.payload['started_at'] is None
        owner = task.lease._process_owner
        def release_control():
            assert owner.finished.wait(3)
            gate.set()
        release_thread = threading.Thread(target=release_control, name='native-private-start-schedule-consumer')
        release_thread.start()
        (host if entry == 'host' else task.lease).close()
        assert done.wait(2) and owner.closed
        assert len(results) == 1 and isinstance(results[0], ToolProviderFailure)
        assert results[0].effect_certainty == 'unknown'
        proof = env.turns.get_immutable_payload(task.identity, 'external-task-receipt-v1')[1]
        assert proof['status'] == 'failed' and proof['effect_certainty'] == 'unknown'
        ended = env.records.read('v2_external_runs', task.identity)
        assert ended.payload['started_at'] is None and ended.payload['ended_at'] is not None
        assert claims(env) == [] and (task.cwd / 'spawn-count').read_text() == '1'
        assert fault.generation_receipts == [(0, fault.jobs[0])]
    finally:
        gate.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        if release_thread is not None:
            release_thread.join(timeout=2)
            assert not release_thread.is_alive()
        host.close()
        fault.rescue()
