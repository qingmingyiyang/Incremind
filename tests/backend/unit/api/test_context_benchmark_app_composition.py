from __future__ import annotations

from types import SimpleNamespace

from backend.api.app import create_app
from backend.api.context_benchmark_composition import ContextBenchmarkRuntime


def test_production_app_composes_only_the_request_scoped_benchmark_factory(
    tmp_path_factory,
) -> None:
    root = tmp_path_factory.mktemp("lmb")
    application = create_app(SimpleNamespace(root_dir=root))

    factory = application.state.context_benchmark_runtime_factory
    assert not hasattr(application.state, "ai_runtime")
    assert not hasattr(application.state, "ai_turn_runner")
    runtime = factory(SimpleNamespace(app=application))

    assert isinstance(runtime, ContextBenchmarkRuntime)
    assert not hasattr(application.state, "ai_runtime")
    assert not hasattr(application.state, "ai_turn_runner")
    assert not hasattr(application.state, "context_benchmark_runner")
    assert not hasattr(application.state, "context_benchmark_reaper")
    active = {
        manifest.capability_id: manifest.capability_revision
        for manifest in application.state.capability_package_catalog.active()
    }
    assert active["thought_graph_context"] == "4.2.0"


def test_app_reuses_startup_compiled_contributions_for_context_graph(
    tmp_path_factory, monkeypatch,
) -> None:
    def should_not_compile(_loader):
        raise AssertionError("Context Graph recompiled startup contributions")

    monkeypatch.setattr(
        "backend.api.capability_package_runtime.compile_capability_package_contributions",
        should_not_compile,
    )

    application = create_app(SimpleNamespace(root_dir=tmp_path_factory.mktemp("lmb-reuse")))

    assert application.state.context_graph_runtime.import_registry.registered()["importers"] == (
        "markdown", "thoughtdag",
    )
