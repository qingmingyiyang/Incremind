from __future__ import annotations

import ast
from pathlib import Path

from core.product_core.video_link_adapter import (
    BilibiliDownloaderSettings,
    BilibiliVideoLinkResolver,
    LinkedVideoDownloadPlanner,
    serialize_linked_video_download_plan,
)
from core.product_core.video_workflow_endpoint import (
    ServeAuthorizedBilibiliDownloadEndpoint,
)
from core.product_core.workflow_progression import (
    WorkflowDecisionBoundary,
    WorkflowProgressionMode,
    decide_workflow_progression,
)


ROOT = Path(__file__).resolve().parents[2]
PURE_DECIDERS = (
    ROOT / "src/core/product_core/workflow_progression.py",
    ROOT / "src/core/product_core/audio_auto_decider.py",
    ROOT / "src/core/product_core/long_audio_decider.py",
    ROOT / "src/core/product_core/video_auto_decider.py",
)
REBUILD_ROUTES = ROOT / "src/backend/api/routes/product/bilibili.py"
ASK_BOUNDARIES = (
    WorkflowDecisionBoundary.PERMISSION_EXPANSION,
    WorkflowDecisionBoundary.IRREVERSIBLE_CHANGE,
    WorkflowDecisionBoundary.BUDGET_EXCEEDED,
    WorkflowDecisionBoundary.MATERIAL_AMBIGUITY,
    WorkflowDecisionBoundary.UNKNOWN_EFFECT,
    WorkflowDecisionBoundary.EXTERNAL_DOWNLOAD_WRITES_FILE,
    WorkflowDecisionBoundary.FORMAL_MEMORY_PUBLICATION,
    WorkflowDecisionBoundary.HARD_REDACT,
    WorkflowDecisionBoundary.HIGH_RISK,
)
FORBIDDEN_RUNTIME_IMPORTS = ("effect_log", "job_runner", "storage_provider")


def test_workflow_progression_is_a_closed_three_mode_contract() -> None:
    assert tuple(WorkflowProgressionMode) == (
        WorkflowProgressionMode.AUTO,
        WorkflowProgressionMode.AUTO_WITH_NOTICE,
        WorkflowProgressionMode.ASK,
    )
    assert tuple(WorkflowDecisionBoundary) == (
        WorkflowDecisionBoundary.DETERMINISTIC,
        WorkflowDecisionBoundary.REVERSIBLE_VISIBLE_CHANGE,
        *ASK_BOUNDARIES,
    )

    assert decide_workflow_progression(
        WorkflowDecisionBoundary.DETERMINISTIC,
    ).mode is WorkflowProgressionMode.AUTO
    assert decide_workflow_progression(
        WorkflowDecisionBoundary.REVERSIBLE_VISIBLE_CHANGE,
    ).mode is WorkflowProgressionMode.AUTO_WITH_NOTICE

    for boundary in ASK_BOUNDARIES:
        decision = decide_workflow_progression(boundary)
        assert decision.mode is WorkflowProgressionMode.ASK
        assert decision.requires_user_confirmation is True
        assert decision.reason is boundary

    unspecified = decide_workflow_progression()
    assert unspecified.mode is WorkflowProgressionMode.ASK
    assert unspecified.reason is WorkflowDecisionBoundary.MATERIAL_AMBIGUITY


def test_bilibili_resolution_and_dry_run_advance_automatically() -> None:
    resolution = BilibiliVideoLinkResolver().resolve(
        url="https://www.bilibili.com/video/BV1abcDEF234?p=2",
    )
    plan = LinkedVideoDownloadPlanner().create_dry_run_plan(
        resolution=resolution,
        video_id="BV1abcDEF234_p2",
    )

    assert resolution.progression_mode == WorkflowProgressionMode.AUTO.value
    assert resolution.requires_user_confirmation is False
    assert plan.progression_mode == WorkflowProgressionMode.AUTO.value
    assert plan.requires_user_confirmation is False
    assert plan.downloads_video is False
    assert plan.writes_video_file is False
    assert plan.next_step == "ready_for_authorized_download_confirmation"


def test_real_bilibili_download_is_an_ask_boundary_before_handler_execution() -> None:
    resolution = BilibiliVideoLinkResolver().resolve(
        url="https://www.bilibili.com/video/BV1abcDEF234",
    )
    plan = LinkedVideoDownloadPlanner().create_dry_run_plan(
        resolution=resolution,
        video_id="BV1abcDEF234",
    )
    handler_called = False

    def unexpected_handler(**_: object) -> object:
        nonlocal handler_called
        handler_called = True
        raise AssertionError("download handler must not run before user confirmation")

    response = ServeAuthorizedBilibiliDownloadEndpoint().execute(
        method="POST",
        path="/api/rebuild/video-links/bilibili/authorized-download",
        body={
            "plan": serialize_linked_video_download_plan(plan),
            "settings": {"enabled": True, "output_root": "local-video-output"},
        },
        download_video=unexpected_handler,
        trusted_settings=BilibiliDownloaderSettings(
            status="ready",
            enabled=True,
            provider_name="yt-dlp-bilibili",
            output_root="trusted-output",
            cookie_mode="none",
            cookies_from_browser="",
            cookies_file=None,
            allow_restricted_content=False,
            explicit_enable_required=True,
            remote_processing=False,
            memory_publication="not_started",
        ),
    )

    assert response.status_code == 400
    assert response.body["progression_mode"] == WorkflowProgressionMode.ASK.value
    assert response.body["progression_reason"] == "external_download_writes_file"
    assert handler_called is False


def test_pure_deciders_cannot_import_effect_job_or_storage_runtimes() -> None:
    offenders: dict[str, tuple[str, ...]] = {}
    for path in PURE_DECIDERS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_modules = tuple(
            module
            for node in ast.walk(tree)
            for module in _imported_modules(node)
        )
        forbidden = tuple(
            module
            for module in imported_modules
            if any(_has_module_segment(module, forbidden_name) for forbidden_name in FORBIDDEN_RUNTIME_IMPORTS)
        )
        if forbidden:
            offenders[str(path.relative_to(ROOT))] = forbidden

    assert offenders == {}


def test_legacy_auto_download_cannot_execute_or_forge_user_confirmation() -> None:
    tree = ast.parse(REBUILD_ROUTES.read_text(encoding="utf-8"), filename=str(REBUILD_ROUTES))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_legacy_bilibili_auto_download"
    )
    calls = {
        _call_name(node)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
    }
    assert calls.isdisjoint(
        {
            "ServeAuthorizedBilibiliDownloadEndpoint",
            "RegisterDownloadedBilibiliVideoSource",
            "_effect_bilibili_downloader",
            "_finish_legacy_bilibili_download",
        }
    )


def _imported_modules(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return (node.module or "",)
    return ()


def _has_module_segment(module: str, segment: str) -> bool:
    return segment in module.split(".")


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""
