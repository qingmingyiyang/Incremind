"""Strict local composition for the packaged Codex Hook baseline.

The TOML file is declarative policy only.  It cannot name a shell command,
file path, URL, Python module, or executable.  Code-owned handlers must be
registered below by their immutable ``(handler_id, handler_revision)`` pair.
The packaged baseline deliberately has no handlers, so its runner cannot
execute anything.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
import tomllib
from uuid import NAMESPACE_URL, uuid5

from core.ai_kernel.codex_hook_parity import CODEX_HOOK_PARITY_REVISION, HookEvent, HookRun
from core.ai_kernel.codex_hook_runtime import (
    CodexHookHost,
    HookHandlerManifest,
    HookPolicyCatalog,
    HookPolicySnapshot,
    HookRuntimeError,
    RevisionPinnedHookRunner,
)


class CodexHookConfigurationError(ValueError):
    """Raised when a local Hook policy cannot be safely composed."""


class NoopHookHandlerRunner:
    """Non-executable runner for an intentionally empty packaged policy."""

    def supports(self, _manifest: HookHandlerManifest) -> bool:
        return False

    def __call__(self, _manifest: HookHandlerManifest, _payload: Mapping[str, object]) -> HookRun:
        raise CodexHookConfigurationError("the empty Codex Hook policy has no executable handlers")


# This registry is intentionally code-owned.  TOML can select only a key that
# is present here; it never supplies a process, module, URL, path, or callable.
_BUILTIN_HANDLERS: Mapping[
    tuple[str, str], Callable[[HookHandlerManifest, Mapping[str, object]], HookRun]
] = {}

_ROOT_FIELDS = frozenset({"hooks"})
_HOOK_FIELDS = frozenset({
    "enabled",
    "policy_revision",
    "upstream_revision",
    "manifest_ref",
    "manifest_revision",
    "local_hard_guard_revision",
    "handlers",
})
_HANDLER_FIELDS = frozenset({
    "handler_id",
    "handler_revision",
    "event",
    "order",
    "sync",
    "enabled",
    "timeout_ms",
})


def build_codex_hook_host(
    config_path: Path,
    *,
    additional_handlers: Sequence[HookHandlerManifest] = (),
    additional_runner: object | None = None,
) -> CodexHookHost | None:
    """Build the configured local Hook Host, or ``None`` when explicitly off.

    A missing config is an explicit compatibility state: callers retain the
    legacy Boundary path.  An existing file is parsed strictly, including when
    it says ``enabled = false``, so configuration typos never silently become
    a different policy on the next enablement.
    """

    if not config_path.exists():
        return None
    if not config_path.is_file():
        raise CodexHookConfigurationError("Codex Hook configuration must be a file")
    try:
        value = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CodexHookConfigurationError("Codex Hook configuration is unreadable") from error
    enabled, snapshot = _parse_policy(value)
    if not enabled:
        return None
    if additional_handlers:
        identities = {(item.event, item.hook_id) for item in snapshot.handlers}
        if any((item.event, item.hook_id) in identities for item in additional_handlers):
            raise CodexHookConfigurationError("Codex Hook handler identity conflicts")
        suffix = uuid5(NAMESPACE_URL, "|".join(
            f"{item.event.value}:{item.hook_id}:{item.revision}" for item in additional_handlers
        )).hex
        snapshot = HookPolicySnapshot(
            revision=f"{snapshot.revision}.plugin.{suffix}",
            handlers=snapshot.handlers + tuple(additional_handlers),
            codex_revision=snapshot.codex_revision,
            manifest_ref=snapshot.manifest_ref,
            manifest_revision=f"{snapshot.manifest_revision}.plugin.{suffix}",
            local_hard_guard_revision=snapshot.local_hard_guard_revision,
        )
    runner = _runner_for(snapshot, additional_runner=additional_runner)
    try:
        return CodexHookHost(catalog=HookPolicyCatalog(snapshot), runner=runner)
    except HookRuntimeError as error:
        raise CodexHookConfigurationError("Codex Hook configuration is invalid") from error


def _parse_policy(value: object) -> tuple[bool, HookPolicySnapshot]:
    root = _mapping(value, "Codex Hook configuration")
    _exact_fields(root, _ROOT_FIELDS, "Codex Hook configuration")
    hooks = _mapping(root.get("hooks"), "Codex Hook configuration hooks")
    _exact_fields(hooks, _HOOK_FIELDS, "Codex Hook configuration hooks")
    enabled = hooks.get("enabled")
    if not isinstance(enabled, bool):
        raise CodexHookConfigurationError("Codex Hook configuration enabled must be boolean")
    upstream_revision = hooks.get("upstream_revision")
    if upstream_revision != CODEX_HOOK_PARITY_REVISION:
        raise CodexHookConfigurationError("Codex Hook upstream revision is unsupported")
    handlers = _parse_handlers(hooks.get("handlers"))
    try:
        snapshot = HookPolicySnapshot(
            revision=_text(hooks.get("policy_revision"), "policy_revision"),
            handlers=handlers,
            codex_revision=CODEX_HOOK_PARITY_REVISION,
            manifest_ref=_text(hooks.get("manifest_ref"), "manifest_ref"),
            manifest_revision=_text(hooks.get("manifest_revision"), "manifest_revision"),
            local_hard_guard_revision=_text(
                hooks.get("local_hard_guard_revision"), "local_hard_guard_revision"
            ),
        )
    except HookRuntimeError as error:
        raise CodexHookConfigurationError("Codex Hook policy fields are invalid") from error
    return enabled, snapshot


def _parse_handlers(value: object) -> tuple[HookHandlerManifest, ...]:
    if not isinstance(value, list):
        raise CodexHookConfigurationError("Codex Hook handlers must be an array")
    handlers: list[HookHandlerManifest] = []
    for item in value:
        handler = _mapping(item, "Codex Hook handler")
        _exact_fields(handler, _HANDLER_FIELDS, "Codex Hook handler")
        try:
            event = HookEvent(_text(handler.get("event"), "handler event"))
        except ValueError as error:
            raise CodexHookConfigurationError("Codex Hook handler event is unsupported") from error
        enabled = handler.get("enabled")
        synchronous = handler.get("sync")
        order = handler.get("order")
        timeout_ms = handler.get("timeout_ms")
        if not isinstance(enabled, bool) or not isinstance(synchronous, bool):
            raise CodexHookConfigurationError("Codex Hook handler enabled and sync must be boolean")
        if not isinstance(order, int) or isinstance(order, bool):
            raise CodexHookConfigurationError("Codex Hook handler order must be an integer")
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
            raise CodexHookConfigurationError("Codex Hook handler timeout_ms must be an integer")
        try:
            handlers.append(HookHandlerManifest(
                hook_id=_text(handler.get("handler_id"), "handler_id"),
                revision=_text(handler.get("handler_revision"), "handler_revision"),
                event=event,
                config_order=order,
                synchronous=synchronous,
                enabled=enabled,
                timeout_ms=timeout_ms,
            ))
        except HookRuntimeError as error:
            raise CodexHookConfigurationError("Codex Hook handler fields are invalid") from error
    return tuple(handlers)


class _CompositeHookRunner:
    def __init__(self, builtin, additional) -> None:
        self._builtin = builtin
        self._additional = additional

    def supports(self, manifest: HookHandlerManifest) -> bool:
        return bool(
            getattr(self._builtin, "supports", lambda _item: False)(manifest)
            or getattr(self._additional, "supports", lambda _item: False)(manifest)
        )

    def __call__(self, manifest: HookHandlerManifest, payload: Mapping[str, object]) -> HookRun:
        if getattr(self._builtin, "supports", lambda _item: False)(manifest):
            return self._builtin(manifest, payload)
        if getattr(self._additional, "supports", lambda _item: False)(manifest):
            return self._additional(manifest, payload)
        raise CodexHookConfigurationError("frozen Hook handler revision is unavailable")


def _runner_for(snapshot: HookPolicySnapshot, *, additional_runner: object | None = None):
    if not snapshot.handlers:
        empty = NoopHookHandlerRunner()
        return _CompositeHookRunner(empty, additional_runner) if additional_runner is not None else empty
    missing = [
        (handler.hook_id, handler.revision)
        for handler in snapshot.handlers
        if (handler.hook_id, handler.revision) not in _BUILTIN_HANDLERS
        and not bool(getattr(additional_runner, "supports", lambda _item: False)(handler))
    ]
    if missing:
        raise CodexHookConfigurationError("Codex Hook handler revision is not registered locally")
    builtin_handlers = {
        key: _BUILTIN_HANDLERS[key]
        for key in {(handler.hook_id, handler.revision) for handler in snapshot.handlers}
        if key in _BUILTIN_HANDLERS
    }
    builtin = RevisionPinnedHookRunner(builtin_handlers) if builtin_handlers else NoopHookHandlerRunner()
    return _CompositeHookRunner(builtin, additional_runner)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CodexHookConfigurationError(f"{label} must be an object")
    return value


def _exact_fields(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise CodexHookConfigurationError(f"{label} fields are invalid")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodexHookConfigurationError(f"Codex Hook {label} must be non-empty text")
    return value.strip()
