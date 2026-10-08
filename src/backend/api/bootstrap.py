from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
from importlib import import_module
import os
from pathlib import Path
from threading import Lock
from typing import Callable
import logging
import time
from urllib.parse import urlsplit

from backend.agent import AgentContextBudgetService, FileAgentSessionStore
from backend.model_route_context import provider_fallback, provider_egress_manifest
from backend.providers import ProviderRegistry
from backend.security import ProviderEgressPolicyStore, SecretEgressBroker
from backend.bilibili import (
    BackgroundBilibiliDownloadStarter,
    BilibiliDownloader,
    BilibiliLinkedVideoDownloader,
    BilibiliLinkedVideoDownloadStarter,
    CompositeLinkedVideoDownloader,
    CompositeLinkedVideoDownloadStarter,
    YtDlpBilibiliResolver,
)
from backend.api.media_ingress_selection_authority import (
    SelectionGatedLegacyBilibiliDownloader,
    media_ingress_selection_authority_for_root,
)
from backend.chaoxing import (
    ChaoxingCourseImporter,
    ChaoxingDownloaderClient,
    ChaoxingLinkedVideoDownloader,
    ChaoxingLinkedVideoDownloadStarter,
)
from backend.video_summary.agent_adapter import WorkspaceAgentContextLoader
from backend.video_summary.infrastructure.filesystem_video_workspace import FileSystemVideoWorkspace
from backend.video_summary.infrastructure.faster_whisper_models import FasterWhisperModelManager
from backend.video_summary.infrastructure.in_memory_progress_tracker import InMemoryProgressTracker
from backend.video_summary.infrastructure.library_generation_adapters import (
    WorkspaceBackedVideoMindmapGenerator,
    WorkspaceBackedVideoSummaryGenerator,
)
from backend.video_summary.infrastructure.litellm_web_search import LiteLLMNativeWebSearchGateway
from backend.video_summary.infrastructure.mindmap_workflow import ConfiguredMindmapWorkflow
from backend.video_summary.infrastructure.rag_models import RAG_EMBEDDING_REQUIRED_MESSAGE, RagModelManager
from backend.video_summary.infrastructure.settings import load_env_settings, normalize_openai_base_url
from backend.video_summary.infrastructure.settings_service import SettingsService, SettingsServicePort
from backend.video_summary.infrastructure.settings import load_settings
from backend.video_summary.infrastructure.video_summary_workflow import ConfiguredVideoSummaryWorkflow
from backend.video_summary.library.ports import VideoMindmapGenerator, VideoSummaryGenerator
from backend.video_summary.library.usecases import (
    DeleteSeries,
    DeleteVideoSource,
    RefreshSeriesKnowledgeMemory,
    GenerateSeriesSummaryFromLibrary,
    GenerateVideoMindmapFromLibrary,
    GenerateVideoSummaryFromLibrary,
    GetVideoChapterCards,
    GetVideoKnowledgeCards,
    GetVideoMindmap,
    GetVideoNotes,
    GetVideoSource,
    GetVideoSummary,
    GetVideoWorkspaceTools,
    ImportLocalPlaygroundVideos,
    ImportLocalSeries,
    ImportLocalSeriesVideos,
    ListVideoLibrary,
    ResolveBilibiliSeries,
    ResolveBilibiliVideo,
    StartLinkedVideoDownload,
    CreateVideoNote,
    DeleteVideoNote,
    UpdateVideoNote,
)
from backend.security import SecretStore, build_active_provider_egress_guard, build_secret_store

LOGGER = logging.getLogger(__name__)

_LAZY_AGENT_MEMORY_EXPORTS = {
    "AgentWorkspaceIndexBuilder": ("backend.video_summary.infrastructure.agent_memory.index_builder", "AgentWorkspaceIndexBuilder"),
    "BGEReranker": ("backend.video_summary.infrastructure.agent_memory.pinpoint", "BGEReranker"),
    "SeriesRetrievalService": ("backend.video_summary.infrastructure.agent_memory.retrieval", "SeriesRetrievalService"),
}


def __getattr__(name: str):
    return _agent_memory_export(name)


def _agent_memory_export(name: str):
    cached = globals().get(name)
    if cached is not None:
        return cached
    target = _LAZY_AGENT_MEMORY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value

@dataclass(frozen=True)
class ApiContainer:
    config_path: Path
    root_dir: Path
    faster_whisper_model_manager: FasterWhisperModelManager
    list_video_library: ListVideoLibrary
    get_video_source: GetVideoSource
    get_video_summary: GetVideoSummary
    get_video_mindmap: GetVideoMindmap
    get_video_chapter_cards: GetVideoChapterCards
    get_video_cards: GetVideoKnowledgeCards
    get_video_notes: GetVideoNotes
    create_video_note: CreateVideoNote
    update_video_note: UpdateVideoNote
    delete_video_note: DeleteVideoNote
    get_video_workspace_tools: GetVideoWorkspaceTools
    generate_video_summary: GenerateVideoSummaryFromLibrary
    generate_series_summaries: GenerateSeriesSummaryFromLibrary
    generate_video_mindmap: GenerateVideoMindmapFromLibrary
    delete_series: DeleteSeries
    delete_video_source: DeleteVideoSource
    import_local_series: ImportLocalSeries
    import_local_playground_videos: ImportLocalPlaygroundVideos
    import_local_series_videos: ImportLocalSeriesVideos
    resolve_bilibili_series: ResolveBilibiliSeries
    resolve_bilibili_video: ResolveBilibiliVideo
    start_linked_video_download: StartLinkedVideoDownload
    generation_progress_tracker: InMemoryProgressTracker
    video_download_progress_tracker: InMemoryProgressTracker
    model_download_progress_tracker: InMemoryProgressTracker
    chaoxing_import_progress_tracker: InMemoryProgressTracker
    knowledge_memory_progress_tracker: InMemoryProgressTracker
    rag_model_manager: RagModelManager
    chaoxing_importer: ChaoxingCourseImporter
    linked_series_workspace: FileSystemVideoWorkspace
    workspace_index_invalidator: object
    settings_service: SettingsServicePort
    secret_store: SecretStore
    get_agent_graph_service: Callable[[], object]
    get_agent_context_usage: Callable[[], AgentContextBudgetService]
    agent_session_store: FileAgentSessionStore
    invalidate_agent_graph_service: Callable[[], None]
    invalidate_agent_workspace_indexes: Callable[[], None]
    refresh_agent_workspace_indexes: Callable[[], None]
    workspace_index_refresher: object


def build_api_container(
    root_dir: Path,
    generator: VideoSummaryGenerator | None = None,
    mindmap_generator: VideoMindmapGenerator | None = None,
    faster_whisper_model_manager: FasterWhisperModelManager | None = None,
    shared_resources=None,
) -> ApiContainer:
    config_path = root_dir / "config" / "settings.toml"
    settings = load_settings(config_path, root_dir)
    secret_store = build_secret_store(root_dir)
    workspace = FileSystemVideoWorkspace(root_dir)
    progress_tracker = InMemoryProgressTracker()
    video_download_progress_tracker = InMemoryProgressTracker()
    model_download_progress_tracker = InMemoryProgressTracker()
    chaoxing_import_progress_tracker = InMemoryProgressTracker()
    knowledge_memory_progress_tracker = InMemoryProgressTracker()
    rag_model_progress_tracker = InMemoryProgressTracker()
    index_refresher_ref: dict[str, _WorkspaceIndexRefresher | None] = {"value": None}

    def on_rag_model_download_completed(model_key: str) -> None:
        if model_key != "embedding":
            return
        index_refresher = index_refresher_ref["value"]
        if index_refresher is not None:
            index_refresher.refresh_all()

    rag_model_manager = RagModelManager(
        root_dir=root_dir,
        progress_tracker=rag_model_progress_tracker,
        on_download_completed=on_rag_model_download_completed,
        models_root=shared_resources.model_path('fastembed') if shared_resources is not None else None,
    )
    model_manager = faster_whisper_model_manager or FasterWhisperModelManager(
        shared_resources.model_path('faster-whisper') if shared_resources is not None else root_dir / "data" / "models" / "faster-whisper"
    )
    resolved_generator = generator or WorkspaceBackedVideoSummaryGenerator(
        workspace=workspace,
        workflow=ConfiguredVideoSummaryWorkflow(root_dir),
    )
    resolved_mindmap_generator = mindmap_generator or WorkspaceBackedVideoMindmapGenerator(
        workspace=workspace,
        workflow=ConfiguredMindmapWorkflow(root_dir),
    )
    agent_runtime = LazyAgentRuntimeProvider(
        root_dir=root_dir,
        workspace=workspace,
        rag_model_manager=rag_model_manager,
        secret_store=secret_store,
    )
    index_refresher = _WorkspaceIndexRefresher(
        refresh_all=agent_runtime.refresh_workspace_indexes,
        upsert_video=agent_runtime.upsert_workspace_video,
        delete_video=agent_runtime.delete_workspace_video,
        delete_series=agent_runtime.delete_workspace_series,
        progress_tracker=knowledge_memory_progress_tracker,
        execute_effect=agent_runtime.execute_workspace_index_effect,
        query_completion=agent_runtime.query_workspace_index_effect,
        authority_snapshot=agent_runtime.workspace_index_authority_snapshot,
    )
    agent_runtime.set_workspace_index_refresher(index_refresher)
    workspace_index_invalidator = _WorkspaceIndexInvalidator(agent_runtime.invalidate_workspace_indexes)
    index_refresher_ref["value"] = index_refresher
    series_memory_refresher = RefreshSeriesKnowledgeMemory(
        workspace=workspace,
        index_refresher=index_refresher,
    )
    # The legacy downloader remains available for an explicit rollback revision.
    # Its real network/file effect is fenced by the same durable selection used by
    # the Media Hands ingress route; this is an adapter, not another downloader.
    bilibili_downloader = SelectionGatedLegacyBilibiliDownloader(
        BilibiliDownloader(),
        media_ingress_selection_authority_for_root(root_dir),
    )
    bilibili_linked_downloader = BilibiliLinkedVideoDownloader(
        root_dir=root_dir,
        downloader=bilibili_downloader,
    )
    chaoxing_client = ChaoxingDownloaderClient(
        state_dir=root_dir / "data" / "chaoxing",
        request_delay_seconds=settings.external_import.chaoxing.request_delay_seconds,
        init_course_delay_seconds=settings.external_import.chaoxing.init_course_delay_seconds,
    )
    linked_video_downloader = CompositeLinkedVideoDownloader(
        {
            "bilibili": bilibili_linked_downloader,
            "chaoxing": ChaoxingLinkedVideoDownloader(root_dir=root_dir, client=chaoxing_client),
        }
    )
    summary_generation_use_case = GenerateVideoSummaryFromLibrary(
        workspace,
        resolved_generator,
        progress_tracker,
        video_generation_concurrency=settings.generation.video_generation_concurrency,
        series_memory_refresher=series_memory_refresher,
        linked_video_downloader=linked_video_downloader,
    )
    series_generation_use_case = GenerateSeriesSummaryFromLibrary(
        workspace,
        summary_generation_use_case,
        progress_tracker,
    )
    bilibili_resolver = YtDlpBilibiliResolver()
    bilibili_download_starter = BackgroundBilibiliDownloadStarter(
        root_dir=root_dir,
        downloader=bilibili_downloader,
        progress_tracker=video_download_progress_tracker,
    )
    chaoxing_importer = ChaoxingCourseImporter(client=chaoxing_client)
    linked_download_starter = CompositeLinkedVideoDownloadStarter(
        {
            "bilibili": BilibiliLinkedVideoDownloadStarter(bilibili_download_starter),
            "chaoxing": ChaoxingLinkedVideoDownloadStarter(
                root_dir=root_dir,
                client=chaoxing_client,
                progress_tracker=video_download_progress_tracker,
            ),
        }
    )
    return ApiContainer(
        config_path=config_path,
        root_dir=root_dir,
        faster_whisper_model_manager=model_manager,
        list_video_library=ListVideoLibrary(workspace),
        get_video_source=GetVideoSource(workspace),
        get_video_summary=GetVideoSummary(workspace),
        get_video_mindmap=GetVideoMindmap(workspace),
        get_video_chapter_cards=GetVideoChapterCards(workspace),
        get_video_cards=GetVideoKnowledgeCards(workspace),
        get_video_notes=GetVideoNotes(workspace),
        create_video_note=CreateVideoNote(workspace, index_refresher),
        update_video_note=UpdateVideoNote(workspace, index_refresher),
        delete_video_note=DeleteVideoNote(workspace, index_refresher),
        get_video_workspace_tools=GetVideoWorkspaceTools(workspace),
        generate_video_summary=summary_generation_use_case,
        generate_series_summaries=series_generation_use_case,
        generate_video_mindmap=GenerateVideoMindmapFromLibrary(workspace, resolved_mindmap_generator),
        delete_series=DeleteSeries(workspace, index_refresher, generation_activity_checker=series_generation_use_case),
        delete_video_source=DeleteVideoSource(workspace, index_refresher, generation_activity_checker=series_generation_use_case),
        import_local_series=ImportLocalSeries(workspace),
        import_local_playground_videos=ImportLocalPlaygroundVideos(workspace),
        import_local_series_videos=ImportLocalSeriesVideos(workspace),
        resolve_bilibili_series=ResolveBilibiliSeries(workspace, bilibili_resolver, workspace_index_invalidator),
        resolve_bilibili_video=ResolveBilibiliVideo(workspace, bilibili_resolver, workspace_index_invalidator),
        start_linked_video_download=StartLinkedVideoDownload(workspace, linked_download_starter),
        generation_progress_tracker=progress_tracker,
        video_download_progress_tracker=video_download_progress_tracker,
        model_download_progress_tracker=model_download_progress_tracker,
        chaoxing_import_progress_tracker=chaoxing_import_progress_tracker,
        knowledge_memory_progress_tracker=knowledge_memory_progress_tracker,
        rag_model_manager=rag_model_manager,
        chaoxing_importer=chaoxing_importer,
        linked_series_workspace=workspace,
        workspace_index_invalidator=workspace_index_invalidator,
        settings_service=SettingsService(
            config_path=config_path,
            root_dir=root_dir,
            faster_whisper_model_manager=model_manager,
            rag_model_manager=rag_model_manager,
            secret_store=secret_store,
        ),
        secret_store=secret_store,
        get_agent_graph_service=agent_runtime.get_agent_graph_service,
        get_agent_context_usage=agent_runtime.get_context_budget_service,
        agent_session_store=agent_runtime.session_store,
        invalidate_agent_graph_service=agent_runtime.invalidate_agent_graph_service,
        invalidate_agent_workspace_indexes=agent_runtime.invalidate_workspace_indexes,
        refresh_agent_workspace_indexes=agent_runtime.refresh_workspace_indexes,
        workspace_index_refresher=index_refresher,
    )


class _WorkspaceIndexInvalidator:
    def __init__(self, invalidate: Callable[[], None]) -> None:
        self._invalidate = invalidate

    def invalidate(self) -> None:
        self._invalidate()


class _WorkspaceIndexRefresher:
    """Admit every index mutation to the already-composed Core runtime.

    This facade intentionally owns neither a queue nor a worker.  Before app
    composition binds it, calls fail closed; production binding happens before
    any recovery service can dispatch persisted Effects.
    """
    def __init__(
        self,
        refresh_all: Callable[[], None],
        upsert_video: Callable[[str, str], None],
        delete_video: Callable[[str, str], None],
        delete_series: Callable[[str], None],
        *,
        progress_tracker: InMemoryProgressTracker,
        task_id: str = "agent-memory-refresh",
        execute_effect: Callable | None = None,
        query_completion: Callable | None = None,
        authority_snapshot: Callable | None = None,
    ) -> None:
        self._refresh_all = refresh_all
        self._upsert_video = upsert_video
        self._delete_video = delete_video
        self._delete_series = delete_series
        self._progress_tracker = progress_tracker
        self._task_id = task_id
        self._effect_runtime = None
        self._execute_effect = execute_effect
        self._query_completion = query_completion
        self._authority_snapshot = authority_snapshot

    def bind_effect_runtime(self, effect_runtime) -> None:
        """Register the domain Handler/Probe in the one Core Reaper."""
        from core.effect_log import EffectClass, EffectHandlerRegistration, EffectRecoveryRegistration
        from core.effect_log.runtime import EffectLeaseCheckpoint
        from core.product_core.video_knowledge_index_effect_admission import (
            EFFECT_KIND, INTENT_SCHEMA, RECEIPT_KIND, RECEIPT_SCHEMA,
        )
        from core.product_core.video_knowledge_index_effect_execution import (
            VideoKnowledgeIndexEffectExecutionHandler,
            VideoKnowledgeIndexEffectExecutionProbe,
            VideoKnowledgeIndexQueryOutcome,
        )
        if self._effect_runtime is not None:
            if self._effect_runtime is not effect_runtime:
                raise RuntimeError("workspace index runtime binding drifted")
            return

        def execute(operation_id, request, checkpoint):
            if self._execute_effect is not None:
                return self._execute_effect(operation_id, dict(request), checkpoint)
            checkpoint()
            self._apply_operation((request["operation"], request.get("series_id", ""), request.get("video_id")))
            checkpoint()
            return {
                "artifact_ref": "lancedbindex",
                "artifact_revision": request["index_generation"],
                "entry_count": 0,
            }

        def probe(_operation_id, _request):
            # A missing immutable Receipt after a crash is deliberately
            # UNKNOWN: LanceDB is only a derived probe and cannot authorize a
            # replay merely because a table happens to exist.
            if self._query_completion is not None:
                return self._query_completion(_operation_id, dict(_request))
            return VideoKnowledgeIndexQueryOutcome("unknown")

        handler = VideoKnowledgeIndexEffectExecutionHandler(
            effect_runtime.log.database, execute, probe,
            lambda effect: EffectLeaseCheckpoint.for_claim(effect_runtime, effect).checkpoint,
        )
        recovery_probe = VideoKnowledgeIndexEffectExecutionProbe(effect_runtime.log.database, probe)
        effect_runtime.handlers.register(EffectHandlerRegistration(
            kind=EFFECT_KIND, effect_class=EffectClass.QUERYABLE, handler=handler, probe=recovery_probe,
            contract_version="effect-v2", intent_schema_version=INTENT_SCHEMA,
            receipt_kind=RECEIPT_KIND, receipt_schema_version=RECEIPT_SCHEMA,
        ))
        effect_runtime.recoveries.register(EffectRecoveryRegistration(
            kind=EFFECT_KIND, effect_class=EffectClass.QUERYABLE, probe=recovery_probe,
            verify=recovery_probe, contract_version="effect-v2",
        ))
        self._effect_runtime = effect_runtime

    def refresh(self) -> None:
        self._admit("full_rebuild", None, None)

    def refresh_all(self) -> None:
        self.refresh()

    def upsert_video(self, series_id: str, video_id: str) -> None:
        self._admit("upsert_video", series_id, video_id)

    def delete_video(self, series_id: str, video_id: str) -> None:
        self._admit("delete_video", series_id, video_id)

    def delete_series(self, series_id: str) -> None:
        self._admit("delete_series", series_id, None)

    def _admit(self, operation: str, series_id: str | None, video_id: str | None) -> None:
        if self._effect_runtime is None:
            raise RuntimeError("workspace index Effect runtime is unavailable")
        from core.product_core.video_knowledge_index_effect_admission import (
            SQLiteVideoKnowledgeIndexEffectAdmission, VideoKnowledgeIndexEffectAdmissionFactory,
            allocate_identity_in_connection,
        )
        snapshot = self._authority_snapshot(operation, series_id, video_id) if self._authority_snapshot else {
            "source_revision": "source-legacy", "workspace_revision": "workspace-legacy",
            "embedding_profile_revision": "embedding-legacy", "lancedb_schema_revision": "lancedb-legacy",
            "index_generation": "generation-legacy",
        }
        encoded_series, encoded_video = _index_token(series_id or "all"), _index_token(video_id or "all")
        now = int(time.time())
        import sqlite3
        with sqlite3.connect(self._effect_runtime.log.database, isolation_level=None) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            identity_id = allocate_identity_in_connection(connection, {
                "operation": operation, "series_id": encoded_series, "video_id": encoded_video,
                "source_revision": snapshot["source_revision"], "workspace_revision": snapshot["workspace_revision"],
                "embedding_profile_revision": snapshot["embedding_profile_revision"], "lancedb_schema_revision": snapshot["lancedb_schema_revision"],
            }, now=now)
            request = {"id": f"index-{identity_id}", "operation": operation, **snapshot,
                "index_generation": f"generation-{identity_id}"}
            if series_id is not None:
                request["series_id"] = encoded_series
            if video_id is not None:
                request["video_id"] = encoded_video
            admission = VideoKnowledgeIndexEffectAdmissionFactory(admitted_at=now).build(request=request)
            effect, _created = SQLiteVideoKnowledgeIndexEffectAdmission(self._effect_runtime.log).admit_in_connection(
                connection, admission, now=now,
            )
            connection.commit()
        # Low latency callers only persist the Effect.  The Core reaper is the
        # sole scheduler and will later claim the registered planned work.

    def _apply_operation(self, operation: tuple[str, str, str | None]) -> None:
        kind, series_id, video_id = operation
        series_id = _decode_index_token(series_id)
        video_id = _decode_index_token(video_id) if video_id is not None else None
        if kind == "full_rebuild":
            self._refresh_all()
            return
        if kind == "upsert_video":
            self._upsert_video(series_id, str(video_id))
            return
        if kind == "delete_video":
            self._delete_video(series_id, str(video_id))
            return
        if kind == "delete_series":
            self._delete_series(series_id)
            return
        raise RuntimeError(f"unsupported workspace index operation '{kind}'")


def _index_token(value: str) -> str:
    token = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
    if not token or len(token) > 80:
        raise ValueError("workspace index identifier is too long")
    return token


def _decode_index_token(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii")).decode("utf-8")

class LazyAgentRuntimeProvider:
    def __init__(
        self,
        *,
        root_dir: Path,
        workspace: FileSystemVideoWorkspace,
        rag_model_manager: RagModelManager | None = None,
        secret_store: SecretStore | None = None,
    ) -> None:
        self._root_dir = root_dir
        self._workspace = workspace
        self._rag_model_manager = rag_model_manager
        self._secret_store = secret_store or build_secret_store(root_dir)
        self._context_loader = WorkspaceAgentContextLoader(workspace)
        self.session_store = FileAgentSessionStore(root_dir / "data" / "agent_sessions")
        self._lock = Lock()
        self._cached_agent_graph_service: object | None = None
        self._cached_context_budget_service: AgentContextBudgetService | None = None
        self._cached_retrieval_service: SeriesRetrievalService | None = None
        self._workspace_index_refresher: _WorkspaceIndexRefresher | None = None

    def set_workspace_index_refresher(self, refresher: _WorkspaceIndexRefresher) -> None:
        self._workspace_index_refresher = refresher

    def admit_workspace_index(self, operation: str, series_id: str | None, video_id: str | None) -> None:
        if self._workspace_index_refresher is None:
            raise RuntimeError("workspace index admission is unavailable")
        if operation == "full_rebuild":
            self._workspace_index_refresher.refresh_all()
        elif operation == "upsert_video" and series_id is not None and video_id is not None:
            self._workspace_index_refresher.upsert_video(series_id, video_id)
        elif operation == "delete_video" and series_id is not None and video_id is not None:
            self._workspace_index_refresher.delete_video(series_id, video_id)
        elif operation == "delete_series" and series_id is not None:
            self._workspace_index_refresher.delete_series(series_id)
        else:
            raise ValueError("workspace index operation is invalid")

    def get_agent_graph_service(self) -> object:
        raise RuntimeError("Legacy Agent API is retired; use the unified AI Turn API")

    def invalidate_agent_graph_service(self) -> None:
        with self._lock:
            self._cached_agent_graph_service = None
            self._cached_context_budget_service = None

    def invalidate_workspace_indexes(self) -> None:
        with self._lock:
            if self._cached_retrieval_service is not None:
                self._cached_retrieval_service.invalidate()

    def refresh_workspace_indexes(self) -> None:
        index_builder = _agent_memory_export("AgentWorkspaceIndexBuilder")
        index_builder(retrieval_service=self._get_or_create_retrieval_service()).refresh()

    def upsert_workspace_video(self, series_id: str, video_id: str) -> None:
        self._get_or_create_retrieval_service().upsert_video(series_id, video_id)

    def delete_workspace_video(self, series_id: str, video_id: str) -> None:
        self._get_or_create_retrieval_service().delete_video(series_id, video_id)

    def delete_workspace_series(self, series_id: str) -> None:
        self._get_or_create_retrieval_service().delete_series(series_id)

    def execute_workspace_index_effect(self, operation_id: str, request: dict[str, str], checkpoint):
        return self._get_or_create_retrieval_service().execute_index_effect(operation_id, request, checkpoint)

    def query_workspace_index_effect(self, operation_id: str, request: dict[str, str]):
        return self._get_or_create_retrieval_service().query_index_effect_completion(operation_id, request)

    def workspace_index_authority_snapshot(self, operation: str, series_id: str | None, video_id: str | None) -> dict[str, str]:
        return self._get_or_create_retrieval_service().index_authority_snapshot(operation, series_id, video_id)

    def get_context_budget_service(self) -> AgentContextBudgetService:
        with self._lock:
            if self._cached_context_budget_service is None:
                app_settings = load_settings(self._root_dir / "config" / "settings.toml", self._root_dir)
                self._cached_context_budget_service = AgentContextBudgetService(
                    context_loader=self._context_loader,
                    session_store=self.session_store,
                    window_tokens=app_settings.agent_context.window_tokens,
                    reserved_output_tokens=app_settings.agent_context.reserved_output_tokens,
                    warning_threshold_ratio=app_settings.agent_context.warning_threshold_ratio,
                    compact_threshold_ratio=app_settings.agent_context.compact_threshold_ratio,
                    blocking_threshold_ratio=app_settings.agent_context.blocking_threshold_ratio,
                )
            return self._cached_context_budget_service

    def _get_or_create_retrieval_service(self) -> SeriesRetrievalService:
        with self._lock:
            retrieval_service = self._cached_retrieval_service
            if retrieval_service is None:
                retrieval_service = self._build_lazy_retrieval_service()
                self._cached_retrieval_service = retrieval_service
            return retrieval_service

    def _build_lazy_retrieval_service(self):
        if self._rag_model_manager is None:
            return self._build_series_retrieval_service()
        return _RagModelAwareRetrievalService(
            rag_model_manager=self._rag_model_manager,
            factory=self._build_series_retrieval_service,
            settings_loader=lambda: load_settings(self._root_dir / "config" / "settings.toml", self._root_dir),
        )

    def _build_series_retrieval_service(self) -> SeriesRetrievalService:
        retrieval_service = _agent_memory_export("SeriesRetrievalService")
        return retrieval_service(
            workspace=self._workspace,
            db_uri=str(_resolve_agent_lancedb_uri(self._root_dir)),
            reranker=self._build_reranker(self._resolve_retrieval_device()),
            root_dir=self._root_dir,
            index_admission=self.admit_workspace_index,
        )

    def _resolve_retrieval_device(self) -> str:
        settings_path = self._root_dir / "config" / "settings.toml"
        if not settings_path.exists():
            return "cpu"
        return load_settings(settings_path, self._root_dir).agent_retrieval.embedding_device

    def _build_reranker(self, device: str) -> BGEReranker | None:
        reranker = _agent_memory_export("BGEReranker")
        if self._rag_model_manager is not None:
            if not self._rag_model_manager.is_downloaded("reranker"):
                return None
            return reranker(
                model_name="BAAI/bge-reranker-base",
                cache_dir=str(self._rag_model_manager.local_model_dir("reranker").parent),
                device=device,
            )
        return reranker(device=device, cache_dir=_resolve_local_reranker_cache_dir(self._root_dir))

    def _build_web_search_gateway(self, *, settings, env_settings, api_key: str | None = None):
        web_search_settings = settings.web_search
        if not web_search_settings.enabled:
            return None
        if web_search_settings.provider == "litellm" and web_search_settings.mode == "native":
            registry = ProviderRegistry(self._root_dir)
            active = next(
                item for item in registry.list_readonly(fallback=provider_fallback(self))
                if item.get("is_active") is True
            )
            provider_id = str(active["provider_id"])
            policy = ProviderEgressPolicyStore(self._root_dir)
            manifest = provider_egress_manifest(active, policy)
            endpoint = normalize_openai_base_url(env_settings.base_url)
            host = urlsplit(endpoint).hostname
            if not host:
                raise RuntimeError("web search provider host is unavailable")
            project_id = f"provider:{provider_id}"
            def current_revision(project: str) -> str:
                if project != project_id:
                    return "denied"
                current = registry.get_readonly(provider_id, fallback=provider_fallback(self))
                return provider_egress_manifest(current, ProviderEgressPolicyStore(self._root_dir)).manifest_id
            broker = SecretEgressBroker(self._secret_store, boundary_revision_reader=current_revision)
            def api_key_provider() -> str:
                lease = broker.grant(
                    project_id=project_id, secret_ref=project_id, purpose="web_search",
                    allowed_hosts=(host,), boundary_revision=manifest.manifest_id, ttl_seconds=30,
                )
                try:
                    return broker.materialize_for_sdk(
                        lease, project_id=project_id, purpose="web_search",
                        boundary_revision=manifest.manifest_id, url=endpoint,
                    )
                finally:
                    broker.revoke(lease.lease_id)
            return LiteLLMNativeWebSearchGateway(
                provider=env_settings.provider,
                model=env_settings.model,
                base_url=normalize_openai_base_url(env_settings.base_url),
                api_key=api_key,
                api_key_provider=None if api_key is not None else api_key_provider,
                search_context_size=web_search_settings.search_context_size,
                egress_guard=build_active_provider_egress_guard(
                    self._root_dir,
                    endpoint=normalize_openai_base_url(env_settings.base_url),
                ),
            )
        raise RuntimeError(
            "Unsupported web_search provider/mode: "
            f"{web_search_settings.provider}/{web_search_settings.mode}"
        )


def _resolve_agent_lancedb_uri(root_dir: Path) -> Path:
    override = os.environ.get("CHRIPTMAS_REPLAY_AGENT_LANCEDB_URI", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name != "nt":
        return root_dir / "data" / "agent_graph" / "lancedb"

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return root_dir / "data" / "agent_graph" / "lancedb"
    workspace_key = hashlib.sha1(str(root_dir.resolve()).encode("utf-8")).hexdigest()[:12]
    return Path(local_app_data) / "Chriptmas_Replay" / "agent_graph" / workspace_key / "lancedb"


class _RagModelAwareRetrievalService:
    def __init__(
        self,
        *,
        rag_model_manager: RagModelManager,
        factory: Callable[[], SeriesRetrievalService],
        settings_loader: Callable[[], object],
    ) -> None:
        self._rag_model_manager = rag_model_manager
        self._factory = factory
        self._settings_loader = settings_loader
        self._service: SeriesRetrievalService | None = None
        self._signature: tuple[bool, bool, str] | None = None
        self._lock = Lock()

    def search(self, **kwargs):
        return self._require_service().search(**kwargs)

    def default_max_hits(self) -> int:
        return self._settings_loader().agent_retrieval.max_hits

    def refresh(self) -> None:
        self._require_service().refresh()

    def refresh_all(self) -> None:
        self._require_service().refresh_all()

    def upsert_video(self, series_id: str, video_id: str) -> None:
        self._require_service().upsert_video(series_id, video_id)

    def delete_video(self, series_id: str, video_id: str) -> None:
        self._require_service().delete_video(series_id, video_id)

    def delete_series(self, series_id: str) -> None:
        self._require_service().delete_series(series_id)

    def execute_index_effect(self, operation_id: str, request: dict[str, str], checkpoint):
        return self._require_service().execute_index_effect(operation_id, request, checkpoint)

    def query_index_effect_completion(self, operation_id: str, request: dict[str, str]):
        return self._require_service().query_index_effect_completion(operation_id, request)

    def index_authority_snapshot(self, operation: str, series_id: str | None, video_id: str | None) -> dict[str, str]:
        return self._require_service().index_authority_snapshot(operation, series_id, video_id)

    def invalidate(self) -> None:
        with self._lock:
            if self._service is not None:
                self._service.invalidate()

    def _require_service(self) -> SeriesRetrievalService:
        with self._lock:
            if not self._rag_model_manager.is_downloaded("embedding"):
                raise RuntimeError(RAG_EMBEDDING_REQUIRED_MESSAGE)
            signature = self._build_signature()
            if self._service is None or self._signature != signature:
                self._service = self._factory()
                self._signature = signature
            return self._service

    def _build_signature(self) -> tuple[bool, bool, str]:
        settings = self._settings_loader()
        return (
            self._rag_model_manager.is_downloaded("embedding"),
            self._rag_model_manager.is_downloaded("reranker"),
            settings.agent_retrieval.embedding_device,
        )


def _resolve_local_reranker_cache_dir(root_dir: Path) -> str | None:
    cache_dir = root_dir / "data" / "models" / "fastembed"
    local_dir = cache_dir / "models--BAAI--bge-reranker-base"
    if local_dir.is_dir():
        return str(cache_dir)
    return None
