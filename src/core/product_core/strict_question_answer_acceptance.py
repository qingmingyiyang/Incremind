from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class StrictQuestionAnswerAcceptanceError(ValueError):
    """Raised when Q&A readiness payloads are malformed."""


@dataclass(frozen=True, slots=True)
class StrictQuestionAnswerAcceptance:
    status: str
    checks: Mapping[str, bool]
    source_links: tuple[str, ...]
    next_step: str


class ReviewStrictQuestionAnswerAcceptance:
    """Verifies the product rule: read L3/L2 first, drill into L1/L0 only with traceable sources."""

    def execute(
        self,
        *,
        recall_result: Mapping[str, object],
        model_request: Mapping[str, object],
    ) -> StrictQuestionAnswerAcceptance:
        hits = _hits(recall_result)
        source_links = tuple(_source_links(hits))
        checks = {
            "starts_from_l3_project_skill": _starts_from_l3_project_skill(hits),
            "high_layers_before_low_layers": _high_layers_before_low_layers(hits),
            "lower_layers_have_source_refs": _lower_layers_have_source_refs(hits),
            "model_request_uses_recall_result": _model_request_uses_recall_result(
                recall_result=recall_result,
                model_request=model_request,
            ),
            "answer_prompt_requires_clickable_sources": _answer_prompt_requires_clickable_sources(model_request),
            "clickable_source_links_ready": bool(source_links),
        }
        status = "ready" if all(checks.values()) else "blocked"
        return StrictQuestionAnswerAcceptance(
            status=status,
            checks=checks,
            source_links=source_links,
            next_step=(
                "strict_question_answer_ready"
                if status == "ready"
                else "补齐 Project Skill 首读、L3/L2 优先顺序、下钻来源或回答来源链接后再生成答案。"
            ),
        )


def serialize_strict_question_answer_acceptance(
    result: StrictQuestionAnswerAcceptance,
) -> dict[str, object]:
    return {
        "status": result.status,
        "checks": dict(result.checks),
        "source_links": list(result.source_links),
        "next_step": result.next_step,
    }


def _hits(recall_result: Mapping[str, object]) -> list[Mapping[str, object]]:
    hits = recall_result.get("hits")
    if not isinstance(hits, Sequence) or isinstance(hits, (str, bytes)):
        raise StrictQuestionAnswerAcceptanceError("recall_result.hits must be a list")
    result = [hit for hit in hits if isinstance(hit, Mapping)]
    if not result:
        raise StrictQuestionAnswerAcceptanceError("recall_result.hits cannot be empty")
    return result


def _starts_from_l3_project_skill(hits: Sequence[Mapping[str, object]]) -> bool:
    first_non_persona = next(
        (
            hit
            for hit in hits
            if hit.get("layer") not in {"l4_persona", "l3_persona"}
        ),
        None,
    )
    return isinstance(first_non_persona, Mapping) and first_non_persona.get("layer") == "l3_project_skill"


def _high_layers_before_low_layers(hits: Sequence[Mapping[str, object]]) -> bool:
    rank = {
        "l4_persona": 0,
        "l3_persona": 0,
        "l3_project_skill": 0,
        "l3_series_memory": 0,
        "l2_scenario": 1,
        "l1_atom": 2,
        "l0_source": 3,
    }
    previous = -1
    for hit in hits:
        layer = hit.get("layer")
        if layer not in rank:
            return False
        current = rank[str(layer)]
        if current < previous:
            return False
        previous = current
    return True


def _lower_layers_have_source_refs(hits: Sequence[Mapping[str, object]]) -> bool:
    lower_hits = [hit for hit in hits if hit.get("layer") in {"l1_atom", "l0_source"}]
    if not lower_hits:
        return True
    return all(_has_source_refs(hit) for hit in lower_hits)


def _model_request_uses_recall_result(
    *,
    recall_result: Mapping[str, object],
    model_request: Mapping[str, object],
) -> bool:
    payload = model_request.get("payload")
    if not isinstance(payload, Mapping):
        return False
    return payload.get("recall_result_id") == recall_result.get("id")


def _answer_prompt_requires_clickable_sources(model_request: Mapping[str, object]) -> bool:
    payload = model_request.get("payload")
    if not isinstance(payload, Mapping):
        return False
    content = payload.get("content")
    return isinstance(content, str) and "](crp://" in content and "/sources/" in content and "必须给出 Markdown 可点击来源" in content


def _source_links(hits: Sequence[Mapping[str, object]]) -> list[str]:
    links: list[str] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        source_refs = hit.get("source_refs")
        if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)):
            continue
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
            links.append(f"crp://default/sources/{source_id}.json#{locator}")
    return links


def _has_source_refs(hit: Mapping[str, object]) -> bool:
    source_refs = hit.get("source_refs")
    if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)):
        return False
    return any(isinstance(ref, Mapping) and ref.get("source_id") and ref.get("locator") for ref in source_refs)
