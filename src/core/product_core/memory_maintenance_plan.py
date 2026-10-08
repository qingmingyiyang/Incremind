from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata


MAINTENANCE_PLAN_VERSION = "memory-maintenance-plan-v1"
_WORD = re.compile(r"[a-z0-9][a-z0-9_-]{1,31}")
_CJK = re.compile(r"[\u3400-\u9fff]{2,32}")
_STOP = {
    "一个",
    "以及",
    "使用",
    "当前",
    "已经",
    "项目",
    "系列",
    "记忆",
    "进行",
    "这个",
    "需要",
}


class MemoryMaintenancePlanError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryMaintenanceSuggestion:
    suggestion_id: str
    suggestion_type: str
    risk: str
    confidence: float
    target_ids: tuple[str, ...]
    base_revisions: tuple[tuple[str, int, int], ...]
    reason_codes: tuple[str, ...]
    candidate_supported: bool
    batch_candidate_supported: bool
    action_endpoint: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "suggestion_id": self.suggestion_id,
            "suggestion_type": self.suggestion_type,
            "status": "draft",
            "risk": self.risk,
            "confidence": self.confidence,
            "target_ids": list(self.target_ids),
            "base_revisions": [
                {
                    "object_id": object_id,
                    "object_revision": object_revision,
                    "domain_revision": domain_revision,
                }
                for object_id, object_revision, domain_revision
                in self.base_revisions
            ],
            "reason_codes": list(self.reason_codes),
            "candidate_supported": self.candidate_supported,
            "batch_candidate_supported": self.batch_candidate_supported,
            "action_endpoint": self.action_endpoint,
            "requires_user_confirmation": True,
            "auto_apply_allowed": False,
            "content_included": False,
        }


@dataclass(frozen=True, slots=True)
class MemoryMaintenancePlan:
    project_id: str
    plan_fingerprint: str
    suggestions: tuple[MemoryMaintenanceSuggestion, ...]

    def to_payload(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        for suggestion in self.suggestions:
            counts[suggestion.suggestion_type] = (
                counts.get(suggestion.suggestion_type, 0) + 1
            )
        return {
            "plan_version": MAINTENANCE_PLAN_VERSION,
            "project_id": self.project_id,
            "plan_fingerprint": self.plan_fingerprint,
            "suggestion_count": len(self.suggestions),
            "counts": dict(sorted(counts.items())),
            "suggestions": [
                suggestion.to_payload()
                for suggestion in self.suggestions
            ],
            "writes_performed": False,
            "network_called": False,
            "content_included": False,
        }


def build_memory_maintenance_plan(
    *,
    project_id: str,
    atoms: Sequence[Mapping[str, object]],
    scenarios: Sequence[Mapping[str, object]],
    series: Sequence[Mapping[str, object]],
    classification_suggestions: Sequence[Mapping[str, object]],
    freshness_items: Sequence[Mapping[str, object]],
    project_skill: Mapping[str, object] | None,
) -> MemoryMaintenancePlan:
    clean_project_id = _text(project_id, "project_id", 120)
    clean_atoms = _objects(atoms, "atoms")
    clean_scenarios = _objects(scenarios, "scenarios")
    clean_series = _objects(series, "series")
    suggestions = [
        *_classification_suggestions(
            clean_project_id,
            classification_suggestions,
            clean_scenarios,
        ),
        *_freshness_suggestions(
            clean_project_id,
            freshness_items,
            clean_series,
        ),
        *_duplicate_atom_suggestions(clean_project_id, clean_atoms),
        *_fact_replacement_suggestions(clean_project_id, clean_atoms),
        *_series_merge_suggestions(clean_project_id, clean_series),
        *_project_skill_suggestions(
            clean_project_id,
            clean_series,
            project_skill,
        ),
    ]
    unique = {
        suggestion.suggestion_id: suggestion
        for suggestion in suggestions
    }
    ordered = tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                item.risk,
                item.suggestion_type,
                item.suggestion_id,
            ),
        )
    )
    canonical = [
        {
            "suggestion_id": item.suggestion_id,
            "type": item.suggestion_type,
            "targets": item.target_ids,
            "revisions": item.base_revisions,
        }
        for item in ordered
    ]
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "version": MAINTENANCE_PLAN_VERSION,
                "project_id": clean_project_id,
                "suggestions": canonical,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return MemoryMaintenancePlan(
        project_id=clean_project_id,
        plan_fingerprint=fingerprint,
        suggestions=ordered,
    )


def _classification_suggestions(
    project_id: str,
    values: Sequence[Mapping[str, object]],
    scenarios: Sequence[Mapping[str, object]],
) -> tuple[MemoryMaintenanceSuggestion, ...]:
    scenario_by_id = {
        _object_id(item): item
        for item in scenarios
    }
    result: list[MemoryMaintenanceSuggestion] = []
    for item in _mappings(values, "classification_suggestions"):
        scenario_id = _text(
            item.get("scenario_id"),
            "scenario_id",
            160,
        )
        scenario = scenario_by_id.get(scenario_id)
        candidates = item.get("candidates")
        if scenario is None or not isinstance(candidates, list) or not candidates:
            continue
        top = candidates[0]
        if not isinstance(top, Mapping):
            continue
        target_series_id = _text(
            top.get("series_id"),
            "target series_id",
            160,
        )
        score = _score(top.get("score"))
        if score < 0.12:
            continue
        result.append(
            _suggestion(
                project_id=project_id,
                suggestion_type="scenario_reclassification",
                risk="low",
                confidence=score,
                target_ids=(scenario_id, target_series_id),
                base_revisions=(_revision(scenario),),
                reason_codes=("local_series_overlap",),
                candidate_supported=True,
                batch_candidate_supported=True,
                action_endpoint=(
                    "/api/rebuild/memory-hierarchy/"
                    "update-candidates/batch"
                ),
            )
        )
    return tuple(result)


def _freshness_suggestions(
    project_id: str,
    values: Sequence[Mapping[str, object]],
    series: Sequence[Mapping[str, object]],
) -> tuple[MemoryMaintenanceSuggestion, ...]:
    series_by_id = {
        _object_id(item): item
        for item in series
    }
    result: list[MemoryMaintenanceSuggestion] = []
    for item in _mappings(values, "freshness_items"):
        if item.get("needs_refresh") is not True:
            continue
        object_id = _text(
            item.get("series_object_id"),
            "series_object_id",
            160,
        )
        current = series_by_id.get(object_id)
        if current is None:
            continue
        reasons = item.get("reasons")
        reason_codes = tuple(
            sorted(
                {
                    str(reason.get("code"))
                    for reason in reasons
                    if isinstance(reason, Mapping)
                    and isinstance(reason.get("code"), str)
                    and reason.get("code")
                }
            )
        ) if isinstance(reasons, list) else ()
        result.append(
            _suggestion(
                project_id=project_id,
                suggestion_type="series_refresh",
                risk="low",
                confidence=1.0,
                target_ids=(object_id,),
                base_revisions=(_revision(current),),
                reason_codes=reason_codes or ("series_freshness_changed",),
                candidate_supported=True,
                batch_candidate_supported=False,
                action_endpoint=(
                    "/api/rebuild/memory-hierarchy/update-candidates"
                ),
            )
        )
    return tuple(result)


def _duplicate_atom_suggestions(
    project_id: str,
    atoms: Sequence[Mapping[str, object]],
) -> tuple[MemoryMaintenanceSuggestion, ...]:
    groups: dict[str, list[Mapping[str, object]]] = {}
    for atom in atoms:
        preview = _normalized_preview(atom.get("preview"))
        if len(preview) < 4:
            continue
        groups.setdefault(preview, []).append(atom)
    return tuple(
        _suggestion(
            project_id=project_id,
            suggestion_type="duplicate_atom_merge_review",
            risk="high",
            confidence=1.0,
            target_ids=tuple(
                sorted(_object_id(item) for item in group)
            ),
            base_revisions=tuple(
                sorted(_revision(item) for item in group)
            ),
            reason_codes=(
                "exact_normalized_content_match",
                "hard_forget_and_reference_rewrite_required",
            ),
            candidate_supported=False,
            batch_candidate_supported=False,
            action_endpoint=None,
        )
        for group in groups.values()
        if len(group) > 1
    )


def _fact_replacement_suggestions(
    project_id: str,
    atoms: Sequence[Mapping[str, object]],
) -> tuple[MemoryMaintenanceSuggestion, ...]:
    result: list[MemoryMaintenanceSuggestion] = []
    for index, left in enumerate(atoms):
        left_tags = _tags(left)
        if len(left_tags) < 2:
            continue
        for right in atoms[index + 1 :]:
            if left_tags != _tags(right):
                continue
            if _normalized_preview(left.get("preview")) == _normalized_preview(
                right.get("preview")
            ):
                continue
            result.append(
                _suggestion(
                    project_id=project_id,
                    suggestion_type="fact_replacement_review",
                    risk="high",
                    confidence=0.5,
                    target_ids=tuple(
                        sorted((_object_id(left), _object_id(right)))
                    ),
                    base_revisions=tuple(
                        sorted((_revision(left), _revision(right)))
                    ),
                    reason_codes=(
                        "same_tags_different_content",
                        "conflict_requires_human_authority",
                    ),
                    candidate_supported=False,
                    batch_candidate_supported=False,
                    action_endpoint=None,
                )
            )
    return tuple(result)


def _series_merge_suggestions(
    project_id: str,
    series: Sequence[Mapping[str, object]],
) -> tuple[MemoryMaintenanceSuggestion, ...]:
    result: list[MemoryMaintenanceSuggestion] = []
    for index, left in enumerate(series):
        left_features = _features(left)
        for right in series[index + 1 :]:
            right_features = _features(right)
            shared = left_features & right_features
            union = left_features | right_features
            if len(shared) < 3 or not union:
                continue
            score = len(shared) / len(union)
            if score < 0.6:
                continue
            result.append(
                _suggestion(
                    project_id=project_id,
                    suggestion_type="series_merge_review",
                    risk="high",
                    confidence=round(score, 6),
                    target_ids=tuple(
                        sorted((_object_id(left), _object_id(right)))
                    ),
                    base_revisions=tuple(
                        sorted((_revision(left), _revision(right)))
                    ),
                    reason_codes=(
                        "high_title_tag_summary_overlap",
                        "scenario_rewrite_and_rollback_plan_required",
                    ),
                    candidate_supported=False,
                    batch_candidate_supported=False,
                    action_endpoint=None,
                )
            )
    return tuple(result)


def _project_skill_suggestions(
    project_id: str,
    series: Sequence[Mapping[str, object]],
    project_skill: Mapping[str, object] | None,
) -> tuple[MemoryMaintenanceSuggestion, ...]:
    if not series:
        return ()
    series_features = set().union(*(_features(item) for item in series))
    if project_skill is None:
        return (
            _suggestion(
                project_id=project_id,
                suggestion_type="project_skill_refresh_review",
                risk="medium",
                confidence=1.0,
                target_ids=tuple(
                    sorted(_object_id(item) for item in series)
                ),
                base_revisions=tuple(
                    sorted(_revision(item) for item in series)
                ),
                reason_codes=("project_skill_missing",),
                candidate_supported=False,
                batch_candidate_supported=False,
                action_endpoint=None,
            ),
        )
    skill_features = _tokens(
        json.dumps(
            dict(project_skill),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    missing = series_features - skill_features
    if len(missing) < 3:
        return ()
    revision = project_skill.get("revision")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
    ):
        raise MemoryMaintenancePlanError(
            "project skill revision is invalid"
        )
    skill_id = _text(
        project_skill.get("id"),
        "project skill id",
        160,
    )
    return (
        _suggestion(
            project_id=project_id,
            suggestion_type="project_skill_refresh_review",
            risk="medium",
            confidence=round(
                min(1.0, len(missing) / max(1, len(series_features))),
                6,
            ),
            target_ids=(skill_id,),
            base_revisions=((skill_id, revision, revision),),
            reason_codes=(
                "published_series_not_covered_by_project_skill",
            ),
            candidate_supported=False,
            batch_candidate_supported=False,
            action_endpoint=None,
        ),
    )


def _suggestion(
    *,
    project_id: str,
    suggestion_type: str,
    risk: str,
    confidence: float,
    target_ids: tuple[str, ...],
    base_revisions: tuple[tuple[str, int, int], ...],
    reason_codes: tuple[str, ...],
    candidate_supported: bool,
    batch_candidate_supported: bool,
    action_endpoint: str | None,
) -> MemoryMaintenanceSuggestion:
    identity = {
        "version": MAINTENANCE_PLAN_VERSION,
        "project_id": project_id,
        "type": suggestion_type,
        "targets": target_ids,
        "revisions": base_revisions,
        "reasons": reason_codes,
    }
    digest = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return MemoryMaintenanceSuggestion(
        suggestion_id=f"memory-maintenance-{digest[:24]}",
        suggestion_type=suggestion_type,
        risk=risk,
        confidence=confidence,
        target_ids=target_ids,
        base_revisions=base_revisions,
        reason_codes=reason_codes,
        candidate_supported=candidate_supported,
        batch_candidate_supported=batch_candidate_supported,
        action_endpoint=action_endpoint,
    )


def _objects(
    values: Sequence[Mapping[str, object]],
    field: str,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise MemoryMaintenancePlanError(f"{field} must be a sequence")
    result = tuple(values)
    if len(result) > 10_000 or not all(
        isinstance(item, Mapping) for item in result
    ):
        raise MemoryMaintenancePlanError(f"{field} is invalid")
    identities = [_object_id(item) for item in result]
    if len(identities) != len(set(identities)):
        raise MemoryMaintenancePlanError(
            f"{field} object IDs must be unique"
        )
    for item in result:
        _revision(item)
    return result


def _mappings(
    values: Sequence[Mapping[str, object]],
    field: str,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise MemoryMaintenancePlanError(f"{field} must be a sequence")
    result = tuple(values)
    if len(result) > 10_000 or not all(
        isinstance(item, Mapping) for item in result
    ):
        raise MemoryMaintenancePlanError(f"{field} is invalid")
    return result


def _object_id(item: Mapping[str, object]) -> str:
    return _text(item.get("object_id"), "object_id", 160)


def _revision(
    item: Mapping[str, object],
) -> tuple[str, int, int]:
    object_id = _object_id(item)
    object_revision = item.get("object_revision")
    domain_revision = item.get("revision")
    if (
        not isinstance(object_revision, int)
        or isinstance(object_revision, bool)
        or object_revision < 1
        or not isinstance(domain_revision, int)
        or isinstance(domain_revision, bool)
        or domain_revision < 1
    ):
        raise MemoryMaintenancePlanError(
            "maintenance object revisions are invalid"
        )
    return object_id, object_revision, domain_revision


def _tags(item: Mapping[str, object]) -> frozenset[str]:
    values = item.get("tags")
    if not isinstance(values, list):
        return frozenset()
    return frozenset(
        value.strip().casefold()
        for value in values
        if isinstance(value, str) and value.strip()
    )


def _features(item: Mapping[str, object]) -> set[str]:
    values = [
        item.get("title"),
        item.get("preview"),
        " ".join(sorted(_tags(item))),
    ]
    return set().union(*(_tokens(value) for value in values))


def _tokens(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens = {
        match
        for match in _WORD.findall(normalized)
        if match not in _STOP
    }
    for run in _CJK.findall(normalized):
        tokens.update(
            run[index : index + 2]
            for index in range(len(run) - 1)
            if run[index : index + 2] not in _STOP
        )
    return tokens


def _normalized_preview(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(
        unicodedata.normalize("NFKC", value).casefold().split()
    )


def _score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MemoryMaintenancePlanError(
            "maintenance confidence is invalid"
        )
    score = float(value)
    if not 0.0 <= score <= 1.0:
        raise MemoryMaintenancePlanError(
            "maintenance confidence is invalid"
        )
    return score


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise MemoryMaintenancePlanError(f"{field} must be a string")
    normalized = " ".join(value.split()).strip()
    if not normalized or len(normalized) > limit:
        raise MemoryMaintenancePlanError(f"{field} is invalid")
    return normalized
