from __future__ import annotations

import os
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.access_log import (
    AccessLogBuffer,
    install_access_log_filters,
    make_access_log_entry,
    should_capture_request,
)
from backend.api.bootstrap import ApiContainer
from backend.api.container import build_default_container
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.desktop_session import (
    DESKTOP_SESSION_HEADER,
    bind_desktop_request_session,
    desktop_session,
    desktop_session_for_header,
    reset_desktop_request_session,
)
from backend.api.external_apply_startup import (
    backfill_external_apply_effects,
    register_external_apply_handler,
)
from backend.api.external_project_skill_apply_startup import (
    backfill_external_project_skill_apply_effects,
    register_external_project_skill_apply_handler,
)
from backend.api.external_extension_runtime_startup import (
    register_external_extension_runtime,
    register_external_extension_recovery,
)
from backend.api.effect_partition_inventory import (
    AI_TURNS_EFFECT_PARTITION,
    PRIMARY_EFFECT_PARTITION,
    PPT_MASTER_EFFECT_PARTITION,
    register_enabled_effect_partitions,
)
from backend.api.memory_publication_effect_runtime import register_memory_publication_handler
from backend.api.retention_effect_runtime import register_retention_effect_handlers
from backend.api.external_series_apply_startup import quarantine_legacy_external_series_apply_sagas
from backend.api.external_series_candidate_startup import (
    backfill_external_series_candidate_effects,
    register_external_series_candidate_handler,
)
from backend.api.external_agent_publication_change_startup import (
    backfill_external_agent_publication_effects,
    backfill_external_agent_publication_changes,
    register_external_agent_publication_handler,
    dispatch_memory_invalidation_changes,
)
from backend.api.memory_import_batch_startup import recover_memory_import_batches
from backend.api.memory_lifecycle_batch_startup import recover_memory_lifecycle_batches
from backend.api.job_execution_runtime import register_job_execution_handler
from backend.api.capability_package_runtime import (
    compile_capability_package_contributions,
    compose_capability_package_catalog,
)
from backend.api.context_benchmark_composition import (
    build_context_benchmark_runtime_factory,
)
from backend.api.context_graph_runtime import build_context_graph_runtime
from backend.api.mcp_runtime import MCPRemoteStatusRecoverySession, shutdown_ai_mcp_runtime
from backend.api.mcp_recovery_probe import MCPRemoteEffectRecoveryProbe
from backend.api.plugin_hands_runtime import (
    backfill_plugin_hands_upgrade_effects,
    register_plugin_hands_cleanup_handler,
    register_plugin_hands_execution_handler,
    register_plugin_hands_upgrade_handler,
    shutdown_plugin_hands_runtime,
)
from backend.api.ai_turn_runner import shutdown_ai_turn_runner
from backend.api.ai_turn_recovery_startup import scan_due_ai_turn_recovery
from backend.api.ai_turn_recovery_worker import (
    shutdown_ai_turn_recovery_worker,
    start_ai_turn_recovery_worker,
)
from backend.api.ai_runtime import build_ai_runtime, get_or_build_ai_runtime
from backend.api.personal_world_model_terminal_observer import (
    PersonalWorldModelTerminalObserver,
)
from backend.api.workbench_ai_runtime import WORLD_PROJECT_SESSION_ID
from backend.api.ppt_master_runtime_bootstrap import (
    bootstrap_ppt_master_runtime,
    build_ppt_master_effect_runtime,
)
from core.ai_kernel import SQLiteAITurnStore, SQLiteAgentStore
from core.effect_log import (
    EffectClass,
    EffectBackfillRegistration,
    EffectHandlerRegistration,
    CoordinationTaskRegistration,
    EffectRecoveryCoordinator,
    EffectRecoveryRegistration,
    EffectRecoveryService,
    EffectState,
    EffectWorkflowHandler,
    RecoveryPreparationRegistration,
    WORKFLOW_EFFECT_CLASSES,
    WORKFLOW_EFFECT_KINDS,
    build_effect_runtime,
)
from core.plugin_hands.durable_lifecycle import (
    backfill_plugin_hands_execution_effects,
    verify_plugin_hands_effect,
)
from core.product_core.workflow_handler_governance import (
    MAIN_WORKFLOW_HANDLER_KINDS,
    INLINE_WORKFLOW_EFFECT_KINDS,
    AI_WORKFLOW_EFFECT_KINDS,
    validate_workflow_handler_governance,
)
from core.plugin_host.hands_upgrade import (
    PluginHandsUpgradeConflict,
    PluginHandsUpgradeError,
    decode_plugin_hands_upgrade_record,
)
from core.storage_provider import SQLiteStructuredRecord, SQLiteStructuredRecordStore
from backend.api.team_memory_source_forget_startup import (
    backfill_team_memory_source_forget_effects,
    register_team_memory_source_forget_handler,
)
from backend.api.fresh_vault_shared_trust_audit_startup import (
    bootstrap_fresh_vault_shared_trust_audit_on_startup,
)
from backend.api.project_skill_review_staging_startup import (
    backfill_project_skill_review_staging_effects,
    register_project_skill_review_staging_handler,
)
from backend.api.memory_publication_review_staging_startup import (
    backfill_memory_review_staging_effects,
    register_memory_review_staging_handler,
)
from backend.api.shared_trust_audit_activation_startup import (
    backfill_shared_trust_activation_effects,
    register_shared_trust_activation_handler,
)
from backend.api.routes import include_api_routers
from backend.api.routes.product.job_lifecycle import shutdown_rebuild_job_lifecycle
from backend.api.static_assets import mount_frontend_dist
from backend.api.worker_auth import (
    WORKER_SECRET_HEADER,
    WorkerRequestAuthenticator,
    worker_config,
)
from backend.api.streamed_asset_startup import cleanup_stale_streamed_asset_parts
from backend.companion_scheduler_runtime import CompanionSchedulerRuntime
from backend.companion_weather_runtime import CompanionWeatherRuntime
from backend.replay.series_workspace import SeriesWorkspace
from core.companion_core import build_companion_clock, clean_stale_vision_grants, clean_stale_voice_grants


LOGGER = logging.getLogger(__name__)
_PLUGIN_HANDS_UPGRADE_STARTUP_SCAN_LIMIT = 64
_PLUGIN_HANDS_UPGRADE_TERMINAL_STAGES = frozenset({"completed", "rolled_back", "finalized"})
_PLUGIN_HANDS_UPGRADE_STAGES = frozenset({
    "prepared", "old_revoked", "new_switched", "new_registered", "completed",
    "rollback_prepared", "rollback_new_revoked", "rollback_old_switched",
    "rollback_old_registered", "rolled_back", "finalized",
})
_AGENT_RUNTIME_STARTUP_RECOVERY_LIMIT = 128

def _recover_agent_runtime_coordination(
    application: FastAPI,
    *,
    runtime_factory: Callable[[], object] | None = None,
) -> dict[str, int | str]:
    """Repair topology, then replay organization work through the sole runner."""
    composition = getattr(application.state, "agent_runtime_composition", None)
    if composition is None and runtime_factory is not None:
        try:
            runtime_factory()
        except Exception as exc:
            LOGGER.warning(
                "agent_runtime_startup_recovery_runtime_build_failed",
                extra={
                    "event": "agent_runtime_startup_recovery_runtime_build_failed",
                    "error_type": type(exc).__name__,
                },
            )
        composition = getattr(application.state, "agent_runtime_composition", None)
    store = getattr(composition, "store", None)
    coordinator = getattr(composition, "coordinator", None)
    if store is None or coordinator is None:
        report: dict[str, int | str] = {"status": "skipped", "candidates": 0, "reconciled": 0, "failed": 0}
    else:
        try:
            candidates = tuple(store.list_recovery_candidates(limit=_AGENT_RUNTIME_STARTUP_RECOVERY_LIMIT))
        except Exception as exc:
            LOGGER.warning("agent_runtime_startup_recovery_scan_failed", extra={"event": "agent_runtime_startup_recovery_scan_failed", "error_type": type(exc).__name__})
            report = {"status": "scan_failed", "candidates": 0, "reconciled": 0, "failed": 1}
        else:
            reconciled = failed = 0
            for candidate in candidates:
                try:
                    coordinator.reconcile_recovery_candidate(candidate)
                    reconciled += 1
                except Exception as exc:
                    failed += 1
                    LOGGER.warning("agent_runtime_startup_recovery_candidate_failed", extra={"event": "agent_runtime_startup_recovery_candidate_failed", "error_type": type(exc).__name__})
            report = {"status": "completed", "candidates": len(candidates), "reconciled": reconciled, "failed": failed}
    application.state.agent_runtime_startup_recovery = report
    _recover_agent_organization(application)
    _recover_world_supervision_agent_observer(application)
    return report


def _has_world_supervision_recovery_candidate(root_dir: Path) -> bool:
    """Read a bounded Turn/session proof before eagerly composing AI runtime."""

    database = root_dir / ".rebuild-data" / "ai-turns.sqlite3"
    if not database.exists():
        return False
    try:
        turns = SQLiteAITurnStore(database)
        runs = SQLiteAgentStore(database)
        for candidate in runs.list_supervision_candidates(
            limit=_AGENT_RUNTIME_STARTUP_RECOVERY_LIMIT
        ):
            request = turns.get_request(candidate.turn_id)
            if (
                isinstance(request, Mapping)
                and request.get("session_id") == WORLD_PROJECT_SESSION_ID
            ):
                return True
    except Exception:
        return False
    return False


def _recover_agent_organization(application: FastAPI) -> dict[str, int | str]:
    """Run one bounded organization replay without exposing task payloads."""

    runtime = getattr(application.state, "agent_organization_runtime", None)
    recover = getattr(runtime, "recover", None)
    if not callable(recover):
        report: dict[str, int | str] = {
            "status": "skipped", "scanned": 0, "plans": 0,
            "replayed": 0, "failed": 0,
        }
        application.state.agent_organization_startup_recovery = report
        return report
    try:
        result = recover(limit=_AGENT_RUNTIME_STARTUP_RECOVERY_LIMIT)
        if not isinstance(result, Mapping):
            raise TypeError("organization recovery report is invalid")
        scanned = result.get("scanned", 0)
        plans = result.get("plans", ())
        replayed = result.get("replayed_turn_ids", ())
        if (
            not isinstance(scanned, int) or isinstance(scanned, bool) or scanned < 0
            or not isinstance(plans, (tuple, list))
            or not isinstance(replayed, (tuple, list))
        ):
            raise TypeError("organization recovery report is invalid")
        status = result.get("status")
        report = {
            "status": status if isinstance(status, str) else "completed",
            "scanned": scanned, "plans": len(plans),
            "replayed": len(replayed), "failed": 0,
        }
    except Exception as exc:
        LOGGER.warning("agent_organization_startup_recovery_failed", extra={"event": "agent_organization_startup_recovery_failed", "error_type": type(exc).__name__})
        report = {
            "status": "failed", "scanned": 0, "plans": 0,
            "replayed": 0, "failed": 1,
        }
    application.state.agent_organization_startup_recovery = report
    return report


def _recover_world_supervision_agent_observer(
    application: FastAPI,
) -> dict[str, int | str]:
    """Replay only durable World-review candidates after organization recovery."""

    observer = getattr(application.state, "world_supervision_agent_observer", None)
    recover = getattr(observer, "recover", None)
    if not callable(recover):
        report: dict[str, int | str] = {
            "status": "skipped", "scanned": 0, "recorded": 0,
            "noop": 0, "ignored": 0, "failed": 0,
        }
    else:
        try:
            result = recover(limit=_AGENT_RUNTIME_STARTUP_RECOVERY_LIMIT)
            if not isinstance(result, Mapping):
                raise TypeError("World supervision recovery report is invalid")
            safe_counts = {
                name: result.get(name, 0)
                for name in ("scanned", "recorded", "noop", "ignored")
            }
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in safe_counts.values()
            ):
                raise TypeError("World supervision recovery report is invalid")
            status = result.get("status")
            report = {
                "status": status if isinstance(status, str) else "completed",
                **safe_counts, "failed": 0,
            }
        except Exception as exc:
            LOGGER.warning("world_supervision_agent_startup_recovery_failed", extra={"event": "world_supervision_agent_startup_recovery_failed", "error_type": type(exc).__name__})
            report = {
                "status": "failed", "scanned": 0, "recorded": 0,
                "noop": 0, "ignored": 0, "failed": 1,
            }
    application.state.world_supervision_agent_startup_recovery = report
    return report


def _has_unfinished_plugin_hands_upgrades(root_dir) -> bool:
    """Read only the durable cutover stage before eagerly composing Hands.

    Normal sidecar startup must not provision an AI runtime merely because the
    Plugin Hands subsystem exists.  A recognized non-terminal cutover is the
    narrow exception: it needs the production manager to finish its durable
    revoke/switch/register protocol before ordinary requests may arrive.
    Corrupt or future records are deliberately left untouched and do not turn
    into an eager runtime bootstrap.
    """

    database_path = Path(root_dir) / ".rebuild-data" / "jobs.sqlite3"
    if not database_path.is_file():
        return False
    connection = None
    try:
        connection = sqlite3.connect(
            f"{database_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=0.05,
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 50")
        schema_row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='crp_structured_records'"
        ).fetchone()
        if schema_row is None:
            return False
        rows = connection.execute(
            """
            SELECT object_id, payload_json, revision
            FROM crp_structured_records
            WHERE collection = ?
            ORDER BY object_id ASC
            LIMIT ?
            """,
            ("plugin_hands_upgrade_cutovers", _PLUGIN_HANDS_UPGRADE_STARTUP_SCAN_LIMIT + 1),
        ).fetchall()
        if len(rows) > _PLUGIN_HANDS_UPGRADE_STARTUP_SCAN_LIMIT:
            LOGGER.warning("plugin_hands_upgrade_startup_scan_over_limit", extra={"event": "plugin_hands_upgrade_startup_scan_over_limit"})
            return False
        for object_id, payload_json, revision in rows:
            try:
                payload = json.loads(payload_json)
                if not isinstance(payload, dict):
                    continue
                decoded = decode_plugin_hands_upgrade_record(
                    SQLiteStructuredRecord("plugin_hands_upgrade_cutovers", object_id, payload, revision),
                )
            except (TypeError, ValueError, json.JSONDecodeError, PluginHandsUpgradeError, PluginHandsUpgradeConflict):
                continue
            stage = decoded["stage"]
            if stage in _PLUGIN_HANDS_UPGRADE_STAGES and stage not in _PLUGIN_HANDS_UPGRADE_TERMINAL_STAGES:
                return True
    except (OSError, sqlite3.Error) as exc:
        # Startup recovery remains bounded and isolated.  The durable record
        # is retained for a later explicit recovery instead of guessing a
        # runtime configuration from an unreadable database.
        LOGGER.warning(
            "plugin_hands_upgrade_startup_scan_failed",
            extra={"event": "plugin_hands_upgrade_startup_scan_failed", "error_type": type(exc).__name__},
        )
        return False
    finally:
        if connection is not None:
            connection.close()
    return False


def create_worker_auth_middleware(application: FastAPI) -> None:
    """Authenticate local worker requests without transmitting its long-lived secret."""

    authenticator: WorkerRequestAuthenticator | None = None
    auth_config: tuple[str, str] | None = None

    @application.middleware("http")
    async def worker_auth(request: Request, call_next):
        nonlocal authenticator, auth_config
        from backend.security.device_identity import server_mode, server_authorized
        if server_mode(request):
            state = request.scope.get("state", {})
            if server_authorized(request) or state.get("server_auth_public") or state.get("server_internal_model"):
                return await call_next(request)
            return JSONResponse({"detail": "device_unauthorized"}, status_code=401)
        try:
            session = desktop_session()
        except RuntimeError:
            return JSONResponse({"detail": "desktop_session_config_invalid"}, status_code=503)
        if session is not None:
            if request.method == "OPTIONS":
                return await call_next(request)
            authenticated = desktop_session_for_header(request.headers.get(DESKTOP_SESSION_HEADER))
            if authenticated is None:
                return JSONResponse({"detail": "desktop_session_unauthorized"}, status_code=403)
            token = bind_desktop_request_session(authenticated)
            try:
                return await call_next(request)
            finally:
                reset_desktop_request_session(token)
        try:
            config = worker_config()
        except RuntimeError:
            return JSONResponse({"detail": "worker_auth_config_invalid"}, status_code=503)
        if config is None:
            return await call_next(request)
        # OPTIONS 预检请求不携带自定义头，必须豁免，否则 CORSMiddleware 无法完成预检
        if request.method == "OPTIONS":
            return await call_next(request)
        if request.url.path == "/api/health" and "X-Worker-Challenge" not in request.headers:
            return await call_next(request)
        secret, instance_id, _ = config
        if authenticator is None or auth_config != (secret, instance_id):
            authenticator = WorkerRequestAuthenticator(secret, instance_id)
            auth_config = (secret, instance_id)
        if not authenticator.verify(request, request.headers.get(WORKER_SECRET_HEADER)):
            return JSONResponse({"detail": "Unauthorized"}, status_code=403)
        response = await call_next(request)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if location:
                try:
                    origin = urlsplit(str(request.url))
                    target = urlsplit(urljoin(str(request.url), location))
                    same_origin = (target.scheme, target.hostname, target.port) == (
                        origin.scheme, origin.hostname, origin.port
                    )
                except ValueError:
                    same_origin = False
                if not same_origin:
                    return JSONResponse({"detail": "worker_external_redirect_blocked"}, status_code=502)
        return response


def create_access_log_middleware(application: FastAPI, buffer: AccessLogBuffer) -> None:
    """注册 HTTP access log 采集中间件，写入内存环形缓冲。

    采集规则：跳过 SSE 流、静态资源、健康检查；只记录 method/path/status/
    duration/timestamp，不采集 headers/body/query，天然脱敏。
    """

    @application.middleware("http")
    async def access_log_capture(request: Request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        try:
            duration_ms = int((time.monotonic() - start) * 1000)
            path = request.url.path
            if should_capture_request(path):
                buffer.append(
                    make_access_log_entry(
                        method=request.method,
                        path=path,
                        status=response.status_code,
                        duration_ms=duration_ms,
                        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    )
                )
        except Exception:
            # 采集失败绝不影响主请求
            pass
        return response


def create_app(container: ApiContainer | None = None) -> FastAPI:
    install_access_log_filters()
    application = FastAPI(title="Chriptmas_Replay API")
    from backend.api.routes.realtime_asr import consume_server_ticket
    application.state.server_realtime_ticket_consumer = consume_server_ticket
    try:
        current_desktop_session = desktop_session()
    except RuntimeError:
        current_desktop_session = None
    allowed_origins = [
        "http://127.0.0.1:8001",
        "http://localhost:8001",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
    ]
    if current_desktop_session is not None:
        allowed_origins = [current_desktop_session.allowed_origin]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Series-Id", WORKER_SECRET_HEADER, DESKTOP_SESSION_HEADER],
    )
    create_worker_auth_middleware(application)
    # HTTP access log 内存缓冲（Developer Studio 诊断视图使用）
    access_log_buffer = AccessLogBuffer(capacity=200)
    application.state.access_log_buffer = access_log_buffer
    create_access_log_middleware(application, access_log_buffer)
    resolved_container = container or build_default_container()
    application.state.container = resolved_container
    from .favorites_discovery import discover_bilibili_favorites
    application.state.bilibili_favorites_discovery = discover_bilibili_favorites
    include_api_routers(application)
    root_dir = getattr(resolved_container, "root_dir", None)
    if root_dir is not None:
        workflow_receipts, workflow_settings = build_rebuild_object_store(Path(root_dir))
        application.state.capability_package_catalog = compose_capability_package_catalog(
            Path(root_dir)
        )
        application.state.capability_package_contributions = (
            compile_capability_package_contributions(
                application.state.capability_package_catalog
            )
        )
        context_graph_runtime = build_context_graph_runtime(
            resolved_container,
            application.state.capability_package_catalog,
            workflow_receipts,
            namespace_id=str(workflow_settings.namespace_id),
            contributions=application.state.capability_package_contributions,
        )
        application.state.context_graph_runtime = context_graph_runtime
        application.state.context_graph_snapshots = context_graph_runtime.snapshots
        application.state.context_graph_import_registry = (
            context_graph_runtime.import_registry
        )
        application.state.context_graph_import_selection_authority = (
            context_graph_runtime.import_selections
        )
        application.state.context_graph_import_service = context_graph_runtime.imports
        application.state.context_compilation_facts = (
            context_graph_runtime.compilation_facts
        )
        application.state.context_binding_composition_service = (
            context_graph_runtime.composition
        )
        application.state.context_graph_replay_service = context_graph_runtime.replay
        application.state.effect_runtime = build_effect_runtime(
            PRIMARY_EFFECT_PARTITION.database(Path(root_dir)),
            owner_id=PRIMARY_EFFECT_PARTITION.owner_id(os.getpid()),
            lease_seconds=PRIMARY_EFFECT_PARTITION.lease_seconds,
            lease_heartbeat_seconds=PRIMARY_EFFECT_PARTITION.lease_heartbeat_seconds,
        )
        external_extension_runtime = register_external_extension_runtime(
            Path(root_dir), application.state.effect_runtime,
        )
        # API state publishes the workflow, never a FactStore, Gate authority
        # or raw intake planner.  The limited package callbacks remain on the
        # startup facade for read-only AI composition below.
        application.state.external_extension_runtime = external_extension_runtime
        application.state.external_extension_install_workflow = (
            external_extension_runtime.install_workflow
        )
        ai_effect_runtime = build_effect_runtime(
            AI_TURNS_EFFECT_PARTITION.database(Path(root_dir)),
            owner_id=AI_TURNS_EFFECT_PARTITION.owner_id(os.getpid()),
            lease_seconds=AI_TURNS_EFFECT_PARTITION.lease_seconds,
        )
        application.state.ai_effect_runtime = ai_effect_runtime
        application.state.ppt_master_effect_runtime = build_ppt_master_effect_runtime(
            Path(root_dir),
            owner_id=PPT_MASTER_EFFECT_PARTITION.owner_id(os.getpid()),
        )
        # Must precede recovery registration and service start: this registers
        # the two installer handlers and the fixed-generation handler before
        # any persisted effect can be recovered or dispatched.
        bootstrap_ppt_master_runtime(
            application,
            root_dir=Path(root_dir),
            packaged=current_desktop_session is not None,
        )
        register_job_execution_handler(
            application, Path(root_dir), application.state.effect_runtime,
        )
        effect_recovery = EffectRecoveryCoordinator(application.state.effect_runtime)
        register_external_extension_recovery(
            effect_recovery,
            external_extension_runtime,
        )
        register_enabled_effect_partitions(
            effect_recovery, application.state, root_dir=Path(root_dir),
        )
        ai_turn_effect_store = SQLiteAITurnStore(
            Path(root_dir) / ".rebuild-data" / "ai-turns.sqlite3",
            effect_runner=ai_effect_runtime.runner,
        )
        # Replay completion reads this same governed Turn store.  It does not
        # own a second runner, effect log, lease, or recovery partition.
        application.state.ai_turn_effect_store = ai_turn_effect_store
        application.state.personal_world_model_terminal_observer = (
            PersonalWorldModelTerminalObserver(
                root_dir=Path(root_dir),
                turn_store=ai_turn_effect_store,
            )
        )
        mcp_remote_recovery_probe = MCPRemoteEffectRecoveryProbe(
            ai_turn_effect_store,
            MCPRemoteStatusRecoverySession(
                root_dir=Path(root_dir),
                turn_store=ai_turn_effect_store,
                secret_store=getattr(resolved_container, "secret_store", None),
            ),
        )
        application.state.context_benchmark_runtime_factory = (
            build_context_benchmark_runtime_factory(
                resolved_container,
                application.state.capability_package_catalog,
                application.state.capability_package_contributions,
                context_graph_runtime,
                ai_turn_effect_store,
            )
        )
        for tool_effect_class in (
            EffectClass.PURE,
            EffectClass.IDEMPOTENT,
            EffectClass.QUERYABLE,
            EffectClass.AT_MOST_ONCE,
            EffectClass.NEEDS_REAUTH,
        ):
            ai_effect_runtime.recoveries.register(EffectRecoveryRegistration(
                kind=f"tool_call_{tool_effect_class.value.lower()}",
                effect_class=tool_effect_class,
                probe=(
                    ai_turn_effect_store.verify_tool_call_effect
                    if tool_effect_class is EffectClass.QUERYABLE else None
                ),
                verify=ai_turn_effect_store.verify_tool_call_effect,
                reauthorize=(
                    (lambda _effect: (
                        EffectState.UNKNOWN, "ai.tool_reauthorization_required",
                    ))
                    if tool_effect_class is EffectClass.NEEDS_REAUTH else None
                ),
            ))
        # MCP writes are a QUERYABLE child Effect of the governed Tool call.
        # Its Receipt is immutable and atomically settles that child Effect;
        # Core Reaper owns restart convergence and never guesses or replays a
        # wire call when the Receipt is absent.
        ai_effect_runtime.recoveries.register(EffectRecoveryRegistration(
            kind="mcp_call",
            effect_class=EffectClass.QUERYABLE,
            probe=mcp_remote_recovery_probe,
            verify=mcp_remote_recovery_probe,
        ))
        validate_workflow_handler_governance(
            ai_effect_runtime.recoveries.kinds(),
            expected_kinds=AI_WORKFLOW_EFFECT_KINDS,
            allow_missing=True,
        )
        plugin_hands_records = SQLiteStructuredRecordStore(
            Path(root_dir) / ".rebuild-data" / "jobs.sqlite3"
        )
        application.state.effect_runtime.recoveries.register(EffectRecoveryRegistration(
            kind="plugin_hands_execution",
            effect_class=EffectClass.AT_MOST_ONCE,
            verify=lambda effect: verify_plugin_hands_effect(plugin_hands_records, effect),
        ))
        register_plugin_hands_execution_handler(
            application,
            effect_runtime=application.state.effect_runtime,
            tool_intents=ai_turn_effect_store,
            runtime_factory=lambda: get_or_build_ai_runtime(
                SimpleNamespace(app=application), resolved_container,
            ),
        )
        register_plugin_hands_cleanup_handler(
            root_dir=Path(root_dir), effect_runtime=application.state.effect_runtime,
        )
        from backend.api.routes.product.document_delivery_services import (
            _document_delivery_service,
            _document_pdf_delivery_service,
        )

        document_delivery = _document_delivery_service(
            resolved_container, application.state.effect_runtime.runner,
        )
        document_pdf_delivery = _document_pdf_delivery_service(
            resolved_container, application.state.effect_runtime.runner,
        )
        application.state.effect_runtime.handlers.register(EffectHandlerRegistration(
            kind="document_delivery",
            effect_class=EffectClass.IDEMPOTENT,
            handler=document_delivery.handle_effect,
        ))
        application.state.effect_runtime.recoveries.register(EffectRecoveryRegistration(
            kind="document_delivery",
            effect_class=EffectClass.IDEMPOTENT,
            verify=lambda effect: document_delivery.verify_effect(effect.operation_id),
        ))
        application.state.effect_runtime.recoveries.register(EffectRecoveryRegistration(
            kind="document_pdf_delivery",
            effect_class=EffectClass.QUERYABLE,
            probe=lambda effect: document_pdf_delivery.verify_effect(effect.operation_id),
            verify=lambda effect: document_pdf_delivery.verify_effect(effect.operation_id),
        ))
        workflow_verifier = EffectWorkflowHandler(
            application.state.effect_runtime.runner,
            workflow_receipts,
            namespace_id=workflow_settings.namespace_id,
        )
        from backend.api.routes.settings import register_provider_model_discovery_handler

        register_provider_model_discovery_handler(
            application.state.effect_runtime, resolved_container,
        )
        register_external_apply_handler(
            Path(root_dir), application.state.effect_runtime,
        )
        register_external_project_skill_apply_handler(
            Path(root_dir), application.state.effect_runtime,
        )
        register_memory_publication_handler(
            Path(root_dir), application.state.effect_runtime,
        )
        register_retention_effect_handlers(
            Path(root_dir), application.state.effect_runtime,
        )
        register_external_series_candidate_handler(
            Path(root_dir), application.state.effect_runtime,
        )
        register_project_skill_review_staging_handler(
            Path(root_dir), application.state.effect_runtime,
        )
        register_memory_review_staging_handler(
            Path(root_dir), application.state.effect_runtime,
        )
        register_shared_trust_activation_handler(
            Path(root_dir), application.state.effect_runtime,
        )
        register_team_memory_source_forget_handler(
            Path(root_dir), application.state.effect_runtime,
        )
        register_external_agent_publication_handler(
            Path(root_dir), application.state.effect_runtime,
        )
        register_plugin_hands_upgrade_handler(
            application,
            root_dir=Path(root_dir),
            effect_runtime=application.state.effect_runtime,
        )
        validate_workflow_handler_governance(
            application.state.effect_runtime.handlers.kinds(),
            expected_kinds=MAIN_WORKFLOW_HANDLER_KINDS,
        )
        validate_workflow_handler_governance(
            WORKFLOW_EFFECT_KINDS,
            expected_kinds=INLINE_WORKFLOW_EFFECT_KINDS,
        )
        # The container is composed before the Effect Runtime.  Binding here
        # keeps video-index writes behind the single Core runner/reaper rather
        # than creating an application-local worker during container startup.
        index_refresher = getattr(resolved_container, "workspace_index_refresher", None)
        if index_refresher is not None:
            index_refresher.bind_effect_runtime(application.state.effect_runtime)
        for workflow_effect_kind in WORKFLOW_EFFECT_KINDS:
            if workflow_effect_kind == "provider_model_discovery":
                continue
            application.state.effect_runtime.recoveries.register(
                EffectRecoveryRegistration(
                    kind=workflow_effect_kind,
                    effect_class=WORKFLOW_EFFECT_CLASSES[workflow_effect_kind],
                    probe=(
                        workflow_verifier.verify_effect
                        if WORKFLOW_EFFECT_CLASSES[workflow_effect_kind]
                        is EffectClass.QUERYABLE else None
                    ),
                    verify=workflow_verifier.verify_effect,
                )
            )
        application.state.effect_recovery_coordinator = effect_recovery
        effect_recovery_service = EffectRecoveryService(
            effect_recovery, interval_seconds=1.0, clock=time.time,
        )
        application.state.effect_recovery_service = effect_recovery_service
        companion_scheduler_runtime = CompanionSchedulerRuntime(root_dir, clock=build_companion_clock())
        application.state.companion_scheduler_runtime = companion_scheduler_runtime
        companion_weather_runtime = CompanionWeatherRuntime(root_dir)
        application.state.companion_weather_runtime = companion_weather_runtime

        def start_companion_scheduler() -> None:
            companion_scheduler_runtime.start()

        def stop_companion_scheduler() -> None:
            companion_scheduler_runtime.stop()

        def start_companion_weather() -> None:
            companion_weather_runtime.start()

        def stop_companion_weather() -> None:
            companion_weather_runtime.stop()

        def initialize_series_workspace() -> None:
            workspace = SeriesWorkspace(root_dir)
            workspace.migrate_legacy()
            workspace.record_launch()

        def bootstrap_fresh_shared_trust_audit() -> None:
            bootstrap_fresh_vault_shared_trust_audit_on_startup(application, root_dir)

        def backfill_plugin_hands_executions() -> None:
            backfill_plugin_hands_execution_effects(
                plugin_hands_records,
                application.state.effect_runtime.log,
                now=int(time.time()),
            )

        def prepare_plugin_hands_upgrade_runtime() -> None:
            """Bootstrap AI only for a recognized unfinished Hands cutover."""

            if not _has_unfinished_plugin_hands_upgrades(root_dir):
                return
            runtime = getattr(application.state, "ai_runtime", None)
            if runtime is None:
                # ``get_or_build`` owns the single runtime lock and, while it
                # builds, projects active Hands before resuming the cutover.
                # It also preserves the usual lazy-runtime wiring for later
                # ordinary requests.
                get_or_build_ai_runtime(
                    SimpleNamespace(app=application), resolved_container,
                )
                return
        def backfill_plugin_hands_upgrade_cutovers() -> None:
            backfill_plugin_hands_upgrade_effects(
                root_dir=root_dir, effects=application.state.effect_runtime.log,
            )

        def shutdown_rebuild_jobs() -> None:
            shutdown_rebuild_job_lifecycle(application)

        def shutdown_mcp_connections() -> None:
            shutdown_ai_mcp_runtime(application)

        def shutdown_ai_turns() -> None:
            shutdown_ai_turn_runner(application)

        def shutdown_plugin_hands() -> None:
            shutdown_plugin_hands_runtime(application)

        def scan_due_ai_turns() -> None:
            # Bounded, metadata-only recovery classification; failures are
            # isolated so startup never depends on a recovery pass.
            try:
                database_path = root_dir / ".rebuild-data" / "ai-turns.sqlite3"
                candidate = SQLiteAITurnStore.has_recovery_or_expert_wait_candidate(
                    database_path, now=datetime.now(timezone.utc),
                )
                application.state.ai_turn_recovery_candidate = candidate
                if candidate:
                    scan_due_ai_turn_recovery(SQLiteAITurnStore(database_path))
            except Exception:
                application.state.ai_turn_recovery_candidate = False
                pass

        def resume_safe_ai_turns() -> None:
            # The worker itself rechecks audit + queue + lease atomically.  It
            # is bounded and isolated so an unavailable model never blocks app
            # startup.
            try:
                if not getattr(application.state, "ai_turn_recovery_candidate", False):
                    return
                turn_store = SQLiteAITurnStore(root_dir / ".rebuild-data" / "ai-turns.sqlite3")
                has_expert_job_wait = bool(turn_store.list_expert_job_waits(limit=1))
                if not turn_store.has_pending_safe_recovery() and not has_expert_job_wait:
                    return
                runtime = getattr(application.state, "ai_runtime", None)
                if runtime is None:
                    from backend.api.media_hands_composition import (
                        compose_application_media_hands,
                    )

                    compose_application_media_hands(application, resolved_container)
                    runtime = build_ai_runtime(resolved_container, application=application)
                    application.state.ai_runtime = runtime
                start_ai_turn_recovery_worker(application, turn_store, runtime)
            except Exception:
                pass

        def shutdown_ai_turn_recovery() -> None:
            shutdown_ai_turn_recovery_worker(application)

        def backfill_external_document_applies() -> None:
            backfill_external_apply_effects(
                root_dir, application.state.effect_runtime.log,
            )

        def backfill_external_project_skill_applies() -> None:
            backfill_external_project_skill_apply_effects(
                root_dir, application.state.effect_runtime.log,
            )

        def recover_legacy_external_series_applies() -> None:
            quarantine_legacy_external_series_apply_sagas(application, root_dir)

        def backfill_external_series_candidates() -> None:
            backfill_external_series_candidate_effects(
                root_dir, application.state.effect_runtime.log,
            )

        def prepare_external_agent_publication_outbox() -> None:
            application.state.memory_invalidation_outbox_dispatch = (
                dispatch_memory_invalidation_changes(root_dir)
            )
            application.state.external_agent_publication_backfill = (
                backfill_external_agent_publication_changes(root_dir)
            )

        def backfill_external_agent_publication_outbox() -> None:
            backfill_external_agent_publication_effects(
                root_dir, application.state.effect_runtime.log,
            )

        def backfill_project_skill_review_staging() -> None:
            backfill_project_skill_review_staging_effects(
                root_dir, application.state.effect_runtime.log,
            )

        def backfill_memory_publication_review_staging() -> None:
            backfill_memory_review_staging_effects(
                root_dir, application.state.effect_runtime.log,
            )

        def backfill_shared_trust_audit_activations() -> None:
            backfill_shared_trust_activation_effects(
                root_dir, application.state.effect_runtime.log,
            )

        def recover_stranded_memory_import_batches() -> None:
            recover_memory_import_batches(application, root_dir)

        def recover_confirmed_memory_lifecycle_batches() -> None:
            recover_memory_lifecycle_batches(application, root_dir)

        def recover_agent_runtime_coordination() -> None:
            def build_for_world_supervision_recovery() -> object | None:
                if getattr(
                    application.state,
                    "world_supervision_recovery_probe_complete",
                    False,
                ) is True:
                    return None
                if not _has_world_supervision_recovery_candidate(root_dir):
                    application.state.world_supervision_recovery_probe_complete = True
                    return None
                runtime = get_or_build_ai_runtime(
                    SimpleNamespace(app=application), resolved_container,
                )
                application.state.world_supervision_recovery_probe_complete = True
                return runtime

            _recover_agent_runtime_coordination(
                application,
                runtime_factory=build_for_world_supervision_recovery,
            )

        def backfill_team_memory_source_forget_operations() -> None:
            backfill_team_memory_source_forget_effects(
                root_dir, application.state.effect_runtime.log,
            )

        def cleanup_streamed_asset_parts() -> None:
            cleanup_stale_streamed_asset_parts(root_dir)

        def cleanup_companion_vision_grants() -> None:
            clean_stale_vision_grants()

        def cleanup_companion_voice_grants() -> None:
            clean_stale_voice_grants()

        effect_recovery.register_coordination(CoordinationTaskRegistration(
            kind="ai-turn-scan", run=scan_due_ai_turns,
        ))
        effect_recovery.register_coordination(CoordinationTaskRegistration(
            kind="ai-turn-resume", run=resume_safe_ai_turns,
        ))
        effect_recovery.register_coordination(CoordinationTaskRegistration(
            kind="legacy-series-quarantine", run=recover_legacy_external_series_applies,
        ))
        effect_recovery.register_coordination(CoordinationTaskRegistration(
            kind="memory-import-interruption", run=recover_stranded_memory_import_batches,
        ))
        effect_recovery.register_coordination(CoordinationTaskRegistration(
            kind="memory-lifecycle-batch-resume",
            run=recover_confirmed_memory_lifecycle_batches,
        ))
        effect_recovery.register_coordination(CoordinationTaskRegistration(
            kind="agent-runtime-coordination-recovery",
            run=recover_agent_runtime_coordination,
        ))
        effect_recovery.register_backfill(EffectBackfillRegistration(
            kind="team-memory-source-forget", run=backfill_team_memory_source_forget_operations,
        ))
        effect_recovery.register_backfill(EffectBackfillRegistration(
            kind="external-document-apply", run=backfill_external_document_applies,
        ))
        effect_recovery.register_backfill(EffectBackfillRegistration(
            kind="external-project-skill-apply", run=backfill_external_project_skill_applies,
        ))
        effect_recovery.register_backfill(EffectBackfillRegistration(
            kind="external-series-candidate", run=backfill_external_series_candidates,
        ))
        effect_recovery.register_backfill(EffectBackfillRegistration(
            kind="project-skill-review-staging", run=backfill_project_skill_review_staging,
        ))
        effect_recovery.register_backfill(EffectBackfillRegistration(
            kind="memory-review-staging", run=backfill_memory_publication_review_staging,
        ))
        effect_recovery.register_backfill(EffectBackfillRegistration(
            kind="shared-trust-activation", run=backfill_shared_trust_audit_activations,
        ))
        effect_recovery.register_preparation(RecoveryPreparationRegistration(
            kind="external-agent-publication-outbox-prepare",
            run=prepare_external_agent_publication_outbox,
        ))
        effect_recovery.register_backfill(EffectBackfillRegistration(
            kind="external-agent-publication", run=backfill_external_agent_publication_outbox,
        ))
        effect_recovery.register_preparation(RecoveryPreparationRegistration(
            kind="plugin-hands-upgrade-runtime", run=prepare_plugin_hands_upgrade_runtime,
        ))
        effect_recovery.register_backfill(EffectBackfillRegistration(
            kind="plugin-hands-execution", run=backfill_plugin_hands_executions,
        ))
        effect_recovery.register_backfill(EffectBackfillRegistration(
            kind="plugin-hands-upgrade", run=backfill_plugin_hands_upgrade_cutovers,
        ))

        def recover_external_effects() -> None:
            application.state.effect_recovery_report = effect_recovery.recover_once(
                now=int(time.time()), limit=100,
            )
            effect_recovery_service.start()

        def shutdown_effect_recovery() -> None:
            effect_recovery_service.shutdown(timeout_seconds=2.0)

        application.router.add_event_handler("startup", bootstrap_fresh_shared_trust_audit)
        application.router.add_event_handler("startup", start_companion_scheduler)
        application.router.add_event_handler("startup", start_companion_weather)
        application.router.add_event_handler("startup", initialize_series_workspace)
        application.router.add_event_handler("startup", recover_external_effects)
        application.router.add_event_handler("shutdown", shutdown_rebuild_jobs)
        application.router.add_event_handler("shutdown", shutdown_effect_recovery)
        application.router.add_event_handler("shutdown", shutdown_ai_turn_recovery)
        application.router.add_event_handler("shutdown", shutdown_ai_turns)
        application.router.add_event_handler("shutdown", shutdown_plugin_hands)
        application.router.add_event_handler("shutdown", shutdown_mcp_connections)
        application.router.add_event_handler("shutdown", stop_companion_scheduler)
        application.router.add_event_handler("shutdown", stop_companion_weather)
        application.router.add_event_handler("startup", cleanup_streamed_asset_parts)
        application.router.add_event_handler("startup", cleanup_companion_vision_grants)
        application.router.add_event_handler("startup", cleanup_companion_voice_grants)
        mount_frontend_dist(application, root_dir)
    return application


if os.environ.get('CHRIPTMAS_DEPLOY') == 'server':
    from backend.shared.lazy_application import LazyApplication
    app = LazyApplication(create_app)
else:
    app = create_app()
