"""AI Turn proposal boundary for review-only four-layer memory candidates."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import json
import re
import secrets
from threading import Lock
import time

from core.ai_kernel import CapabilityDefinition, TurnPayloadStorePort, validate_turn_presentation_artifact
from core.model_gateway import (
    ModelCallMetadataSinkPort,
    ModelExecutionControlPort,
    ModelGatewayPort,
    ModelRequest,
    ModelResult,
)

from backend.api.ai_execution_control import (
    begin_nested_model_call,
    execution_checkpoint,
    execution_control_from,
)
from backend.api.turn_model_routing_binding import TurnModelRoutingBinding, load_turn_model_routing_binding


FOUR_LAYER_MEMORY_CANDIDATE_OUTCOME = "memory.candidate.propose"
FOUR_LAYER_MEMORY_EVIDENCE_CAPABILITY = "memory.candidate.evidence.read"
FOUR_LAYER_MEMORY_PROPOSE_CAPABILITY = "memory.candidate.propose.write"
EvidenceLoader = Callable[[str], Mapping[str, object]]
AuthorityLoader = Callable[[], Mapping[str, object]]
ProposalGenerator = Callable[[Mapping[str, object], Mapping[str, object], ModelGatewayPort | None, ModelExecutionControlPort | None], Mapping[str, object]]
Importer = Callable[[Mapping[str, object], Mapping[str, object], str], Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class MemoryCandidateEvidenceGrant:
    grant_id: str
    revision: str
    source_id: str
    evidence_kind: str
    evidence_id: str
    allowed_layers: tuple[str, ...]
    project_id: str
    expires_at: float

    def public(self) -> dict[str, object]:
        return {"grant_id": self.grant_id, "revision": self.revision}


class FourLayerMemoryCandidateEvidenceGrantStore:
    """Short-lived evidence authority with no preview/body projection."""

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now, self._grants, self._lock = now, {}, Lock()

    def issue(self, *, source_id: str, evidence_kind: str, evidence_id: str, allowed_layers: Sequence[str], project_id: str, grant_id: str | None = None) -> dict[str, object]:
        values = (source_id, evidence_kind, evidence_id, project_id)
        if any(not isinstance(value, str) or not value.strip() for value in values) or not isinstance(allowed_layers, Sequence) or isinstance(allowed_layers, (str, bytes)):
            raise ValueError("memory candidate evidence grant is invalid")
        layers = tuple(item.strip() for item in allowed_layers if isinstance(item, str) and item.strip())
        if not layers or len(layers) != len(allowed_layers): raise ValueError("memory candidate evidence grant layers are invalid")
        identifier = grant_id or f"memory-evidence-grant-{secrets.token_hex(16)}"
        if not isinstance(identifier, str) or not identifier.strip(): raise ValueError("memory candidate evidence grant id is invalid")
        grant = MemoryCandidateEvidenceGrant(identifier.strip(), secrets.token_urlsafe(18), *(value.strip() for value in values[:3]), layers, project_id.strip(), self._now()+120)
        with self._lock:
            existing = self._grants.get(grant.grant_id)
            if existing is not None and existing != grant: raise ValueError("memory candidate evidence grant identity conflict")
            self._grants[grant.grant_id] = grant
        return grant.public()

    def inspect(self, grant_id: object) -> MemoryCandidateEvidenceGrant:
        with self._lock: grant = self._grants.get(grant_id) if isinstance(grant_id, str) else None
        if grant is None or grant.expires_at <= self._now(): raise ValueError("memory candidate evidence grant is unavailable")
        return grant

    def consume_expected(self, grant_id: object, expected: object) -> MemoryCandidateEvidenceGrant:
        if not isinstance(expected, Mapping): raise ValueError("memory candidate evidence grant baseline is invalid")
        with self._lock: grant = self._grants.pop(grant_id, None) if isinstance(grant_id, str) else None
        if grant is None or grant.expires_at <= self._now(): raise ValueError("memory candidate evidence grant is unavailable")
        if dict(expected) != grant.public(): raise ValueError("memory candidate evidence grant baseline is stale")
        return grant

    def revoke(self, grant_id: object) -> bool:
        with self._lock: return self._grants.pop(grant_id, None) is not None if isinstance(grant_id, str) else False


GrantEvidenceLoader = Callable[[MemoryCandidateEvidenceGrant], Mapping[str, object]]


class ScopedFourLayerMemoryEvidenceCapability:
    def __init__(self, *, application: object | None, evidence_loader: GrantEvidenceLoader, authority_loader: AuthorityLoader) -> None:
        self._application, self._evidence_loader, self._authority_loader = application, evidence_loader, authority_loader

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        arguments = _arguments(request)
        grant = self._store().inspect(_required(arguments.get("grant_id", arguments.get("evidence_id")), "grant_id"))
        result = FourLayerMemoryEvidenceCapability(
            evidence_loader=lambda evidence_id: self._evidence_for(grant, evidence_id), authority_loader=self._authority_loader,
        ).invoke({"arguments": {"evidence_id": grant.evidence_id}})
        context = dict(result["result"])
        baseline = dict(context["baseline"]); baseline["grant_revision"] = grant.revision
        context["baseline"] = baseline; context["grant"] = grant.public()
        return {**result, "result": context}

    def _store(self) -> FourLayerMemoryCandidateEvidenceGrantStore:
        store = getattr(getattr(self._application, "state", None), "four_layer_memory_candidate_evidence_grant_store", None)
        if not isinstance(store, FourLayerMemoryCandidateEvidenceGrantStore): raise ValueError("memory candidate evidence grant is unavailable")
        return store

    def _evidence_for(self, grant: MemoryCandidateEvidenceGrant, evidence_id: str) -> Mapping[str, object]:
        if evidence_id != grant.evidence_id: raise ValueError("memory candidate evidence grant is invalid")
        evidence = self._evidence_loader(grant)
        if evidence.get("source_id") != grant.source_id or evidence.get("kind") != grant.evidence_kind: raise ValueError("memory candidate evidence grant is stale")
        return evidence


class ScopedFourLayerMemoryCandidateProposalCapability(ScopedFourLayerMemoryEvidenceCapability):
    def __init__(self, *, application: object | None, evidence_loader: GrantEvidenceLoader, authority_loader: AuthorityLoader, importer: Importer, receipt_store: TurnPayloadStorePort, gateway: ModelGatewayPort | None = None, proposal_generator: ProposalGenerator | None = None) -> None:
        super().__init__(application=application, evidence_loader=evidence_loader, authority_loader=authority_loader)
        self._importer, self._receipt_store, self._gateway, self._proposal_generator = importer, receipt_store, gateway, proposal_generator

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        context = _arguments(request).get("context")
        if not isinstance(context, Mapping) or not isinstance(context.get("grant"), Mapping) or not isinstance(context.get("baseline"), Mapping): raise ValueError("memory candidate evidence grant context is invalid")
        grant_id = _required(context["grant"].get("grant_id"), "grant_id")
        execution_checkpoint(request)
        grant = self._store().consume_expected(grant_id, context["grant"])
        scope = request.get("scope")
        if not isinstance(scope, Mapping) or scope.get("project_id") != grant.project_id: raise ValueError("memory candidate evidence grant project is invalid")
        baseline = dict(context["baseline"])
        if baseline.pop("grant_revision", None) != grant.revision: raise ValueError("memory candidate evidence grant baseline is stale")
        core_context = dict(context); core_context["baseline"] = baseline; core_context.pop("grant", None)
        return FourLayerMemoryCandidateProposalCapability(
            evidence_loader=lambda evidence_id: self._evidence_for(grant, evidence_id), authority_loader=self._authority_loader,
            importer=self._importer, receipt_store=self._receipt_store, gateway=self._gateway, proposal_generator=self._proposal_generator,
        ).invoke({**request, "arguments": {**_arguments(request), "context": core_context}})


class FourLayerMemoryEvidenceCapability:
    """Verify completed source evidence without network or domain writes."""
    def __init__(self, *, evidence_loader: EvidenceLoader, authority_loader: AuthorityLoader) -> None:
        self._evidence_loader, self._authority_loader = evidence_loader, authority_loader

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        evidence_id = _required(_arguments(request).get("evidence_id"), "evidence_id")
        evidence = _evidence(self._evidence_loader(evidence_id))
        authority = _authority(self._authority_loader())
        return {"summary": "Four-layer candidate evidence is ready", "receipt_ref": None, "payload_ref": None,
            "evidence_refs": list(evidence["refs"]), "result": {"schema_version": "1.0.0", "kind": "memory.candidate.evidence",
                "evidence_id": evidence_id, "source_id": evidence["source_id"], "evidence_kind": evidence["kind"],
                "summary": _redact(str(evidence["summary"]), 800), "refs": list(evidence["refs"]),
                "baseline": {**_evidence_baseline(evidence), **_authority_baseline(authority)}}}


class _GatewayProposalGenerator:
    def __call__(self, evidence: Mapping[str, object], authority: Mapping[str, object], gateway: ModelGatewayPort | None, execution_control: ModelExecutionControlPort | None) -> Mapping[str, object]:
        if gateway is None: raise ValueError("four-layer provider is unavailable")
        request = evidence.get("provider_request")
        if not isinstance(request, Mapping): raise ValueError("four-layer provider request is unavailable")
        system_prompt, user_payload = request.get("system_prompt"), request.get("user_payload")
        if not isinstance(system_prompt, str) or not isinstance(user_payload, Mapping): raise ValueError("four-layer provider request is invalid")
        result = gateway.invoke(ModelRequest(capability="structured", input=json.dumps(user_payload, ensure_ascii=False, separators=(",", ":")), parameters={"messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))}], "temperature": 0}, privacy_scope="remote_allowed", execution_control=execution_control))
        output = result.output
        if isinstance(output, str):
            try: output = json.loads(output)
            except json.JSONDecodeError as error: raise ValueError("four-layer provider output is not JSON") from error
        if not isinstance(output, Mapping): raise ValueError("four-layer provider output is invalid")
        return dict(output)


@dataclass(frozen=True, slots=True)
class _BoundModelGateway:
    gateway: ModelGatewayPort
    routing: TurnModelRoutingBinding
    metadata_sink: ModelCallMetadataSinkPort

    def invoke(self, request: ModelRequest) -> ModelResult:
        return self.gateway.invoke(replace(
            request,
            parameters={**request.parameters, **self.routing.parameters()},
            metadata_sink=self.metadata_sink,
        ))


class FourLayerMemoryCandidateProposalCapability:
    """Create only pending-review candidates after explicit Turn approval."""
    def __init__(self, *, evidence_loader: EvidenceLoader, authority_loader: AuthorityLoader, importer: Importer, receipt_store: TurnPayloadStorePort, gateway: ModelGatewayPort | None = None, proposal_generator: ProposalGenerator | None = None) -> None:
        self._evidence_loader, self._authority_loader, self._importer = evidence_loader, authority_loader, importer
        self._receipt_store, self._gateway = receipt_store, gateway
        self._proposal_generator = proposal_generator or _GatewayProposalGenerator()

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        turn_id = _required(request.get("turn_id"), "turn_id"); context = _arguments(request).get("context")
        if not isinstance(context, Mapping): raise ValueError("four-layer proposal requires evidence context")
        evidence_id, project_id, baseline = _context(context, request)
        evidence, authority = _evidence(self._evidence_loader(evidence_id)), _authority(self._authority_loader())
        expected = {**_evidence_baseline(evidence), **_authority_baseline(authority)}
        if expected != baseline: raise ValueError("four-layer candidate baseline is stale")
        execution_control = execution_control_from(request)
        model_evidence_refs: tuple[str, ...] = ()
        gateway: ModelGatewayPort | None = None
        if self._gateway is not None and _arguments(request).get("allow_remote") is True:
            routing = load_turn_model_routing_binding(
                self._receipt_store, request, required_capability="structured",
            )
            nested_model = begin_nested_model_call(request)
            gateway = _BoundModelGateway(self._gateway, routing, nested_model)
            nested_error_code: str | None = None
            try:
                output = self._proposal_generator(
                    evidence, authority, gateway, execution_control,
                )
            except Exception:
                nested_error_code = "ai.nested_model_failed"
                raise
            finally:
                model_evidence_refs = nested_model.finalize(
                    error_code=nested_error_code,
                )
        else:
            output = self._proposal_generator(
                evidence, authority, gateway, execution_control,
            )
        if execution_control is not None:
            execution_control.checkpoint()
        # The importer is the only domain-write port.  Its existing boundary
        # rejects forbidden output and guarantees pending_review-only output.
        imported = self._importer(output, evidence, project_id)
        receipt = _receipt(imported, evidence_id)
        artifact = validate_turn_presentation_artifact({"schema_version": "1.0.0", "kind": FOUR_LAYER_MEMORY_CANDIDATE_OUTCOME, "content": receipt})
        receipt_ref = self._receipt_store.put(turn_id, "four-layer-memory-candidate-receipt", artifact)
        return {"summary": "Four-layer memory candidates proposed for review", "receipt_ref": receipt_ref, "payload_ref": None, "evidence_refs": [*evidence["refs"], *model_evidence_refs], "result": artifact}


class FourLayerMemoryCandidateTurnPlanner:
    def plan(self, request: Mapping[str, object], events: Sequence[Mapping[str, object]], capabilities: Sequence[CapabilityDefinition], payloads: TurnPayloadStorePort, execution_control: ModelExecutionControlPort | None = None) -> Mapping[str, object]:
        completed = _completed(events, FOUR_LAYER_MEMORY_PROPOSE_CAPABILITY)
        if completed is not None:
            data = completed.get("data")
            return {"type": "complete", "summary": "Four-layer memory candidates proposed for review", "payload_ref": data.get("payload_ref") if isinstance(data, Mapping) else None, "evidence_refs": list(data.get("evidence_refs") or ()) if isinstance(data, Mapping) else []}
        read = _completed(events, FOUR_LAYER_MEMORY_EVIDENCE_CAPABILITY)
        if read is None: return {"type": "tool", "capability_id": FOUR_LAYER_MEMORY_EVIDENCE_CAPABILITY, "arguments": {"evidence_id": _evidence_id(request)}}
        data = read.get("data"); ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        if not isinstance(ref, str): raise ValueError("four-layer evidence payload is unavailable")
        context = payloads.get(ref)
        if not isinstance(context, Mapping) or context.get("kind") != "memory.candidate.evidence": raise ValueError("four-layer evidence payload is invalid")
        return {"type": "tool", "capability_id": FOUR_LAYER_MEMORY_PROPOSE_CAPABILITY, "arguments": {"context": dict(context), "allow_remote": isinstance(request.get("privacy"), Mapping) and request["privacy"].get("allow_remote") is True}}


def _arguments(request: Mapping[str, object]) -> Mapping[str, object]:
    value = request.get("arguments")
    if not isinstance(value, Mapping): raise ValueError("capability arguments are required")
    return value
def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip(): raise ValueError(f"{label} is required")
    return value.strip()
def _evidence(value: Mapping[str, object]) -> dict[str, object]:
    if value.get("status") != "completed": raise ValueError("four-layer source evidence must be completed")
    result = {"evidence_id": _required(value.get("evidence_id"), "evidence_id"), "source_id": _required(value.get("source_id"), "source_id"), "kind": _required(value.get("kind"), "evidence kind"), "revision": value.get("revision"), "summary": value.get("summary"), "refs": value.get("refs"), "provider_request": value.get("provider_request")}
    if not isinstance(result["revision"], int) or isinstance(result["revision"], bool) or result["revision"] < 1 or not isinstance(result["summary"], str) or not isinstance(result["refs"], (list, tuple)) or not all(isinstance(ref, str) and ref.startswith("crp://") for ref in result["refs"]): raise ValueError("four-layer source evidence is invalid")
    return result
def _authority(value: Mapping[str, object]) -> dict[str, object]:
    result = {key: value.get(key) for key in ("prompt_revision", "route_revision", "provider_revision", "provider_id")}
    if any(not isinstance(result[key], int) or isinstance(result[key], bool) or result[key] < 1 for key in ("prompt_revision", "route_revision", "provider_revision")) or not isinstance(result["provider_id"], str) or not result["provider_id"]: raise ValueError("four-layer authority is invalid")
    return result
def _evidence_baseline(evidence: Mapping[str, object]) -> dict[str, object]: return {"source_id": evidence["source_id"], "evidence_kind": evidence["kind"], "evidence_revision": evidence["revision"]}
def _authority_baseline(authority: Mapping[str, object]) -> dict[str, object]: return dict(authority)
def _context(context: Mapping[str, object], request: Mapping[str, object]) -> tuple[str, str, Mapping[str, object]]:
    if context.get("kind") != "memory.candidate.evidence": raise ValueError("four-layer evidence context is invalid")
    baseline = context.get("baseline"); scope = request.get("scope")
    if not isinstance(baseline, Mapping) or not isinstance(scope, Mapping): raise ValueError("four-layer evidence context is invalid")
    return _required(context.get("evidence_id"), "evidence_id"), _required(scope.get("project_id"), "project_id"), dict(baseline)
def _evidence_id(request: Mapping[str, object]) -> str:
    input_payload = request.get("input")
    if not isinstance(input_payload, Mapping) or not isinstance(input_payload.get("refs"), list): raise ValueError("four-layer Turn input is invalid")
    refs = input_payload["refs"]; found = [_required(item.get("object_id"), "evidence_id") for item in refs if isinstance(item, Mapping) and item.get("kind") == "memory_evidence"]
    if len(refs) != 1 or len(found) != 1: raise ValueError("four-layer Turn requires exactly one evidence ref")
    return found[0]
def _receipt(imported: Mapping[str, object], evidence_id: str) -> dict[str, object]:
    candidate_ids = imported.get("candidate_ids", ())
    if not isinstance(candidate_ids, (list, tuple)) or not all(isinstance(item, str) for item in candidate_ids): raise ValueError("four-layer importer result is invalid")
    if imported.get("memory_publication_state") not in {"candidates_created_not_published", "pending_review"}: raise ValueError("four-layer importer attempted memory publication")
    return {"status": "pending_review", "evidence_id": evidence_id, "candidate_ids": list(candidate_ids), "candidate_count": len(candidate_ids), "memory_publication_state": "candidates_created_not_published", "auto_promote_allowed": False}
def _redact(value: str, limit: int) -> str:
    value = re.sub(r"(?i)(sk-[A-Za-z0-9_-]{8,})|(api[_-]?key|token|password|authorization|cookie)\s*[:=]\s*\S+|[A-Za-z]:\\[^\s]+", "[REDACTED]", value)
    return value[:limit]
def _completed(events: Sequence[Mapping[str, object]], capability_id: str) -> Mapping[str, object] | None:
    return next((event for event in reversed(events) if event.get("type") == "tool.completed" and isinstance(event.get("data"), Mapping) and event["data"].get("capability_id") == capability_id), None)
