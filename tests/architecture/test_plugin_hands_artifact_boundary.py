from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "src" / "core" / "plugin_host" / "hands_artifact.py"
DISCONNECTED = (
    ROOT / "src" / "backend" / "api" / "plugin_runtime.py",
    ROOT / "src" / "core" / "plugin_host" / "tool_activation.py",
    ROOT / "src" / "core" / "plugin_host" / "skill_activation.py",
    ROOT / "src" / "core" / "plugin_host" / "mcp_reference_activation.py",
)


def test_hands_artifact_is_not_activation_or_runtime_wiring() -> None:
    for path in DISCONNECTED:
        text = path.read_text(encoding="utf-8")
        assert "hands_artifact" not in text
        assert "PluginHandsArtifactService" not in text
    route = (ROOT / "src" / "backend" / "api" / "routes" / "plugin_packages.py").read_text(encoding="utf-8")
    composition = (ROOT / "src" / "backend" / "memory_app" / "kernel" / "ai_runtime.py").read_text(encoding="utf-8")
    assert "build_plugin_hands_artifacts" in route
    assert "PluginHandsArtifactService" not in route
    assert "PluginHandsArtifactService" not in composition


def test_hands_artifact_has_no_execution_permission_secret_or_network_authority() -> None:
    text = ARTIFACT.read_text(encoding="utf-8")
    forbidden = ("import subprocess", "import socket", "import requests", "import httpx", "from core.plugin_hands", "from core.ai_boundary", "from core.ai_session", "from backend.api", "SecretStore(")
    for marker in forbidden:
        assert marker not in text
