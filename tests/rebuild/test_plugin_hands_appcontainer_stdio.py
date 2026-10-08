from __future__ import annotations

import ctypes
import msvcrt
import os
from pathlib import Path
from threading import Event, Thread
import time

import psutil
import pytest

import core.plugin_hands.windows_appcontainer as native


LIMITS = native.AppContainerResourceLimits(256 * 1024 * 1024, 2500)


@pytest.fixture
def profile():
    value = native.AppContainerProfile.create()
    try:
        yield value
    finally:
        value.close()
        assert value._deleted and value._sid == 0


def _spec(root: Path, command: str) -> native.AppContainerLaunchSpec:
    executable = (Path(os.environ["SYSTEMROOT"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe").resolve(strict=True)
    names = ("ALLUSERSPROFILE", "APPDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATHEXT", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "PUBLIC", "SystemDrive", "SYSTEMROOT", "USERPROFILE", "WINDIR")
    environment = {name: os.environ[name] for name in names if name in os.environ}
    environment.update({"PATH": str(executable.parent), "TEMP": str(root), "TMP": str(root)})
    return native.AppContainerLaunchSpec(executable, ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command), root.resolve(), environment)


def _reader(stream):
    entered, done = Event(), Event()
    values = []

    def read():
        entered.set()
        try:
            values.append(stream.read())
        except (OSError, ValueError):
            values.append(b"")
        finally:
            done.set()

    worker = Thread(target=read, daemon=True)
    worker.start()
    assert entered.wait(1)
    return worker, done, values


def _gone(process_id: int) -> None:
    try:
        psutil.Process(process_id).wait(3)
    except psutil.NoSuchProcess:
        return
    assert not psutil.pid_exists(process_id)


def _handle_open(handle):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetHandleInformation.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetHandleInformation.restype = ctypes.c_int
    flags = ctypes.c_ulong()
    return bool(kernel32.GetHandleInformation(ctypes.c_void_p(handle), ctypes.byref(flags)))


def _owned_handles(process):
    handles = {name: msvcrt.get_osfhandle(getattr(process, name).fileno()) for name in ("stdin", "stdout", "stderr")}
    handles.update({"process": process.process_handle, "thread": process.thread_handle, "job": process._job._handle})
    assert all(_handle_open(handle) for handle in handles.values())
    return handles


def _released(handles):
    assert all(not _handle_open(handle) for handle in handles.values())


@pytest.mark.parametrize("size", (29, 262144))
def test_host_exclusively_reads_separate_native_stdout_and_stderr(tmp_path, profile, size):
    command = "$v=[Console]::In.ReadLine(); [Console]::Out.Write('out:'+$v+'|'); [Console]::Error.Write(('e' * " + str(size) + ")); [Console]::Out.Write('end'); [Console]::Error.Write('|err-end')"
    process = native.launch_in_appcontainer(_spec(tmp_path, command), profile, resource_limits=LIMITS, stderr_reader="host")
    handles = _owned_handles(process)
    workers = []
    try:
        assert process.is_appcontainer() is True
        assert process.resource_limits() == LIMITS
        assert process._stderr_drain is None
        workers = [_reader(process.stdout), _reader(process.stderr)]
        process.stdin.write(b"synthetic-input\n")
        process.stdin.close()
        for worker, done, _ in workers:
            assert done.wait(5)
            worker.join(1)
            assert not worker.is_alive()
        assert workers[0][2] == [b"out:synthetic-input|end"]
        assert workers[1][2] == [b"e" * size + b"|err-end"]
        assert process.wait(3) == 0
    finally:
        process.close()
        _released(handles)
        for worker, _, _ in workers:
            worker.join(3)
            assert not worker.is_alive()
    process.close()
    assert process.process_handle == process.thread_handle == process._job._handle == 0


def test_default_internal_reader_drains_native_stderr_flood(tmp_path, profile):
    command = "[Console]::Error.Write(('e' * 262144)); [Console]::Out.Write('default-done')"
    process = native.launch_in_appcontainer(_spec(tmp_path, command), profile, resource_limits=LIMITS)
    handles = _owned_handles(process)
    worker, done, values = _reader(process.stdout)
    try:
        assert process.is_appcontainer() is True
        assert process._stderr_drain is not None
        assert done.wait(5)
        assert values == [b"default-done"]
        assert process.wait(3) == 0
    finally:
        process.close()
        _released(handles)
        worker.join(3)
    assert not worker.is_alive()
    assert not process._stderr_drain.is_alive()
    process.close()


@pytest.mark.parametrize("mode", ("internal", "host"))
def test_close_kills_native_job_and_releases_blocked_reader_before_repeat_close(tmp_path, profile, mode):
    process = native.launch_in_appcontainer(_spec(tmp_path, "[Console]::Out.WriteLine('ready'); while ($true) { [Threading.Thread]::Sleep(10) }"), profile, resource_limits=LIMITS, stderr_reader=mode)
    handles = _owned_handles(process)
    worker = None
    try:
        assert process.stdout.readline() == b"ready\r\n"
        assert process.wait(0.01) is None
        if mode == "host":
            assert process._stderr_drain is None
            worker, done, _ = _reader(process.stderr)
            assert not done.wait(0.05)
        else:
            assert process._stderr_drain.is_alive()
        streams = (process.stdin, process.stdout, process.stderr)
        started = time.monotonic()
        process.close()
        _released(handles)
        assert time.monotonic() - started < 3
        _gone(process.process_id)
        assert all(stream.closed for stream in streams)
        assert process.process_handle == process.thread_handle == process._job._handle == 0
        if worker is not None:
            worker.join(3)
            assert not worker.is_alive()
        else:
            assert not process._stderr_drain.is_alive()
        process.close()
    finally:
        process.close()
        if worker is not None:
            worker.join(3)
            assert not worker.is_alive()


class _FailCloseOnce:
    def __init__(self, stream, label, when):
        self.stream, self.label, self.when = stream, label, when
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def close(self):
        if self.failed:
            return self.stream.close()
        self.failed = True
        if self.when == "after":
            self.stream.close()
        raise OSError("synthetic-" + self.label + "-" + self.when)


@pytest.mark.parametrize("mode", ("internal", "host"))
@pytest.mark.parametrize("stream_name", ("stdin", "stdout", "stderr"))
@pytest.mark.parametrize("when", ("before", "after"))
def test_one_stream_close_failure_still_releases_other_native_resources(tmp_path, profile, mode, stream_name, when):
    kwargs = {} if mode == "internal" else {"stderr_reader": mode}
    process = native.launch_in_appcontainer(_spec(tmp_path, "[Console]::Out.WriteLine('ready'); while ($true) { [Threading.Thread]::Sleep(10) }"), profile, resource_limits=LIMITS, **kwargs)
    handles = _owned_handles(process)
    originals = {name: getattr(process, name) for name in ("stdin", "stdout", "stderr")}
    failed = _FailCloseOnce(originals[stream_name], stream_name, when)
    try:
        assert process.stdout.readline() == b"ready\r\n"
        setattr(process, stream_name, failed)
        with pytest.raises(OSError, match="synthetic-" + stream_name + "-" + when):
            process.close()
        # 首次清理即证明剩余资源释放，不能靠 finally 的重试补成通过。
        assert process.process_handle == process.thread_handle == process._job._handle == 0
        _released({name: handle for name, handle in handles.items() if when == "after" or name != stream_name})
        _gone(process.process_id)
        assert all(stream.closed for name, stream in originals.items() if name != stream_name)
        if when == "before":
            assert getattr(process, stream_name) is failed
            assert failed.closed is False
            assert _handle_open(handles[stream_name])
        else:
            assert getattr(process, stream_name) is None
            assert failed.closed is True
        if process._stderr_drain is not None:
            assert not process._stderr_drain.is_alive()
        process.close()
        _released(handles)
        assert failed.closed
        assert getattr(process, stream_name) is None
        process.close()
    finally:
        process.close()


@pytest.mark.parametrize("stream_name", ("stdin", "stdout", "stderr"))
@pytest.mark.parametrize("when", ("before", "after"))
def test_launch_failure_keeps_original_error_and_cleans_job_before_faulty_pipe(tmp_path, profile, monkeypatch, stream_name, when):
    original_files = native._AnonymousStdio.parent_files
    original_assign = native._NoBreakawayJob.assign
    streams, jobs, handles = [], [], {}

    def parent_files(stdio):
        values = list(original_files(stdio))
        index = ("stdin", "stdout", "stderr").index(stream_name)
        values[index] = _FailCloseOnce(values[index], stream_name, when)
        streams.extend(values)
        for name, stream in zip(("stdin", "stdout", "stderr"), values):
            handles[name] = msvcrt.get_osfhandle(stream.fileno())
        return tuple(values)

    def assign(job, handle):
        original_assign(job, handle)
        jobs.append(job)
        handles["process"] = handle
        handles["job"] = job._handle

    def fail_resume(kernel32, thread_handle):
        handles.update({"kernel32": kernel32, "thread": thread_handle})
        raise native.AppContainerError("synthetic-resume")

    monkeypatch.setattr(native._AnonymousStdio, "parent_files", parent_files)
    monkeypatch.setattr(native._NoBreakawayJob, "assign", assign)
    monkeypatch.setattr(native, "_resume_suspended_process", fail_resume)
    try:
        with pytest.raises(native.AppContainerError, match="synthetic-resume") as caught:
            native.launch_in_appcontainer(_spec(tmp_path, "[Console]::Out.Write('must-not-run')"), profile, resource_limits=LIMITS)
        assert any("synthetic-" + stream_name + "-" + when in note for note in caught.value.__notes__)
        assert all(stream.closed for stream in streams)
        assert jobs and all(job._handle == 0 for job in jobs)
        _released({name: handle for name, handle in handles.items() if name != "kernel32"})
    finally:
        # RED 时也清理真实暂停子进程；此前断言只采用被测实现的首次清理结果。
        for job in jobs:
            job.close()
        for stream in streams:
            stream.close()
        if "kernel32" in handles:
            kernel32 = handles["kernel32"]
            for name in ("process", "thread"):
                flags = ctypes.c_ulong()
                handle = ctypes.c_void_p(handles[name])
                if kernel32.GetHandleInformation(handle, ctypes.byref(flags)):
                    if name == "process":
                        kernel32.TerminateProcess(handle, 1)
                    kernel32.CloseHandle(handle)


def test_pre_job_failure_retries_false_termination_before_releasing_native_process(tmp_path, profile, monkeypatch):
    original_dll = native.ctypes.WinDLL
    kernel32 = original_dll("kernel32", use_last_error=True)
    native._configure_kernel32(kernel32)
    kernel32.GetProcessId.argtypes = [ctypes.c_void_p]
    kernel32.GetProcessId.restype = ctypes.c_ulong
    original_create = native._AnonymousStdio.create
    handles, process_ids, termination_results = {}, [], []

    def create_stdio():
        stdio = original_create()
        for name in ("child_stdin", "parent_stdin", "parent_stdout", "child_stdout", "parent_stderr", "child_stderr"):
            handles[name] = getattr(stdio, name)
        return stdio

    def create_process(*args):
        result = kernel32.CreateProcessW(*args)
        if result:
            info = ctypes.cast(args[-1], ctypes.POINTER(native._PROCESS_INFORMATION)).contents
            handles.update({"process": int(info.hProcess), "thread": int(info.hThread)})
            process_ids.append(int(kernel32.GetProcessId(info.hProcess)))
        return result

    def terminate(handle, exit_code):
        if not termination_results:
            ctypes.set_last_error(5)
            termination_results.append(False)
            return 0
        result = kernel32.TerminateProcess(handle, exit_code)
        termination_results.append(bool(result))
        return result

    class NativeApi:
        CreateProcessW = staticmethod(create_process)
        TerminateProcess = staticmethod(terminate)

        def __getattr__(self, name):
            return getattr(kernel32, name)

    def load_dll(name, *args, **kwargs):
        return NativeApi() if name == "kernel32" else original_dll(name, *args, **kwargs)

    def fail_job(_limits):
        raise native.AppContainerError("synthetic-job-create")

    monkeypatch.setattr(native._AnonymousStdio, "create", create_stdio)
    monkeypatch.setattr(native._NoBreakawayJob, "create", fail_job)
    monkeypatch.setattr(native.ctypes, "WinDLL", load_dll)
    try:
        with pytest.raises(native.AppContainerError, match="synthetic-job-create") as caught:
            native.launch_in_appcontainer(_spec(tmp_path, "[Console]::Out.Write('must-not-run')"), profile, resource_limits=LIMITS)
        assert len(process_ids) == 1 and process_ids[0] > 0
        # 必须先证明真实暂停子进程已退出，不能把 HANDLE 关闭当成终止成功。
        _gone(process_ids[0])
        assert termination_results == [False, True]
        assert any("TerminateProcess" in note for note in caught.value.__notes__)
        _released(handles)
    finally:
        # 仅收口严格 RED 留下的本测试 PID，不参与此前终止断言。
        for process_id in process_ids:
            try:
                child = psutil.Process(process_id)
                child.kill()
                child.wait(3)
            except psutil.NoSuchProcess:
                pass


class _NativeFaults:
    def __init__(self, monkeypatch):
        original_dll = native.ctypes.WinDLL
        self.kernel32 = original_dll("kernel32", use_last_error=True)
        native._configure_kernel32(self.kernel32)
        self.kernel32.GetProcessId.argtypes = [ctypes.c_void_p]
        self.kernel32.GetProcessId.restype = ctypes.c_ulong
        self.kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self.kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        self.userenv = original_dll("userenv", use_last_error=True)
        self.userenv.DeleteAppContainerProfile.argtypes = [ctypes.c_wchar_p]
        self.userenv.DeleteAppContainerProfile.restype = ctypes.c_long
        self.handles, self.process_ids, self.owners, self.profiles = {}, [], [], []
        self.closed_handles, self.parent_streams, self.stdios, self.jobs = set(), {}, [], []
        self.close_events = []
        self.fail_job = self.block_terminate = self.block_wait = self.block_create = self.block_delete = False
        self.fail_pipe_at = self.fail_descriptor_at = self.fail_file_at = None
        self.fail_attribute_at = None
        self.fail_job_information = None
        self.block_descriptor_close = None
        self.parent_descriptors, self.closed_descriptors = {}, set()
        self.pipe_calls = self.descriptor_calls = self.file_calls = 0
        self.attribute_calls = 0
        self.block_close = None
        original_stdio = native._AnonymousStdio.create
        original_stdio_init = native._AnonymousStdio.__init__
        original_files = native._AnonymousStdio.parent_files
        original_job = native._NoBreakawayJob.create
        original_job_init = native._NoBreakawayJob.__init__
        original_profile = native.AppContainerProfile.create
        original_owner = native.AppContainerProcess.__init__
        original_descriptor = msvcrt.open_osfhandle
        original_fdopen, original_close = os.fdopen, os.close

        def observe_stdio(stdio, *args, **kwargs):
            original_stdio_init(stdio, *args, **kwargs)
            self.stdios.append(stdio)

        def create_pipe(read, write, security, size):
            self.pipe_calls += 1
            if self.pipe_calls == self.fail_pipe_at:
                ctypes.set_last_error(5)
                return 0
            result = self.kernel32.CreatePipe(read, write, security, size)
            if result:
                names = (("child_stdin", "parent_stdin"), ("parent_stdout", "child_stdout"), ("parent_stderr", "child_stderr"))[self.pipe_calls - 1]
                for name, pointer in zip(names, (read, write)):
                    self.handles[name] = int(ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value)
                    self.closed_handles.discard(name)
            return result

        def descriptor(handle, flags):
            self.descriptor_calls += 1
            if self.descriptor_calls == self.fail_descriptor_at:
                raise OSError("synthetic-descriptor-conversion")
            result = original_descriptor(handle, flags)
            name = next(name for name in ("parent_stdin", "parent_stdout", "parent_stderr") if self.handles[name] == handle)
            self.parent_descriptors[name] = result
            return result

        def fdopen(descriptor, *args, **kwargs):
            name = next((name for name, value in self.parent_descriptors.items() if value == descriptor), None)
            if name is not None:
                self.file_calls += 1
                if self.file_calls == self.fail_file_at:
                    raise OSError("synthetic-file-construction")
            stream = original_fdopen(descriptor, *args, **kwargs)
            if name is not None:
                self.parent_streams[name] = stream
            return stream

        def close_descriptor(descriptor):
            name = next((name for name, value in self.parent_descriptors.items() if value == descriptor and name not in self.parent_streams), None)
            if name is not None and name == self.block_descriptor_close:
                raise OSError("synthetic-descriptor-close")
            result = original_close(descriptor)
            if name is not None:
                self.closed_descriptors.add(name)
            return result

        def create_stdio():
            stdio = original_stdio()
            for name in ("child_stdin", "parent_stdin", "parent_stdout", "child_stdout", "parent_stderr", "child_stderr"):
                self.handles[name] = getattr(stdio, name)
                self.closed_handles.discard(name)
            return stdio

        def parent_files(stdio):
            streams = original_files(stdio)
            self.parent_streams.update(zip(("parent_stdin", "parent_stdout", "parent_stderr"), streams))
            return streams

        def create_job(limits):
            if self.fail_job:
                raise native.AppContainerError("synthetic-job-create")
            return original_job(limits)

        def create_job_handle(*args):
            handle = self.kernel32.CreateJobObjectW(*args)
            if handle:
                self.handles["job"] = int(handle)
                self.closed_handles.discard("job")
            return handle

        def observe_job(job, *args, **kwargs):
            original_job_init(job, *args, **kwargs)
            self.jobs.append(job)

        def set_job_information(handle, kind, *args):
            value = handle.value if isinstance(handle, ctypes.c_void_p) else handle
            failed_kind = {"limits": native._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, "cpu": native._JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION}.get(self.fail_job_information)
            if kind == failed_kind and value == self.handles.get("job"):
                ctypes.set_last_error(87)
                return 0
            return self.kernel32.SetInformationJobObject(handle, kind, *args)

        def create_profile(*, prefix="ChriptmasOS.PluginHands"):
            value = original_profile(prefix=prefix)
            self.profiles.append(value)
            return value

        def observe_owner(owner, *args, **kwargs):
            self.owners.append(owner)
            original_owner(owner, *args, **kwargs)

        def create_process(*args):
            if self.block_create:
                ctypes.set_last_error(5)
                return 0
            result = self.kernel32.CreateProcessW(*args)
            if result:
                info = ctypes.cast(args[-1], ctypes.POINTER(native._PROCESS_INFORMATION)).contents
                self.handles.update({"process": int(info.hProcess), "thread": int(info.hThread)})
                self.closed_handles.difference_update(("process", "thread"))
                self.process_ids.append(int(self.kernel32.GetProcessId(info.hProcess)))
            return result

        def update_attribute(*args):
            self.attribute_calls += 1
            if self.attribute_calls == self.fail_attribute_at:
                ctypes.set_last_error(5)
                return 0
            return self.kernel32.UpdateProcThreadAttribute(*args)

        def terminate(handle, exit_code):
            if self.block_terminate:
                ctypes.set_last_error(5)
                return 0
            return self.kernel32.TerminateProcess(handle, exit_code)

        def wait(handle, timeout):
            value = handle.value if isinstance(handle, ctypes.c_void_p) else handle
            if self.block_wait and value == self.handles.get("process"):
                return 0x00000102
            return self.kernel32.WaitForSingleObject(handle, timeout)

        def close(handle):
            value = handle.value if isinstance(handle, ctypes.c_void_p) else handle
            if self.block_close and value == self.handles.get(self.block_close):
                self.close_events.append((self.block_close, False))
                ctypes.set_last_error(5)
                return 0
            result = self.kernel32.CloseHandle(handle)
            self.close_events.extend((name, bool(result)) for name, target in self.handles.items() if target == value)
            if result:
                # 按创建代次记录真实 BOOL；旧整数随后可被 Job/RPC 重用。
                self.closed_handles.update(name for name, target in self.handles.items() if target == value)
            return result

        def delete(name):
            if self.block_delete:
                return -2147467259
            return self.userenv.DeleteAppContainerProfile(name)

        class KernelApi:
            CreatePipe = staticmethod(create_pipe)
            CreateJobObjectW = staticmethod(create_job_handle)
            SetInformationJobObject = staticmethod(set_job_information)
            UpdateProcThreadAttribute = staticmethod(update_attribute)
            CreateProcessW = staticmethod(create_process)
            TerminateProcess = staticmethod(terminate)
            WaitForSingleObject = staticmethod(wait)
            CloseHandle = staticmethod(close)

            def __getattr__(_self, name):
                return getattr(self.kernel32, name)

        class ProfileApi:
            DeleteAppContainerProfile = staticmethod(delete)

            def __getattr__(_self, name):
                return getattr(self.userenv, name)

        def load_dll(name, *args, **kwargs):
            if name == "kernel32":
                return KernelApi()
            if name == "userenv":
                return ProfileApi()
            return original_dll(name, *args, **kwargs)

        monkeypatch.setattr(native._AnonymousStdio, "create", create_stdio)
        monkeypatch.setattr(native._AnonymousStdio, "__init__", observe_stdio)
        monkeypatch.setattr(native._AnonymousStdio, "parent_files", parent_files)
        monkeypatch.setattr(native._NoBreakawayJob, "create", create_job)
        monkeypatch.setattr(native._NoBreakawayJob, "__init__", observe_job)
        monkeypatch.setattr(native.AppContainerProfile, "create", create_profile)
        monkeypatch.setattr(native.AppContainerProcess, "__init__", observe_owner)
        monkeypatch.setattr(native.ctypes, "WinDLL", load_dll)
        monkeypatch.setattr(msvcrt, "open_osfhandle", descriptor)
        monkeypatch.setattr(os, "fdopen", fdopen)
        monkeypatch.setattr(os, "close", close_descriptor)

    def recover(self):
        self.fail_job = self.block_terminate = self.block_wait = self.block_create = self.block_delete = False
        self.fail_pipe_at = self.fail_descriptor_at = self.fail_file_at = None
        self.fail_attribute_at = None
        self.fail_job_information = None
        self.block_descriptor_close = None
        self.block_close = None

    def cleanup_red(self):
        self.recover()
        # 仅收口本测试原生资源；通过断言必须早于这段 fixture 安全回收。
        for owner in self.owners:
            try:
                owner.close()
            except (OSError, native.AppContainerError):
                pass
        for job in self.jobs:
            if job._handle:
                job.close()
        for stdio in self.stdios:
            stdio.close_child()
            stdio.close_parent()
        for stream in self.parent_streams.values():
            if not stream.closed:
                stream.close()
        for value in self.profiles:
            value.close()

    def assert_released(self):
        for name in self.handles:
            if name in self.parent_streams:
                assert self.parent_streams[name].closed
            else:
                assert name in self.closed_handles


@pytest.mark.parametrize("fault", ("terminate", "wait", "process-close"))
def test_permanent_launch_cleanup_failure_retains_same_real_owner_for_retry(tmp_path, profile, monkeypatch, fault):
    controls = _NativeFaults(monkeypatch)
    controls.fail_job = True
    if fault == "terminate":
        controls.block_terminate = True
    elif fault == "wait":
        controls.block_wait = True
    else:
        controls.block_close = "process"
    try:
        with pytest.raises(native.AppContainerError, match="synthetic-job-create") as caught:
            native.launch_in_appcontainer(_spec(tmp_path, "exit 0"), profile, resource_limits=LIMITS)
        owner = caught.value._cleanup_process
        assert controls.owners == [owner]
        assert owner.process_id == controls.process_ids[0]
        assert owner.process_handle == controls.handles["process"]
        assert _handle_open(owner.process_handle)
        if fault != "process-close":
            assert owner.thread_handle == controls.handles["thread"]
            assert _handle_open(owner.thread_handle)
        _released({name: handle for name, handle in controls.handles.items() if name not in ("process", "thread")})
        if fault == "terminate":
            assert owner.wait(0.01) is None
        assert caught.value.__notes__
        controls.recover()
        owner.close()
        _gone(owner.process_id)
        controls.assert_released()
        assert owner.process_handle == owner.thread_handle == 0
        owner.close()
    finally:
        controls.cleanup_red()


def test_native_job_close_false_retains_original_handle_for_retry(monkeypatch):
    controls = _NativeFaults(monkeypatch)
    job = native._NoBreakawayJob.create(LIMITS)
    handle = job._handle
    controls.block_close = "job"
    try:
        with pytest.raises(native.AppContainerError, match="CloseHandle"):
            job.close()
        assert job._handle == handle and _handle_open(handle)
        controls.recover()
        job.close()
        assert job._handle == 0
        _released({"job": handle})
        job.close()
    finally:
        controls.cleanup_red()


def _cleanup_job_red(controls):
    controls.cleanup_red()
    # 失败配置中的 Job 没有成功关闭回执，仍是本次创建的原对象。
    handle = controls.handles.get("job")
    if handle and "job" not in controls.closed_handles and _handle_open(handle):
        assert controls.kernel32.CloseHandle(ctypes.c_void_p(handle))
        assert not _handle_open(handle)


@pytest.mark.parametrize("failure", ("limits", "cpu"))
def test_native_job_information_and_close_failures_retain_same_job_and_original_error(monkeypatch, failure):
    controls = _NativeFaults(monkeypatch)
    controls.fail_job_information = failure
    controls.block_close = "job"
    try:
        with pytest.raises(native.AppContainerError, match="SetInformationJobObject") as caught:
            native._NoBreakawayJob.create(LIMITS)
        assert _handle_open(controls.handles["job"])
        assert "job" not in controls.closed_handles
        print(f"OBSERVED failed_job_configuration={failure} handle={controls.handles['job']} native_valid=True close_true=False wrappers={len(controls.jobs)} pending_job={getattr(caught.value, '_cleanup_job', None) is not None}")
        operation = "SetInformationJobObject" + ("(CPU rate)" if failure == "cpu" else "")
        assert str(caught.value) == operation + " failed: winerror=87"
        assert len(controls.jobs) == 1
        job, handle = controls.jobs[0], controls.handles["job"]
        assert caught.value._cleanup_job is job
        assert job._handle == handle and _handle_open(handle)
        assert controls.process_ids == controls.owners == controls.stdios == []
        assert ("job", False) in controls.close_events and ("job", True) not in controls.close_events
        assert any("CloseHandle" in note for note in caught.value.__notes__)
        controls.recover()
        job.close()
        assert job._handle == 0 and not _handle_open(handle)
        assert controls.close_events.count(("job", True)) == 1
        job.close()
        assert controls.close_events.count(("job", True)) == 1
    finally:
        _cleanup_job_red(controls)


@pytest.mark.parametrize("failure", ("limits", "cpu"))
def test_failed_native_job_configuration_is_adopted_by_original_process_before_cleanup(tmp_path, profile, monkeypatch, failure):
    controls = _NativeFaults(monkeypatch)
    controls.fail_job_information = failure
    controls.block_close = "job"
    try:
        with pytest.raises(native.AppContainerError, match="SetInformationJobObject") as caught:
            native.launch_in_appcontainer(_spec(tmp_path, "[Console]::Out.Write('must-not-run')"), profile, resource_limits=LIMITS)
        assert _handle_open(controls.handles["job"])
        assert "job" not in controls.closed_handles
        print(f"OBSERVED failed_launch_job={failure} handle={controls.handles['job']} native_valid=True close_true=False pending_owner={getattr(caught.value, '_cleanup_process', None) is not None}")
        assert "winerror=87" in str(caught.value)
        assert len(controls.jobs) == len(controls.owners) == 1
        job, owner = controls.jobs[0], controls.owners[0]
        handle = controls.handles["job"]
        assert caught.value._cleanup_process is owner
        assert owner._job is job and job._handle == handle and _handle_open(handle)
        assert owner._has_cleanup_resources()
        assert owner.process_handle == owner.thread_handle == 0
        assert owner.stdin is owner.stdout is owner.stderr is owner._stdio is None
        assert not controls.parent_streams
        _gone(owner.process_id)
        assert all(name in controls.closed_handles for name in controls.handles if name != "job")
        assert ("job", False) in controls.close_events and ("job", True) not in controls.close_events
        controls.recover()
        owner.close()
        assert not owner._has_cleanup_resources()
        assert job._handle == 0 and not _handle_open(handle)
        assert controls.close_events.count(("job", True)) == 1
        controls.assert_released()
        owner.close()
        assert controls.close_events.count(("job", True)) == 1
    finally:
        _cleanup_job_red(controls)


def test_normal_job_close_requires_same_process_exit_fact_before_releasing_owner(tmp_path, profile, monkeypatch):
    controls = _NativeFaults(monkeypatch)
    try:
        owner = native.launch_in_appcontainer(_spec(tmp_path, "while ($true) { Start-Sleep -Milliseconds 100 }"), profile, resource_limits=LIMITS)
        assert controls.owners == [owner]
        assert owner.is_appcontainer()
        assert owner.wait(0.01) is None
        controls.block_wait = True
        with pytest.raises(native.AppContainerError, match="exit is unconfirmed"):
            owner.close()
        assert owner._job._handle == 0 and "job" in controls.closed_handles
        assert owner.process_handle == controls.handles["process"] and _handle_open(owner.process_handle)
        assert owner.thread_handle == controls.handles["thread"] and _handle_open(owner.thread_handle)
        assert owner.stdout is controls.parent_streams["parent_stdout"] and not owner.stdout.closed
        assert owner.stderr is controls.parent_streams["parent_stderr"] and not owner.stderr.closed
        assert profile.sid and not profile._deleted
        controls.recover()
        owner.close()
        assert owner.process_handle == owner.thread_handle == 0
        assert owner.stdin is owner.stdout is owner.stderr is None
        assert not owner._stderr_drain.is_alive()
        _gone(owner.process_id)
        controls.assert_released()
        profile.close()
        assert profile._deleted and profile._sid == 0
        owner.close()
    finally:
        controls.cleanup_red()


def test_internal_thread_start_failure_uses_one_real_process_owner(tmp_path, profile, monkeypatch):
    controls = _NativeFaults(monkeypatch)
    original_start = Thread.start

    def fail_after_start(worker):
        original_start(worker)
        if getattr(worker._target, "__name__", None) == "_drain_stderr":
            raise native.AppContainerError("synthetic-thread-start")

    monkeypatch.setattr(Thread, "start", fail_after_start)
    try:
        with pytest.raises(native.AppContainerError, match="synthetic-thread-start"):
            native.launch_in_appcontainer(_spec(tmp_path, "[Console]::In.ReadLine() | Out-Null"), profile, resource_limits=LIMITS)
        assert len(controls.owners) == 1
        owner = controls.owners[0]
        assert owner.process_handle == owner.thread_handle == owner._job._handle == 0
        assert not owner._stderr_drain.is_alive()
        _gone(owner.process_id)
        controls.assert_released()
    finally:
        controls.cleanup_red()


def _cleanup_raw_red(controls, name):
    controls.cleanup_red()
    # 此目标从未转交 CRT；没有成功关闭回执时才收口原创建代次。
    handle = controls.handles.get(name)
    if handle and name not in controls.closed_handles and _handle_open(handle):
        assert controls.kernel32.CloseHandle(ctypes.c_void_p(handle))
        assert not _handle_open(handle)


@pytest.mark.parametrize("name", ("parent_stdin", "parent_stdout", "parent_stderr", "child_stdin", "child_stdout", "child_stderr"))
def test_raw_pipe_close_false_retains_same_process_owner_and_cleans_other_handles(tmp_path, profile, monkeypatch, name):
    controls = _NativeFaults(monkeypatch)
    controls.fail_job = True
    controls.block_close = name
    try:
        with pytest.raises(native.AppContainerError) as caught:
            native.launch_in_appcontainer(_spec(tmp_path, "exit 0"), profile, resource_limits=LIMITS)
        expected = "synthetic-job-create" if name.startswith("parent_") else "CloseHandle(" + name + ")"
        assert expected in str(caught.value)
        assert len(controls.stdios) == len(controls.owners) == 1
        stdio, owner = controls.stdios[0], controls.owners[0]
        handle = controls.handles[name]
        assert name not in controls.closed_handles and _handle_open(handle)
        assert getattr(stdio, name) == handle
        assert caught.value._cleanup_process is owner and owner._has_cleanup_resources()
        assert owner._stdio is stdio
        assert owner.process_handle == owner.thread_handle == 0
        _gone(owner.process_id)
        assert all(target in controls.closed_handles for target in controls.handles if target != name)
        controls.recover()
        owner.close()
        assert getattr(stdio, name) == 0 and owner._stdio is None
        assert not owner._has_cleanup_resources()
        assert not _handle_open(handle)
        controls.assert_released()
        owner.close()
    finally:
        _cleanup_raw_red(controls, name)


@pytest.mark.parametrize("fail_at,name", ((2, "parent_stdout"), (3, "parent_stderr")))
def test_partial_parent_conversion_keeps_raw_handle_and_closes_already_converted_streams(tmp_path, profile, monkeypatch, fail_at, name):
    controls = _NativeFaults(monkeypatch)
    controls.fail_descriptor_at = fail_at
    controls.block_close = name
    try:
        with pytest.raises(OSError, match="synthetic-descriptor-conversion") as caught:
            native.launch_in_appcontainer(_spec(tmp_path, "exit 0"), profile, resource_limits=LIMITS)
        assert len(controls.owners) == len(controls.stdios) == 1
        owner, stdio = controls.owners[0], controls.stdios[0]
        assert len(controls.parent_streams) == fail_at - 1
        assert all(stream.closed for stream in controls.parent_streams.values())
        handle = controls.handles[name]
        assert getattr(stdio, name) == handle and _handle_open(handle)
        assert name not in controls.closed_handles
        assert caught.value._cleanup_process is owner and owner._stdio is stdio
        assert owner.process_handle == owner.thread_handle == owner._job._handle == 0
        _gone(owner.process_id)
        controls.recover()
        owner.close()
        assert owner._stdio is None and not owner._has_cleanup_resources()
        assert getattr(stdio, name) == 0 and not _handle_open(handle)
        controls.assert_released()
        owner.close()
    finally:
        _cleanup_raw_red(controls, name)


@pytest.mark.parametrize("fail_at,name", ((1, "parent_stdin"), (2, "parent_stdout"), (3, "parent_stderr")))
def test_file_construction_failure_retains_original_crt_descriptor_until_close_succeeds(tmp_path, profile, monkeypatch, fail_at, name):
    controls = _NativeFaults(monkeypatch)
    controls.fail_file_at = fail_at
    controls.block_descriptor_close = name
    descriptor = None
    try:
        with pytest.raises(OSError, match="synthetic-file-construction") as caught:
            native.launch_in_appcontainer(_spec(tmp_path, "exit 0"), profile, resource_limits=LIMITS)
        descriptor = controls.parent_descriptors[name]
        stdio, owner = controls.stdios[0], controls.owners[0]
        assert os.fstat(descriptor)
        assert _handle_open(controls.handles[name])
        assert getattr(stdio, name) == 0
        assert stdio._parent_descriptors[name] == descriptor
        assert name not in stdio._parent_streams and name not in controls.closed_descriptors
        assert all(stream.closed for stream in controls.parent_streams.values())
        assert caught.value._cleanup_process is owner and owner._stdio is stdio
        assert owner.process_handle == owner.thread_handle == owner._job._handle == 0
        assert all(target in controls.closed_handles for target in controls.handles if target != name and target not in controls.parent_streams)
        _gone(owner.process_id)
        controls.recover()
        owner.close()
        assert name in controls.closed_descriptors
        with pytest.raises(OSError):
            os.fstat(descriptor)
        assert owner._stdio is None and not owner._has_cleanup_resources()
        owner.close()
    finally:
        controls.cleanup_red()
        # 构造失败的 fd 未交给 FileIO，且未成功关闭时仍归本测试创建代次。
        pending = controls.parent_descriptors.get(name)
        if pending is not None and name not in controls.closed_descriptors:
            try:
                os.fstat(pending)
            except OSError:
                pass
            else:
                os.close(pending)


@pytest.mark.parametrize("name", ("parent_stdin", "parent_stdout", "parent_stderr", "child_stdin", "child_stdout", "child_stderr"))
def test_create_process_failure_retains_original_stdio_without_fabricating_process_owner(tmp_path, profile, monkeypatch, name):
    controls = _NativeFaults(monkeypatch)
    controls.block_create = True
    controls.block_close = name
    try:
        with pytest.raises(native.AppContainerError, match="CreateProcessW failed: winerror=5") as caught:
            native.launch_in_appcontainer(_spec(tmp_path, "exit 0"), profile, resource_limits=LIMITS)
        assert controls.process_ids == controls.owners == []
        assert len(controls.stdios) == 1
        stdio = controls.stdios[0]
        handle = controls.handles[name]
        assert caught.value._cleanup_stdio is stdio
        assert getattr(stdio, name) == handle and _handle_open(handle)
        assert all(target in controls.closed_handles for target in controls.handles if target != name)
        controls.recover()
        stdio.close()
        assert not stdio._has_cleanup_resources()
        assert getattr(stdio, name) == 0 and not _handle_open(handle)
        controls.assert_released()
        stdio.close()
    finally:
        _cleanup_raw_red(controls, name)


@pytest.mark.parametrize("fail_at", (2, 3))
def test_partial_pipe_creation_failure_retains_same_raw_stdio_for_retry(tmp_path, profile, monkeypatch, fail_at):
    controls = _NativeFaults(monkeypatch)
    controls.fail_pipe_at = fail_at
    controls.block_close = "parent_stdin"
    try:
        with pytest.raises(native.AppContainerError, match="CreatePipe") as caught:
            native.launch_in_appcontainer(_spec(tmp_path, "exit 0"), profile, resource_limits=LIMITS)
        assert controls.process_ids == controls.owners == []
        assert len(controls.stdios) == 1
        stdio = controls.stdios[0]
        handle = controls.handles["parent_stdin"]
        assert caught.value._cleanup_stdio is stdio
        assert stdio.parent_stdin == handle and _handle_open(handle)
        assert all(target in controls.closed_handles for target in controls.handles if target != "parent_stdin")
        controls.recover()
        stdio.close()
        assert not stdio._has_cleanup_resources() and not _handle_open(handle)
        controls.assert_released()
        stdio.close()
    finally:
        _cleanup_raw_red(controls, "parent_stdin")


@pytest.mark.parametrize("fail_at", (1, 2))
def test_security_attribute_failure_retains_same_stdio_and_preserves_original_error(tmp_path, profile, monkeypatch, fail_at):
    controls = _NativeFaults(monkeypatch)
    controls.fail_attribute_at = fail_at
    controls.block_close = "parent_stdin"
    try:
        with pytest.raises(native.AppContainerError, match="UpdateProcThreadAttribute") as caught:
            native.launch_in_appcontainer(_spec(tmp_path, "exit 0"), profile, resource_limits=LIMITS)
        assert controls.process_ids == controls.owners == []
        assert len(controls.stdios) == 1
        stdio = controls.stdios[0]
        handle = controls.handles["parent_stdin"]
        assert caught.value._cleanup_stdio is stdio
        assert stdio.parent_stdin == handle and _handle_open(handle)
        assert all(target in controls.closed_handles for target in controls.handles if target != "parent_stdin")
        controls.recover()
        stdio.close()
        assert not stdio._has_cleanup_resources() and not _handle_open(handle)
        controls.assert_released()
        stdio.close()
    finally:
        _cleanup_raw_red(controls, "parent_stdin")


@pytest.mark.parametrize("mode", (None, False, 1, "", "external", "Host", []))
def test_invalid_reader_mode_rejects_before_any_pipe_or_process(tmp_path, profile, monkeypatch, mode):
    entered = []

    def forbidden_create():
        entered.append(True)
        raise AssertionError("pipe creation must not start")

    monkeypatch.setattr(native._AnonymousStdio, "create", forbidden_create)
    with pytest.raises(ValueError, match="stderr reader"):
        native.launch_in_appcontainer(_spec(tmp_path, "exit 0"), profile, resource_limits=LIMITS, stderr_reader=mode)
    assert entered == []
