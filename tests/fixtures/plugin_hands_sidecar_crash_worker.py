from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import core.plugin_hands.contained_host as contained_host_module
from core.plugin_hands.contained_host import WindowsContainedPluginHandsHost
from core.plugin_hands.contracts import PluginHandsInvocation, PluginHandsLaunch, PluginHandsLease, PluginHandsOutcome
from core.plugin_hands.durable_lifecycle import PluginHandsDurableLifecycle, PluginHandsExecutionScope, PluginHandsLifecycleBinding
from core.plugin_hands.workspace import PluginHandsWorkspace, PluginHandsWorkspaceManager
from core.storage_provider import SQLiteStructuredRecordStore


class Authority:
    def __init__(self, launch: PluginHandsLaunch) -> None:
        self.launch = launch

    def resolve(self, _binding: PluginHandsLifecycleBinding, _scope: PluginHandsExecutionScope) -> PluginHandsLaunch:
        return self.launch

    def prepare_workspace(self, _binding: PluginHandsLifecycleBinding, _scope: PluginHandsExecutionScope, _workspace: PluginHandsWorkspace) -> None:
        return None

    def verify_workspace(self, _binding: PluginHandsLifecycleBinding, _scope: PluginHandsExecutionScope, _workspace: PluginHandsWorkspace) -> None:
        return None

    def validate_outcome(self, _binding: PluginHandsLifecycleBinding, _scope: PluginHandsExecutionScope, _outcome: PluginHandsOutcome) -> None:
        return None


def main() -> int:
    root, pid_path, launch_count_path = (Path(value).resolve() for value in sys.argv[1:4])
    workspace_root = root / "workspaces"
    workspace_root.mkdir(parents=True, exist_ok=True)
    powershell = (
        Path(os.environ["SYSTEMROOT"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    ).resolve(strict=True)
    hello = json.dumps({
        "protocol": "plugin-hands/1", "type": "hello", "launch_id": "launch-sidecar-0001",
        "lease_id": "lease-sidecar-0001", "invocation_id": "invoke-sidecar-0001",
    }, separators=(",", ":"))
    command = f"[Console]::Out.WriteLine('{hello}'); [Console]::In.ReadLine() | Out-Null; while ($true) {{}}"
    launch = PluginHandsLaunch(
        "launch-sidecar-0001", powershell,
        ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command), {},
    )
    lease = PluginHandsLease(
        "lease-sidecar-0001", "invoke-sidecar-0001", 1, "project-sidecar-0001",
        "turn-sidecar-000001", 1, "recipe-sidecar-0001", (), "2099-08-26T00:00:00Z",
    )
    invocation = PluginHandsInvocation(
        "invoke-sidecar-0001", "plugin-sidecar-0001", launch, lease, 300_000, {"fixture": True},
    )
    binding = PluginHandsLifecycleBinding(
        "intent-sidecar-0001", "capability-sidecar-0001", "artifact-sidecar-0001",
        "plugin-sidecar-0001", "hand-sidecar-0001", "package-sidecar-0001", 1, 1, 1,
        "appcontainer-v1", "recipe-sidecar-0001",
    )
    original_launch = contained_host_module.launch_in_appcontainer

    def observed_launch(spec, profile):
        process = original_launch(spec, profile)
        process_fact = json.dumps({
            "pid": process.process_id,
            "is_appcontainer": process.is_appcontainer(),
        }, separators=(",", ":"))
        pending_pid_path = pid_path.with_suffix(".pending")
        pending_pid_path.write_text(process_fact, encoding="ascii")
        pending_pid_path.replace(pid_path)
        launch_count_path.write_text("1", encoding="ascii")
        return process

    contained_host_module.launch_in_appcontainer = observed_launch
    lifecycle = PluginHandsDurableLifecycle(
        SQLiteStructuredRecordStore(root / "records.sqlite3"), Authority(launch),
        now=lambda: "2026-08-26T00:00:00Z",
    )
    lifecycle.execute(
        WindowsContainedPluginHandsHost(), PluginHandsWorkspaceManager(workspace_root),
        binding, invocation,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
