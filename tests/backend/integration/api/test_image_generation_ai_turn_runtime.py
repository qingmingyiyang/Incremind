from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

from backend.api.ai_runtime import build_ai_runtime
from backend.api.image_generation_ai_runtime import (
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_GENERATION_OUTCOME,
)
from backend.model_route_context import model_route_provider_context_from_record
from backend.model_routing_profile import ModelRoutingProfileStore
from backend.providers import ProviderRegistry
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.product_core.model_route_registry import ModelRouteRegistry
from core.product_core.model_route_runtime import ModelRouteRuntimeService
from core.storage_provider import GeneratedAssetAuthority


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL6qQAAAABJRU5ErkJggg=="
)


class _ImageGenerationLoopback:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                content_length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(content_length))
                fixture.requests.append({
                    "path": self.path,
                    "model": payload.get("model"),
                    "response_format": payload.get("response_format"),
                    "has_authorization": "Authorization" in self.headers,
                })
                response = json.dumps({
                    "data": [{
                        "b64_json": base64.b64encode(_PNG_1X1).decode("ascii"),
                        "media_type": "image/png",
                    }]
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


class _NoSecrets:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, key: str) -> str:
        self.calls.append(key)
        return ""


def test_image_generation_turn_uses_one_keyless_loopback_post_and_replays_after_restart(tmp_path) -> None:
    loopback = _ImageGenerationLoopback()
    loopback.start()
    try:
        _activate_image_generation(tmp_path, base_url=loopback.base_url)
        secrets = _NoSecrets()
        runtime = build_ai_runtime(SimpleNamespace(root_dir=tmp_path, secret_store=secrets))
        request = _request()

        waiting = runtime.submit_turn(request)
        assert waiting.status == "waiting_approval"
        completed = _approve(runtime, waiting)

        assert completed.status == "completed"
        assert secrets.calls == []
        assert loopback.requests == [{
            "path": "/v1/images/generations",
            "model": "fixture-image-model",
            "response_format": "b64_json",
            "has_authorization": False,
        }]
        presentation = runtime.presentation_for(completed.turn_id)
        assert presentation is not None
        serialized_presentation = json.dumps(presentation, ensure_ascii=False).casefold()
        assert all(term not in serialized_presentation for term in (
            "private prompt", "url", "path", "bytes", "b64", "base64",
        ))
        assert presentation["asset_ref"].startswith("crp-ref-default-assets-generated-")

        completed_event = next(
            event for event in reversed(tuple(runtime.events_after(completed.turn_id)))
            if event["type"] == "tool.completed"
            and event["data"]["capability_id"] == IMAGE_GENERATION_CAPABILITY
        )
        receipt_ref = completed_event["data"]["receipt_ref"]
        assert isinstance(receipt_ref, str)
        receipt = runtime._payloads.get(receipt_ref)  # noqa: SLF001 -- inspect durable Turn receipt.
        serialized_receipt = json.dumps(receipt, ensure_ascii=False).casefold()
        assert all(term not in serialized_receipt for term in (
            "private prompt", "url", "path", "bytes", "b64", "base64",
        ))
        objects = _object_store(tmp_path)
        records = objects.list("generated_assets")
        assert len(records) == 1
        asset_id = records[0]["id"]
        assert isinstance(asset_id, str)
        # The authority owns bytes while the Turn only keeps references and
        # metadata.
        asset = GeneratedAssetAuthority(
            object_store=objects,
            vault_root=tmp_path / "library",
        ).get(asset_id)
        assert asset is not None
        assert asset.media_type == "image/png"
        assert (asset.dimensions.width, asset.dimensions.height) == (1, 1)
        assert asset.turn_id == request["turn_id"]
        assert asset.operation_id == request["operation_id"]

        replayed = runtime.submit_turn(request)
        restarted = build_ai_runtime(SimpleNamespace(root_dir=tmp_path, secret_store=secrets))
        recovered = restarted.submit_turn(request)
        assert replayed.replayed is True and recovered.replayed is True
        assert loopback.requests and len(loopback.requests) == 1
    finally:
        loopback.close()


def _activate_image_generation(root, *, base_url: str) -> None:
    provider = ProviderRegistry(root).create({
        "provider_id": "fixture-image-provider",
        "name": "fixture-image-provider",
        "llm_provider": "openai",
        "base_url": base_url,
        "api_path": "/images/generations",
        "model": "fixture-image-model",
        "models": ["fixture-image-model"],
        "enabled": True,
    }, fallback={})
    context = model_route_provider_context_from_record(root, provider)
    registry = ModelRouteRegistry(root)
    registry.update(
        "tier.image_generation",
        {
            "provider_id": "fixture-image-provider",
            "model_name": "fixture-image-model",
            "adapter_kind": "openai-compatible-image-generation",
            "enabled": True,
            "reason": "image generation loopback integration fixture",
        },
        expected_registry_revision=0,
        provider=provider,
        egress_consented=True,
    )
    route_runtime = ModelRouteRuntimeService(root)
    preview = route_runtime.preview(
        route_keys=["tier.image_generation"],
        compatibility={"tier.image_generation": context},
        providers=[context],
    )
    route_runtime.activate(
        shadow_token=str(preview["shadow_token"]),
        route_keys=preview["route_keys"],
        expected_runtime_revision=0,
        confirm=True,
        compatibility={"tier.image_generation": context},
        providers=[context],
    )
    ModelRoutingProfileStore(root).update(
        expected_revision=1,
        rules_version=1,
        text_default_tier="standard",
        tier_routes={
            "fast": None,
            "standard": None,
            "deep": None,
            "vision": None,
            "image_generation": "tier.image_generation",
        },
    )
    boundary = ProjectBoundaryProfileStore(root).update(
        "project-image",
        expected_revision=0,
        mode="open",
        remote_default="allow",
    )
    ProjectCapabilityProfileStore(root).update(
        "project-image",
        expected_revision=0,
        boundary_profile_id=boundary.profile.profile_id,
        boundary_profile_revision=boundary.profile.revision,
        preferred_model_tier="standard",
    )


def _request() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "turn_id": "turn-image-generation-loopback-001",
        "session_id": "session-image-generation-loopback-001",
        "operation_id": "image-generation-loopback-operation-001",
        "idempotency_key": "image-generation-loopback-turn-001",
        "scope": {"kind": "project", "project_id": "project-image", "series_id": None},
        "input": {"kind": "text", "text": "private prompt: a tiny blue square", "refs": []},
        "desired_outcome": IMAGE_GENERATION_OUTCOME,
        "privacy": {
            "mode": "remote_allowed",
            "allow_remote": True,
            "pii": "none",
            "consent_refs": [],
            "retention": "local_durable",
        },
        "capability_policy": {
            "allowed": [IMAGE_GENERATION_CAPABILITY],
            "denied": [],
            "require_approval": [IMAGE_GENERATION_CAPABILITY],
        },
        "context_policy": {
            "include_project_skill": False,
            "include_memory": False,
            "include_session_history": False,
            "max_context_bytes": 4096,
        },
        "approval_policy": {"mode": "explicit", "auto_approve_read_only": True},
        "created_at": "2026-08-25T00:00:00+00:00",
    }


def _approve(runtime, waiting):
    approval = next(
        event for event in reversed(tuple(runtime.events_after(waiting.turn_id)))
        if event["type"] == "approval.required"
    )
    return runtime.apply_action({
        "schema_version": "1.0.0",
        "action_id": "action-image-generation-loopback-001",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval["event_id"],
        "reason": "generate image",
        "actor": "user",
        "expected_sequence": approval["sequence"],
        "idempotency_key": "approve-image-generation-loopback-001",
        "created_at": "2026-08-25T00:00:01+00:00",
    })


def _object_store(root):
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store

    return build_rebuild_object_store(root)[0]
