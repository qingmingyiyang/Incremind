from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from backend import companion_diary_runtime, companion_runtime
from backend.companion_provider_runtime import CompanionModelRuntime
from backend.companion_runtime_layout import resolve_companion_config_root
from core.companion_core import CompanionModelRouter


def _local_model_runtime(*_args) -> CompanionModelRuntime:
    return CompanionModelRuntime(
        router=CompanionModelRouter(provider=None, provider_capabilities=(), egress_consented=False),
        model_name="local-template-v1",
    )


def test_chat_service_passes_the_selected_project_to_memory_bridge(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    bridge = SimpleNamespace(recall=lambda **_kwargs: None)

    def build_bridge(_container, *, repository, project_id):
        captured["repository"] = repository
        captured["project_id"] = project_id
        return bridge

    reducer = SimpleNamespace(
        project=lambda: SimpleNamespace(effective_mood="normal", snapshot=SimpleNamespace(mood="normal")),
        apply_chat_affect=lambda **_kwargs: None,
    )
    monkeypatch.setattr(companion_runtime, "build_companion_memory_bridge", build_bridge)
    monkeypatch.setattr(companion_runtime, "build_companion_model_runtime", _local_model_runtime)
    monkeypatch.setattr(companion_runtime, "_state_reducer", lambda **_kwargs: reducer)

    service = companion_runtime.build_companion_chat_service(
        SimpleNamespace(root_dir=tmp_path), project_id="project-alpha",
    )

    assert isinstance(service, companion_runtime.CompanionChatService)
    assert captured["project_id"] == "project-alpha"
    assert isinstance(captured["repository"], companion_runtime.CompanionRepository)
    assert service.state_projection_loader() == {"mood": "normal", "reply_style": "balanced"}


def test_state_reducer_uses_the_strict_fixed_companion_clock(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_COMPANION_E2E_CLOCK_MODE", "packaged-fixed")
    monkeypatch.setenv("CHRIPTMAS_COMPANION_E2E_CLOCK_UTC", "2026-07-23T23:59:58Z")
    repository = companion_runtime.CompanionRepository.at_data_root(tmp_path)
    reducer = companion_runtime._state_reducer(container=SimpleNamespace(root_dir=tmp_path), repository=repository)

    assert reducer.now() == datetime(2026, 7, 23, 23, 59, 58, tzinfo=timezone.utc)
    assert reducer.local_day(reducer.now()) == datetime(2026, 7, 23, 23, 59, 58, tzinfo=timezone.utc).astimezone().date().isoformat()


def test_memory_bridge_uses_the_strict_fixed_companion_clock(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_COMPANION_E2E_CLOCK_MODE", "packaged-fixed")
    monkeypatch.setenv("CHRIPTMAS_COMPANION_E2E_CLOCK_UTC", "2026-07-23T23:59:58Z")

    bridge = companion_runtime.build_companion_memory_bridge(
        SimpleNamespace(root_dir=tmp_path), project_id="project-alpha",
    )
    instant = datetime(2026, 7, 23, 23, 59, 58, tzinfo=timezone.utc)

    assert bridge.now() == instant
    assert bridge.today() == instant.astimezone().date()
    assert bridge.local_timezone() == instant.astimezone().tzinfo


def test_state_reducer_and_diary_use_packaged_environment_without_container_attributes(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources_root = tmp_path / "resources"
    config_root = resources_root / "companion-config"
    config_root.mkdir(parents=True)
    source_config = resolve_companion_config_root(companion_runtime.Path(companion_runtime.__file__)) / "config" / "companion"
    (config_root / "economy-rules.json").write_bytes((source_config / "economy-rules.json").read_bytes())
    (config_root / "items.json").write_bytes((source_config / "items.json").read_bytes())
    monkeypatch.setenv("CHRIPTMAS_COMPANION_MODE", "packaged")
    monkeypatch.setenv("CHRIPTMAS_COMPANION_REPOSITORY_ROOT", str(tmp_path / "repository"))
    monkeypatch.setenv("CHRIPTMAS_COMPANION_RESOURCES_ROOT", str(resources_root))
    monkeypatch.setattr(companion_runtime, "build_companion_model_runtime", _local_model_runtime)

    container = SimpleNamespace(root_dir=tmp_path / "vault")
    repository = companion_runtime.CompanionRepository.at_data_root(container.root_dir)
    reducer = companion_runtime._state_reducer(container=container, repository=repository)
    diary = companion_runtime.build_companion_diary_service(container)
    chat = companion_runtime.build_companion_chat_service(container)
    media = companion_runtime.build_companion_media_service(container)
    ambient = companion_runtime.build_companion_ambient_service(container, rules=reducer.rules)

    assert reducer.project().snapshot.coins == 0
    assert diary.food_names
    assert isinstance(chat, companion_runtime.CompanionChatService)
    assert isinstance(media, companion_runtime.CompanionMediaSessionService)
    assert isinstance(ambient, companion_runtime.CompanionAmbientService)


def test_diary_service_recovers_when_food_catalog_contains_non_object_items(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "companion-config"
    config_root.mkdir()
    (config_root / "items.json").write_text('{"items":[42,"invalid",null]}', encoding="utf-8")
    monkeypatch.setattr(
        companion_diary_runtime,
        "resolve_companion_runtime_layout",
        lambda _container: SimpleNamespace(companion_config_root=config_root),
    )
    monkeypatch.setattr(
        companion_runtime,
        "build_companion_model_runtime",
        _local_model_runtime,
    )

    diary = companion_runtime.build_companion_diary_service(SimpleNamespace(root_dir=tmp_path))

    assert diary.food_names == {}
def test_diary_service_preserves_valid_food_names_when_another_catalog_item_is_malformed(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "companion-config"
    config_root.mkdir()
    (config_root / "items.json").write_text(
        '{"items":['
        '{"id":"food:apple","kind":"food","name":"红苹果"},'
        '42,'
        '{"id":"outfit:red","kind":"outfit","name":"红围巾"}'
        ']}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        companion_diary_runtime,
        "resolve_companion_runtime_layout",
        lambda _container: SimpleNamespace(companion_config_root=config_root),
    )
    monkeypatch.setattr(
        companion_runtime,
        "build_companion_model_runtime",
        _local_model_runtime,
    )

    diary = companion_runtime.build_companion_diary_service(SimpleNamespace(root_dir=tmp_path))

    assert diary.food_names == {"food:apple": "红苹果"}


def test_diary_service_recovers_when_food_catalog_is_not_utf8(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "companion-config"
    config_root.mkdir()
    (config_root / "items.json").write_bytes(b"\xff\xfeinvalid")
    monkeypatch.setattr(
        companion_diary_runtime,
        "resolve_companion_runtime_layout",
        lambda _container: SimpleNamespace(companion_config_root=config_root),
    )
    monkeypatch.setattr(
        companion_runtime,
        "build_companion_model_runtime",
        _local_model_runtime,
    )

    diary = companion_runtime.build_companion_diary_service(SimpleNamespace(root_dir=tmp_path))

    assert diary.food_names == {}


def test_service_assembly_uses_the_external_prompt_runtime_adapter() -> None:
    assert companion_runtime.load_active_character_prompt.__module__ == "backend.companion_prompt_runtime"


def test_service_assembly_uses_the_external_diary_runtime_adapter() -> None:
    assert companion_runtime.load_diary_food_names.__module__ == "backend.companion_diary_runtime"
