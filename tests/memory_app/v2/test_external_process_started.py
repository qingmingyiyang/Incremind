"""实际命令启动证据与进程 owner 的回调生命周期。"""
import os
import sys
import threading
import time

import pytest

from backend.memory_app.v2.external_process import ProcessResult, run_process


def environment():
    return {name: os.environ[name] for name in ('SystemRoot', 'WINDIR') if name in os.environ}


def command(body):
    return [sys._base_executable, '-I', '-S', '-u', '-c', body]


@pytest.mark.parametrize('body', ['', "print('safe output')"])
def test_real_cli_start_is_reported_once_even_without_stdout(tmp_path, body):
    observed = []
    result = run_process(command(body), cwd=tmp_path, environment=environment(),
        on_started=lambda: observed.append('started'))
    assert result.status == 'completed' and result.exit_code == 0
    assert observed == ['started']


def test_actual_cli_spawn_failure_never_reports_started(tmp_path):
    observed = []
    result = run_process([str(tmp_path / 'missing-executable')], cwd=tmp_path,
        environment=environment(), on_started=lambda: observed.append('started'))
    assert result == ProcessResult('failed', None, 0, ('external_process_start_failed',))
    assert observed == []


def test_cancel_before_launch_has_no_start_fact(tmp_path):
    cancel = threading.Event()
    cancel.set()
    observed = []
    result = run_process(command(''), cwd=tmp_path, environment=environment(), cancel=cancel,
        on_started=lambda: observed.append('started'))
    assert result == ProcessResult('cancelled', None, 0, ())
    assert observed == []


def test_start_callback_failure_closes_owner_without_private_diagnostic(tmp_path):
    def reject():
        raise RuntimeError('synthetic-private-start-detail')
    before = time.monotonic()
    result = run_process(command('import time; time.sleep(20)'), cwd=tmp_path,
        environment=environment(), timeout=5, on_started=reject)
    assert result == ProcessResult('failed', None, 0, ('external_process_callback_failed',))
    assert time.monotonic() - before < 4
    assert 'synthetic-private-start-detail' not in repr(result)
    assert not [thread for thread in threading.enumerate() if thread.name.startswith('external-process-')]


@pytest.mark.parametrize('callback', ['on_started', 'on_line'])
def test_invalid_callback_is_rejected_before_cli_side_effect(tmp_path, callback):
    proof = tmp_path / 'should-not-exist'
    body = 'from pathlib import Path; Path(' + repr(str(proof)) + ').write_text("unexpected")'
    result = run_process(command(body), cwd=tmp_path, environment=environment(), **{callback: 'invalid'})
    assert result == ProcessResult('failed', None, 0, ('external_process_start_failed',))
    assert not proof.exists()
