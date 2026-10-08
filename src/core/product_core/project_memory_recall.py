from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from core.memory_core import MemoryReaderPort
from core.project_skill_core import ProjectSkillRepositoryPort


class PersonaReaderPort(Protocol):
    """Read-only Persona digest reader used by Q&A recall.

    Implementations (e.g. ``ObjectStorePersonaRepository``) return a
    ``PersonaDigest`` from ``digest()``. When no Persona has been published,
    ``digest.ready`` is ``False`` and recall skips the Persona hit.
    """

    def digest(self, scope: str | None = None) -> object:
        """Return a Persona digest (``ready=False`` when no Persona)."""


class RecallRepositoryPort(Protocol):
    """Persists recall requests and evidence results."""

    def create_project_default_request(
        self,
        *,
        project_id: str,
        query: str,
        project_skill_id: str,
        layers: Sequence[str] | None = None,
        created_at: str | None = None,
    ) -> Mapping[str, object]:
        """Create the project-scoped request that starts from Project Skill."""

    def save_result(self, result: Mapping[str, object]) -> Mapping[str, object]:
        """Persist a Recall Result evidence package."""

    def create_insufficient_evidence_result(
        self,
        *,
        request_id: str,
        message: str = "没有找到足够证据。",
        created_at: str | None = None,
    ) -> Mapping[str, object]:
        """Persist a no-evidence Recall Result."""


class ProjectMemoryRecallError(ValueError):
    """Raised when same-project evidence selection cannot run safely."""


@dataclass(frozen=True, slots=True)
class ProjectMemoryRecallResult:
    project_id: str
    skill_id: str
    request_id: str
    result_id: str
    hit_count: int
    evidence_hits: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class BudgetedRecallHits:
    selected: list[dict[str, object]]
    dropped_hit_ids: list[str]
    reason: str


class CreateProjectMemoryRecall:
    """Selects Project Skill plus same-project memory evidence before model generation."""

    def __init__(
        self,
        *,
        skills: ProjectSkillRepositoryPort,
        memory: MemoryReaderPort,
        recalls: RecallRepositoryPort,
        persona: PersonaReaderPort | None = None,
    ) -> None:
        self._skills = skills
        self._memory = memory
        self._recalls = recalls
        self._persona = persona

    def execute(
        self,
        project_id: str,
        *,
        query: str,
        layers: Sequence[str] | None = None,
        created_at: str | None = None,
    ) -> ProjectMemoryRecallResult:
        requested_layers = _requested_layers(layers)
        skill = (
            self._current_skill(project_id)
            if "l3_project_skill" in requested_layers
            else None
        )
        skill_id = _required_str(skill, "id") if skill is not None else ""
        request = self._recalls.create_project_default_request(
            project_id=project_id,
            query=query,
            project_skill_id=skill_id,
            layers=requested_layers,
            created_at=created_at,
        )
        candidate_hits: list[dict[str, object]] = []
        if "l4_persona" in requested_layers:
            persona_hit = _persona_hit(request=request, persona=self._persona)
            if persona_hit is not None:
                candidate_hits.append(persona_hit)
        if "l3_project_skill" in requested_layers:
            assert skill is not None
            candidate_hits.append(_skill_hit(request=request, skill=skill))
        memory_layers = frozenset(requested_layers) & {
            "l3_series_memory",
            "l2_scenario",
            "l1_atom",
        }
        if memory_layers:
            candidate_hits.extend(
                _memory_hits(
                    request=request,
                    memory_objects=self._memory.list_by_project(project_id),
                    allowed_layers=memory_layers,
                    query=query,
                ),
            )
        budgeted = _apply_budget(request=request, hits=candidate_hits)
        if not budgeted.selected:
            result = self._recalls.create_insufficient_evidence_result(
                request_id=_required_str(request, "id"),
                created_at=created_at,
            )
        else:
            result = self._recalls.save_result(
                _recall_result(
                    request=request,
                    hits=budgeted.selected,
                    dropped_hit_ids=budgeted.dropped_hit_ids,
                    truncation_reason=budgeted.reason,
                    created_at=created_at or _required_str(request, "created_at"),
                )
            )
        return ProjectMemoryRecallResult(
            project_id=project_id,
            skill_id=skill_id,
            request_id=_required_str(request, "id"),
            result_id=_required_str(result, "id"),
            hit_count=len(budgeted.selected),
            evidence_hits=tuple(
                dict(hit) for hit in budgeted.selected if isinstance(hit, Mapping)
            ),
        )

    def _current_skill(self, project_id: str) -> Mapping[str, object]:
        skill = self._skills.load(project_id)
        if skill is None:
            raise ProjectMemoryRecallError(f"Project Skill not found: {project_id}")
        if skill.get("status") != "active":
            raise ProjectMemoryRecallError("Project Skill must be active for memory recall")
        if skill.get("trust_status") not in {"user_confirmed", "trusted", "system_generated"}:
            raise ProjectMemoryRecallError("Project Skill trust status is not eligible for memory recall")
        conflict = skill.get("conflict")
        if not isinstance(conflict, Mapping) or conflict.get("status") != "none":
            raise ProjectMemoryRecallError("Project Skill cannot drive memory recall while conflict is unresolved")
        if not _source_refs_from_skill(skill):
            raise ProjectMemoryRecallError("Project Skill memory recall requires source refs")
        return skill


def _recall_result(
    *,
    request: Mapping[str, object],
    hits: list[dict[str, object]],
    dropped_hit_ids: list[str],
    truncation_reason: str,
    created_at: str,
) -> dict[str, object]:
    request_id = _required_str(request, "id")
    project_id = _required_str(request, "project_id")
    layers = _required_string_list(request, "layers")
    covered_layers = _unique_strings(hit["layer"] for hit in hits if isinstance(hit.get("layer"), str))
    missing_layers = [layer for layer in layers if layer not in covered_layers]
    token_total = sum(_required_int(hit, "token_estimate") for hit in hits)
    source_ref_count = sum(len(_required_list(hit, "source_refs")) for hit in hits)
    truncation_applied = bool(dropped_hit_ids)
    return {
        "schema_version": "1.0.0",
        "id": _stable_id("recall-result", request_id, _evidence_fingerprint(hits)),
        "request_id": request_id,
        "project_id": project_id,
        "status": "partial" if missing_layers else "evidence_found",
        "hits": hits,
        "coverage": {
            "status": "partial" if missing_layers else "sufficient",
            "requested_layers": layers,
            "covered_layers": covered_layers,
            "missing_layers": missing_layers,
            "low_trust": False,
            "source_ref_count": source_ref_count,
        },
        "truncation": {
            "applied": truncation_applied,
            "reason": truncation_reason if truncation_applied else "none",
            "dropped_hit_ids": dropped_hit_ids,
            "final_hit_count": len(hits),
            "final_token_estimate": token_total,
        },
        "explanation": {
            "summary": (
                "已生成 L4 Persona、L3 Project Skill/Series Memory、"
                "L2 Scenario、L1 Atom 的兼容回退证据包；生产问答由渐进检索器按需选层。"
            ),
            "layer_order": layers,
            "warnings": ["legacy_fallback_package"],
        },
        "cross_project": {
            "used": False,
            "grant_id": None,
            "project_ids": [],
        },
        "errors": [],
        "created_at": created_at,
    }


def _evidence_fingerprint(hits: Sequence[Mapping[str, object]]) -> str:
    canonical = json.dumps(hits, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _persona_hit(
    *,
    request: Mapping[str, object],
    persona: PersonaReaderPort | None,
) -> dict[str, object] | None:
    """Build a Persona L4 hit for Q&A recall.

    Returns None when no Persona reader is configured or no Persona has been
    published (``ready=False``). Persona is cross-project, so ``project_id``
    is set to the request's project but ``source_project_label`` is None.
    """
    if persona is None:
        return None
    digest = persona.digest("global")
    if digest is None:
        return None
    ready = getattr(digest, "ready", False)
    if not ready:
        return None
    request_id = _required_str(request, "id")
    project_id = _required_str(request, "project_id")
    scope = getattr(digest, "scope", "global") or "global"
    snippet = _persona_snippet(digest)
    source_refs = _persona_source_refs(digest)
    trust_status = getattr(digest, "trust_status", None) or "system_generated"
    return {
        "hit_id": _stable_id("hit-persona", request_id, scope),
        "layer": "l4_persona",
        "object_id": f"persona-{scope}",
        "project_id": project_id,
        "source_project_label": None,
        "trust_status": trust_status,
        "score": 0.95,
        "token_estimate": _token_estimate(snippet),
        "source_refs": source_refs,
        "snippet": snippet,
        "explanation": "Persona 提供跨项目的用户偏好与约束作为高层上下文。",
    }


def _persona_snippet(digest: object) -> str:
    """Render a short text snippet from a Persona digest for recall evidence."""
    parts: list[str] = []
    language_style = getattr(digest, "language_style", None)
    if language_style:
        parts.append("语言风格：" + "；".join(str(s) for s in language_style))
    format_preferences = getattr(digest, "format_preferences", None)
    if format_preferences:
        parts.append("格式偏好：" + "；".join(str(s) for s in format_preferences))
    common_projects = getattr(digest, "common_projects", None)
    if common_projects:
        parts.append("常用项目：" + "；".join(str(s) for s in common_projects))
    avoidances = getattr(digest, "avoidances", None)
    if avoidances:
        parts.append("需要避免：" + "；".join(str(s) for s in avoidances))
    if not parts:
        return "Persona 已就绪，但暂无可读偏好。"
    return "Persona 高层上下文：\n" + "\n".join(parts)


def _persona_source_refs(digest: object) -> list[dict[str, object]]:
    """Flatten Persona evidence_refs into recall source_refs format.

    Persona evidence_refs have shape ``{object_type, object_id, source_refs}``
    where ``source_refs`` is a list of ``{source_id, locator, ...}`` dicts.
    """
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str, str | None]] = set()
    evidence_refs = getattr(digest, "evidence_refs", None)
    if not evidence_refs:
        return refs
    for evidence in evidence_refs:
        inner_refs = getattr(evidence, "source_refs", None) if not isinstance(evidence, Mapping) else evidence.get("source_refs")
        if not inner_refs:
            continue
        for candidate in inner_refs:
            if isinstance(candidate, Mapping):
                _append_source_ref(refs, seen, candidate)
            else:
                src_dict = dict(getattr(candidate, "__dict__", {}))
                if src_dict:
                    _append_source_ref(refs, seen, src_dict)
    return refs


def _skill_hit(*, request: Mapping[str, object], skill: Mapping[str, object]) -> dict[str, object]:
    project_id = _required_str(request, "project_id")
    skill_id = _required_str(skill, "id")
    source_refs = _source_refs_from_skill(skill)
    snippet = _skill_snippet(skill)
    return {
        "hit_id": _stable_id("hit-project-skill", _required_str(request, "id"), skill_id),
        "layer": "l3_project_skill",
        "object_id": skill_id,
        "project_id": project_id,
        "source_project_label": None,
        "trust_status": _required_str(skill, "trust_status"),
        "score": 1.0,
        "token_estimate": _token_estimate(snippet),
        "source_refs": source_refs,
        "snippet": snippet,
        "explanation": "当前项目 Skill 是项目问答的首个证据层。",
    }


def _memory_hits(
    *,
    request: Mapping[str, object],
    memory_objects: Sequence[Mapping[str, object]],
    allowed_layers: frozenset[str],
    query: str,
) -> list[dict[str, object]]:
    project_id = _required_str(request, "project_id")
    hits: list[dict[str, object]] = []
    for item in memory_objects:
        hit = _memory_hit(project_id=project_id, item=item)
        if hit is not None and hit["layer"] in allowed_layers:
            hits.append(hit)
    hits.sort(
        key=lambda hit: (
            _MEMORY_LAYER_ORDER[_required_str(hit, "layer")],
            -_query_relevance(query, _required_str(hit, "snippet")),
            -float(hit.get("score") or 0.0),
            _required_str(hit, "object_id"),
        )
    )
    return hits


def _memory_hit(*, project_id: str, item: Mapping[str, object]) -> dict[str, object] | None:
    if item.get("stale") is True:
        return None
    trust_status = item.get("trust_status")
    if trust_status not in {"trusted", "user_confirmed", "system_generated"}:
        return None
    object_id = _required_str(item, "id")
    source_refs = _source_refs_from_memory(item)
    if not source_refs:
        return None
    layer = _memory_layer(item)
    if layer is None:
        return None
    snippet = _memory_snippet(layer=layer, item=item)
    return {
        "hit_id": _stable_id("hit", layer, object_id),
        "layer": layer,
        "object_id": object_id,
        "project_id": project_id,
        "source_project_label": None,
        "trust_status": trust_status,
        "score": _layer_score(layer),
        "token_estimate": _token_estimate(snippet),
        "source_refs": source_refs,
        "snippet": snippet,
        "explanation": _layer_explanation(layer),
    }


def _apply_budget(*, request: Mapping[str, object], hits: list[dict[str, object]]) -> BudgetedRecallHits:
    budget = request.get("budget")
    max_hits = 12
    max_tokens = 12000
    per_layer_limits: Mapping[str, object] = {}
    if isinstance(budget, Mapping):
        if isinstance(budget.get("max_hits"), int):
            max_hits = int(budget["max_hits"])
        if isinstance(budget.get("max_tokens"), int):
            max_tokens = int(budget["max_tokens"])
        limits = budget.get("per_layer_limits")
        if isinstance(limits, Mapping):
            per_layer_limits = limits
    selected: list[dict[str, object]] = []
    dropped_hit_ids: list[str] = []
    counts: dict[str, int] = {}
    token_total = 0
    hit_limited = False
    token_limited = False
    for hit in hits:
        layer = _required_str(hit, "layer")
        token_estimate = _required_int(hit, "token_estimate")
        layer_limit = per_layer_limits.get(layer, max_hits)
        if isinstance(layer_limit, int) and counts.get(layer, 0) >= layer_limit:
            dropped_hit_ids.append(_required_str(hit, "hit_id"))
            hit_limited = True
            continue
        if len(selected) >= max_hits:
            dropped_hit_ids.append(_required_str(hit, "hit_id"))
            hit_limited = True
            continue
        if token_total + token_estimate > max_tokens:
            dropped_hit_ids.append(_required_str(hit, "hit_id"))
            token_limited = True
            continue
        selected.append(hit)
        counts[layer] = counts.get(layer, 0) + 1
        token_total += token_estimate
    return BudgetedRecallHits(
        selected=selected,
        dropped_hit_ids=dropped_hit_ids,
        reason=_truncation_reason(hit_limited=hit_limited, token_limited=token_limited),
    )


def _truncation_reason(*, hit_limited: bool, token_limited: bool) -> str:
    if hit_limited and token_limited:
        return "budget"
    if token_limited:
        return "token_limit"
    if hit_limited:
        return "hit_limit"
    return "none"


def _memory_layer(item: Mapping[str, object]) -> str | None:
    if "overview" in item and "scenario_ids" in item and "project_ids" in item:
        return "l3_series_memory"
    if "summary" in item and "atom_ids" in item:
        return "l2_scenario"
    if "content" in item and "atom_type" in item:
        return "l1_atom"
    return None


def _memory_snippet(*, layer: str, item: Mapping[str, object]) -> str:
    if layer == "l3_series_memory":
        return _required_str(item, "overview")
    if layer == "l2_scenario":
        return _required_str(item, "summary")
    return _required_str(item, "content")


def _layer_score(layer: str) -> float:
    return {
        "l4_persona": 0.95,
        "l3_series_memory": 0.86,
        "l2_scenario": 0.78,
        "l1_atom": 0.7,
    }.get(layer, 0.5)


def _layer_explanation(layer: str) -> str:
    return {
        "l4_persona": "L4 Persona 提供跨项目的稳定偏好、事实与约束作为第一层上下文。",
        "l3_series_memory": "L3 Series Memory 补充当前项目长期总览。",
        "l2_scenario": "L2 Scenario 补充当前项目阶段性摘要。",
        "l1_atom": "L1 Atom 补充当前项目可追溯事实。",
    }[layer]


def _skill_snippet(skill: Mapping[str, object]) -> str:
    lines = [_required_str(skill, "purpose")]
    for rule in _required_list(skill, "output_rules"):
        if not isinstance(rule, Mapping):
            continue
        value = rule.get("rule")
        if isinstance(value, str) and value:
            lines.append(value)
            break
    return " ".join(lines)


def _source_refs_from_skill(skill: Mapping[str, object]) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for candidate in (
        *_required_list(skill, "source_refs"),
        *_required_list(skill, "evidence_refs"),
    ):
        _append_source_ref(refs, seen, candidate)
    for rule in _required_list(skill, "output_rules"):
        if not isinstance(rule, Mapping):
            continue
        for candidate in _required_list(rule, "source_refs"):
            _append_source_ref(refs, seen, candidate)
    return refs


def _source_refs_from_memory(item: Mapping[str, object]) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for candidate in _required_list(item, "source_refs"):
        _append_source_ref(refs, seen, candidate)
    return refs


def _append_source_ref(
    refs: list[dict[str, object]],
    seen: set[tuple[str, str, str | None]],
    candidate: object,
) -> None:
    if not isinstance(candidate, Mapping):
        return
    source_id = candidate.get("source_id")
    locator = candidate.get("locator")
    quote = candidate.get("quote")
    if not isinstance(source_id, str) or not source_id:
        return
    if not isinstance(locator, str) or not locator:
        return
    key = (source_id, locator, quote if isinstance(quote, str) else None)
    if key in seen:
        return
    seen.add(key)
    ref: dict[str, object] = {"source_id": source_id, "locator": locator}
    if isinstance(quote, str):
        ref["quote"] = quote
    refs.append(ref)


def _token_estimate(text: str) -> int:
    return max(1, min(12000, len(text)))


_DEFAULT_RECALL_LAYERS = (
    "l4_persona",
    "l3_project_skill",
    "l3_series_memory",
    "l2_scenario",
    "l1_atom",
    "l0_source",
)
_MEMORY_LAYER_ORDER = {
    "l3_series_memory": 0,
    "l2_scenario": 1,
    "l1_atom": 2,
}


def _requested_layers(layers: Sequence[str] | None) -> tuple[str, ...]:
    if layers is None:
        return _DEFAULT_RECALL_LAYERS
    requested = tuple(dict.fromkeys(layers))
    if any(layer not in _DEFAULT_RECALL_LAYERS for layer in requested):
        raise ProjectMemoryRecallError("recall layers contain an unsupported layer")
    return requested


def _query_relevance(query: str, content: str) -> float:
    query_terms = _search_terms(query)
    if not query_terms:
        return 0.0
    content_terms = _search_terms(content)
    return len(query_terms & content_terms) / len(query_terms)


def _search_terms(value: str) -> frozenset[str]:
    normalized = value.casefold()
    latin = {
        token
        for token in re.findall(r"[a-z0-9_]+", normalized)
        if len(token) > 1
    }
    han = {
        char
        for char in normalized
        if "\u3400" <= char <= "\u9fff"
    }
    return frozenset((*latin, *han))


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ProjectMemoryRecallError(f"{key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProjectMemoryRecallError(f"{key} must be an integer")
    return value


def _required_list(mapping: Mapping[str, object], key: str) -> list[object]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ProjectMemoryRecallError(f"{key} must be a list")
    return list(value)


def _required_string_list(mapping: Mapping[str, object], key: str) -> list[str]:
    values = _required_list(mapping, key)
    if not all(isinstance(value, str) and value for value in values):
        raise ProjectMemoryRecallError(f"{key} must contain strings")
    return list(values)


def _unique_strings(values: object) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value not in result:
            result.append(value)
    return result


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"
