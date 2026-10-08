from __future__ import annotations

from pathlib import Path
import ast


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = (ROOT / "src" / "backend", ROOT / "src" / "core")
MATERIALIZATION_OWNERS = {
    ROOT / "src" / "backend" / "security" / "secrets.py",
    ROOT / "src" / "backend" / "security" / "secret_egress.py",
    ROOT / "src" / "backend" / "security" / "host_signing_identity_store.py",
}
SDK_MATERIALIZATION_CALLERS = {
    ROOT / "src" / "backend" / "api" / "bootstrap.py",
    ROOT / "src" / "backend" / "model_runtime.py",
    ROOT / "src" / "backend" / "security" / "xiaohongshu_controlled_credentials.py",
    ROOT / "src" / "backend" / "team_memory.py",
    ROOT / "src" / "backend" / "video_summary" / "infrastructure" / "settings_service.py",
}


def _python_sources():
    for root in PRODUCTION:
        yield from root.rglob("*.py")


def test_secret_plaintext_materialization_is_limited_to_core_owners() -> None:
    violations: list[str] = []
    for path in _python_sources():
        if path in MATERIALIZATION_OWNERS:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            receiver = node.func.value
            receiver_name = receiver.id if isinstance(receiver, ast.Name) else ""
            built_secret_store = (
                isinstance(receiver, ast.Call)
                and isinstance(receiver.func, ast.Name)
                and receiver.func.id == "build_secret_store"
            )
            if (receiver_name in {"secret_store", "_secret_store", "_secrets"} or built_secret_store) and node.func.attr in {"get", "get_snapshot"}:
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}:{receiver_name}.{node.func.attr}",
                )
        for forbidden in (
            "secret_store.get(", "_secret_store.get(", "_secrets.get_snapshot(",
            "secret_store.get_snapshot(", "_secret_store.get_snapshot(",
        ):
            if forbidden in source:
                violations.append(f"{path.relative_to(ROOT)}:{forbidden}")
    assert violations == []
    store = (ROOT / "src" / "backend" / "security" / "secrets.py").read_text(encoding="utf-8")
    assert "def get(self, key:" not in store


def test_session_placement_receives_only_the_local_signer_capability() -> None:
    runtime = (ROOT / "src" / "backend" / "api" / "session_placement_runtime.py").read_text(encoding="utf-8")
    assert "HostSigningIdentityStore" in runtime
    assert "get_snapshot" not in runtime
    assert "SecretSnapshot" not in runtime


def test_mcp_has_no_plaintext_resolver_or_secret_stdio_environment() -> None:
    mcp = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src" / "core" / "mcp_host").rglob("*.py")
    )
    runtime = (ROOT / "src" / "backend" / "api" / "mcp_runtime.py").read_text(encoding="utf-8")
    assert "SecretResolver" not in mcp
    assert "secret_resolver" not in mcp
    assert "secret_resolver" not in runtime
    assert "MCP stdio secret environment is prohibited" in mcp
    assert "headers_for_wire" in mcp
    broker = (ROOT / "src" / "backend" / "security" / "secret_egress.py").read_text(encoding="utf-8")
    assert "inject_stdio" not in broker


def test_provider_gateways_do_not_retain_plaintext_keys() -> None:
    paths = (
        ROOT / "src" / "backend" / "shared" / "llm" / "litellm_gateway.py",
        ROOT / "src" / "backend" / "shared" / "llm" / "image_generation_gateway.py",
        ROOT / "src" / "backend" / "video_summary" / "infrastructure" / "litellm_web_search.py",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "self._api_key =" not in source
        assert "api_key_provider" in source


def test_sdk_plaintext_materialization_is_limited_to_reviewed_wire_factories() -> None:
    callers = {
        path
        for path in _python_sources()
        if path not in MATERIALIZATION_OWNERS
        and ".materialize_for_sdk(" in path.read_text(encoding="utf-8")
    }
    assert callers == SDK_MATERIALIZATION_CALLERS
    for path in callers:
        source = path.read_text(encoding="utf-8")
        assert "broker.revoke(" in source


def test_four_layer_provider_has_no_environment_or_secret_reader_bypass() -> None:
    source = (
        ROOT / "src/core/product_core/openai_compatible_four_layer_provider.py"
    ).read_text(encoding="utf-8")
    assert "os.environ" not in source
    assert "secret_reader" not in source
    assert "get_secret(" not in source
    assert "authorization_header_provider" in source


def test_legacy_bilibili_ytdlp_path_is_anonymous_only() -> None:
    source = (ROOT / "src/backend/bilibili/ytdlp_bilibili.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "cookiesfrombrowser", "cookiefile", "--cookies", "BILIBILI_COOKIES_FILE",
        "BILIBILI_COOKIES_FROM_BROWSER",
    ):
        assert forbidden not in source


def test_legacy_vision_provider_uses_broker_header_injection() -> None:
    vision = (ROOT / "src/backend/video_intake/vision.py").read_text(encoding="utf-8")
    service = (ROOT / "src/backend/video_intake/service.py").read_text(encoding="utf-8")
    assert "    api_key: str =" not in vision
    assert "store.get(" not in vision
    assert "Authorization\": f\"Bearer" not in vision
    assert "authorization_header_provider" in vision
    assert "SecretEgressBroker(" in service
    assert "broker.inject_header(" in service


def test_legacy_video_intake_bilibili_is_anonymous_only() -> None:
    source = (ROOT / "src/backend/video_intake/bilibili.py").read_text(encoding="utf-8")
    for forbidden in (
        "cookiesfrombrowser", "cookiefile", "cookiejar.save", "edge-cookies.txt",
    ):
        assert forbidden not in source


def test_authorized_public_bilibili_rejects_credentialed_subprocess_modes() -> None:
    source = (ROOT / "src/backend/bilibili/authorized_public_download.py").read_text(
        encoding="utf-8"
    )
    assert 'if clean != "none"' in source
    assert "credentialed Bilibili subprocess mode is prohibited" in source
    tree = ast.parse(source)
    command = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_yt_dlp_command"
    )
    rendered = ast.unparse(command)
    assert "--cookies-from-browser" not in rendered
    assert "'--cookies'" not in rendered


def test_rebuild_provider_status_never_reads_secret_or_environment_plaintext() -> None:
    source = (ROOT / "src/backend/api/routes/product/providers.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    status = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_deepseek_provider_status"
    )
    selector = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_deepseek_api_key_secret_name"
    )
    rendered = ast.unparse(status) + ast.unparse(selector)
    assert "os.environ" not in rendered
    assert "get_secret" not in rendered
    assert "has_secret" in rendered


def test_connection_diagnostic_accepts_only_a_wire_secret_provider() -> None:
    runtime = (ROOT / "src/backend/shared/llm/connection_diagnostic.py").read_text(encoding="utf-8")
    settings = (
        ROOT / "src/backend/video_summary/infrastructure/settings_service.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(runtime)
    diagnostic = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_model_connection_diagnostic"
    )
    rendered = ast.unparse(diagnostic)
    assert "api_key_provider" in rendered
    assert "api_key=None" in rendered
    assert "from backend.model_runtime import" not in settings
    assert "SecretEgressBroker(" in settings
    assert "materialize_for_sdk(" in settings


def test_secret_lease_contract_binds_all_required_dimensions() -> None:
    source = (ROOT / "src" / "backend" / "security" / "secret_egress.py").read_text(encoding="utf-8")
    for field in (
        "project_id", "secret_ref", "secret_revision", "purpose",
        "allowed_hosts", "boundary_revision", "expires_at",
    ):
        assert field in source
    for failure in (
        "secret_lease_expired", "secret_lease_revoked", "secret_lease_host_denied",
        "secret_lease_boundary_drift", "secret_lease_revision_drift",
        "secret_lease_context_denied",
    ):
        assert failure in source
