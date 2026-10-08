from __future__ import annotations

from pathlib import Path

import pytest

from core.plugin_hands.contracts import (
    PLUGIN_HANDS_PROTOCOL,
    PluginHandsContractError,
    PluginHandsLaunch,
    PluginHandsLease,
    PluginHandsOutcome,
    require_exact_workspace_resources,
)
from core.plugin_hands.workspace import PluginHandsWorkspaceError, PluginHandsWorkspaceManager


def _lease() -> PluginHandsLease:
    return PluginHandsLease(
        "lease-00000001", "invoke-0000001", 1,
        "project-0000001", "turn-0000000001", 1, "recipe-0000001",
        ("workspace_input", "workspace_output"), "2099-08-26T00:00:00Z",
    )


def test_contracts_freeze_protocol_identity_and_environment(tmp_path: Path) -> None:
    launch = PluginHandsLaunch(
        "launch-0000001", tmp_path / "runner.exe", (), {"LANG": "C"},
    )
    assert launch.protocol == PLUGIN_HANDS_PROTOCOL
    assert dict(launch.environment) == {"LANG": "C"}
    with pytest.raises(TypeError):
        launch.environment["X"] = "1"  # type: ignore[index]
    with pytest.raises(PluginHandsContractError, match="environment"):
        PluginHandsLaunch("launch-0000001", tmp_path / "runner.exe", (), {"API_KEY": "forbidden"})
    with pytest.raises(PluginHandsContractError, match="environment"):
        PluginHandsLaunch(
            "launch-0000001", tmp_path / "runner.exe", (),
            {"CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID": "plugin-controlled"},
        )
    with pytest.raises(PluginHandsContractError, match="lease does not match"):
        from core.plugin_hands.contracts import PluginHandsInvocation
        PluginHandsInvocation("other-00000001", "plugin-0000001", launch, _lease(), 1000, {"value": "x"})


def test_invocation_input_and_outcome_payloads_are_shallow_frozen(tmp_path: Path) -> None:
    from core.plugin_hands.contracts import PluginHandsInvocation

    launch = PluginHandsLaunch("launch-0000001", tmp_path / "runner.exe", (), {})
    request = {"nested": {"still": "shallow"}}
    invocation = PluginHandsInvocation("invoke-0000001", "plugin-0000001", launch, _lease(), 1000, request)
    request["late"] = "not copied"
    assert dict(invocation.input) == {"nested": {"still": "shallow"}}
    output = {"accepted": True}
    success = PluginHandsOutcome("invoke-0000001", "lease-00000001", "success", output)
    output["late"] = False
    assert dict(success.output or {}) == {"accepted": True}
    with pytest.raises(PluginHandsContractError, match="success outcome"):
        PluginHandsOutcome("invoke-0000001", "lease-00000001", "success")
    with pytest.raises(PluginHandsContractError, match="error outcome"):
        PluginHandsOutcome("invoke-0000001", "lease-00000001", "failed", {"unsafe": True}, "failed")


def test_workspace_creates_only_host_paths_and_disposes_known_outcome(tmp_path: Path) -> None:
    manager = PluginHandsWorkspaceManager(tmp_path)
    workspace = manager.create(_lease())
    assert workspace.input_dir.is_dir() and workspace.output_dir.is_dir()
    assert workspace.root.parent == tmp_path.resolve()
    assert manager.dispose(workspace, PluginHandsOutcome("invoke-0000001", "lease-00000001", "success", {})) is None
    assert not workspace.root.exists()


def test_workspace_creates_exactly_the_leased_resources_and_empty_is_not_writable(tmp_path: Path) -> None:
    input_only = PluginHandsLease(
        "lease-00000002", "invoke-0000002", 1,
        "project-0000001", "turn-0000000002", 1, "recipe-0000001",
        ("workspace_input",), "2026-08-26T00:00:00Z",
    )
    empty = PluginHandsLease(
        "lease-00000003", "invoke-0000003", 1,
        "project-0000001", "turn-0000000003", 1, "recipe-0000001",
        (), "2026-08-26T00:00:00Z",
    )
    manager = PluginHandsWorkspaceManager(tmp_path)
    read_workspace = manager.create(input_only)
    assert read_workspace.input_dir is not None and read_workspace.input_dir.is_dir()
    assert read_workspace.output_dir is None and not (read_workspace.root / "output").exists()
    assert not (read_workspace.root / "tmp").exists()
    empty_workspace = manager.create(empty)
    assert empty_workspace.input_dir is None and empty_workspace.output_dir is None
    assert {item.name for item in empty_workspace.root.iterdir()} == {"code"}


def test_workspace_stages_and_rechecks_reviewed_payload_without_artifact_path(tmp_path: Path) -> None:
    manager = PluginHandsWorkspaceManager(tmp_path)
    workspace = manager.create(_lease())
    payload = (("payload/lib/util.py", b"VALUE = 1\n"), ("payload/main.py", b"print('reviewed')\n"))
    manager.stage_code(workspace, payload)
    manager.verify_code(workspace, payload)
    assert workspace.code_dir.joinpath("payload", "main.py").read_bytes() == b"print('reviewed')\n"
    assert str(workspace.code_dir) not in repr(workspace)
    workspace.code_dir.joinpath("payload", "main.py").write_bytes(b"drift")
    with pytest.raises(PluginHandsWorkspaceError, match="bytes drifted"):
        manager.verify_code(workspace, payload)


@pytest.mark.parametrize("payload", [(), (("main.py", b"x"),), (("payload/../escape.py", b"x"),), (("payload/a.py", b"x"), ("payload/a.py", b"x"))])
def test_workspace_rejects_untrusted_or_empty_code_payload(tmp_path: Path, payload) -> None:
    manager = PluginHandsWorkspaceManager(tmp_path)
    workspace = manager.create(_lease())
    with pytest.raises(PluginHandsWorkspaceError, match="payload files"):
        manager.stage_code(workspace, payload)


def test_workspace_pre_spawn_validation_rejects_missing_or_extra_staged_code(tmp_path: Path) -> None:
    from core.plugin_hands.stdio_runner import validate_plugin_hands_request
    from core.plugin_hands.contracts import PluginHandsControl, PluginHandsInvocation

    manager = PluginHandsWorkspaceManager(tmp_path)
    lease = _lease()
    workspace = manager.create(lease)
    launch = PluginHandsLaunch("launch-0000001", (tmp_path / "runner.exe").resolve(), ())
    invocation = PluginHandsInvocation("invoke-0000001", "plugin-0000001", launch, lease, 1000, {})
    # An empty fixed code directory is permitted for legacy non-plugin fixtures.
    validate_plugin_hands_request(launch, workspace, invocation, PluginHandsControl())
    (workspace.code_dir / "unexpected.txt").write_text("drift", encoding="utf-8")
    with pytest.raises(PluginHandsWorkspaceError, match="staged code is invalid"):
        validate_plugin_hands_request(launch, workspace, invocation, PluginHandsControl())


def test_workspace_rejects_resource_expansion_before_spawn_validation(tmp_path: Path) -> None:
    from core.plugin_hands.stdio_runner import validate_plugin_hands_request

    lease = PluginHandsLease(
        "lease-00000004", "invoke-0000004", 1,
        "project-0000001", "turn-0000000004", 1, "recipe-0000001",
        ("workspace_input",), "2099-08-26T00:00:00Z",
    )
    manager = PluginHandsWorkspaceManager(tmp_path)
    workspace = manager.create(lease)
    (workspace.root / "output").mkdir()
    launch = PluginHandsLaunch("launch-0000004", (tmp_path / "runner.exe").resolve(), ())
    from core.plugin_hands.contracts import PluginHandsControl, PluginHandsInvocation
    invocation = PluginHandsInvocation("invoke-0000004", "plugin-0000004", launch, lease, 1000, {})
    with pytest.raises(PluginHandsWorkspaceError, match="resources drifted"):
        validate_plugin_hands_request(launch, workspace, invocation, PluginHandsControl())


def test_lease_rejects_unknown_or_duplicate_resource_names() -> None:
    values = (("network",), ("workspace_input", "workspace_input"))
    for resources in values:
        with pytest.raises(PluginHandsContractError, match="allowed resources"):
            PluginHandsLease(
                "lease-00000005", "invoke-0000005", 1,
                "project-0000001", "turn-0000000005", 1, "recipe-0000001",
                resources, "2026-08-26T00:00:00Z",
            )


def test_artifact_resource_scope_requires_exact_lease_declaration() -> None:
    require_exact_workspace_resources(("workspace_input",), ("workspace_input",))
    for actual in ((), ("workspace_input", "workspace_output")):
        with pytest.raises(PluginHandsContractError, match="resources drifted"):
            require_exact_workspace_resources(actual, ("workspace_input",))


def test_unknown_outcome_retains_workspace_with_opaque_host_reference(tmp_path: Path) -> None:
    manager = PluginHandsWorkspaceManager(tmp_path)
    workspace = manager.create(_lease())
    result = manager.dispose(workspace, PluginHandsOutcome("invoke-0000001", "lease-00000001", "unknown", error_code="unknown_effect"))
    assert result == "plugin-hands-workspace:lease-00000001:1"
    assert workspace.root.is_dir()
    assert str(workspace.root) not in result


def test_unknown_outcome_never_enters_workspace_tree_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = PluginHandsWorkspaceManager(tmp_path)
    workspace = manager.create(_lease())

    def forbidden_remove(*_args, **_kwargs):
        raise AssertionError("unknown workspace entered cleanup")

    monkeypatch.setattr(
        "core.plugin_hands.workspace.WindowsHandleTreeRemover.remove",
        forbidden_remove,
    )
    result = manager.dispose(
        workspace,
        PluginHandsOutcome(
            "invoke-0000001", "lease-00000001", "unknown",
            error_code="unknown_effect",
        ),
    )

    assert result == "plugin-hands-workspace:lease-00000001:1"
    assert workspace.root.is_dir()


def test_workspace_rejects_preexisting_lease_path_and_forged_outcome(tmp_path: Path) -> None:
    (tmp_path / "lease-00000001").mkdir()
    manager = PluginHandsWorkspaceManager(tmp_path)
    with pytest.raises(PluginHandsWorkspaceError, match="already exists"):
        manager.create(_lease())

    other = PluginHandsLease(
        "lease-00000002", "invoke-0000002", 1,
        "project-0000001", "turn-0000000002", 1, "recipe-0000001",
        (), "2026-08-26T00:00:00Z",
    )
    workspace = manager.create(other)
    with pytest.raises(PluginHandsWorkspaceError, match="does not match"):
        manager.dispose(workspace, PluginHandsOutcome("invoke-0000002", "lease-00000003", "success", {}))


def test_workspace_rejects_symlink_or_reparse_paths(tmp_path: Path) -> None:
    link = tmp_path / "linked-root"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(PluginHandsWorkspaceError, match="link"):
        PluginHandsWorkspaceManager(link)


def test_workspace_manager_rejects_root_identity_change_during_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured_root = tmp_path / "configured-workspaces"
    configured_root.mkdir()
    replacement_target = tmp_path / "replacement-target"
    replacement_target.mkdir()
    path_type = type(configured_root)
    original_resolve = path_type.resolve

    def resolve_after_replacement(path, *args, **kwargs):
        if path == configured_root:
            return original_resolve(replacement_target, *args, **kwargs)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "resolve", resolve_after_replacement)

    with pytest.raises(PluginHandsWorkspaceError, match="root identity changed"):
        PluginHandsWorkspaceManager(configured_root)


def test_dispose_rejects_link_injected_after_workspace_creation(tmp_path: Path) -> None:
    manager = PluginHandsWorkspaceManager(tmp_path)
    workspace = manager.create(_lease())
    link = workspace.output_dir / "escape"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(PluginHandsWorkspaceError, match="link"):
        manager.dispose(workspace, PluginHandsOutcome("invoke-0000001", "lease-00000001", "success", {}))
    assert workspace.root.is_dir()
