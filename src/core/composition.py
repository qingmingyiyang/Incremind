from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .application_skill import (
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillConsumerRuntime,
    ApplicationSkillPackage,
    ApplicationSkillResolver,
    ApplicationSkillSource,
    ObjectStoreApplicationSkillTraceRepository,
)
from .ingestion_core import ObjectStoreSourceRegistrar
from .aggregate_repository_factory import AggregateRepositoryFactory
from .document_engine import ObjectStoreDocumentRepository, SQLiteDocumentRepository
from .job_runner import ObjectStoreJobRepository, RoutedJobRepository, SQLiteJobStore
from .memory_core import ObjectStoreMemoryCandidateRepository, ObjectStoreMemoryStore, SQLiteMemoryReader
from .model_gateway import ObjectStoreModelRequestRepository, ObjectStoreModelResultRepository
from .product_core.answer_feedback import (
    CreateAnswerFeedbackFromReviewedCandidate,
)
from .product_core.ports import (
    ContractCatalogPort,
    IndexHealth,
    IndexHealthPort,
    PlatformHealth,
    PlatformHealthPort,
    StorageBoundary,
    StorageBoundaryPort,
)
from .product_core.answer_model_request import (
    CreateAnswerModelRequestFromRecallResult,
)
from .product_core.model_result_document_handoff import (
    CreateDocumentFromModelResult,
)
from .product_core.workbench_document_draft import (
    CreateDocumentDraftFromWorkbenchSelection,
)
from .product_core.skill_document_handoff import (
    CreateDocumentFromProjectSkill,
)
from .product_core.project_document_model_request import (
    CreateProjectDocumentModelRequest,
)
from .product_core.local_extractive_answer import (
    CreateLocalExtractiveAnswerFromModelRequest,
)
from .product_core.four_layer_memory_candidate_import import (
    ImportFourLayerMemoryCandidatesFromProviderOutput,
)
from .product_core.workbench_document_memory_candidate import (
    CreateMemoryCandidateFromWorkbenchDocument,
)
from .product_core.source_output_memory_candidate import (
    CreateMemoryCandidateFromSourceOutput,
)
from .product_core.source_content_qa_recall import (
    CreateSourceContentQaRecall,
)
from .product_core.workbench_document_qa_recall import (
    CreateWorkbenchDocumentQaRecall,
)
from .product_core.model_result_memory_candidate import (
    CreateMemoryCandidateFromModelResult,
)
from .product_core.platform_recovery_action import (
    CreatePlatformRecoveryActions,
)
from .product_core.project_memory_recall import (
    CreateProjectMemoryRecall,
)
from .product_core.project_skill_recall import (
    CreateProjectSkillFirstRecall,
)
from .product_core.source_file_authorization import (
    AuthorizeLocalAudioFileForSource,
    AuthorizeLocalDocumentFileForSource,
    AuthorizeLocalImageFileForSource,
    AuthorizeLocalVideoFileForSource,
)
from .product_core.local_asr_provider_settings import (
    GetLocalAsrProviderSettings,
    RunConfiguredLocalAsrProviderForSource,
    SaveLocalAsrProviderSettings,
)
from .product_core.local_document_text_extractor_settings import (
    GetLocalDocumentTextExtractorSettings,
    RunConfiguredLocalDocumentTextExtractorForSource,
    SaveLocalDocumentTextExtractorSettings,
)
from .product_core.local_video_provider_settings import (
    GetLocalVideoProviderSettings,
    RunConfiguredLocalVideoProviderForSource,
    SaveLocalVideoProviderSettings,
)
from .product_core.project_skill_overview import (
    GetProjectSkillOverview,
)
from .product_core.health import (
    GetProductHealth,
)
from .product_core.library_overview import (
    GetLibraryOverview,
)
from .product_core.local_ocr_provider_settings import (
    GetLocalOcrProviderSettings,
    RunConfiguredLocalOcrProviderForSource,
    SaveLocalOcrProviderSettings,
)
from .product_core.runtime_readiness import (
    GetProductRuntimeReadiness,
)
from .product_core.phase6_readiness import (
    GetPhase6Readiness,
    PlatformCapabilityProbePort,
)
from .product_core.local_document_text_extractor import (
    LocalCommandDocumentTextExtractor,
)
from .product_core.local_asr_provider import (
    LocalCommandAudioTranscriptionAdapter,
)
from .product_core.local_ocr_provider import (
    LocalCommandImageOcrAdapter,
)
from .product_core.local_video_provider import (
    LocalCommandVideoFrameExtractionAdapter,
)
from .product_core.persona import (
    ObjectStorePersonaRepository,
)
from .product_core.video_workflow_endpoint import (
    ServeAudioAssetTranscriptionEndpoint,
    ServeAuthorizedBilibiliDownloadEndpoint,
    ServeBilibiliVideoDownloadPlanEndpoint,
    ServeTranscriptSummaryEndpoint,
    ServeVideoAudioExtractionEndpoint,
)
from .product_core.memory_candidate_review import (
    ReviewMemoryCandidate,
)
from .product_core.memory_candidate_review_endpoint import (
    ServeMemoryCandidateReviewEndpoint,
)
from .product_core.source_output_memory_candidate_endpoint import (
    ServeSourceOutputMemoryCandidateEndpoint,
)
from .product_core.memory_publication import (
    PublishStagingAtomToMemory,
    RollbackPublishedAtomMemory,
)
from .product_core.memory_publication_endpoint import (
    ServeMemoryPublicationEndpoint,
)
from .product_core.source_job_memory_loop import (
    SourceJobMemoryLoop,
)
from .product_core.phase6_readiness import REQUIRED_PHASE6_CAPABILITIES
from .project_skill_core import ObjectStoreProjectSkillRepository, SQLiteProjectSkillRepository
from .search_and_recall import ObjectStoreRecallIndex, ObjectStoreRecallRepository
from .storage_provider import JsonObjectStore, ObjectStoreAnswerFeedbackRepository, RebuildStorageSettings
from .storage_provider import ObjectStorePlatformRecoveryActionRepository


@dataclass(frozen=True, slots=True)
class FilesystemContractCatalog(ContractCatalogPort):
    contract_root: Path

    def contract_names(self) -> tuple[str, ...]:
        return tuple(sorted(path.name for path in self.contract_root.glob("*.schema.json") if path.is_file()))


@dataclass(frozen=True, slots=True)
class ConfiguredStorageBoundary(StorageBoundaryPort):
    settings: RebuildStorageSettings

    def storage_boundary(self) -> StorageBoundary:
        return StorageBoundary(
            rebuild_root=str(self.settings.rebuild_root),
            legacy_root=str(self.settings.legacy_root),
            legacy_access=self.settings.legacy_access,
            isolated=self.settings.isolated,
            namespace_id=self.settings.namespace_id,
            storage_version=self.settings.storage_version,
            app_root_uri=self.settings.app_root_uri,
            root_uri=self.settings.root_uri,
            reference_root_uri=self.settings.reference_root_uri,
            backup_ready=self.settings.backup_ready,
        )


def _application_skill_runtime(
    *,
    repository_root: Path,
    runtime_root: Path,
    object_store: JsonObjectStore,
    external_packages: Callable[[str], tuple[ApplicationSkillPackage, ...]] | None = None,
) -> ApplicationSkillConsumerRuntime:
    trace_store = ObjectStoreApplicationSkillTraceRepository(object_store)
    resolver = ApplicationSkillResolver(
        ApplicationSkillBindingRegistry(object_store),
        trace_store=trace_store,
    )
    return ApplicationSkillConsumerRuntime(
        catalog=ApplicationSkillCatalog(),
        sources=(
            ApplicationSkillSource(
                "bundled",
                repository_root / "config" / "application-skills",
                "bundled",
            ),
            ApplicationSkillSource(
                "user",
                runtime_root / "skills",
                "user",
            ),
        ),
        resolver=resolver,
        external_packages=external_packages,
    )


@dataclass(frozen=True, slots=True)
class FilesystemRecallIndexHealth(IndexHealthPort):
    """Read-only health adapter for the persistent Recall index files."""

    rebuild_root: Path
    namespace_id: str

    def index_health(self) -> IndexHealth:
        manifest = self._read_manifest()
        if manifest is None:
            return IndexHealth(
                status="degraded",
                manifest_present=False,
                backend_kind=None,
                entry_count=0,
                traceable=False,
                vector_enabled=False,
            )
        backend_kind = manifest.get("backend_kind")
        vector = manifest.get("vector")
        vector_enabled = bool(vector.get("enabled")) if isinstance(vector, Mapping) else False
        if backend_kind == "sqlite_fts5":
            entry_count = _sqlite_fts5_source_count(manifest)
            traceable = _sqlite_fts5_active_manifest_traceable(manifest)
            ready = entry_count > 0 and traceable and not vector_enabled
        else:
            entries = self._read_entries()
            manifest_count = manifest.get("entry_count")
            entry_count = (
                manifest_count
                if isinstance(manifest_count, int) and not isinstance(manifest_count, bool)
                else len(entries)
            )
            traceable = (
                bool(entries)
                and len(entries) == entry_count
                and all(_index_entry_traceable(entry) for entry in entries)
            )
            ready = (
                backend_kind == "object_store_lexical"
                and entry_count > 0
                and traceable
                and not vector_enabled
            )
        return IndexHealth(
            status="ready" if ready else "degraded",
            manifest_present=True,
            backend_kind=backend_kind if isinstance(backend_kind, str) else None,
            entry_count=entry_count,
            traceable=traceable,
            vector_enabled=vector_enabled,
        )

    def _read_manifest(self) -> Mapping[str, object] | None:
        path = self._collection_path("recall_index_manifests") / "active.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, Mapping) else None

    def _read_entries(self) -> tuple[Mapping[str, object], ...]:
        directory = self._collection_path("recall_index_entries")
        if not directory.exists():
            return ()
        entries: list[Mapping[str, object]] = []
        for path in sorted(directory.glob("*.json")):
            if path.name.endswith(".meta.json"):
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, Mapping):
                entries.append(payload)
        return tuple(entries)

    def _collection_path(self, collection: str) -> Path:
        return self.rebuild_root / "objects" / self.namespace_id / collection


@dataclass(frozen=True, slots=True)
class FilesystemPlatformCapabilityProbe(PlatformCapabilityProbePort):
    """Fixture-backed platform capability probe for Phase 6 readiness."""

    fixture_root: Path

    def platform_capabilities(self) -> tuple[Mapping[str, object], ...]:
        capabilities: list[Mapping[str, object]] = []
        for path in sorted(self.fixture_root.glob("valid-*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, Mapping):
                capabilities.append(payload)
        return tuple(capabilities)


@dataclass(frozen=True, slots=True)
class FilesystemPlatformHealth(PlatformHealthPort):
    """Read-only Product Health adapter for platform capability fixtures."""

    fixture_root: Path

    def platform_health(self) -> PlatformHealth:
        capabilities = self._capabilities()
        names = tuple(sorted(name for name in (_capability_name(item) for item in capabilities) if name))
        ready = tuple(sorted(name for name, item in _capability_items(capabilities) if _platform_capability_ready(item)))
        degraded = tuple(
            sorted(
                name
                for name, item in _capability_items(capabilities)
                if name in REQUIRED_PHASE6_CAPABILITIES and not _platform_capability_ready(item)
            )
        )
        missing = tuple(name for name in REQUIRED_PHASE6_CAPABILITIES if name not in names)
        os_path_leaks = tuple(
            sorted(name for name, item in _capability_items(capabilities) if _platform_capability_has_os_path(item))
        )
        status = "ready" if not degraded and not missing and not os_path_leaks else "degraded"
        return PlatformHealth(
            status=status,
            capability_count=len(capabilities),
            ready_capabilities=ready,
            degraded_capabilities=degraded,
            missing_capabilities=missing,
            os_path_leaks=os_path_leaks,
        )

    def _capabilities(self) -> tuple[Mapping[str, object], ...]:
        capabilities: list[Mapping[str, object]] = []
        for path in sorted(self.fixture_root.glob("valid-*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, Mapping):
                capabilities.append(payload)
        return tuple(capabilities)


def _index_entry_traceable(entry: Mapping[str, object]) -> bool:
    source_refs = entry.get("source_refs")
    if not isinstance(source_refs, list) or not source_refs:
        return False
    return all(isinstance(ref, str) and "#" in ref for ref in source_refs)


def _sqlite_fts5_source_count(manifest: Mapping[str, object]) -> int:
    source_count = manifest.get("source_count")
    return source_count if isinstance(source_count, int) and not isinstance(source_count, bool) else 0


def _sqlite_fts5_active_manifest_traceable(manifest: Mapping[str, object]) -> bool:
    if manifest.get("status") != "active":
        return False
    if manifest.get("index_role") != "active_manifest":
        return False
    if manifest.get("source") != "verified_index_rebuild_job":
        return False
    if not _non_empty_string(manifest.get("source_fingerprint")):
        return False
    source_refs = manifest.get("source_refs")
    if not isinstance(source_refs, list) or not source_refs:
        return False
    if _sqlite_fts5_source_count(manifest) != len(source_refs):
        return False
    if not all(isinstance(ref, str) and "#rev:" in ref for ref in source_refs):
        return False
    verification_ref = manifest.get("verification_ref")
    if not (
        isinstance(verification_ref, str)
        and verification_ref.startswith("crp://")
        and "/recall/index-verifications/" in verification_ref
    ):
        return False
    if not _non_empty_string(manifest.get("verified_job_id")):
        return False
    if not _non_empty_string(manifest.get("activated_by")):
        return False
    if not _non_empty_string(manifest.get("activated_at")):
        return False
    fts = manifest.get("fts")
    if not isinstance(fts, Mapping):
        return False
    if fts.get("engine") != "sqlite" or fts.get("module") != "fts5" or fts.get("ranker") != "bm25":
        return False
    filters = fts.get("filters")
    if not isinstance(filters, list):
        return False
    return all(item in filters for item in ("project_id", "layer", "trust_status"))


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _capability_items(
    capabilities: tuple[Mapping[str, object], ...],
) -> tuple[tuple[str, Mapping[str, object]], ...]:
    return tuple((name, item) for item in capabilities if (name := _capability_name(item)))


def _capability_name(capability: Mapping[str, object]) -> str | None:
    name = capability.get("name")
    return name if isinstance(name, str) else None


def _platform_capability_ready(capability: Mapping[str, object]) -> bool:
    if capability.get("available") is True:
        return capability.get("error") is None and capability.get("permission") != "denied"
    return False


def _platform_capability_has_os_path(capability: Mapping[str, object]) -> bool:
    uri = capability.get("provided_uri")
    return isinstance(uri, str) and ("\\" in uri or uri.startswith("file:"))


def build_product_health(
    repository_root: Path,
    *,
    config_path: Path | None = None,
) -> GetProductHealth:
    """Compose the read-only R002 health use case without creating storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    return GetProductHealth(
        FilesystemContractCatalog(repository_root / "core-contracts" / "rebuild"),
        ConfiguredStorageBoundary(settings),
        FilesystemRecallIndexHealth(settings.rebuild_root, settings.namespace_id),
        FilesystemPlatformHealth(
            repository_root / "core-contracts" / "rebuild" / "fixtures" / "platform_capability"
        ),
    )


@dataclass(frozen=True, slots=True)
class ObjectStoreLibraryOverviewReader:
    object_store: JsonObjectStore
    document_repository: ObjectStoreDocumentRepository | SQLiteDocumentRepository | None = None
    job_repository: object | None = None
    memory_repository: object | None = None
    project_skill_repository: object | None = None

    def sources(self) -> tuple[Mapping[str, object], ...]:
        items: list[Mapping[str, object]] = []
        for item in self.object_store.list("sources"):
            projected = dict(item)
            source_id = str(projected.get("id") or "")
            projected["revision"] = self.object_store.revision("sources", source_id) if source_id else 0
            items.append(projected)
        return tuple(items)

    def documents(self) -> tuple[Mapping[str, object], ...]:
        if self.document_repository is not None:
            return tuple(dict(item) for item in self.document_repository.list())
        return tuple(
            dict(item)
            for item in self.object_store.list("documents")
            if item.get("status") != "archived"
        )

    def memory_candidates(self) -> tuple[Mapping[str, object], ...]:
        items: list[Mapping[str, object]] = []
        for item in self.object_store.list("memory_candidates"):
            projected = dict(item)
            candidate_id = str(projected.get("id") or "")
            projected["candidate_revision"] = (
                self.object_store.revision("memory_candidates", candidate_id)
                if candidate_id
                else 0
            )
            items.append(projected)
        return tuple(items)

    def external_agent_review_drafts(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list("external_agent_review_drafts"))

    def memory_objects(self) -> tuple[Mapping[str, object], ...]:
        memory_objects = (
            tuple(
                {**dict(item), "layer": str(item.get("layer") or layer)}
                for layer in ("atom", "scenario", "series_memory")
                for item in self.memory_repository.list(layer)
            )
            if self.memory_repository is not None
            else (
                *tuple(dict(item) for item in self.object_store.list("memory_atoms")),
                *tuple(dict(item) for item in self.object_store.list("memory_scenarios")),
                *tuple(dict(item) for item in self.object_store.list("memory_series_memory")),
            )
        )
        project_skills = (
            tuple(
                {**dict(item), "layer": str(item.get("layer") or "project_skill")}
                for item in self.project_skill_repository.list_all()
            )
            if self.project_skill_repository is not None
            else tuple(dict(item) for item in self.object_store.list("project_skills"))
        )
        return (
            *memory_objects,
            *project_skills,
        )

    def capture_job_id(self, source_id: str) -> str | None:
        expected_job_id = f"job-capture-{source_id}"
        return self._verified_source_job_id(
            source_id=source_id,
            expected_job_id=expected_job_id,
            expected_job_type="capture",
        )

    def user_job_id(self, source_id: str) -> str | None:
        intake_job_id = self._verified_source_job_id(
            source_id=source_id,
            expected_job_id=f"job-intake-{source_id}",
            expected_job_type="workbench_auto_intake",
        )
        return intake_job_id or self.capture_job_id(source_id)

    def _verified_source_job_id(
        self,
        *,
        source_id: str,
        expected_job_id: str,
        expected_job_type: str,
    ) -> str | None:
        if self.job_repository is None:
            return None
        job = self.job_repository.get(expected_job_id)
        if job is None:
            return None
        if (
            job.get("id") != expected_job_id
            or job.get("source_id") != source_id
            or job.get("job_type") != expected_job_type
        ):
            return None
        return expected_job_id


def build_library_overview(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> GetLibraryOverview:
    """Compose read-only Library overview against controlled temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return GetLibraryOverview(
        ObjectStoreLibraryOverviewReader(object_store),
        namespace_id=settings.namespace_id,
    )


def build_phase6_readiness(
    repository_root: Path,
    *,
    config_path: Path | None = None,
) -> GetPhase6Readiness:
    """Compose Phase 6 entry readiness without touching legacy storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    return GetPhase6Readiness(
        storage=ConfiguredStorageBoundary(settings),
        index=FilesystemRecallIndexHealth(settings.rebuild_root, settings.namespace_id),
        platform=FilesystemPlatformCapabilityProbe(
            repository_root / "core-contracts" / "rebuild" / "fixtures" / "platform_capability"
        ),
    )


def build_product_runtime_readiness(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> GetProductRuntimeReadiness:
    """Compose the executable Phase 2 runtime readiness smoke.

    The caller must pass a controlled temporary runtime root. This function
    does not read or write the real legacy library.
    """

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    sources = ObjectStoreSourceRegistrar(object_store, namespace_id=settings.namespace_id)
    jobs = RoutedJobRepository(
        legacy=ObjectStoreJobRepository(object_store),
        sqlite=SQLiteJobStore(runtime_root / ".rebuild-data" / "jobs.sqlite3"),
        sqlite_job_types=frozenset({"extract_memory"}),
    )
    memory = ObjectStoreMemoryStore(object_store)
    recall_index = ObjectStoreRecallIndex(object_store)
    loop = SourceJobMemoryLoop(
        source_registrar=sources,
        job_repository=jobs,
        memory_reader=memory,
        memory_writer=memory,
        namespace_id=settings.namespace_id,
    )
    return GetProductRuntimeReadiness(loop=loop, jobs=jobs, memory=memory, recall_index=recall_index)


def build_document_repository(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> ObjectStoreDocumentRepository | SQLiteDocumentRepository:
    """Compose the Phase 3 document repository against controlled temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return AggregateRepositoryFactory(
        runtime_root=runtime_root,
        namespace_id=settings.namespace_id,
        json_store=object_store,
    ).document_repository()


def build_project_skill_repository(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> ObjectStoreProjectSkillRepository | SQLiteProjectSkillRepository:
    """Compose the Phase 4 Project Skill repository against controlled temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return AggregateRepositoryFactory(
        runtime_root=runtime_root,
        namespace_id=settings.namespace_id,
        json_store=object_store,
    ).project_skill_repository()


def build_project_skill_overview(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> GetProjectSkillOverview:
    """Compose a read-only Phase 13 Project Skill overview against controlled temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    skills = ObjectStoreProjectSkillRepository(object_store, namespace_id=settings.namespace_id)
    return GetProjectSkillOverview(skills)


def build_skill_document_handoff(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> CreateDocumentFromProjectSkill:
    """Compose Phase 4 Skill → Document handoff against controlled temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    skills = ObjectStoreProjectSkillRepository(object_store, namespace_id=settings.namespace_id)
    documents = ObjectStoreDocumentRepository(object_store, namespace_id=settings.namespace_id)
    return CreateDocumentFromProjectSkill(skills=skills, documents=documents)


def build_project_document_model_request(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
    external_packages: Callable[[str], tuple[ApplicationSkillPackage, ...]] | None = None,
) -> CreateProjectDocumentModelRequest:
    """Compose Project Skill + Application Skill → local document Model Request."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    project_skills = AggregateRepositoryFactory(
        runtime_root=runtime_root,
        namespace_id=settings.namespace_id,
        json_store=object_store,
    ).project_skill_repository()
    return CreateProjectDocumentModelRequest(
        project_skills=project_skills,
        model_requests=ObjectStoreModelRequestRepository(object_store),
        application_skills=_application_skill_runtime(
            repository_root=repository_root,
            runtime_root=runtime_root,
            object_store=object_store,
            external_packages=external_packages,
        ),
        namespace_id=settings.namespace_id,
    )


def build_recall_repository(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> ObjectStoreRecallRepository:
    """Compose the Phase 5 recall repository against controlled temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return ObjectStoreRecallRepository(object_store, namespace_id=settings.namespace_id)


def build_project_skill_first_recall(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> CreateProjectSkillFirstRecall:
    """Compose Phase 5 Project Skill-first recall against controlled temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    skills = ObjectStoreProjectSkillRepository(object_store, namespace_id=settings.namespace_id)
    recalls = ObjectStoreRecallRepository(object_store, namespace_id=settings.namespace_id)
    return CreateProjectSkillFirstRecall(skills=skills, recalls=recalls)


def build_project_memory_recall(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> CreateProjectMemoryRecall:
    """Compose Phase 5 same-project memory evidence recall against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    factory = AggregateRepositoryFactory(
        runtime_root=runtime_root,
        namespace_id=settings.namespace_id,
        json_store=object_store,
    )
    skills = factory.project_skill_repository()
    resolution = factory.memory_publication_authority_resolution()
    memory = SQLiteMemoryReader(resolution.records) if resolution.records is not None else ObjectStoreMemoryStore(object_store)
    recalls = ObjectStoreRecallRepository(object_store, namespace_id=settings.namespace_id)
    persona = ObjectStorePersonaRepository(object_store)
    return CreateProjectMemoryRecall(skills=skills, memory=memory, recalls=recalls, persona=persona)


def build_answer_model_request_from_recall(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
    external_packages: Callable[[str], tuple[ApplicationSkillPackage, ...]] | None = None,
) -> CreateAnswerModelRequestFromRecallResult:
    """Compose Phase 5 Recall Result → local Model Request against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    recalls = ObjectStoreRecallRepository(object_store, namespace_id=settings.namespace_id)
    model_requests = ObjectStoreModelRequestRepository(object_store)
    return CreateAnswerModelRequestFromRecallResult(
        recalls=recalls,
        model_requests=model_requests,
        application_skills=_application_skill_runtime(
            repository_root=repository_root,
            runtime_root=runtime_root,
            object_store=object_store,
            external_packages=external_packages,
        ),
        namespace_id=settings.namespace_id,
    )


def build_workbench_document_qa_recall(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
    external_packages: Callable[[str], tuple[ApplicationSkillPackage, ...]] | None = None,
) -> CreateWorkbenchDocumentQaRecall:
    """Compose selected Workbench Document → QA recall against controlled temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    documents = ObjectStoreDocumentRepository(object_store, namespace_id=settings.namespace_id)
    recalls = ObjectStoreRecallRepository(object_store, namespace_id=settings.namespace_id)
    model_requests = ObjectStoreModelRequestRepository(object_store)
    answer_requests = CreateAnswerModelRequestFromRecallResult(
        recalls=recalls,
        model_requests=model_requests,
        application_skills=_application_skill_runtime(
            repository_root=repository_root,
            runtime_root=runtime_root,
            object_store=object_store,
            external_packages=external_packages,
        ),
        namespace_id=settings.namespace_id,
    )
    return CreateWorkbenchDocumentQaRecall(
        documents=documents,
        recalls=recalls,
        answer_requests=answer_requests,
        namespace_id=settings.namespace_id,
    )


def build_source_content_qa_recall(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
    external_packages: Callable[[str], tuple[ApplicationSkillPackage, ...]] | None = None,
) -> CreateSourceContentQaRecall:
    """Compose completed Source content_read -> local QA recall request."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    recalls = ObjectStoreRecallRepository(object_store, namespace_id=settings.namespace_id)
    model_requests = ObjectStoreModelRequestRepository(object_store)
    answer_requests = CreateAnswerModelRequestFromRecallResult(
        recalls=recalls,
        model_requests=model_requests,
        application_skills=_application_skill_runtime(
            repository_root=repository_root,
            runtime_root=runtime_root,
            object_store=object_store,
            external_packages=external_packages,
        ),
        namespace_id=settings.namespace_id,
    )
    return CreateSourceContentQaRecall(
        object_store=object_store,
        recalls=recalls,
        answer_requests=answer_requests,
        namespace_id=settings.namespace_id,
    )


def build_model_result_repository(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> ObjectStoreModelResultRepository:
    """Compose local Model Result persistence against controlled temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return ObjectStoreModelResultRepository(object_store)


def build_local_extractive_answer(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> CreateLocalExtractiveAnswerFromModelRequest:
    """Compose local answer Model Request -> completed local Model Result."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return CreateLocalExtractiveAnswerFromModelRequest(
        model_requests=ObjectStoreModelRequestRepository(object_store),
        model_results=ObjectStoreModelResultRepository(object_store),
    )


def build_model_result_document_handoff(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> CreateDocumentFromModelResult:
    """Compose Phase 5 completed Model Result → Document draft handoff."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    model_requests = ObjectStoreModelRequestRepository(object_store)
    model_results = ObjectStoreModelResultRepository(object_store)
    documents = ObjectStoreDocumentRepository(object_store, namespace_id=settings.namespace_id)
    return CreateDocumentFromModelResult(
        model_requests=model_requests,
        model_results=model_results,
        documents=documents,
    )


def build_workbench_document_draft_handoff(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> CreateDocumentDraftFromWorkbenchSelection:
    """Compose Phase 11 Workbench selected Source evidence → editable Document draft."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    documents = ObjectStoreDocumentRepository(object_store, namespace_id=settings.namespace_id)
    return CreateDocumentDraftFromWorkbenchSelection(documents=documents)


def build_workbench_document_memory_candidate_handoff(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> CreateMemoryCandidateFromWorkbenchDocument:
    """Compose Phase 11 selected Workbench Document → reviewable Memory Candidate."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    documents = ObjectStoreDocumentRepository(object_store, namespace_id=settings.namespace_id)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    return CreateMemoryCandidateFromWorkbenchDocument(
        documents=documents,
        candidates=candidates,
        namespace_id=settings.namespace_id,
    )


def build_source_output_memory_candidate_handoff(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> CreateMemoryCandidateFromSourceOutput:
    """Compose completed Source outputs -> reviewable Memory Candidate handoff."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return CreateMemoryCandidateFromSourceOutput(
        object_store,
        candidates=ObjectStoreMemoryCandidateRepository(object_store),
        namespace_id=settings.namespace_id,
    )


def build_four_layer_memory_candidate_import(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> ImportFourLayerMemoryCandidatesFromProviderOutput:
    """Compose AI provider JSON output -> reviewable four-layer Memory Candidates."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return ImportFourLayerMemoryCandidatesFromProviderOutput(
        object_store,
        candidates=ObjectStoreMemoryCandidateRepository(object_store),
        namespace_id=settings.namespace_id,
    )




def build_source_image_authorization(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> AuthorizeLocalImageFileForSource:
    """Compose user-authorized image reference binding against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return AuthorizeLocalImageFileForSource(object_store, namespace_id=settings.namespace_id)


def build_source_audio_authorization(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> AuthorizeLocalAudioFileForSource:
    """Compose user-authorized audio reference binding against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return AuthorizeLocalAudioFileForSource(object_store, namespace_id=settings.namespace_id)


def build_source_video_authorization(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> AuthorizeLocalVideoFileForSource:
    """Compose user-authorized video reference binding against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return AuthorizeLocalVideoFileForSource(object_store, namespace_id=settings.namespace_id)


def build_source_document_authorization(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> AuthorizeLocalDocumentFileForSource:
    """Compose user-authorized PDF / Word reference binding against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return AuthorizeLocalDocumentFileForSource(object_store, namespace_id=settings.namespace_id)


def build_local_command_document_text_extractor(
    repository_root: Path,
    *,
    runtime_root: Path,
    command: tuple[str, ...],
    enabled: bool = False,
    provider_name: str = "local-command-document-text",
    timeout_seconds: float = 60.0,
    config_path: Path | None = None,
) -> LocalCommandDocumentTextExtractor:
    """Compose a default-off local command document text extractor."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    _ = runtime_root
    return LocalCommandDocumentTextExtractor(
        command=command,
        enabled=enabled,
        provider_name=provider_name,
        timeout_seconds=timeout_seconds,
    )


def build_local_command_ocr_adapter(
    repository_root: Path,
    *,
    runtime_root: Path,
    command: tuple[str, ...],
    enabled: bool = False,
    provider_name: str = "local-command-ocr",
    config_path: Path | None = None,
) -> LocalCommandImageOcrAdapter:
    """Compose a default-off local command OCR adapter against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return LocalCommandImageOcrAdapter(
        object_store=object_store,
        command=command,
        enabled=enabled,
        provider_name=provider_name,
    )


def build_local_command_asr_adapter(
    repository_root: Path,
    *,
    runtime_root: Path,
    command: tuple[str, ...],
    enabled: bool = False,
    provider_name: str = "local-command-asr",
    config_path: Path | None = None,
) -> LocalCommandAudioTranscriptionAdapter:
    """Compose a default-off local command ASR adapter against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return LocalCommandAudioTranscriptionAdapter(
        object_store=object_store,
        command=command,
        enabled=enabled,
        provider_name=provider_name,
    )


def build_local_command_video_adapter(
    repository_root: Path,
    *,
    runtime_root: Path,
    command: tuple[str, ...],
    enabled: bool = False,
    provider_name: str = "local-command-video",
    config_path: Path | None = None,
) -> LocalCommandVideoFrameExtractionAdapter:
    """Compose a default-off local command video adapter against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return LocalCommandVideoFrameExtractionAdapter(
        object_store=object_store,
        command=command,
        enabled=enabled,
        provider_name=provider_name,
    )


def build_local_ocr_provider_settings(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> GetLocalOcrProviderSettings:
    """Compose read-only local OCR Provider settings against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return GetLocalOcrProviderSettings(object_store)


def build_local_asr_provider_settings(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> GetLocalAsrProviderSettings:
    """Compose read-only local ASR Provider settings against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return GetLocalAsrProviderSettings(object_store)


def build_local_video_provider_settings(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> GetLocalVideoProviderSettings:
    """Compose read-only local video Provider settings against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return GetLocalVideoProviderSettings(object_store)


def build_local_document_text_extractor_settings(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> GetLocalDocumentTextExtractorSettings:
    """Compose read-only local document text extractor settings against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return GetLocalDocumentTextExtractorSettings(object_store)


def build_local_ocr_provider_settings_writer(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> SaveLocalOcrProviderSettings:
    """Compose local OCR Provider settings writer against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return SaveLocalOcrProviderSettings(object_store)


def build_local_asr_provider_settings_writer(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> SaveLocalAsrProviderSettings:
    """Compose local ASR Provider settings writer against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return SaveLocalAsrProviderSettings(object_store)


def build_local_video_provider_settings_writer(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> SaveLocalVideoProviderSettings:
    """Compose local video Provider settings writer against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return SaveLocalVideoProviderSettings(object_store)


def build_local_document_text_extractor_settings_writer(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> SaveLocalDocumentTextExtractorSettings:
    """Compose local document text extractor settings writer against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return SaveLocalDocumentTextExtractorSettings(object_store)


def build_configured_local_ocr_provider_run(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> RunConfiguredLocalOcrProviderForSource:
    """Compose stored-settings local OCR execution against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return RunConfiguredLocalOcrProviderForSource(object_store, namespace_id=settings.namespace_id)


def build_configured_local_asr_provider_run(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> RunConfiguredLocalAsrProviderForSource:
    """Compose stored-settings local ASR execution against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return RunConfiguredLocalAsrProviderForSource(object_store, namespace_id=settings.namespace_id)


def build_configured_local_video_provider_run(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> RunConfiguredLocalVideoProviderForSource:
    """Compose stored-settings local video execution against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return RunConfiguredLocalVideoProviderForSource(object_store, namespace_id=settings.namespace_id)


def build_configured_local_document_text_extractor_run(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> RunConfiguredLocalDocumentTextExtractorForSource:
    """Compose stored-settings local document text extraction against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return RunConfiguredLocalDocumentTextExtractorForSource(object_store, namespace_id=settings.namespace_id)


def build_model_result_memory_candidate_handoff(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> CreateMemoryCandidateFromModelResult:
    """Compose Phase 5 completed Model Result → reviewable Memory Candidate."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    model_requests = ObjectStoreModelRequestRepository(object_store)
    model_results = ObjectStoreModelResultRepository(object_store)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    return CreateMemoryCandidateFromModelResult(
        model_requests=model_requests,
        model_results=model_results,
        candidates=candidates,
        namespace_id=settings.namespace_id,
    )


def build_memory_candidate_review(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> ReviewMemoryCandidate:
    """Compose Phase 5 Memory Candidate explicit review against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    memory = ObjectStoreMemoryStore(object_store)
    return ReviewMemoryCandidate(candidates=candidates, memory=memory)


def build_memory_candidate_review_endpoint() -> ServeMemoryCandidateReviewEndpoint:
    """Compose the narrow Memory Candidate review endpoint adapter."""

    return ServeMemoryCandidateReviewEndpoint()


def build_source_output_memory_candidate_endpoint() -> ServeSourceOutputMemoryCandidateEndpoint:
    """Compose the narrow Source output -> Memory Candidate endpoint adapter."""

    return ServeSourceOutputMemoryCandidateEndpoint()


def build_bilibili_video_download_plan_endpoint() -> ServeBilibiliVideoDownloadPlanEndpoint:
    """Compose the narrow Bilibili video download plan endpoint adapter."""

    return ServeBilibiliVideoDownloadPlanEndpoint()


def build_authorized_bilibili_download_endpoint() -> ServeAuthorizedBilibiliDownloadEndpoint:
    """Compose the narrow authorized Bilibili downloader endpoint adapter."""

    return ServeAuthorizedBilibiliDownloadEndpoint()


def build_video_audio_extraction_endpoint() -> ServeVideoAudioExtractionEndpoint:
    """Compose the narrow video Source -> audio track endpoint adapter."""

    return ServeVideoAudioExtractionEndpoint()


def build_audio_asset_transcription_endpoint() -> ServeAudioAssetTranscriptionEndpoint:
    """Compose the narrow audio asset -> transcript endpoint adapter."""

    return ServeAudioAssetTranscriptionEndpoint()


def build_transcript_summary_endpoint() -> ServeTranscriptSummaryEndpoint:
    """Compose the narrow transcript output -> summary endpoint adapter."""

    return ServeTranscriptSummaryEndpoint()


def build_memory_publication(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> PublishStagingAtomToMemory:
    """Compose staging Atom -> long-term Memory publication against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return PublishStagingAtomToMemory(
        object_store,
        memory=ObjectStoreMemoryStore(object_store),
        namespace_id=settings.namespace_id,
    )


def build_memory_publication_endpoint() -> ServeMemoryPublicationEndpoint:
    """Compose the narrow staging Atom publication endpoint adapter."""

    return ServeMemoryPublicationEndpoint()


def build_memory_rollback(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> RollbackPublishedAtomMemory:
    """Compose published Atom rollback against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return RollbackPublishedAtomMemory(object_store, namespace_id=settings.namespace_id)


def build_answer_feedback_from_reviewed_candidate(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> CreateAnswerFeedbackFromReviewedCandidate:
    """Compose Phase 5 reviewed Candidate → Answer Feedback against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    recalls = ObjectStoreRecallRepository(object_store, namespace_id=settings.namespace_id)
    model_requests = ObjectStoreModelRequestRepository(object_store)
    model_results = ObjectStoreModelResultRepository(object_store)
    documents = ObjectStoreDocumentRepository(object_store, namespace_id=settings.namespace_id)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    feedback = ObjectStoreAnswerFeedbackRepository(object_store)
    return CreateAnswerFeedbackFromReviewedCandidate(
        recalls=recalls,
        model_requests=model_requests,
        model_results=model_results,
        documents=documents,
        candidates=candidates,
        feedback=feedback,
        namespace_id=settings.namespace_id,
    )


def build_platform_recovery_actions(
    repository_root: Path,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> CreatePlatformRecoveryActions:
    """Compose Product Health degradation -> recovery action persistence against temp storage."""

    config = config_path or repository_root / "config" / "rebuild.toml.example"
    settings = RebuildStorageSettings.from_toml(config, repository_root=repository_root)
    object_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
    )
    return CreatePlatformRecoveryActions(
        actions=ObjectStorePlatformRecoveryActionRepository(object_store),
        namespace_id=settings.namespace_id,
    )
