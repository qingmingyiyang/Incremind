from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_legacy_bilibili_download_effects_have_one_guarded_route_owner() -> None:
    production = ROOT / "src"
    target_names = {
        "AuthorizedBilibiliDownloader",
        "RegisterDownloadedBilibiliVideoSource",
        "BilibiliVideoLinkResolver",
    }
    owners: list[tuple[Path, str, str]] = []
    for path in production.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in (
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                name = _call_name(call)
                if name in target_names:
                    owners.append((path.relative_to(ROOT), function.name, name))

    assert owners == [
        (Path("src/backend/api/routes/product/bilibili.py"), "_legacy_bilibili_video_download_plan", "BilibiliVideoLinkResolver"),
        (Path("src/backend/api/routes/product/bilibili.py"), "_legacy_bilibili_authorized_download", "RegisterDownloadedBilibiliVideoSource"),
        (Path("src/backend/api/routes/product/bilibili.py"), "_effect_bilibili_downloader", "AuthorizedBilibiliDownloader"),
        (Path("src/core/product_core/video_workflow_endpoint.py"), "_download_plan_from_payload", "BilibiliVideoLinkResolver"),
    ]
    source = (ROOT / "src/backend/api/routes/product/bilibili.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    guarded = {
        function.name
        for function in tree.body
        if isinstance(function, ast.FunctionDef)
        and function.name.startswith("_legacy_bilibili_")
        and any(_is_legacy_writer(item) for item in ast.walk(function))
    }
    assert {
        "_legacy_bilibili_video_download_plan",
        "_legacy_bilibili_authorized_download",
        "_legacy_bilibili_auto_download",
    } <= guarded


def test_frontend_hands_entry_has_one_client_and_legacy_clients_are_selection_gated() -> None:
    frontend = ROOT / "src/frontend/src"
    hands_endpoint = "/api/rebuild/media-ingress/bilibili/resolve"
    legacy_endpoint = "/api/rebuild/video-links/bilibili/"
    hands_owners = []
    legacy_owners = []
    for path in (*frontend.rglob("*.js"), *frontend.rglob("*.jsx")):
        source = path.read_text(encoding="utf-8")
        if hands_endpoint in source:
            hands_owners.append(path.relative_to(ROOT))
        if legacy_endpoint in source:
            legacy_owners.append(path.relative_to(ROOT))

    assert hands_owners == [
        Path("src/frontend/src/features/rebuild/bilibiliMediaIngressApi.js")
    ]
    assert set(legacy_owners) == {
        Path("src/frontend/src/features/rebuild/libraryOverviewApi.js"),
        Path("src/frontend/src/features/rebuild/workbenchIntakeApi.js"),
    }
    for relative in (
        "src/frontend/src/features/rebuild/RebuildHomeSmoke.jsx",
        "src/frontend/src/features/rebuild/RebuildVideoWorkflowDisplay.jsx",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "prepareBilibiliMediaIngress" in source or "prepareVideoIngress" in source
        assert "approveBilibiliMediaIngress" in source or "approveVideoIngress" in source
    application = (ROOT / "src/frontend/src/App.jsx").read_text(encoding="utf-8")
    assert "prepareMediaIngress={prepareMediaIngress}" in application
    assert "approveMediaIngress={approveMediaIngress}" in application


def test_generic_auto_intake_uses_shared_target_selector_inside_legacy_fence() -> None:
    route_path = ROOT / "src/backend/api/routes/workbench_auto_intake.py"
    tree = ast.parse(route_path.read_text(encoding="utf-8"), filename=str(route_path))
    imports_shared_selector = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "core.product_core.workbench_auto_intake"
        and any(alias.name == "select_workbench_link_target" for alias in node.names)
        for node in tree.body
    )
    selector = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_single_bilibili_video_url"
    )
    route = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_legacy_auto_intake"
    )
    guarded_effects = [
        item
        for item in ast.walk(route)
        if _is_legacy_writer(item)
        and any(
            isinstance(descendant, ast.Call) and _call_name(descendant) == "_execute_auto_intake"
            for descendant in ast.walk(item)
        )
    ]

    assert imports_shared_selector
    assert any(
        isinstance(node, ast.Call) and _call_name(node) == "select_workbench_link_target"
        for node in ast.walk(selector)
    )
    assert len(guarded_effects) == 1


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _is_legacy_writer(node: ast.AST) -> bool:
    if not isinstance(node, ast.With):
        return False
    for item in node.items:
        expression = item.context_expr
        if not isinstance(expression, ast.Call) or not isinstance(expression.func, ast.Attribute):
            continue
        if expression.func.attr != "writer" or len(expression.args) != 1:
            continue
        argument = expression.args[0]
        if isinstance(argument, ast.Constant) and argument.value == "legacy":
            return True
    return False
