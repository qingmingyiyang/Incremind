from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json

from core.aggregate_repository_factory import AggregateRepositoryFactory, AggregateRepositoryFactoryError
from core.ai_kernel import CapabilityDefinition, TurnPayloadStorePort, validate_turn_presentation_artifact
from core.memory_core import ObjectStoreMemoryCandidateRepository, ObjectStoreMemoryStore, SQLiteMemoryReader
from core.model_gateway import ModelExecutionControlPort, ModelGatewayPort, ModelRequest
from backend.api.turn_model_routing_binding import load_turn_model_routing_binding
from core.product_core.project_skill_ai_authoring import (
    build_project_skill_evidence_bundle,
    normalize_project_skill_ai_output,
    project_skill_ai_system_prompt,
    project_skill_ai_user_payload,
)


PROJECT_SKILL_EVIDENCE_CAPABILITY = "project_skill.evidence.read"
PROJECT_SKILL_PROPOSE_CAPABILITY = "project_skill.draft.propose"
PROJECT_SKILL_DRAFT_OUTCOME = "project_skill.draft.generate"


class ProjectSkillEvidenceCapability:
    def __init__(self, *, runtime_root: object, store: object, namespace_id: str) -> None:
        self._runtime_root = runtime_root
        self._store = store
        self._namespace_id = namespace_id

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        arguments = _arguments(request)
        project_id = _project_scope(request)
        goal = _required(arguments.get("goal"), "goal")
        operation = arguments.get("operation") if isinstance(arguments.get("operation"), str) else None
        factory = AggregateRepositoryFactory(runtime_root=self._runtime_root, namespace_id=self._namespace_id, json_store=self._store)
        skills = factory.project_skill_repository()
        current = skills.load(project_id)
        unavailable = []
        try:
            documents = factory.document_repository().list()
        except AggregateRepositoryFactoryError:
            documents = ()
            unavailable.append("documents_current_authority_unavailable")
        try:
            memory_resolution = factory.memory_publication_authority_resolution()
            memory = SQLiteMemoryReader(memory_resolution.records) if memory_resolution.records is not None else ObjectStoreMemoryStore(self._store)
            memories = memory.list_by_project(project_id)
        except AggregateRepositoryFactoryError:
            memories = ()
            unavailable.append("published_memories_current_authority_unavailable")
        evidence = build_project_skill_evidence_bundle(
            project_id=project_id,
            sources=self._store.list("sources"),
            documents=documents,
            memories=memories,
            current_skill=current,
            unavailable_evidence=unavailable,
        )
        if evidence.insufficient_evidence:
            raise ValueError("Project Skill AI draft needs verified project evidence")
        model_input = project_skill_ai_user_payload(
            project_id=project_id,
            goal=goal,
            operation=operation,
            evidence=evidence,
            current_skill=current,
        )
        return {
            "summary": f"resolved {len(evidence.items)} Project Skill evidence items",
            "receipt_ref": None,
            "payload_ref": None,
            "evidence_refs": list(_crp_evidence_refs(self._namespace_id, evidence.source_refs)),
            "result": {
                "schema_version": "1.0.0",
                "kind": "project_skill.evidence",
                "project_id": project_id,
                "goal": goal,
                "operation": operation,
                "expected_project_skill_revision": 0 if current is None else current.get("revision"),
                "model_input": model_input,
                "source_refs": [dict(item) for item in evidence.source_refs],
            },
        }


class ProjectSkillDraftProposalCapability:
    """Persist one review candidate and return a stable domain receipt; never activate Skill."""

    def __init__(self, *, runtime_root: object, store: object, namespace_id: str) -> None:
        self._runtime_root = runtime_root
        self._store = store
        self._namespace_id = namespace_id

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        arguments = _arguments(request)
        project_id = _project_scope(request)
        goal = _required(arguments.get("goal"), "goal")
        generated = arguments.get("generated")
        source_refs = arguments.get("source_refs")
        if not isinstance(generated, Mapping) or not isinstance(source_refs, list):
            raise ValueError("Project Skill proposal requires generated draft and source refs")
        clean_refs = tuple(dict(item) for item in source_refs if isinstance(item, Mapping))
        structured = normalize_project_skill_ai_output(project_id=project_id, value=generated, allowed_source_refs=clean_refs)
        expected_revision = arguments.get("expected_project_skill_revision")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("expected Project Skill revision is invalid")
        factory = AggregateRepositoryFactory(
            runtime_root=self._runtime_root,
            namespace_id=self._namespace_id,
            json_store=self._store,
        )
        current = factory.project_skill_repository().load(project_id)
        current_revision = 0 if current is None else current.get("revision")
        if current_revision != expected_revision:
            raise ValueError("Project Skill AI draft baseline is stale")
        provider_id = str(arguments.get("provider_id") or "")
        model_name = str(arguments.get("model_name") or "")
        material = json.dumps({
            "prompt_version": "project-skill-ai-authoring-v2",
            "project_id": project_id,
            "goal": goal,
            "expected_project_skill_revision": expected_revision,
            "evidence_refs": clean_refs,
            "draft": structured,
        }, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        draft_id = f"project-skill-ai-draft-{digest[:24]}"
        candidate_id = f"memory-candidate-{digest[24:40]}"
        existing_audit = self._store.read("project_skill_ai_drafts", draft_id)
        existing_candidate = self._store.read("memory_candidates", candidate_id)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for existing in (existing_audit, existing_candidate):
            recorded = existing.get("created_at") if isinstance(existing, Mapping) else None
            if isinstance(recorded, str) and recorded:
                timestamp = recorded
                break
        source_ref = {"source_id": draft_id, "locator": "ai-draft:generated-output"}
        project_refs = [dict(item) for item in clean_refs]
        all_refs = [*project_refs, source_ref]
        candidate = {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "project_id": project_id,
            "target_layer": "project_skill",
            "candidate_type": "other",
            "status": "pending_review",
            "expected_project_skill_revision": expected_revision,
            "proposed_content": str(structured["purpose"]),
            "project_skill_draft": structured,
            "source_refs": all_refs,
            "evidence_refs": all_refs,
            "provenance": {
                "source_content_read_id": draft_id,
                "input_refs": [
                    {"kind": "source", "object_id": draft_id, "uri": f"crp://{self._namespace_id}/project-skill-ai-drafts/{draft_id}.json"},
                    {"kind": "source_content_read", "object_id": draft_id, "uri": f"crp://{self._namespace_id}/project-skill-ai-drafts/{draft_id}.json#generated"},
                ],
                "provider_id": provider_id,
                "model_name": model_name,
                "prompt_version": "project-skill-ai-authoring-v2",
                "project_evidence_refs": project_refs,
            },
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": "AI生成内容必须先由用户审核，再进入Project Skill staging。",
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        audit = {
            "id": draft_id,
            "project_id": project_id,
            "goal": goal,
            "candidate_id": candidate_id,
            "expected_project_skill_revision": expected_revision,
            "provider_id": provider_id,
            "model_name": model_name,
            "status": "pending_review",
            "generated": structured,
            "prompt_version": "project-skill-ai-authoring-v2",
            "evidence_refs": project_refs,
            "created_at": timestamp,
        }
        if existing_audit is not None and dict(existing_audit) != audit:
            raise ValueError("Project Skill AI draft audit identity conflict")
        if existing_candidate is not None and dict(existing_candidate) != candidate:
            raise ValueError("Project Skill AI draft candidate identity conflict")
        if existing_audit is None:
            self._store.write("project_skill_ai_drafts", draft_id, audit, expected_revision=0)
        if existing_candidate is None:
            ObjectStoreMemoryCandidateRepository(self._store).save(candidate)
        presentation = {
            "status": "pending_review",
            "draft_id": draft_id,
            "candidate_id": candidate_id,
            "candidate_revision": self._store.revision("memory_candidates", candidate_id),
            "project_id": project_id,
            "expected_project_skill_revision": expected_revision,
            "preview": {
                "name": structured["name"],
                "purpose": structured["purpose"],
                "outline": structured.get("outline", []),
                "output_rules": structured.get("output_rules", []),
            },
            "provider_call_performed": True,
            "replayed": existing_audit is not None or existing_candidate is not None,
            "active_project_skill_changed": False,
            "next_action": "review_candidate",
        }
        artifact = validate_turn_presentation_artifact({"schema_version": "1.0.0", "kind": PROJECT_SKILL_DRAFT_OUTCOME, "content": presentation})
        receipt_ref = f"crp://{self._namespace_id}/project-skill-ai-drafts/{draft_id}.json"
        return {
            "summary": "Project Skill draft candidate is pending review",
            "receipt_ref": receipt_ref,
            "payload_ref": None,
            "evidence_refs": list(_crp_evidence_refs(self._namespace_id, clean_refs)),
            "result": artifact,
        }


class ProjectSkillDraftPlanner:
    def __init__(self, gateway: ModelGatewayPort | None) -> None:
        self._gateway = gateway

    def plan(
        self,
        request: Mapping[str, object],
        events: Sequence[Mapping[str, object]],
        capabilities: Sequence[CapabilityDefinition],
        payloads: TurnPayloadStorePort,
        execution_control: ModelExecutionControlPort | None = None,
    ) -> Mapping[str, object]:
        proposed = next((event for event in reversed(events) if _tool_completed(event, PROJECT_SKILL_PROPOSE_CAPABILITY)), None)
        if proposed is not None:
            data = proposed.get("data")
            assert isinstance(data, Mapping)
            return {
                "type": "complete",
                "summary": "Project Skill draft candidate is pending review",
                "payload_ref": data.get("payload_ref"),
                "evidence_refs": list(data.get("evidence_refs") or ()),
            }
        evidence_event = next((event for event in reversed(events) if _tool_completed(event, PROJECT_SKILL_EVIDENCE_CAPABILITY)), None)
        if evidence_event is None:
            input_payload = request.get("input")
            goal = input_payload.get("text") if isinstance(input_payload, Mapping) else None
            return {
                "type": "tool",
                "capability_id": PROJECT_SKILL_EVIDENCE_CAPABILITY,
                "arguments": {"goal": str(goal or "").strip(), "operation": "unspecified"},
            }
        if self._gateway is None:
            raise ValueError("Project Skill AI model gateway is unavailable")
        privacy = request.get("privacy")
        if not isinstance(privacy, Mapping) or privacy.get("allow_remote") is not True:
            raise ValueError("Project Skill AI draft requires remote consent")
        data = evidence_event.get("data")
        payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        if not isinstance(payload_ref, str):
            raise ValueError("Project Skill evidence payload is unavailable")
        evidence = payloads.get(payload_ref)
        if not isinstance(evidence, Mapping) or evidence.get("kind") != "project_skill.evidence":
            raise ValueError("Project Skill evidence payload is invalid")
        user_content = json.dumps(evidence.get("model_input"), ensure_ascii=False, separators=(",", ":"))
        routing = load_turn_model_routing_binding(
            payloads, request, required_capability="structured",
        )
        result = self._gateway.invoke(ModelRequest(
            capability="structured",
            input=user_content,
            parameters={
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": project_skill_ai_system_prompt()},
                    {"role": "user", "content": user_content},
                ],
                **routing.parameters(),
            },
            privacy_scope="remote_allowed",
            execution_control=execution_control,
            metadata_sink=execution_control,  # type: ignore[arg-type]
        ))
        if not isinstance(result.output, Mapping):
            raise ValueError("Project Skill AI model output must be an object")
        source_refs = evidence.get("source_refs")
        if not isinstance(source_refs, list):
            raise ValueError("Project Skill evidence refs are invalid")
        generated = normalize_project_skill_ai_output(project_id=str(evidence["project_id"]), value=result.output, allowed_source_refs=source_refs)
        return {
            "type": "tool",
            "capability_id": PROJECT_SKILL_PROPOSE_CAPABILITY,
            "arguments": {
                "goal": evidence["goal"],
                "operation": evidence.get("operation"),
                "generated": generated,
                "source_refs": source_refs,
                "expected_project_skill_revision": evidence["expected_project_skill_revision"],
                "provider_id": result.provider,
                "model_name": result.model,
            },
        }


def _tool_completed(event: Mapping[str, object], capability_id: str) -> bool:
    data = event.get("data")
    return event.get("type") == "tool.completed" and isinstance(data, Mapping) and data.get("capability_id") == capability_id


def _arguments(request: Mapping[str, object]) -> Mapping[str, object]:
    value = request.get("arguments")
    if not isinstance(value, Mapping):
        raise ValueError("capability arguments are required")
    return value


def _project_scope(request: Mapping[str, object]) -> str:
    scope = request.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("Project Skill capability requires project scope")
    return _required(scope.get("project_id"), "project_id")


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _crp_evidence_refs(namespace_id: str, refs: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    values = []
    for ref in refs:
        source_id = ref.get("source_id")
        if isinstance(source_id, str) and source_id:
            values.append(f"crp://{namespace_id}/sources/{source_id}")
    return tuple(dict.fromkeys(values))
