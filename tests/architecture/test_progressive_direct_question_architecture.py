from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_ai_question_capability_admits_rebuilds_to_the_core_effect_scheduler() -> None:
    route = (ROOT / "src" / "backend" / "api" / "workbench_ai_runtime.py").read_text(
        encoding="utf-8"
    )
    ai_runtime = (ROOT / "src" / "backend" / "memory_app" / "kernel" / "ai_runtime.py").read_text(
        encoding="utf-8"
    )

    assert "CurrentMemoryProjectionAuthority(" in route
    assert "ProgressiveDirectQuestionRecall(" in route
    assert "admit_memory_projection_rebuild(" in route
    assert "return effect.operation_id" in route
    assert "dispatch_operation(" not in route
    assert "CreateMemoryProjectionRebuildJob" not in route
    assert "run_memory_projection_rebuild_job" not in route
    assert "build_rebuild_job_repository" not in route
    assert "effect_runtime=getattr(application_state, \"effect_runtime\", None)" in ai_runtime
    assert "run_progressive_recall_drilldown(" not in route


def test_progressive_direct_question_adapter_is_read_only_for_business_authority() -> None:
    source = (
        ROOT
        / "src"
        / "core"
        / "product_core"
        / "progressive_direct_question_recall.py"
    ).read_text(encoding="utf-8")

    for forbidden in (
        ".publish(",
        ".save_candidate(",
        "complete_json(",
        "answer_provider",
        "team_memory",
        "source_content_reads",
        "documents",
    ):
        assert forbidden not in source.lower()
    assert '"r2_r3_provider_egress_allowed": False' in source
    assert '"business_writes_allowed": False' in source


def test_progressive_direct_question_trace_schema_cannot_store_private_content() -> None:
    schema = json.loads(
        (
            ROOT
            / "core-contracts"
            / "rebuild"
            / "progressive_direct_question_trace.schema.json"
        ).read_text(encoding="utf-8")
    )
    serialized = json.dumps(schema, ensure_ascii=False)

    for forbidden_property in (
        '"query"',
        '"content"',
        '"snippet"',
        '"source_refs"',
        '"locator"',
        '"path"',
        '"url"',
    ):
        assert forbidden_property not in serialized
    safety = schema["properties"]["safety"]["properties"]
    assert safety["query_recorded"]["const"] is False
    assert safety["content_recorded"]["const"] is False
    assert safety["r2_r3_provider_egress_allowed"]["const"] is False


def test_project_brain_status_composes_current_authority_without_scheduling_work() -> None:
    route = (ROOT / "src" / "backend" / "api" / "routes" / "product" / "project_brain.py").read_text(
        encoding="utf-8"
    )
    start = route.index('@router.get("/api/rebuild/project-brain")')
    end = route.index('@router.get("/api/rebuild/settings/memory-retrieval")', start)
    endpoint = route[start:end]

    assert 'request.query_params.get("project_id", "default")' in endpoint
    assert "CurrentMemoryProjectionAuthority(" in endpoint
    assert "get_project_brain_projection_status(" in endpoint
    assert "BackgroundTasks" not in endpoint
    assert "CreateMemoryProjectionRebuildJob" not in endpoint
    assert "background_tasks.add_task" not in endpoint


def test_project_brain_status_adapter_and_ui_keep_private_projection_details_hidden() -> None:
    adapter = (
        ROOT
        / "src"
        / "core"
        / "product_core"
        / "project_brain_projection_status.py"
    ).read_text(encoding="utf-8")
    view = (
        ROOT
        / "src"
        / "frontend"
        / "src"
        / "features"
        / "rebuild"
        / "ProjectBrainDisplay.jsx"
    ).read_text(encoding="utf-8")

    for forbidden in (
        ".save(",
        ".publish(",
        "CreateMemoryProjectionRebuildJob",
        "run_memory_projection_rebuild_job",
        "complete_json(",
    ):
        assert forbidden not in adapter
    assert '"content_included": False' in adapter
    assert '"locators_included": False' in adapter
    assert '"paths_included": False' in adapter
    assert '"urls_included": False' in adapter
    assert "已发布项目记忆" in view
    assert "diagnostic_code" not in view


def test_memory_projection_settings_and_diagnostics_keep_writes_explicit() -> None:
    route = (ROOT / "src" / "backend" / "api" / "routes" / "product" / "project_brain.py").read_text(
        encoding="utf-8"
    )
    settings_start = route.index('@router.get("/api/rebuild/settings/memory-retrieval")')
    refresh_start = route.index(
        '@router.post("/api/rebuild/developer-studio/memory-projection/refresh")'
    )
    settings_endpoint = route[settings_start:refresh_start]
    refresh_endpoint = route[refresh_start:route.index(
        '@router.get("/api/rebuild/project-brain/layer/', refresh_start
    )]

    assert "CreateMemoryProjectionRebuildJob" not in settings_endpoint
    assert "background_tasks.add_task" not in settings_endpoint
    assert '"memory_projection_refresh_migrated_to_automation_grant"' in refresh_endpoint
    assert "admit_memory_projection_rebuild(" not in refresh_endpoint
    assert "dispatch_operation(" not in refresh_endpoint
    assert "CreateMemoryProjectionRebuildJob" not in refresh_endpoint
    assert "BackgroundTasks" not in refresh_endpoint
    assert "background_tasks" not in refresh_endpoint
    assert "run_memory_projection_rebuild_job" not in refresh_endpoint
    assert '"replacement"' in refresh_endpoint


def test_normal_settings_hide_memory_layer_codes_while_developer_view_is_diagnostic() -> None:
    settings = (
        ROOT
        / "src"
        / "frontend"
        / "src"
        / "features"
        / "rebuild"
        / "RebuildSettingsDisplay.jsx"
    ).read_text(encoding="utf-8")
    developer = (
        ROOT
        / "src"
        / "frontend"
        / "src"
        / "features"
        / "rebuild"
        / "DeveloperStudio.jsx"
    ).read_text(encoding="utf-8")

    summary_start = settings.index("function MemorySystemSummary")
    summary_end = settings.index("function MemoryEngineSection", summary_start)
    summary = settings[summary_start:summary_end]
    for internal in ('"L0"', '"L1"', '"L2"', '"L3"', "source_id", "locator"):
        assert internal not in summary
    assert "项目总览" in summary
    assert "原始来源" in summary
    assert "未确认来源不会自动混入" in summary
    assert "团队记忆" not in summary
    assert "TeamMemorySettingsPanel" not in settings
    assert "authority_fingerprint_hint" in developer
    assert "latest_rebuild" in developer
