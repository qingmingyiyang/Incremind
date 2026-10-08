from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_settings_hands_canonical_model_route_work_to_developer_without_renderer_drafts() -> None:
    settings = _read("src/frontend/src/features/rebuild/RebuildSettingsDisplay.jsx")
    developer = _read("src/frontend/src/features/rebuild/DeveloperStudio.jsx")
    ai_engine = _read("src/frontend/src/features/rebuild/AIEnginePage.jsx")
    api = _read("src/frontend/src/features/rebuild/rebuildSettingsApi.js")
    assert "ModelRouteAuthorityPanel" not in settings
    assert "ModelRouteAuthorityPanel" in developer
    assert "domain=model-execution&page=providers" in settings
    assert "chriptmas-os-task-model-map" not in settings
    assert "chriptmas-os-task-model-map" not in ai_engine
    assert "保存分配" not in ai_engine
    assert "选择模型" not in ai_engine
    assert "查看处理边界" in ai_engine
    assert "chriptmas-os-task-model-map" not in api


def test_developer_task_map_string_shape_resolves_to_no_runtime_reference() -> None:
    defaults = _read("src/frontend/src/features/rebuild/developerModeStore.js")
    resolver = _read("src/core/product_core/task_model_map_resolver.py")
    assert 'lightweight: "mp-lightweight"' in defaults
    assert 'intakeMain: "mp-intake-main"' in defaults
    assert "if not isinstance(entry, Mapping):" in resolver
    assert "TaskModelMapResolution(use_key=use_key, profile=None)" in resolver
    assert "This use case" not in resolver  # keep the source assertion language-independent
    assert "不访问 ObjectStore" in resolver


def test_structured_task_map_is_tracking_metadata_not_provider_execution() -> None:
    resolver = _read("src/core/product_core/task_model_map_resolver.py")
    structuring = _read("src/core/product_core/source_structuring.py")
    intake = _read("src/core/product_core/workbench_auto_intake.py")
    assert "model_profile_refs" in structuring
    assert "model_profile_refs" in intake
    assert "provider_id" in resolver and "model_name" in resolver
    assert "LiteLLM" not in resolver
    assert "completion(" not in resolver


def test_model_studio_advanced_values_are_uncontrolled_and_not_serialized() -> None:
    source = _read("src/frontend/src/features/rebuild/ModelStudioSection.jsx")
    for removed_control in (
        ">请求格式</span>", ">超时时间 (秒)</span>", ">最大重试</span>",
        ">自定义 Headers (JSON)</span>", ">流式输出</span>", ">JSON 输出测试</span>",
    ):
        assert removed_control not in source
    serialize_block = source[source.index("function serializeDraft"):source.index("export function ModelStudioSection")]
    for forbidden in ("request_format", "timeout", "retry", "headers", "stream", "json_output"):
        assert forbidden not in serialize_block.lower()
    assert "API 路径" in source
    assert "api_path: draft.api_path" in serialize_block
    assert "ModelRouteAuthorityPanel" in source
    assert 'aria-label="真实模型路由"' in source
    assert "等待模型路由接入" not in source


def test_test_lab_route_prompt_recipe_and_video_claims_match_backend_behavior() -> None:
    api = _read("src/backend/api/routes/product/developer_test_lab.py")
    frontend_api = _read("src/frontend/src/features/rebuild/rebuildSettingsApi.js")
    endpoint = api[api.index("async def developer_studio_test_lab"):len(api)]
    assert 'route_key=route_key' in endpoint
    assert 'source=str(body.get("prompt_source") or "")' in endpoint
    assert ".evaluate_for_test(" in endpoint
    assert 'body.get("provider_call_confirmed") is not True' in endpoint
    assert "body: JSON.stringify(payload || {})" in frontend_api
    assert 'if test_type == "video":' in api
    assert '"skipped": True' in api
    assert "_call_deepseek_for_test_lab" not in api


def test_project_skill_outline_is_separate_active_cas_authority() -> None:
    editor = _read("src/frontend/src/features/rebuild/ProjectSkillOutlineEditor.jsx")
    api = _read("src/backend/api/routes/product/project_skills.py")
    assert "saveProjectSkillOutline" in editor
    assert "expectedRevision: state.expectedRevision" in editor
    route_block = api[api.index('async def project_skill_outline_update('):]
    assert 'expected_revision = body.get("expected_revision")' in route_block
    assert "skills.save(" in route_block


def test_developer_config_rejects_secret_material() -> None:
    config = _read("src/core/product_core/developer_studio_config.py")
    assert "_reject_sensitive_material(record)" in config
    for key in ("api_key", "authorization", "cookie", "cookies_file"):
        assert key in config
