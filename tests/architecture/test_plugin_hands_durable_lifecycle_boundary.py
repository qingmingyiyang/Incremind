from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def test_durable_lifecycle_is_host_local_and_does_not_claim_global_authorities() -> None:
    source = (ROOT / "src" / "core" / "plugin_hands" / "durable_lifecycle.py").read_text(encoding="utf-8")
    forbidden_imports = ("fastapi", "requests", "httpx", "socket", "subprocess", "launch_in_appcontainer", "session", "turn", "receipt", "registry", "secret", "activation")
    assert all(re.search(rf"^\\s*(?:from|import)\\s+.*\\b{token}\\b", source, re.MULTILINE) is None for token in forbidden_imports)
    assert "SQLiteStructuredRecordStore" in source
    assert "execute_prepared" in source
    assert "never retries Plugin code" in source


def test_global_composition_paths_do_not_import_the_disabled_lifecycle() -> None:
    paths = (
        ROOT / "src" / "core" / "plugin_host" / "__init__.py",
        ROOT / "src" / "core" / "plugin_host" / "skill_activation.py",
        ROOT / "src" / "core" / "plugin_host" / "tool_activation.py",
        ROOT / "src" / "core" / "plugin_host" / "mcp_reference_activation.py",
        ROOT / "src" / "backend" / "api" / "mcp_runtime.py",
        ROOT / "src" / "backend" / "api" / "plugin_runtime.py",
        ROOT / "src" / "backend" / "api" / "routes" / "plugin_packages.py",
        ROOT / "src" / "backend" / "memory_app" / "kernel" / "ai_runtime.py",
        ROOT / "src" / "core" / "plugin_hands" / "__init__.py",
    )
    for path in paths:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            assert "plugin_hands.durable_lifecycle" not in text
            assert "from core.plugin_hands import PluginHandsDurableLifecycle" not in text
            assert "PluginHandsDurableLifecycle" not in text
