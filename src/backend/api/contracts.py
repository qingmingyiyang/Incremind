from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GenerateVideoSummaryRequest(BaseModel):
    transcript_enhancement_enabled: bool | None = None


class GenerateSeriesSummariesRequest(BaseModel):
    transcript_enhancement_enabled: bool | None = None
    run_id: str | None = None


class CancelSeriesSummariesRequest(BaseModel):
    run_id: str | None = None


class CreateVideoNoteRequest(BaseModel):
    title: str
    content: str
    source: str = "manual"


class UpdateVideoNoteRequest(BaseModel):
    title: str
    content: str


class WorkspaceSettingsResponse(BaseModel):
    theme: str
    show_takeaways: bool
    profile_name: str = ""
    font_scale: int = 100
    content_density: str = "comfortable"
    transcript_enhancement_enabled: bool
    asr_model_quality: str
    transcription_mode: str
    rag_embedding_device: str
    rag_max_hits: int
    rag_rerank_enabled: bool
    window_tokens: int
    answer_detail_level: str = "medium"
    reasoning_effort: str = "none"
    talk_custom_prompt: str = ""
    video_generation_concurrency: int
    web_search_enabled: bool
    chaoxing_request_delay_seconds: float = 0.2
    chaoxing_init_course_delay_seconds: float = 0.3


class UpdateWorkspaceSettingsRequest(BaseModel):
    theme: str
    show_takeaways: bool
    profile_name: str = ""
    font_scale: int = 100
    content_density: str = "comfortable"
    transcript_enhancement_enabled: bool
    asr_model_quality: str
    transcription_mode: str
    rag_embedding_device: str
    rag_max_hits: int
    rag_rerank_enabled: bool
    window_tokens: int
    answer_detail_level: str = "medium"
    reasoning_effort: str = "none"
    talk_custom_prompt: str = ""
    video_generation_concurrency: int
    web_search_enabled: bool
    chaoxing_request_delay_seconds: float = 0.2
    chaoxing_init_course_delay_seconds: float = 0.3


class ProviderSettingsResponse(BaseModel):
    llm_provider: str
    openai_base_url: str
    openai_model: str
    has_openai_api_key: bool
    openai_api_key_masked: str
    hf_endpoint: str


class ProviderSecretStatusResponse(BaseModel):
    has_api_key: bool


class SaveProviderSecretRequest(BaseModel):
    api_key: str


class UpdateProviderSettingsRequest(BaseModel):
    llm_provider: str
    openai_base_url: str
    openai_model: str
    openai_api_key: str | None = None
    hf_endpoint: str | None = None


class TestProviderSettingsResponse(BaseModel):
    ok: bool
    message: str


class ProviderRecordRequest(BaseModel):
    provider_id: str
    name: str
    llm_provider: str = "openai"
    base_url: str = ""
    api_path: str = "/chat/completions"
    model: str = ""
    models: list[str] = Field(default_factory=list)
    enabled: bool = True


class UpdateProviderRecordRequest(BaseModel):
    name: str | None = None
    llm_provider: str | None = None
    base_url: str | None = None
    api_path: str | None = None
    model: str | None = None
    models: list[str] | None = None
    enabled: bool | None = None


class ProviderRecordResponse(BaseModel):
    provider_id: str
    name: str
    llm_provider: str
    base_url: str
    api_path: str
    model: str
    models: list[str]
    enabled: bool
    is_active: bool
    has_api_key: bool
    created_at: str
    updated_at: str
    egress_manifest: dict[str, object]


class ProviderRouteReferenceResponse(BaseModel):
    route_key: str
    model_name: str
    enabled: bool


class ProviderReplacementCandidateResponse(BaseModel):
    provider_id: str
    name: str
    model: str
    ready: bool


class ProviderDisconnectPreviewResponse(BaseModel):
    provider_id: str
    name: str
    is_active: bool
    has_api_key: bool
    egress_external: bool
    egress_consented: bool
    route_registry_revision: int
    referenced_routes: list[ProviderRouteReferenceResponse]
    replacement_candidates: list[ProviderReplacementCandidateResponse]
    delete_blockers: list[str]
    can_delete: bool
    disconnect_effect: str


class ProviderEgressConsentRequest(BaseModel):
    manifest_id: str
    confirm: bool = False


class ProviderEgressConsentResponse(BaseModel):
    provider_id: str
    egress_manifest: dict[str, object]


class ModelRouteDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model_name: str
    adapter_kind: Literal["openai-compatible", "openai-compatible-vision"] = "openai-compatible"
    enabled: bool = True
    reason: str = Field(min_length=3, max_length=500)


class UpdateModelRouteRequest(ModelRouteDraftRequest):
    expected_registry_revision: int = Field(ge=0)


class ModelRouteBatchAssignmentRequest(ModelRouteDraftRequest):
    route_key: str


class UpdateModelRouteBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_registry_revision: int = Field(ge=0)
    assignments: list[ModelRouteBatchAssignmentRequest] = Field(min_length=1, max_length=20)


class ModelRouteRecordResponse(BaseModel):
    route_key: str
    provider_id: str
    provider_revision: str
    model_name: str
    adapter_kind: str
    enabled: bool
    revision: int
    reason: str
    created_at: str
    updated_at: str


class ModelRouteListResponse(BaseModel):
    schema_version: str
    registry_revision: int
    runtime_activation: bool
    routes: list[ModelRouteRecordResponse]


class ModelRouteBatchUpdateResponse(ModelRouteListResponse):
    changed_route_keys: list[str]
    replayed: bool


class ModelRouteDetailResponse(BaseModel):
    schema_version: str
    registry_revision: int
    runtime_activation: bool
    route: ModelRouteRecordResponse
    history: list[dict[str, object]]


class ModelRoutePreviewResponse(BaseModel):
    valid: bool
    registry_revision: int
    runtime_activation: bool
    runtime_effect: str
    route: ModelRouteRecordResponse


class ProviderModelsResponse(BaseModel):
    provider_id: str
    models: list[str]


class FasterWhisperModelResponse(BaseModel):
    id: str
    label: str
    downloaded: bool
    current: bool
    recommended: bool
    status: str = "idle"
    progress: float | None = None
    detail: str | None = None
    error: str | None = None


class RagModelResponse(BaseModel):
    key: str
    label: str
    repo_id: str
    local_path: str
    purpose: str
    downloaded: bool
    status: str
    progress: float | None = None
    detail: str | None = None
    error: str | None = None
