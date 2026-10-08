"""Windows-only, handle-relative deletion for Plugin Hands workspaces.

This module deliberately has a very small surface: a caller fixes a trusted
directory root once and can delete exactly one direct child below it.  Every
descendant is subsequently opened relative to a HANDLE for its verified
parent.  It never recurses through a path and rejects reparse points both
before and after directory enumeration.

It is intentionally not a general-purpose file utility.  In particular,
there is no best-effort fallback to ``shutil`` or path-based removal.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Final


class WindowsHandleTreeError(RuntimeError):
    """A handle-relative workspace cleanup could not complete safely."""


_STATUS_SUCCESS: Final = 0
_STATUS_NO_MORE_FILES: Final = ctypes.c_long(0x80000006).value
_STATUS_OBJECT_NAME_NOT_FOUND: Final = ctypes.c_long(0xC0000034).value
_STATUS_OBJECT_PATH_NOT_FOUND: Final = ctypes.c_long(0xC000003A).value

_OBJ_CASE_INSENSITIVE: Final = 0x00000040
_OBJ_DONT_REPARSE: Final = 0x00001000
_FILE_READ_DATA: Final = 0x00000001  # FILE_LIST_DIRECTORY for directories.
_FILE_READ_ATTRIBUTES: Final = 0x00000080
_DELETE: Final = 0x00010000
_SYNCHRONIZE: Final = 0x00100000
_FILE_SHARE_READ: Final = 0x00000001
_FILE_SHARE_WRITE: Final = 0x00000002
_FILE_SHARE_DELETE: Final = 0x00000004
_FILE_OPEN: Final = 0x00000001
_FILE_DIRECTORY_FILE: Final = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT: Final = 0x00000020
_FILE_OPEN_REPARSE_POINT: Final = 0x00200000
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x00000400
_FILE_BASIC_INFORMATION: Final = 4
_FILE_STANDARD_INFORMATION: Final = 5
_FILE_DIRECTORY_INFORMATION: Final = 1
_FILE_DISPOSITION_INFORMATION_EX: Final = 64
_FILE_DISPOSITION_DELETE: Final = 0x00000001
_FILE_DISPOSITION_POSIX_SEMANTICS: Final = 0x00000002
_FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE: Final = 0x00000010
_DIRECTORY_BUFFER_BYTES: Final = 64 * 1024


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_wchar_p),
    ]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", ctypes.c_ulong),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_long), ("Information", ctypes.c_size_t)]


class _DISPOSITION_INFORMATION_EX(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_ulong)]


class _NativeNtApi:
    """Narrow ctypes wrapper for the NT calls used by the remover."""

    def __init__(self) -> None:
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtCreateFile.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong, ctypes.POINTER(_OBJECT_ATTRIBUTES),
            ctypes.POINTER(_IO_STATUS_BLOCK), ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
        ]
        ntdll.NtCreateFile.restype = ctypes.c_long
        ntdll.NtQueryDirectoryFile.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(_IO_STATUS_BLOCK), ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_ubyte, ctypes.c_void_p, ctypes.c_ubyte,
        ]
        ntdll.NtQueryDirectoryFile.restype = ctypes.c_long
        ntdll.NtQueryInformationFile.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_IO_STATUS_BLOCK), ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
        ]
        ntdll.NtQueryInformationFile.restype = ctypes.c_long
        ntdll.NtSetInformationFile.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_IO_STATUS_BLOCK), ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
        ]
        ntdll.NtSetInformationFile.restype = ctypes.c_long
        ntdll.NtClose.argtypes = [ctypes.c_void_p]
        ntdll.NtClose.restype = ctypes.c_long
        self._ntdll = ntdll

    def close(self, handle: int) -> None:
        if handle:
            self._ntdll.NtClose(ctypes.c_void_p(handle))

    def open_root(self, path: Path) -> int:
        native_path = "\\??\\" + str(path)
        return self._create(None, native_path, _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                            _FILE_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT)

    def open_child(self, parent: int, name: str) -> int:
        return self._create(parent, name, _DELETE | _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                            _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT)

    def _create(self, parent: int | None, name: str, desired_access: int, options: int) -> int:
        unicode_name = _UNICODE_STRING(len(name) * 2, (len(name) + 1) * 2, name)
        attributes = _OBJECT_ATTRIBUTES(
            ctypes.sizeof(_OBJECT_ATTRIBUTES), ctypes.c_void_p(parent or 0), ctypes.pointer(unicode_name),
            _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE, None, None,
        )
        handle = ctypes.c_void_p()
        io_status = _IO_STATUS_BLOCK()
        status = int(self._ntdll.NtCreateFile(
            ctypes.byref(handle), desired_access, ctypes.byref(attributes), ctypes.byref(io_status), None, 0,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE, _FILE_OPEN, options, None, 0,
        ))
        if status in (_STATUS_OBJECT_NAME_NOT_FOUND, _STATUS_OBJECT_PATH_NOT_FOUND):
            return 0
        _raise_ntstatus(status, "NtCreateFile")
        return int(handle.value or 0)

    def is_directory(self, handle: int) -> bool:
        buffer = (ctypes.c_ubyte * 32)()
        io_status = _IO_STATUS_BLOCK()
        status = int(self._ntdll.NtQueryInformationFile(
            ctypes.c_void_p(handle), ctypes.byref(io_status), ctypes.byref(buffer), len(buffer), _FILE_STANDARD_INFORMATION,
        ))
        _raise_ntstatus(status, "NtQueryInformationFile(FileStandardInformation)")
        # FILE_STANDARD_INFORMATION is packed through the two BOOLEAN fields:
        # AllocationSize (0), EndOfFile (8), NumberOfLinks (16),
        # DeletePending (20), Directory (21).
        return bool(buffer[21])

    def is_reparse_point(self, handle: int) -> bool:
        buffer = (ctypes.c_ubyte * 40)()
        io_status = _IO_STATUS_BLOCK()
        status = int(self._ntdll.NtQueryInformationFile(
            ctypes.c_void_p(handle), ctypes.byref(io_status), ctypes.byref(buffer), len(buffer), _FILE_BASIC_INFORMATION,
        ))
        _raise_ntstatus(status, "NtQueryInformationFile(FileBasicInformation)")
        attributes = int.from_bytes(bytes(buffer[32:36]), byteorder="little")
        return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)

    def list_names(self, directory: int) -> tuple[str, ...]:
        names: list[str] = []
        restart_scan = True
        while True:
            buffer = (ctypes.c_ubyte * _DIRECTORY_BUFFER_BYTES)()
            io_status = _IO_STATUS_BLOCK()
            status = int(self._ntdll.NtQueryDirectoryFile(
                ctypes.c_void_p(directory), None, None, None, ctypes.byref(io_status), ctypes.byref(buffer), len(buffer),
                _FILE_DIRECTORY_INFORMATION, 0, None, int(restart_scan),
            ))
            restart_scan = False
            if status == _STATUS_NO_MORE_FILES:
                break
            _raise_ntstatus(status, "NtQueryDirectoryFile")
            length = int(io_status.Information)
            if length <= 0 or length > len(buffer):
                raise WindowsHandleTreeError("NtQueryDirectoryFile returned an invalid buffer")
            offset = 0
            while True:
                if offset + 64 > length:
                    raise WindowsHandleTreeError("NtQueryDirectoryFile returned a truncated entry")
                next_offset = int.from_bytes(bytes(buffer[offset:offset + 4]), byteorder="little")
                name_length = int.from_bytes(bytes(buffer[offset + 60:offset + 64]), byteorder="little")
                if name_length % 2 or offset + 64 + name_length > length:
                    raise WindowsHandleTreeError("NtQueryDirectoryFile returned an invalid name")
                name = ctypes.string_at(ctypes.addressof(buffer) + offset + 64, name_length).decode("utf-16-le")
                if name not in (".", ".."):
                    _validate_component(name)
                    names.append(name)
                if next_offset == 0:
                    break
                if next_offset < 64 or offset + next_offset >= length:
                    raise WindowsHandleTreeError("NtQueryDirectoryFile returned an invalid offset")
                offset += next_offset
        if len(names) != len(set(names)):
            raise WindowsHandleTreeError("directory changed during handle enumeration")
        return tuple(names)

    def mark_delete(self, handle: int) -> None:
        disposition = _DISPOSITION_INFORMATION_EX(
            _FILE_DISPOSITION_DELETE
            | _FILE_DISPOSITION_POSIX_SEMANTICS
            | _FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE
        )
        io_status = _IO_STATUS_BLOCK()
        status = int(self._ntdll.NtSetInformationFile(
            ctypes.c_void_p(handle), ctypes.byref(io_status), ctypes.byref(disposition), ctypes.sizeof(disposition),
            _FILE_DISPOSITION_INFORMATION_EX,
        ))
        _raise_ntstatus(status, "NtSetInformationFile(FileDispositionInformationEx)")


class WindowsHandleTreeRemover:
    """Delete one trusted-root child using only verified relative handles."""

    def remove(self, root: Path, target_name: str) -> bool:
        """Remove ``target_name`` below ``root`` and return whether it existed.

        The absolute root path is used exactly once to acquire a HANDLE.  No
        target or descendant is later addressed by path.
        """

        _require_windows()
        if not isinstance(root, Path) or not root.is_absolute():
            raise WindowsHandleTreeError("workspace cleanup root is invalid")
        _validate_component(target_name)
        api = _NativeNtApi()
        root_handle = api.open_root(root)
        if not root_handle:
            raise WindowsHandleTreeError("workspace cleanup root is unavailable")
        try:
            self._assert_regular_directory(api, root_handle)
            self._after_root_open(root_handle)
            target_handle = api.open_child(root_handle, target_name)
            if not target_handle:
                return False
            try:
                self._remove_open(api, target_handle)
            finally:
                api.close(target_handle)
        finally:
            api.close(root_handle)
        return True

    def _after_root_open(self, _root_handle: int) -> None:
        """Private test seam for a configured-root path replacement race."""

    def _before_child_open(self, _parent_handle: int, _name: str) -> None:
        """Private test seam for an enumerate-to-open replacement race."""

    def _remove_open(self, api: _NativeNtApi, handle: int) -> None:
        if api.is_reparse_point(handle):
            raise WindowsHandleTreeError("workspace cleanup encountered a reparse point")
        if api.is_directory(handle):
            for name in api.list_names(handle):
                self._before_child_open(handle, name)
                child_handle = api.open_child(handle, name)
                if not child_handle:
                    raise WindowsHandleTreeError("workspace cleanup directory changed during enumeration")
                try:
                    self._remove_open(api, child_handle)
                finally:
                    api.close(child_handle)
        api.mark_delete(handle)

    @staticmethod
    def _assert_regular_directory(api: _NativeNtApi, handle: int) -> None:
        if api.is_reparse_point(handle) or not api.is_directory(handle):
            raise WindowsHandleTreeError("workspace cleanup root is unsafe")


def _validate_component(name: object) -> None:
    if not isinstance(name, str) or not name or name in (".", ".."):
        raise WindowsHandleTreeError("workspace cleanup target is not one component")
    if "\\" in name or "/" in name or ":" in name or "\x00" in name:
        raise WindowsHandleTreeError("workspace cleanup target is not one component")


def _raise_ntstatus(status: int, operation: str) -> None:
    if status != _STATUS_SUCCESS:
        raise WindowsHandleTreeError(f"{operation} failed: ntstatus=0x{status & 0xFFFFFFFF:08x}")


def _require_windows() -> None:
    if os.name != "nt":
        raise WindowsHandleTreeError("Windows handle-relative cleanup requires Windows")
