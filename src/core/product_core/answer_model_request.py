from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


class RecallResultRepositoryPort(Protocol):
    """Reads persisted recall evidence packages."""

    def get_request(self, request_id: str) -> Mapping[str, object] | None:
        """Return the Recall Request that produced a result."""

    def get_result(self, result_id: str) -> Mapping[str, object] | None:
        """Return one Recall Result evidence package."""


class ModelRequestRepositoryPort(Protocol):
    """Persists local Model Request contracts without invoking providers."""

    def save_request(self, request: Mapping[str, object]) -> Mapping[str, object]:
        """Persist one Model Request."""


class AnswerApplicationSkillRuntimePort(Protocol):
    """Resolves one project-bound, traced Skill context for an answer request."""

    def resolve_context(
        self,
        *,
        project_id: str,
        consumer: str,
        task_kind: str,
        task_text: str,
        invocation_id: str,
        project_summary: str = "",
    ) -> Mapping[str, object]: ...


class AnswerModelRequestError(ValueError):
    """Raised when recall evidence is not safe enough for model answer generation."""


_SKILL_RESOLUTION_ID = re.compile(r"^skill-resolution-[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class AnswerModelRequestResult:
    project_id: str
    recall_result_id: str
    model_request_id: str
    source_ref_count: int
    application_skill_resolution_id: str | None


class CreateAnswerModelRequestFromRecallResult:
    """Creates a local answer Model Request from evidence-bearing Recall Result only."""

    def __init__(
        self,
        *,
        recalls: RecallResultRepositoryPort,
        model_requests: ModelRequestRepositoryPort,
        application_skills: AnswerApplicationSkillRuntimePort | None = None,
        namespace_id: str = "default",
    ) -> None:
        self._recalls = recalls
        self._model_requests = model_requests
        self._application_skills = application_skills
        self._namespace_id = namespace_id

    def execute(
        self,
        recall_result_id: str,
        *,
        created_at: str | None = None,
    ) -> AnswerModelRequestResult:
        recall_result = self._recall_result(recall_result_id)
        hits = _evidence_hits(recall_result)
        recall_request = self._recall_request(recall_result)
        project_id = _required_str(recall_result, "project_id")
        query = _required_str(recall_request, "query")
        source_refs = _source_refs_from_hits(hits)
        if not source_refs:
            raise AnswerModelRequestError("Recall Result evidence must include source refs")
        model_request_id = _answer_model_request_id(
            recall_result_id=_required_str(recall_result, "id"),
            query=query,
            hits=hits,
        )
        application_skill = self._resolve_application_skill(
            project_id=project_id,
            query=query,
            hits=hits,
            model_request_id=model_request_id,
        )
        model_request = self._model_request(
            recall_request=recall_request,
            recall_result=recall_result,
            hits=hits,
            source_refs=source_refs,
            model_request_id=model_request_id,
            application_skill=application_skill,
            created_at=created_at or _utc_now(),
        )
        saved = self._model_requests.save_request(model_request)
        return AnswerModelRequestResult(
            project_id=project_id,
            recall_result_id=recall_result_id,
            model_request_id=_required_str(saved, "id"),
            source_ref_count=len(source_refs),
            application_skill_resolution_id=(
                _required_str(application_skill, "resolution_id")
                if application_skill is not None
                else None
            ),
        )

    def _resolve_application_skill(
        self,
        *,
        project_id: str,
        query: str,
        hits: Sequence[Mapping[str, object]],
        model_request_id: str,
    ) -> Mapping[str, object] | None:
        if self._application_skills is None:
            return None
        try:
            value = self._application_skills.resolve_context(
                project_id=project_id,
                consumer="answer.model-request",
                task_kind="project-answer",
                task_text=query,
                invocation_id=model_request_id,
                project_summary=_project_summary(hits),
            )
        except Exception as error:
            raise AnswerModelRequestError("Application Skill resolution failed") from error
        return _application_skill_context(value, project_id=project_id)

    def _recall_result(self, recall_result_id: str) -> Mapping[str, object]:
        result = self._recalls.get_result(recall_result_id)
        if result is None:
            raise AnswerModelRequestError(f"Recall Result not found: {recall_result_id}")
        status = result.get("status")
        if status not in {"evidence_found", "partial"}:
            raise AnswerModelRequestError(f"Recall Result is not evidence-bearing: {status}")
        errors = result.get("errors")
        if not isinstance(errors, Sequence) or isinstance(errors, (str, bytes)):
            raise AnswerModelRequestError("Recall Result errors must be a list")
        blocked_codes = {
            str(error.get("code"))
            for error in errors
            if isinstance(error, Mapping) and error.get("code") in {"insufficient_evidence", "index_unavailable"}
        }
        if blocked_codes:
            raise AnswerModelRequestError(f"Recall Result has blocking errors: {', '.join(sorted(blocked_codes))}")
        return result

    def _recall_request(self, recall_result: Mapping[str, object]) -> Mapping[str, object]:
        request_id = _required_str(recall_result, "request_id")
        request = self._recalls.get_request(request_id)
        if request is None:
            raise AnswerModelRequestError(f"Recall Request not found: {request_id}")
        if request.get("project_id") != recall_result.get("project_id"):
            raise AnswerModelRequestError("Recall Request and Result project_id mismatch")
        return request

    def _model_request(
        self,
        *,
        recall_request: Mapping[str, object],
        recall_result: Mapping[str, object],
        hits: list[Mapping[str, object]],
        source_refs: list[dict[str, object]],
        model_request_id: str,
        application_skill: Mapping[str, object] | None,
        created_at: str,
    ) -> dict[str, object]:
        query = _required_str(recall_request, "query")
        recall_result_id = _required_str(recall_result, "id")
        return {
            "schema_version": "1.0.0",
            "id": model_request_id,
            "project_id": _required_str(recall_result, "project_id"),
            "capability": "text_generation",
            "provider_preference": {
                "mode": "local_only",
                "provider": None,
                "model": None,
                "allow_remote": False,
                "config_version": 1,
            },
            "payload": {
                "kind": "answer",
                "content": _answer_prompt(
                    namespace_id=self._namespace_id,
                    query=query,
                    hits=hits,
                    application_skill_context=(
                        str(application_skill.get("context_markdown") or "")
                        if application_skill is not None
                        else ""
                    ),
                ),
                "input_refs": _input_refs(
                    namespace_id=self._namespace_id,
                    recall_result=recall_result,
                    hits=hits,
                    application_skill=application_skill,
                ),
                "source_refs": source_refs,
                "recall_result_id": recall_result_id,
            },
            "privacy": {
                "scope": "local_only",
                "pii": "possible",
                "allow_remote": False,
                "redaction": {
                    "applied": False,
                    "strategy": "none",
                },
                "retention": "none",
            },
            "timeout": {
                "request_timeout_ms": 60000,
                "idle_timeout_ms": 10000,
                "deadline_at": None,
            },
            "cancel": {
                "cancellable": True,
                "cancel_token": _stable_id("cancel-token", model_request_id),
                "requested": False,
            },
            "budget": {
                "max_input_tokens": 12000,
                "max_output_tokens": 2048,
                "max_total_tokens": 14048,
                "max_cost_usd": 0,
            },
            "response_schema": {
                "type": "text",
                "json_schema_uri": None,
                "strict": False,
            },
            "created_at": created_at,
        }


def _evidence_hits(recall_result: Mapping[str, object]) -> list[Mapping[str, object]]:
    hits = recall_result.get("hits")
    if not isinstance(hits, Sequence) or isinstance(hits, (str, bytes)):
        raise AnswerModelRequestError("Recall Result hits must be a list")
    evidence_hits = [hit for hit in hits if isinstance(hit, Mapping)]
    if not evidence_hits:
        raise AnswerModelRequestError("Recall Result must include evidence hits before model request")
    evidence_hits.sort(
        key=lambda hit: (
            _ANSWER_LAYER_ORDER.get(str(hit.get("layer")), 99),
            str(hit.get("object_id") or ""),
        )
    )
    return evidence_hits


_ANSWER_LAYER_ORDER = {
    "l4_persona": 0,
    "l3_project_skill": 1,
    "l3_series_memory": 2,
    "l2_scenario": 3,
    "l1_atom": 4,
    "l0_source": 5,
}


def _source_refs_from_hits(hits: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for hit in hits:
        source_refs = hit.get("source_refs")
        if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)):
            continue
        for candidate in source_refs:
            if not isinstance(candidate, Mapping):
                continue
            source_id = candidate.get("source_id")
            locator = candidate.get("locator")
            quote = candidate.get("quote")
            if not isinstance(source_id, str) or not source_id:
                continue
            if not isinstance(locator, str) or not locator:
                continue
            key = (source_id, locator, quote if isinstance(quote, str) else None)
            if key in seen:
                continue
            seen.add(key)
            ref: dict[str, object] = {"source_id": source_id, "locator": locator}
            if isinstance(quote, str):
                ref["quote"] = quote
            refs.append(ref)
    return refs


def _input_refs(
    *,
    namespace_id: str,
    recall_result: Mapping[str, object],
    hits: Sequence[Mapping[str, object]],
    application_skill: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    project_id = _required_str(recall_result, "project_id")
    recall_result_id = _required_str(recall_result, "id")
    refs = [
        {
            "kind": "recall_result",
            "object_id": recall_result_id,
            "uri": f"crp://{namespace_id}/recall-results/{recall_result_id}.json",
        }
    ]
    seen = {("recall_result", recall_result_id)}
    for hit in hits:
        kind = _input_ref_kind(_required_str(hit, "layer"))
        object_id = _required_str(hit, "object_id")
        key = (kind, object_id)
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            {
                "kind": kind,
                "object_id": object_id,
                "uri": _input_ref_uri(
                    namespace_id=namespace_id,
                    project_id=project_id,
                    kind=kind,
                    object_id=object_id,
                ),
            }
        )
    if application_skill is not None:
        resolution_id = _required_str(application_skill, "resolution_id")
        refs.append(
            {
                "kind": "application_skill_resolution",
                "object_id": resolution_id,
                "uri": (
                    f"crp://{namespace_id}/application-skill-resolutions/"
                    f"{resolution_id}.json"
                ),
            }
        )
    return refs


def _input_ref_kind(layer: str) -> str:
    layer_to_kind = {
        "l4_persona": "persona",
        "l3_project_skill": "project_skill",
        "l3_persona": "persona",
        "l3_series_memory": "series_memory",
        "l2_scenario": "scenario",
        "l1_atom": "atom",
        "l0_source": "source",
    }
    try:
        return layer_to_kind[layer]
    except KeyError as exc:
        raise AnswerModelRequestError(f"Unsupported recall layer: {layer}") from exc


def _input_ref_uri(*, namespace_id: str, project_id: str, kind: str, object_id: str) -> str:
    if kind == "source":
        return f"crp://{namespace_id}/sources/{object_id}.json"
    if kind == "project_skill":
        return f"crp://{namespace_id}/projects/{project_id}/project-skill.json"
    return f"crp://{namespace_id}/projects/{project_id}/{kind}s/{object_id}.json"


def _answer_prompt(
    *,
    namespace_id: str,
    query: str,
    hits: Sequence[Mapping[str, object]],
    application_skill_context: str,
) -> str:
    evidence_blocks = []
    for index, hit in enumerate(hits, start=1):
        source_links = _markdown_source_links(namespace_id=namespace_id, source_refs=hit.get("source_refs"))
        evidence_blocks.append(
            f"[{index}] layer={_required_str(hit, 'layer')} object={_required_str(hit, 'object_id')}\n"
            f"{_required_str(hit, 'snippet')}\n"
            f"来源：{source_links or '无可点击来源'}"
        )
    fixed_policy = (
        "你是项目记忆证据回答器。请只根据以下已召回证据回答用户问题。"
        "若证据不足，必须说明不足，不得补充未引用信息。\n\n"
        "记忆读取顺序固定为 L4 稳定画像 → L3 系列与项目方法 → "
        "L2 结构化场景 → L1 原子事实 → L0 原始来源。"
        "高层用于约束和定位，低层只用于补足细节或核验原文；"
        "不得把未召回层当作已知内容。\n\n"
        f"回答末尾必须给出 Markdown 可点击来源，格式为 [source_id#locator](crp://{namespace_id}/sources/source_id.json#locator)。\n\n"
    )
    skill_section = f"{application_skill_context.rstrip()}\n\n" if application_skill_context else ""
    return (
        fixed_policy
        + skill_section
        + f"用户问题：{query}\n\n"
        + "召回证据：\n"
        + "\n\n".join(evidence_blocks)
    )


def _answer_model_request_id(
    *,
    recall_result_id: str,
    query: str,
    hits: Sequence[Mapping[str, object]],
) -> str:
    return _stable_id(
        "model-request-answer",
        recall_result_id,
        query,
        *(_required_str(hit, "hit_id") for hit in hits),
    )


def _project_summary(hits: Sequence[Mapping[str, object]]) -> str:
    summaries = [
        _required_str(hit, "snippet")
        for hit in hits
        if hit.get("layer") == "l3_project_skill"
    ]
    return "\n".join(summaries)[:8000]


def _application_skill_context(
    value: object,
    *,
    project_id: str,
) -> dict[str, object]:
    expected = {
        "resolution_id",
        "project_id",
        "consumer",
        "context_markdown",
        "selected",
        "fallback",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise AnswerModelRequestError("Application Skill context schema drifted")
    if value.get("project_id") != project_id or value.get("consumer") != "answer.model-request":
        raise AnswerModelRequestError("Application Skill context project or consumer drifted")
    resolution_id = _required_str(value, "resolution_id")
    if not _SKILL_RESOLUTION_ID.fullmatch(resolution_id):
        raise AnswerModelRequestError("Application Skill resolution id is invalid")
    context = value.get("context_markdown")
    if not isinstance(context, str) or len(context.encode("utf-8")) > 32 * 1024:
        raise AnswerModelRequestError("Application Skill context is invalid or unbounded")
    selected = value.get("selected")
    if not isinstance(selected, Sequence) or isinstance(selected, (str, bytes)) or len(selected) > 3:
        raise AnswerModelRequestError("Application Skill selection is invalid or unbounded")
    clean_selected: list[dict[str, object]] = []
    for item in selected:
        if not isinstance(item, Mapping):
            raise AnswerModelRequestError("Application Skill selection item is invalid")
        clean_selected.append(dict(item))
    fallback = value.get("fallback")
    if fallback not in {"none", "default_consumer_flow"}:
        raise AnswerModelRequestError("Application Skill fallback is invalid")
    if (bool(clean_selected) and (not context or fallback != "none")) or (
        not clean_selected and (context or fallback != "default_consumer_flow")
    ):
        raise AnswerModelRequestError("Application Skill context selection drifted")
    return {
        "resolution_id": resolution_id,
        "project_id": project_id,
        "consumer": "answer.model-request",
        "context_markdown": context,
        "selected": clean_selected,
        "fallback": fallback,
    }


def _markdown_source_links(*, namespace_id: str, source_refs: object) -> str:
    if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)):
        return ""
    links: list[str] = []
    seen: set[tuple[str, str]] = set()
    for ref in source_refs:
        if not isinstance(ref, Mapping):
            continue
        source_id = ref.get("source_id")
        locator = ref.get("locator")
        if not isinstance(source_id, str) or not source_id:
            continue
        if not isinstance(locator, str) or not locator:
            continue
        key = (source_id, locator)
        if key in seen:
            continue
        seen.add(key)
        links.append(f"[{source_id}#{locator}](crp://{namespace_id}/sources/{source_id}.json#{locator})")
    return "，".join(links)


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise AnswerModelRequestError(f"{key} is required")
    return value


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
