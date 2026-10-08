"""Host-owned temporary workspace lifecycle for Plugin Hands.

Plugin code receives only the already-created input/output locations through the
process protocol.  It never selects, deletes, or retains a cleanup target.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .contracts import PluginHandsLease, PluginHandsOutcome, PluginHandsWorkspaceError
from .windows_handle_tree import WindowsHandleTreeError, WindowsHandleTreeRemover


_REPARSE_POINT = 0x0400
_MAX_CODE_FILES = 128
_MAX_CODE_BYTES = 4 * 1024 * 1024


def _is_reparse(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & _REPARSE_POINT)


def _contained(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _assert_safe_path(root: Path, path: Path) -> None:
    """Check every existing component below root without following reparse points."""

    if not _contained(root, path):
        raise PluginHandsWorkspaceError("Plugin Hands workspace escapes configured root")
    current = root
    if current.is_symlink() or _is_reparse(current):
        raise PluginHandsWorkspaceError("Plugin Hands workspace root is a link")
    for component in path.relative_to(root).parts:
        current = current / component
        if not current.exists() and not current.is_symlink():
            break
        if current.is_symlink() or _is_reparse(current):
            raise PluginHandsWorkspaceError("Plugin Hands workspace contains a link")


def _assert_safe_tree(root: Path, directory: Path) -> None:
    _assert_safe_path(root, directory)
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    child = Path(entry.path)
                    if child.is_symlink() or _is_reparse(child):
                        raise PluginHandsWorkspaceError("Plugin Hands workspace contains a link")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(child)
        except PluginHandsWorkspaceError:
            raise
        except OSError as error:
            raise PluginHandsWorkspaceError("Plugin Hands workspace cannot be inspected") from error


@dataclass(frozen=True, slots=True)
class PluginHandsWorkspace:
    """Internal workspace identity. Paths are host-only implementation details."""

    lease: PluginHandsLease
    root: Path = field(repr=False)
    code_dir: Path = field(repr=False)
    input_dir: Path | None = field(repr=False)
    output_dir: Path | None = field(repr=False)

    @property
    def host_ref(self) -> str:
        """Opaque retained-workspace reference safe to place in a host result."""
        return f"plugin-hands-workspace:{self.lease.lease_id}:{self.lease.generation}"


class PluginHandsWorkspaceManager:
    """Creates and disposes lease-scoped workspaces below one configured root."""

    def __init__(self, configured_root: Path) -> None:
        if not isinstance(configured_root, Path) or not configured_root.is_absolute():
            raise PluginHandsWorkspaceError("Plugin Hands workspace root is invalid")
        if configured_root.is_symlink() or _is_reparse(configured_root):
            raise PluginHandsWorkspaceError("Plugin Hands workspace root is a link")
        try:
            root = configured_root.resolve(strict=True)
        except OSError as error:
            raise PluginHandsWorkspaceError("Plugin Hands workspace root is unavailable") from error
        if root != configured_root:
            raise PluginHandsWorkspaceError("Plugin Hands workspace root identity changed")
        if not root.is_dir() or root.is_symlink() or _is_reparse(root):
            raise PluginHandsWorkspaceError("Plugin Hands workspace root is invalid")
        self._root = root
        self._active: dict[str, PluginHandsWorkspace] = {}

    @property
    def configured_root(self) -> Path:
        return self._root

    def create(self, lease: PluginHandsLease) -> PluginHandsWorkspace:
        if not isinstance(lease, PluginHandsLease):
            raise PluginHandsWorkspaceError("Plugin Hands workspace lease is invalid")
        if lease.lease_id in self._active:
            raise PluginHandsWorkspaceError("Plugin Hands workspace lease is already active")
        _assert_safe_path(self._root, self._root)
        lease_root = self._root / lease.lease_id
        if lease_root.exists() or lease_root.is_symlink():
            raise PluginHandsWorkspaceError("Plugin Hands workspace lease already exists")
        try:
            lease_root.mkdir()
            code_dir = lease_root / "code"
            code_dir.mkdir()
            input_dir = lease_root / "input" if "workspace_input" in lease.allowed_resources else None
            output_dir = lease_root / "output" if "workspace_output" in lease.allowed_resources else None
            if input_dir is not None:
                input_dir.mkdir()
                _assert_safe_path(self._root, input_dir)
            if output_dir is not None:
                output_dir.mkdir()
                _assert_safe_path(self._root, output_dir)
        except PluginHandsWorkspaceError:
            raise
        except OSError as error:
            raise PluginHandsWorkspaceError("Plugin Hands workspace cannot be created") from error
        workspace = PluginHandsWorkspace(lease, lease_root, code_dir, input_dir, output_dir)
        self._active[lease.lease_id] = workspace
        return workspace

    def dispose(self, workspace: PluginHandsWorkspace, outcome: PluginHandsOutcome) -> str | None:
        """Remove known-effect workspaces; retain unknown effects under an opaque ref."""

        if not isinstance(workspace, PluginHandsWorkspace) or not isinstance(outcome, PluginHandsOutcome):
            raise PluginHandsWorkspaceError("Plugin Hands workspace disposition is invalid")
        active = self._active.get(workspace.lease.lease_id)
        if active is not workspace:
            raise PluginHandsWorkspaceError("Plugin Hands workspace is not active")
        if outcome.lease_id != workspace.lease.lease_id or outcome.invocation_id != workspace.lease.invocation_id:
            raise PluginHandsWorkspaceError("Plugin Hands outcome does not match workspace")
        if outcome.status == "unknown":
            self._active.pop(workspace.lease.lease_id, None)
            return workspace.host_ref
        try:
            WindowsHandleTreeRemover().remove(self._root, workspace.lease.lease_id)
        except WindowsHandleTreeError as error:
            raise PluginHandsWorkspaceError("Plugin Hands workspace cleanup failed") from error
        self._active.pop(workspace.lease.lease_id, None)
        return None

    def cleanup_pre_fence(self, lease: PluginHandsLease) -> None:
        """Remove a restart orphan which was created before its spawn fence.

        The durable lifecycle coordinator is the only caller.  It never uses
        this operation once a process could have started, so this method cannot
        accidentally destroy an unknown-effect workspace.
        """
        if not isinstance(lease, PluginHandsLease):
            raise PluginHandsWorkspaceError("Plugin Hands workspace lease is invalid")
        if lease.lease_id in self._active:
            raise PluginHandsWorkspaceError("Plugin Hands workspace is active")
        self._remove_retained(lease)

    def cleanup_pre_fence_identity(self, lease_id: str) -> None:
        """Restart-only pre-fence cleanup from a non-executable lease identity."""
        self._remove_retained_identity(lease_id)

    def cleanup_known(self, lease: PluginHandsLease, outcome: PluginHandsOutcome) -> None:
        """Retry cleanup after a terminal outcome was durably recorded."""
        if not isinstance(lease, PluginHandsLease) or not isinstance(outcome, PluginHandsOutcome):
            raise PluginHandsWorkspaceError("Plugin Hands workspace disposition is invalid")
        if outcome.status == "unknown" or outcome.lease_id != lease.lease_id or outcome.invocation_id != lease.invocation_id:
            raise PluginHandsWorkspaceError("Plugin Hands outcome does not match workspace")
        # A coordinator may retry immediately after a failed disposition. Its
        # known outcome is already durable, so detaching the local cache cannot
        # permit execution and is safe to remove.
        self._active.pop(lease.lease_id, None)
        self._remove_retained(lease)

    def cleanup_known_identity(self, lease_id: str, invocation_id: str) -> None:
        """Remove known-effect workspace without reconstructing an invocation."""
        if not isinstance(lease_id, str) or not isinstance(invocation_id, str) or not lease_id or not invocation_id:
            raise PluginHandsWorkspaceError("Plugin Hands workspace identity is invalid")
        self._active.pop(lease_id, None)
        self._remove_retained_identity(lease_id)

    def stage_code(self, workspace: PluginHandsWorkspace, payload_files: tuple[tuple[str, bytes], ...]) -> None:
        """Copy reviewed payload bytes into the lease-local immutable code tree.

        The code directory is created by the host with the workspace.  This
        method never reads or grants the managed artifact tree; callers provide
        the already revalidated bytes from ``ManagedHandsArtifact``.
        """

        self._require_active_workspace(workspace)
        files = _payload_files(payload_files)
        _assert_workspace_code_tree(workspace, allow_empty=True)
        _ensure_exact_code_tree(workspace.code_dir, files, create=True)

    def verify_code(self, workspace: PluginHandsWorkspace, payload_files: tuple[tuple[str, bytes], ...]) -> None:
        """Fail closed if staged code differs from the reviewed payload bytes."""

        self._require_active_workspace(workspace)
        _ensure_exact_code_tree(workspace.code_dir, _payload_files(payload_files), create=False)

    def _require_active_workspace(self, workspace: PluginHandsWorkspace) -> None:
        if not isinstance(workspace, PluginHandsWorkspace) or self._active.get(workspace.lease.lease_id) is not workspace:
            raise PluginHandsWorkspaceError("Plugin Hands workspace is not active")

    def _remove_retained(self, lease: PluginHandsLease) -> None:
        self._remove_retained_identity(lease.lease_id)

    def _remove_retained_identity(self, lease_id: str) -> None:
        if not isinstance(lease_id, str) or not lease_id:
            raise PluginHandsWorkspaceError("Plugin Hands workspace identity is invalid")
        try:
            WindowsHandleTreeRemover().remove(self._root, lease_id)
        except WindowsHandleTreeError as error:
            raise PluginHandsWorkspaceError("Plugin Hands workspace cleanup failed") from error


def assert_workspace_resource_shape(workspace: PluginHandsWorkspace) -> None:
    """Validate a workspace against its frozen lease before process creation.

    A caller cannot smuggle a richer workspace to the runner by constructing a
    dataclass directly or by placing unleased ``input``, ``output`` or ``tmp``
    directories below the host root.  ``tmp`` is host runtime scratch and is
    permitted only together with the explicitly writable output resource.
    """

    if not isinstance(workspace, PluginHandsWorkspace):
        raise PluginHandsWorkspaceError("Plugin Hands workspace is invalid")
    root = workspace.root
    if not isinstance(root, Path) or not root.is_absolute() or not root.is_dir() or root.is_symlink() or _is_reparse(root):
        raise PluginHandsWorkspaceError("Plugin Hands workspace is invalid")
    allowed = set(workspace.lease.allowed_resources)
    expected = {
        "input": "workspace_input" in allowed,
        "output": "workspace_output" in allowed,
        "tmp": "workspace_output" in allowed,
    }
    declared = {"input": workspace.input_dir, "output": workspace.output_dir}
    for name in ("input", "output"):
        path = root / name
        present = path.exists() or path.is_symlink()
        if present != expected[name] or (declared[name] is None) == expected[name]:
            raise PluginHandsWorkspaceError("Plugin Hands workspace resources drifted")
        if expected[name]:
            if declared[name] != path or not path.is_dir():
                raise PluginHandsWorkspaceError("Plugin Hands workspace resources drifted")
            _assert_safe_path(root, path)
    tmp_dir = root / "tmp"
    tmp_present = tmp_dir.exists() or tmp_dir.is_symlink()
    if tmp_present and not expected["tmp"]:
        raise PluginHandsWorkspaceError("Plugin Hands workspace resources drifted")
    if tmp_present:
        if not tmp_dir.is_dir():
            raise PluginHandsWorkspaceError("Plugin Hands workspace resources drifted")
        _assert_safe_path(root, tmp_dir)
    _assert_workspace_code_tree(workspace, allow_empty=True)


def _assert_workspace_code_tree(workspace: PluginHandsWorkspace, *, allow_empty: bool) -> None:
    root, code_dir = workspace.root, workspace.code_dir
    if not isinstance(code_dir, Path) or code_dir != root / "code" or not code_dir.is_dir():
        raise PluginHandsWorkspaceError("Plugin Hands staged code is unavailable")
    _assert_safe_path(root, code_dir)
    files = 0
    for path in sorted(code_dir.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(code_dir).as_posix()
        if path.is_symlink() or _is_reparse(path):
            raise PluginHandsWorkspaceError("Plugin Hands staged code contains a link")
        if path.is_dir():
            if relative != "payload" and not relative.startswith("payload/"):
                raise PluginHandsWorkspaceError("Plugin Hands staged code is invalid")
            continue
        if not path.is_file() or not relative.startswith("payload/"):
            raise PluginHandsWorkspaceError("Plugin Hands staged code is invalid")
        files += 1
    if not allow_empty and files == 0:
        raise PluginHandsWorkspaceError("Plugin Hands staged code is missing")


def _payload_files(value: object) -> tuple[tuple[str, bytes], ...]:
    if not isinstance(value, tuple) or not value or len(value) > _MAX_CODE_FILES:
        raise PluginHandsWorkspaceError("Plugin Hands payload files are invalid")
    normalized: list[tuple[str, bytes]] = []
    total = 0
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise PluginHandsWorkspaceError("Plugin Hands payload files are invalid")
        path, content = item
        if not isinstance(path, str) or not isinstance(content, bytes) or not path.startswith("payload/"):
            raise PluginHandsWorkspaceError("Plugin Hands payload files are invalid")
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or "." in pure.parts or len(pure.parts) < 2 or path in seen:
            raise PluginHandsWorkspaceError("Plugin Hands payload files are invalid")
        seen.add(path)
        total += len(content)
        if total > _MAX_CODE_BYTES:
            raise PluginHandsWorkspaceError("Plugin Hands payload files are invalid")
        normalized.append((path, content))
    if tuple(path for path, _ in normalized) != tuple(sorted(path for path, _ in normalized)):
        raise PluginHandsWorkspaceError("Plugin Hands payload files are invalid")
    return tuple(normalized)


def _ensure_exact_code_tree(code_dir: Path, files: tuple[tuple[str, bytes], ...], *, create: bool) -> None:
    _assert_safe_path(code_dir.parent, code_dir)
    expected = {path: content for path, content in files}
    observed: set[str] = set()
    directories = {
        PurePosixPath(path).parent.as_posix()
        for path in expected
        if PurePosixPath(path).parent.as_posix() != "."
    }
    observed_directories: set[str] = set()
    for path in sorted(code_dir.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(code_dir).as_posix()
        if path.is_symlink() or _is_reparse(path):
            raise PluginHandsWorkspaceError("Plugin Hands staged code contains a link")
        if path.is_dir():
            observed_directories.add(relative)
            continue
        if not path.is_file() or relative not in expected:
            raise PluginHandsWorkspaceError("Plugin Hands staged code contains an unexpected entry")
        if path.read_bytes() != expected[relative]:
            raise PluginHandsWorkspaceError("Plugin Hands staged code bytes drifted")
        observed.add(relative)
    if observed:
        if observed != set(expected) or observed_directories != directories:
            raise PluginHandsWorkspaceError("Plugin Hands staged code is incomplete")
        return
    if observed_directories:
        raise PluginHandsWorkspaceError("Plugin Hands staged code is incomplete")
    if not create:
        raise PluginHandsWorkspaceError("Plugin Hands staged code is missing")
    for relative, content in files:
        destination = code_dir.joinpath(*PurePosixPath(relative).parts)
        if not _contained(code_dir, destination):
            raise PluginHandsWorkspaceError("Plugin Hands staged code escapes workspace")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as error:
            raise PluginHandsWorkspaceError("Plugin Hands staged code changed while staging") from error
        except OSError as error:
            raise PluginHandsWorkspaceError("Plugin Hands staged code cannot be created") from error
    _ensure_exact_code_tree(code_dir, files, create=False)
