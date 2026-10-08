from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INTAKE = ROOT / "src" / "core" / "plugin_host" / "package_intake.py"
MCP_REFERENCE = ROOT / "src" / "core" / "plugin_host" / "mcp_reference_activation.py"
ROUTES = ROOT / "src" / "backend" / "api" / "routes" / "__init__.py"


def test_plugin_intake_has_no_execution_permission_or_network_dependencies() -> None:
    source = INTAKE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    forbidden = {"subprocess", "socket", "urllib", "requests", "httpx", "secrets", "hashlib"}
    assert not {name.split(".")[0] for name in imports} & forbidden
    assert "ai_kernel" not in source
    assert "mcp_host" not in source
    assert "boundary" not in source.lower()


def test_plugin_package_router_is_registered_once() -> None:
    source = ROUTES.read_text(encoding="utf-8")
    assert source.count("app.include_router(plugin_packages_router)") == 1


def test_plugin_mcp_reference_cannot_own_transport_secret_or_backend_runtime() -> None:
    source = MCP_REFERENCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    forbidden = {"subprocess", "socket", "urllib", "requests", "httpx", "secrets"}
    assert not {name.split(".")[0] for name in imports} & forbidden
    assert "backend." not in source
    assert "mcp_host" not in source
    assert all(name not in source for name in ("endpoint_url", "executable", "secret_ref", "environment"))
