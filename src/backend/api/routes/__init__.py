from __future__ import annotations

from fastapi import FastAPI

from backend.api.routes.ai import router as ai_router
from backend.api.routes.ai_agents import router as ai_agents_router
from backend.api.routes.application_skill_learning import router as application_skill_learning_router
from backend.api.routes.automation_memory_projection import router as automation_memory_projection_router
from backend.api.routes.bilibili_media_ingress import router as bilibili_media_ingress_router
from backend.api.routes.chaoxing import router as chaoxing_router
from backend.api.routes.companion import router as companion_router
from backend.api.routes.companion_memory_state import router as companion_memory_state_router
from backend.api.routes.context_graph import router as context_graph_router
from backend.api.routes.credential_capture import router as credential_capture_router
from backend.api.routes.cloud_asr_settings import router as cloud_asr_settings_router
from backend.api.routes.realtime_asr import router as realtime_asr_router
from backend.api.routes.external_extensions import router as external_extensions_router
from backend.api.routes.expert_catalog import router as expert_catalog_router
from backend.api.routes.health import router as health_router
from backend.api.routes.intake import router as intake_router
from backend.api.routes.library_lifecycle import router as library_lifecycle_router
from backend.api.routes.library_query import router as library_query_router
from backend.api.routes.library_overview import router as library_overview_router
from backend.api.routes.memory_distillation import router as memory_distillation_router
from backend.api.routes.mcp_migrations import router as mcp_migrations_router
from backend.api.routes.plugin_packages import router as plugin_packages_router
from backend.api.routes.ppt_master_installation import router as ppt_master_installation_router
from backend.api.routes.personal_world_model import router as personal_world_model_router
from backend.api.routes.project_task_cases import router as project_task_cases_router
from backend.api.routes.workbench_original_asset import router as workbench_original_asset_router
from backend.api.routes.workbench_audio_intake import router as workbench_audio_intake_router
from backend.api.routes.workbench_auto_intake import router as workbench_auto_intake_router
from backend.api.routes.workbench_input_classifier import router as workbench_input_classifier_router
from backend.api.routes.workbench_source_intake import router as workbench_source_intake_router
from backend.api.routes.workbench_video_intake import router as workbench_video_intake_router
from backend.api.routes.workflow_decisions import router as workflow_decisions_router
from backend.api.routes.xiaohongshu_controlled_credentials import (
    router as xiaohongshu_controlled_credentials_router,
)
from backend.api.routes.linked import router as linked_router
from backend.api.routes.replay import router as replay_router
from backend.api.routes.product import router as product_router
from backend.api.routes.recursive_evolution import router as recursive_evolution_router
from backend.api.routes.series import router as series_router
from backend.api.routes.settings import router as settings_router
from backend.api.routes.session_placement import router as session_placement_router
from backend.api.routes.team_memory import router as team_memory_router
from backend.api.routes.tasks import router as tasks_router
from backend.api.routes.videos import router as videos_router


def include_api_routers(app: FastAPI) -> None:
    app.include_router(ai_router)
    app.include_router(ai_agents_router)
    app.include_router(application_skill_learning_router)
    app.include_router(automation_memory_projection_router)
    app.include_router(expert_catalog_router)
    app.include_router(bilibili_media_ingress_router)
    app.include_router(health_router)
    app.include_router(settings_router)
    app.include_router(session_placement_router)
    app.include_router(team_memory_router)
    app.include_router(tasks_router)
    app.include_router(series_router)
    app.include_router(videos_router)
    app.include_router(linked_router)
    app.include_router(chaoxing_router)
    app.include_router(companion_router)
    app.include_router(companion_memory_state_router)
    app.include_router(context_graph_router)
    app.include_router(credential_capture_router)
    app.include_router(cloud_asr_settings_router)
    app.include_router(realtime_asr_router)
    app.include_router(external_extensions_router)
    app.include_router(intake_router)
    app.include_router(library_lifecycle_router)
    app.include_router(library_query_router)
    app.include_router(library_overview_router)
    app.include_router(memory_distillation_router)
    app.include_router(mcp_migrations_router)
    app.include_router(plugin_packages_router)
    app.include_router(ppt_master_installation_router)
    app.include_router(personal_world_model_router)
    app.include_router(project_task_cases_router)
    app.include_router(workbench_original_asset_router)
    app.include_router(workbench_audio_intake_router)
    app.include_router(workbench_auto_intake_router)
    app.include_router(workbench_input_classifier_router)
    app.include_router(workbench_source_intake_router)
    app.include_router(workbench_video_intake_router)
    app.include_router(workflow_decisions_router)
    app.include_router(xiaohongshu_controlled_credentials_router)
    app.include_router(replay_router)
    app.include_router(product_router)
    app.include_router(recursive_evolution_router)
