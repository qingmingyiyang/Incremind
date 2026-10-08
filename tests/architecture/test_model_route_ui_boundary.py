from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_developer_studio_owns_the_single_editable_model_route_surface() -> None:
    settings = _read("src/frontend/src/features/rebuild/RebuildSettingsDisplay.jsx")
    studio = _read("src/frontend/src/features/rebuild/DeveloperStudio.jsx")
    provider_panel = _read("src/frontend/src/features/rebuild/DeveloperProviderPanel.jsx")
    assert 'import { ModelRouteAuthorityPanel } from "./ModelRouteAuthorityPanel"' not in settings
    assert 'import { ModelRouteAuthorityPanel } from "./ModelRouteAuthorityPanel"' in studio
    assert "domain=model-execution&page=providers" in settings
    assert "<ModelRouteAuthorityPanel editable" in studio
    assert "showRouteSummary={false}" in provider_panel


def test_shared_panel_reads_and_updates_registry_without_legacy_writes() -> None:
    panel = _read("src/frontend/src/features/rebuild/ModelRouteAuthorityPanel.jsx")
    api = _read("src/frontend/src/features/rebuild/rebuildSettingsApi.js")
    assert 'MODEL_ROUTES_ENDPOINT = "/api/model-routes"' in api
    assert 'MODEL_ROUTE_RUNTIME_ENDPOINT = "/api/model-route-runtime"' in api
    assert "listModelRoutes" in panel and "loadModelRouteRuntime" in panel and "updateModelRoute" in panel
    for forbidden in ("localStorage.setItem", "saveDeveloperStudioConfig", "task_model_map:", "confirmModelRouteMigration"):
        assert forbidden not in panel


def test_developer_studio_preserves_legacy_map_without_sending_parallel_authority() -> None:
    studio = _read("src/frontend/src/features/rebuild/DeveloperStudio.jsx")
    rollback = studio[studio.index("function confirmRollback()") : studio.index("function exportConfig()")]
    serializer = studio[studio.index("function toBackendDeveloperConfig") : studio.index("function fromBackendDeveloperConfig")]
    assert "taskModelMap: { ...config.taskModelMap }" in rollback
    assert "rollbackTarget.config.taskModelMap" not in rollback
    assert "task_model_map" not in serializer


def test_ui_claims_only_the_verified_intake_and_answer_consumers() -> None:
    panel = _read("src/frontend/src/features/rebuild/ModelRouteAuthorityPanel.jsx")
    assert '"intake.classification": "输入分类 · Auto Intake"' in panel
    assert '"search.answer": "项目问答 · 已发布证据回答"' in panel
    assert '"memory.project_routing": "项目语义路由（实验评估）"' in panel
    assert "尚未接入生产 consumer" in panel
    consumer_block = panel[panel.index("const CONSUMERS") : panel.index("const ROUTE_LABELS")]
    for unverified in ("memory.project_routing", "memory.candidate", "conversation.default", "task.lightweight"):
        assert unverified not in consumer_block


def test_three_tier_ui_is_a_registry_projection_not_a_second_authority() -> None:
    tiers = _read("src/frontend/src/features/rebuild/ModelTierSettings.jsx")
    assert "updateModelRouteBatch" in tiers
    assert '"memory.project_routing"' not in tiers
    assert '"search.answer"' in tiers
    for forbidden in ("localStorage", "task_model_map", "saveDeveloperStudioConfig", "saveProviderSecret", "apiKey"):
        assert forbidden not in tiers


def test_provider_credentials_use_only_the_desktop_capture_boundary() -> None:
    provider_surfaces = {
        relative: _read(relative)
        for relative in (
            "src/frontend/src/features/rebuild/RebuildSettingsDisplay.jsx",
            "src/frontend/src/features/rebuild/useModelConnectionController.js",
            "src/frontend/src/features/rebuild/AIEnginePage.jsx",
            "src/frontend/src/features/rebuild/DeveloperProviderPanel.jsx",
            "src/frontend/src/features/rebuild/ModelStudioSection.jsx",
        )
    }
    for source in provider_surfaces.values():
        assert "saveProviderSecret" not in source
        assert "const [apiKey" not in source
        assert "onApiKeyChange" not in source
    assert "<CredentialCaptureCard" in provider_surfaces[
        "src/frontend/src/features/rebuild/RebuildSettingsDisplay.jsx"
    ]
    assert "<CredentialCaptureCard" in provider_surfaces[
        "src/frontend/src/features/rebuild/ModelStudioSection.jsx"
    ]

    api = _read("src/frontend/src/features/rebuild/rebuildSettingsApi.js")
    assert "export function saveProviderSecret" not in api
    provider_test = api[api.index("export function testProvider") : api.index("export function listProviderModels")]
    assert "api_key" not in provider_test
    assert "openai_api_key" not in provider_test
