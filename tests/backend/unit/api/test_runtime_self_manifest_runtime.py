from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.runtime_self_manifest_runtime import (
    authenticated_runtime_self_manifest_projection,
    build_runtime_self_manifest_for_app,
    freeze_runtime_self_manifest_for_turn,
)
from core.ai_kernel import InMemoryTurnPayloadStore
from core.plugin_host.runtime_self_manifest import RuntimeProbeResult, RuntimeSelfManifestError


def _manifest(tmp_path: Path, *, features: tuple[str, ...] = ("isolated-python-artifact",)) -> dict[str, object]:
    resources = tmp_path / "resources"
    app_data = tmp_path / "app-data"
    resources.mkdir(exist_ok=True)
    app_data.mkdir(exist_ok=True)
    return build_runtime_self_manifest_for_app(
        tmp_path,
        packaged=True,
        resources_root=resources,
        app_data_root=app_data,
        runtime_source="bundled",
        features=features,
        probes=(RuntimeProbeResult("sidecar-auth", "pass"),),
    )


def test_runtime_probe_serializes_only_sanitized_controlled_facts(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    encoded = str(manifest).lower()

    assert manifest["runtime"]["roots"] == {
        "app_data": "present", "repository": "present", "resources": "present",
    }
    assert "str(tmp_path)" not in encoded
    assert str(tmp_path).lower() not in encoded
    for forbidden in ("secret", "token", "pid", "origin", "port", "c:\\", "f:\\"):
        assert forbidden not in encoded


def test_manifest_revision_is_stable_and_changes_only_with_facts(tmp_path) -> None:
    first = _manifest(tmp_path)
    second = _manifest(tmp_path)
    changed = _manifest(tmp_path, features=("isolated-python-artifact", "runtime-diagnostics"))

    assert first["manifest_revision"] == second["manifest_revision"]
    assert first["manifest_revision"] != changed["manifest_revision"]


def test_turn_context_uses_same_turn_payload_and_does_not_touch_capabilities(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    payloads = InMemoryTurnPayloadStore()
    capabilities = ("workbench.question.answer",)

    context = freeze_runtime_self_manifest_for_turn(
        manifest, turn_id="turn-runtime-1", payloads=payloads, disclosure="model",
    )

    assert context is not None
    assert context.payload_ref.startswith("crp://session/turn-runtime-1/runtime-self-manifest-v1/")
    assert context.entry.kind == "runtime_self_manifest"
    assert context.entry.payload_ref == context.payload_ref
    assert context.entry.disclosure == "model"
    assert context.entry.content_bytes > 0
    assert payloads.get(context.payload_ref)["manifest_revision"] == context.manifest_revision
    assert capabilities == ("workbench.question.answer",)


def test_optional_manifest_degrades_without_blocking_turn() -> None:
    assert freeze_runtime_self_manifest_for_turn(
        None, turn_id="turn-runtime-2", payloads=InMemoryTurnPayloadStore(),
    ) is None


def test_authenticated_projection_revalidates_and_rejects_sensitive_content(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    projection = authenticated_runtime_self_manifest_projection(manifest)
    assert projection == manifest

    tampered = dict(manifest)
    tampered["origin"] = "http://127.0.0.1:9999"
    with pytest.raises(RuntimeSelfManifestError):
        authenticated_runtime_self_manifest_projection(tampered)
