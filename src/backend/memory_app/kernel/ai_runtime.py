from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from backend.api.ai_turn_runner import AITurnRunner
from backend.api.agent_assignment_resolvers import build_agent_assignment_resolvers
from backend.api.agent_organization_planner import (
    AgentRoleDispatchPlanner,
    MainCoordinationPlanner,
    StewardPlanningPlanner,
)
from backend.memory_app.kernel.agent_organization_runtime import AgentOrganizationRuntime
from backend.memory_app.kernel.agent_runtime_composition import build_agent_runtime_composition
from backend.api.recursive_evolution_composition import (
    RecursiveEvolutionPolicyAuthorityComposition,
    build_recursive_evolution_composition,
    build_recursive_evolution_policy_authority,
)
from backend.api.agent_steward_proposal import (
    AgentStewardProposalBuilder,
    is_valid_steward_proposal,
)
from backend.api.capability_admission import (
    ReviewedCoreCapabilityRegistry,
    RuntimeCapabilityAdmission,
)
from backend.api.codex_hook_composition import build_codex_hook_host
from backend.api.analyze_source_ai_runtime import (
    AnalyzeSourceCapability,
    ArtifactSourceManifestResolver,
    analyze_source_capability_definition,
    disabled_analyze_source_capability,
)

from backend.api.library_query_runtime import build_library_search_service
from backend.api.mcp_runtime import build_mcp_connection_manager
from backend.api.mcp_migration_runtime import MCPApprovedServerMigrationRuntime
from backend.security.mcp_approved_server_migration import MCPApprovedServerMigrationAuthority
from backend.api.plugin_runtime import (
    PluginToolRegistrationManager,
    build_plugin_skill_activation,
    build_plugin_tool_activation,
)
from backend.api.plugin_hands_runtime import (
    build_plugin_hands_registration_manager,
)
from backend.api.plugin_hands_outcome_projection import PluginHandsToolOutcomeProjector
from backend.api.plugin_hook_runtime import PluginHookProjectionManager, build_plugin_hook_runner, plugin_hook_projection_fault_probe
from backend.api.media_hands_runtime import current_media_hands_runtime
from backend.api.ai_profile_resolvers import (
    ProjectAwareCapabilityManifestResolver,
    ProjectAwareContextManifestResolver,
    TurnProjectProfileSnapshotAuthority,
)
from backend.api.application_skill_snapshot import (
    TurnApplicationSkillSnapshotAuthority,
    reviewed_external_application_skill_ids,
)
from backend.api.model_routing_snapshot_authority import TurnModelRoutingSnapshotAuthority
from backend.api.published_project_memory_snapshot import PublishedProjectMemorySnapshotAuthority
from backend.api.personal_world_model_context import TurnWorldStateSnapshotAuthority
from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.world_supervision_runtime import WorldSupervisionRuntime
from backend.api.world_supervision_agent_observer import WorldSupervisionAgentObserver
from backend.api.context_binding_runtime import (
    ContextBindingRegistry,
    TurnContextBindingSnapshotAuthority,
)
from backend.api.context_binding_composition import SQLiteCompilationFactRepository
from backend.api.expert_turn_binding_runtime import build_expert_turn_binding_runtime
from backend.api.expert_memory_proposal_runtime import ExpertMemoryProposalRuntime
from backend.api.expert_turn_e2e_fixture import install_expert_turn_e2e_fixture
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.model_runtime import (
    ModelRuntimeError,
    resolve_frozen_image_generation_gateway,
    resolve_model_gateway_runtime,
)
from backend.api.image_generation_ai_runtime import (
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_GENERATION_OUTCOME,
    ImageGenerationCapability,
    ImageGenerationTurnPlanner,
)
from backend.api.workbench_ai_runtime import (
    WORKBENCH_QUESTION_CAPABILITY,
    WORKBENCH_QUESTION_OUTCOME,
    WorkbenchQuestionCapability,
    WorkbenchQuestionPlanner,
    resolve_workbench_answer_instruction,
)
from backend.api.project_skill_ai_runtime import (
    PROJECT_SKILL_DRAFT_OUTCOME,
    PROJECT_SKILL_EVIDENCE_CAPABILITY,
    PROJECT_SKILL_PROPOSE_CAPABILITY,
    ProjectSkillDraftPlanner,
    ProjectSkillDraftProposalCapability,
    ProjectSkillEvidenceCapability,
)
from backend.api.source_document_ai_runtime import (
    DOCUMENT_DRAFT_PROPOSE_CAPABILITY,
    SOURCE_DOCUMENT_DRAFT_OUTCOME,
    SOURCE_EVIDENCE_CAPABILITY,
    SourceDocumentDraftPlanner,
    SourceDocumentDraftProposalCapability,
    SourceDocumentEvidenceCapability,
)
from backend.api.companion_chat_ai_runtime import (
    COMPANION_CHAT_CONTEXT_CAPABILITY,
    COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY,
    COMPANION_CHAT_OUTCOME,
    CompanionChatTurnPlanner,
    ScopedCompanionChatContextCapability,
    ScopedCompanionChatMessageWriteCapability,
)
from backend.api.companion_vision_ai_runtime import (
    COMPANION_VISION_ANALYZE_CAPABILITY,
    COMPANION_VISION_CONTEXT_CAPABILITY,
    COMPANION_VISION_OUTCOME,
    CompanionVisionTurnPlanner,
    ScopedCompanionVisionAnalyzeCapability,
    ScopedCompanionVisionContextCapability,
)
from backend.api.workbench_input_classifier_ai_runtime import (
    WORKBENCH_INPUT_CLASSIFICATION_CONTEXT_CAPABILITY,
    WORKBENCH_INPUT_CLASSIFICATION_ENHANCE_CAPABILITY,
    WORKBENCH_INPUT_CLASSIFICATION_OUTCOME,
    ScopedWorkbenchInputClassificationContextCapability,
    ScopedWorkbenchInputClassificationEnhanceCapability,
    WorkbenchInputClassificationTurnPlanner,
)
from backend.api.workbench_input_classifier_runtime import (
    INPUT_CLASSIFIER_MODEL_ROUTE,
    build_workbench_input_classifier_runtime,
)
from backend.api.developer_studio_test_lab_ai_runtime import (
    DEVELOPER_STUDIO_TEST_LAB_OUTCOME,
    DeveloperStudioTestLabCapability,
    DeveloperStudioTestLabTurnPlanner,
    developer_studio_test_lab_capability_definition,
)
from backend.api.ppt_master_capability_runtime import ppt_master_fixed_capability_definition
from backend.api.series_intake_ai_runtime import (
    SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY,
    SERIES_INTAKE_ORGANIZE_OUTCOME,
    SeriesIntakeOrganizeCommitCapability,
    SeriesIntakeOrganizeTurnPlanner,
)
from backend.replay.series_workspace import SeriesWorkspace
from backend.api.four_layer_memory_candidate_ai_runtime import (
    FOUR_LAYER_MEMORY_CANDIDATE_OUTCOME,
    FOUR_LAYER_MEMORY_EVIDENCE_CAPABILITY,
    FOUR_LAYER_MEMORY_PROPOSE_CAPABILITY,
    FourLayerMemoryCandidateTurnPlanner,
    ScopedFourLayerMemoryCandidateProposalCapability,
    ScopedFourLayerMemoryEvidenceCapability,
)
from backend.model_route_context import provider_fallback
from backend.providers import ProviderRegistry
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.security.ai_tool_execution_boundary import (
    AIToolExecutionBoundary,
    TurnCapabilityBindingGuard,
)
from backend.security.turn_frozen_authorization import TurnFrozenAuthorizationAuthority
from .policy_runtime import ProductPolicyRuntime
from core.ai_kernel import (
    AgentProfileRegistry,
    CapabilityDefinition,
    MemoryRecallCapability,
    ModelGatewayAgentPlanner,
    ScopedCapabilityRegistry,
    SQLiteAITurnStore,
    SQLiteAgentStore,
    SynchronousAIRuntime,
)
from core.application_skill import (
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillResolver,
    ApplicationSkillSource,
    ObjectStoreApplicationSkillTraceRepository,
)
from core.aggregate_repository_factory import AggregateRepositoryFactory, AggregateRepositoryFactoryError
from core.model_gateway import ModelExecutionControlPort
from core.storage_provider import GeneratedAssetAuthority, SQLiteStructuredRecordStore
from core.source_processing import SourceManifestArtifactRepository
from core.product_core.model_route_registry import ModelRouteRegistry
from core.product_core.four_layer_memory_candidate_import import (
    ImportFourLayerMemoryCandidatesFromProviderOutput,
    serialize_four_layer_memory_candidate_import,
)
from core.product_core.four_layer_memory_prompt import build_four_layer_memory_prompt
from core.search_and_recall import RecallHit, RecallQuery


_RUNTIME_LOCK = Lock()
_RUNTIME_ASSET_SEARCH_DEPTH = 6
_RUNTIME_EXECUTABLE_NAMES = ("python.exe", "python")


def register_ppt_master_fixed_capability(registry: object, application: object | None) -> bool:
    """Register the managed PPT writer only when bootstrap proved it usable.

    This deliberately changes no profile or execution-boundary filtering: the
    ordinary frozen project capability manifest remains the authorization
    authority for the registered definition.
    """
    capability = (
        getattr(getattr(application, "state", None), "ppt_master_capability", None)
        if application is not None else None
    )
    if capability is None:
        return False
    definition = ppt_master_fixed_capability_definition()
    register_core = getattr(registry, "register_core", None)
    if callable(register_core):
        register_core(definition, capability)
    else:
        # Unit composition can call this small helper with the raw registry.
        # Preserve that fixture seam while applying the same reviewed inventory
        # check as production composition.
        RuntimeCapabilityAdmission(registry).register_core(definition, capability)
    return True


class RuntimeAssetRootError(RuntimeError):
    """Raised when the sidecar cannot prove one owned runtime asset root."""


@dataclass(frozen=True)
class RuntimeAssetRoot:
    root_dir: Path
    hook_config: Path
    runtime_executable: Path


def resolve_runtime_asset_root(module_path: Path | None = None) -> RuntimeAssetRoot:
    """Locate the one sidecar-owned config/runtime pair near this module.

    Source installs keep those assets at the repository root, while a packaged
    sidecar keeps them beside the flattened backend tree. Deliberately do not
    use the current working directory or environment variables: either layout
    must prove its own assets, and an ambiguous layout fails closed.
    """

    module = (module_path or Path(__file__)).resolve(strict=False)
    candidates: list[RuntimeAssetRoot] = []
    for root_dir in module.parents[:_RUNTIME_ASSET_SEARCH_DEPTH]:
        hook_config = root_dir / "config" / "codex-hooks.toml"
        runtime_dir = root_dir / "runtime"
        executables = tuple(
            runtime_dir / executable_name
            for executable_name in _RUNTIME_EXECUTABLE_NAMES
            if (runtime_dir / executable_name).is_file()
        )
        # The web-only source checkout intentionally uses its project venv,
        # without copying the desktop sidecar Python into the data directory.
        # Only the exact source layout can supply this fallback; packaged and
        # ambiguous runtime layouts retain their existing admission rules.
        if not executables and module in (
            root_dir / "src" / "backend" / "api" / "ai_runtime.py",
            root_dir / "src" / "backend" / "memory_app" / "kernel" / "ai_runtime.py",
        ):
            executables = tuple(path for path in (
                root_dir / ".venv" / "Scripts" / "python.exe",
                root_dir / ".venv" / "bin" / "python",
            ) if path.is_file())
        if not hook_config.is_file() or len(executables) != 1:
            continue
        candidates.append(RuntimeAssetRoot(root_dir, hook_config, executables[0]))
    if not candidates:
        raise RuntimeAssetRootError(
            "AI runtime assets are unavailable: expected config/codex-hooks.toml "
            "and exactly one runtime Python executable near ai_runtime.py"
        )
    if len(candidates) != 1:
        raise RuntimeAssetRootError("AI runtime asset root is ambiguous")
    return candidates[0]


class LibrarySearchRecallAdapter:
    def __init__(self, service: object) -> None:
        self._service = service

    def recall(self, query: RecallQuery) -> tuple[RecallHit, ...]:
        result = self._service.search(
            query=query.text,
            project_id=query.project_id,
            layers=query.layers,
            trust_statuses=query.allowed_trust_statuses,
            limit=query.limit,
        )
        return tuple(
            RecallHit(hit.object_id, hit.layer, hit.content, tuple(hit.source_refs), hit.trust_status, hit.score)
            for hit in result.hits
        )


class UnavailableRecallAdapter:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def recall(self, _query):
        raise ValueError(f"memory recall authority is unavailable: {self._reason}")


class RecallFirstLocalPlanner:
    """Deterministic local policy used when remote inference is unavailable or disallowed."""

    def plan(self, request, events, capabilities, payloads, execution_control: ModelExecutionControlPort | None = None):
        completed = next((event for event in reversed(events) if event.get("type") == "tool.completed"), None)
        if completed is not None:
            data = completed.get("data")
            assert isinstance(data, Mapping)
            return {
                "type": "complete",
                "summary": str(data.get("summary") or "local memory recall completed"),
                "payload_ref": data.get("payload_ref"),
                "evidence_refs": list(data.get("evidence_refs") or ()),
            }
        input_payload = request.get("input")
        text = input_payload.get("text") if isinstance(input_payload, Mapping) else None
        return {
            "type": "tool",
            "capability_id": "memory.recall",
            "arguments": {"query": str(text or "").strip()},
        }


class PolicyAwareAgentPlanner:
    def __init__(self, *, local: object, remote: object | None) -> None:
        self._local = local
        self._remote = remote

    def plan(self, request, events, capabilities, payloads, execution_control: ModelExecutionControlPort | None = None):
        privacy = request.get("privacy")
        allow_remote = isinstance(privacy, Mapping) and privacy.get("allow_remote") is True
        planner = self._remote if allow_remote and self._remote is not None else self._local
        try:
            return planner.plan(request, events, capabilities, payloads, execution_control)
        except ModelRuntimeError:
            if planner is self._local:
                raise
            return self._local.plan(
                request, events, capabilities, payloads, execution_control,
            )


class OutcomeDispatchPlanner:
    def __init__(self, *, default: object, outcomes: Mapping[str, object]) -> None:
        self._default = default
        self._outcomes = dict(outcomes)

    def plan(self, request, events, capabilities, payloads, execution_control: ModelExecutionControlPort | None = None):
        planner = self._outcomes.get(str(request.get("desired_outcome")), self._default)
        return planner.plan(request, events, capabilities, payloads, execution_control)


class FrozenTurnImageGatewayResolver:
    """Resolve only the image route already frozen for the current Turn."""

    def __init__(self, container: object) -> None:
        self._container = container

    def resolve(self, *, project_id: str, routing_snapshot: Mapping[str, object], privacy_scope: str):
        resolution = resolve_frozen_image_generation_gateway(
            self._container,
            project_id=project_id,
            snapshot=routing_snapshot,
            privacy_scope=privacy_scope,
        )
        if resolution.gateway is None:
            raise ModelRuntimeError("image generation gateway is unavailable")
        return resolution.gateway


def _ensure_recursive_evolution_runtime(
    *, application: object, container: object,
    turns: SQLiteAITurnStore | None = None,
    profiles: AgentProfileRegistry | None = None,
    world: PersonalWorldModelRuntime | None = None,
    policy_authority: RecursiveEvolutionPolicyAuthorityComposition | None = None,
) -> object:
    """Attach the governed evolution graph to the same durable authorities.

    This helper is deliberately idempotent because a long-lived desktop
    process can have an AI runtime created before the route is first visited.
    It never creates a second World or a second AI Turn database.
    """

    state = application.state
    existing = getattr(state, "recursive_evolution_runtime", None)
    if existing is not None:
        return existing
    runtime_root = Path(getattr(container, "root_dir"))
    session_store = turns or getattr(state, "ai_turn_store", None)
    if not isinstance(session_store, SQLiteAITurnStore):
        session_store = SQLiteAITurnStore(runtime_root / ".rebuild-data" / "ai-turns.sqlite3")
    profile_registry = profiles or getattr(state, "agent_profile_registry", None)
    if not isinstance(profile_registry, AgentProfileRegistry):
        profile_registry = AgentProfileRegistry(
            SQLiteAgentStore(runtime_root / ".rebuild-data" / "ai-turns.sqlite3")
        )
    shared_world = world or getattr(state, "personal_world_model_runtime", None)
    if not isinstance(shared_world, PersonalWorldModelRuntime):
        supervision = getattr(state, "world_supervision_runtime", None)
        candidate = getattr(supervision, "_world", None)
        shared_world = candidate if isinstance(candidate, PersonalWorldModelRuntime) else None
    if shared_world is None:
        shared_world = PersonalWorldModelRuntime.for_root(runtime_root)
    shared_policy_authority = policy_authority or getattr(
        state, "recursive_evolution_policy_authority_composition", None,
    )
    if not isinstance(
        shared_policy_authority, RecursiveEvolutionPolicyAuthorityComposition,
    ):
        shared_policy_authority = build_recursive_evolution_policy_authority(
            runtime_root=runtime_root,
            world=shared_world,
            turns=session_store,
            profiles=profile_registry,
        )
    object_store, settings = build_rebuild_object_store(runtime_root)
    project_skills = AggregateRepositoryFactory(
        runtime_root=runtime_root,
        namespace_id=str(settings.namespace_id),
        json_store=object_store,
    ).project_skill_repository()
    composition = build_recursive_evolution_composition(
        runtime_root=runtime_root,
        world=shared_world,
        turns=session_store,
        profiles=profile_registry,
        project_skills=project_skills,
        agent_runtime=getattr(state, "agent_runtime_composition", None),
        policy_authority=shared_policy_authority,
    )
    # Recovery is bounded by the durable World stream index.  It repairs only
    # already-decided rollback/provenance work and never reruns a model or Tool.
    composition.recover(shared_world.project_ids())
    state.personal_world_model_runtime = shared_world
    state.recursive_evolution_composition = composition
    state.recursive_evolution_runtime = composition.runtime
    state.recursive_evolution_authority = composition.authority
    state.recursive_evolution_local_actions = composition.local_actions
    state.recursive_evolution_verified_workflow = composition.verified_workflow
    state.recursive_evolution_targets = composition.targets
    state.recursive_evolution_policy_catalog = composition.policy
    state.recursive_evolution_policy_authority_composition = (
        shared_policy_authority
    )
    return composition.runtime


def get_or_build_ai_runtime(request: object, container: object) -> SynchronousAIRuntime:
    application = getattr(request, "app")
    with _RUNTIME_LOCK:
        existing = getattr(application.state, "ai_runtime", None)
        if existing is not None:
            composition = getattr(application.state, "agent_runtime_composition", None)
            if composition is not None:
                runner = getattr(application.state, "ai_turn_runner", None)
                if runner is None:
                    runner = AITurnRunner(
                        existing,
                        max_workers=4, max_child_workers=4, max_child_pending=16,
                        terminal_observer=getattr(
                            application.state,
                            "personal_world_model_terminal_observer",
                            None,
                        ),
                    )
                    application.state.ai_turn_runner = runner
                composition.bind_runtime(existing)
                composition.bind_runner(runner)
            _ensure_recursive_evolution_runtime(application=application, container=container)
            _bind_recognition_terminal_projection(application)
            return existing
        existing = build_ai_runtime(container, application=application)
        application.state.ai_runtime = existing
        application.state.ai_turn_runner = AITurnRunner(
            existing,
            max_workers=4, max_child_workers=4, max_child_pending=16,
            terminal_observer=getattr(
                application.state,
                "personal_world_model_terminal_observer",
                None,
            ),
        )
        composition = getattr(application.state, "agent_runtime_composition", None)
        if composition is not None:
            composition.bind_runtime(existing)
            composition.bind_runner(application.state.ai_turn_runner)
        _bind_recognition_terminal_projection(application)
        return existing


def _bind_recognition_terminal_projection(application) -> None:
    state = application.state
    authority = getattr(state, "recognition_turn_authority", None)
    runner = getattr(state, "ai_turn_runner", None)
    if authority is not None and runner is not None and not getattr(state, "recognition_terminal_subscription", None):
        state.recognition_terminal_subscription = runner.subscribe_terminal(authority.observe_terminal)


def _active_capability_revision(
    application: object | None, capability_id: str,
) -> str | None:
    state = getattr(application, "state", None)
    catalog = getattr(state, "capability_package_catalog", None)
    active = getattr(catalog, "active", None)
    if not callable(active):
        return None
    matches = [item for item in active() if item.capability_id == capability_id]
    return matches[0].capability_revision if len(matches) == 1 else None


def build_ai_runtime(container: object, *, application: object | None = None) -> SynchronousAIRuntime:
    asset_root = resolve_runtime_asset_root()
    runtime_root = getattr(container, "root_dir")
    # A packaged-only fixture may seed the same production catalog, binding,
    # model-route, and selected Memory authorities before Turn facts freeze.
    # It is unreachable without the Electron + sidecar dual nonce gate.
    install_expert_turn_e2e_fixture(Path(runtime_root))
    shared_effect_runner = (
        getattr(getattr(application, "state", object()), "ai_effect_runtime", None).runner
        if application is not None
        and getattr(getattr(application, "state", object()), "ai_effect_runtime", None) is not None
        else None
    )
    session_store = SQLiteAITurnStore(
        runtime_root / ".rebuild-data" / "ai-turns.sqlite3",
        effect_runner=shared_effect_runner, cache_immutable_reads=True,
    )
    store, _settings = build_rebuild_object_store(runtime_root)
    application_state = getattr(application, "state", None)
    answer_service = getattr(application_state, "product_answer_turns", None)
    external_extension_runtime = getattr(
        application_state, "external_extension_runtime", None,
    )
    external_application_skill_packages = getattr(
        external_extension_runtime, "active_packages", None,
    )
    if not callable(external_application_skill_packages):
        external_application_skill_packages = None
    capability_profiles = ProjectCapabilityProfileStore(runtime_root)
    expert_binding_runtime = build_expert_turn_binding_runtime(runtime_root)

    def reviewed_external_skill_ids(project_id: str) -> tuple[str, ...]:
        if external_application_skill_packages is None:
            return ()
        return reviewed_external_application_skill_ids(
            external_application_skill_packages(project_id)
        )

    assignment_resolvers = build_agent_assignment_resolvers(
        expert_runtime=expert_binding_runtime,
        capability_profiles=capability_profiles,
        reviewed_external_skill_ids=reviewed_external_skill_ids,
    )
    context_compilation_facts = getattr(
        application_state, "context_compilation_facts", None,
    )
    if context_compilation_facts is None:
        context_compilation_facts = SQLiteCompilationFactRepository(
            SQLiteStructuredRecordStore(
                Path(runtime_root) / ".rebuild-data" / "context-graphs.sqlite3"
            ),
            lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    context_binding_snapshots = TurnContextBindingSnapshotAuthority(
        ContextBindingRegistry(
            Path(runtime_root), namespace_id=str(_settings.namespace_id),
        ),
        session_store,
        capability_revision_reader=lambda capability_id: _active_capability_revision(
            application, capability_id,
        ),
        compilation_facts=context_compilation_facts,
    )
    try:
        recalls = LibrarySearchRecallAdapter(build_library_search_service(runtime_root, store))
    except AggregateRepositoryFactoryError as error:
        recalls = UnavailableRecallAdapter(str(error))
    dispatch_registry = ScopedCapabilityRegistry()
    package_catalog = getattr(application_state, "capability_package_catalog", None)
    package_contributions = getattr(
        application_state, "capability_package_contributions", None,
    )
    capability_admission = RuntimeCapabilityAdmission(
        dispatch_registry,
        packages=package_catalog,
        contributions=package_contributions,
    )
    # Static capabilities are an explicit, reviewed Core inventory.  Future
    # non-Core additions must use RuntimeCapabilityAdmission.register_package_tool
    # against an active CapabilityPackage contribution instead of this view.
    registry = ReviewedCoreCapabilityRegistry(capability_admission)
    privacy_registry = getattr(application_state,"source_privacy_registry_wrapper",None)
    if privacy_registry is not None:
        registry = privacy_registry(registry)
    recognition_routing = None
    answer_routing = None
    if answer_service is not None:
        from .answer_turns import answer_definition, ProductAnswerCapability
        from .answer_routing import ProductAnswerRouting
        registry.register(answer_definition(), ProductAnswerCapability(answer_service, session_store))
        answer_routing = ProductAnswerRouting(answer_service.query.models, session_store,
            records=answer_service.query.records, answer_owner=True)
    installer = getattr(application_state, "recognition_turn_installer", None)
    if installer is not None and getattr(application_state, "recognition_service", None) is not None:
        recognition_routing = installer(registry, session_store, application_state)
    external_installer = getattr(application_state, "external_context_installer", None)
    external_bind = external_installer(registry, session_store) if external_installer is not None else None
    external_runner_installer = getattr(application_state, 'external_runner_installer', None)
    external_runner = external_runner_installer(
        application_state, registry, session_store, runtime_root=Path(runtime_root),
    ) if external_runner_installer is not None else None
    if application_state is not None:
        application_state.runtime_capability_admission = capability_admission
    # Build the one World/Profile authority first, then inject the policy
    # catalog before AgentCoordinator exists.  Consequently a policy rollout
    # can influence only newly admitted Turns and can never rewrite a live
    # Turn's binding.
    personal_world = PersonalWorldModelRuntime.for_root(Path(runtime_root))
    profile_registry = AgentProfileRegistry(
        SQLiteAgentStore(Path(runtime_root) / ".rebuild-data" / "ai-turns.sqlite3")
    )
    if recognition_routing is not None:
        # A new recognition workbench gets one explicit versioned main-profile
        # override. Existing/customized profiles keep their chosen permission set.
        from dataclasses import replace
        profile_store = SQLiteAgentStore(Path(runtime_root) / ".rebuild-data" / "ai-turns.sqlite3")
        main_profile = profile_registry.get("main.orchestrator")
        initial = profile_store.get("main.orchestrator") is None
        permitted = set(main_profile.capability_ids)
        if initial:
            permitted.add("recognition.task.execute")
        if "recognition.task.execute" in permitted:
            permitted.add("recognition.task.execute.local")
        if permitted != set(main_profile.capability_ids):
            profile_registry.update(replace(main_profile, revision=main_profile.revision + 1,
                capability_ids=tuple(sorted(permitted))), expected_revision=main_profile.revision)
    profile_registry.upgrade_untouched_builtin_limits()
    evolution_policy = build_recursive_evolution_policy_authority(
        runtime_root=Path(runtime_root), world=personal_world,
        turns=session_store, profiles=profile_registry,
    )
    agent_runtime_composition = build_agent_runtime_composition(
        runtime_root=Path(runtime_root), session_store=session_store, registry=registry,
        expert_assignment_resolver=assignment_resolvers.expert,
        skill_assignment_resolver=assignment_resolvers.skill,
        agent_policy_snapshots=evolution_policy.policy,
        profiles=profile_registry,
    )
    # One world projection is shared by dispatch freshness and the receipt-only
    # observer; neither path establishes a parallel task/agent state source.
    world_supervision_runtime = WorldSupervisionRuntime(
        world=personal_world,
    )
    world_supervision_observer = WorldSupervisionAgentObserver(
        supervision=world_supervision_runtime,
        run_store=agent_runtime_composition.store,
        request_loader=agent_runtime_composition.request_loader,
        payload_loader=session_store.get,
        now=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    agent_organization_runtime = AgentOrganizationRuntime(
        coordinator=agent_runtime_composition.coordinator,
        dispatch_store=agent_runtime_composition.dispatch_store,
        run_store=agent_runtime_composition.store,
        request_loader=agent_runtime_composition.request_loader,
        start_pair_scanner=lambda limit: (
            agent_runtime_composition.store.list_organization_start_pairs(limit=limit)
        ),
        recovery_plan_scanner=lambda limit: (
            agent_runtime_composition.dispatch_store.list_recovery_plans(limit=limit)
        ),
        freshness_gate=world_supervision_runtime.freshness_allows,
    )
    agent_runtime_composition.bind_organization_runtime(agent_organization_runtime)
    agent_runtime_composition.bind_supervision_observer(world_supervision_observer)
    if application_state is not None:
        application_state.agent_runtime_composition = agent_runtime_composition
        application_state.agent_profile_registry = agent_runtime_composition.profiles
        application_state.personal_world_model_runtime = personal_world
        application_state.recursive_evolution_policy_authority_composition = (
            evolution_policy
        )
        application_state.agent_run_coordinator = agent_runtime_composition.coordinator
        application_state.agent_turn_request_loader = agent_runtime_composition.request_loader
        application_state.agent_organization_runtime = agent_organization_runtime
        application_state.world_supervision_runtime = world_supervision_runtime
        application_state.world_supervision_agent_observer = world_supervision_observer
        application_state.agent_expert_binding_runtime = expert_binding_runtime
    registry.register(
        CapabilityDefinition(
            "memory.recall",
            1,
            "read",
            False,
            "read_only",
            "crp://default/contracts/recall-request.schema.json",
            "crp://default/contracts/recall-result.schema.json",
        ),
        MemoryRecallCapability(recalls),
    )
    register_ppt_master_fixed_capability(registry, application)
    media_runtime = current_media_hands_runtime(application) if application is not None else None
    if media_runtime is None:
        analyze_source = disabled_analyze_source_capability(
            payloads=session_store,
            namespace_id=str(getattr(_settings, "namespace_id", "default")),
            resolver=ArtifactSourceManifestResolver(
                repository=SourceManifestArtifactRepository(
                    store,
                    namespace_id=str(getattr(_settings, "namespace_id", "default")),
                ),
            ),
        )
        analyze_source_available = False
    else:
        analyze_source = AnalyzeSourceCapability(
            resolver=media_runtime.resolver,
            provisioner=media_runtime,
            payloads=session_store,
            readiness=media_runtime.readiness,
            created_at=lambda: datetime.now(timezone.utc).isoformat(),
            namespace_id=media_runtime.namespace_id,
        )
        analyze_source_available = media_runtime.readiness().ready
    registry.register(
        analyze_source_capability_definition(available=analyze_source_available),
        analyze_source,
    )
    registry.register(
        CapabilityDefinition(
            IMAGE_GENERATION_CAPABILITY,
            1,
            "external",
            True,
            "receipt_required",
            "crp://default/contracts/image-generation-request.schema.json",
            "crp://default/contracts/image-generation-result.schema.json",
        ),
        ImageGenerationCapability(
            gateway_resolver=FrozenTurnImageGatewayResolver(container),
            assets=GeneratedAssetAuthority(
                object_store=store,
                vault_root=runtime_root / "library",
                namespace_id=str(getattr(_settings, "namespace_id", "default")),
            ),
            payloads=session_store,
        ),
    )
    question_resolution = resolve_model_gateway_runtime(
        container,
        "search.answer",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
        tiered_capability="structured",
    )
    project_skill_resolution = resolve_model_gateway_runtime(
        container,
        "search.answer",
        egress_purpose="memory_candidate",
        egress_categories=("instructions", "source_excerpt"),
        tiered_capability="structured",
    )
    source_document_resolution = resolve_model_gateway_runtime(
        container,
        "search.answer",
        egress_purpose="document_draft",
        egress_categories=("instructions", "source_excerpt"),
        tiered_capability="structured",
    )
    companion_chat_resolution = resolve_model_gateway_runtime(
        container,
        "companion.chat",
        egress_purpose="companion_chat",
        egress_categories=("instructions", "source_excerpt"),
        tiered_capability="text",
    )
    companion_vision_resolution = resolve_model_gateway_runtime(
        container,
        "companion.vision",
        egress_purpose="companion_vision",
        egress_categories=("image_frame", "instructions"),
        tiered_capability="vision",
    )
    intake_classification_resolution = resolve_model_gateway_runtime(
        container,
        INPUT_CLASSIFIER_MODEL_ROUTE,
        egress_purpose="intake_classification",
        egress_categories=("instructions", "source_excerpt"),
        tiered_capability="structured",
    )
    test_lab_resolution = resolve_model_gateway_runtime(
        container,
        "developer_studio.test_lab",
        egress_purpose="connection_test",
        egress_categories=("instructions", "source_excerpt"),
        tiered_capability="structured",
    )
    series_intake_resolution = resolve_model_gateway_runtime(
        container,
        "search.answer",
        egress_purpose="series_intake_organize",
        egress_categories=("instructions", "source_excerpt"),
        tiered_capability="structured",
    )
    classifier_runtime = build_workbench_input_classifier_runtime(container, store)

    def classifier_authority() -> Mapping[str, object]:
        prompt = classifier_runtime.active_prompt()
        if not isinstance(prompt, Mapping):
            raise ValueError("Workbench classifier prompt authority is unavailable")
        route = ModelRouteRegistry(runtime_root).get(INPUT_CLASSIFIER_MODEL_ROUTE)["route"]
        provider_id = str(route.get("provider_id") or "").strip()
        provider = ProviderRegistry(runtime_root).get_readonly(
            provider_id, fallback=provider_fallback(container),
        )
        prompt_revision = prompt.get("revision")
        route_revision = route.get("revision")
        provider_revision = provider.get("revision", 1)
        return {
            "prompt": dict(prompt), "provider_id": provider_id,
            "prompt_revision": prompt_revision, "route_revision": route_revision,
            "provider_revision": provider_revision,
        }

    def memory_candidate_authority() -> Mapping[str, object]:
        route = ModelRouteRegistry(runtime_root).get("search.answer")["route"]
        provider_id = str(route.get("provider_id") or "").strip()
        provider = ProviderRegistry(runtime_root).get_readonly(
            provider_id, fallback=provider_fallback(container),
        )
        return {
            "prompt_revision": 1, "route_revision": route.get("revision"),
            "provider_revision": provider.get("revision", 1), "provider_id": provider_id,
        }

    def memory_candidate_evidence(grant: object) -> Mapping[str, object]:
        evidence_id = str(getattr(grant, "evidence_id"))
        evidence_kind = str(getattr(grant, "evidence_kind"))
        source_id = str(getattr(grant, "source_id"))
        if evidence_kind == "source_content_read":
            record = store.read("source_content_reads", evidence_id)
            if not isinstance(record, Mapping) or record.get("source_id") != source_id:
                raise ValueError("memory candidate source content evidence is unavailable")
            preview = record.get("preview")
            source_kind, refs = "source_content_read", [f"crp://{getattr(_settings, 'namespace_id', 'default')}/source-content-reads/{evidence_id}.json"]
            extra = {"content_preview": preview}
        elif evidence_kind == "media_processing_output":
            record = store.read("media_processing_outputs", evidence_id)
            if not isinstance(record, Mapping) or record.get("source_id") != source_id:
                raise ValueError("memory candidate media evidence is unavailable")
            preview = record.get("preview")
            output_kind = str(record.get("output_kind") or "unknown")
            source_kind, refs = f"media_processing_output:{output_kind}", [str(record.get("ref") or f"crp://{getattr(_settings, 'namespace_id', 'default')}/media-processing-outputs/{evidence_id}.json")]
            extra = {"output_kind": output_kind, "output_preview": preview}
        else:
            raise ValueError("memory candidate evidence kind is invalid")
        if record.get("status") != "completed" or not isinstance(preview, str) or not preview.strip():
            raise ValueError("memory candidate evidence is incomplete")
        prompt = build_four_layer_memory_prompt(source_kind=source_kind, source_summary=preview, allowed_layers=getattr(grant, "allowed_layers"))
        user_payload = {
            "prompt_version": prompt.prompt_version, "source_kind": evidence_kind,
            "project_id": str(getattr(grant, "project_id")), "source_id": source_id,
            "evidence_id": evidence_id, "source_refs": [{"source_id": source_id, "locator": source_kind, "quote": preview}],
            **extra, "output_contract": prompt.output_contract, "provider_boundary": prompt.provider_boundary,
        }
        revision = record.get("revision", 1)
        return {
            "status": "completed", "evidence_id": evidence_id, "source_id": source_id,
            "kind": evidence_kind, "revision": revision if isinstance(revision, int) and revision > 0 else 1,
            "summary": preview, "refs": refs,
            "provider_request": {"system_prompt": prompt.system_prompt, "user_payload": user_payload},
        }

    memory_candidate_importer = ImportFourLayerMemoryCandidatesFromProviderOutput(
        store, namespace_id=str(getattr(_settings, "namespace_id", "default")),
    )

    def import_memory_candidates(output: Mapping[str, object], evidence: Mapping[str, object], project_id: str) -> Mapping[str, object]:
        evidence_id = str(evidence["evidence_id"])
        if evidence["kind"] == "source_content_read":
            result = memory_candidate_importer.execute_from_content_read(
                source_id=str(evidence["source_id"]), project_id=project_id,
                content_read_id=evidence_id, provider_output=output,
            )
        else:
            result = memory_candidate_importer.execute_from_media_output(
                output_id=evidence_id, project_id=project_id, provider_output=output,
            )
        return serialize_four_layer_memory_candidate_import(result)
    remote = (
        ModelGatewayAgentPlanner(question_resolution.gateway)
        if question_resolution.gateway is not None and question_resolution.egress_consented
        else None
    )
    registry.register(
        CapabilityDefinition(
            FOUR_LAYER_MEMORY_EVIDENCE_CAPABILITY, 1, "read", False, "read_only",
            "crp://default/contracts/memory-candidate-evidence-request.schema.json",
            "crp://default/contracts/memory-candidate-evidence-result.schema.json",
        ),
        ScopedFourLayerMemoryEvidenceCapability(
            application=application, evidence_loader=memory_candidate_evidence,
            authority_loader=memory_candidate_authority,
        ),
    )
    registry.register(
        CapabilityDefinition(
            SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY, 1, "write", True, "receipt_required",
            "crp://default/contracts/series-intake-organize-request.schema.json",
            "crp://default/contracts/series-intake-organize-result.schema.json",
        ),
        SeriesIntakeOrganizeCommitCapability(
            workspace=SeriesWorkspace(runtime_root),
            gateway=(
                series_intake_resolution.gateway
                if series_intake_resolution.egress_consented else None
            ),
            payloads=session_store,
            namespace_id=str(getattr(_settings, "namespace_id", "default")),
        ),
    )
    registry.register(
        CapabilityDefinition(
            FOUR_LAYER_MEMORY_PROPOSE_CAPABILITY, 1, "external", True, "receipt_required",
            "crp://default/contracts/memory-candidate-propose-request.schema.json",
            "crp://default/contracts/memory-candidate-propose-result.schema.json",
        ),
        ScopedFourLayerMemoryCandidateProposalCapability(
            application=application, evidence_loader=memory_candidate_evidence,
            authority_loader=memory_candidate_authority, importer=import_memory_candidates,
            receipt_store=session_store,
            gateway=project_skill_resolution.gateway if project_skill_resolution.egress_consented else None,
        ),
    )
    registry.register(
        CapabilityDefinition(
            WORKBENCH_INPUT_CLASSIFICATION_CONTEXT_CAPABILITY, 1, "read", False, "read_only",
            "crp://default/contracts/workbench-input-classification-context-request.schema.json",
            "crp://default/contracts/workbench-input-classification-context-result.schema.json",
        ),
        ScopedWorkbenchInputClassificationContextCapability(
            application=application, authority_loader=classifier_authority,
        ),
    )
    registry.register(
        developer_studio_test_lab_capability_definition(),
        DeveloperStudioTestLabCapability(
            gateway=(
                test_lab_resolution.gateway
                if test_lab_resolution.egress_consented else None
            ),
            receipt_store=session_store,
        ),
    )
    registry.register(
        CapabilityDefinition(
            WORKBENCH_INPUT_CLASSIFICATION_ENHANCE_CAPABILITY, 1, "external", True, "receipt_required",
            "crp://default/contracts/workbench-input-classification-enhance-request.schema.json",
            "crp://default/contracts/workbench-input-classification-enhance-result.schema.json",
        ),
        ScopedWorkbenchInputClassificationEnhanceCapability(
            application=application, authority_loader=classifier_authority,
            gateway=(
                intake_classification_resolution.gateway
                if intake_classification_resolution.egress_consented else None
            ),
            receipt_store=session_store,
        ),
    )
    registry.register(
        CapabilityDefinition(
            WORKBENCH_QUESTION_CAPABILITY,
            1,
            "read",
            False,
            "read_only",
            "crp://default/contracts/workbench-question-request.schema.json",
            "crp://default/contracts/workbench-question-presentation.schema.json",
        ),
        WorkbenchQuestionCapability(
            runtime_root=runtime_root,
            store=store,
            namespace_id=str(getattr(_settings, "namespace_id", "default")),
            effect_runtime=getattr(application_state, "effect_runtime", None),
        ),
    )
    registry.register(
        CapabilityDefinition(
            COMPANION_VISION_CONTEXT_CAPABILITY,
            1,
            "read",
            False,
            "read_only",
            "crp://default/contracts/companion-vision-context-request.schema.json",
            "crp://default/contracts/companion-vision-context-result.schema.json",
        ),
        ScopedCompanionVisionContextCapability(container=container, application=application),
    )
    registry.register(
        CapabilityDefinition(
            COMPANION_VISION_ANALYZE_CAPABILITY,
            1,
            "write",
            True,
            "receipt_required",
            "crp://default/contracts/companion-vision-analyze-request.schema.json",
            "crp://default/contracts/companion-vision-analyze-result.schema.json",
        ),
        ScopedCompanionVisionAnalyzeCapability(
            container=container,
            application=application,
            receipt_store=session_store,
            gateway=(
                companion_vision_resolution.gateway
                if getattr(companion_vision_resolution, "adapter_kind", "") == "openai-compatible-vision"
                and companion_vision_resolution.egress_consented
                else None
            ),
        ),
    )
    registry.register(
        CapabilityDefinition(
            PROJECT_SKILL_EVIDENCE_CAPABILITY,
            1,
            "read",
            False,
            "read_only",
            "crp://default/contracts/project-skill-evidence-request.schema.json",
            "crp://default/contracts/project-skill-evidence-result.schema.json",
        ),
        ProjectSkillEvidenceCapability(runtime_root=runtime_root, store=store, namespace_id=str(getattr(_settings, "namespace_id", "default"))),
    )
    registry.register(
        CapabilityDefinition(
            PROJECT_SKILL_PROPOSE_CAPABILITY,
            1,
            "write",
            True,
            "receipt_required",
            "crp://default/contracts/project-skill-draft-request.schema.json",
            "crp://default/contracts/project-skill-draft-result.schema.json",
        ),
        ProjectSkillDraftProposalCapability(
            runtime_root=runtime_root,
            store=store,
            namespace_id=str(getattr(_settings, "namespace_id", "default")),
        ),
    )
    registry.register(
        CapabilityDefinition(
            SOURCE_EVIDENCE_CAPABILITY,
            1,
            "read",
            False,
            "read_only",
            "crp://default/contracts/source-evidence-request.schema.json",
            "crp://default/contracts/source-evidence-result.schema.json",
        ),
        SourceDocumentEvidenceCapability(
            runtime_root=runtime_root,
            store=store,
            namespace_id=str(getattr(_settings, "namespace_id", "default")),
        ),
    )
    draft_definition = CapabilityDefinition(
        DOCUMENT_DRAFT_PROPOSE_CAPABILITY, 1, "write", True, "receipt_required",
        "crp://default/contracts/source-document-draft-request.schema.json",
        "crp://default/contracts/source-document-draft-result.schema.json",
    )
    draft_provider = SourceDocumentDraftProposalCapability(runtime_root=runtime_root,
        store=store, namespace_id=str(getattr(_settings, "namespace_id", "default")))
    task_drafts = getattr(application_state, "task_drafts", None)
    if task_drafts is not None:
        from .task_draft_capability import task_draft_definition, TaskDraftCapability
        from .task_division_authority import frozen_division_capabilities, frozen_division_binding
        draft_definition = task_draft_definition(draft_definition)
        draft_provider = TaskDraftCapability(legacy=draft_provider, drafts=task_drafts,
            request_loader=agent_runtime_composition.request_loader,
            division_capabilities=lambda request: frozen_division_capabilities(agent_runtime_composition, request),
            division_binding=lambda request: frozen_division_binding(agent_runtime_composition, request, session_store))
    registry.register(draft_definition, draft_provider)
    registry.register(
        CapabilityDefinition(
            COMPANION_CHAT_CONTEXT_CAPABILITY,
            1,
            "read",
            False,
            "read_only",
            "crp://default/contracts/companion-chat-context-request.schema.json",
            "crp://default/contracts/companion-chat-context-result.schema.json",
        ),
        ScopedCompanionChatContextCapability(
            container=container,
            namespace_id=str(getattr(_settings, "namespace_id", "default")),
        ),
    )
    registry.register(
        CapabilityDefinition(
            COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY,
            1,
            "write",
            True,
            "receipt_required",
            "crp://default/contracts/companion-chat-message-write-request.schema.json",
            "crp://default/contracts/companion-chat-message-write-result.schema.json",
        ),
        ScopedCompanionChatMessageWriteCapability(
            container=container,
            namespace_id=str(getattr(_settings, "namespace_id", "default")),
            receipt_store=session_store,
            # A configured gateway is not sufficient: the Companion egress
            # manifest must have an existing consent before it is even made
            # available to the approved write capability.
            gateway=(
                companion_chat_resolution.gateway
                if companion_chat_resolution.egress_consented
                else None
            ),
        ),
    )
    default_planner = PolicyAwareAgentPlanner(local=RecallFirstLocalPlanner(), remote=remote)
    base_planner = OutcomeDispatchPlanner(
        default=default_planner,
        outcomes={
            WORKBENCH_QUESTION_OUTCOME: WorkbenchQuestionPlanner(
                question_resolution.gateway if question_resolution.egress_consented else None,
                developer_instruction=resolve_workbench_answer_instruction(store),
            ),
            PROJECT_SKILL_DRAFT_OUTCOME: ProjectSkillDraftPlanner(
                project_skill_resolution.gateway if project_skill_resolution.egress_consented else None,
            ),
            SOURCE_DOCUMENT_DRAFT_OUTCOME: SourceDocumentDraftPlanner(
                source_document_resolution.gateway if source_document_resolution.egress_consented else None,
            ),
            COMPANION_CHAT_OUTCOME: CompanionChatTurnPlanner(),
            COMPANION_VISION_OUTCOME: CompanionVisionTurnPlanner(),
            WORKBENCH_INPUT_CLASSIFICATION_OUTCOME: WorkbenchInputClassificationTurnPlanner(),
            DEVELOPER_STUDIO_TEST_LAB_OUTCOME: DeveloperStudioTestLabTurnPlanner(),
            FOUR_LAYER_MEMORY_CANDIDATE_OUTCOME: FourLayerMemoryCandidateTurnPlanner(),
            SERIES_INTAKE_ORGANIZE_OUTCOME: SeriesIntakeOrganizeTurnPlanner(),
            IMAGE_GENERATION_OUTCOME: ImageGenerationTurnPlanner(),
        },
    )
    steward_proposal_builder = AgentStewardProposalBuilder(
        capability_definition=dispatch_registry.get,
        profiles=agent_runtime_composition.profiles,
        agent_store=agent_runtime_composition.store,
        request_loader=agent_runtime_composition.request_loader,
        capability_profiles=capability_profiles,
        expert_catalog=expert_binding_runtime.catalog,
        expert_bindings=expert_binding_runtime.bindings,
    )
    from backend.api.agent_steward_decomposition import StewardDecompositionPlanner

    planner = AgentRoleDispatchPlanner(
        delegate=base_planner,
        steward=StewardPlanningPlanner(
            remote=StewardDecompositionPlanner(
                question_resolution.gateway if question_resolution.egress_consented else None,
                steward_proposal_builder,
            ),
            proposal_validator=is_valid_steward_proposal,
            proposal_builder=lambda request, _events, _capabilities, _payloads: (
                steward_proposal_builder.build(request)
            ),
        ),
        main=MainCoordinationPlanner(remote=default_planner, wait_timeout_ms=30_000),
    )
    task_planner = None
    task_guard = getattr(application_state,'product_task_guard',None)
    if callable(task_guard):
        from .task_planner import ProductTaskPlanner
        task_planner = ProductTaskPlanner(models=application_state.product_task_models,
            profile_reader=getattr(application_state, 'product_task_profile', None),
            context_reader=getattr(application_state, 'product_task_context', None),
            outcome=getattr(application_state, 'product_task_outcomes', None),
            store=session_store,composition=agent_runtime_composition,guard=task_guard,
            builder=steward_proposal_builder,fallback=planner, drafts=task_drafts,
            records=application_state.recognition_service.records, runtime_root=runtime_root,
            read_control_type=getattr(application_state, 'product_task_read_control_type', None),
            frame_factory=getattr(application_state, 'product_task_frame_factory', None))
        planner = task_planner
    privacy_planner = getattr(application_state,"source_privacy_planner_wrapper",None)
    if privacy_planner is not None:
        planner = privacy_planner(planner,session_store,agent_runtime_composition.store)
    boundary_profiles = ProjectBoundaryProfileStore(runtime_root)
    profile_snapshots = TurnProjectProfileSnapshotAuthority(capability_profiles, boundary_profiles)
    skill_bindings = ApplicationSkillBindingRegistry(store)
    plugin_skills = build_plugin_skill_activation(runtime_root)
    skill_snapshots = TurnApplicationSkillSnapshotAuthority(
        catalog=ApplicationSkillCatalog(),
        sources=(
            ApplicationSkillSource(
                "bundled", asset_root.root_dir / "config" / "application-skills", "bundled"
            ),
            ApplicationSkillSource("user", runtime_root / "skills", "user"),
        ),
        plugin_sources=plugin_skills.active_sources,
        plugin_claimed_skill_ids=plugin_skills.reviewed_skill_ids,
        external_packages=external_application_skill_packages,
        bindings=skill_bindings,
        resolver=ApplicationSkillResolver(
            skill_bindings,
            trace_store=ObjectStoreApplicationSkillTraceRepository(store),
        ),
        payloads=session_store,
        agent_binding_verifier=agent_runtime_composition.coordinator.verify_agent_binding,
    )
    model_snapshots = TurnModelRoutingSnapshotAuthority(
        container,
        session_store,
        agent_binding_verifier=agent_runtime_composition.coordinator.verify_agent_binding,
        recognition_routing=recognition_routing,
        task_routing=task_planner,

        answer_routing=answer_routing,
    )
    published_memory_snapshots = PublishedProjectMemorySnapshotAuthority(
        factory=AggregateRepositoryFactory(
            runtime_root=runtime_root,
            namespace_id=store.namespace_id,
            json_store=store,
        ),
        payloads=session_store,
    )
    world_state_snapshots = TurnWorldStateSnapshotAuthority.for_root(
        runtime_root,
        payloads=session_store,
    )
    # A prior process cannot retain an MCP provider or Registry lease.  Finish
    # any durable pointer switch before provisioning the new process manager.
    if MCPApprovedServerMigrationAuthority.database_exists(
        runtime_root / ".rebuild-data" / "security"
    ):
        MCPApprovedServerMigrationRuntime(
            runtime_root, manager=None,
        ).recover_interrupted_switch()
    mcp_manager = build_mcp_connection_manager(
        root_dir=runtime_root,
        registry=dispatch_registry,
        turn_store=session_store,
        secret_store=getattr(container, "secret_store", None),
    )
    # Plugin Tools have already passed the durable review/activation authority.
    # Register them before any project capability snapshot can be frozen, using
    # the same registry the Dispatcher and Boundary resolve at execution time.
    plugin_tool_manager = PluginToolRegistrationManager(
        activation=build_plugin_tool_activation(runtime_root), registry=dispatch_registry,
    )
    plugin_tool_manager.reconcile()
    from .task_division_authority import frozen_division_capabilities
    turn_binding_guard = TurnCapabilityBindingGuard(
        capability_profiles, boundary_profiles, events=session_store, payloads=session_store,
    )
    execution_boundary = AIToolExecutionBoundary(
        boundary_profiles,
        division_capabilities=lambda request: frozen_division_capabilities(agent_runtime_composition, request),
        binding_guard=turn_binding_guard,
        external_execution_authority=(external_runner.authorize if external_runner is not None else None),
    )
    hook_host = build_codex_hook_host(asset_root.hook_config)
    frozen_authorization = (
        TurnFrozenAuthorizationAuthority(
            payloads=session_store,
            execution_boundary=execution_boundary,
            agent_capability_authorizer=(
                agent_runtime_composition.coordinator.authorize_agent_capability
            ),
        )
        if hook_host is not None
        else None
    )
    plugin_hands_manager = None
    plugin_hands_outcome_projector = PluginHandsToolOutcomeProjector(
        SQLiteStructuredRecordStore(runtime_root / ".rebuild-data" / "jobs.sqlite3")
    )
    if frozen_authorization is not None:
        plugin_hands_manager = build_plugin_hands_registration_manager(
            root_dir=runtime_root, registry=dispatch_registry,
            frozen_authorization=frozen_authorization,
            runtime_executable=asset_root.runtime_executable,
            effect_runner=(
                getattr(getattr(application, "state", None), "effect_runtime", None).runner
                if getattr(getattr(application, "state", None), "effect_runtime", None) is not None
                else None
            ),
        )
        # First rebuild the single Registry projection from the durable active
        # pointer.  Interrupted cutovers then revoke/switch/re-register through
        # this same manager, so startup cannot create a second lease for one
        # stable capability id.
        plugin_hands_manager.reconcile()
        plugin_hook_runner = build_plugin_hook_runner(
            root_dir=runtime_root,
            frozen_authorization=frozen_authorization,
            runtime_executable=asset_root.runtime_executable,
        )
        plugin_hook_manifests = plugin_hook_runner.manifests()
        if plugin_hook_manifests:
            hook_host = build_codex_hook_host(
                asset_root.hook_config,
                additional_handlers=plugin_hook_manifests,
                additional_runner=plugin_hook_runner,
            )
        elif hook_host is not None:
            # Keep the contained runner installed even with an empty startup
            # projection so a later explicit activation can publish atomically.
            hook_host = build_codex_hook_host(
                asset_root.hook_config,
                additional_runner=plugin_hook_runner,
            )
        plugin_hook_projection_manager = PluginHookProjectionManager(
            hook_host, plugin_hook_runner,
            fault_probe=plugin_hook_projection_fault_probe(runtime_root),
        ) if hook_host is not None else None
    runtime = ProductPolicyRuntime(
        planner=planner,
        registry=dispatch_registry,
        events=session_store,
        payloads=session_store,
        state=session_store,
        manifest_resolver=ProjectAwareCapabilityManifestResolver(
            profile_snapshots,
            application_skills=skill_snapshots,
            model_routing=model_snapshots,
        ),
        context_manifest_resolver=ProjectAwareContextManifestResolver(
            profile_snapshots,
            application_skills=skill_snapshots,
            model_routing=model_snapshots,
            published_memory=published_memory_snapshots,
            world_state=world_state_snapshots,
            context_bindings=context_binding_snapshots,
            runtime_self_manifest=getattr(
                getattr(application, "state", None), "runtime_self_manifest", None,
            ),
            payloads=session_store,
        ),
        expert_binding=expert_binding_runtime,
        expert_memory_proposal_sink=ExpertMemoryProposalRuntime(
            runtime_root, store,
            namespace_id=str(getattr(_settings, "namespace_id", "default")),
        ),
        execution_boundary=execution_boundary,
        hook_host=hook_host,
        frozen_authorization=frozen_authorization,
        mcp_continuation_reconnector=mcp_manager.reconnect_for_continuation,
        external_tool_outcome_projector=plugin_hands_outcome_projector.project_for_runtime,
        effect_runner=session_store.effect_runner,
        max_steps=profile_registry.get("main.orchestrator").max_steps,
    )
    runtime.task_continuations = task_planner.continuations if task_planner is not None else None
    # Compatibility adapters must project the same immutable composition
    # snapshot used by the capability.  A configured gateway without existing
    # egress consent remains local-only.
    runtime.composition_metadata = {
        "companion_chat_remote_usable": companion_chat_resolution.gateway is not None
        and companion_chat_resolution.egress_consented,
        "companion_vision_remote_usable": companion_vision_resolution.gateway is not None
        and getattr(companion_vision_resolution, "adapter_kind", "") == "openai-compatible-vision"
        and companion_vision_resolution.egress_consented,
        "intake_classification_remote_usable": intake_classification_resolution.gateway is not None
        and intake_classification_resolution.egress_consented,
        "memory_candidate_remote_usable": project_skill_resolution.gateway is not None
        and project_skill_resolution.egress_consented,
        "series_intake_organize_remote_usable": series_intake_resolution.gateway is not None
        and series_intake_resolution.egress_consented,
    }
    runtime.mcp_connection_manager = mcp_manager
    runtime.plugin_hands_registration_manager = plugin_hands_manager
    runtime.plugin_hook_runner = plugin_hook_runner if frozen_authorization is not None else None
    runtime.plugin_hook_projection_manager = (
        plugin_hook_projection_manager if frozen_authorization is not None else None
    )
    agent_runtime_composition.bind_runtime(runtime)
    if external_bind is not None:
        external_bind(runtime)
    if external_runner is not None:
        external_runner.configure(runtime=runtime, frozen_authorization=frozen_authorization,
            boundary_profiles=boundary_profiles, turn_binding_guard=turn_binding_guard,
            execution_boundary=execution_boundary, application=application)
    if application is not None:
        application.state.external_runner = external_runner
        application.state.ai_turn_store = session_store
        application.state.ai_runtime = runtime
        application.state.ai_mcp_connection_manager = mcp_manager
        application.state.plugin_tool_registration_manager = plugin_tool_manager
        application.state.plugin_hands_registration_manager = plugin_hands_manager
        application.state.plugin_hook_projection_manager = (
            plugin_hook_projection_manager if frozen_authorization is not None else None
        )
        _ensure_recursive_evolution_runtime(
            application=application,
            container=container,
            turns=session_store,
            profiles=agent_runtime_composition.profiles,
            world=personal_world,
            policy_authority=evolution_policy,
        )
    return runtime
