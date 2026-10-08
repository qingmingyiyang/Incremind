from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "src" / "core" / "external_extension_runtime"


def test_external_extension_runtime_has_no_private_scheduler_or_direct_authority_writers() -> None:
    forbidden_imports = {
        "threading",
        "subprocess",
        "core.secret_store",
        "core.memory",
        "core.document",
        "core.ai_kernel",
        "core.mcp_host",
        "core.plugin_hands",
    }
    # Pure URL parsing is allowed in adapters.  Only modules that can open a
    # connection belong to the single outbound transport implementation.
    network_imports = {
        "socket",
        "ssl",
        "urllib.request",
        "http.client",
        "requests",
        "httpx",
        "aiohttp",
    }
    forbidden_names = {
        "Thread",
        "Timer",
        "Scheduler",
        "RetryWorker",
        "RecoveryWorker",
        "SecretResolver",
        "MemoryWriter",
        "DocumentWriter",
    }
    for path in RUNTIME.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        defined: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
        effective_forbidden_imports = (
            forbidden_imports - {"threading"}
            if path.name == "outbound_fetch.py"
            else forbidden_imports
        )
        assert not any(
            name == forbidden or name.startswith(f"{forbidden}.")
            for name in imported
            for forbidden in effective_forbidden_imports
        ), path
        if path.name != "outbound_fetch.py":
            assert not any(
                name == forbidden or name.startswith(f"{forbidden}.")
                for name in imported
                for forbidden in network_imports
            ), path
        assert defined.isdisjoint(forbidden_names), path


def test_runtime_effect_handlers_are_queryable_strategies_for_core_reaper() -> None:
    lifecycle = (RUNTIME / "lifecycle.py").read_text(encoding="utf-8")
    assert "EffectClass.QUERYABLE" in lifecycle
    assert "def probe(self, effect: Effect)" in lifecycle
    assert "-> ArtifactInventory" in lifecycle
    assert "self._facts.commit_artifact" in lifecycle
    assert "self._facts.artifact_evidence" in lifecycle
    assert "self._facts.resolution_observation" in lifecycle
    assert "self._acquirer.probe" not in lifecycle
    assert "self._resolver.probe" not in lifecycle
    assert "EffectReaper" not in lifecycle
    assert "EffectRunner(" not in lifecycle


def test_production_egress_factory_is_private_to_backend_startup_composition() -> None:
    startup = ROOT / "src" / "backend" / "api" / "external_extension_runtime_startup.py"
    app = ROOT / "src" / "backend" / "api" / "app.py"
    production_uses: list[Path] = []
    forbidden_test_seam_uses: list[Path] = []
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "production_extension_acquisition_fetcher" in text:
            production_uses.append(path)
        if "_fetcher_for_test" in text or "_CONSTRUCTION_TOKEN" in text:
            forbidden_test_seam_uses.append(path)
    assert set(production_uses) == {
        RUNTIME / "outbound_fetch.py",
        startup,
    }
    assert forbidden_test_seam_uses == [RUNTIME / "outbound_fetch.py"]
    rendered = startup.read_text(encoding="utf-8")
    assert "build_effect_runtime" not in rendered
    assert "EffectRunner" not in rendered
    assert "EffectReaper" not in rendered
    assert "production_extension_acquisition_fetcher()" in rendered
    assert "GitHubSourceResolver(fetcher)" in rendered
    assert "GitHubArtifactAcquirer(fetcher)" in rendered
    app_rendered = app.read_text(encoding="utf-8")
    registration = "external_extension_runtime = register_external_extension_runtime("
    assert app_rendered.count(registration) == 1
    assert (
        "application.state.external_extension_runtime = external_extension_runtime"
        in app_rendered
    )
    assert (
        "application.state.external_extension_install_workflow = (\n"
        "            external_extension_runtime.install_workflow\n"
        "        )"
        in app_rendered
    )
    assert app_rendered.index(registration) < app_rendered.index(
        "effect_recovery = EffectRecoveryCoordinator"
    )
