"""Approved provider enhancement for a locally classified Workbench input."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
import re
import secrets
from threading import RLock
import time

from core.ai_kernel import CapabilityDefinition, TurnPayloadStorePort, validate_turn_presentation_artifact
from core.model_gateway import (
    ModelCallMetadataSinkPort,
    ModelExecutionControlPort,
    ModelGatewayPort,
    ModelRequest,
)

from backend.api.ai_execution_control import begin_nested_model_call, execution_control_from
from backend.api.turn_model_routing_binding import (
    TurnModelRoutingBinding,
    load_turn_model_routing_binding,
)
from core.product_core.workbench_input_classifier import (
    ClassifyWorkbenchInput,
    EnhanceWorkbenchInputClassification,
    WorkbenchInputClassificationResult,
    serialize_workbench_input_classification,
)


WORKBENCH_INPUT_CLASSIFICATION_OUTCOME = "workbench.input.classification.enhance"
WORKBENCH_INPUT_CLASSIFICATION_CONTEXT_CAPABILITY = "workbench.input.classification.context.read"
WORKBENCH_INPUT_CLASSIFICATION_ENHANCE_CAPABILITY = "workbench.input.classification.enhance.write"

InputLoader = Callable[[str], Mapping[str, object]]
AuthorityLoader = Callable[[], Mapping[str, object]]


class WorkbenchClassificationInputGrantStore:
    """Short-lived authority for classifier input that must not enter a Turn."""

    def __init__(self, *, ttl_seconds: float = 120.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._records: dict[str, tuple[float, dict[str, object]]] = {}
        self._lock = RLock()

    def issue(self, source: Mapping[str, object], *, grant_id: str | None = None) -> str:
        content, media_type, file_name, urls, _revision = _source(source)
        grant_id = _required(grant_id, "input grant id") if grant_id is not None else f"input-grant-{secrets.token_urlsafe(24)}"
        record: dict[str, object] = {
            "content": content,
            "media_type": media_type,
            "file_name": file_name,
            "urls": urls,
            "grant_revision": secrets.token_urlsafe(18),
        }
        with self._lock:
            self._cleanup_locked()
            self._records[grant_id] = (self._clock() + self._ttl_seconds, record)
        return grant_id

    def inspect(self, grant_id: str) -> Mapping[str, object]:
        with self._lock:
            self._cleanup_locked()
            item = self._records.get(_required(grant_id, "input grant id"))
            if item is None:
                raise ValueError("Workbench classification input grant is unavailable")
            return dict(item[1])

    def revoke(self, grant_id: str) -> bool:
        with self._lock:
            return self._records.pop(grant_id, None) is not None

    def _cleanup_locked(self) -> None:
        now = self._clock()
        for grant_id, (expires_at, _source_record) in tuple(self._records.items()):
            if expires_at <= now:
                self._records.pop(grant_id, None)


class ScopedWorkbenchInputClassificationContextCapability:
    def __init__(self, *, application: object | None, authority_loader: AuthorityLoader) -> None:
        self._application = application
        self._authority_loader = authority_loader

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        return WorkbenchInputClassificationContextCapability(
            input_loader=self._store().inspect,
            authority_loader=self._authority_loader,
        ).invoke(request)

    def _store(self) -> WorkbenchClassificationInputGrantStore:
        state = getattr(self._application, "state", None)
        store = getattr(state, "workbench_classification_input_store", None)
        if not isinstance(store, WorkbenchClassificationInputGrantStore):
            raise ValueError("Workbench classification input authority is unavailable")
        return store


class ScopedWorkbenchInputClassificationEnhanceCapability:
    def __init__(self, *, application: object | None, authority_loader: AuthorityLoader, gateway: ModelGatewayPort | None, receipt_store: TurnPayloadStorePort) -> None:
        self._context = ScopedWorkbenchInputClassificationContextCapability(
            application=application, authority_loader=authority_loader,
        )
        self._gateway = gateway
        self._receipt_store = receipt_store

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        return WorkbenchInputClassificationEnhanceCapability(
            input_loader=self._context._store().inspect,
            authority_loader=self._context._authority_loader,
            gateway=self._gateway,
            receipt_store=self._receipt_store,
        ).invoke(request)


class WorkbenchInputClassificationContextCapability:
    """Run the local classifier and retain only a redacted projection."""

    def __init__(self, *, input_loader: InputLoader, authority_loader: AuthorityLoader, classifier: ClassifyWorkbenchInput | None = None) -> None:
        self._input_loader, self._authority_loader = input_loader, authority_loader
        self._classifier = classifier or ClassifyWorkbenchInput()

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        arguments = _arguments(request)
        source_id = _required(arguments.get("source_id"), "source_id")
        source = self._input_loader(source_id)
        content, media_type, file_name, urls, grant_revision = _source(source)
        authority = _authority(self._authority_loader())
        local = self._classifier.execute(content=content, media_type=media_type, file_name=file_name, urls=urls, classifier_prompt=authority["prompt"])
        return {
            "summary": "Workbench input classification context is ready",
            "receipt_ref": None, "payload_ref": None,
            "evidence_refs": [f"crp://default/workbench/inputs/{source_id}"],
            "result": {
                "schema_version": "1.0.0", "kind": "workbench.input.classification.context",
                "source_id": source_id,
                "input": _input_projection(content, media_type, file_name, urls, grant_revision),
                "local_classification": _safe_classification(serialize_workbench_input_classification(local)),
                "baseline": _baseline(authority),
            },
        }


class _GatewayJsonProvider:
    """Adapter that lets the existing enhancer own JSON validation and merge."""

    def __init__(
        self,
        gateway: ModelGatewayPort,
        routing: TurnModelRoutingBinding,
        metadata_sink: ModelCallMetadataSinkPort,
        execution_control: ModelExecutionControlPort | None = None,
    ) -> None:
        self.gateway = gateway
        self.routing = routing
        self.metadata_sink = metadata_sink
        self.execution_control = execution_control
        self.result = None

    def complete_json(self, *, system_prompt: str, user_payload: Mapping[str, object]) -> Mapping[str, object]:
        result = self.gateway.invoke(ModelRequest(
            capability="structured", input=json.dumps(user_payload, ensure_ascii=False, separators=(",", ":")),
            parameters={
                "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))}],
                "temperature": 0,
                **self.routing.parameters(),
            },
            privacy_scope="remote_allowed",
            execution_control=self.execution_control,
            metadata_sink=self.metadata_sink,
        ))
        self.result = result
        output = result.output
        if isinstance(output, str):
            try: output = json.loads(output)
            except json.JSONDecodeError as error: raise ValueError("provider classifier output is not JSON") from error
        if not isinstance(output, Mapping): raise ValueError("provider classifier output is invalid")
        return dict(output)


class WorkbenchInputClassificationEnhanceCapability:
    """Invoke the existing forbidden-output and merge logic after approval."""

    def __init__(self, *, input_loader: InputLoader, authority_loader: AuthorityLoader, gateway: ModelGatewayPort | None, receipt_store: TurnPayloadStorePort) -> None:
        self._input_loader, self._authority_loader, self._gateway, self._receipt_store = input_loader, authority_loader, gateway, receipt_store

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        turn_id = _required(request.get("turn_id"), "turn_id")
        context = _arguments(request).get("context")
        if not isinstance(context, Mapping): raise ValueError("Workbench classification enhancement requires context")
        source_id, local, input_baseline, baseline = _context(context)
        authority = _authority(self._authority_loader())
        if _baseline(authority) != baseline: raise ValueError("Workbench classification authority baseline is stale")
        # The protected source is read again only after approval.  It never
        # crosses a Turn event/payload boundary; the persisted baseline is
        # intentionally limited to non-hash shape metadata.
        content, media_type, file_name, urls, grant_revision = _source(self._input_loader(source_id))
        if _input_projection(content, media_type, file_name, urls, grant_revision) != input_baseline:
            raise ValueError("Workbench classification input baseline is stale")
        model_evidence_refs: tuple[str, ...] = ()
        if self._gateway is not None and _arguments(request).get("allow_remote") is True:
            routing = load_turn_model_routing_binding(
                self._receipt_store, request, required_capability="structured",
            )
            nested_model = begin_nested_model_call(request)
            nested_error_code: str | None = None
            provider = _GatewayJsonProvider(
                self._gateway, routing, nested_model, execution_control_from(request),
            )
            try:
                enhanced = EnhanceWorkbenchInputClassification().execute(
                    local_result=local, provider=provider, provider_name=str(authority["provider_id"]),
                    content=content, media_type=media_type, file_name=file_name,
                    urls=urls, classifier_prompt=authority["prompt"],
                )
            except Exception:
                nested_error_code = "ai.nested_model_failed"
                raise
            finally:
                model_evidence_refs = nested_model.finalize(
                    error_code=nested_error_code,
                )
            assert provider.result is not None
            provider_id, model_name, called = provider.result.provider or str(authority["provider_id"]), provider.result.model, True
        else:
            enhanced, provider_id, model_name, called = local, "local-fallback", "", False
        classification = _safe_classification(serialize_workbench_input_classification(enhanced))
        artifact = validate_turn_presentation_artifact({"schema_version": "1.0.0", "kind": WORKBENCH_INPUT_CLASSIFICATION_OUTCOME, "content": {"status": "completed", "source_id": source_id, "classification": classification, "provider_id": provider_id, "model_name": model_name, "provider_call_performed": called}})
        receipt_ref = self._receipt_store.put(turn_id, "workbench-input-classification-receipt", artifact)
        return {"summary": "Workbench input classification enhanced", "receipt_ref": receipt_ref, "payload_ref": None, "evidence_refs": [f"crp://default/workbench/inputs/{source_id}", *model_evidence_refs], "result": artifact}


class WorkbenchInputClassificationTurnPlanner:
    def plan(self, request: Mapping[str, object], events: Sequence[Mapping[str, object]], capabilities: Sequence[CapabilityDefinition], payloads: TurnPayloadStorePort, execution_control: ModelExecutionControlPort | None = None) -> Mapping[str, object]:
        done = _completed(events, WORKBENCH_INPUT_CLASSIFICATION_ENHANCE_CAPABILITY)
        if done is not None:
            data = done.get("data")
            return {"type": "complete", "summary": "Workbench input classification enhanced", "payload_ref": data.get("payload_ref") if isinstance(data, Mapping) else None, "evidence_refs": list(data.get("evidence_refs") or ()) if isinstance(data, Mapping) else []}
        read = _completed(events, WORKBENCH_INPUT_CLASSIFICATION_CONTEXT_CAPABILITY)
        if read is None:
            return {"type": "tool", "capability_id": WORKBENCH_INPUT_CLASSIFICATION_CONTEXT_CAPABILITY, "arguments": {"source_id": _source_id(request)}}
        data = read.get("data")
        ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        if not isinstance(ref, str): raise ValueError("Workbench classification context payload is unavailable")
        context = payloads.get(ref)
        if not isinstance(context, Mapping) or context.get("kind") != "workbench.input.classification.context": raise ValueError("Workbench classification context payload is invalid")
        return {"type": "tool", "capability_id": WORKBENCH_INPUT_CLASSIFICATION_ENHANCE_CAPABILITY, "arguments": {"context": dict(context), "allow_remote": isinstance(request.get("privacy"), Mapping) and request["privacy"].get("allow_remote") is True}}


def _arguments(request: Mapping[str, object]) -> Mapping[str, object]:
    value = request.get("arguments")
    if not isinstance(value, Mapping): raise ValueError("capability arguments are required")
    return value

def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip(): raise ValueError(f"{label} is required")
    return value.strip()

def _source(source: Mapping[str, object]) -> tuple[str, str, str, tuple[str, ...], str]:
    content, media_type, file_name, urls = source.get("content", ""), source.get("media_type", ""), source.get("file_name", ""), source.get("urls", ())
    if not all(isinstance(value, str) for value in (content, media_type, file_name)) or not isinstance(urls, (list, tuple)) or any(not isinstance(item, str) for item in urls): raise ValueError("Workbench input source is invalid")
    revision = source.get("grant_revision", "unversioned")
    if not isinstance(revision, str) or not revision:
        raise ValueError("Workbench input source revision is invalid")
    return content, media_type, file_name, tuple(urls), revision

def _authority(value: Mapping[str, object]) -> dict[str, object]:
    prompt = value.get("prompt")
    if not isinstance(prompt, Mapping) or not isinstance(prompt.get("content"), str) or not prompt["content"].strip(): raise ValueError("Workbench classifier prompt authority is unavailable")
    result = {"prompt": dict(prompt), "provider_id": _required(value.get("provider_id"), "provider_id"), "prompt_revision": value.get("prompt_revision"), "route_revision": value.get("route_revision"), "provider_revision": value.get("provider_revision")}
    if any(not isinstance(result[key], int) or isinstance(result[key], bool) or result[key] < 1 for key in ("prompt_revision", "route_revision", "provider_revision")): raise ValueError("Workbench classifier authority baseline is invalid")
    return result

def _baseline(authority: Mapping[str, object]) -> dict[str, object]:
    return {key: authority[key] for key in ("provider_id", "prompt_revision", "route_revision", "provider_revision")}

def _input_projection(content: str, media_type: str, file_name: str, urls: Sequence[str], grant_revision: str) -> dict[str, object]:
    # URL values deliberately become a count; full source URLs never enter a Turn payload.
    return {"content_length": len(content), "media_type": media_type[:160], "file_name": _redact(file_name.replace("\\", "/").rsplit("/", 1)[-1], 160), "url_count": len(urls), "grant_revision": grant_revision}

def _safe_classification(value: Mapping[str, object]) -> dict[str, object]:
    result = {key: _sanitize_value(item) for key, item in value.items()}
    children = value.get("child_inputs", [])
    result["child_inputs"] = [
        {key: _sanitize_value(item) for key, item in child.items() if key not in {"raw_input", "url", "path", "local_path"}}
        for child in children if isinstance(child, Mapping)
    ]
    return result

def _sanitize_value(value: object) -> object:
    if isinstance(value, str): return _redact(value, 800)
    if isinstance(value, Mapping): return {str(key): _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [_sanitize_value(item) for item in value]
    return value

def _redact(value: str, limit: int) -> str:
    value = re.sub(r"(?i)(sk-[A-Za-z0-9_-]{8,})", "[REDACTED_API_KEY]", value)
    value = re.sub(r"(?i)(api[_-]?key|token|password|authorization|cookie)\s*[:=]\s*\S+", "[SENSITIVE_VALUE_REMOVED]", value)
    value = re.sub(r"[A-Za-z]:\\[^\s]+|/(?:Users|home|mnt|Volumes)/[^\s]+", "[REDACTED_LOCAL_PATH]", value)
    value = re.sub(r"https?://[^\s]+", "[REDACTED_URL]", value)
    return value.strip()[:limit]

def _rehydrate(value: Mapping[str, object]) -> WorkbenchInputClassificationResult:
    fields = {field: value.get(field) for field in WorkbenchInputClassificationResult.__dataclass_fields__}
    tuple_fields = {"workflow_steps", "structured_output_plan", "memory_layer_update_plan", "suggested_next_actions", "child_inputs"}
    for field in tuple_fields: fields[field] = tuple(fields[field] or ())
    return WorkbenchInputClassificationResult(**fields)  # type: ignore[arg-type]

def _context(value: Mapping[str, object]) -> tuple[str, WorkbenchInputClassificationResult, Mapping[str, object], Mapping[str, object]]:
    if value.get("kind") != "workbench.input.classification.context": raise ValueError("Workbench classification context is invalid")
    source_id = _required(value.get("source_id"), "source_id"); local, projection, baseline = value.get("local_classification"), value.get("input"), value.get("baseline")
    if not isinstance(local, Mapping) or not isinstance(projection, Mapping) or not isinstance(baseline, Mapping): raise ValueError("Workbench classification context is invalid")
    return source_id, _rehydrate(local), projection, dict(baseline)

def _source_id(request: Mapping[str, object]) -> str:
    input_payload = request.get("input")
    if not isinstance(input_payload, Mapping) or not isinstance(input_payload.get("refs"), list): raise ValueError("Workbench classification Turn input is invalid")
    refs = input_payload["refs"]
    ids = [_required(item.get("object_id"), "source_id") for item in refs if isinstance(item, Mapping) and item.get("kind") == "workbench_input"]
    if len(refs) != 1 or len(ids) != 1: raise ValueError("Workbench classification Turn requires exactly one input ref")
    return ids[0]

def _completed(events: Sequence[Mapping[str, object]], capability_id: str) -> Mapping[str, object] | None:
    return next((event for event in reversed(events) if event.get("type") == "tool.completed" and isinstance(event.get("data"), Mapping) and event["data"].get("capability_id") == capability_id), None)
