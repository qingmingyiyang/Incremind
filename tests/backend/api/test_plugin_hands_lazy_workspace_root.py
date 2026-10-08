from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import app as api_app
from backend.api.ai_runtime import get_or_build_ai_runtime
import backend.api.plugin_hands_runtime as plugin_hands_runtime
from backend.api.plugin_hands_runtime import _workspace_manager_factory, register_plugin_hands_cleanup_handler
from core.effect_log import EffectHandlerRegistry, EffectLog, EffectState
from core.plugin_hands.contracts import PluginHandsInvocation, PluginHandsLaunch, PluginHandsLease
from core.plugin_hands.durable_lifecycle import PluginHandsDurableLifecycle, PluginHandsLifecycleBinding
from core.plugin_hands.workspace import PluginHandsWorkspaceError
from core.storage_provider import SQLiteStructuredRecordStore


class _UnusedAuthority:
    def resolve(self, *_args):
        raise AssertionError("fenced startup recovery must not resolve a launch")

    def prepare_workspace(self, *_args):
        raise AssertionError("fenced startup recovery must not create a workspace")

    def verify_workspace(self, *_args):
        raise AssertionError("fenced startup recovery must not create a workspace")

    def validate_outcome(self, *_args):
        raise AssertionError("fenced startup recovery must not validate an outcome")


def _binding() -> PluginHandsLifecycleBinding:
    return PluginHandsLifecycleBinding(
        "intent-0000001", "capability-0001", "artifact-000001", "plugin-0000001", "1h",
        "package-000001", 1, 1, 1, "containment-r1", "recipe-0000001",
    )


def _fenced_lifecycle(root) -> None:
    (root / ".rebuild-data").mkdir(parents=True, exist_ok=True)
    launch = PluginHandsLaunch("launch-0000001", (root / "runner.exe").absolute(), (), {})
    lease = PluginHandsLease(
        "lease-00000001", "invoke-0000001", 1, "project-0000001", "turn-0000000001",
        1, "recipe-0000001", (), "2026-08-25T00:00:00Z",
    )
    invocation = PluginHandsInvocation("invoke-0000001", "plugin-0000001", launch, lease, 1000, {})
    lifecycle = PluginHandsDurableLifecycle(
        SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3"),
        _UnusedAuthority(), now=lambda: "2026-08-24T00:00:00Z",
    )
    prepared = lifecycle.prepare(_binding(), invocation)
    lifecycle.fence(invocation.invocation_id, expected_revision=prepared.revision)


def test_real_fastapi_startup_does_not_provision_workspace_root_without_cleanup(tmp_path) -> None:
    root = tmp_path / ".rebuild-data" / "plugin-hands-workspaces"

    with TestClient(api_app.create_app(SimpleNamespace(root_dir=tmp_path))):
        pass

    assert not root.exists()


def test_full_get_or_build_ai_runtime_does_not_provision_workspace_root(tmp_path) -> None:
    application = FastAPI()
    root = tmp_path / ".rebuild-data" / "plugin-hands-workspaces"

    runtime = get_or_build_ai_runtime(SimpleNamespace(app=application), SimpleNamespace(root_dir=tmp_path))

    assert runtime is application.state.ai_runtime
    assert not root.exists()


def test_production_workspace_cleanup_does_not_decide_fenced_effect(tmp_path) -> None:
    _fenced_lifecycle(tmp_path)
    root = tmp_path / ".rebuild-data" / "plugin-hands-workspaces"

    handlers = EffectHandlerRegistry()
    register_plugin_hands_cleanup_handler(root_dir=tmp_path, effect_runtime=SimpleNamespace(handlers=handlers))
    assert handlers.kinds() == ("plugin_hands_workspace_cleanup",)
    record = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / "jobs.sqlite3"
    ).read("plugin_hands_lifecycles", "invoke-0000001")
    assert record is not None and record.payload["state"] == "fenced"
    assert not root.exists()


def test_production_core_reaper_marks_expired_plugin_effect_unknown(tmp_path) -> None:
    _fenced_lifecycle(tmp_path)

    with TestClient(api_app.create_app(SimpleNamespace(root_dir=tmp_path))):
        pass

    effect = EffectLog(tmp_path / ".rebuild-data" / "jobs.sqlite3").get(
        "plugin-hands-effect-invoke-0000001"
    )
    assert effect.state is EffectState.UNKNOWN
    assert effect.error_ref == "plugin-hands.outcome-receipt-missing"


def test_production_workspace_factory_rejects_root_link_before_first_provision(tmp_path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    configured_root = tmp_path / "configured-workspaces"
    try:
        configured_root.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")

    with pytest.raises(PluginHandsWorkspaceError, match="root is a link"):
        _workspace_manager_factory(configured_root)()

    assert not tuple(target.iterdir())


def test_production_workspace_factory_passes_unresolved_configured_root_to_strict_manager(tmp_path, monkeypatch) -> None:
    configured_root = tmp_path / "configured-workspaces"
    received = []

    class _StrictManager:
        def __init__(self, configured):
            received.append(configured)

    monkeypatch.setattr(plugin_hands_runtime, "PluginHandsWorkspaceManager", _StrictManager)

    _workspace_manager_factory(configured_root)()

    assert received == [configured_root.absolute()]


def test_production_workspace_factory_rejects_root_replaced_by_link_after_creation(tmp_path, monkeypatch) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    configured_root = tmp_path / "configured-workspaces"
    original_mkdir = type(configured_root).mkdir

    def replace_created_root(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == configured_root:
            try:
                configured_root.rmdir()
                configured_root.symlink_to(target, target_is_directory=True)
            except OSError:
                pytest.skip("symlink creation is unavailable on this Windows host")
        return result

    monkeypatch.setattr(type(configured_root), "mkdir", replace_created_root)

    with pytest.raises(PluginHandsWorkspaceError, match="root is a link"):
        _workspace_manager_factory(configured_root)()

    assert not tuple(target.iterdir())
