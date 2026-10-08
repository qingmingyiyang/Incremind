from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from backend.api.ppt_master_installation_runtime import FinalInstallationReceiptStore
from backend.api.effect_partition_inventory import PPT_MASTER_EFFECT_PARTITION
from backend.api.ppt_master_runtime_bootstrap import bootstrap_ppt_master_runtime
from backend.api.ppt_master_runtime_bootstrap import (
    build_ppt_master_effect_runtime,
    register_ppt_master_effect_recovery_partition,
)
from backend.api.ai_runtime import register_ppt_master_fixed_capability
from backend.api.ppt_master_capability_runtime import PPT_MASTER_FIXED_CAPABILITY
from core.ai_kernel import ScopedCapabilityRegistry
from core.effect_log import build_effect_runtime


def _installed_root(tmp_path):
    root = tmp_path / "runtime-root"
    (root / "config").mkdir(parents=True)
    runtime = root / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"fixture")
    operation = "eff2_" + "a" * 64
    source = root / ".rebuild-data" / "ppt-master-artifacts" / operation / "source" / "scripts"
    source.mkdir(parents=True)
    for name in ("attribution_guard.py", "svg_quality_checker.py", "svg_to_pptx.py", "pptx_delivery_check.py"):
        (source / name).write_text("# fixture", encoding="utf-8")
    receipts = FinalInstallationReceiptStore(root / ".rebuild-data" / "ppt-master-installation-receipts")
    receipts.write(operation, {
        "receipt": f"receipt:ppt-master-installation:{operation}", "operation_id": operation,
        "project_id": "default", "generation": 0,
        "commit": "abcdef0123456789abcdef0123456789abcdef01",
        "manifest_revision": "manifest-v1", "artifact_receipt": f"ppt-master-artifact:{operation}",
        "plugin_id": "ppt-master", "skill_id": "ppt-master", "activation_revision": 1,
        "binding_revision": 1, "profile_projection": {
            "project_id": "default", "status": "active", "profile_revision": 1,
            "owned_plugin_source": True, "owned_skill_id": True,
            "owned_plugin_id": True, "owned_tool_id": True,
        },
    })
    artifact_receipt = root / ".rebuild-data" / "ppt-master-artifacts" / operation / "receipt.json"
    artifact_receipt.write_text('{"receipt": "ppt-master-artifact:' + operation + '"}', encoding="utf-8")
    receipts.set_installed("default", operation, generation=0, activation_revision=1)
    return root, operation, receipts


def test_bootstrap_attaches_manifest_handlers_and_resolves_only_current_artifact(tmp_path, monkeypatch) -> None:
    root, operation, receipts = _installed_root(tmp_path)
    effects = build_effect_runtime(root / ".rebuild-data" / "jobs.sqlite3", owner_id="test")
    installation = SimpleNamespace(status=lambda project_id: {
        "state": "installed", "operation_id": operation,
    })
    monkeypatch.setattr(
        "backend.api.ppt_master_runtime_bootstrap.build_ppt_master_installation_runtime",
        lambda **_kwargs: installation,
    )
    dedicated = build_effect_runtime(root / ".rebuild-data" / "ppt-effects.sqlite3", owner_id="ppt")
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=effects, ppt_master_effect_runtime=dedicated))

    bootstrap_ppt_master_runtime(application, root_dir=root, packaged=False)

    manifest = application.state.runtime_self_manifest
    assert manifest["compatibility"]["ppt-master"]["status"] == "compatible"
    assert application.state.ppt_master_installation_runtime is installation
    assert application.state.ppt_master_capability._effects is dedicated
    assert ("presentation_pptx_fixed_generate", "effect-v2") in dedicated.handlers._registrations
    assert ("presentation_pptx_fixed_generate", "effect-v2") not in effects.handlers._registrations
    service, artifact = application.state.ppt_master_capability._artifact_resolver("default")
    assert artifact.source_commit == "abcdef0123456789abcdef0123456789abcdef01"
    assert service is not None
    receipts.mark_rolled_back("default", installation_operation_id=operation, rollback_operation_id="rollback-1", generation=1, activation_revision=2)
    with pytest.raises(Exception):
        application.state.ppt_master_capability._artifact_resolver("default")


@pytest.mark.parametrize("field,value", [
    ("generation", 9), ("activation_revision", 9), ("artifact_receipt", "ppt-master-artifact:other"),
    ("manifest_revision", ""), ("binding_revision", -1),
    ("profile_projection", {
        "project_id": "default", "status": "inactive", "profile_revision": 1,
        "owned_plugin_source": True, "owned_skill_id": True,
        "owned_plugin_id": True, "owned_tool_id": True,
    }),
    ("profile_projection", {
        "project_id": "default", "status": "active", "profile_revision": 1,
        "owned_plugin_source": True, "owned_skill_id": True,
        "owned_plugin_id": True, "owned_tool_id": "true",
    }),
    ("profile_projection", {
        "project_id": "default", "status": "active", "profile_revision": 1,
        "owned_plugin_source": True, "owned_skill_id": True, "owned_plugin_id": True,
    }),
])
def test_bootstrap_rejects_tampered_current_installation_receipt(tmp_path, monkeypatch, field, value) -> None:
    root, operation, receipts = _installed_root(tmp_path)
    effects = build_effect_runtime(root / ".rebuild-data" / "jobs.sqlite3", owner_id="test")
    monkeypatch.setattr("backend.api.ppt_master_runtime_bootstrap.build_ppt_master_installation_runtime", lambda **_kwargs: SimpleNamespace(status=lambda _project: {"state": "installed", "operation_id": operation}))
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=effects))
    bootstrap_ppt_master_runtime(application, root_dir=root, packaged=False)
    payload = dict(receipts.read(operation))
    payload[field] = value
    receipt_path = root / ".rebuild-data" / "ppt-master-installation-receipts" / f"{operation}.json"
    receipt_path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    with pytest.raises(Exception):
        application.state.ppt_master_capability._artifact_resolver("default")


def test_bootstrap_manifest_is_incompatible_without_isolated_runtime(tmp_path, monkeypatch) -> None:
    root = tmp_path / "runtime-root"
    (root / "config").mkdir(parents=True)
    effects = build_effect_runtime(root / ".rebuild-data" / "jobs.sqlite3", owner_id="test")
    def must_not_construct(**_kwargs):
        raise AssertionError("unavailable bundled runtime must not construct writer")
    monkeypatch.setattr("backend.api.ppt_master_runtime_bootstrap.build_ppt_master_installation_runtime", must_not_construct)
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=effects))

    bootstrap_ppt_master_runtime(application, root_dir=root, packaged=False)

    assert application.state.runtime_self_manifest["compatibility"]["ppt-master"]["status"] == "incompatible"
    with pytest.raises(Exception):
        application.state.ppt_master_installation_runtime.status("default")
    assert application.state.ppt_master_capability is None


def test_bootstrap_degrades_when_interpreter_exists_but_manifest_is_incompatible(tmp_path, monkeypatch) -> None:
    root = tmp_path / "runtime-root"
    (root / "config").mkdir(parents=True)
    (root / "runtime").mkdir()
    (root / "runtime" / "python.exe").write_bytes(b"fixture")
    effects = build_effect_runtime(root / ".rebuild-data" / "jobs.sqlite3", owner_id="test")
    constructed = []
    monkeypatch.setattr(
        "backend.api.ppt_master_runtime_bootstrap.build_runtime_self_manifest_for_app",
        lambda *_args, **_kwargs: {
            "manifest_revision": "runtime-test-v1",
            "compatibility": {"ppt-master": {"status": "incompatible"}},
        },
    )
    monkeypatch.setattr(
        "backend.api.ppt_master_runtime_bootstrap.build_ppt_master_installation_runtime",
        lambda **_kwargs: constructed.append(True),
    )
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=effects))

    bootstrap_ppt_master_runtime(application, root_dir=root, packaged=False)

    assert constructed == []
    assert application.state.runtime_self_manifest["manifest_revision"] == "runtime-test-v1"
    assert application.state.ppt_master_capability is None
    assert application.state.ppt_master_smoke is None


def test_ppt_effect_runtime_is_dedicated_and_has_long_conversion_lease(tmp_path) -> None:
    runtime = build_ppt_master_effect_runtime(tmp_path, "ppt-test")

    assert runtime.runner.lease_seconds >= 360
    assert runtime.runner.lease_heartbeat_seconds == 60
    assert Path(runtime.log.database) == PPT_MASTER_EFFECT_PARTITION.database(tmp_path)


def test_ppt_partition_registers_only_after_capability_bootstrap(tmp_path) -> None:
    class Coordinator:
        def __init__(self):
            self.calls = []
        def register_partition(self, name, runtime):
            self.calls.append((name, runtime))

    coordinator = Coordinator()
    unavailable = SimpleNamespace(state=SimpleNamespace(ppt_master_effect_runtime=object(), ppt_master_capability=None))
    assert register_ppt_master_effect_recovery_partition(coordinator, unavailable) is False
    assert coordinator.calls == []

    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="ppt")
    ready = SimpleNamespace(state=SimpleNamespace(ppt_master_effect_runtime=runtime, ppt_master_capability=object()))
    assert register_ppt_master_effect_recovery_partition(coordinator, ready) is True
    assert coordinator.calls == [("ppt-master", runtime)]


def test_ai_registration_is_conditional_and_preserves_registered_provider() -> None:
    registry = ScopedCapabilityRegistry()
    assert register_ppt_master_fixed_capability(registry, None) is False
    assert registry.resolve(PPT_MASTER_FIXED_CAPABILITY) is None

    provider = object()
    application = SimpleNamespace(state=SimpleNamespace(ppt_master_capability=provider))
    assert register_ppt_master_fixed_capability(registry, application) is True
    definition, actual_provider = registry.resolve(PPT_MASTER_FIXED_CAPABILITY)
    assert definition.capability_id == PPT_MASTER_FIXED_CAPABILITY
    assert actual_provider is provider
