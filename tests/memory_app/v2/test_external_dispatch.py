"""真实假 CLI 连接原工具取消和期限；不执行模型或真实 CLI。"""
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import time

import psutil
import pytest

from core.ai_kernel.dispatcher import ToolCancellationToken, ToolExecutionContext
from backend.memory_app.v2.external_adapters import LaunchPlan
from tests.memory_app.v2.test_external_process import CLI as TREE_CLI


CLI = r'''
import json, os, sys, time
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
mode, executor = sys.argv[1:]
Path('spawned').write_bytes(sys.stdin.buffer.read())
Path('cli.pid').write_text(str(os.getpid()))
secret = os.environ.get('SYNTHETIC_VALUE', '')
def emit(value): print(json.dumps(value), flush=True)
if mode == 'crash':
    print(secret, file=sys.stderr, flush=True)
    sys.exit(7)
if mode == 'slow': time.sleep(2)
if executor == 'codex':
    emit({'type':'turn.started','usage':{'input_tokens':999,'output_tokens':999}})
    for _ in range(300 if mode == 'many' else 1):
        emit({'type':'item.started','item':{'type':'command_execution','arguments':secret}})
    emit({'type':'item.completed','item':{'type':'agent_message','text':'合成消息 '+secret}})
    if mode != 'missing':
        event = {'type':'turn.failed' if mode == 'failure' else 'turn.completed'}
        if mode != 'no_usage': event['usage'] = {'input_tokens':4,'output_tokens':2}
        emit(event)
else:
    emit({'type':'system','subtype':'init'})
    emit({'type':'assistant','message':{'content':[{'type':'text','text':'合成消息 '+secret}]}})
    if mode != 'missing':
        event = {'type':'result','subtype':'error_during_execution' if mode == 'failure' else 'success',
            'is_error':mode == 'failure'}
        if mode != 'no_usage': event['usage'] = {'input_tokens':4,'output_tokens':2}
        emit(event)
print(secret, file=sys.stderr, flush=True)
if mode == 'success_crash': sys.exit(7)
if mode == 'success_hold':
    print('holding', flush=True)
    Path('holding').write_text('ready')
    while True: time.sleep(.01)
'''


def context(timeout_ms=5000):
    return ToolExecutionContext('synthetic-invocation', 1, timeout_ms, ToolCancellationToken())


def plan(tmp_path, mode='normal', executor='codex'):
    script = tmp_path / 'synthetic-cli.py'
    script.write_text(CLI, encoding='utf-8')
    return LaunchPlan(executor, executor+'@1', '0.156.1' if executor == 'codex' else '2.1.257',
        'workspace','disabled',(sys.executable,str(script),mode,executor),tmp_path,'合成任务')


def dispatch(value, owner, **kwargs):
    module = importlib.import_module('backend.memory_app.v2.external_dispatch')
    env = {'SystemRoot':os.environ['SystemRoot']} if os.name == 'nt' else {}
    env['SYNTHETIC_VALUE'] = '-'.join(('synthetic','dispatch','secret'))
    return module.dispatch_external(value, owner, environment=env,
        secret_values=(env['SYNTHETIC_VALUE'],), **kwargs)


@pytest.mark.parametrize('executor', ['codex','claude-code'])
@pytest.mark.parametrize('mode,status', [('normal','completed'),('failure','failed'),
    ('missing','failed'),('no_usage','completed')])
def test_real_protocol_status_usage_and_redacted_results(tmp_path, executor, mode, status):
    value, owner = plan(tmp_path, mode, executor), context()
    started, delivered = [], []
    result = dispatch(value, owner, on_started=lambda:started.append(True), event_sink=delivered.append)
    assert result.status == status and result.exit_code == 0
    assert result.usage == (None if mode in {'missing','no_usage'} else {'input_tokens':4,'output_tokens':2})
    assert started == [True] and owner._provider_started is True
    assert tuple(delivered) == result.events
    assert (tmp_path/'spawned').read_text(encoding='utf-8') == value.input_text
    assert '-'.join(('synthetic','dispatch','secret')) not in repr(result)
    if mode == 'missing':
        assert result.tail == ('external_dispatch_terminal_missing',)


@pytest.mark.parametrize('condition,status', [('cancelled','cancelled'),('expired','timed_out'),
    ('invalid','failed')])
def test_preflight_never_spawns(tmp_path, condition, status):
    owner = context(1 if condition == 'expired' else 5000)
    if condition == 'cancelled': owner.cancellation.request()
    if condition == 'expired': time.sleep(.02)
    if condition == 'invalid': owner = object()
    result = dispatch(plan(tmp_path), owner)
    assert result.status == status and result.exit_code is None and result.usage is None
    assert not (tmp_path/'spawned').exists() and result.events == ()


def test_crash_cannot_be_completed_and_tail_is_redacted(tmp_path):
    result = dispatch(plan(tmp_path,'crash'), context())
    assert result.status == 'failed' and result.exit_code == 7 and result.usage is None
    assert '-'.join(('synthetic','dispatch','secret')) not in repr(result)


def test_remaining_context_budget_wins_over_default_twenty_minutes(tmp_path):
    owner = context(700)
    time.sleep(.15)
    started = time.monotonic()
    result = dispatch(plan(tmp_path,'slow'), owner)
    assert result.status == 'timed_out' and result.usage is None
    assert time.monotonic() - started < 2


def tree_plan(tmp_path):
    script = tmp_path/'tree.py'
    script.write_text(TREE_CLI, encoding='utf-8')
    return LaunchPlan('codex','codex@1','0.156.1','workspace','disabled',
        (sys.executable,str(script),'tree'),tmp_path,'task')


def assert_dead_tree(tmp_path):
    assert (tmp_path/'grand.pid').exists()
    for name in ('tree','child','grand'):
        pid = int((tmp_path/(name+'.pid')).read_text())
        assert not psutil.pid_exists(pid)


def test_original_token_cancellation_closes_entire_real_process_tree(tmp_path):
    owner = context()
    def cancel_when_ready():
        deadline = time.monotonic()+3
        while not (tmp_path/'grand.pid').exists() and time.monotonic()<deadline:
            time.sleep(.01)
        owner.cancellation.request()
    worker = threading.Thread(target=cancel_when_ready)
    worker.start()
    try:
        result = dispatch(tree_plan(tmp_path), owner)
    finally:
        worker.join()
    assert result.status == 'cancelled' and result.usage is None
    assert_dead_tree(tmp_path)


def test_callback_failure_is_fixed_and_cleanup_finishes(tmp_path):
    def fail(_event):
        raise RuntimeError('synthetic-private-error-detail')
    result = dispatch(plan(tmp_path), context(), event_sink=fail)
    assert result.status == 'failed' and result.tail == ('external_process_callback_failed',)
    assert 'synthetic-private-error-detail' not in repr(result)


def test_started_callback_failure_does_not_escape(tmp_path):
    def fail():
        deadline = time.monotonic()+3
        while not (tmp_path/'grand.pid').exists() and time.monotonic()<deadline:
            time.sleep(.01)
        raise RuntimeError('synthetic-private-error-detail')
    result = dispatch(tree_plan(tmp_path), context(), on_started=fail)
    assert result.status == 'failed' and result.tail == ('external_process_callback_failed',)
    assert_dead_tree(tmp_path)


def test_event_summary_is_bounded_and_does_not_keep_raw_arguments(tmp_path):
    result = dispatch(plan(tmp_path,'many'), context())
    assert result.status == 'completed' and len(result.events) == 128
    assert result.events[-1] == {'kind':'finished','status':'completed'}
    assert all('arguments' not in event for event in result.events)


def test_expiry_closes_entire_real_process_tree(tmp_path):
    result = dispatch(tree_plan(tmp_path), context(1500))
    assert result.status == 'timed_out'
    assert_dead_tree(tmp_path)


@pytest.mark.parametrize('field,value', [('timeout_ms',True),('timeout_ms',0),
    ('attempt',False),('invocation_id',''),('cancellation',None)])
def test_invalid_kernel_context_is_rejected_without_spawning(tmp_path, field, value):
    owner = context()
    setattr(owner,field,value)
    result = dispatch(plan(tmp_path),owner)
    assert result.status == 'failed' and result.tail == ('external_dispatch_context_invalid',)
    assert not (tmp_path/'spawned').exists()


def test_secret_iterable_is_shared_by_parser_and_process_tail(tmp_path):
    module = importlib.import_module('backend.memory_app.v2.external_dispatch')
    secret = '-'.join(('synthetic','dispatch','secret'))
    environment = {'SYNTHETIC_VALUE':secret}
    if os.name == 'nt': environment['SystemRoot'] = os.environ['SystemRoot']
    result = module.dispatch_external(plan(tmp_path),context(),environment=environment,
        secret_values=(value for value in (secret,)))
    assert result.status == 'completed'
    assert secret not in repr(result)


@pytest.mark.parametrize('executor', ['codex','claude-code'])
@pytest.mark.parametrize('ending,status', [('crash','failed'),('cancel','cancelled'),
    ('timeout','timed_out')])
def test_terminal_sink_waits_for_real_owner_cleanup_and_final_status(tmp_path, executor, ending, status):
    owner = context(1200 if ending == 'timeout' else 5000)
    delivered, early = [], []
    def receive(event):
        if event['kind'] == 'finished':
            early.append(psutil.pid_exists(int((tmp_path/'cli.pid').read_text())))
        delivered.append(event)
    def cancel_when_holding():
        deadline = time.monotonic()+3
        while not (tmp_path/'holding').exists() and time.monotonic()<deadline:
            time.sleep(.01)
        owner.cancellation.request()
    worker = threading.Thread(target=cancel_when_holding) if ending == 'cancel' else None
    if worker is not None: worker.start()
    try:
        result = dispatch(plan(tmp_path,'success_crash' if ending == 'crash' else 'success_hold',executor),
            owner,event_sink=receive)
    finally:
        if worker is not None: worker.join()
    assert result.status == status
    assert early == [False]
    assert [event for event in delivered if event['kind'] == 'finished'] == [
        {'kind':'finished','status':status}]
    assert result.events[-1] == {'kind':'finished','status':status}
    assert tuple(delivered) == result.events


def test_terminal_sink_exception_is_fixed_after_cleanup_without_retry(tmp_path):
    calls = []
    def receive(event):
        if event['kind'] == 'finished':
            calls.append(event)
            assert not psutil.pid_exists(int((tmp_path/'cli.pid').read_text()))
            raise RuntimeError('synthetic-private-terminal-detail')
    result = dispatch(plan(tmp_path),context(),event_sink=receive)
    assert len(calls) == 1
    assert result.status == 'failed' and result.tail == ('external_dispatch_callback_failed',)
    assert result.events[-1] == {'kind':'finished','status':'failed'}
    assert 'synthetic-private-terminal-detail' not in repr(result)
