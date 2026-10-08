"""Fail-closed AppContainer ACLs for one Plugin Hands lease workspace.

This is deliberately an OS-boundary primitive.  It augments the existing DACL
instead of replacing it, so the host, SYSTEM and local administrators retain
their ordinary recovery access.  The per-profile AppContainer SID receives
only the three lease-local grants defined below; callers must not turn this
into a package-provided policy surface.
"""
from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final


_DACL_SECURITY_INFORMATION: Final = 0x00000004
_SE_FILE_OBJECT: Final = 1
_GRANT_ACCESS: Final = 1
_TRUSTEE_IS_SID: Final = 0
_TRUSTEE_IS_UNKNOWN: Final = 0
_SUB_CONTAINERS_AND_OBJECTS_INHERIT: Final = 0x00000003
_FILE_GENERIC_READ: Final = 0x00120089
_FILE_GENERIC_WRITE: Final = 0x00120116
_FILE_GENERIC_EXECUTE: Final = 0x001200A0
_DELETE: Final = 0x00010000
_REPARSE_POINT: Final = 0x0400


class PluginHandsAclError(RuntimeError):
    """A lease ACL could not be established and must not be bypassed."""


@dataclass(frozen=True, slots=True)
class PluginHandsWorkspaceAcl:
    """Host-only record of the exact directories covered by the grants."""

    workspace_root: Path
    code_dir: Path
    input_dir: Path | None
    output_dir: Path | None
    tmp_dir: Path | None
    appcontainer_sid: int


class _TRUSTEE_W(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_ulong),
        ("TrusteeForm", ctypes.c_ulong),
        ("TrusteeType", ctypes.c_ulong),
        ("ptstrName", ctypes.c_void_p),
    ]


class _EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", ctypes.c_ulong),
        ("grfAccessMode", ctypes.c_ulong),
        ("grfInheritance", ctypes.c_ulong),
        ("Trustee", _TRUSTEE_W),
    ]


def grant_appcontainer_workspace_acl(
    workspace_root: Path, appcontainer_sid: int, *, allowed_resources: tuple[str, ...] = (),
) -> PluginHandsWorkspaceAcl:
    """Grant a profile SID the minimum lease-workspace access, or raise.

    The root is traverse-only.  ``input`` is recursively read/execute only if
    leased; ``output`` and host-created ``tmp`` are recursively modifiable
    only if the writable output resource is leased.  The pre-existing DACL is
    supplied to ``SetEntriesInAclW``, never discarded.  There is intentionally
    no best-effort mode or non-AppContainer fallback.
    """

    _require_windows()
    root = _safe_directory(workspace_root, "workspace")
    if not isinstance(appcontainer_sid, int) or isinstance(appcontainer_sid, bool) or appcontainer_sid <= 0:
        raise PluginHandsAclError("Plugin Hands AppContainer SID is invalid")
    resources = _resources(allowed_resources)
    code_dir = _safe_directory(root / "code", "workspace code")
    input_dir = _resource_directory(root, "input", "workspace_input" in resources)
    output_dir = _resource_directory(root, "output", "workspace_output" in resources)
    tmp_dir = _resource_directory(root, "tmp", "workspace_output" in resources, create=True)

    root_access = _FILE_GENERIC_EXECUTE
    read_access = _FILE_GENERIC_READ | _FILE_GENERIC_EXECUTE
    modify_access = _FILE_GENERIC_READ | _FILE_GENERIC_WRITE | _FILE_GENERIC_EXECUTE | _DELETE
    _append_dacl_ace(root, _ace(appcontainer_sid, root_access, 0))
    _append_dacl_ace(code_dir, _ace(appcontainer_sid, read_access, _SUB_CONTAINERS_AND_OBJECTS_INHERIT))
    if input_dir is not None:
        _append_dacl_ace(input_dir, _ace(appcontainer_sid, read_access, _SUB_CONTAINERS_AND_OBJECTS_INHERIT))
    if output_dir is not None and tmp_dir is not None:
        _append_dacl_ace(output_dir, _ace(appcontainer_sid, modify_access, _SUB_CONTAINERS_AND_OBJECTS_INHERIT))
        _append_dacl_ace(tmp_dir, _ace(appcontainer_sid, modify_access, _SUB_CONTAINERS_AND_OBJECTS_INHERIT))
    return PluginHandsWorkspaceAcl(root, code_dir, input_dir, output_dir, tmp_dir, appcontainer_sid)


def _resources(value: object) -> frozenset[str]:
    if not isinstance(value, tuple) or len(value) != len(set(value)) or set(value) - {"workspace_input", "workspace_output"}:
        raise PluginHandsAclError("Plugin Hands workspace resources are invalid")
    return frozenset(value)


def _resource_directory(root: Path, name: str, enabled: bool, *, create: bool = False) -> Path | None:
    path = root / name
    exists = path.exists() or path.is_symlink()
    if not enabled:
        if exists:
            raise PluginHandsAclError("Plugin Hands workspace resources drifted")
        return None
    if create and not exists:
        try:
            path.mkdir()
        except OSError as error:
            raise PluginHandsAclError("Plugin Hands workspace tmp cannot be created") from error
    return _safe_directory(path, f"workspace {name}")


def _ace(sid: int, permissions: int, inheritance: int) -> _EXPLICIT_ACCESS_W:
    return _EXPLICIT_ACCESS_W(
        permissions,
        _GRANT_ACCESS,
        inheritance,
        _TRUSTEE_W(None, 0, _TRUSTEE_IS_SID, _TRUSTEE_IS_UNKNOWN, ctypes.c_void_p(sid)),
    )


def _append_dacl_ace(path: Path, entry: _EXPLICIT_ACCESS_W) -> None:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _configure_advapi32(advapi32)
    security_descriptor = ctypes.c_void_p()
    old_dacl = ctypes.c_void_p()
    result = int(advapi32.GetNamedSecurityInfoW(str(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION, None, None, ctypes.byref(old_dacl), None, ctypes.byref(security_descriptor)))
    if result:
        raise PluginHandsAclError(f"GetNamedSecurityInfoW failed: winerror={result}")
    new_dacl = ctypes.c_void_p()
    try:
        result = int(advapi32.SetEntriesInAclW(1, ctypes.byref(entry), old_dacl, ctypes.byref(new_dacl)))
        if result:
            raise PluginHandsAclError(f"SetEntriesInAclW failed: winerror={result}")
        result = int(advapi32.SetNamedSecurityInfoW(str(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION, None, None, new_dacl, None))
        if result:
            raise PluginHandsAclError(f"SetNamedSecurityInfoW failed: winerror={result}")
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if new_dacl.value:
            kernel32.LocalFree(new_dacl)
        if security_descriptor.value:
            kernel32.LocalFree(security_descriptor)


def _safe_directory(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise PluginHandsAclError(f"Plugin Hands {label} is invalid")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PluginHandsAclError(f"Plugin Hands {label} is unavailable") from error
    if resolved != path or not resolved.is_dir() or path.is_symlink() or _is_reparse(path):
        raise PluginHandsAclError(f"Plugin Hands {label} is unsafe")
    return resolved


def _is_reparse(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & _REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def _require_windows() -> None:
    if os.name != "nt":
        raise PluginHandsAclError("Plugin Hands AppContainer ACL requires Windows")


def _configure_advapi32(advapi32: ctypes.WinDLL) -> None:
    advapi32.GetNamedSecurityInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetNamedSecurityInfoW.restype = ctypes.c_ulong
    advapi32.SetEntriesInAclW.argtypes = [ctypes.c_ulong, ctypes.POINTER(_EXPLICIT_ACCESS_W), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.SetEntriesInAclW.restype = ctypes.c_ulong
    advapi32.SetNamedSecurityInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    advapi32.SetNamedSecurityInfoW.restype = ctypes.c_ulong
