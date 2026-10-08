"""Recognition task capability for the preserved AI Turn runtime.

The recognition authority owns prompts, source revisions and document bodies.
This capability only asks that authority for an already-confirmed packet, runs
the governed model call, and commits through its fenced callback.  Turn
payloads deliberately contain only opaque task/document identities.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

from backend.memory_app.kernel.ai_execution_control import begin_nested_model_call, execution_control_from
from core.ai_kernel import CapabilityDefinition, TurnPayloadStorePort, validate_turn_presentation_artifact
from core.ai_kernel.dispatcher import ToolDispatchCancelled, ToolProviderFailure
from core.ai_kernel.event_store import RunLeaseRevoked
from core.ai_tooling import tool_from_capability


RECOGNITION_TASK_CAPABILITY = "recognition.task.execute"
RECOGNITION_TASK_LOCAL_CAPABILITY = "recognition.task.execute.local"
RECOGNITION_TASK_OUTCOME = "recognition.task.result"
_RECEIPT_KIND = "recognition-task-receipt-v1"


def recognition_task_capability_definition(*, local: bool = False) -> CapabilityDefinition:
    """Declare approval-gated task tools with separate local/remote boundaries.

    The local tool coordinates a loopback model and document write. The
    original external tool retains provider boundary admission for remote
    routing. Both keep the frozen model and source-egress checks.
    """

    definition = CapabilityDefinition(
        RECOGNITION_TASK_LOCAL_CAPABILITY if local else RECOGNITION_TASK_CAPABILITY,
        1,
        "write" if local else "external",
        True,
        "receipt_required",
        "crp://default/contracts/recognition-task-request.schema.json",
        "crp://default/contracts/recognition-task-result.schema.json",
    )
    return replace(
        definition,
        tool_definition=replace(tool_from_capability(definition), nested_model_handle_budget=1,
                                timeout_ms=240_000),
    )


class RecognitionTaskCapability:
    """Execute a pre-confirmed recognition packet once and retain an ID receipt."""

    def __init__(
        self,
        *,
        models: object,
        payloads: TurnPayloadStorePort,
        load_task: Callable[[Mapping[str, object]], Mapping[str, object]],
        commit_result: Callable[[Mapping[str, object], Mapping[str, object], str, Mapping[str, object]], Mapping[str, object]],
    ) -> None:
        self._models = models
        self._payloads = payloads
        self._load_task = load_task
        self._commit_result = commit_result

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        turn_id = _required(request.get("turn_id"), "turn id")
        requested_task_id = _required(_arguments(request).get("task_id"), "task id")
        # This read is intentionally performed before replaying a receipt. A
        # revoked source packet must never be made visible by a historical Turn
        # receipt, and it must never cause another model call.
        loaded = self._load_confirmed_task(request, requested_task_id)
        receipt = self._payloads.get_immutable_payload(turn_id, _RECEIPT_KIND)
        if receipt is not None:
            receipt_ref, stored = receipt
            artifact = _receipt_artifact(stored, loaded, turn_id)
            return {
                "summary": "Recognition task replayed from its immutable receipt",
                "receipt_ref": receipt_ref,
                "payload_ref": None,
                "evidence_refs": _evidence_refs(loaded),
                "result": artifact,
            }
        # The authority may have committed the document before a process
        # stopped between that commit and the immutable Turn receipt.  It is
        # authoritative, source-fenced evidence of the outcome, so repair the
        # metadata receipt without issuing a second provider request.
        existing_result = loaded.get("existing_result")
        if existing_result is not None:
            public = _committed_result(existing_result, loaded)
            artifact, receipt_ref = self._store_receipt(turn_id, loaded, public)
            return {
                "summary": "Recognition task recovered its immutable receipt",
                "receipt_ref": receipt_ref,
                "payload_ref": None,
                "evidence_refs": _evidence_refs(loaded),
                "result": artifact,
            }

        complete = getattr(self._models, "complete_governed", None)
        if not callable(complete):
            raise ToolProviderFailure("recognition.task.model_unavailable", effect_certainty="confirmed_none")
        try:
            control = execution_control_from(request)
            if control is None:
                raise ValueError("recognition task execution control is unavailable")
            control.checkpoint()
            nested = begin_nested_model_call(
                request, invocation_key="recognition-task", purpose="primary",
            )
        except BaseException as error:
            _raise_control_or_provider(error, request, "recognition.task.execution_unavailable", "confirmed_none")

        try:
            answer, model_result = complete(
                list(loaded["messages"]),
                routing_snapshot=loaded["routing_snapshot"],
                execution_control=control,
                metadata_sink=nested,
                wire_attempt_sink=nested,
                max_tokens=7000,
                **({"validate_current": loaded["validate_current"]} if loaded.get("validate_current") is not None else {}),
            )
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("recognition task model output is invalid")
        except BaseException as error:
            _finalize_failed(nested)
            _raise_control_or_provider(
                error, request, "recognition.task.model_failed", "unknown", preserve_provider=False,
            )

        try:
            model_evidence_refs = nested.finalize(error_code=None)
            if not isinstance(model_evidence_refs, tuple) or not all(isinstance(item, str) for item in model_evidence_refs):
                raise ValueError("recognition task model receipt is invalid")
            committed = self._commit_result(
                request,
                loaded,
                answer,
                _commit_metadata(loaded, model_result, model_evidence_refs),
            )
            public = _committed_result(committed, loaded)
            artifact, receipt_ref = self._store_receipt(turn_id, loaded, public)
        except BaseException as error:
            # A governed model wire has completed.  Neither a failing receipt
            # write nor a late authority conflict can honestly claim no effect.
            _raise_control_or_provider(
                error, request, "recognition.task.commit_failed", "unknown", preserve_provider=False,
            )
        return {
            "summary": "Recognition task completed",
            "receipt_ref": receipt_ref,
            "payload_ref": None,
            "evidence_refs": [*_evidence_refs(loaded), *model_evidence_refs],
            "result": artifact,
        }

    def _store_receipt(
        self, turn_id: str, loaded: Mapping[str, object], public: Mapping[str, object],
    ) -> tuple[dict[str, object], str]:
        artifact = validate_turn_presentation_artifact({
            "schema_version": "1.0.0", "kind": RECOGNITION_TASK_OUTCOME, "content": dict(public),
        })
        receipt_payload = {
            "schema_version": "1.0.0",
            "kind": _RECEIPT_KIND,
            "turn_id": turn_id,
            "task_id": public["task_id"],
            "project_id": loaded["project_id"],
            "context_packet_id": loaded["context_packet_id"],
            "document_id": public["document_id"],
            "document_revision": public["document_revision"],
        }
        receipt_ref = self._payloads.get_or_create_immutable_payload(turn_id, _RECEIPT_KIND, receipt_payload)
        return artifact, receipt_ref

    def _load_confirmed_task(self, request: Mapping[str, object], requested_task_id: str) -> dict[str, object]:
        try:
            loaded = self._load_task(request)
            if not isinstance(loaded, Mapping):
                raise ValueError("recognition task authority result is invalid")
            result = dict(loaded)
            task_id = _required(result.get("task_id"), "loaded task id")
            project_id = _required(result.get("project_id"), "loaded project id")
            packet_id = _required(result.get("context_packet_id"), "context packet id")
            scope = request.get("scope")
            if task_id != requested_task_id or not isinstance(scope, Mapping) or scope.get("project_id") != project_id:
                raise ValueError("recognition task authority identity drifted")
            messages = result.get("messages")
            if not isinstance(messages, list) or not messages or not all(isinstance(item, Mapping) for item in messages):
                raise ValueError("recognition task messages are invalid")
            routing = result.get("routing_snapshot")
            if not isinstance(routing, Mapping):
                raise ValueError("recognition task routing snapshot is invalid")
            # Canonicalize only the outer envelope.  Message text is consumed
            # directly by the model call and never put into Turn payloads.
            if "existing_result" in result and result["existing_result"] is not None and not isinstance(result["existing_result"], Mapping):
                raise ValueError("recognition task existing result is invalid")
            validator = result.get("validate_current")
            if validator is not None and not callable(validator):
                raise ValueError("recognition task authority validator is invalid")
            return {
                "task_id": task_id,
                "project_id": project_id,
                "context_packet_id": packet_id,
                "messages": [dict(item) for item in messages],
                "routing_snapshot": dict(routing),
                "validate_current": validator,
                "existing_result": (
                    dict(result["existing_result"])
                    if isinstance(result.get("existing_result"), Mapping)
                    else None
                ),
            }
        except BaseException as error:
            _raise_control_or_provider(error, request, "recognition.task.source_invalid", "confirmed_none")


def _arguments(request: Mapping[str, object]) -> Mapping[str, object]:
    value = request.get("arguments")
    if not isinstance(value, Mapping):
        raise ValueError("recognition task arguments are required")
    return value


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _committed_result(value: object, loaded: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"task_id", "document_id", "document_revision"}:
        raise ValueError("recognition task commit result is invalid")
    task_id = _required(value.get("task_id"), "committed task id")
    document_id = _required(value.get("document_id"), "committed document id")
    revision = value.get("document_revision")
    if task_id != loaded["task_id"] or not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("recognition task commit identity drifted")
    return {"task_id": task_id, "document_id": document_id, "document_revision": revision}


def _receipt_artifact(value: object, loaded: Mapping[str, object], turn_id: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "kind", "turn_id", "task_id", "project_id", "context_packet_id", "document_id", "document_revision",
    }:
        raise ValueError("recognition task receipt is invalid")
    if value.get("schema_version") != "1.0.0" or value.get("kind") != _RECEIPT_KIND or value.get("turn_id") != turn_id:
        raise ValueError("recognition task receipt identity drifted")
    if any(value.get(key) != loaded[key] for key in ("task_id", "project_id", "context_packet_id")):
        raise ValueError("recognition task receipt authority drifted")
    public = _committed_result({key: value.get(key) for key in ("task_id", "document_id", "document_revision")}, loaded)
    return validate_turn_presentation_artifact({
        "schema_version": "1.0.0", "kind": RECOGNITION_TASK_OUTCOME, "content": public,
    })


def _commit_metadata(loaded: Mapping[str, object], model_result: object, evidence_refs: tuple[str, ...]) -> dict[str, object]:
    """Pass only metadata to authority; answer text never enters this mapping."""

    metadata: dict[str, object] = {
        "task_id": loaded["task_id"], "project_id": loaded["project_id"],
        "context_packet_id": loaded["context_packet_id"], "model_evidence_refs": evidence_refs,
    }
    if isinstance(model_result, Mapping):
        for key in ("model", "configuration_revision", "usage"):
            if key in model_result:
                metadata[key] = model_result[key]
    return metadata


def _evidence_refs(loaded: Mapping[str, object]) -> list[str]:
    return [f"crp://recognition/tasks/{loaded['task_id']}", f"crp://recognition/context-packets/{loaded['context_packet_id']}"]


def _finalize_failed(nested: object) -> None:
    try:
        nested.finalize(error_code="ai.nested_model_failed")
    except Exception:
        # The surrounding outcome remains unknown: a model wire may have been
        # attempted even when finalizing its metadata fails.
        pass


def _raise_control_or_provider(
    error: BaseException,
    request: Mapping[str, object],
    code: str,
    certainty: str,
    *,
    preserve_provider: bool = True,
) -> None:
    if isinstance(error, (RunLeaseRevoked, ToolDispatchCancelled)):
        raise error
    try:
        control = execution_control_from(request)
        if control is not None:
            control.checkpoint()
    except BaseException as stopped:
        raise stopped from None
    if isinstance(error, ToolProviderFailure) and preserve_provider:
        raise error
    raise ToolProviderFailure(code, effect_certainty=certainty) from error
