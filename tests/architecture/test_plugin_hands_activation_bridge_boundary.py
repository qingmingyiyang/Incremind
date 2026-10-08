from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "src" / "backend" / "api" / "plugin_hands_runtime.py"


def test_only_production_outer_bridge_composes_kernel_host_and_activation() -> None:
    source = BRIDGE.read_text(encoding="utf-8")
    for required in (
        "core.ai_kernel.dispatcher", "core.plugin_hands.durable_lifecycle",
        "core.plugin_hands.contained_host", "core.plugin_host.hands_activation",
        "core.plugin_host.hands_artifact",
    ):
        assert required in source

    registration_paths = (
        ROOT / "src" / "backend" / "api" / "plugin_runtime.py",
        ROOT / "src" / "core" / "plugin_host" / "__init__.py",
    )
    for path in registration_paths:
        text = path.read_text(encoding="utf-8")
        assert "backend.api.plugin_hands_runtime" not in text
        assert "PluginHandsCapabilityProvider" not in text

    app = (ROOT / "src" / "backend" / "api" / "app.py").read_text(encoding="utf-8")
    for registration in (
        "register_plugin_hands_execution_handler",
        "register_plugin_hands_cleanup_handler",
        "register_plugin_hands_upgrade_handler",
        "backfill_plugin_hands_execution_effects",
        "backfill_plugin_hands_upgrade_effects",
    ):
        assert registration in app
    assert "recover_plugin_hands_runtime" not in app
    assert "shutdown_plugin_hands_runtime" in app
    assert "PluginHandsCapabilityProvider" not in app
    assert app.index('add_event_handler("shutdown", shutdown_ai_turns)') < app.index(
        'add_event_handler("shutdown", shutdown_plugin_hands)'
    )

    composition = (ROOT / "src" / "backend" / "memory_app" / "kernel" / "ai_runtime.py").read_text(encoding="utf-8")
    assert "build_plugin_hands_registration_manager" in composition
    assert "plugin_hands_manager.reconcile()" in composition
    routes = (ROOT / "src" / "backend" / "api" / "routes" / "plugin_packages.py").read_text(encoding="utf-8")
    assert "build_plugin_hands_activation" in routes
    assert "PluginHandsCapabilityProvider" not in routes


def test_activation_and_host_layers_do_not_cross_import_the_bridge() -> None:
    paths = (
        ROOT / "src" / "core" / "plugin_host" / "hands_activation.py",
        ROOT / "src" / "core" / "plugin_host" / "hands_artifact.py",
        ROOT / "src" / "core" / "plugin_hands",
    )
    for path in paths:
        files = path.rglob("*.py") if path.is_dir() else (path,)
        text = "\n".join(file.read_text(encoding="utf-8") for file in files)
        assert "plugin_hands_runtime" not in text
        assert "backend.api" not in text
