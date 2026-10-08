"""Governed AI Turn capabilities for Companion screen analysis.

The desktop upload grant remains the authority for the temporary image.  A
Turn may inspect its public metadata while it is being planned, but can only
consume image pixels from the approved capability.  In particular, neither
the planner nor a persisted Turn context ever receives a path, digest, or
pixel payload.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from backend.api.desktop_session import desktop_session
from backend.companion_prompt_runtime import load_active_character_prompt
from backend.model_runtime import ModelRuntimeError
from backend.model_routing_snapshot import (
    turn_model_routing_snapshot_revision,
    validate_turn_model_routing_snapshot,
)
from core.ai_kernel import CapabilityDefinition, TurnPayloadStorePort, validate_turn_presentation_artifact
from core.companion_core import CompanionRepository
from core.model_gateway import ModelExecutionControlPort, ModelGatewayPort, ModelRequest

from backend.api.ai_execution_control import begin_nested_model_call, execution_checkpoint


COMPANION_VISION_OUTCOME = "companion.vision.analyze"
COMPANION_VISION_CONTEXT_CAPABILITY = "companion.vision.context.read"
COMPANION_VISION_ANALYZE_CAPABILITY = "companion.vision.analyze.write"

PromptProfileLoader = Callable[[], tuple[str, int, int]]


class ScopedCompanionVisionContextCapability:
    """Resolve the current desktop grant at invocation time.

    The shared Turn runtime deliberately cannot retain a grant store from the
    request that first created it.  This wrapper obtains only the already
    issued app-state authority and rejects a missing or cross-instance store;
    it never constructs, issues, or repairs a grant.
    """

    def __init__(self, *, container: object, application: object | None) -> None:
        self._runtime_root = Path(getattr(container, "root_dir")).resolve()
        self._application = application

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        return CompanionVisionContextCapability(
            grant_store=self._grant_store(),
            prompt_profile_loader=self._prompt_profile,
        ).invoke(request)

    def _grant_store(self) -> object:
        session = desktop_session()
        state = getattr(self._application, "state", None)
        store = getattr(state, "companion_vision_grant_store", None)
        if session is None or store is None or getattr(store, "session_id", None) != session.instance_id:
            raise ValueError("Companion Vision grant authority is unavailable")
        return store

    def _prompt_profile(self) -> tuple[str, int, int]:
        return _current_prompt_profile(self._runtime_root)


class ScopedCompanionVisionAnalyzeCapability:
    """Use the same request-owned grant authority at the approved write step."""

    def __init__(
        self,
        *,
        container: object,
        application: object | None,
        gateway: ModelGatewayPort | None,
        receipt_store: TurnPayloadStorePort,
    ) -> None:
        self._context = ScopedCompanionVisionContextCapability(
            container=container, application=application,
        )
        self._gateway = gateway
        self._receipt_store = receipt_store

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        return CompanionVisionAnalyzeCapability(
            grant_store=self._context._grant_store(),
            prompt_profile_loader=self._context._prompt_profile,
            gateway=self._gateway,
            receipt_store=self._receipt_store,
        ).invoke(request)


@dataclass(frozen=True, slots=True)
class VisionGrantMetadata:
    """The only grant projection eligible for an AI Turn payload."""

    grant_id: str
    media_type: str
    byte_length: int

    def as_dict(self) -> dict[str, object]:
        return {"grant_id": self.grant_id, "media_type": self.media_type, "byte_length": self.byte_length}


class CompanionVisionContextCapability:
    """Read a still-live upload grant without consuming its image."""

    def __init__(self, *, grant_store: object, prompt_profile_loader: PromptProfileLoader) -> None:
        self._grant_store = grant_store
        self._prompt_profile_loader = prompt_profile_loader

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        arguments = _arguments(request)
        request_id = _required(arguments.get("request_id"), "request_id")
        project_id = _required(arguments.get("project_id"), "project_id")
        question = _question(arguments.get("question"))
        grant_id = _required(arguments.get("grant_id"), "grant_id")
        # inspect is deliberately non-consuming.  The grant store is expected
        # to reject expired or revoked grants here without exposing its file.
        grant = self._grant_store.inspect(grant_id)
        metadata = _metadata(grant)
        _prompt, prompt_revision, profile_revision = _prompt_profile(self._prompt_profile_loader)
        return {
            "summary": "Companion Vision context is ready",
            "receipt_ref": None,
            "payload_ref": None,
            "evidence_refs": [f"crp://default/companion/vision/grants/{metadata.grant_id}"],
            "result": {
                "schema_version": "1.0.0",
                "kind": "companion.vision.context",
                "request_id": request_id,
                "project_id": project_id,
                "question": question,
                "grant": metadata.as_dict(),
                "baseline": {"prompt_revision": prompt_revision, "profile_revision": profile_revision},
            },
        }


class CompanionVisionAnalyzeCapability:
    """Consume pixels and call the vision gateway only after approval."""

    def __init__(
        self,
        *,
        grant_store: object,
        prompt_profile_loader: PromptProfileLoader,
        gateway: ModelGatewayPort | None,
        receipt_store: TurnPayloadStorePort,
        local_answer: str = "我现在不能连接视觉模型，不过我已经收到你的问题。",
    ) -> None:
        self._grant_store = grant_store
        self._prompt_profile_loader = prompt_profile_loader
        self._gateway = gateway
        self._receipt_store = receipt_store
        self._local_answer = local_answer

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        turn_id = _required(request.get("turn_id"), "turn_id")
        arguments = _arguments(request)
        context = arguments.get("context")
        if not isinstance(context, Mapping):
            raise ValueError("Companion Vision write requires context")
        request_id, project_id, question, baseline, expected_metadata = _context(context)
        _prompt, prompt_revision, profile_revision = _prompt_profile(self._prompt_profile_loader)
        if baseline["prompt_revision"] != prompt_revision or baseline["profile_revision"] != profile_revision:
            raise ValueError("Companion Vision authority baseline is stale")
        # Re-inspect immediately before consumption.  This catches a replaced,
        # expired, or otherwise drifted grant while keeping any hash private to
        # the grant store / invocation stack.
        inspected = self._grant_store.inspect(expected_metadata.grant_id)
        current_metadata = _metadata(inspected)
        if current_metadata != expected_metadata:
            raise ValueError("Companion Vision grant baseline is stale")
        execution_control = execution_checkpoint(request)
        expected_public = _public_grant(inspected)
        grant, pixels = self._grant_store.consume_expected(expected_metadata.grant_id, expected_public)
        consumed = _metadata(grant)
        if consumed != expected_metadata:
            raise ValueError("Companion Vision grant baseline is stale")
        allow_remote = arguments.get("allow_remote") is True
        model_result = None
        model_evidence_refs: tuple[str, ...] = ()
        if self._gateway is not None and allow_remote:
            try:
                model_routing_snapshot, model_routing_snapshot_ref = _vision_model_routing_snapshot(
                    self._receipt_store,
                    turn_id=turn_id,
                    project_id=project_id,
                )
                nested_model = begin_nested_model_call(request)
                nested_error_code: str | None = None
                try:
                    model_result = self._gateway.invoke(ModelRequest(
                        capability="vision",
                        input=question,
                        parameters={
                            "messages": _messages(_prompt, question),
                            # Deliberately invocation-local: this is never returned or
                            # persisted in a Turn payload, event, receipt, or artifact.
                            "image_payload": {"media_type": consumed.media_type, "pixels": pixels},
                            "temperature": 0,
                            "_routing_project_id": project_id,
                            "_model_routing_snapshot": model_routing_snapshot,
                            "_model_routing_snapshot_ref": model_routing_snapshot_ref,
                            "_model_routing_snapshot_revision": turn_model_routing_snapshot_revision(
                                model_routing_snapshot
                            ),
                        },
                        privacy_scope="remote_allowed",
                        execution_control=execution_control,
                        metadata_sink=nested_model,
                    ))
                except Exception:
                    nested_error_code = "ai.nested_model_failed"
                    raise
                finally:
                    model_evidence_refs = nested_model.finalize(
                        error_code=nested_error_code,
                    )
            except (ModelRuntimeError, ValueError):
                model_result = None
        if model_result is None:
            answer = self._local_answer
            provider_id, model_name, called = "local-fallback", "", False
        else:
            answer = _answer(model_result.output)
            provider_id, model_name, called = model_result.provider or "model-gateway", model_result.model, True
        content = {
            "status": "completed", "request_id": request_id, "text": answer,
            "provider_id": provider_id, "model_name": model_name,
            "provider_call_performed": called, "replayed": False,
        }
        artifact = validate_turn_presentation_artifact({
            "schema_version": "1.0.0",
            "kind": COMPANION_VISION_OUTCOME,
            "content": content,
        })
        receipt_ref = self._receipt_store.put(turn_id, "companion-vision-receipt", artifact)
        return {
            "summary": "Companion Vision analysis completed",
            "receipt_ref": receipt_ref,
            "payload_ref": None,
            "evidence_refs": [
                f"crp://default/companion/vision/grants/{expected_metadata.grant_id}",
                *model_evidence_refs,
            ],
            "result": artifact,
        }


def _vision_model_routing_snapshot(
    payloads: TurnPayloadStorePort,
    *,
    turn_id: str,
    project_id: str,
) -> tuple[dict[str, object], str]:
    stored = payloads.get_immutable_payload(turn_id, "turn-model-routing-snapshot-v1")
    if stored is None:
        raise ValueError("Companion Vision model routing snapshot is unavailable")
    snapshot = validate_turn_model_routing_snapshot(stored[1])
    turn = snapshot["turn"]
    project = snapshot["project"]
    requirement = snapshot["requirement"]
    if not all(isinstance(item, Mapping) for item in (turn, project, requirement)):
        raise ValueError("Companion Vision model routing identity is unavailable")
    if (
        turn.get("turn_id") != turn_id
        or project.get("project_id") != project_id
        or requirement.get("required_capability") != "vision"
        or requirement.get("modality") != "image_input"
        or requirement.get("privacy_scope") != "remote_allowed"
    ):
        raise ValueError("Companion Vision model routing identity drifted")
    return snapshot, stored[0]


class CompanionVisionTurnPlanner:
    """A deterministic planner that never calls a model or consumes a grant."""

    def plan(
        self, request: Mapping[str, object], events: Sequence[Mapping[str, object]],
        capabilities: Sequence[CapabilityDefinition], payloads: TurnPayloadStorePort,
        execution_control: ModelExecutionControlPort | None = None,
    ) -> Mapping[str, object]:
        completed = _completed(events, COMPANION_VISION_ANALYZE_CAPABILITY)
        if completed is not None:
            data = completed.get("data")
            return {"type": "complete", "summary": "Companion Vision analysis completed", "payload_ref": data.get("payload_ref") if isinstance(data, Mapping) else None, "evidence_refs": list(data.get("evidence_refs") or ()) if isinstance(data, Mapping) else []}
        context_event = _completed(events, COMPANION_VISION_CONTEXT_CAPABILITY)
        if context_event is None:
            question, grant_id = _input(request)
            return {"type": "tool", "capability_id": COMPANION_VISION_CONTEXT_CAPABILITY, "arguments": {"request_id": str(request["operation_id"]), "project_id": _project_id(request), "question": question, "grant_id": grant_id}}
        data = context_event.get("data")
        ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        if not isinstance(ref, str):
            raise ValueError("Companion Vision context payload is unavailable")
        context = payloads.get(ref)
        if not isinstance(context, Mapping) or context.get("kind") != "companion.vision.context":
            raise ValueError("Companion Vision context payload is invalid")
        return {"type": "tool", "capability_id": COMPANION_VISION_ANALYZE_CAPABILITY, "arguments": {"context": dict(context), "allow_remote": isinstance(request.get("privacy"), Mapping) and request["privacy"].get("allow_remote") is True}}


def _arguments(request: Mapping[str, object]) -> Mapping[str, object]:
    value = request.get("arguments")
    if not isinstance(value, Mapping):
        raise ValueError("capability arguments are required")
    return value


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _question(value: object) -> str:
    question = _required(value, "question")
    if len(question) > 2_000 or any(ord(char) < 32 and char not in "\n\t" for char in question):
        raise ValueError("Companion Vision question is invalid")
    return question


def _prompt_profile(loader: PromptProfileLoader) -> tuple[str, int, int]:
    prompt, prompt_revision, profile_revision = loader()
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(prompt_revision, int) or prompt_revision < 1 or not isinstance(profile_revision, int) or profile_revision < 1:
        raise ValueError("Companion Vision prompt authority is unavailable")
    return prompt, prompt_revision, profile_revision


def _current_prompt_profile(runtime_root: Path) -> tuple[str, int, int]:
    """Read prompt and profile revisions for each capability invocation."""

    prompt, prompt_revision = load_active_character_prompt(runtime_root)
    repository = CompanionRepository.at_data_root(runtime_root)
    repository.initialize()
    profile = repository.get_master_profile()
    profile_revision = profile.revision if profile is not None else 1
    return prompt, prompt_revision, profile_revision


def _metadata(grant: object) -> VisionGrantMetadata:
    public = _public_grant(grant)
    grant_id = _required(public.get("grant_id"), "grant_id")
    media_type = public.get("media_type")
    byte_length = public.get("byte_length")
    if media_type not in {"image/jpeg", "image/png"} or not isinstance(byte_length, int) or isinstance(byte_length, bool) or byte_length < 1:
        raise ValueError("Companion Vision grant is invalid")
    return VisionGrantMetadata(grant_id, str(media_type), byte_length)


def _public_grant(grant: object) -> Mapping[str, object]:
    value = getattr(grant, "public", None)
    public = value() if callable(value) else grant
    if not isinstance(public, Mapping):
        raise ValueError("Companion Vision grant is invalid")
    return public


def _context(
    context: Mapping[str, object],
) -> tuple[str, str, str, Mapping[str, int], VisionGrantMetadata]:
    if context.get("kind") != "companion.vision.context":
        raise ValueError("Companion Vision context is invalid")
    request_id = _required(context.get("request_id"), "request_id")
    project_id = _required(context.get("project_id"), "project_id")
    question = _question(context.get("question"))
    baseline = context.get("baseline")
    grant = context.get("grant")
    if not isinstance(baseline, Mapping) or not isinstance(grant, Mapping):
        raise ValueError("Companion Vision context is invalid")
    prompt_revision, profile_revision = baseline.get("prompt_revision"), baseline.get("profile_revision")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in (prompt_revision, profile_revision)):
        raise ValueError("Companion Vision context baseline is invalid")
    return request_id, project_id, question, {"prompt_revision": prompt_revision, "profile_revision": profile_revision}, _metadata(grant)


def _project_id(request: Mapping[str, object]) -> str:
    scope = request.get("scope")
    if isinstance(scope, Mapping):
        project_id = scope.get("project_id")
        if isinstance(project_id, str) and project_id.strip():
            return project_id.strip()
    return "default"


def _messages(prompt: str, question: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": prompt}, {"role": "user", "content": f"请仅依据可见画面回答：{question}"}]


def _answer(output: object) -> str:
    value = output.get("text", output.get("answer")) if isinstance(output, Mapping) else output
    answer = _required(value, "Companion Vision model response")
    if len(answer) > 4_000:
        raise ValueError("Companion Vision model response is invalid")
    return answer


def _input(request: Mapping[str, object]) -> tuple[str, str]:
    input_payload = request.get("input")
    if not isinstance(input_payload, Mapping):
        raise ValueError("Companion Vision Turn input is required")
    question = _question(input_payload.get("text"))
    refs = input_payload.get("refs")
    if not isinstance(refs, list):
        raise ValueError("Companion Vision Turn refs are invalid")
    grant_ids = [_required(item.get("object_id"), "grant_id") for item in refs if isinstance(item, Mapping) and item.get("kind") == "companion_vision_grant"]
    if len(grant_ids) != 1 or len(refs) != 1:
        raise ValueError("Companion Vision Turn requires exactly one grant ref")
    return question, grant_ids[0]


def _completed(events: Sequence[Mapping[str, object]], capability_id: str) -> Mapping[str, object] | None:
    return next((event for event in reversed(events) if event.get("type") == "tool.completed" and isinstance(event.get("data"), Mapping) and event["data"].get("capability_id") == capability_id), None)
