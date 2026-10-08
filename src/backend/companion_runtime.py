from __future__ import annotations

from pathlib import Path

from backend.companion_provider_runtime import build_companion_model_runtime
from backend.companion_prompt_runtime import load_active_character_prompt
from backend.companion_memory_runtime import build_companion_memory_bridge
from backend.companion_diary_runtime import load_diary_food_names
from backend.companion_voice_asr_runtime import build_voice_asr_settings_loader
from backend.companion_state_runtime import resolve_economy_rules_path
from core.companion_core import (
    CompanionChatService,
    CompanionAmbientService,
    build_companion_clock,
    CompanionMediaSessionService,
    CompanionDiaryService,
    CompanionRepository,
    CompanionStateReducer,
    CompanionVisionGrantStore,
    CompanionVisionService,
    CompanionVoiceGrantStore,
    CompanionVoiceTranscriptionService,
    companion_chat_state_context,
)

def build_companion_chat_service(container: object, *, project_id: str = "default") -> CompanionChatService:
    root_dir = Path(getattr(container, "root_dir")).resolve()
    repository = CompanionRepository.at_data_root(root_dir)
    model_runtime = build_companion_model_runtime(container, "companion.chat")
    reducer = _state_reducer(container=container, repository=repository)
    memory_bridge = build_companion_memory_bridge(
        container, repository=repository, project_id=project_id,
    )
    return CompanionChatService(
        repository,
        model_router=model_runtime.router,
        character_prompt_loader=lambda: load_active_character_prompt(root_dir),
        state_projection_loader=lambda: companion_chat_state_context(reducer.project()),
        affect_sink=lambda signal, request_id: reducer.apply_chat_affect(signal=signal, request_id=request_id),
        memory_recall_loader=memory_bridge.recall,
        project_id=project_id,
    )


def build_companion_diary_service(container: object) -> CompanionDiaryService:
    root_dir = Path(getattr(container, "root_dir")).resolve()
    repository = CompanionRepository.at_data_root(root_dir)
    model_runtime = build_companion_model_runtime(container, "companion.diary")
    return CompanionDiaryService(
        repository,
        model_router=model_runtime.router,
        character_prompt_loader=lambda: load_active_character_prompt(root_dir),
        food_names=load_diary_food_names(container),
        model_version=model_runtime.model_name,
    )


def build_companion_media_service(container: object) -> CompanionMediaSessionService:
    root_dir = Path(getattr(container, "root_dir")).resolve()
    repository = CompanionRepository.at_data_root(root_dir)
    model_runtime = build_companion_model_runtime(container, "companion.event")
    return CompanionMediaSessionService(
        repository,
        model_router=model_runtime.router,
        character_prompt_loader=lambda: load_active_character_prompt(root_dir),
    )


def build_companion_ambient_service(container: object, *, rules) -> CompanionAmbientService:
    root_dir = Path(getattr(container, "root_dir")).resolve()
    repository = CompanionRepository.at_data_root(root_dir)
    model_runtime = build_companion_model_runtime(container, "companion.ambient")
    return CompanionAmbientService(
        repository, rules=rules, model_router=model_runtime.router,
    )


def build_companion_vision_service(container: object, grant_store: CompanionVisionGrantStore) -> CompanionVisionService:
    root_dir = Path(getattr(container, "root_dir")).resolve()
    repository = CompanionRepository.at_data_root(root_dir)
    model_runtime = build_companion_model_runtime(container, "companion.vision")
    return CompanionVisionService(repository, model_router=model_runtime.router, character_prompt_loader=lambda: load_active_character_prompt(root_dir), grant_store=grant_store)


def build_companion_voice_transcription_service(container: object, grant_store: CompanionVoiceGrantStore) -> CompanionVoiceTranscriptionService:
    root_dir = Path(getattr(container, "root_dir")).resolve()
    return CompanionVoiceTranscriptionService(
        grant_store=grant_store,
        settings_loader=build_voice_asr_settings_loader(root_dir),
    )


def _state_reducer(*, container: object, repository: CompanionRepository) -> CompanionStateReducer:
    clock = build_companion_clock()
    return CompanionStateReducer(
        repository,
        rules_path=resolve_economy_rules_path(container),
        now=clock.now_utc,
        local_day=clock.local_day,
    )
