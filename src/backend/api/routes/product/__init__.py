"""Compose product API domains; business behavior belongs to their modules."""
from fastapi import APIRouter

from .project_brain import router as project_brain_router
from .vault_status import router as vault_status_router
from .providers import router as providers_router
from .vault_snapshots import router as vault_snapshots_router
from .source_retention import router as source_retention_router
from .asset_retention import router as asset_retention_router
from .settings import router as settings_router
from .developer_prompts import router as developer_prompts_router
from .application_skills import router as application_skills_router
from .processing_recipes import router as processing_recipes_router
from .model_routes import router as model_routes_router
from .developer_logs import router as developer_logs_router
from .developer_test_lab import router as developer_test_lab_router
from .project_skills import router as project_skills_router
from .source_content import router as source_content_router
from .inspirations import router as inspirations_router
from .external_review import router as external_review_router
from .source_documents import router as source_documents_router
from .memory_candidates import router as memory_candidates_router
from .documents import router as documents_router
from .document_delivery import router as document_delivery_router
from .workbench_compat import router as workbench_compat_router
from .library_index import router as library_index_router
from .library_persona import router as library_persona_router
from .jobs import router as jobs_router
from .bilibili import router as bilibili_router
from .media_processing import router as media_processing_router
from .memory_hierarchy import router as memory_hierarchy_router
from .scenario_transfer import router as scenario_transfer_router
from .memory_import import router as memory_import_router
from .memory_import_review import router as memory_import_review_router
from .memory_export import router as memory_export_router
from .route_order import ENDPOINT_ORDER

router = APIRouter()
for domain_router in (
    project_brain_router,
    vault_status_router,
    providers_router,
    vault_snapshots_router,
    source_retention_router,
    asset_retention_router,
    settings_router,
    developer_prompts_router,
    application_skills_router,
    processing_recipes_router,
    model_routes_router,
    developer_logs_router,
    developer_test_lab_router,
    project_skills_router,
    source_content_router,
    inspirations_router,
    external_review_router,
    source_documents_router,
    memory_candidates_router,
    documents_router,
    document_delivery_router,
    workbench_compat_router,
    library_index_router,
    library_persona_router,
    jobs_router,
    bilibili_router,
    media_processing_router,
    memory_hierarchy_router,
    scenario_transfer_router,
    memory_import_router,
    memory_import_review_router,
    memory_export_router,
):
    router.routes.extend(domain_router.routes)

_endpoint_position = {name: index for index, name in enumerate(ENDPOINT_ORDER)}
router.routes.sort(key=lambda route: _endpoint_position[route.name])
