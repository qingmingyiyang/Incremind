"""Scenario transfer ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib, json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep

from core.aggregate_repository_factory import (
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
)
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.storage_provider import ObjectStoreRevisionError

from . import http as product_http
from . import memory_hierarchy as product_memory_hierarchy
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.get("/api/rebuild/projects/{project_id:path}/memory-scenario-transfer-preview")
def memory_scenario_transfer_preview(
    project_id: str,
    container: ApiContainerDep,
    scenario_object_id: str,
    target_series_object_id: str | None = None,
) -> dict[str, object]:
    return _memory_scenario_transfer_preview_payload(
        project_id=project_id,
        scenario_object_id=scenario_object_id,
        target_series_object_id=target_series_object_id,
        container=container,
    )


@router.post("/api/rebuild/memory-scenario-transfer-plans")
async def create_memory_scenario_transfer_plan(
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
        return response(400, {"detail": "Scenario transfer plan requires confirmed=true"})
    project_id = body.get("project_id")
    scenario_object_id = body.get("scenario_object_id")
    target_series_object_id = body.get("target_series_object_id")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (project_id, scenario_object_id, target_series_object_id)
    ):
        return response(400, {"detail": "Scenario transfer identifiers are required"})
    try:
        preview = _memory_scenario_transfer_preview_payload(
            project_id=project_id,
            scenario_object_id=scenario_object_id,
            target_series_object_id=target_series_object_id,
            container=container,
        )
    except HTTPException as error:
        return response(error.status_code, {"detail": error.detail})
    selected = preview.get("selected_target")
    scenario = preview.get("scenario")
    source = preview.get("source_series")
    if not all(isinstance(value, Mapping) for value in (selected, scenario, source)):
        return response(409, {"detail": "Scenario transfer preview is incomplete"})
    expected = {
        "scenario_object_revision": body.get("expected_scenario_object_revision"),
        "scenario_revision": body.get("expected_scenario_revision"),
        "source_series_object_revision": body.get("expected_source_series_object_revision"),
        "source_series_revision": body.get("expected_source_series_revision"),
        "target_series_object_revision": body.get("expected_target_series_object_revision"),
        "target_series_revision": body.get("expected_target_series_revision"),
    }
    actual = {
        "scenario_object_revision": scenario.get("object_revision"),
        "scenario_revision": scenario.get("revision"),
        "source_series_object_revision": source.get("object_revision"),
        "source_series_revision": source.get("revision"),
        "target_series_object_revision": selected.get("object_revision"),
        "target_series_revision": selected.get("revision"),
    }
    if expected != actual:
        return response(
            409,
            {
                "detail": "Scenario transfer preview revisions conflicted",
                "current_revisions": actual,
            },
        )
    plan_shape = {
        "project_id": project_id.strip(),
        "scenario_object_id": scenario_object_id.strip(),
        "source_series_object_id": source["object_id"],
        "target_series_object_id": selected["object_id"],
        "input_revisions": actual,
    }
    plan_sha256 = hashlib.sha256(
        json.dumps(
            plan_shape,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    plan_id = f"memory-scenario-transfer-{plan_sha256[:20]}"
    store, _settings = product_repositories._object_store(container.root_dir)
    existing = store.read("memory_scenario_transfer_plans", plan_id)
    if existing is not None:
        return response(
            200,
            {
                **existing,
                "plan_revision": store.revision(
                    "memory_scenario_transfer_plans",
                    plan_id,
                ),
                "replayed": True,
                "writes_performed": False,
            },
        )
    candidate_response = product_memory_hierarchy._create_memory_hierarchy_update_candidate(
        body={
            "project_id": project_id.strip(),
            "layer": "scenario",
            "object_id": scenario_object_id.strip(),
            "expected_object_revision": scenario["object_revision"],
            "expected_domain_revision": scenario["revision"],
            "series_id": selected["id"],
            "atom_ids": list(scenario.get("atom_ids", ())),
            "scenario_ids": [],
            "confirmed": True,
        },
        container=container,
        preserve_scenario_atom_ids=True,
    )
    candidate_payload = json.loads(candidate_response.body.decode("utf-8"))
    if candidate_response.status_code != 200:
        return response(candidate_response.status_code, candidate_payload)
    created_at = datetime.now(timezone.utc).isoformat()
    plan = {
        "schema_version": "1.0.0",
        "plan_id": plan_id,
        **plan_shape,
        "source_series_id": source["id"],
        "target_series_id": selected["id"],
        "scenario_candidate_id": candidate_payload["candidate_id"],
        "status": "scenario_candidate_pending_review",
        "created_at": created_at,
        "updated_at": created_at,
        "content_included": False,
        "network_called": False,
    }
    try:
        store.write(
            "memory_scenario_transfer_plans",
            plan_id,
            plan,
            expected_revision=0,
        )
    except ObjectStoreRevisionError:
        recovered = store.read("memory_scenario_transfer_plans", plan_id)
        if recovered is None:
            return response(409, {"detail": "Scenario transfer plan write conflicted"})
        return response(
            200,
            {
                **recovered,
                "plan_revision": store.revision(
                    "memory_scenario_transfer_plans",
                    plan_id,
                ),
                "replayed": True,
                "writes_performed": False,
            },
        )
    return response(
        200,
        {
            **plan,
            "plan_revision": 1,
            "replayed": False,
            "writes_performed": True,
        },
    )


@router.get("/api/rebuild/memory-scenario-transfer-plans/{plan_id}")
def memory_scenario_transfer_plan_status(
    plan_id: str,
    container: ApiContainerDep,
) -> dict[str, object]:
    clean_plan_id = plan_id.strip()
    if not clean_plan_id:
        raise HTTPException(status_code=400, detail="plan_id is required")
    store, _settings = product_repositories._object_store(container.root_dir)
    plan_record = store.read("memory_scenario_transfer_plans", clean_plan_id)
    if plan_record is None:
        raise HTTPException(status_code=404, detail="Scenario transfer plan not found")
    plan = dict(plan_record)
    preview = _memory_scenario_transfer_preview_payload(
        project_id=str(plan["project_id"]),
        scenario_object_id=str(plan["scenario_object_id"]),
        target_series_object_id=str(plan["target_series_object_id"]),
        container=container,
        allow_completed_transfer=True,
    )
    scenario = preview["scenario"]
    current_series_id = scenario.get("series_id")
    target_series_id = plan.get("target_series_id")
    source_series_id = plan.get("source_series_id")
    candidate = ObjectStoreMemoryCandidateRepository(store).get(
        str(plan["scenario_candidate_id"])
    )
    if current_series_id == target_series_id:
        freshness_items: list[dict[str, object]] = []
        for series_object_id in (
            plan["source_series_object_id"],
            plan["target_series_object_id"],
        ):
            payload = product_memory_hierarchy.memory_series_freshness(
                project_id=str(plan["project_id"]),
                container=container,
                series_object_id=str(series_object_id),
            )
            freshness_items.extend(payload["items"])
        needs_refresh = [
            value for value in freshness_items if bool(value.get("needs_refresh"))
        ]
        status = "series_refresh_required" if needs_refresh else "completed"
    elif current_series_id == source_series_id:
        freshness_items = []
        candidate_status = candidate.get("status") if candidate is not None else "missing"
        if candidate_status == "pending_review":
            status = "scenario_candidate_pending_review"
        elif candidate_status == "promoted":
            status = "scenario_publication_pending"
        else:
            status = "conflicted"
    else:
        freshness_items = []
        status = "conflicted"
    return {
        **plan,
        "plan_revision": store.revision(
            "memory_scenario_transfer_plans",
            clean_plan_id,
        ),
        "status": status,
        "scenario_current_series_id": current_series_id,
        "scenario_candidate_status": (
            candidate.get("status") if candidate is not None else "missing"
        ),
        "series_refresh_items": freshness_items,
        "writes_performed": False,
        "network_called": False,
    }


def _memory_scenario_transfer_preview_payload(
    *,
    project_id: str,
    scenario_object_id: str,
    target_series_object_id: str | None,
    container: ApiContainerDep,
    allow_completed_transfer: bool = False,
) -> dict[str, object]:
    clean_project_id = project_id.strip()
    clean_scenario_object_id = scenario_object_id.strip()
    clean_target_object_id = (
        target_series_object_id.strip()
        if isinstance(target_series_object_id, str)
        else None
    )
    if not clean_project_id or not clean_scenario_object_id:
        raise HTTPException(
            status_code=400,
            detail="project_id and scenario_object_id are required",
        )
    if target_series_object_id is not None and not clean_target_object_id:
        raise HTTPException(status_code=400, detail="target_series_object_id is invalid")
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
            detail="Scenario transfer requires active SQLite authority",
        )
    scenarios = product_memory_hierarchy._memory_hierarchy_layer_options(
        records=resolution.records,
        candidates=candidates,
        project_id=clean_project_id,
        layer="scenario",
    )
    scenario = next(
        (
            value
            for value in scenarios
            if value["object_id"] == clean_scenario_object_id
        ),
        None,
    )
    if scenario is None:
        raise HTTPException(
            status_code=404,
            detail="published Scenario is not in this project",
        )
    series = product_memory_hierarchy._memory_hierarchy_layer_options(
        records=resolution.records,
        candidates=candidates,
        project_id=clean_project_id,
        layer="series_memory",
    )
    current_series_id = scenario.get("series_id")
    source = next(
        (value for value in series if value.get("id") == current_series_id),
        None,
    )
    selected = next(
        (
            value
            for value in series
            if value["object_id"] == clean_target_object_id
        ),
        None,
    )
    if source is None:
        raise HTTPException(
            status_code=409,
            detail="Scenario current Series is not a published project Series",
        )
    if clean_target_object_id is not None and selected is None:
        raise HTTPException(
            status_code=404,
            detail="target Series is not published in this project",
        )
    if (
        selected is not None
        and selected["object_id"] == source["object_id"]
        and not allow_completed_transfer
    ):
        raise HTTPException(status_code=409, detail="Scenario transfer has no changes")
    targets = [
        value for value in series if value["object_id"] != source["object_id"]
    ]
    if selected is not None and selected not in targets and not allow_completed_transfer:
        raise HTTPException(status_code=409, detail="Scenario transfer has no changes")
    related_plans = sorted(
        (
            {
                "plan_id": value.get("plan_id"),
                "source_series_object_id": value.get("source_series_object_id"),
                "target_series_object_id": value.get("target_series_object_id"),
                "scenario_candidate_id": value.get("scenario_candidate_id"),
                "created_at": value.get("created_at"),
            }
            for value in store.list("memory_scenario_transfer_plans")
            if isinstance(value, Mapping)
            and value.get("project_id") == clean_project_id
            and value.get("scenario_object_id") == clean_scenario_object_id
        ),
        key=lambda value: (str(value.get("created_at") or ""), str(value.get("plan_id") or "")),
        reverse=True,
    )[:10]
    return {
        "project_id": clean_project_id,
        "scenario": scenario,
        "source_series": source,
        "target_options": targets,
        "selected_target": selected,
        "existing_plans": related_plans,
        "relationship_diff": (
            {
                "remove_from_series_object_id": source["object_id"],
                "add_to_series_object_id": selected["object_id"],
                "scenario_series_from": source["id"],
                "scenario_series_to": selected["id"],
            }
            if selected is not None
            else None
        ),
        "content_included": False,
        "writes_performed": False,
        "network_called": False,
    }
