from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.codex_hook_composition import (
    CodexHookConfigurationError,
    NoopHookHandlerRunner,
    build_codex_hook_host,
)
from core.ai_kernel.codex_hook_parity import CODEX_HOOK_PARITY_REVISION, HookEvent
from core.ai_kernel.codex_hook_runtime import HookHandlerManifest


ROOT = Path(__file__).resolve().parents[4]


def test_packaged_enabled_empty_policy_builds_non_executable_host() -> None:
    host = build_codex_hook_host(ROOT / "config" / "codex-hooks.toml")

    assert host is not None
    snapshot = host.current_snapshot()
    assert snapshot.revision == "builtin-empty-v1"
    assert snapshot.codex_revision == CODEX_HOOK_PARITY_REVISION
    assert snapshot.handlers == ()
    receipt = host.invoke(HookEvent.PRE_TOOL_USE, {"tool_input": {}})
    assert receipt.handler_ids == ()
    assert receipt.outcome.dispatch_blocked is False


def test_noop_runner_advertises_no_executable_handler() -> None:
    runner = NoopHookHandlerRunner()

    assert runner.supports(HookHandlerManifest(
        "handler-a", "r1", HookEvent.PRE_TOOL_USE, 0,
    )) is False


def test_missing_or_explicitly_disabled_config_keeps_host_off(tmp_path: Path) -> None:
    assert build_codex_hook_host(tmp_path / "missing.toml") is None
    config = _write(tmp_path, _policy(enabled="false"))

    assert build_codex_hook_host(config) is None


def _policy(
    *,
    enabled: str = "true",
    upstream_revision: str = CODEX_HOOK_PARITY_REVISION,
    extra: str = "",
    handler: str = "",
) -> str:
    lines = [
        "[hooks]",
        f"enabled = {enabled}",
        'policy_revision = "builtin-empty-v1"',
        f'upstream_revision = "{upstream_revision}"',
        'manifest_ref = "crp://local/hooks/manifests/builtin-empty-v1"',
        'manifest_revision = "builtin-empty-v1"',
        'local_hard_guard_revision = "local-hard-guard-default"',
        "handlers = []",
    ]
    if extra:
        lines.append(extra)
    if handler:
        lines.extend(("", "[[hooks.handlers]]", handler))
        lines = [line for line in lines if line != "handlers = []"]
    return "\n".join(lines) + "\n"


def _unknown_handler() -> str:
    return "\n".join((
        'handler_id = "not-registered"',
        'handler_revision = "r999"',
        'event = "PreToolUse"',
        "order = 0",
        "sync = true",
        "enabled = true",
        "timeout_ms = 100",
    ))


@pytest.mark.parametrize(
    "body",
    (
        "[hooks]\nenabled = true\n",
        _policy(extra='command = "powershell -Command Invoke-Anything"'),
        _policy(extra='path = "C:/unsafe/hook.py"'),
        _policy(extra='url = "https://unsafe.example/hook"'),
        _policy(extra='module = "unsafe.hook"'),
        _policy(upstream_revision="unreviewed-upstream"),
        _policy(handler=_unknown_handler()),
    ),
)
def test_invalid_or_unregistered_hook_policy_fails_closed(tmp_path: Path, body: str) -> None:
    with pytest.raises(CodexHookConfigurationError):
        build_codex_hook_host(_write(tmp_path, body))


def test_existing_malformed_toml_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CodexHookConfigurationError):
        build_codex_hook_host(_write(tmp_path, "[hooks\nenabled = true"))


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "codex-hooks.toml"
    path.write_text(body, encoding="utf-8")
    return path
