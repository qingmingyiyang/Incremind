import ctypes
import os
from pathlib import Path
from secrets import token_hex
import stat

from fastapi.responses import FileResponse
from starlette.datastructures import MutableHeaders
from starlette.concurrency import run_in_threadpool

from core.external_extension_runtime.windows_handle_io import (
    _Nt, _Unicode, _ObjectAttributes, _IoStatus, _NOT_FOUND, _status, _directory,
    _handle_directory_identity, WindowsHandleIoError,
    _READ, _ATTR, _SYNCHRONIZE, _OPEN, _DIR, _SYNC, _OPEN_REPARSE,
)
from .external_workspace import task_path_identity
from ..server_jobs import retain_until_finished
from .shared_projects import SharedProjectError


def _changed():
    raise SharedProjectError('shared_library_file_changed', status=409)


def source_workspace(query):
    query.validate_retrieval_origin()
    root = Path(query.material_owner.root).absolute()
    try:
        root_identity = task_path_identity(root, directory=True)
        if tuple(root_identity[:2]) != query.origin_admission.lease.root_identity:
            _changed()
        try:
            workspace_identity = task_path_identity(root / 'workspace', directory=True)
        except FileNotFoundError:
            workspace_identity = None
        return root, root_identity, workspace_identity
    except (OSError, ValueError):
        _changed()


class _SourceNt(_Nt):
    def _create(self, parent, name, access, disposition, options):
        # 原生名称长度按 UTF-16 字节计算，保留中文及补充平面字符路径。
        length = len(name.encode('utf-16-le'))
        if length > 65532:
            _changed()
        unit = _Unicode(length, length + 2, name)
        attributes = _ObjectAttributes(ctypes.sizeof(_ObjectAttributes), ctypes.c_void_p(parent or 0),
            ctypes.pointer(unit), 0x40 | 0x1000, None, None)
        handle, io = ctypes.c_void_p(), _IoStatus()
        result = int(self.dll.NtCreateFile(ctypes.byref(handle), access, ctypes.byref(attributes),
            ctypes.byref(io), None, 0, 7, disposition, options, None, 0))
        if result in _NOT_FOUND:
            return 0
        _status(result, 'NtCreateFile')
        return int(handle.value or 0)

    def open_dir(self, parent, name, *, create=False):
        if create:
            _changed()
        return self._create(parent, name, _READ | _ATTR | _SYNCHRONIZE,
            _OPEN, _DIR | _SYNC | _OPEN_REPARSE)


class SourceFile:
    def __init__(self, query, path):
        self.query, self.path = query, Path(path).absolute()
        self.stream, self.native_state, self.api = None, None, None
        self.expected = self._state()

    def _state(self):
        root, root_identity, workspace_identity = source_workspace(self.query)
        if workspace_identity is None or not self.path.is_relative_to(root / 'workspace'):
            _changed()
        try:
            file_identity = task_path_identity(self.path)
            if self.path.lstat().st_nlink != 1:
                _changed()
            if file_identity[0] != root_identity[0]:
                _changed()
            directories = [(root, root_identity)]
            for node in reversed(self.path.parent.parents):
                if node != root and node.is_relative_to(root):
                    directories.append((node, task_path_identity(node, directory=True)))
            if self.path.parent != root:
                directories.append((self.path.parent, task_path_identity(self.path.parent, directory=True)))
            if any(identity[0] != root_identity[0] for _path, identity in directories):
                _changed()
            return tuple((str(node), tuple(identity)) for node, identity in directories), tuple(file_identity)
        except (OSError, ValueError):
            _changed()

    def _open_windows(self):
        import msvcrt
        self.api = _SourceNt()
        handles, file_handle = [], 0
        try:
            for path, expected in self.expected[0]:
                current = (self.api.open_root(Path(path)) if not handles
                    else self.api.open_dir(handles[-1], Path(path).name))
                if not current:
                    _changed()
                handles.append(current)
                _directory(self.api, current)
                if _handle_directory_identity(current) != list(expected):
                    _changed()
            file_handle = self.api.open_file(handles[-1], self.path.name)
            if not file_handle or self.api.is_dir(file_handle) or self.api.is_reparse(file_handle):
                _changed()
            self.native_state = self.api.file_state(file_handle)
            descriptor = msvcrt.open_osfhandle(file_handle, os.O_RDONLY | os.O_BINARY)
            file_handle = 0
            return descriptor
        finally:
            if file_handle:
                self.api.close(file_handle)
            for handle in reversed(handles):
                self.api.close(handle)

    def _open_posix(self):
        from .server_deletion import _mount_id
        handles = []
        try:
            for path, expected in self.expected[0]:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                current = (os.open(path, flags) if not handles
                    else os.open(Path(path).name, flags, dir_fd=handles[-1]))
                handles.append(current)
                value = os.fstat(current)
                if (value.st_dev, value.st_ino, value.st_mode) != expected:
                    _changed()
                if _mount_id(current) != _mount_id(handles[0]):
                    _changed()
            descriptor = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=handles[-1])
            try:
                if _mount_id(descriptor) != _mount_id(handles[0]):
                    _changed()
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise
        finally:
            for descriptor in reversed(handles):
                os.close(descriptor)

    def open(self):
        try:
            self.validate()
            descriptor = self._open_windows() if os.name == 'nt' else self._open_posix()
            try:
                self.stream = os.fdopen(descriptor, 'rb')
            except BaseException:
                os.close(descriptor)
                raise
            self.validate()
            return os.fstat(descriptor)
        except (OSError, ValueError, WindowsHandleIoError):
            self.close()
            _changed()

    def validate(self):
        if self._state() != self.expected:
            _changed()
        if self.stream is not None:
            value = os.fstat(self.stream.fileno())
            if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
                    or (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns) != self.expected[1]):
                _changed()
            if self.api is not None:
                import msvcrt
                if self.api.file_state(msvcrt.get_osfhandle(self.stream.fileno())) != self.native_state:
                    _changed()

    def read(self, size):
        self.validate()
        value = self.stream.read(size)
        self.validate()
        return value

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


class SharedSourceResponse(FileResponse):
    def __init__(self, query, original):
        self.file = SourceFile(query, original.path)
        super().__init__(original.path, media_type=original.media_type, filename=original.filename,
            headers={key: value for key, value in original.headers.items()
                if key not in {'content-length', 'last-modified', 'etag'}})
        self.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    async def __call__(self, scope, receive, send):
        try:
            self.stat_result = await retain_until_finished(run_in_threadpool(self.file.open))
            self.set_stat_headers(self.stat_result)
            async def checked(message):
                await retain_until_finished(run_in_threadpool(self.file.validate))
                await send(message)
            await super().__call__(scope, receive, checked)
        finally:
            await retain_until_finished(run_in_threadpool(self.file.close))

    async def _body(self, send, start, end, *, more=False):
        await retain_until_finished(run_in_threadpool(self.file.stream.seek, start))
        while start < end:
            chunk = await retain_until_finished(run_in_threadpool(self.file.read, min(self.chunk_size, end - start)))
            if not chunk:
                _changed()
            start += len(chunk)
            await send({'type': 'http.response.body', 'body': chunk, 'more_body': more or start < end})
        if start == end and self.stat_result.st_size == 0 and not more:
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    async def _handle_simple(self, send, send_header_only, send_pathsend):
        await send({'type': 'http.response.start', 'status': self.status_code, 'headers': self.raw_headers})
        if send_header_only:
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        else:
            await self._body(send, 0, self.stat_result.st_size)

    async def _handle_single_range(self, send, start, end, file_size, send_header_only):
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers['content-range'], headers['content-length'] = f'bytes {start}-{end - 1}/{file_size}', str(end - start)
        await send({'type': 'http.response.start', 'status': 206, 'headers': headers.raw})
        if send_header_only:
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        else:
            await self._body(send, start, end)

    async def _handle_multiple_ranges(self, send, ranges, file_size, send_header_only):
        # 范围解析及 multipart 编排复用 Starlette，文件读取始终使用已绑定的真实句柄。
        boundary = token_hex(13)
        length, header = self.generate_multipart(ranges, boundary, file_size, self.headers['content-type'])
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers['content-type'], headers['content-length'] = f'multipart/byteranges; boundary={boundary}', str(length)
        await send({'type': 'http.response.start', 'status': 206, 'headers': headers.raw})
        if send_header_only:
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
            return
        for start, end in ranges:
            await send({'type': 'http.response.body', 'body': header(start, end), 'more_body': True})
            await self._body(send, start, end, more=True)
            await send({'type': 'http.response.body', 'body': b'\r\n', 'more_body': True})
        await send({'type': 'http.response.body', 'body': f'--{boundary}--'.encode('latin-1'), 'more_body': False})
