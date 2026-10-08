from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_HANDS = ROOT / "src" / "core" / "plugin_hands"
ACTIVATION_PATHS = (
    ROOT / "src" / "core" / "plugin_host" / "__init__.py",
    ROOT / "src" / "core" / "plugin_host" / "package_intake.py",
    ROOT / "src" / "core" / "plugin_host" / "skill_activation.py",
    ROOT / "src" / "core" / "plugin_host" / "tool_activation.py",
    ROOT / "src" / "core" / "plugin_host" / "mcp_reference_activation.py",
    ROOT / "src" / "backend" / "api" / "plugin_runtime.py",
    ROOT / "src" / "backend" / "api" / "routes" / "plugin_packages.py",
)


def _python_text(path: Path) -> str:
    files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
    return "\n".join(file.read_text(encoding="utf-8") for file in files)


def test_plugin_activation_cannot_reach_disabled_hands_host() -> None:
    for path in ACTIVATION_PATHS:
        text = _python_text(path)
        assert "core.plugin_hands" not in text
        assert "plugin_hands.contained_host" not in text
        assert "PluginHandsStdioRunner" not in text
        assert "WindowsContainedPluginHandsHost" not in text


def test_plugin_hands_has_no_activation_network_secret_or_session_authority() -> None:
    text = _python_text(PLUGIN_HANDS)
    forbidden = ("core.plugin_host", "backend.api", "SecretStore", "session_writer", "requests", "httpx", "urllib", "socket")
    for marker in forbidden:
        assert marker not in text


def test_plugin_hands_windows_workspace_cleanup_is_handle_relative_and_has_no_path_fallback() -> None:
    workspace = (PLUGIN_HANDS / "workspace.py").read_text(encoding="utf-8")
    remover = (PLUGIN_HANDS / "windows_handle_tree.py").read_text(encoding="utf-8")
    assert "WindowsHandleTreeRemover().remove" in workspace
    assert "shutil.rmtree" not in workspace
    assert "NtCreateFile" in remover
    assert "RootDirectory" in remover
    assert "_OBJ_DONT_REPARSE" in remover
    assert "_FILE_OPEN_REPARSE_POINT" in remover
    assert "NtQueryDirectoryFile" in remover
    assert "NtSetInformationFile" in remover
    assert "import shutil" not in remover
