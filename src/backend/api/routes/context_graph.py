from __future__ import annotations

from backend.security.device_identity import DeviceIdentity, server_mode, server_identity

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.context_binding_composition import (
    ContextBindingCompositionRequest,
    ContextBindingCompositionResult,
)
from backend.api.context_graph_import_composition import (
    ContextGraphImportRequest,
    ContextGraphImportResult,
)
from backend.api.context_graph_import_selection_authority import (
    ContextGraphImportSelectionError,
    ContextGraphImportSelectionReceipt,
)
from backend.api.context_graph_replay_composition import (
    ContextGraphReplayCompositionError,
    PreparedReplayTurn,
    ReplayCompletionResult,
    ReplayPlanCommand,
)
from backend.api.context_benchmark_composition import (
    ContextBenchmarkCompositionError,
    ContextBenchmarkPrepareCommand,
)
from backend.api.context_benchmark_run_authority import ContextBenchmarkRunError
from backend.api.context_binding_runtime import ContextBindingRegistryError
from backend.api.desktop_session import (
    DESKTOP_SESSION_HEADER,
    DesktopSession,
    desktop_session,
    desktop_session_authorized,
)
from backend.security import DesktopFileGrantError, verify_desktop_file_grant


router = APIRouter(tags=["context-graph"])

_FIELDS = {
    "binding_id", "project_id", "graph_id", "graph_revision", "token_budget",
    "acknowledge_staleness", "allow_remote",
}
_RETIRED_FIELDS = {
    "binding", "capability_id", "capability_revision", "expected_revision",
    "permission_grant", "revisions", "staleness_input",
}
_IMPORT_SELECTION_FIELDS = {
    "asset_id", "command_id", "project_id", "source_type",
}
_IMPORT_FIELDS = {
    "command_id", "confirm_read", "expected_predecessor", "project_id",
    "selection_id", "source_type",
}
_REPLAY_PLAN_FIELDS = {
    "command_id", "project_id", "graph_id", "graph_revision", "binding_ref",
    "confirm_replay", "allow_remote", "consent_refs",
}
_BENCHMARK_PREPARE_FIELDS = {
    "run_id", "suite_run_id", "project_id", "confirm_benchmark", "consent_refs",
    "replicate_index",
}
_BENCHMARK_SUBMIT_FIELDS = {"confirm"}


@router.post("/api/rebuild/context-benchmark-runs")
async def create_context_benchmark_run(request: Request) -> JSONResponse:
    """Freeze one confirmed remote benchmark plan for its desktop session."""

    session = _benchmark_session(request)
    if session is None:
        return _benchmark_response(403, "benchmark_desktop_session_required")
    body = await _json_body(request)
    if not isinstance(body, Mapping) or set(body) != _BENCHMARK_PREPARE_FIELDS:
        return _benchmark_response(400, "benchmark_prepare_request_invalid")
    runtime = _benchmark_runtime(request)
    if runtime is None:
        return _benchmark_response(503, "benchmark_authority_unavailable")
    try:
        if _boolean(body.get("confirm_benchmark"), "benchmark confirmation") is not True:
            return _benchmark_response(409, "benchmark_confirmation_required")
        consent_refs = _text_list(body.get("consent_refs"), "consent refs")
        if not consent_refs:
            return _benchmark_response(409, "benchmark_remote_consent_required")
        plan = runtime.prepare(ContextBenchmarkPrepareCommand(
            run_id=_text(body.get("run_id"), "run id"),
            suite_run_id=_text(body.get("suite_run_id"), "suite run id"),
            project_id=_text(body.get("project_id"), "project id"),
            session_id=_benchmark_session_id(session),
            actor_id=f"desktop:{session.instance_id}",
            consent_refs=consent_refs,
            confirmed=True,
            replicate_index=_nonnegative_integer(body.get("replicate_index"), "replicate index"),
        ))
        summary = _benchmark_plan_summary(plan, runtime)
    except (ContextBenchmarkCompositionError, ContextBenchmarkRunError) as error:
        return _benchmark_response(_benchmark_error_status(str(error)), str(error))
    except (AttributeError, TypeError, ValueError):
        return _benchmark_response(400, "benchmark_prepare_request_invalid")
    return _response(201, summary)


@router.get("/api/rebuild/context-benchmark-runs/{run_id}")
async def get_context_benchmark_run(run_id: str, request: Request) -> JSONResponse:
    """Return a content-free status projection for the bound benchmark run."""

    session = _benchmark_session(request)
    if session is None:
        return _benchmark_response(403, "benchmark_desktop_session_required")
    runtime = _benchmark_runtime(request)
    if runtime is None:
        return _benchmark_response(503, "benchmark_authority_unavailable")
    try:
        plan = runtime.get(run_id, session_id=_benchmark_session_id(session))
        statuses = runtime.statuses(run_id, session_id=_benchmark_session_id(session))
        summary = _benchmark_plan_summary(plan, runtime, statuses=statuses)
    except (ContextBenchmarkCompositionError, ContextBenchmarkRunError) as error:
        return _benchmark_response(_benchmark_error_status(str(error)), str(error))
    except (AttributeError, TypeError, ValueError):
        return _benchmark_response(503, "benchmark_authority_unavailable")
    return _response(200, summary)


@router.post("/api/rebuild/context-benchmark-runs/{run_id}/turns/{turn_id}/submit")
async def submit_context_benchmark_turn(
    run_id: str, turn_id: str, request: Request,
) -> JSONResponse:
    """Submit exactly one frozen ordinary Turn; client envelopes are forbidden."""

    session = _benchmark_session(request)
    if session is None:
        return _benchmark_response(403, "benchmark_desktop_session_required")
    body = await _json_body(request)
    if not isinstance(body, Mapping) or set(body) != _BENCHMARK_SUBMIT_FIELDS:
        return _benchmark_response(400, "benchmark_submit_request_invalid")
    runtime = _benchmark_runtime(request)
    if runtime is None:
        return _benchmark_response(503, "benchmark_authority_unavailable")
    try:
        if _boolean(body.get("confirm"), "benchmark Turn confirmation") is not True:
            return _benchmark_response(409, "benchmark_turn_confirmation_required")
        runtime.submit(
            run_id, turn_id, session_id=_benchmark_session_id(session), confirmed=True,
        )
    except (ContextBenchmarkCompositionError, ContextBenchmarkRunError) as error:
        return _benchmark_response(_benchmark_error_status(str(error)), str(error))
    except (AttributeError, TypeError, ValueError):
        return _benchmark_response(400, "benchmark_submit_request_invalid")
    return _response(202, {"run_id": run_id, "turn_id": turn_id, "submitted": True})


@router.post("/api/rebuild/context-benchmark-runs/{run_id}/finalize")
async def finalize_context_benchmark_run(run_id: str, request: Request) -> JSONResponse:
    """Materialize the generic immutable suite result after all ordinary Turns end."""

    session = _benchmark_session(request)
    if session is None:
        return _benchmark_response(403, "benchmark_desktop_session_required")
    body = await _json_body(request)
    if body != {}:
        return _benchmark_response(400, "benchmark_finalize_request_invalid")
    runtime = _benchmark_runtime(request)
    if runtime is None:
        return _benchmark_response(503, "benchmark_authority_unavailable")
    try:
        result = runtime.finalize(run_id, session_id=_benchmark_session_id(session))
        artifact_ref = getattr(result, "artifact_ref", None)
        suite = getattr(result, "suite", None)
        if not isinstance(artifact_ref, str) or not artifact_ref or not is_dataclass(suite):
            return _benchmark_response(503, "benchmark_finalization_invalid")
        suite_result = asdict(suite)
    except (ContextBenchmarkCompositionError, ContextBenchmarkRunError) as error:
        return _benchmark_response(_benchmark_error_status(str(error)), str(error))
    except (AttributeError, TypeError, ValueError):
        return _benchmark_response(503, "benchmark_finalization_invalid")
    return _response(200, {"run_id": run_id, "artifact_ref": artifact_ref, "suite_result": suite_result})


@router.post("/api/rebuild/context-graph-replays")
async def create_context_graph_replay(request: Request) -> JSONResponse:
    """Freeze a confirmed stale replay; this endpoint never submits a Turn."""

    authorized, session = _local_authorized(request)
    if not authorized:
        return _replay_response(403, "replay_desktop_session_required")
    body = await _json_body(request)
    if not isinstance(body, Mapping) or set(body) != _REPLAY_PLAN_FIELDS:
        return _replay_response(400, "replay_plan_request_invalid")
    service = getattr(request.app.state, "context_graph_replay_service", None)
    registry = getattr(request.app.state, "context_graph_runtime", None)
    bindings = getattr(registry, "bindings", None)
    create = getattr(service, "create_plan", None)
    resolve = getattr(bindings, "resolve", None)
    if not callable(create) or not callable(resolve):
        return _replay_response(503, "replay_authority_unavailable")
    try:
        if _boolean(body.get("confirm_replay"), "replay confirmation") is not True:
            return _replay_response(409, "replay_confirmation_required")
        allow_remote = _boolean(body.get("allow_remote"), "remote policy")
        consent_refs = _text_list(body.get("consent_refs"), "consent refs")
        if allow_remote and not consent_refs:
            return _replay_response(409, "replay_remote_consent_required")
        project_id = _text(body.get("project_id"), "project id")
        binding_ref = _text(body.get("binding_ref"), "binding ref")
        snapshot = resolve(binding_ref, project_id=project_id)
        plan = create(ReplayPlanCommand(
            command_id=_text(body.get("command_id"), "command id"),
            project_id=project_id,
            graph_id=_text(body.get("graph_id"), "graph id"),
            source_graph_revision=_text(body.get("graph_revision"), "graph revision"),
            binding_ref=binding_ref,
            binding=snapshot.binding,
            session_id=_replay_session_id(session),
            allow_remote=allow_remote,
            consent_refs=consent_refs,
        ))
    except ContextBindingRegistryError:
        return _replay_response(409, "replay_binding_unavailable")
    except ContextGraphReplayCompositionError as error:
        return _replay_response(_replay_error_status(str(error)), str(error))
    except (AttributeError, TypeError, ValueError):
        return _replay_response(400, "replay_plan_request_invalid")
    return _response(201, {
        "replay_plan_id": plan.replay_plan_id,
        "project_id": plan.project_id,
        "graph_id": plan.graph_id,
        "source_graph_revision": plan.source_graph_revision,
        "result_graph_revision": plan.result_graph_revision,
        "node_ids": list(plan.node_ids),
    })


@router.post("/api/rebuild/context-graph-replays/{replay_plan_id}/next")
async def prepare_context_graph_replay_turn(
    replay_plan_id: str, request: Request,
) -> JSONResponse:
    """Return the one persisted ordinary Turn envelope, without submitting it."""

    authorized, session = _local_authorized(request)
    if not authorized:
        return _replay_response(403, "replay_desktop_session_required")
    body = await _json_body(request)
    if body != {}:
        return _replay_response(400, "replay_next_request_invalid")
    service = getattr(request.app.state, "context_graph_replay_service", None)
    assert_session = getattr(service, "assert_session", None)
    prepare = getattr(service, "prepare_next", None)
    if not callable(assert_session) or not callable(prepare):
        return _replay_response(503, "replay_authority_unavailable")
    try:
        assert_session(replay_plan_id, _replay_session_id(session))
        prepared = prepare(replay_plan_id)
    except ContextGraphReplayCompositionError as error:
        return _replay_response(_replay_error_status(str(error)), str(error))
    except (AttributeError, TypeError, ValueError):
        return _replay_response(503, "replay_preparation_invalid")
    if prepared is None:
        return _response(200, {"replay_plan_id": replay_plan_id, "complete": True})
    if not isinstance(prepared, PreparedReplayTurn):
        return _replay_response(503, "replay_preparation_invalid")
    replay = prepared.request
    return _response(200, {
        "replay_plan_id": replay.replay_plan_id,
        "replay_request_id": replay.replay_request_id,
        "node_id": replay.node_id,
        "plan_index": replay.plan_index,
        "turn_envelope": dict(prepared.turn_envelope),
    })


@router.post("/api/rebuild/context-graph-replays/{replay_plan_id}/accept")
async def accept_context_graph_replay_turn(
    replay_plan_id: str, request: Request,
) -> JSONResponse:
    """Read a governed Turn's durable proof and accept it into the replay."""

    authorized, session = _local_authorized(request)
    if not authorized:
        return _replay_response(403, "replay_desktop_session_required")
    body = await _json_body(request)
    if body != {}:
        return _replay_response(400, "replay_accept_request_invalid")
    service = getattr(request.app.state, "context_graph_replay_service", None)
    evidence = getattr(request.app.state, "ai_turn_effect_store", None)
    assert_session = getattr(service, "assert_session", None)
    accept = getattr(service, "accept_completed_turn", None)
    if not callable(assert_session) or not callable(accept) or evidence is None:
        return _replay_response(503, "replay_evidence_authority_unavailable")
    try:
        assert_session(replay_plan_id, _replay_session_id(session))
        result = accept(replay_plan_id, evidence)
    except ContextGraphReplayCompositionError as error:
        return _replay_response(_replay_error_status(str(error)), str(error))
    except (AttributeError, TypeError, ValueError):
        return _replay_response(503, "replay_completion_invalid")
    if not isinstance(result, ReplayCompletionResult):
        return _replay_response(503, "replay_completion_invalid")
    receipt = result.receipt
    response: dict[str, object] = {
        "replay_plan_id": receipt.replay_plan_id,
        "replay_request_id": receipt.replay_request_id,
        "node_id": receipt.node_id,
        "plan_index": receipt.plan_index,
        "receipt_ref": receipt.receipt_ref,
        "complete": result.finalized_snapshot is not None,
        "idempotent": result.idempotent,
    }
    if result.finalized_snapshot is not None:
        response["result_graph_revision"] = result.finalized_snapshot.graph_revision
    return _response(200, response)


@router.post("/api/rebuild/context-graph-import-selections")
async def create_context_graph_import_selection(request: Request) -> JSONResponse:
    """Mint one opaque, session-bound selection for a signed desktop file grant."""

    authorized, session = _local_authorized(request)
    if not authorized or not isinstance(session, DesktopSession):
        return _response(403, {
            "detail": "LineMap file selection requires desktop main authorization",
            "code": "desktop_file_selection_required",
        })
    body = await _json_body(request)
    if not isinstance(body, Mapping) or set(body) != _IMPORT_SELECTION_FIELDS:
        return _response(400, {
            "detail": "LineMap import selection body rejected",
            "code": "import_selection_request_invalid",
        })
    registry = getattr(request.app.state, "context_graph_import_registry", None)
    resolve = getattr(registry, "resolve", None)
    authority = getattr(
        request.app.state, "context_graph_import_selection_authority", None,
    )
    create = getattr(authority, "create", None)
    if not callable(resolve) or not callable(create):
        return _response(503, {
            "detail": "LineMap import authority is unavailable",
            "code": "import_authority_unavailable",
        })
    try:
        project_id = _text(body.get("project_id"), "project id")
        source_type = _text(body.get("source_type"), "source type")
        command_id = _text(body.get("command_id"), "command id")
        asset_id = _text(body.get("asset_id"), "asset id")
        if resolve(source_type) is None:
            return _response(409, {
                "detail": "LineMap importer is unavailable",
                "code": "importer_unavailable",
            })
        file_grant = verify_desktop_file_grant(
            {key.lower(): value for key, value in request.headers.items()},
            session_secret=session.secret,
            session_instance_id=session.instance_id,
        )
        receipt = create(
            command_id,
            project_id,
            source_type,
            asset_id,
            f"desktop:{session.instance_id}",
            session.instance_id,
            file_grant=file_grant,
        )
    except DesktopFileGrantError:
        return _response(403, {
            "detail": "LineMap file grant rejected",
            "code": "desktop_file_grant_rejected",
        })
    except ContextGraphImportSelectionError as error:
        return _response(_selection_error_status(str(error)), {
            "detail": "LineMap import selection rejected",
            "code": str(error),
        })
    except (TypeError, ValueError):
        return _response(400, {
            "detail": "LineMap import selection request rejected",
            "code": "import_selection_request_invalid",
        })
    if not isinstance(receipt, ContextGraphImportSelectionReceipt):
        return _response(503, {
            "detail": "LineMap import selection evidence is invalid",
            "code": "import_selection_evidence_invalid",
        })
    return _response(201, receipt.to_dict())


@router.post("/api/rebuild/context-graphs/imports")
async def import_context_graph(request: Request) -> JSONResponse:
    """Import one consumed selection and return a content-free graph preview."""

    authorized, session = _local_authorized(request)
    if not authorized or not isinstance(session, DesktopSession):
        return _response(403, {
            "detail": "LineMap import requires a desktop session",
            "code": "desktop_import_session_required",
        })
    body = await _json_body(request)
    if not isinstance(body, Mapping) or set(body) != _IMPORT_FIELDS:
        return _response(400, {
            "detail": "LineMap import body rejected",
            "code": "import_request_invalid",
        })
    service = getattr(request.app.state, "context_graph_import_service", None)
    execute = getattr(service, "import_file", None)
    if not callable(execute):
        return _response(503, {
            "detail": "LineMap import authority is unavailable",
            "code": "import_authority_unavailable",
        })
    try:
        command = ContextGraphImportRequest(
            project_id=_text(body.get("project_id"), "project id"),
            source_type=_text(body.get("source_type"), "source type"),
            command_id=_text(body.get("command_id"), "command id"),
            selection_id=_text(body.get("selection_id"), "selection id"),
            actor_id=f"desktop:{session.instance_id}",
            session_instance_id=session.instance_id,
            confirm_read=_boolean(body.get("confirm_read"), "read confirmation"),
            expected_predecessor=_optional_text(
                body.get("expected_predecessor"), "expected predecessor",
            ),
        )
        result = execute(command)
    except (TypeError, ValueError):
        return _response(400, {
            "detail": "LineMap import request rejected",
            "code": "import_request_invalid",
        })
    if not isinstance(result, ContextGraphImportResult):
        return _response(503, {
            "detail": "LineMap import authority returned invalid evidence",
            "code": "import_result_invalid",
        })
    if not result.ok:
        assert result.error_code is not None
        return _response(_import_error_status(result.error_code), {
            "detail": "LineMap import rejected",
            "code": result.error_code,
        })
    if result.preview is None or result.evidence is None or result.record is None:
        return _response(503, {
            "detail": "LineMap import evidence is incomplete",
            "code": "import_result_incomplete",
        })
    preview = result.preview
    evidence = result.evidence
    return _response(201, {
        "project_id": preview.project_id,
        "graph_id": preview.graph_id,
        "graph_revision": preview.graph_revision,
        "source_type": preview.source_type,
        "source_revision": preview.source_revision,
        "node_count": preview.node_count,
        "edge_count": preview.edge_count,
        "selected_outputs": list(preview.selected_outputs),
        "token_estimate": preview.token_estimate,
        "integrity_issue_codes": list(preview.integrity_issue_codes),
        "evidence_ref": preview.evidence_ref,
        "importer_id": evidence.importer_id,
        "importer_revision": evidence.importer_revision,
        "capability_id": evidence.capability_id,
        "capability_revision": evidence.capability_revision,
    })


@router.post("/api/rebuild/context-bindings")
async def create_context_binding(request: Request) -> JSONResponse:
    """Compile a stored graph through Core authority and register its Binding."""

    authorized, session = _local_authorized(request)
    if not authorized:
        return _response(403, {"detail": "ContextBinding creation is local-only"})
    body = await _json_body(request)
    if not isinstance(body, Mapping):
        return _response(400, {"detail": "ContextBinding body rejected"})
    if set(body) & _RETIRED_FIELDS:
        return _response(410, {
            "detail": "Client-authored ContextBinding payload submission is retired",
            "code": "binding_payload_submission_retired",
        })
    if set(body) != _FIELDS:
        return _response(400, {"detail": "ContextBinding body rejected"})

    service = getattr(
        request.app.state, "context_binding_composition_service", None,
    )
    create = getattr(service, "create", None)
    if not callable(create):
        return _response(503, {
            "detail": "ContextBinding composition authority is unavailable",
            "code": "composition_authority_unavailable",
        })
    try:
        command = ContextBindingCompositionRequest(
            project_id=_text(body.get("project_id"), "project id"),
            graph_id=_text(body.get("graph_id"), "graph id"),
            graph_revision=_text(body.get("graph_revision"), "graph revision"),
            binding_id=_text(body.get("binding_id"), "binding id"),
            token_budget=_positive_integer(body.get("token_budget"), "token budget"),
            acknowledge_staleness=_boolean(
                body.get("acknowledge_staleness"), "staleness acknowledgement",
            ),
            actor_id=_replay_session_id(session),
            allow_remote=_boolean(body.get("allow_remote"), "remote policy"),
        )
        result = create(command)
    except (TypeError, ValueError) as error:
        return _response(400, {
            "detail": "ContextBinding composition request rejected",
            "reason": str(error),
        })
    if not isinstance(result, ContextBindingCompositionResult):
        return _response(503, {
            "detail": "ContextBinding composition authority returned invalid evidence",
            "code": "composition_result_invalid",
        })
    if not result.ok:
        assert result.error is not None
        return _response(_error_status(result.error.code), {
            "detail": "ContextBinding composition rejected",
            "code": result.error.code,
            "reason": result.error.detail,
            "staleness": _preview(result),
        })
    if result.binding is None or not isinstance(result.registry_record, Mapping):
        return _response(503, {
            "detail": "ContextBinding composition evidence is incomplete",
            "code": "composition_result_incomplete",
        })
    record = result.registry_record
    return _response(200 if result.idempotent else 201, {
        "binding_id": record.get("binding_id"),
        "project_id": record.get("project_id"),
        "binding_ref": record.get("binding_ref"),
        "registry_revision": record.get("registry_revision"),
        "graph_id": result.binding.graph_id,
        "graph_revision": result.binding.graph_revision,
        "total_token_cost": result.binding.total_token_cost,
        "staleness": _preview(result),
        "idempotent": result.idempotent,
    })


async def _json_body(request: Request) -> Mapping[str, object] | None:
    try:
        value: Any = await request.json()
    except Exception:
        return None
    return value if isinstance(value, Mapping) else None


def _local_authorized(request: Request) -> tuple[bool, DesktopSession | DeviceIdentity | None]:
    if server_mode(request):
        identity = server_identity(request)
        return identity is not None, identity
    host = request.client.host if request.client is not None else ""
    if host not in {"127.0.0.1", "::1", "testclient"}:
        return False, None
    try:
        session = desktop_session()
    except RuntimeError:
        return False, None
    if session is None:
        return True, None
    return desktop_session_authorized(
        request.headers.get(DESKTOP_SESSION_HEADER),
    ), session


def _benchmark_session(request: Request) -> DesktopSession | None:
    """External benchmark egress never accepts the local-development session."""

    authorized, session = _local_authorized(request)
    return session if authorized and isinstance(session, DesktopSession) else None


def _benchmark_runtime(request: Request) -> object | None:
    factory = getattr(request.app.state, "context_benchmark_runtime_factory", None)
    if not callable(factory):
        return None
    try:
        return factory(request)
    except (AttributeError, LookupError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _benchmark_session_id(session: DesktopSession) -> str:
    return f"desktop:{session.instance_id}"


def _benchmark_plan_summary(
    plan: object,
    runtime: object,
    *,
    statuses: object | None = None,
) -> dict[str, object]:
    """Project only durable identity/revision/status facts, never Turn payloads."""

    required = (
        "run_id", "suite_run_id", "project_id", "replicate_index", "capability_id",
        "capability_revision", "revisions",
    )
    if any(not hasattr(plan, field) for field in required):
        raise ValueError("benchmark plan projection is invalid")
    if statuses is None:
        statuses = runtime.statuses(
            getattr(plan, "run_id"), session_id=getattr(plan, "session_id"),
        )
    if not isinstance(statuses, tuple):
        statuses = tuple(statuses)
    projected_statuses = []
    for status in statuses:
        turn_id = getattr(status, "turn_id", None)
        state = getattr(status, "state", None)
        terminal_event_type = getattr(status, "terminal_event_type", None)
        if not isinstance(turn_id, str) or not isinstance(state, str):
            raise ValueError("benchmark status projection is invalid")
        if terminal_event_type is not None and not isinstance(terminal_event_type, str):
            raise ValueError("benchmark status projection is invalid")
        projected_statuses.append({
            "turn_id": turn_id,
            "state": state,
            "terminal_event_type": terminal_event_type,
        })
    revisions = getattr(plan, "revisions")
    revision_fields = (
        "capability_revision", "boundary_revision", "provider_revision",
        "model_route_revision", "compiler_revision",
    )
    revision_payload = {
        field: getattr(revisions, field, None) for field in revision_fields
    }
    if not all(isinstance(value, str) and value for value in revision_payload.values()):
        raise ValueError("benchmark revision projection is invalid")
    return {
        "run_id": getattr(plan, "run_id"),
        "suite_run_id": getattr(plan, "suite_run_id"),
        "project_id": getattr(plan, "project_id"),
        "replicate_index": getattr(plan, "replicate_index"),
        "capability_id": getattr(plan, "capability_id"),
        "capability_revision": getattr(plan, "capability_revision"),
        "revisions": revision_payload,
        "turns": projected_statuses,
    }


def _preview(result: ContextBindingCompositionResult) -> Mapping[str, object] | None:
    preview = result.preview
    if preview is None:
        return None
    return {
        "graph_id": preview.graph_id,
        "graph_revision": preview.graph_revision,
        "confirmation_required": preview.confirmation_required,
        "affected_node_ids": list(preview.affected_node_ids),
        "replay_order": list(preview.replay_order),
        "stale_reasons": [list(item) for item in preview.stale_reasons],
    }


def _error_status(code: str) -> int:
    if code == "graph_revision_unavailable":
        return 404
    if code == "permission_denied":
        return 403
    if code in {
        "active_capability_unavailable", "binding_identity_drift",
        "binding_registry_drift", "binding_registry_rejected",
        "capability_revision_drift", "compiler_extension_missing",
        "graph_scope_drift", "revision_drift",
        "staleness_confirmation_required", "unsupported_core_api",
        "unsupported_current_compiler",
    }:
        return 409
    if code in {"current_revisions_unavailable", "graph_snapshot_unavailable"}:
        return 503
    return 400


def _selection_error_status(code: str) -> int:
    if code == "context_graph_import_asset_unavailable":
        return 404
    if code in {
        "context_graph_import_selection_clock_invalid",
        "context_graph_import_selection_consume_conflict",
        "context_graph_import_selection_object_store_invalid",
        "context_graph_import_selection_store_invalid",
        "context_graph_import_selection_write_conflict",
    }:
        return 503
    return 409


def _import_error_status(code: str) -> int:
    if code in {
        "importer_execution_unavailable", "import_result_incomplete",
    }:
        return 503
    if code == "file_authorization_rejected":
        return 403
    if code in {
        "importer_unavailable", "selection_rejected", "snapshot_conflict",
        "snapshot_rejected",
    }:
        return 409
    return 400


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is invalid")
    return value.strip()


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} is invalid")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= 9_999:
        raise ValueError(f"{label} is invalid")
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} is invalid")
    return value


def _text_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} are invalid")
    values = tuple(_text(item, label) for item in value)
    if len(values) != len(set(values)):
        raise ValueError(f"{label} are invalid")
    return values


def _replay_session_id(session: DesktopSession | DeviceIdentity | None) -> str:
    if isinstance(session, DeviceIdentity):
        return f'device:{session.device_id}'
    return (
        f"desktop:{session.instance_id}"
        if session is not None else "local-development-session"
    )


def _replay_response(status: int, code: str) -> JSONResponse:
    return _response(status, {
        "detail": "LineMap replay request rejected",
        "code": code,
    })


def _replay_error_status(code: str) -> int:
    if code in {"replay_plan_unavailable"}:
        return 404
    if code == "replay_session_drift":
        return 403
    if code in {
        "replay_source_graph_drift", "replay_revision_drift",
        "replay_staleness_not_confirmed",
        "replay_turn_evidence_rejected", "replay_not_complete",
        "replay_remote_consent_required",
    }:
        return 409
    if code in {
        "replay_plan_write_conflict", "replay_request_write_conflict",
        "replay_clock_unavailable", "replay_source_graph_unavailable",
    }:
        return 503
    return 400


def _benchmark_error_status(code: str) -> int:
    if code in {"benchmark run is unavailable"}:
        return 404
    if code in {"benchmark session drifted"}:
        return 403
    if code in {
        "benchmark confirmation is unavailable",
        "benchmark preparation requires confirmation",
    }:
        return 409
    if code in {
        "benchmark frozen revisions drifted", "benchmark Turn is already submitted",
        "benchmark Turns are not all completed", "benchmark requires remote routing",
        "benchmark confirmation reference drifted",
    }:
        return 409
    if any(token in code for token in (
        "authority drifted", "capability revision drifted", "frozen plan drifted",
        "identity drifted", "request drifted",
    )):
        return 409
    if any(token in code for token in (
        "unavailable", "invalid evidence", "registry", "submitter identity drifted",
    )):
        return 503
    return 400


def _benchmark_response(status: int, code: str) -> JSONResponse:
    return _response(status, {
        "detail": "LineMap benchmark request rejected",
        "code": code,
    })


def _response(status: int, body: Mapping[str, object]) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=dict(body), headers={"Cache-Control": "no-store"},
    )
