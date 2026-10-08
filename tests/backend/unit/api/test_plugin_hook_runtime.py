from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from backend.api.plugin_hands_runtime import PLUGIN_HANDS_RECIPE_REVISION, FixedPluginHandsPythonRuntimeCatalog
from backend.api.plugin_hook_runtime import (
    PluginHandsHookRunner,
    PluginHookProjectionManager,
    plugin_hook_projection_fault_probe,
    plugin_hook_handler_manifest,
)
from core.ai_kernel import CodexHookHost, HookPolicyCatalog, HookPolicySnapshot
from core.ai_kernel.codex_hook_parity import HookEvent
from core.plugin_hands import PluginHandsOutcome, PluginHandsWorkspaceManager, WindowsContainedPluginHandsHost
from core.plugin_host.hook_activation import PluginHookActivation


def _binding() -> PluginHookActivation:
    return PluginHookActivation(
        "policy-plugin", "pre-tool-policy", "policy-hand", "policy-plugin~1.0.0",
        "PreToolUse", 10, True, 500, 1, 1, 1,
    )


class _Hooks:
    binding = _binding()

    def all_active(self):
        return (self.binding,)

    def resolve_active(self, plugin_id: str, *, hook_id: str):
        if plugin_id == self.binding.plugin_id and hook_id == self.binding.hook_id:
            return self.binding
        return None


class _Contained:
    def __init__(self) -> None:
        self.inputs: list[dict[str, object]] = []

    def close(self) -> None:
        pass

    def execute_prepared(self, workspace, launch, invocation):
        assert workspace.code_dir.joinpath("payload", "main.ps1").read_bytes() == b"reviewed"
        self.inputs.append(dict(invocation.input))
        return PluginHandsOutcome(
            invocation.invocation_id, invocation.lease.lease_id, "success",
            output={
                "exit_code": 0,
                "stdout": json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": "local plugin policy",
                }}),
                "stderr": "",
            },
        )


class _UnknownContained(_Contained):
    def execute_prepared(self, workspace, launch, invocation):
        self.inputs.append(dict(invocation.input))
        return PluginHandsOutcome(
            invocation.invocation_id, invocation.lease.lease_id, "unknown",
            error_code="contained_status_unknown",
        )


def test_contained_plugin_hook_feeds_existing_codex_parser_before_dispatch(tmp_path: Path) -> None:
    (tmp_path / "workspaces").mkdir()
    binding = _binding()
    hand = SimpleNamespace(
        activation_revision=1, containment_profile_revision="appcontainer-v1",
        artifact_opaque_ref="plugin-hands-artifact:policy-plugin:policy-hand:r1",
    )
    artifact = SimpleNamespace(
        package_record_id=binding.package_record_id, opaque_ref=hand.artifact_opaque_ref,
        effect="read", operation_semantics="read_only", requested_resources=(),
        runtime="powershell-stdio-v1", entrypoint="payload/main.ps1",
        payload_files=(("payload/main.ps1", b"reviewed"),),
    )
    contained = _Contained()
    hooks = _Hooks()
    hooks.binding = binding
    runner = PluginHandsHookRunner(
        hooks=hooks, hands=SimpleNamespace(resolve_active=lambda plugin_id, hand_id: hand),
        artifacts=SimpleNamespace(resolve=lambda plugin_id, hand_id: artifact),
        frozen_authorization=SimpleNamespace(current_handle=lambda turn_id: SimpleNamespace(
            facts=SimpleNamespace(project_id="project-0001", boundary_profile_revision=7),
        )),
        runtime_catalog=FixedPluginHandsPythonRuntimeCatalog(
            Path(__import__("sys").executable).resolve(), recipe_revision=PLUGIN_HANDS_RECIPE_REVISION,
        ),
        workspace_manager=PluginHandsWorkspaceManager(tmp_path / "workspaces"),
        contained_host=contained,
    )
    manifest = plugin_hook_handler_manifest(binding)
    host = CodexHookHost(
        catalog=HookPolicyCatalog(HookPolicySnapshot("plugin-policy-r1", (manifest,))),
        runner=runner,
    )

    receipt = host.invoke(HookEvent.PRE_TOOL_USE, {
        "turn_id": "turn-00000001", "step_id": "step-1", "tool_call_id": "call-1",
        "tool_name": "local.lookup", "tool_input": {"key": "a"},
    })

    assert receipt.outcome.dispatch_blocked is True
    assert receipt.outcome.stop_reason == "local plugin policy"
    assert contained.inputs == [{"hook_event": "PreToolUse", "payload": {
        "turn_id": "turn-00000001", "step_id": "step-1", "tool_call_id": "call-1",
        "tool_name": "local.lookup",
    }}]
    # Successful cleanup normally removes the attempt workspace. A platform
    # cleanup failure may leave only quarantined reviewed code; there is no
    # lifecycle database and the directory is never interpreted or replayed.
    assert not (tmp_path / "hook-lifecycle.sqlite3").exists()

    replay = host.invoke(HookEvent.PRE_TOOL_USE, {
        "turn_id": "turn-00000001", "step_id": "step-1", "tool_call_id": "call-1",
        "tool_name": "local.lookup", "tool_input": {"different": "secret"},
    })
    assert replay.outcome.dispatch_blocked is True
    assert len(contained.inputs) == 2

    host.set_handler_prefix_enabled("crp://plugin-hands/", enabled=False)
    fenced = host.invoke(HookEvent.PRE_TOOL_USE, {
        "turn_id": "turn-00000001", "step_id": "step-2", "tool_call_id": "call-2",
        "tool_name": "local.lookup", "tool_input": {"key": "not-observed"},
    })
    assert fenced.outcome.dispatch_blocked is False
    assert len(contained.inputs) == 2


def test_frozen_plugin_hook_manifest_becomes_unavailable_on_activation_drift(tmp_path: Path) -> None:
    (tmp_path / "workspaces").mkdir()
    hooks = _Hooks()
    runner = PluginHandsHookRunner(
        hooks=hooks, hands=SimpleNamespace(), artifacts=SimpleNamespace(), frozen_authorization=SimpleNamespace(),
        runtime_catalog=FixedPluginHandsPythonRuntimeCatalog(
            Path(__import__("sys").executable).resolve(), recipe_revision=PLUGIN_HANDS_RECIPE_REVISION,
        ),
        workspace_manager=PluginHandsWorkspaceManager(tmp_path / "workspaces"), contained_host=_Contained(),
    )
    frozen = plugin_hook_handler_manifest(hooks.binding)
    assert runner.supports(frozen) is True
    hooks.binding = PluginHookActivation(
        "policy-plugin", "pre-tool-policy", "policy-hand", "policy-plugin~1.0.0",
        "PreToolUse", 10, True, 500, 1, 2, 2,
    )
    assert runner.supports(frozen) is False


def test_unknown_read_only_hook_is_not_persisted_recovered_or_replayed(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    binding = _binding()
    hand = SimpleNamespace(
        activation_revision=1, containment_profile_revision="appcontainer-v1",
        artifact_opaque_ref="plugin-hands-artifact:policy-plugin:policy-hand:r1",
    )
    artifact = SimpleNamespace(
        package_record_id=binding.package_record_id, opaque_ref=hand.artifact_opaque_ref,
        effect="read", operation_semantics="read_only", requested_resources=(),
        runtime="powershell-stdio-v1", entrypoint="payload/main.ps1",
        payload_files=(("payload/main.ps1", b"reviewed"),),
    )
    contained = _UnknownContained()
    runner = PluginHandsHookRunner(
        hooks=_Hooks(), hands=SimpleNamespace(resolve_active=lambda plugin_id, hand_id: hand),
        artifacts=SimpleNamespace(resolve=lambda plugin_id, hand_id: artifact),
        frozen_authorization=SimpleNamespace(current_handle=lambda turn_id: SimpleNamespace(
            facts=SimpleNamespace(project_id="project-0001", boundary_profile_revision=7),
        )),
        runtime_catalog=FixedPluginHandsPythonRuntimeCatalog(
            Path(__import__("sys").executable).resolve(), recipe_revision=PLUGIN_HANDS_RECIPE_REVISION,
        ),
        workspace_manager=PluginHandsWorkspaceManager(workspace_root),
        contained_host=contained,
    )

    with __import__("pytest").raises(ValueError, match="contained execution failed"):
        runner(plugin_hook_handler_manifest(binding), {
            "turn_id": "turn-00000003", "step_id": "step-3",
            "tool_call_id": "call-3", "tool_name": "local.lookup",
        })

    assert len(contained.inputs) == 1
    assert len(tuple(workspace_root.iterdir())) == 1
    assert not (tmp_path / "hook-lifecycle.sqlite3").exists()


def test_projection_fail_closed_removes_all_third_party_handlers() -> None:
    plugin = plugin_hook_handler_manifest(_binding())
    base = HookPolicySnapshot("base-r1", ())

    class _Host:
        snapshot = HookPolicySnapshot("with-plugin-r1", (plugin,))
        disabled = False

        def current_snapshot(self):
            return self.snapshot

        def install_snapshot(self, snapshot):
            self.snapshot = snapshot
            return snapshot

        def set_handler_prefix_enabled(self, prefix, *, enabled):
            assert prefix == "crp://plugin-hands/"
            self.disabled = not enabled

    host = _Host()
    manager = PluginHookProjectionManager(host, SimpleNamespace(manifests=lambda: (plugin,)))

    installed = manager.fail_closed()

    assert installed.handlers == base.handlers
    assert host.current_snapshot().handlers == ()
    assert host.disabled is True


def test_projection_fault_probe_requires_matching_desktop_nonce_and_safe_marker(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / ".rebuild-data" / "e2e-plugin-hook-projection-fault"
    marker.parent.mkdir()
    marker.write_text("fail", encoding="ascii")
    probe = plugin_hook_projection_fault_probe(tmp_path)

    probe()
    monkeypatch.setenv("CHRIPTMAS_E2E_PLUGIN_HOOK_FAULT_NONCE", "fixture")
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_NONCE", "other")
    probe()
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_NONCE", "fixture")
    with __import__("pytest").raises(ValueError, match="fault injected"):
        probe()


@__import__("pytest").mark.skipif(os.name != "nt", reason="Windows AppContainer Plugin Hook fixture")
def test_real_appcontainer_plugin_hook_denies_through_codex_parser(tmp_path: Path) -> None:
    (tmp_path / "workspaces").mkdir()
    binding = replace(_binding(), timeout_ms=5_000)
    denied = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "contained policy",
    }}, separators=(",", ":"))
    escaped = denied.replace("'", "''")
    source = (
        "$hello = @{protocol='plugin-hands/1';type='hello';launch_id=$env:CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID;lease_id=$env:CHRIPTMAS_PLUGIN_HANDS_LEASE_ID;invocation_id=$env:CHRIPTMAS_PLUGIN_HANDS_INVOCATION_ID}\n"
        "[Console]::Out.WriteLine(($hello | ConvertTo-Json -Compress))\n"
        "[Console]::In.ReadLine() | Out-Null\n"
        f"$hookOut = @{{exit_code=0;stdout='{escaped}';stderr=''}}\n"
        "$result = @{protocol='plugin-hands/1';type='result';launch_id=$env:CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID;lease_id=$env:CHRIPTMAS_PLUGIN_HANDS_LEASE_ID;invocation_id=$env:CHRIPTMAS_PLUGIN_HANDS_INVOCATION_ID;output=$hookOut}\n"
        "[Console]::Out.WriteLine(($result | ConvertTo-Json -Depth 8 -Compress))\n"
    ).encode("utf-8")
    hand = SimpleNamespace(
        activation_revision=1, containment_profile_revision="appcontainer-v1",
        artifact_opaque_ref="plugin-hands-artifact:policy-plugin:policy-hand:r1",
    )
    artifact = SimpleNamespace(
        package_record_id=binding.package_record_id, opaque_ref=hand.artifact_opaque_ref,
        effect="read", operation_semantics="read_only", requested_resources=(),
        runtime="powershell-stdio-v1", entrypoint="payload/main.ps1",
        payload_files=(("payload/main.ps1", source),),
    )
    hooks = _Hooks()
    hooks.binding = binding
    runner = PluginHandsHookRunner(
        hooks=hooks, hands=SimpleNamespace(resolve_active=lambda plugin_id, hand_id: hand),
        artifacts=SimpleNamespace(resolve=lambda plugin_id, hand_id: artifact),
        frozen_authorization=SimpleNamespace(current_handle=lambda turn_id: SimpleNamespace(
            facts=SimpleNamespace(project_id="project-0001", boundary_profile_revision=7),
        )),
        runtime_catalog=FixedPluginHandsPythonRuntimeCatalog(
            Path(__import__("sys").executable).resolve(), recipe_revision=PLUGIN_HANDS_RECIPE_REVISION,
        ),
        workspace_manager=PluginHandsWorkspaceManager(tmp_path / "workspaces"),
        contained_host=WindowsContainedPluginHandsHost(),
    )
    manifest = plugin_hook_handler_manifest(binding)
    host = CodexHookHost(
        catalog=HookPolicyCatalog(HookPolicySnapshot("plugin-policy-real-r1", (manifest,))),
        runner=runner,
    )

    receipt = host.invoke(HookEvent.PRE_TOOL_USE, {
        "turn_id": "turn-00000002", "step_id": "step-2", "tool_call_id": "call-2",
        "tool_name": "local.lookup", "tool_input": {},
    })

    assert receipt.outcome.dispatch_blocked is True
    assert receipt.outcome.stop_reason == "contained policy"
    assert tuple((tmp_path / "workspaces").iterdir()) == ()
    runner.close()
