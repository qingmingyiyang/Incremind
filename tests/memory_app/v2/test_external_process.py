"""真实标准库模拟命令验证进程树、双管道和脱敏生命周期。"""
import json
import os
from contextlib import contextmanager
from pathlib import Path
import sys
import threading
import time

import psutil
import pytest

from backend.memory_app.v2.external_process import ProcessResult, run_process
from backend.shared.secret_detection import contains_secret


CLI = r'''
import json, os, subprocess, sys, threading, time
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8', newline='\n')
sys.stderr.reconfigure(encoding='utf-8', newline='\n')
mode = sys.argv[1]
if mode == 'normal':
    print(json.dumps({'cwd': os.getcwd(), 'args': sys.argv[2:],
        'value': os.environ.get('SYNTHETIC_VALUE'),
        'input': sys.stdin.buffer.read().decode('utf-8')}, ensure_ascii=False), flush=True)
elif mode == 'crash':
    print('safe crash output', file=sys.stderr, flush=True)
    sys.exit(7)
elif mode == 'saturate':
    def emit(fd):
        for _ in range(128): os.write(fd, b'x' * 4096)
        os.write(fd, b'\n')
    threads = [threading.Thread(target=emit, args=(fd,)) for fd in (1, 2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
elif mode == 'flood':
    while True: os.write(1, b'x' * 8192)
elif mode == 'no_input':
    print('not_reading_input', flush=True)
    while True: time.sleep(1)
elif mode == 'unicode':
    for byte in '边界😀结束\n'.encode('utf-8'): os.write(1, bytes([byte]))
elif mode == 'many_lines':
    for index in range(256): print('row-' + str(index), flush=True)
elif mode == 'generic_secret':
    os.write(1, b'api_key =\n'); time.sleep(.05)
    os.write(1, ('"' + 'A' * 24 + '"\n').encode('utf-8'))
elif mode == 'owner_identity':
    print(json.dumps({'pid': os.getpid(), 'executable': sys.executable}), flush=True)
elif mode == 'ownership':
    if os.name == 'nt':
        try:
            child = subprocess.Popen([sys._base_executable, '-c', 'import time; time.sleep(5)'], creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB)
        except OSError:
            print('breakaway_denied', flush=True)
        else:
            print(json.dumps({'pid': child.pid}), flush=True)
            time.sleep(.5)
    else:
        print(json.dumps({'pid': os.getpid(), 'group': os.getpgrp()}), flush=True)
elif mode == 'secrets':
    secret = os.environ['SYNTHETIC_VALUE']
    for chunk in (secret[:3], secret[3:8], secret[8:], '\n'):
        os.write(1, chunk.encode('utf-8')); time.sleep(.01)
    print('sk-' + 'Z' * 28, flush=True)
    print('-----BEGIN ' + 'PRIVATE KEY-----', flush=True)
    print('synthetic-private-body', flush=True)
    print('-----END PRIVATE KEY-----', flush=True)
    print(secret, file=sys.stderr, flush=True)
elif mode in {'tree', 'orphan', 'orphan_closed', 'child', 'grand'}:
    Path(mode + '.pid').write_text(str(os.getpid()))
    if mode in {'tree', 'orphan', 'orphan_closed'}:
        options = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL} if mode == 'orphan_closed' else {}
        subprocess.Popen([sys.executable, __file__, 'child'], **options)
        while not Path('grand.pid').exists(): time.sleep(.01)
        print('tree_ready', flush=True)
        if mode != 'tree': sys.exit(0)
    elif mode == 'child':
        subprocess.Popen([sys.executable, __file__, 'grand'])
    while True: time.sleep(1)
'''


@pytest.fixture
def command(tmp_path):
    script = tmp_path / '模拟命令.py'
    script.write_text(CLI, encoding='utf-8')
    return lambda mode, *args: [sys.executable, '-u', str(script), mode, *args]


def environment(**values):
    return {**{key: value for key, value in os.environ.items()
        if key.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH'}}, **values}


def assert_tree_stopped(folder):
    files = list(folder.glob('*.pid'))
    assert len(files) == 3
    pids = [int(path.read_text()) for path in files]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            api.OpenProcess.restype = wintypes.HANDLE
            api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            alive = []
            for pid in pids:
                handle = api.OpenProcess(0x00100000, False, pid)
                if handle:
                    try:
                        if api.WaitForSingleObject(handle, 0) != 0:
                            alive.append(pid)
                    finally:
                        api.CloseHandle(handle)
                else:
                    assert ctypes.get_last_error() == 87
        else:
            alive = [pid for pid in pids if psutil.pid_exists(pid)
                     and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE]
        if not alive:
            break
        time.sleep(.02)
    assert not alive, '受控进程树仍有活进程'


def test_real_command_uses_exact_args_cwd_environment_and_utf8_stdin(command, tmp_path):
    lines = []
    result = run_process(command('normal', 'a b', '引号"参数'), cwd=tmp_path,
        environment=environment(SYNTHETIC_VALUE='visible'), input_text='真实标准输入😀', on_line=lines.append)
    assert isinstance(result, ProcessResult)
    assert result.status == 'completed' and result.exit_code == 0
    body = json.loads(lines[0])
    assert Path(body['cwd']) == tmp_path and body['args'] == ['a b', '引号"参数']
    assert body['value'] == 'visible' and body['input'] == '真实标准输入😀'
    assert result.output_bytes == len((lines[0] + '\n').encode('utf-8'))


def test_real_crash_keeps_exit_code_and_only_bounded_stderr_tail(command, tmp_path):
    lines = []
    result = run_process(command('crash'), cwd=tmp_path, environment=environment(), on_line=lines.append)
    assert result.status == 'failed' and result.exit_code == 7
    assert lines == [] and 'safe crash output' in result.tail


def test_both_real_pipes_are_drained_without_deadlock_or_unbounded_tail(command, tmp_path):
    lines = []
    result = run_process(command('saturate'), cwd=tmp_path, environment=environment(),
        timeout=5, output_limit=2 * 1048576, on_line=lines.append)
    assert result.status == 'completed' and result.output_bytes == 2 * (128 * 4096 + 1)
    assert len(lines) == 1 and len(lines[0]) == 128 * 4096
    assert len(result.tail) <= 128 and all(len(line) <= 4096 for line in result.tail)


def test_infinite_no_newline_output_hits_real_byte_limit_promptly(command, tmp_path):
    start = time.monotonic()
    result = run_process(command('flood'), cwd=tmp_path, environment=environment(), timeout=10, output_limit=32768)
    assert result.status == 'output_limit' and result.output_bytes > 32768
    assert time.monotonic() - start < 4
    assert len(result.tail) <= 128 and all(len(line) <= 4096 for line in result.tail)


@pytest.mark.parametrize('ending,mode', [('cancelled', 'tree'), ('timed_out', 'tree'),
    ('completed', 'orphan'), ('completed', 'orphan_closed')])
def test_real_child_and_grandchild_end_even_when_cli_parent_exits(command, tmp_path, ending, mode):
    cancel = threading.Event()
    def observe(line):
        if ending == 'cancelled' and line == 'tree_ready':
            cancel.set()
    result = run_process(command(mode), cwd=tmp_path,
        environment=environment(), timeout=.8 if ending == 'timed_out' else 5, cancel=cancel, on_line=observe)
    assert result.status == ending
    assert_tree_stopped(tmp_path)


@pytest.mark.parametrize('ending', ['cancelled', 'timed_out'])
def test_stdin_larger_than_pipe_capacity_cannot_block_timeout_or_cancellation(command, tmp_path, ending):
    cancel = threading.Event()
    def observe(line):
        if ending == 'cancelled':
            cancel.set()
    start = time.monotonic()
    result = run_process(command('no_input'), cwd=tmp_path, environment=environment(),
        input_text='x' * 2097152, timeout=.8 if ending == 'timed_out' else 5,
        cancel=cancel, on_line=observe)
    assert result.status == ending and time.monotonic() - start < 4
    assert not [thread for thread in threading.enumerate() if thread.name.startswith('external-process-')]


def test_cancel_before_start_never_spawns_the_real_cli(command, tmp_path):
    cancel = threading.Event()
    cancel.set()
    result = run_process(command('tree'), cwd=tmp_path, environment=environment(), cancel=cancel)
    assert result == ProcessResult('cancelled', None, 0, ())
    assert not list(tmp_path.glob('*.pid'))


def test_missing_executable_returns_only_fixed_error_without_command_or_environment(tmp_path):
    hidden = 'synthetic-hidden-command'
    result = run_process([str(tmp_path / hidden)], cwd=tmp_path, environment=environment(SYNTHETIC_VALUE=hidden))
    assert result.status == 'failed'
    assert result.tail == ('external_process_start_failed',)
    assert hidden not in repr(result)


def test_callback_exception_cleans_real_descendants_and_returns_fixed_error(command, tmp_path):
    def reject(line):
        raise ValueError('synthetic-private-exception-detail')
    result = run_process(command('tree'), cwd=tmp_path, environment=environment(), on_line=reject)
    assert result.status == 'failed' and result.tail == ('external_process_callback_failed',)
    assert_tree_stopped(tmp_path)
    assert 'synthetic-private-exception-detail' not in repr(result)


@pytest.mark.parametrize('multiline', [False, True])
def test_split_utf8_and_split_secrets_never_reach_callback_or_tail(command, tmp_path, multiline):
    lines = []
    result = run_process(command('unicode'), cwd=tmp_path, environment=environment(), on_line=lines.append)
    assert result.status == 'completed' and lines == ['边界😀结束']
    secret = 'synthetic-凭据-value' + ('\nsecret-suffix' if multiline else '')
    lines = []
    result = run_process(command('secrets'), cwd=tmp_path, environment=environment(SYNTHETIC_VALUE=secret),
        secret_values=[secret], on_line=lines.append)
    assert result.status == 'completed'
    rendered = '\n'.join((*lines, *result.tail))
    assert secret not in rendered and 'Z' * 28 not in rendered and 'synthetic-private-body' not in rendered
    assert 'synthetic-凭据-value' not in rendered and 'secret-suffix' not in rendered
    assert '[REDACTED_SECRET]' in rendered


def test_real_many_lines_keep_only_the_last_128_and_callbacks_keep_all(command, tmp_path):
    lines = []
    result = run_process(command('many_lines'), cwd=tmp_path, environment=environment(), on_line=lines.append)
    assert result.status == 'completed'
    assert lines == ['row-' + str(index) for index in range(256)]
    assert result.tail == tuple(lines[-128:])


def test_real_platform_owner_denies_windows_breakaway_or_has_a_private_posix_group(command, tmp_path, monkeypatch):
    if os.name == 'nt':
        with windows_job_probe(monkeypatch) as (observe, observed, stopped):
            result = run_process(command('ownership'), cwd=tmp_path, environment=environment(), on_line=observe)
            assert result.status == 'completed'
            assert observed['flags'] == 0x2000 and observed['bootstrap_member'] is True
            assert observed.get('denied') is True or observed.get('cli_member') is True
            stopped()
    else:
        lines = []
        result = run_process(command('ownership'), cwd=tmp_path, environment=environment(), on_line=lines.append)
        assert result.status == 'completed' and json.loads(lines[0])['group'] != os.getpgrp()


def test_original_generic_secret_pattern_across_newline_never_leaks(command, tmp_path):
    assert contains_secret('api_key =\n"' + 'A' * 24 + '"\n')
    lines = []
    result = run_process(command('generic_secret'), cwd=tmp_path, environment=environment(), on_line=lines.append)
    assert result.status == 'completed'
    assert 'A' * 24 not in '\n'.join((*lines, *result.tail))


@contextmanager
def windows_job_probe(monkeypatch, *, delay=0):
    """委托原绑定，并持有真实进程句柄核验 Job 成员和最终终止。"""
    import ctypes
    from ctypes import wintypes as w
    from backend.memory_app.v2 import external_process
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
    api.QueryInformationJobObject.restype = w.BOOL
    api.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]
    api.IsProcessInJob.restype = w.BOOL
    api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    api.OpenProcess.restype = w.HANDLE
    api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    api.WaitForSingleObject.restype = w.DWORD
    api.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    api.TerminateProcess.restype = w.BOOL
    api.CloseHandle.argtypes = [w.HANDLE]
    api.CloseHandle.restype = w.BOOL
    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
            ('flags', w.DWORD), ('min_working', ctypes.c_size_t), ('max_working', ctypes.c_size_t),
            ('active', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('schedule', w.DWORD)]
    original_assign = external_process._WindowsJob.assign
    observed = {}
    handles = []
    def assign(job, process):
        # 控制外部资源绑定的时序，验证启动闸门所在进程不会先脱离 Job。
        time.sleep(delay)
        original_assign(job, process)
        limits = Basic()
        assert api.QueryInformationJobObject(job.handle, 2, ctypes.byref(limits), ctypes.sizeof(limits), None)
        member = w.BOOL()
        assert api.IsProcessInJob(int(process._handle), job.handle, ctypes.byref(member))
        observed.update(job=job, flags=limits.flags, bootstrap_member=bool(member.value), bootstrap_pid=process.pid)
    monkeypatch.setattr(external_process._WindowsJob, 'assign', assign)
    def observe(line):
        if line == 'breakaway_denied':
            observed['denied'] = True
            return
        identity = json.loads(line)
        handle = api.OpenProcess(0x00101001, False, identity['pid'])
        assert handle
        handles.append(handle)
        member = w.BOOL()
        own_job = observed['job'].handle
        assert own_job
        assert api.IsProcessInJob(handle, own_job, ctypes.byref(member))
        observed.update(cli_member=bool(member.value), cli_pid=identity['pid'])
    def stopped():
        for handle in handles:
            assert api.WaitForSingleObject(handle, 3000) == 0
    try:
        yield observe, observed, stopped
    finally:
        for handle in handles:
            # 断言失败时只清理本测试已持有句柄的进程，不用 PID 再查找。
            if api.WaitForSingleObject(handle, 0) != 0:
                api.TerminateProcess(handle, 1)
            api.CloseHandle(handle)


@pytest.mark.parametrize('breakaway', [False, True])
def test_real_windows_job_flags_and_native_cli_membership(command, tmp_path, monkeypatch, breakaway):
    if os.name != 'nt':
        lines = []
        result = run_process(command('ownership'), cwd=tmp_path, environment=environment(), on_line=lines.append)
        assert result.status == 'completed' and json.loads(lines[0])['group'] != os.getpgrp()
        return
    # 让回调读取仍存活的真实 CLI；不替换被测进程 owner。
    script = tmp_path / '成员诊断.py'
    script.write_text("import os,json,sys,time,subprocess\n"
        "if sys.argv[1] == 'True':\n"
        "    child=subprocess.Popen([sys._base_executable,'-c','import time; time.sleep(20)'],creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "    identity=child.pid\n"
        "else: identity=os.getpid()\n"
        "print(json.dumps({'pid':identity,'executable':sys.executable}),flush=True); time.sleep(.5)\n", encoding='utf-8')
    with windows_job_probe(monkeypatch, delay=.1) as (observe, observed, stopped):
        result = run_process([sys.executable, '-u', str(script), str(breakaway)], cwd=tmp_path, environment=environment(), on_line=observe)
        assert result.status == 'completed'
        print('实际 Job 诊断:', {key: value for key, value in observed.items() if key != 'job'})
        assert observed['flags'] == 0x2000 and observed['bootstrap_member'] is True
        assert observed['cli_member'] is True
        stopped()


def test_windows_real_job_assignment_failure_keeps_start_gate_closed(command, tmp_path, monkeypatch):
    if os.name != 'nt':
        cancel = threading.Event()
        cancel.set()
        assert run_process(command('tree'), cwd=tmp_path, environment=environment(), cancel=cancel).status == 'cancelled'
        assert not list(tmp_path.glob('*.pid'))
        return
    from backend.memory_app.v2 import external_process
    original_assign = external_process._WindowsJob.assign
    before = psutil.Process().num_handles()
    def close_before_assign(job, process):
        job.close()
        original_assign(job, process)
    monkeypatch.setattr(external_process._WindowsJob, 'assign', close_before_assign)
    result = run_process(command('tree'), cwd=tmp_path, environment=environment())
    assert result == ProcessResult('failed', None, 0, ('external_process_start_failed',))
    assert not list(tmp_path.glob('*.pid'))
    assert not [thread for thread in threading.enumerate() if thread.name.startswith('external-process-')]
    assert psutil.Process().num_handles() <= before + 1


def test_repeated_runs_leave_no_owned_threads_or_windows_handles(command, tmp_path):
    before = psutil.Process().num_handles() if os.name == 'nt' else psutil.Process().num_fds()
    for _ in range(5):
        result = run_process(command('normal'), cwd=tmp_path, environment=environment())
        assert result.status == 'completed'
    after = psutil.Process().num_handles() if os.name == 'nt' else psutil.Process().num_fds()
    assert after <= before + 1
    assert not [thread for thread in threading.enumerate() if thread.name.startswith('external-process-')]
