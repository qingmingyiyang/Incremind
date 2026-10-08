"""Memory hierarchy ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib, json, re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep

from core.aggregate_repository_factory import (
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
)
from core.memory_core import MemoryCandidateRepositoryError, ObjectStoreMemoryCandidateRepository
from core.product_core.memory_candidate_review import MemoryCandidateReviewError
from core.product_core.memory_maintenance_plan import (
    MemoryMaintenancePlanError,
    build_memory_maintenance_plan,
)
from core.project_skill_core import ProjectSkillRepositoryError

from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.get("/api/rebuild/projects/{project_id:path}/memory-hierarchy-options")
def memory_hierarchy_options(
    project_id: str,
    container: ApiContainerDep,
) -> dict[str, object]:
    clean_project_id = project_id.strip()
    if not clean_project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    _store, _settings, _records, hierarchy = _memory_hierarchy_snapshot(
        project_id=clean_project_id,
        container=container,
        unavailable_detail=(
            "Memory hierarchy options require active SQLite authority"
        ),
    )
    return hierarchy


def _memory_hierarchy_snapshot(
    *,
    project_id: str,
    container: ApiContainerDep,
    unavailable_detail: str,
) -> tuple[Any, Any, Any, dict[str, object]]:
    store, settings = product_repositories._object_store(container.root_dir)
    candidates = ObjectStoreMemoryCandidateRepository(store)
    try:
        resolution = AggregateRepositoryFactory(
            runtime_root=container.root_dir,
            namespace_id=settings.namespace_id,
            json_store=store,
        ).memory_publication_authority_resolution()
    except AggregateRepositoryFactoryError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if resolution.records is None:
        raise HTTPException(
            status_code=409,
            detail=unavailable_detail,
        )
    records = resolution.records
    hierarchy = {
        "project_id": project_id,
        "series": _memory_hierarchy_layer_options(
            records=records,
            candidates=candidates,
            project_id=project_id,
            layer="series_memory",
        ),
        "scenarios": _memory_hierarchy_layer_options(
            records=records,
            candidates=candidates,
            project_id=project_id,
            layer="scenario",
        ),
        "atoms": _memory_hierarchy_layer_options(
            records=records,
            candidates=candidates,
            project_id=project_id,
            layer="atom",
        ),
        "content_included": False,
        "network_called": False,
    }
    return store, settings, records, hierarchy


@router.get("/api/rebuild/projects/{project_id:path}/memory-series-suggestions")
def memory_series_classification_suggestions(
    project_id: str,
    container: ApiContainerDep,
    scenario_id: str | None = None,
) -> dict[str, object]:
    clean_project_id = project_id.strip()
    if not clean_project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    clean_scenario_id = scenario_id.strip() if isinstance(scenario_id, str) else None
    if scenario_id is not None and not clean_scenario_id:
        raise HTTPException(status_code=400, detail="scenario_id is invalid")
    _store, _settings, _records, hierarchy = _memory_hierarchy_snapshot(
        project_id=clean_project_id,
        container=container,
        unavailable_detail=(
            "Memory series suggestions require active SQLite authority"
        ),
    )
    return _memory_series_classification_payload(
        project_id=clean_project_id,
        scenarios=hierarchy["scenarios"],
        series=hierarchy["series"],
        scenario_id=clean_scenario_id,
    )


def _memory_series_classification_payload(
    *,
    project_id: str,
    scenarios: Sequence[Mapping[str, object]],
    series: Sequence[Mapping[str, object]],
    scenario_id: str | None = None,
) -> dict[str, object]:
    selected_scenarios = list(scenarios)
    if scenario_id is not None:
        selected_scenarios = [
            value
            for value in selected_scenarios
            if value["object_id"] == scenario_id
        ]
        if not selected_scenarios:
            raise HTTPException(
                status_code=404,
                detail="published Scenario is not in this project",
            )
    suggestions: list[dict[str, object]] = []
    unresolved: list[dict[str, object]] = []
    for scenario in selected_scenarios:
        ranked = _rank_memory_series_suggestions(
            scenario=scenario,
            series=series,
        )
        if ranked:
            suggestions.append(
                {
                    "scenario_id": scenario["object_id"],
                    "scenario_revision": scenario["revision"],
                    "scenario_object_revision": scenario["object_revision"],
                    "current_series_id": scenario["series_id"],
                    "current_atom_ids": list(scenario["atom_ids"]),
                    "candidates": ranked,
                }
            )
        else:
            unresolved.append(
                {
                    "scenario_id": scenario["object_id"],
                    "current_series_id": scenario["series_id"],
                    "reason": (
                        "当前项目没有其他已发布 Series。"
                        if not any(
                            value.get("id") != scenario.get("series_id")
                            for value in series
                        )
                        else "没有足够的标题、标签或摘要重叠，建议保持当前归类。"
                    ),
                }
            )
    return {
        "project_id": project_id,
        "suggestions": suggestions,
        "unresolved": unresolved,
        "algorithm": {
            "id": "local-series-overlap-v1",
            "minimum_score": 0.12,
            "max_candidates_per_scenario": 3,
        },
        "content_included": False,
        "writes_performed": False,
        "network_called": False,
    }


@router.get("/api/rebuild/projects/{project_id:path}/memory-maintenance-plan")
def memory_maintenance_plan(
    project_id: str,
    container: ApiContainerDep,
) -> dict[str, object]:
    clean_project_id = project_id.strip()
    if not clean_project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    store, settings, records, hierarchy = _memory_hierarchy_snapshot(
        project_id=clean_project_id,
        container=container,
        unavailable_detail=(
            "Memory maintenance plan requires active SQLite authority"
        ),
    )
    classifications = _memory_series_classification_payload(
        project_id=clean_project_id,
        scenarios=hierarchy["scenarios"],
        series=hierarchy["series"],
    )
    freshness = _memory_series_freshness_payload(
        project_id=clean_project_id,
        records=records,
        scenarios=hierarchy["scenarios"],
        series=hierarchy["series"],
    )
    try:
        project_skill = product_repositories._project_skill_repository(
            container.root_dir,
            store,
            settings,
        ).load(clean_project_id)
        plan = build_memory_maintenance_plan(
            project_id=clean_project_id,
            atoms=hierarchy["atoms"],
            scenarios=hierarchy["scenarios"],
            series=hierarchy["series"],
            classification_suggestions=classifications["suggestions"],
            freshness_items=freshness["items"],
            project_skill=project_skill,
        )
    except (
        AggregateRepositoryFactoryError,
        MemoryMaintenancePlanError,
        ProjectSkillRepositoryError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    payload = plan.to_payload()
    payload["candidate_contracts"] = {
        "scenario_reclassification": {
            "preview_endpoint": (
                f"/api/rebuild/projects/{clean_project_id}/"
                "memory-series-suggestions"
            ),
            "candidate_endpoint": (
                "/api/rebuild/memory-hierarchy/"
                "update-candidates/batch"
            ),
        },
        "series_refresh": {
            "preview_endpoint": (
                f"/api/rebuild/projects/{clean_project_id}/"
                "memory-series-freshness"
            ),
            "candidate_endpoint": (
                "/api/rebuild/memory-hierarchy/update-candidates"
            ),
        },
    }
    payload["unsupported_automatic_actions"] = [
        "duplicate_atom_merge_review",
        "fact_replacement_review",
        "series_merge_review",
        "project_skill_refresh_review",
    ]
    return payload


@router.get("/api/rebuild/projects/{project_id:path}/memory-series-freshness")
def memory_series_freshness(
    project_id: str,
    container: ApiContainerDep,
    series_object_id: str | None = None,
) -> dict[str, object]:
    clean_project_id = project_id.strip()
    clean_series_object_id = (
        series_object_id.strip() if isinstance(series_object_id, str) else None
    )
    if not clean_project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    if series_object_id is not None and not clean_series_object_id:
        raise HTTPException(status_code=400, detail="series_object_id is invalid")
    _store, _settings, records, hierarchy = _memory_hierarchy_snapshot(
        project_id=clean_project_id,
        container=container,
        unavailable_detail=(
            "Memory series freshness requires active SQLite authority"
        ),
    )
    return _memory_series_freshness_payload(
        project_id=clean_project_id,
        records=records,
        scenarios=hierarchy["scenarios"],
        series=hierarchy["series"],
        series_object_id=clean_series_object_id,
    )


def _memory_series_freshness_payload(
    *,
    project_id: str,
    records: Any,
    scenarios: Sequence[Mapping[str, object]],
    series: Sequence[Mapping[str, object]],
    series_object_id: str | None = None,
) -> dict[str, object]:
    selected_series = list(series)
    if series_object_id is not None:
        selected_series = [
            value
            for value in selected_series
            if value["object_id"] == series_object_id
        ]
        if not selected_series:
            raise HTTPException(
                status_code=404,
                detail="published Series is not in this project",
            )
    scenario_by_id = {str(value["object_id"]): value for value in scenarios}
    items: list[dict[str, object]] = []
    for current_series in selected_series:
        series_id = str(current_series["id"])
        series_record = records.read(
            "memory_series_memory",
            str(current_series["object_id"]),
        )
        current_overview = (
            series_record.payload.get("overview")
            if series_record is not None
            else current_series["preview"]
        )
        current_ids = tuple(str(value) for value in current_series["scenario_ids"])
        freshness_receipt = records.read(
            "memory_series_freshness_receipts",
            f"{current_series['object_id']}~r{current_series['revision']}",
        )
        snapshot_by_id = {
            str(value.get("scenario_id")): value
            for value in (
                freshness_receipt.payload.get("scenario_revisions", ())
                if freshness_receipt is not None
                else ()
            )
            if isinstance(value, Mapping)
            and isinstance(value.get("scenario_id"), str)
        }
        current_set = set(current_ids)
        assigned_set = {
            scenario_id
            for scenario_id, scenario in scenario_by_id.items()
            if scenario.get("series_id") == series_id
        }
        missing_ids = sorted(current_set - set(scenario_by_id))
        moved_out_ids = sorted(
            scenario_id
            for scenario_id in current_set & set(scenario_by_id)
            if scenario_by_id[scenario_id].get("series_id") != series_id
        )
        missing_from_series_ids = sorted(assigned_set - current_set)
        newer_revision_ids = sorted(
            scenario_id
            for scenario_id in current_set & assigned_set
            if scenario_id in snapshot_by_id
            and (
                scenario_by_id[scenario_id].get("revision")
                != snapshot_by_id[scenario_id].get("revision")
                or scenario_by_id[scenario_id].get("object_revision")
                != snapshot_by_id[scenario_id].get("object_revision")
            )
        )
        recommended_ids = sorted(
            (current_set - set(missing_ids) - set(moved_out_ids)) | assigned_set
        )
        reasons = [
            {
                "code": code,
                "scenario_ids": values,
                "message": message,
            }
            for code, values, message in (
                (
                    "freshness_baseline_missing",
                    [str(current_series["object_id"])]
                    if freshness_receipt is None
                    else [],
                    "Series 尚无 Scenario revision freshness 基线，需要人工确认一次刷新。",
                ),
                (
                    "scenario_missing",
                    missing_ids,
                    "Series 引用的 Scenario 已不在当前项目 publication 中。",
                ),
                (
                    "scenario_moved_out",
                    moved_out_ids,
                    "Series 仍引用已归入其他 Series 的 Scenario。",
                ),
                (
                    "scenario_not_listed",
                    missing_from_series_ids,
                    "Scenario 已归入此 Series，但尚未进入 Series 下钻关系。",
                ),
                (
                    "scenario_revision_newer",
                    newer_revision_ids,
                    "Scenario revision 晚于当前 Series 总览。",
                ),
            )
            if values
        ]
        scenario_summaries = [
            scenario_by_id[value]
            for value in recommended_ids
            if value in scenario_by_id
        ]
        suggested_overview = _local_series_refresh_overview(
            series_title=str(current_series["title"]),
            scenarios=scenario_summaries,
            fallback=str(current_overview),
        )
        items.append(
            {
                "series_id": series_id,
                "series_object_id": current_series["object_id"],
                "series_revision": current_series["revision"],
                "series_object_revision": current_series["object_revision"],
                "current_scenario_ids": list(current_ids),
                "suggested_scenario_ids": recommended_ids,
                "current_overview": current_overview,
                "suggested_overview": suggested_overview,
                "needs_refresh": bool(reasons),
                "reasons": reasons,
                "input_scenario_revisions": [
                    {
                        "scenario_id": value["object_id"],
                        "revision": value["revision"],
                        "object_revision": value["object_revision"],
                    }
                    for value in scenario_summaries
                ],
                "generated_locally": True,
                "freshness_receipt_id": (
                    freshness_receipt.object_id
                    if freshness_receipt is not None
                    else None
                ),
            }
        )
    items.sort(key=lambda value: (str(value["series_id"]), str(value["series_object_id"])))
    return {
        "project_id": project_id,
        "items": items,
        "needs_refresh_count": sum(bool(value["needs_refresh"]) for value in items),
        "content_included": False,
        "writes_performed": False,
        "network_called": False,
    }


def _local_series_refresh_overview(
    *,
    series_title: str,
    scenarios: Sequence[Mapping[str, object]],
    fallback: str,
) -> str:
    summaries = [
        " — ".join(
            value
            for value in (
                str(scenario.get("title") or "").strip(),
                str(scenario.get("preview") or "").strip(),
            )
            if value
        )
        for scenario in scenarios
    ]
    summaries = [value for value in summaries if value]
    if not summaries:
        return fallback.strip()
    return f"{series_title}：" + "；".join(summaries)[:1150]


def _rank_memory_series_suggestions(
    *,
    scenario: Mapping[str, object],
    series: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    scenario_title = _memory_classification_tokens(scenario.get("title"))
    scenario_preview = _memory_classification_tokens(scenario.get("preview"))
    scenario_tags = _memory_classification_tokens(
        " ".join(scenario.get("tags", ()))
    )
    scenario_features = scenario_title | scenario_preview | scenario_tags
    ranked: list[dict[str, object]] = []
    for target in series:
        target_id = target.get("id")
        if not isinstance(target_id, str) or target_id == scenario.get("series_id"):
            continue
        title_matches = sorted(
            scenario_features & _memory_classification_tokens(target.get("title"))
        )
        tag_matches = sorted(
            scenario_features
            & _memory_classification_tokens(" ".join(target.get("tags", ())))
        )
        summary_matches = sorted(
            scenario_features & _memory_classification_tokens(target.get("preview"))
        )
        matched = list(
            dict.fromkeys((*tag_matches, *title_matches, *summary_matches))
        )[:5]
        if not matched:
            continue
        score = min(
            1.0,
            round(
                (0.32 * len(tag_matches))
                + (0.18 * len(title_matches))
                + (0.08 * len(summary_matches)),
                4,
            ),
        )
        if score < 0.12:
            continue
        ranked.append(
            {
                "series_id": target_id,
                "series_object_id": target["object_id"],
                "series_revision": target["revision"],
                "score": score,
                "matched_features": matched,
                "explanation": (
                    f"与 Series 的标题、标签或摘要命中 {len(matched)} 个本地结构化特征："
                    f"{'、'.join(matched)}。"
                ),
            }
        )
    ranked.sort(
        key=lambda value: (
            -float(value["score"]),
            str(value["series_id"]),
            str(value["series_object_id"]),
        )
    )
    return ranked[:3]


_MEMORY_CLASSIFICATION_STOP_WORDS = {
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


def _memory_classification_tokens(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    normalized = " ".join(value.casefold().split())
    tokens = {
        match
        for match in re.findall(r"[a-z0-9][a-z0-9_-]{1,31}", normalized)
        if match not in _MEMORY_CLASSIFICATION_STOP_WORDS
    }
    for run in re.findall(r"[\u3400-\u9fff]{2,24}", normalized):
        tokens.update(
            run[index : index + 2]
            for index in range(len(run) - 1)
            if run[index : index + 2] not in _MEMORY_CLASSIFICATION_STOP_WORDS
        )
    return tokens


def _memory_hierarchy_layer_options(
    *,
    records,
    candidates: ObjectStoreMemoryCandidateRepository,
    project_id: str,
    layer: str,
) -> list[dict[str, object]]:
    collection = {
        "atom": "memory_atoms",
        "scenario": "memory_scenarios",
        "series_memory": "memory_series_memory",
    }[layer]
    publications = {
        str(record.payload.get("published_object_id")): record.payload
        for record in records.list("memory_publications")
        if record.payload.get("layer") == layer
        and record.payload.get("status") == "published"
        and isinstance(record.payload.get("published_object_id"), str)
    }
    options: list[dict[str, object]] = []
    seen: set[str] = set()
    for record in records.list(collection):
        publication = publications.get(record.object_id)
        if publication is None:
            continue
        candidate_id = publication.get("source_candidate_id")
        candidate = candidates.get(candidate_id) if isinstance(candidate_id, str) else None
        if candidate is None or candidate.get("project_id") != project_id:
            continue
        payload = record.payload
        binding_id = (
            payload.get("series_id")
            if layer == "series_memory"
            else record.object_id
        )
        if not isinstance(binding_id, str) or not binding_id.strip() or binding_id in seen:
            continue
        seen.add(binding_id)
        content = (
            payload.get("content")
            if layer == "atom"
            else payload.get("summary")
            if layer == "scenario"
            else payload.get("overview")
        )
        title = (
            payload.get("title")
            if layer == "scenario"
            else payload.get("series_id")
            if layer == "series_memory"
            else None
        )
        options.append(
            {
                "id": binding_id,
                "object_id": record.object_id,
                "title": (
                    title.strip()
                    if isinstance(title, str) and title.strip()
                    else binding_id
                ),
                "preview": (
                    " ".join(content.split())[:180]
                    if isinstance(content, str)
                    else ""
                ),
                "revision": payload.get("revision"),
                "object_revision": record.revision,
                "updated_at": payload.get("updated_at"),
                "status": "published",
                "series_id": payload.get("series_id") if layer == "scenario" else None,
                "atom_ids": (
                    list(payload.get("atom_ids", ()))
                    if layer == "scenario" and isinstance(payload.get("atom_ids"), list)
                    else []
                ),
                "scenario_ids": (
                    list(payload.get("scenario_ids", ()))
                    if layer == "series_memory"
                    and isinstance(payload.get("scenario_ids"), list)
                    else []
                ),
                "tags": (
                    sorted(
                        {
                            value.strip()
                            for value in payload.get("tags", ())
                            if isinstance(value, str) and value.strip()
                        }
                    )[:12]
                    if isinstance(payload.get("tags"), list)
                    else []
                ),
                "source_ref_count": len(
                    payload.get("source_refs")
                    if isinstance(payload.get("source_refs"), list)
                    else ()
                ),
            }
        )
    options.sort(key=lambda value: (str(value["title"]), str(value["id"])))
    return options


@router.post("/api/rebuild/memory-hierarchy/update-candidates")
async def memory_hierarchy_update_candidate(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    return _create_memory_hierarchy_update_candidate(
        body=await product_http._json_body(request),
        container=container,
    )


def _create_memory_hierarchy_update_candidate(
    *,
    body: Mapping[str, Any],
    container: ApiContainerDep,
    preserve_scenario_atom_ids: bool = False,
) -> JSONResponse:
    def response(status_code: int, payload: Mapping[str, Any]) -> JSONResponse:
        return product_http._json_response(
            status_code,
            payload,
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )

    project_id = body.get("project_id")
    layer = body.get("layer")
    object_id = body.get("object_id")
    expected_object_revision = body.get("expected_object_revision")
    expected_domain_revision = body.get("expected_domain_revision")
    if body.get("confirmed") is not True:
        return response(400, {"detail": "hierarchy update requires confirmed=true"})
    if not isinstance(project_id, str) or not project_id.strip():
        return response(400, {"detail": "project_id is required"})
    if layer not in {"scenario", "series_memory"}:
        return response(400, {"detail": "hierarchy update layer is invalid"})
    if not isinstance(object_id, str) or not object_id.strip():
        return response(400, {"detail": "object_id is required"})
    if (
        not isinstance(expected_object_revision, int)
        or isinstance(expected_object_revision, bool)
        or expected_object_revision < 1
        or not isinstance(expected_domain_revision, int)
        or isinstance(expected_domain_revision, bool)
        or expected_domain_revision < 1
    ):
        return response(400, {"detail": "hierarchy update revisions are invalid"})

    def clean_ids(field: str) -> tuple[str, ...]:
        values = body.get(field, [])
        if (
            not isinstance(values, list)
            or not all(isinstance(value, str) and value.strip() for value in values)
        ):
            raise MemoryCandidateReviewError(f"{field} must be a string list")
        return tuple(dict.fromkeys(value.strip() for value in values))

    try:
        atom_ids = clean_ids("atom_ids")
        scenario_ids = clean_ids("scenario_ids")
    except MemoryCandidateReviewError as error:
        return response(400, {"detail": str(error)})
    series_id = body.get("series_id")
    if series_id is not None and (not isinstance(series_id, str) or not series_id.strip()):
        return response(400, {"detail": "series_id is invalid"})
    clean_series_id = series_id.strip() if isinstance(series_id, str) else None
    proposed_overview = body.get("proposed_overview")
    if layer == "scenario" and proposed_overview is not None:
        return response(400, {"detail": "Scenario hierarchy update cannot replace overview"})
    if proposed_overview is not None and (
        not isinstance(proposed_overview, str)
        or not proposed_overview.strip()
        or len(proposed_overview.strip()) > 8000
    ):
        return response(400, {"detail": "proposed_overview is invalid"})

    store, settings = product_repositories._object_store(container.root_dir)
    candidates = ObjectStoreMemoryCandidateRepository(store)
    try:
        resolution = AggregateRepositoryFactory(
            runtime_root=container.root_dir,
            namespace_id=settings.namespace_id,
            json_store=store,
        ).memory_publication_authority_resolution()
    except AggregateRepositoryFactoryError as error:
        return response(409, {"detail": str(error)})
    if resolution.records is None:
        return response(
            409,
            {"detail": "Memory hierarchy update requires active SQLite authority"},
        )
    records = resolution.records
    collection = "memory_scenarios" if layer == "scenario" else "memory_series_memory"
    current = records.read(collection, object_id)
    if current is None:
        return response(404, {"detail": "published Memory object not found"})
    available = _memory_hierarchy_layer_options(
        records=records,
        candidates=candidates,
        project_id=project_id.strip(),
        layer=layer,
    )
    if not any(value["object_id"] == object_id for value in available):
        return response(404, {"detail": "published Memory object is not in this project"})
    if (
        preserve_scenario_atom_ids
        and layer == "scenario"
        and tuple(current.payload.get("atom_ids", ())) != atom_ids
    ):
        return response(
            409,
            {"detail": "batch current Atom bindings conflicted"},
        )
    domain_revision = current.payload.get("revision")
    if (
        current.revision != expected_object_revision
        or domain_revision != expected_domain_revision
    ):
        return response(
            409,
            {
                "detail": "published Memory revision conflicted",
                "current_object_revision": current.revision,
                "current_domain_revision": domain_revision,
            },
        )
    candidate_shape = {
        "project_id": project_id.strip(),
        "target_layer": layer,
    }
    try:
        _validate_memory_hierarchy_bindings(
            records=records,
            candidates=candidates,
            candidate=candidate_shape,
            target_layer=layer,
            series_id=clean_series_id,
            atom_ids=atom_ids,
            scenario_ids=scenario_ids,
        )
    except MemoryCandidateReviewError as error:
        return response(400, {"detail": str(error)})
    proposed = dict(current.payload)
    if layer == "scenario":
        previous_bindings = (
            current.payload.get("series_id"),
            tuple(current.payload.get("atom_ids", ())),
        )
        next_bindings = (clean_series_id, atom_ids)
        proposed["series_id"] = clean_series_id
        proposed["atom_ids"] = list(atom_ids)
        proposed_content = current.payload.get("summary")
    else:
        next_overview = (
            proposed_overview.strip()
            if isinstance(proposed_overview, str)
            else current.payload.get("overview")
        )
        previous_bindings = (
            tuple(current.payload.get("scenario_ids", ())),
            current.payload.get("overview"),
        )
        next_bindings = (scenario_ids, next_overview)
        proposed["scenario_ids"] = list(scenario_ids)
        proposed["overview"] = next_overview
        proposed_content = next_overview
    if previous_bindings == next_bindings:
        return response(409, {"detail": "hierarchy update has no changes"})
    proposed["revision"] = expected_domain_revision + 1
    evidence_payload = {
        "project_id": project_id.strip(),
        "layer": layer,
        "object_id": object_id,
        "expected_object_revision": expected_object_revision,
        "base_domain_revision": expected_domain_revision,
        "payload": proposed,
    }
    payload_sha256 = hashlib.sha256(
        json.dumps(
            evidence_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    candidate_id = f"memory-candidate-hierarchy-{payload_sha256[:20]}"
    existing = candidates.get(candidate_id)
    if existing is None:
        source_refs = current.payload.get("source_refs")
        if not isinstance(source_refs, list) or not source_refs:
            return response(409, {"detail": "published Memory source evidence is unavailable"})
        created_at = datetime.now(timezone.utc).isoformat()
        candidate = {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "project_id": project_id.strip(),
            "target_layer": layer,
            "candidate_type": "other",
            "status": "pending_review",
            "proposed_content": proposed_content,
            "source_refs": [dict(value) for value in source_refs],
            "evidence_refs": [dict(value) for value in source_refs],
            "provenance": {
                "model_result_id": None,
                "model_request_id": None,
                "recall_result_id": None,
                "document_id": None,
                "document_revision": None,
                "source_content_read_id": f"memory-hierarchy:{layer}:{object_id}:r{expected_domain_revision}",
                "input_refs": [
                    {
                        "kind": "source",
                        "object_id": source_refs[0]["source_id"],
                        "uri": (
                            f"crp://{settings.namespace_id}/sources/"
                            f"{source_refs[0]['source_id']}.json"
                        ),
                    },
                    {
                        "kind": "source_content_read",
                        "object_id": (
                            f"memory-hierarchy:{layer}:{object_id}:"
                            f"r{expected_domain_revision}"
                        ),
                        "uri": (
                            f"crp://{settings.namespace_id}/memory/"
                            f"{layer}/{object_id}.json#revision={expected_domain_revision}"
                        ),
                    }
                ],
            },
            "hierarchy_update": {
                "schema_version": "1.0.0",
                "layer": layer,
                "object_id": object_id,
                "expected_object_revision": expected_object_revision,
                "base_domain_revision": expected_domain_revision,
                "payload_sha256": payload_sha256,
                "authority_identity": "sqlite:structured-records-v1",
                "proposed": proposed,
            },
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": "用户修改已发布 Memory 层级关系，必须重新审核和发布。",
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": created_at,
            "updated_at": created_at,
        }
        try:
            candidates.save(candidate)
        except MemoryCandidateRepositoryError as error:
            return response(409, {"detail": str(error)})
        existing = candidate
    return response(
        200,
        {
            "status": existing.get("status"),
            "candidate_id": candidate_id,
            "candidate_revision": store.revision("memory_candidates", candidate_id),
            "layer": layer,
            "object_id": object_id,
            "base_domain_revision": expected_domain_revision,
            "proposed_domain_revision": expected_domain_revision + 1,
            "long_term_memory_written": False,
            "network_called": False,
        },
    )


@router.post("/api/rebuild/memory-hierarchy/update-candidates/batch")
async def memory_hierarchy_update_candidate_batch(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await product_http._json_body(request)

    def response(status_code: int, payload: Mapping[str, Any]) -> JSONResponse:
        return product_http._json_response(
            status_code,
            payload,
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )

    if body.get("confirmed") is not True:
        return response(400, {"detail": "batch hierarchy update requires confirmed=true"})
    project_id = body.get("project_id")
    items = body.get("items")
    if not isinstance(project_id, str) or not project_id.strip():
        return response(400, {"detail": "project_id is required"})
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        return response(400, {"detail": "items must contain between 1 and 50 entries"})
    if not all(isinstance(value, Mapping) for value in items):
        return response(400, {"detail": "each batch item must be an object"})
    scenario_ids = [value.get("scenario_id") for value in items]
    clean_scenario_ids = [
        value.strip() for value in scenario_ids if isinstance(value, str)
    ]
    if (
        len(clean_scenario_ids) != len(scenario_ids)
        or not all(clean_scenario_ids)
        or len(set(clean_scenario_ids)) != len(clean_scenario_ids)
    ):
        return response(
            400,
            {"detail": "batch scenario_id values must be non-empty and unique"},
        )

    results: list[dict[str, object]] = []
    for item in items:
        scenario_id = str(item["scenario_id"]).strip()
        result = _create_memory_hierarchy_update_candidate(
            body={
                "project_id": project_id.strip(),
                "layer": "scenario",
                "object_id": scenario_id,
                "expected_object_revision": item.get("expected_object_revision"),
                "expected_domain_revision": item.get("expected_domain_revision"),
                "series_id": item.get("target_series_id"),
                "atom_ids": item.get("current_atom_ids"),
                "scenario_ids": [],
                "confirmed": True,
            },
            container=container,
            preserve_scenario_atom_ids=True,
        )
        payload = json.loads(result.body.decode("utf-8"))
        results.append(
            {
                "scenario_id": scenario_id,
                "status_code": result.status_code,
                "status": (
                    "candidate_created"
                    if result.status_code == 200
                    else "rejected"
                ),
                "candidate_id": payload.get("candidate_id"),
                "detail": payload.get("detail"),
                "current_object_revision": payload.get("current_object_revision"),
                "current_domain_revision": payload.get("current_domain_revision"),
            }
        )
    succeeded = sum(value["status_code"] == 200 for value in results)
    return response(
        200,
        {
            "status": (
                "completed"
                if succeeded == len(results)
                else "failed"
                if succeeded == 0
                else "partially_completed"
            ),
            "project_id": project_id.strip(),
            "requested_count": len(results),
            "succeeded_count": succeeded,
            "failed_count": len(results) - succeeded,
            "results": results,
            "long_term_memory_written": False,
            "staging_written": False,
            "review_performed": False,
            "network_called": False,
        },
    )


def _validate_memory_hierarchy_bindings(
    *,
    records,
    candidates: ObjectStoreMemoryCandidateRepository,
    candidate: Mapping[str, object],
    target_layer: str,
    series_id: str | None,
    atom_ids: Sequence[str],
    scenario_ids: Sequence[str],
) -> None:
    project_id = candidate.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise MemoryCandidateReviewError("Memory Candidate project_id is invalid")
    hierarchy_update = candidate.get("hierarchy_update")
    proposed = (
        hierarchy_update.get("proposed")
        if isinstance(hierarchy_update, Mapping)
        and isinstance(hierarchy_update.get("proposed"), Mapping)
        else None
    )
    if target_layer == "scenario":
        if proposed is not None and (
            series_id != proposed.get("series_id")
            or tuple(atom_ids) != tuple(proposed.get("atom_ids", ()))
        ):
            raise MemoryCandidateReviewError(
                "Scenario review bindings do not match the pending hierarchy update"
            )
        if series_id is None:
            raise MemoryCandidateReviewError(
                "Scenario review requires a project Series binding"
            )
        current_series = {
            str(value["id"])
            for value in _memory_hierarchy_layer_options(
                records=records,
                candidates=candidates,
                project_id=project_id,
                layer="series_memory",
            )
        }
        current_series.update(
            _same_import_batch_binding_ids(
                candidates=candidates,
                candidate=candidate,
                layer="series_memory",
            )
        )
        if series_id != project_id and series_id not in current_series:
            raise MemoryCandidateReviewError(
                "Scenario Series binding is not available in this project"
            )
        current_atoms = {
            str(value["id"])
            for value in _memory_hierarchy_layer_options(
                records=records,
                candidates=candidates,
                project_id=project_id,
                layer="atom",
            )
        }
        if any(atom_id not in current_atoms for atom_id in atom_ids):
            raise MemoryCandidateReviewError(
                "Scenario Atom binding is not published in this project"
            )
    elif target_layer == "series_memory":
        if proposed is not None and tuple(scenario_ids) != tuple(
            proposed.get("scenario_ids", ())
        ):
            raise MemoryCandidateReviewError(
                "Series review bindings do not match the pending hierarchy update"
            )
        current_scenarios = {
            str(value["id"])
            for value in _memory_hierarchy_layer_options(
                records=records,
                candidates=candidates,
                project_id=project_id,
                layer="scenario",
            )
        }
        if any(scenario_id not in current_scenarios for scenario_id in scenario_ids):
            raise MemoryCandidateReviewError(
                "Series Scenario binding is not published in this project"
            )


def _same_import_batch_binding_ids(
    *,
    candidates: ObjectStoreMemoryCandidateRepository,
    candidate: Mapping[str, object],
    layer: str,
) -> set[str]:
    import_batch_id = candidate.get("import_batch_id")
    project_id = candidate.get("project_id")
    if (
        not isinstance(import_batch_id, str)
        or not import_batch_id
        or not isinstance(project_id, str)
        or not project_id
    ):
        return set()
    binding_ids: set[str] = set()
    for sibling in candidates.list_by_project(project_id):
        if (
            sibling.get("import_batch_id") != import_batch_id
            or sibling.get("target_layer") != layer
            or sibling.get("status") not in {"pending_review", "promoted"}
        ):
            continue
        portable_object_id = sibling.get("portable_object_id")
        if isinstance(portable_object_id, str) and portable_object_id:
            binding_ids.add(portable_object_id)
    return binding_ids
