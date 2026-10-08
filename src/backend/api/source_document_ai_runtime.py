from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.ai_kernel import CapabilityDefinition, TurnPayloadStorePort, validate_turn_presentation_artifact
from core.model_gateway import ModelExecutionControlPort, ModelGatewayPort, ModelRequest
from backend.api.turn_model_routing_binding import load_turn_model_routing_binding
from core.product_core.developer_studio_config import GetDeveloperStudioConfig
from core.product_core.prompt_activation import resolve_active_prompt
from core.product_core.style_profile import StyleProfileService
from core.product_core.source_template_document import (
    ApprovedSourceDocumentDraftWriter,
    SourceTemplateDocumentError,
    normalize_source_document_ai_output,
    prepare_source_document_ai_evidence,
    serialize_source_template_document_result,
    source_document_ai_system_prompt,
    source_document_ai_user_payload,
)


SOURCE_DOCUMENT_DRAFT_OUTCOME = "source.document.draft"
SOURCE_EVIDENCE_CAPABILITY = "source.evidence.read"
DOCUMENT_DRAFT_PROPOSE_CAPABILITY = "document.draft.propose"
_PROMPT_IDS = ("pt-title", "pt-detail-summary", "pt-longterm-organize", "pt-output-validate")
_TEMPLATE_TYPES = frozenset(("answer_manual", "review", "project_summary", "media_summary"))


class SourceDocumentEvidenceCapability:
    """Read completed Source evidence and current authoring authorities without writes."""

    def __init__(self, *, runtime_root: object, store: object, namespace_id: str) -> None:
        self._runtime_root = runtime_root
        self._store = store
        self._namespace_id = namespace_id

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        arguments = _arguments(request)
        source_id = _required(arguments.get("source_id"), "source_id")
        template_type = _required(arguments.get("template_type"), "template_type")
        if template_type not in _TEMPLATE_TYPES:
            raise ValueError("unsupported template_type")
        source = self._store.read("sources", source_id)
        if source is None:
            raise ValueError("source not found")
        project_id = str(source.get("project_id") or "default")
        scoped_project_id = _project_scope(request)
        if scoped_project_id is not None and scoped_project_id != project_id:
            raise ValueError("Source document AI source is outside the Turn project scope")
        factory = AggregateRepositoryFactory(
            runtime_root=self._runtime_root,
            namespace_id=self._namespace_id,
            json_store=self._store,
        )
        skills = factory.project_skill_repository()
        skill = skills.load(project_id) if project_id != "default" else None
        outline = skill.get("outline") if isinstance(skill, Mapping) else None
        if not isinstance(outline, list) or not outline:
            outline = None
        style_prefix = "\n".join(
            item
            for item in (
                StyleProfileService(object_store=self._store).build(project_id=None).render_prompt_prefix(),
                _project_skill_style_prefix(skill),
            )
            if item
        )
        documents = factory.document_repository()
        document_baseline = _document_baseline(
            store=self._store,
            documents=documents,
            source=source,
            template_type=template_type,
        )
        prompt_context = _active_prompt_context(self._store)
        evidence = prepare_source_document_ai_evidence(
            source=source,
            source_revision=self._store.revision("sources", source_id),
            template_type=template_type,
            prompt_context=prompt_context,
            style_prefix=style_prefix,
            outline_override=outline,
            document_baseline=document_baseline,
        )
        if isinstance(skill, Mapping):
            evidence["project_skill_ref"] = {
                "project_id": project_id,
                "revision": skill.get("revision"),
            }
        refs = [f"crp://{self._namespace_id}/sources/{source_id}"]
        if document_baseline is not None:
            refs.append(f"crp://{self._namespace_id}/documents/{document_baseline['document_id']}.json")
        return {
            "summary": "Source document evidence is ready",
            "receipt_ref": None,
            "payload_ref": None,
            "evidence_refs": refs,
            "result": evidence,
        }


class SourceDocumentDraftProposalCapability:
    """Create one editable Document only after kernel approval and return a domain receipt."""

    def __init__(self, *, runtime_root: object, store: object, namespace_id: str) -> None:
        self._runtime_root = runtime_root
        self._store = store
        self._namespace_id = namespace_id

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        arguments = _arguments(request)
        evidence = arguments.get("evidence")
        generated = arguments.get("generated")
        if not isinstance(evidence, Mapping) or not isinstance(generated, Mapping):
            raise ValueError("Source document proposal requires evidence and generated output")
        project_id = _project_scope(request)
        if project_id is not None and evidence.get("project_id") != project_id:
            raise ValueError("Source document proposal is outside the Turn project scope")
        provider_id = str(arguments.get("provider_id") or "model-gateway")
        model_name = str(arguments.get("model_name") or "")
        factory = AggregateRepositoryFactory(
            runtime_root=self._runtime_root,
            namespace_id=self._namespace_id,
            json_store=self._store,
        )
        written = ApprovedSourceDocumentDraftWriter(
            object_store=self._store,
            documents=factory.document_repository(),
            namespace_id=self._namespace_id,
        ).execute(
            evidence=evidence,
            generated=generated,
            provider_id=provider_id,
            model_name=model_name,
        )
        presentation = serialize_source_template_document_result(written.document)
        presentation.update(
            {
                "generation_id": written.generation_id,
                "replayed": written.replayed,
                "provider_call_performed": True,
                "request_payload_persisted": False,
                "key_material_returned": False,
                "next_action": "edit_document",
            }
        )
        artifact = validate_turn_presentation_artifact(
            {
                "schema_version": "1.0.0",
                "kind": SOURCE_DOCUMENT_DRAFT_OUTCOME,
                "content": presentation,
            }
        )
        return {
            "summary": "Editable Source document draft created",
            "receipt_ref": written.receipt_ref,
            "payload_ref": None,
            "evidence_refs": [
                f"crp://{self._namespace_id}/sources/{evidence['source_id']}",
                f"crp://{self._namespace_id}/documents/{written.document.document_id}.json",
            ],
            "result": artifact,
        }


class SourceDocumentDraftPlanner:
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
        proposed = next(
            (event for event in reversed(events) if _tool_completed(event, DOCUMENT_DRAFT_PROPOSE_CAPABILITY)),
            None,
        )
        if proposed is not None:
            data = proposed.get("data")
            assert isinstance(data, Mapping)
            return {
                "type": "complete",
                "summary": "Editable Source document draft created",
                "payload_ref": data.get("payload_ref"),
                "evidence_refs": list(data.get("evidence_refs") or ()),
            }
        evidence_event = next(
            (event for event in reversed(events) if _tool_completed(event, SOURCE_EVIDENCE_CAPABILITY)),
            None,
        )
        if evidence_event is None:
            source_id, template_type = _source_document_request(request)
            return {
                "type": "tool",
                "capability_id": SOURCE_EVIDENCE_CAPABILITY,
                "arguments": {"source_id": source_id, "template_type": template_type},
            }
        if self._gateway is None:
            raise ValueError("Source document AI model gateway is unavailable")
        privacy = request.get("privacy")
        if not isinstance(privacy, Mapping) or privacy.get("allow_remote") is not True:
            raise ValueError("Source document AI draft requires remote consent")
        data = evidence_event.get("data")
        payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        if not isinstance(payload_ref, str):
            raise ValueError("Source document evidence payload is unavailable")
        evidence = payloads.get(payload_ref)
        if not isinstance(evidence, Mapping) or evidence.get("kind") != "source.document.evidence":
            raise ValueError("Source document evidence payload is invalid")
        user_payload = source_document_ai_user_payload(evidence)
        user_content = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
        routing = load_turn_model_routing_binding(
            payloads, request, required_capability="structured",
        )
        result = self._gateway.invoke(
            ModelRequest(
                capability="structured",
                input=user_content,
                parameters={
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": source_document_ai_system_prompt(evidence)},
                        {"role": "user", "content": user_content},
                    ],
                    **routing.parameters(),
                },
                privacy_scope="remote_allowed",
                execution_control=execution_control,
                metadata_sink=execution_control,  # type: ignore[arg-type]
            )
        )
        if not isinstance(result.output, Mapping):
            raise ValueError("Source document AI model output must be an object")
        try:
            generated = normalize_source_document_ai_output(
                evidence=evidence,
                value=result.output,
                provider_name=result.provider or "model-gateway",
            )
        except SourceTemplateDocumentError as error:
            raise ValueError(str(error)) from error
        return {
            "type": "tool",
            "capability_id": DOCUMENT_DRAFT_PROPOSE_CAPABILITY,
            "arguments": {
                "evidence": dict(evidence),
                "generated": generated,
                "provider_id": result.provider,
                "model_name": result.model,
            },
        }


def _source_document_request(request: Mapping[str, object]) -> tuple[str, str]:
    input_payload = request.get("input")
    if not isinstance(input_payload, Mapping):
        raise ValueError("Source document Turn input is required")
    refs = input_payload.get("refs")
    source_ref = next(
        (item for item in refs if isinstance(item, Mapping) and item.get("kind") == "source"),
        None,
    ) if isinstance(refs, list) else None
    if source_ref is None:
        raise ValueError("Source document Turn requires one Source ref")
    source_id = _required(source_ref.get("object_id"), "source_id")
    text = input_payload.get("text")
    template_type = str(text or "answer_manual").strip()
    if template_type.startswith("{"):
        try:
            parsed = json.loads(template_type)
        except json.JSONDecodeError as error:
            raise ValueError("Source document Turn input JSON is invalid") from error
        template_type = str(parsed.get("template_type") or "answer_manual") if isinstance(parsed, Mapping) else ""
    if template_type not in _TEMPLATE_TYPES:
        raise ValueError("Source document Turn template_type is unsupported")
    return source_id, template_type


def _document_baseline(*, store: object, documents: object, source: Mapping[str, object], template_type: str) -> dict[str, object] | None:
    metadata = source.get("metadata")
    outputs = metadata.get("template_outputs") if isinstance(metadata, Mapping) else None
    if not isinstance(outputs, list):
        return None
    for link in reversed(outputs):
        if not isinstance(link, Mapping):
            continue
        output_id = link.get("output_id")
        if not isinstance(output_id, str):
            continue
        output = store.read("source_template_outputs", output_id)
        if not isinstance(output, Mapping) or output.get("template_type") != template_type:
            continue
        document_id = output.get("document_id")
        if not isinstance(document_id, str):
            raise ValueError("Source template output document link is invalid")
        document = documents.read(document_id)
        if not isinstance(document, Mapping):
            raise ValueError("Source template output Document authority is unavailable")
        revision = document.get("revision")
        content_hash = document.get("content_hash")
        if not isinstance(revision, int) or isinstance(revision, bool) or not isinstance(content_hash, str):
            raise ValueError("Source template output Document baseline is invalid")
        return {
            "document_id": document_id,
            "document_revision": revision,
            "content_hash": content_hash,
            "status": str(document.get("status") or "draft"),
        }
    return None


def _active_prompt_context(store: object) -> tuple[Mapping[str, object], ...]:
    try:
        config = GetDeveloperStudioConfig(store).execute()
    except Exception:
        return ()
    prompts: list[Mapping[str, object]] = []
    for prompt_id in _PROMPT_IDS:
        prompt = resolve_active_prompt(config, prompt_id)
        if not isinstance(prompt, Mapping):
            continue
        content = prompt.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        ref: dict[str, object] = {
            "id": prompt_id,
            "revision": prompt.get("version") if isinstance(prompt.get("version"), int) else config.revision,
            "source": "developer_studio_active",
            "content": content.strip(),
        }
        for source_key, target_key in (("stageId", "stage_id"), ("modelProfileId", "model_profile_id")):
            value = prompt.get(source_key)
            if isinstance(value, str) and value.strip():
                ref[target_key] = value.strip()
        prompts.append(ref)
    return tuple(prompts)


def _project_skill_style_prefix(skill: object) -> str:
    if not isinstance(skill, Mapping):
        return ""
    style = skill.get("style_preferences")
    if not isinstance(style, Mapping):
        return ""
    lines: list[str] = []
    voice = style.get("voice")
    if isinstance(voice, str) and voice.strip():
        lines.append(f"语调：{voice.strip()}")
    formats = style.get("format_defaults")
    if isinstance(formats, Sequence) and not isinstance(formats, (str, bytes)):
        clean = [item.strip() for item in formats if isinstance(item, str) and item.strip()]
        if clean:
            lines.append("格式偏好：" + "；".join(clean))
    return "统一输出范式（Project Skill，必须遵循）：\n" + "\n".join(lines) if lines else ""


def _tool_completed(event: Mapping[str, object], capability_id: str) -> bool:
    data = event.get("data")
    return event.get("type") == "tool.completed" and isinstance(data, Mapping) and data.get("capability_id") == capability_id


def _arguments(request: Mapping[str, object]) -> Mapping[str, object]:
    value = request.get("arguments")
    if not isinstance(value, Mapping):
        raise ValueError("capability arguments are required")
    return value


def _project_scope(request: Mapping[str, object]) -> str | None:
    scope = request.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("Source document capability requires scope")
    value = scope.get("project_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()
