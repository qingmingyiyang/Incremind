"""Windows handle-relative I/O for reviewed external extension trees.

The absolute managed-root path is used once, to acquire a directory HANDLE.
Every subsequent walk, read, directory creation, file creation, and publish is
relative to a verified parent HANDLE.  There is deliberately no path-based
fallback on Windows: an unsupported NT operation fails closed.

This protects name-resolution races (including junction and symlink swaps).
It does not attempt to solve same-token DACL attacks, power-loss durability of
directory metadata, or a hostile kernel/filesystem driver.
"""
from __future__ import annotations

import ctypes
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4


class WindowsHandleIoError(RuntimeError):
    """A managed-tree operation could not retain handle-relative safety."""


_OK = 0
_NOT_FOUND = {ctypes.c_long(0xC0000034).value, ctypes.c_long(0xC000003A).value}
_NO_MORE = ctypes.c_long(0x80000006).value
_END_OF_FILE = ctypes.c_long(0xC0000011).value
_OBJ_CASE_INSENSITIVE = 0x40
_OBJ_DONT_REPARSE = 0x1000
_READ = 0x1
_WRITE = 0x2
_ATTR = 0x80
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
_SHARE_ALL = 0x7
_OPEN = 1
_CREATE = 2
_OPEN_IF = 3
_DIR = 1
_SYNC = 0x20
_OPEN_REPARSE = 0x00200000
_REPARSE_ATTRIBUTE = 0x400
_BASIC = 4
_STANDARD = 5
_DIRECTORY_INFO = 1
_RENAME_INFO = 10
_BUFFER = 64 * 1024
_MAX_BOUNDED_RELATIVE_PATH_BYTES = 512
_MAX_BOUNDED_COMPONENTS = 256


class _Unicode(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", ctypes.c_wchar_p)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ulong), ("RootDirectory", ctypes.c_void_p), ("ObjectName", ctypes.POINTER(_Unicode)), ("Attributes", ctypes.c_ulong), ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p)]


class _IoStatus(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_long), ("Information", ctypes.c_size_t)]


class _Nt:
    def __init__(self) -> None:
        dll = ctypes.WinDLL("ntdll", use_last_error=True)
        dll.NtCreateFile.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong, ctypes.POINTER(_ObjectAttributes), ctypes.POINTER(_IoStatus), ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong]
        dll.NtCreateFile.restype = ctypes.c_long
        dll.NtQueryInformationFile.argtypes = [ctypes.c_void_p, ctypes.POINTER(_IoStatus), ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        dll.NtQueryInformationFile.restype = ctypes.c_long
        dll.NtQueryDirectoryFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_IoStatus), ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ubyte, ctypes.c_void_p, ctypes.c_ubyte]
        dll.NtQueryDirectoryFile.restype = ctypes.c_long
        dll.NtReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_IoStatus), ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
        dll.NtReadFile.restype = ctypes.c_long
        dll.NtWriteFile.argtypes = list(dll.NtReadFile.argtypes)
        dll.NtWriteFile.restype = ctypes.c_long
        dll.NtSetInformationFile.argtypes = [ctypes.c_void_p, ctypes.POINTER(_IoStatus), ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        dll.NtSetInformationFile.restype = ctypes.c_long
        dll.NtClose.argtypes = [ctypes.c_void_p]
        dll.NtClose.restype = ctypes.c_long
        self.dll = dll

    def close(self, handle: int) -> None:
        if handle:
            self.dll.NtClose(ctypes.c_void_p(handle))

    def open_root(self, path: Path) -> int:
        return self._create(None, "\\??\\" + str(path), _READ | _ATTR | _SYNCHRONIZE, _OPEN, _DIR | _SYNC | _OPEN_REPARSE)

    def open_dir(self, parent: int, name: str, *, create: bool = False) -> int:
        return self._create(parent, name, _READ | _WRITE | _ATTR | _DELETE | _SYNCHRONIZE, _OPEN_IF if create else _OPEN, _DIR | _SYNC | _OPEN_REPARSE)

    def create_dir_new(self, parent: int, name: str) -> int:
        return self._create(parent, name, _READ | _WRITE | _ATTR | _DELETE | _SYNCHRONIZE, _CREATE, _DIR | _SYNC | _OPEN_REPARSE)

    def open_file_new(self, parent: int, name: str) -> int:
        return self._create(parent, name, _READ | _WRITE | _ATTR | _SYNCHRONIZE, _CREATE, _SYNC | _OPEN_REPARSE)

    def open_file(self, parent: int, name: str) -> int:
        return self._create(parent, name, _READ | _ATTR | _SYNCHRONIZE, _OPEN, _SYNC | _OPEN_REPARSE)

    def _create(self, parent: int | None, name: str, access: int, disposition: int, options: int) -> int:
        unit = _Unicode(len(name) * 2, (len(name) + 1) * 2, name)
        attrs = _ObjectAttributes(ctypes.sizeof(_ObjectAttributes), ctypes.c_void_p(parent or 0), ctypes.pointer(unit), _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE, None, None)
        handle, io = ctypes.c_void_p(), _IoStatus()
        status = int(self.dll.NtCreateFile(ctypes.byref(handle), access, ctypes.byref(attrs), ctypes.byref(io), None, 0, _SHARE_ALL, disposition, options, None, 0))
        if status in _NOT_FOUND:
            return 0
        _status(status, "NtCreateFile")
        return int(handle.value or 0)

    def is_dir(self, handle: int) -> bool:
        buf, io = (ctypes.c_ubyte * 32)(), _IoStatus()
        _status(int(self.dll.NtQueryInformationFile(ctypes.c_void_p(handle), ctypes.byref(io), ctypes.byref(buf), len(buf), _STANDARD)), "NtQueryInformationFile")
        return bool(buf[21])

    def is_reparse(self, handle: int) -> bool:
        buf, io = (ctypes.c_ubyte * 40)(), _IoStatus()
        _status(int(self.dll.NtQueryInformationFile(ctypes.c_void_p(handle), ctypes.byref(io), ctypes.byref(buf), len(buf), _BASIC)), "NtQueryInformationFile")
        return bool(int.from_bytes(bytes(buf[32:36]), "little") & _REPARSE_ATTRIBUTE)

    def size(self, handle: int) -> int:
        """Return a regular file's current EOF without resolving its name again."""
        buf, io = (ctypes.c_ubyte * 32)(), _IoStatus()
        _status(int(self.dll.NtQueryInformationFile(ctypes.c_void_p(handle), ctypes.byref(io), ctypes.byref(buf), len(buf), _STANDARD)), "NtQueryInformationFile")
        size = int.from_bytes(bytes(buf[8:16]), "little", signed=True)
        if size < 0:
            raise WindowsHandleIoError("file has an invalid byte length")
        return size

    def names(self, directory: int, *, maximum: int) -> tuple[str, ...]:
        if maximum < 0: raise WindowsHandleIoError("directory entry limit is invalid")
        result: list[str] = []; restart = True
        while True:
            buf, io = (ctypes.c_ubyte * _BUFFER)(), _IoStatus()
            status = int(self.dll.NtQueryDirectoryFile(ctypes.c_void_p(directory), None, None, None, ctypes.byref(io), ctypes.byref(buf), len(buf), _DIRECTORY_INFO, 0, None, int(restart)))
            restart = False
            if status == _NO_MORE: break
            _status(status, "NtQueryDirectoryFile")
            total, offset = int(io.Information), 0
            if total <= 0 or total > len(buf): raise WindowsHandleIoError("invalid directory buffer")
            while True:
                if offset + 64 > total: raise WindowsHandleIoError("truncated directory entry")
                next_offset = int.from_bytes(bytes(buf[offset:offset+4]), "little")
                size = int.from_bytes(bytes(buf[offset+60:offset+64]), "little")
                if size % 2 or offset + 64 + size > total: raise WindowsHandleIoError("invalid directory name")
                name = ctypes.string_at(ctypes.addressof(buf) + offset + 64, size).decode("utf-16-le")
                if name not in (".", ".."):
                    _component(name); result.append(name)
                    if len(result) > maximum: raise WindowsHandleIoError("directory contains too many entries")
                if not next_offset: break
                if next_offset < 64 or offset + next_offset >= total: raise WindowsHandleIoError("invalid directory offset")
                offset += next_offset
        if len(result) != len(set(result)): raise WindowsHandleIoError("directory changed during enumeration")
        return tuple(result)

    def read(self, handle: int, *, maximum: int) -> bytes:
        if maximum < 0: raise WindowsHandleIoError("file byte limit is invalid")
        parts: list[bytes] = []
        total = 0
        while True:
            remaining = maximum + 1 - total
            if remaining <= 0: raise WindowsHandleIoError("file exceeds expected byte length")
            requested = min(_BUFFER, remaining)
            buf, io = (ctypes.c_ubyte * requested)(), _IoStatus()
            status = int(self.dll.NtReadFile(ctypes.c_void_p(handle), None, None, None, ctypes.byref(io), ctypes.byref(buf), len(buf), None, None))
            if status == _END_OF_FILE: return b"".join(parts)
            _status(status, "NtReadFile")
            count = int(io.Information)
            if count > len(buf): raise WindowsHandleIoError("invalid read length")
            parts.append(bytes(buf[:count]))
            total += count
            if total > maximum: raise WindowsHandleIoError("file exceeds expected byte length")
            if count < requested: return b"".join(parts)

    def write(self, handle: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + _BUFFER]
            buf, io = ctypes.create_string_buffer(chunk), _IoStatus()
            _status(int(self.dll.NtWriteFile(ctypes.c_void_p(handle), None, None, None, ctypes.byref(io), ctypes.byref(buf), len(chunk), None, None)), "NtWriteFile")
            count = int(io.Information)
            if not count or count > len(chunk): raise WindowsHandleIoError("short write")
            offset += count
        if not ctypes.windll.kernel32.FlushFileBuffers(ctypes.c_void_p(handle)):
            raise WindowsHandleIoError("FlushFileBuffers failed")

    def rename_no_replace(self, source: int, target_parent: int, name: str) -> None:
        encoded = name.encode("utf-16-le")
        # FILE_RENAME_INFORMATION: BOOLEAN + padding + HANDLE + ULONG + WCHAR[]
        # On the supported 64-bit ABI the variable file name begins at 20.
        size = 20 + len(encoded); buf = (ctypes.c_ubyte * size)()
        ctypes.memmove(ctypes.addressof(buf) + 8, ctypes.byref(ctypes.c_void_p(target_parent)), ctypes.sizeof(ctypes.c_void_p))
        buf[16:20] = (len(encoded)).to_bytes(4, "little")
        ctypes.memmove(ctypes.addressof(buf) + 20, encoded, len(encoded))
        io = _IoStatus()
        _status(int(self.dll.NtSetInformationFile(ctypes.c_void_p(source), ctypes.byref(io), ctypes.byref(buf), len(buf), _RENAME_INFO)), "NtSetInformationFile(FileRenameInformation)")


class WindowsHandleTreeIo:
    """Reusable handle-relative exact-tree reader and publisher."""

    def ensure_directory_chain(self, root: Path, parts: Sequence[str]) -> None:
        """Create or verify one directory chain below an existing trusted root."""
        _windows()
        target = _parts(parts)
        api = _Nt()
        root_h = api.open_root(root)
        if not root_h:
            raise FileNotFoundError(root)
        try:
            _directory(api, root_h)
            self._after_root_open(root_h)
            directory = self._ensure_dirs(api, root_h, target)
            if directory != root_h:
                api.close(directory)
        finally:
            api.close(root_h)

    def write_new_tree(self, root: Path, target_parts: Sequence[str], files: Mapping[str, bytes],
                       *, expected_root_identity: Sequence[int] | None = None) -> bool:
        _windows(); parts = _parts(target_parts); api = _Nt(); root_h = api.open_root(root)
        if not root_h: raise WindowsHandleIoError("managed root is unavailable")
        try:
            _directory(api, root_h)
            if expected_root_identity is not None:
                if (len(expected_root_identity) != 3
                        or any(type(value) is not int for value in expected_root_identity)
                        or _handle_directory_identity(root_h) != list(expected_root_identity)):
                    raise WindowsHandleIoError("managed root identity changed")
            self._after_root_open(root_h)
            parent = self._ensure_dirs(api, root_h, parts[:-1])
            try:
                existing = api.open_dir(parent, parts[-1])
                if existing:
                    api.close(existing); return False
                staging = self._ensure_dirs(api, root_h, (".staging",))
                try:
                    stage_name = self._new_stage_name(); stage = api.create_dir_new(staging, stage_name)
                    if not stage: raise WindowsHandleIoError("staging creation failed")
                    try:
                        _directory(api, stage); self._write_files(api, stage, files); self._assert_exact(api, stage, {name: len(value) for name, value in files.items()})
                        self._publish(api, stage, parent, parts[-1])
                    finally: api.close(stage)
                finally: api.close(staging)
            finally:
                if parent != root_h: api.close(parent)
        finally: api.close(root_h)
        return True

    def read_exact_tree(self, root: Path, target_parts: Sequence[str], expected_paths: Sequence[str], *, expected_sizes: Mapping[str, int] | None = None) -> dict[str, bytes]:
        _windows(); parts = _parts(target_parts); expected = frozenset(expected_paths)
        sizes = dict(expected_sizes) if expected_sizes is not None else {path: 2 * 1024 * 1024 for path in expected}
        if set(sizes) != expected or any(not isinstance(size, int) or size < 0 for size in sizes.values()):
            raise WindowsHandleIoError("expected file sizes are invalid")
        api = _Nt(); root_h = api.open_root(root)
        if not root_h: raise FileNotFoundError(root)
        try:
            _directory(api, root_h); self._after_root_open(root_h); current = root_h
            handles: list[int] = []
            try:
                for part in parts:
                    self._before_child_open(current, part)
                    child = api.open_dir(current, part)
                    if not child: raise FileNotFoundError(part)
                    _directory(api, child); handles.append(child); current = child
                return self._assert_exact(
                    api, current, sizes, exact_sizes=expected_sizes is not None,
                )
            finally:
                for handle in reversed(handles): api.close(handle)
        finally: api.close(root_h)

    def read_bounded_tree(
        self,
        root: Path,
        parts: Sequence[str],
        *,
        max_files: int,
        max_file_bytes: int,
        max_total_bytes: int,
    ) -> dict[str, bytes]:
        """Freeze a tree through one root HANDLE, enforcing hard resource limits.

        The returned mapping is canonical (``/`` separators) and is safe to
        hand to a later immutable-package parser: no managed path is reopened
        after this method returns.
        """
        _windows()
        target = _parts(parts)
        if (
            not isinstance(max_files, int)
            or not isinstance(max_file_bytes, int)
            or not isinstance(max_total_bytes, int)
            or max_files < 0
            or max_file_bytes < 0
            or max_total_bytes < 0
        ):
            raise WindowsHandleIoError("bounded tree limits are invalid")
        api = _Nt()
        root_h = api.open_root(root)
        if not root_h:
            raise FileNotFoundError(root)
        try:
            _directory(api, root_h)
            self._after_root_open(root_h)
            current = root_h
            handles: list[int] = []
            try:
                for part in target:
                    self._before_child_open(current, part)
                    child = api.open_dir(current, part)
                    if not child:
                        raise FileNotFoundError(part)
                    _directory(api, child)
                    handles.append(child)
                    current = child
                state = {"entries": 0, "files": 0, "total": 0}
                # Directories are not files, but must still be bounded so a
                # deliberately empty deep/wide tree cannot consume unbounded
                # traversal resources.  The allowance preserves ordinary
                # package layouts while keeping the caller's file cap strict.
                # ArtifactInventory permits 320-byte paths.  The generic
                # HANDLE reader allows a bounded superset, including the
                # quarantine ``artifact/`` prefix, without accepting an
                # unbounded all-directory tree.
                entry_limit = max(
                    1,
                    max_files * (_MAX_BOUNDED_COMPONENTS + 1) + 1,
                )
                return self._read_bounded(api, current, "", state, max_files, entry_limit, max_file_bytes, max_total_bytes)
            finally:
                for handle in reversed(handles):
                    api.close(handle)
        finally:
            api.close(root_h)

    def move_dir_no_replace(
        self,
        root: Path,
        source_parts: Sequence[str],
        target_parent_parts: Sequence[str],
        target_name: str,
    ) -> None:
        """Move one managed directory using only root-relative HANDLE opens."""
        _windows()
        source = _parts(source_parts)
        target_parent = _optional_parts(target_parent_parts)
        name = _component(target_name)
        api = _Nt()
        root_h = api.open_root(root)
        if not root_h:
            raise FileNotFoundError(root)
        try:
            _directory(api, root_h)
            self._after_root_open(root_h)
            source_h, source_handles = self._open_existing_dirs(api, root_h, source)
            try:
                parent_h, parent_handles = self._open_existing_dirs(api, root_h, target_parent)
                try:
                    occupied = api.open_dir(parent_h, name)
                    if occupied:
                        api.close(occupied)
                        raise WindowsHandleIoError("target already exists")
                    api.rename_no_replace(source_h, parent_h, name)
                finally:
                    for handle in reversed(parent_handles):
                        api.close(handle)
            finally:
                for handle in reversed(source_handles):
                    api.close(handle)
        finally:
            api.close(root_h)

    def _ensure_dirs(self, api: _Nt, root: int, parts: Sequence[str]) -> int:
        current = root; opened: list[int] = []
        for part in parts:
            self._before_child_open(current, part)
            child = api.open_dir(current, part, create=True)
            if not child: raise WindowsHandleIoError("directory creation failed")
            _directory(api, child); opened.append(child); current = child
        # Return final handle; intermediates can close because children retain objects.
        for handle in opened[:-1]: api.close(handle)
        return opened[-1] if opened else root

    def _write_files(self, api: _Nt, root: int, files: Mapping[str, bytes]) -> None:
        for rel, data in files.items():
            parts = _parts(rel.split("/")); parent = self._ensure_dirs(api, root, parts[:-1])
            try:
                file_h = api.open_file_new(parent, parts[-1])
                if not file_h: raise WindowsHandleIoError("file creation failed")
                try:
                    if api.is_reparse(file_h) or api.is_dir(file_h): raise WindowsHandleIoError("unsafe file handle")
                    api.write(file_h, data)
                finally: api.close(file_h)
            finally:
                if parent != root: api.close(parent)

    def _assert_exact(
        self,
        api: _Nt,
        directory: int,
        expected: Mapping[str, int],
        prefix: str = "",
        *,
        exact_sizes: bool = True,
    ) -> dict[str, bytes]:
        _directory(api, directory); actual: dict[str, bytes] = {}
        direct_names = {path.removeprefix(prefix).split("/", 1)[0] for path in expected if path.startswith(prefix)}
        for name in api.names(directory, maximum=len(direct_names)):
            self._before_child_open(directory, name)
            child = api.open_file(directory, name)
            if not child: raise WindowsHandleIoError("directory changed during enumeration")
            try:
                if api.is_reparse(child): raise WindowsHandleIoError("reparse point encountered")
                rel = f"{prefix}{name}"
                if api.is_dir(child):
                    wanted = any(item.startswith(rel + "/") for item in expected)
                    if not wanted: raise WindowsHandleIoError("unexpected directory")
                    actual.update(self._assert_exact(
                        api, child, expected, rel + "/", exact_sizes=exact_sizes,
                    ))
                else:
                    if rel not in expected: raise WindowsHandleIoError("unexpected file")
                    actual[rel] = api.read(child, maximum=expected[rel])
                    if exact_sizes and len(actual[rel]) != expected[rel]:
                        raise WindowsHandleIoError("file byte length differs from expected")
            finally: api.close(child)
        if not prefix and set(actual) != set(expected): raise WindowsHandleIoError("materialized tree is incomplete")
        return actual

    def _read_bounded(
        self,
        api: _Nt,
        directory: int,
        prefix: str,
        state: dict[str, int],
        max_files: int,
        max_entries: int,
        max_file_bytes: int,
        max_total_bytes: int,
    ) -> dict[str, bytes]:
        _directory(api, directory)
        result: dict[str, bytes] = {}
        # Entry count is also bounded, so an all-directory tree cannot evade a
        # file-count limit by causing unbounded recursion or enumeration.
        remaining_entries = max_entries - state["entries"]
        for name in api.names(directory, maximum=remaining_entries):
            state["entries"] += 1
            if state["entries"] > max_entries:
                raise WindowsHandleIoError("tree contains too many entries")
            self._before_child_open(directory, name)
            child = api.open_file(directory, name)
            if not child:
                raise WindowsHandleIoError("directory changed during enumeration")
            try:
                if api.is_reparse(child):
                    raise WindowsHandleIoError("reparse point encountered")
                rel = f"{prefix}{name}"
                try:
                    path_bytes = len(rel.encode("utf-8"))
                except UnicodeEncodeError as error:
                    raise WindowsHandleIoError("tree path is not valid UTF-8") from error
                if (
                    path_bytes > _MAX_BOUNDED_RELATIVE_PATH_BYTES
                    or rel.count("/") + 1 > _MAX_BOUNDED_COMPONENTS
                ):
                    raise WindowsHandleIoError("tree path exceeds its hard limit")
                if api.is_dir(child):
                    nested = self._read_bounded(
                        api, child, rel + "/", state, max_files, max_entries,
                        max_file_bytes, max_total_bytes,
                    )
                    if not nested:
                        raise WindowsHandleIoError("tree contains an empty directory")
                    if set(result).intersection(nested):
                        raise WindowsHandleIoError("bounded tree changed during read")
                    result.update(nested)
                    continue
                state["files"] += 1
                if state["files"] > max_files:
                    raise WindowsHandleIoError("tree contains too many files")
                before = api.size(child)
                remaining_total = max_total_bytes - state["total"]
                if before > max_file_bytes:
                    raise WindowsHandleIoError("file exceeds per-file byte limit")
                if before > remaining_total:
                    raise WindowsHandleIoError("tree exceeds total byte limit")
                data = api.read(child, maximum=before)
                if len(data) != before or api.size(child) != before:
                    raise WindowsHandleIoError("file byte length changed during read")
                state["total"] += before
                if state["total"] > max_total_bytes or rel in result:
                    raise WindowsHandleIoError("bounded tree changed during read")
                result[rel] = data
            finally:
                api.close(child)
        return result

    def _open_existing_dirs(self, api: _Nt, root: int, parts: Sequence[str]) -> tuple[int, list[int]]:
        current = root
        handles: list[int] = []
        for part in parts:
            self._before_child_open(current, part)
            child = api.open_dir(current, part)
            if not child:
                raise FileNotFoundError(part)
            _directory(api, child)
            handles.append(child)
            current = child
        return current, handles

    @staticmethod
    def _publish(api: _Nt, stage: int, target_parent: int, target_name: str) -> None:
        occupied = api.open_dir(target_parent, target_name)
        if occupied:
            api.close(occupied); raise WindowsHandleIoError("target already exists")
        api.rename_no_replace(stage, target_parent, target_name)

    @staticmethod
    def _new_stage_name() -> str:
        return uuid4().hex

    def _after_root_open(self, _root_handle: int) -> None:
        """Private race-test seam; production callers do not override this."""

    def _before_child_open(self, _parent_handle: int, _name: str) -> None:
        """Private race-test seam; production callers do not override this."""


def _handle_directory_identity(handle: int) -> list[int]:
    """从已打开根句柄取身份；只关闭复制句柄，原 owner 保留原句柄。"""
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.DuplicateHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.DuplicateHandle.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    current, duplicate = kernel.GetCurrentProcess(), ctypes.c_void_p()
    if not kernel.DuplicateHandle(current, ctypes.c_void_p(handle), current,
            ctypes.byref(duplicate), 0, False, 2):
        raise WindowsHandleIoError('managed root identity unavailable')
    descriptor = None
    try:
        descriptor = msvcrt.open_osfhandle(int(duplicate.value), os.O_RDONLY)
        value = os.fstat(descriptor)
        return [value.st_dev, value.st_ino, value.st_mode]
    except OSError:
        raise WindowsHandleIoError('managed root identity unavailable') from None
    finally:
        if descriptor is None:
            kernel.CloseHandle(duplicate)
        else:
            os.close(descriptor)


def _directory(api: _Nt, handle: int) -> None:
    if api.is_reparse(handle) or not api.is_dir(handle): raise WindowsHandleIoError("expected a non-reparse directory")


def _component(value: object) -> str:
    if not isinstance(value, str) or not value or value in (".", "..") or any(token in value for token in ("/", "\\", ":", "\x00")):
        raise WindowsHandleIoError("invalid path component")
    return value


def _parts(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(_component(value) for value in values)
    if not result: raise WindowsHandleIoError("empty managed path")
    return result


def _optional_parts(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(_component(value) for value in values)


def _status(status: int, operation: str) -> None:
    if status != _OK: raise WindowsHandleIoError(f"{operation} failed: ntstatus=0x{status & 0xffffffff:08x}")


def _windows() -> None:
    if os.name != "nt": raise WindowsHandleIoError("Windows handle I/O requires Windows")
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise WindowsHandleIoError("Windows handle I/O currently supports only 64-bit Windows")
