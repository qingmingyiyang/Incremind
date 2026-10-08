"""Project brain ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep

from core.aggregate_repository_factory import (
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
)
from core.memory_core import ObjectStoreMemoryStore, SQLiteMemoryReader
from core.product_core.memory_projection_authority import CurrentMemoryProjectionAuthority
from core.product_core.memory_projection_observability import (
    memory_projection_diagnostics_payload,
    memory_retrieval_settings_payload,
)
from core.product_core.memory_projection_repository import ObjectStoreMemoryProjectionRepository
from core.product_core.memory_retrieval_performance import (
    MAX_SAMPLES as MAX_MEMORY_RETRIEVAL_PERFORMANCE_SAMPLES,
    aggregate_memory_retrieval_performance,
)
from core.product_core.progressive_memory_retrieval import (
    ProgressiveMemoryRetrievalError,
    plan_progressive_memory_retrieval,
    serialize_progressive_retrieval_plan,
)
from core.product_core.project_brain_drill import (
    GetDailyConversationScenario,
    GetMemoryDelta,
    GetProjectBrainLayerDrill,
    ProjectBrainDrillError,
    serialize_daily_conversation_scenario,
    serialize_drill_result,
    serialize_memory_delta,
)
from core.product_core.project_brain_overview import (
    GetProjectBrainOverview,
    ProjectBrainOverviewError,
    serialize_project_brain_overview,
)
from core.product_core.project_brain_projection_status import get_project_brain_projection_status

from . import document_visibility as product_document_visibility
from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.get("/api/rebuild/project-brain")
def project_brain_overview(request: Request, container: ApiContainerDep) -> JSONResponse:
    """阶段 5：项目大脑 / 记忆墙聚合接口。

    返回 L0-L4 五层记忆总览、待确认候选、最近变更。
    """
    store, settings = product_repositories._object_store(container.root_dir)
    scope = request.query_params.get("scope", "global").strip() or "global"
    project_id = request.query_params.get("project_id", "default").strip() or "default"
    try:
        factory = AggregateRepositoryFactory(runtime_root=container.root_dir, namespace_id=settings.namespace_id, json_store=store)
        resolution = factory.memory_publication_authority_resolution()
        skill_resolution = factory.project_skill_repository_resolution()
    except AggregateRepositoryFactoryError as error:
        return product_http._json_response(409, {"detail": "project brain rejected", "reason": str(error)}, product_http._no_store_headers())
    memory = SQLiteMemoryReader(resolution.records) if resolution.records is not None else ObjectStoreMemoryStore(store)
    authority = CurrentMemoryProjectionAuthority(
        memory=memory,
        project_skills=skill_resolution.repository,
        memory_authority_identity=resolution.authority_identity,
        project_skill_authority_identity=skill_resolution.authority_identity,
    )
    retrieval_status = get_project_brain_projection_status(
        project_id=project_id,
        authority=authority,
        projections=ObjectStoreMemoryProjectionRepository(store),
    ).to_payload()
    use_case = GetProjectBrainOverview(
        store,
        memory=memory,
        skills=skill_resolution.repository,
        namespace_id=settings.namespace_id,
        retrieval_status=retrieval_status,
    )
    try:
        result = use_case.execute(scope=scope, project_id=project_id)
        body = serialize_project_brain_overview(result)
        review_sources = product_document_visibility._workspace_review_sources(Path(container.root_dir))
        if review_sources:
            hidden = {item["candidate_id"] for item in body.get("candidates", [])
                      if product_document_visibility._candidate_uses_source(item, review_sources)}
            if hidden:
                body["candidates"] = [item for item in body["candidates"] if item["candidate_id"] not in hidden]
                body["total_candidates"] = len(body["candidates"])
                body["recent_changes"] = [item for item in body.get("recent_changes", [])
                                          if item.get("memory_id") not in hidden]
                body["total_changes"] = len(body["recent_changes"])
        return product_http._json_response(200, body, product_http._no_store_headers())
    except ProjectBrainOverviewError as error:
        return product_http._json_response(400, {"detail": str(error)}, product_http._no_store_headers())


@router.get("/api/rebuild/settings/memory-retrieval")
def memory_retrieval_settings(request: Request, container: ApiContainerDep) -> JSONResponse:
    project_id = request.query_params.get("project_id", "default").strip() or "default"
    try:
        _, _, authority, projections, _ = product_repositories._memory_projection_runtime(container)
        payload = memory_retrieval_settings_payload(
            project_id=project_id,
            authority=authority,
            projections=projections,
        )
    except AggregateRepositoryFactoryError as error:
        return product_http._json_response(409, {"detail": "memory retrieval settings rejected", "reason": str(error)}, product_http._no_store_headers())
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.get("/api/rebuild/developer-studio/memory-projection")
def memory_projection_diagnostics(request: Request, container: ApiContainerDep) -> JSONResponse:
    project_id = request.query_params.get("project_id", "default").strip() or "default"
    try:
        _, _, authority, projections, _ = product_repositories._memory_projection_runtime(container)
        payload = memory_projection_diagnostics_payload(
            project_id=project_id,
            authority=authority,
            projections=projections,
            effects=request.app.state.effect_runtime.log,
        )
    except AggregateRepositoryFactoryError as error:
        return product_http._json_response(409, {"detail": "memory projection diagnostics rejected", "reason": str(error)}, product_http._no_store_headers())
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/memory-retrieval/plan-preview")
async def preview_progressive_memory_retrieval_plan(request: Request) -> JSONResponse:
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_http._json_response(400, {"detail": "memory retrieval plan preview requires an object"}, product_http._no_store_headers())
    try:
        plan = plan_progressive_memory_retrieval(body.get("query"))
    except ProgressiveMemoryRetrievalError as error:
        return product_http._json_response(400, {"detail": str(error)}, product_http._no_store_headers())
    return product_http._json_response(
        200,
        serialize_progressive_retrieval_plan(plan),
        product_http._no_store_headers(),
    )


@router.get("/api/rebuild/developer-studio/memory-retrieval/performance")
def memory_retrieval_performance(request: Request, container: ApiContainerDep) -> JSONResponse:
    project_id = request.query_params.get("project_id", "default").strip() or "default"
    raw_limit = request.query_params.get("limit", "100")
    try:
        limit = int(raw_limit)
    except ValueError:
        return product_http._json_response(400, {"detail": "memory retrieval performance limit must be an integer"}, product_http._no_store_headers())
    if not 1 <= limit <= MAX_MEMORY_RETRIEVAL_PERFORMANCE_SAMPLES:
        return product_http._json_response(
            400,
            {"detail": f"memory retrieval performance limit must be between 1 and {MAX_MEMORY_RETRIEVAL_PERFORMANCE_SAMPLES}"},
            product_http._no_store_headers(),
        )
    store, _ = product_repositories._object_store(container.root_dir)
    payload = aggregate_memory_retrieval_performance(
        store.list("workbench_direct_questions"),
        project_id=project_id,
        limit=limit,
    )
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/memory-projection/refresh")
async def refresh_memory_projection(
    request: Request,
    _container: ApiContainerDep,
) -> JSONResponse:
    # Retained only as an explicit migration tombstone. All production rebuilds
    # must flow through preview -> one-use Automation Grant -> Effect dispatch.
    await product_http._json_body(request)
    return product_http._json_response(
        410,
        {
            "detail": "memory_projection_refresh_migrated_to_automation_grant",
            "replacement": "/api/rebuild/automations/memory-projection-rebuild/preview",
        },
        product_http._no_store_headers(),
    )


@router.get("/api/rebuild/project-brain/layer/{layer}/{object_id}")
def project_brain_layer_drill(
    request: Request, container: ApiContainerDep,
    layer: str, object_id: str,
) -> JSONResponse:
    """阶段 2.5：四层 progressive disclosure 下钻。

    给定 layer（L0/L1/L2/L3/L4）+ object_id，返回自身 + 子层关联 + 证据路径。
    - L4 → Persona evidence 指向的 L3/L2/L1/L0
    - L3 → 相关 L2 scenarios
    - L2 → 相关 L1 atoms
    - L1 → 相关 L0 sources
    - L0 → 叶子节点
    """
    store, settings = product_repositories._object_store(container.root_dir)
    try:
        factory = AggregateRepositoryFactory(runtime_root=container.root_dir, namespace_id=settings.namespace_id, json_store=store)
        resolution = factory.memory_publication_authority_resolution()
    except AggregateRepositoryFactoryError as error:
        return product_http._json_response(409, {"detail": "project brain drill rejected", "reason": str(error)}, product_http._no_store_headers())
    use_case = GetProjectBrainLayerDrill(
        store,
        memory=SQLiteMemoryReader(resolution.records) if resolution.records is not None else ObjectStoreMemoryStore(store),
        skills=factory.project_skill_repository(),
    )
    try:
        result = use_case.execute(layer=layer, object_id=object_id)
        body = serialize_drill_result(result)
        return product_http._json_response(200, body, product_http._no_store_headers())
    except ProjectBrainDrillError as error:
        return product_http._json_response(404, {"detail": str(error), "reason": "not_found"}, product_http._no_store_headers())


@router.get("/api/rebuild/project-brain/daily/{day_bucket}")
def project_brain_daily_scenario(
    request: Request, container: ApiContainerDep,
    day_bucket: str,
) -> JSONResponse:
    """阶段 2.5：每日对话 Scenario（按 day_bucket 聚合）。

    不写入长期记忆，只读聚合当天 atoms/candidates/sources。
    提取主要主题、用户问题、偏好信号、推进系列、待确认候选。
    """
    store, _ = product_repositories._object_store(container.root_dir)
    use_case = GetDailyConversationScenario(store)
    try:
        result = use_case.execute(day_bucket=day_bucket)
        body = serialize_daily_conversation_scenario(result)
        return product_http._json_response(200, body, product_http._no_store_headers())
    except ProjectBrainDrillError as error:
        return product_http._json_response(400, {"detail": str(error), "reason": "invalid_day_bucket"}, product_http._no_store_headers())


@router.get("/api/rebuild/project-brain/delta/{batch_id}")
def project_brain_memory_delta(
    request: Request, container: ApiContainerDep,
    batch_id: str,
) -> JSONResponse:
    """阶段 2.5：memory_delta —— 某次导入/提问/上传后的全部变更。

    按 import_batch_id 聚合：新增 L1 / 更新 L2 / 更新 L3 / 冲突 / 待确认 / 忽略 / 仅 L0。
    回答"这次上传改变了什么"。
    """
    store, _ = product_repositories._object_store(container.root_dir)
    use_case = GetMemoryDelta(store)
    try:
        result = use_case.execute(batch_id=batch_id)
        body = serialize_memory_delta(result)
        return product_http._json_response(200, body, product_http._no_store_headers())
    except ProjectBrainDrillError as error:
        return product_http._json_response(404, {"detail": str(error), "reason": "batch_not_found"}, product_http._no_store_headers())
