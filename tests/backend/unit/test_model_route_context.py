from __future__ import annotations

from types import SimpleNamespace

from backend.model_route_context import (
    list_model_route_provider_contexts,
    model_route_provider_context,
    model_route_provider_context_from_record,
    provider_fallback,
)
from backend.providers import ProviderRegistry
from backend.api.workbench_input_classifier_runtime import (
    WorkbenchInputClassifierRuntime,
)
from backend.api.routes.product.providers import _resolve_provider_for_rebuild_role_record


def test_provider_fallback_is_bounded_when_settings_service_is_absent(tmp_path) -> None:
    fallback = provider_fallback(SimpleNamespace(root_dir=tmp_path))

    assert fallback == {
        "name": "OpenAI Compatible",
        "llm_provider": "openai",
        "base_url": "",
        "api_path": "/chat/completions",
        "model": "",
        "models": [],
        "enabled": True,
    }


def test_provider_context_accepts_a_read_only_compatibility_record(tmp_path) -> None:
    context = model_route_provider_context_from_record(
        tmp_path,
        {
            "provider_id": "compatibility-only",
            "base_url": "https://provider.example.invalid",
            "model": "local-fallback",
            "enabled": True,
        },
    )

    assert context.record["provider_id"] == "compatibility-only"
    assert context.egress_consented is False
    assert not (tmp_path / "library" / "global" / "providers" / "providers.json").exists()


def test_provider_registry_readonly_fallback_does_not_bootstrap_files(tmp_path) -> None:
    registry = ProviderRegistry(tmp_path)
    fallback = {"name": "Fallback", "base_url": "https://provider.example.invalid", "model": "fallback-model"}

    providers = registry.list_readonly(fallback=fallback)

    assert providers[0]["provider_id"] == "openai"
    assert providers[0]["name"] == "Fallback"
    assert providers[0]["is_active"] is True
    assert registry.get_readonly("openai", fallback=fallback)["model"] == "fallback-model"
    # 只读发现保留原协调锁，不创建供应商目录或其他持久文件。
    assert not (tmp_path / "library/global/providers").exists()
    files = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file())
    assert files == ["library/global/model-routes/.dispatch-authority.lock"]
    assert (tmp_path / files[0]).stat().st_size == 1


def test_provider_registry_readonly_reads_existing_records(tmp_path) -> None:
    registry = ProviderRegistry(tmp_path)
    registry.create(
        {"provider_id": "research", "name": "Research", "model": "research-model"},
        fallback={},
    )

    provider = registry.get_readonly("research", fallback={})

    assert provider["provider_id"] == "research"
    assert provider["model"] == "research-model"
    assert provider["is_active"] is False


def test_model_route_provider_contexts_do_not_bootstrap_registry(tmp_path) -> None:
    container = SimpleNamespace(root_dir=tmp_path)
    route = {"provider_id": "openai"}

    providers = list_model_route_provider_contexts(container)
    provider, consented = model_route_provider_context(container, route)

    assert providers[0].record["provider_id"] == "openai"
    assert provider["provider_id"] == "openai"
    assert consented is True
    assert not (tmp_path / "library").exists()


def test_classifier_authority_discovery_does_not_bootstrap_provider_registry(
    tmp_path,
) -> None:
    container = SimpleNamespace(root_dir=tmp_path)
    runtime = WorkbenchInputClassifierRuntime(
        runtime_root=tmp_path,
        object_store=object(),
        container=container,
    )

    compatibility, providers = runtime._model_route_contexts()

    assert compatibility.record["provider_id"] == "deepseek"
    assert providers[0].record["provider_id"] == "openai"
    assert not (
        tmp_path / "library" / "global" / "providers" / "providers.json"
    ).exists()


def test_rebuild_role_discovery_does_not_bootstrap_provider_registry(tmp_path) -> None:
    provider = _resolve_provider_for_rebuild_role_record(
        SimpleNamespace(root_dir=tmp_path),
        "intake-main-model",
    )

    assert provider["provider_id"] == "deepseek"
    assert not (
        tmp_path / "library" / "global" / "providers" / "providers.json"
    ).exists()
