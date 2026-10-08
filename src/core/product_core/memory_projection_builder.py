from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from core.product_core.memory_projection_contract import (
    AtomProjectionRef,
    AuthorityObjectRef,
    MemoryRetrievalProjection,
    ProjectSkillProjectionRef,
    R0SeriesRouterItem,
    R1SeriesDigestItem,
    SafeSourceRef,
    ScenarioProjectionRef,
)


ELIGIBLE_TRUST_STATUSES = frozenset(
    {"trusted", "user_confirmed", "system_generated"}
)
MAX_R0_DESCRIPTION_CHARS = 320
MAX_R1_SUMMARY_CHARS = 1600
MAX_SCENARIOS_PER_SERIES = 8
MAX_ATOMS_PER_SERIES = 16
MAX_SKILLS_PER_PROJECT = 8
MAX_KEYWORDS_PER_SERIES = 16
MAX_SOURCE_REFS_PER_SERIES = 24
MAX_TITLE_CHARS = 160
MAX_PREVIEW_CHARS = 320
MAX_PURPOSE_CHARS = 240
MAX_LOCATOR_CHARS = 320
_SPACE_PATTERN = re.compile(r"\s+")


class MemoryProjectionBuildError(ValueError):
    """Raised when a deterministic, authority-safe projection cannot be built."""


def build_r0_r1_memory_projection(
    *,
    project_id: str,
    authority_identity: str,
    series_memories: Sequence[Mapping[str, object]],
    scenarios: Sequence[Mapping[str, object]],
    atoms: Sequence[Mapping[str, object]],
    project_skills: Sequence[Mapping[str, object]],
    generated_at: str,
) -> MemoryRetrievalProjection:
    project_id = _required_text(project_id, "project_id")
    authority_identity = _required_text(
        authority_identity,
        "authority_identity",
    )
    generated_at = _required_datetime(generated_at, "generated_at")

    series_by_id = _eligible_series(project_id, series_memories)
    scenarios_by_series = _eligible_scenarios(project_id, scenarios, series_by_id)
    referenced_atom_ids = {
        atom_id
        for series_scenarios in scenarios_by_series.values()
        for scenario in series_scenarios
        for atom_id in _string_list(scenario.get("atom_ids"))
    }
    atoms_by_id = _eligible_atoms(atoms, referenced_atom_ids)
    skills = (
        _eligible_project_skills(project_id, project_skills)
        if series_by_id
        else ()
    )

    authority_ref_by_key: dict[tuple[str, str], AuthorityObjectRef] = {}
    skill_refs = tuple(_skill_projection_ref(skill) for skill in skills)
    for skill in skills:
        ref = _authority_ref("project_skill", skill)
        authority_ref_by_key[(ref.object_type, ref.object_id)] = ref

    prepared: list[
        tuple[
            Mapping[str, object],
            tuple[Mapping[str, object], ...],
            tuple[Mapping[str, object], ...],
            tuple[AuthorityObjectRef, ...],
        ]
    ] = []
    for series_id, series in sorted(series_by_id.items()):
        series_scenarios = tuple(scenarios_by_series.get(series_id, ()))
        series_atoms = _atoms_for_scenarios(series_scenarios, atoms_by_id)
        refs = [_authority_ref("series_memory", series)]
        refs.extend(_authority_ref("scenario", item) for item in series_scenarios)
        refs.extend(_authority_ref("atom", item) for item in series_atoms)
        refs.extend(_authority_ref("project_skill", item) for item in skills)
        item_authority_refs = _sorted_authority_refs(refs)
        for ref in item_authority_refs:
            authority_ref_by_key[(ref.object_type, ref.object_id)] = ref
        prepared.append(
            (
                series,
                series_scenarios,
                series_atoms,
                item_authority_refs,
            )
        )

    derived_from = _sorted_authority_refs(authority_ref_by_key.values())
    fingerprint = authority_fingerprint(authority_identity, derived_from)

    r0_items: list[R0SeriesRouterItem] = []
    r1_items: list[R1SeriesDigestItem] = []
    for series, series_scenarios, series_atoms, authority_refs in prepared:
        series_id = _required_mapping_text(series, "series_id")
        series_memory_id = _required_mapping_text(series, "id")
        overview = _bounded_text(
            _required_mapping_text(series, "overview"),
            MAX_R1_SUMMARY_CHARS,
        )
        source_refs = _safe_source_refs(
            series,
            *series_scenarios,
            *series_atoms,
            limit=MAX_SOURCE_REFS_PER_SERIES,
        )
        keywords = _series_keywords(series_scenarios, series_atoms)
        r0_title = _bounded_text(
            _optional_text(series.get("title")) or series_id,
            MAX_TITLE_CHARS,
        )
        r0_description = _bounded_text(overview, MAX_R0_DESCRIPTION_CHARS)
        r0_items.append(
            R0SeriesRouterItem(
                projection_id=_projection_id(
                    "r0_series_router",
                    project_id,
                    series_id,
                    fingerprint,
                ),
                project_id=project_id,
                series_id=series_id,
                series_memory_id=series_memory_id,
                authority_identity=authority_identity,
                authority_fingerprint=fingerprint,
                generated_at=generated_at,
                title=r0_title,
                description=r0_description,
                keywords=keywords,
                source_refs=source_refs,
                derived_from=authority_refs,
                content_length=_text_length(
                    r0_title,
                    r0_description,
                    *keywords,
                ),
            )
        )

        scenario_refs = tuple(
            ScenarioProjectionRef(
                scenario_id=_required_mapping_text(scenario, "id"),
                revision=_required_revision(scenario),
                title=_bounded_text(
                    _required_mapping_text(scenario, "title"),
                    MAX_TITLE_CHARS,
                ),
                summary_preview=_bounded_text(
                    _required_mapping_text(scenario, "summary"),
                    MAX_PREVIEW_CHARS,
                ),
            )
            for scenario in series_scenarios
        )
        atom_refs = tuple(
            AtomProjectionRef(
                atom_id=_required_mapping_text(atom, "id"),
                revision=_required_revision(atom),
                atom_type=_required_mapping_text(atom, "atom_type"),
                content_preview=_bounded_text(
                    _required_mapping_text(atom, "content"),
                    MAX_PREVIEW_CHARS,
                ),
            )
            for atom in series_atoms
        )
        r1_items.append(
            R1SeriesDigestItem(
                projection_id=_projection_id(
                    "r1_series_digest",
                    project_id,
                    series_id,
                    fingerprint,
                ),
                project_id=project_id,
                series_id=series_id,
                series_memory_id=series_memory_id,
                authority_identity=authority_identity,
                authority_fingerprint=fingerprint,
                generated_at=generated_at,
                summary=overview,
                scenario_refs=scenario_refs,
                atom_refs=atom_refs,
                skill_refs=skill_refs,
                source_refs=source_refs,
                derived_from=authority_refs,
                content_length=_text_length(
                    overview,
                    *(
                        text
                        for ref in scenario_refs
                        for text in (ref.title, ref.summary_preview)
                    ),
                    *(ref.content_preview for ref in atom_refs),
                    *(
                        text
                        for ref in skill_refs
                        for text in (ref.name, ref.purpose_preview)
                    ),
                ),
            )
        )

    status = "ready" if r0_items else "empty"
    return MemoryRetrievalProjection(
        project_id=project_id,
        authority_identity=authority_identity,
        authority_fingerprint=fingerprint,
        generated_at=generated_at,
        status=status,
        derived_from=derived_from,
        project_skill_refs=skill_refs if r0_items else (),
        r0_items=tuple(r0_items),
        r1_items=tuple(r1_items),
    )


def projection_matches_authority(
    projection: MemoryRetrievalProjection,
    *,
    project_id: str,
    authority_identity: str,
    series_memories: Sequence[Mapping[str, object]],
    scenarios: Sequence[Mapping[str, object]],
    atoms: Sequence[Mapping[str, object]],
    project_skills: Sequence[Mapping[str, object]],
) -> bool:
    current = build_r0_r1_memory_projection(
        project_id=project_id,
        authority_identity=authority_identity,
        series_memories=series_memories,
        scenarios=scenarios,
        atoms=atoms,
        project_skills=project_skills,
        generated_at=projection.generated_at,
    )
    return (
        projection.project_id == current.project_id
        and projection.authority_identity == current.authority_identity
        and projection.authority_fingerprint == current.authority_fingerprint
    )


def authority_fingerprint(
    authority_identity: str,
    refs: Sequence[AuthorityObjectRef],
) -> str:
    identity = _required_text(authority_identity, "authority_identity")
    canonical = {
        "authority_identity": identity,
        "objects": [
            ref.to_payload()
            for ref in _sorted_authority_refs(refs)
        ],
    }
    return _sha256_json(canonical)


def _eligible_series(
    project_id: str,
    values: Sequence[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    eligible: dict[str, Mapping[str, object]] = {}
    seen_ids: set[str] = set()
    for value in values:
        object_id = _required_mapping_text(value, "id")
        _reject_duplicate(seen_ids, object_id, "series memory")
        if not _eligible_memory(value):
            continue
        if project_id not in _string_list(value.get("project_ids")):
            continue
        series_id = _required_mapping_text(value, "series_id")
        if series_id in eligible:
            raise MemoryProjectionBuildError(
                f"multiple current series memories for series_id {series_id!r}"
            )
        eligible[series_id] = value
    return eligible


def _eligible_scenarios(
    project_id: str,
    values: Sequence[Mapping[str, object]],
    series_by_id: Mapping[str, Mapping[str, object]],
) -> dict[str, tuple[Mapping[str, object], ...]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    seen_ids: set[str] = set()
    for value in values:
        object_id = _required_mapping_text(value, "id")
        _reject_duplicate(seen_ids, object_id, "scenario")
        if not _eligible_memory(value):
            continue
        if value.get("project_id") != project_id:
            continue
        series_id = _optional_text(value.get("series_id"))
        if not series_id or series_id not in series_by_id:
            continue
        allowed_ids = set(
            _string_list(series_by_id[series_id].get("scenario_ids"))
        )
        if object_id not in allowed_ids:
            continue
        grouped.setdefault(series_id, []).append(value)
    return {
        series_id: tuple(
            sorted(
                items,
                key=lambda item: _required_mapping_text(item, "id"),
            )[:MAX_SCENARIOS_PER_SERIES]
        )
        for series_id, items in grouped.items()
    }


def _eligible_atoms(
    values: Sequence[Mapping[str, object]],
    referenced_ids: set[str],
) -> dict[str, Mapping[str, object]]:
    eligible: dict[str, Mapping[str, object]] = {}
    seen_ids: set[str] = set()
    for value in values:
        object_id = _required_mapping_text(value, "id")
        _reject_duplicate(seen_ids, object_id, "atom")
        if object_id not in referenced_ids:
            continue
        if value.get("trust_status") not in ELIGIBLE_TRUST_STATUSES:
            continue
        eligible[object_id] = value
    return eligible


def _eligible_project_skills(
    project_id: str,
    values: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    eligible: list[Mapping[str, object]] = []
    seen_ids: set[str] = set()
    for value in values:
        object_id = _required_mapping_text(value, "id")
        _reject_duplicate(seen_ids, object_id, "project skill")
        if value.get("project_id") != project_id:
            continue
        if value.get("status") != "active":
            continue
        if value.get("trust_status") not in ELIGIBLE_TRUST_STATUSES:
            continue
        conflict = value.get("conflict")
        if not isinstance(conflict, Mapping) or conflict.get("status") != "none":
            continue
        eligible.append(value)
    return tuple(
        sorted(
            eligible,
            key=lambda item: _required_mapping_text(item, "id"),
        )[:MAX_SKILLS_PER_PROJECT]
    )


def _eligible_memory(value: Mapping[str, object]) -> bool:
    return (
        value.get("stale") is False
        and value.get("trust_status") in ELIGIBLE_TRUST_STATUSES
    )


def _atoms_for_scenarios(
    scenarios: Sequence[Mapping[str, object]],
    atoms_by_id: Mapping[str, Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    atom_ids = sorted(
        {
            atom_id
            for scenario in scenarios
            for atom_id in _string_list(scenario.get("atom_ids"))
            if atom_id in atoms_by_id
        }
    )
    return tuple(atoms_by_id[atom_id] for atom_id in atom_ids[:MAX_ATOMS_PER_SERIES])


def _authority_ref(
    object_type: str,
    value: Mapping[str, object],
) -> AuthorityObjectRef:
    return AuthorityObjectRef(
        object_type=object_type,
        object_id=_required_mapping_text(value, "id"),
        revision=_required_revision(value),
        content_hash=_sha256_json(value),
    )


def _skill_projection_ref(
    skill: Mapping[str, object],
) -> ProjectSkillProjectionRef:
    return ProjectSkillProjectionRef(
        skill_id=_required_mapping_text(skill, "id"),
        revision=_required_revision(skill),
        name=_bounded_text(
            _required_mapping_text(skill, "name"),
            MAX_TITLE_CHARS,
        ),
        purpose_preview=_bounded_text(
            _required_mapping_text(skill, "purpose"),
            MAX_PURPOSE_CHARS,
        ),
    )


def _safe_source_refs(
    *values: Mapping[str, object],
    limit: int,
) -> tuple[SafeSourceRef, ...]:
    refs: dict[tuple[str, str], SafeSourceRef] = {}
    for value in values:
        raw_refs = value.get("source_refs")
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, (str, bytes)):
            continue
        for raw_ref in raw_refs:
            if not isinstance(raw_ref, Mapping):
                continue
            source_id = _optional_text(raw_ref.get("source_id"))
            locator = _optional_text(raw_ref.get("locator"))
            if not source_id or not locator:
                continue
            safe_ref = SafeSourceRef(
                source_id=source_id,
                locator=_bounded_text(locator, MAX_LOCATOR_CHARS),
            )
            refs[(safe_ref.source_id, safe_ref.locator)] = safe_ref
    return tuple(refs[key] for key in sorted(refs)[:limit])


def _series_keywords(
    scenarios: Sequence[Mapping[str, object]],
    atoms: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    keywords = {
        _bounded_text(value, 64)
        for item in (*scenarios, *atoms)
        for value in _string_list(item.get("tags"))
        if _normalize_text(value)
    }
    return tuple(sorted(keywords)[:MAX_KEYWORDS_PER_SERIES])


def _sorted_authority_refs(
    refs: Iterable[AuthorityObjectRef],
) -> tuple[AuthorityObjectRef, ...]:
    by_key: dict[tuple[str, str], AuthorityObjectRef] = {}
    for ref in refs:
        key = (ref.object_type, ref.object_id)
        existing = by_key.get(key)
        if existing is not None and existing != ref:
            raise MemoryProjectionBuildError(
                f"conflicting authority refs for {ref.object_type}:{ref.object_id}"
            )
        by_key[key] = ref
    return tuple(
        by_key[key]
        for key in sorted(by_key)
    )


def _projection_id(
    projection_type: str,
    project_id: str,
    series_id: str,
    fingerprint: str,
) -> str:
    digest = _sha256_json(
        {
            "projection_type": projection_type,
            "project_id": project_id,
            "series_id": series_id,
            "authority_fingerprint": fingerprint,
        }
    )
    return f"{projection_type}:{digest[:32]}"


def _sha256_json(value: object) -> str:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise MemoryProjectionBuildError(
            "authority snapshot must be canonical JSON"
        ) from error
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _required_mapping_text(
    value: Mapping[str, object],
    key: str,
) -> str:
    return _required_text(value.get(key), key)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise MemoryProjectionBuildError(f"{field} must be a string")
    normalized = _normalize_text(value)
    if not normalized:
        raise MemoryProjectionBuildError(f"{field} is required")
    return normalized


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _normalize_text(value)
    return normalized or None


def _required_datetime(value: object, field: str) -> str:
    normalized = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise MemoryProjectionBuildError(
            f"{field} must be an ISO 8601 date-time"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MemoryProjectionBuildError(
            f"{field} must include an explicit timezone"
        )
    return normalized


def _normalize_text(value: str) -> str:
    return _SPACE_PATTERN.sub(" ", value).strip()


def _bounded_text(value: str, limit: int) -> str:
    normalized = _normalize_text(value)
    if len(normalized) <= limit:
        return normalized
    if limit <= 1:
        return normalized[:limit]
    return f"{normalized[: limit - 1].rstrip()}…"


def _required_revision(value: Mapping[str, object]) -> int:
    revision = value.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise MemoryProjectionBuildError("revision must be a positive integer")
    return revision


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _reject_duplicate(
    seen: set[str],
    object_id: str,
    label: str,
) -> None:
    if object_id in seen:
        raise MemoryProjectionBuildError(
            f"duplicate {label} id {object_id!r}"
        )
    seen.add(object_id)


def _text_length(*values: str) -> int:
    return sum(len(value) for value in values)
