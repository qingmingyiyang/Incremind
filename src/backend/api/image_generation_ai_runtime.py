"""Receipt-required, replay-safe image-generation capability.

The capability runs through the governed AI Tool path.  A provider returns
only ephemeral bytes, the generated-asset authority ingests them, and only
then is a safe Turn receipt published.  Core Effect is the sole mutable
execution-state authority; historical operation records are read-only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Protocol

from backend.api.turn_model_routing_binding import (
    TurnModelRoutingBinding,
    load_turn_model_routing_binding,
)
from core.ai_kernel import TurnPayloadStorePort, validate_turn_presentation_artifact
from core.ai_kernel.dispatcher import ToolProviderFailure
from core.model_gateway import (
    ImageGenerationGatewayPort,
    ImageGenerationRequest,
    ImageGenerationResult,
)
from core.storage_provider import (
    GeneratedAssetAuthority,
    GeneratedAssetDimensions,
    ObjectStorePort,
)


IMAGE_GENERATION_CAPABILITY = "image.generate"
IMAGE_GENERATION_OUTCOME = "image.generation.completed"
_RECEIPT_KIND = "image-generation-receipt"
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_ASSET_REF = re.compile(r"^crp-ref-[A-Za-z0-9._-]+-assets-generated-generated-[a-f0-9]{32}$")
_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_OPERATION_COLLECTION = "image_generation_operations"
_OPERATION_SCHEMA_VERSION = "1.0.0"
_OPERATION_KIND = "image_generation_operation_evidence"
_SENSITIVE_KEYS = frozenset({
    "prompt", "url", "uri", "secret", "token", "api_key", "apikey",
    "authorization", "password", "path", "bytes", "content", "endpoint",
})


@dataclass(frozen=True, slots=True)
class ImageGenerationOperationEvidence:
    """Safe replay state.  No prompt, URL, bytes, path, or secret is retained."""

    turn_id: str
    project_id: str
    operation_id: str
    invocation_id: str
    state: str
    asset_ref: str | None = None
    media_type: str | None = None
    dimensions: GeneratedAssetDimensions | None = None
    receipt_ref: str | None = None
    revision: int = 0


class ImageGenerationOperationEvidencePort(Protocol):
    """Read-only compatibility for historical operation evidence."""

    def get(self, *, turn_id: str, operation_id: str) -> ImageGenerationOperationEvidence | None: ...


class ImageGenerationGatewayResolverPort(Protocol):
    """Resolve a gateway only from the immutable Turn routing authority."""

    def resolve(
        self,
        *,
        project_id: str,
        routing_snapshot: Mapping[str, object],
        privacy_scope: str,
    ) -> ImageGenerationGatewayPort: ...


class ObjectStoreImageGenerationOperationEvidence:
    """Read historical operation evidence; never create or mutate it."""

    def __init__(self, *, object_store: ObjectStorePort) -> None:
        self._objects = object_store

    def get(self, *, turn_id: str, operation_id: str) -> ImageGenerationOperationEvidence | None:
        turn_id, operation_id = _identity(turn_id, "turn id"), _identity(operation_id, "operation id")
        payload = self._objects.read(_OPERATION_COLLECTION, _operation_object_id(turn_id, operation_id))
        return None if payload is None else _evidence_from_payload(payload, revision=self._objects.revision(_OPERATION_COLLECTION, _operation_object_id(turn_id, operation_id)))



class ImageGenerationCapability:
    """Generate exactly once per operation and return only an asset reference."""

    def __init__(
        self,
        *,
        gateway_resolver: ImageGenerationGatewayResolverPort,
        assets: GeneratedAssetAuthority,
        payloads: TurnPayloadStorePort,
        operations: ImageGenerationOperationEvidencePort | None = None,
    ) -> None:
        self._gateway_resolver = gateway_resolver
        self._assets = assets
        self._payloads = payloads
        # Read-only compatibility for operation records written by builds
        # before Core Effect became the sole execution-state authority.
        self._operations = operations

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        turn_id, project_id, operation_id, invocation_id, arguments = _request_identity(request)
        try:
            routing = load_turn_model_routing_binding(
                self._payloads,
                request,
                required_capability="image_generation",
            )
        except ToolProviderFailure:
            raise
        except Exception as error:
            raise ToolProviderFailure(
                "image_generation.routing_unavailable",
                effect_certainty="confirmed_none",
            ) from error
        prompt, parameters, privacy_scope, input_image_b64 = _arguments(arguments, request)
        try:
            gateway = self._gateway_resolver.resolve(
                project_id=project_id,
                routing_snapshot=routing.snapshot,
                privacy_scope=privacy_scope,
            )
            if not callable(getattr(gateway, "generate", None)):
                raise TypeError("image generation gateway is invalid")
        except Exception as error:
            raise ToolProviderFailure(
                "image_generation.gateway_unavailable",
                effect_certainty="confirmed_none",
            ) from error
        image_request = ImageGenerationRequest(
            capability="image_generation",
            prompt=prompt,
            parameters=parameters,
            privacy_scope=privacy_scope,
            input_image_b64=input_image_b64,
        )
        try:
            result = gateway.generate(image_request)
        except Exception as error:
            raise ToolProviderFailure(
                "image_generation.provider_unknown",
                effect_certainty="unknown",
            ) from error
        try:
            asset, dimensions = self._store_asset(
                result=result,
                routing=routing,
                turn_id=turn_id,
                project_id=project_id,
                operation_id=operation_id,
                invocation_id=invocation_id,
                privacy_scope=privacy_scope,
            )
            receipt = _receipt(
                turn_id=turn_id,
                project_id=project_id,
                operation_id=operation_id,
                invocation_id=invocation_id,
                asset_ref=asset.asset_ref,
                asset_sha256=asset.sha256,
                byte_count=asset.byte_count,
                media_type=asset.media_type,
                routing=routing,
            )
            receipt_ref = self._payloads.put(turn_id, _RECEIPT_KIND, receipt)
            evidence = ImageGenerationOperationEvidence(
                turn_id=turn_id,
                project_id=project_id,
                operation_id=operation_id,
                invocation_id=invocation_id,
                state="completed",
                asset_ref=asset.asset_ref,
                media_type=result.media_type,
                dimensions=dimensions,
                receipt_ref=receipt_ref,
            )
        except ToolProviderFailure:
            raise
        except Exception as error:
            raise ToolProviderFailure(
                "image_generation.effect_unknown",
                effect_certainty="unknown",
            ) from error
        return self._result(evidence, replayed=False)

    def recover_completed_invocation(
        self,
        request: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        """Recover only from durable local evidence, without Provider work.

        The AI Kernel calls this port under the original frozen intent,
        resource locks and authorization fence. Returning ``None`` preserves
        quarantine; it never means the remote effect was absent or retryable.
        """

        try:
            turn_id, _project_id, operation_id, invocation_id, _arguments_value = (
                _request_identity(request)
            )
            if self._operations is not None:
                existing = self._operations.get(
                    turn_id=turn_id,
                    operation_id=operation_id,
                )
                if existing is not None and existing.invocation_id == invocation_id and existing.state == "completed":
                    return self._result(existing, replayed=True)
            return self._recover_from_asset(request)
        except (ToolProviderFailure, KeyError, TypeError, ValueError):
            return None

    def _store_asset(
        self,
        *,
        result: ImageGenerationResult,
        routing: TurnModelRoutingBinding,
        turn_id: str,
        project_id: str,
        operation_id: str,
        invocation_id: str,
        privacy_scope: str,
    ):
        selected = routing.snapshot.get("selected")
        if not isinstance(selected, Mapping):
            raise ValueError("image generation route selection is unavailable")
        provider_id = _identity(selected.get("provider_id"), "provider id")
        provider_revision = _revision_identity(
            selected.get("provider_revision"), "provider revision",
        )
        model_id = _identity(selected.get("model_name"), "model id")
        dimensions = _dimensions(result.image_bytes, result.media_type)
        asset = self._assets.store(
            content=result.image_bytes,
            sha256=hashlib.sha256(result.image_bytes).hexdigest(),
            media_type=result.media_type,
            dimensions=dimensions,
            project_id=project_id,
            turn_id=turn_id,
            operation_id=operation_id,
            invocation_id=invocation_id,
            provider_id=provider_id,
            provider_revision=provider_revision,
            model_id=model_id,
            model_revision=str(_positive(selected.get("route_revision"), "model revision")),
            # The asset authority accepts opaque receipt identities rather
            # than URI payload references.  This binds the durable asset to
            # the frozen routing evidence without persisting its location.
            receipt_refs=(f"image-routing-{routing.snapshot_revision}",),
            provenance={
                "generation_kind": "image",
                "privacy_scope": privacy_scope,
                "route_snapshot_revision": routing.snapshot_revision,
            },
        )
        return asset, dimensions

    def _recover_from_asset(self, request: Mapping[str, object]) -> Mapping[str, object]:
        """Finalize a prepared asset only; never retry a provider invocation."""
        turn_id, project_id, operation_id, invocation_id, _arguments_value = _request_identity(request)
        scope = request.get("scope")
        if not isinstance(scope, Mapping) or scope.get("project_id") != project_id:
            raise ToolProviderFailure(
                "image_generation.needs_reconcile",
                effect_certainty="unknown",
            )
        candidates = tuple(
            item for item in self._assets.find_by_operation(
                turn_id=turn_id,
                operation_id=operation_id,
            )
            if item.invocation_id == invocation_id
        )
        if len(candidates) != 1:
            raise ToolProviderFailure(
                "image_generation.needs_reconcile",
                effect_certainty="unknown",
            )
        try:
            asset = self._assets.reconcile(candidates[0].asset_id)
            if asset.status != "stored":
                raise ValueError("generated image blob is unavailable")
            routing = load_turn_model_routing_binding(
                self._payloads,
                request,
                required_capability="image_generation",
            )
            _assert_asset_matches_routing(asset, routing)
            receipt_ref = self._payloads.put(
                turn_id,
                _RECEIPT_KIND,
                _receipt(
                    turn_id=turn_id,
                    project_id=project_id,
                    operation_id=operation_id,
                    invocation_id=invocation_id,
                    asset_ref=asset.asset_ref,
                    asset_sha256=asset.sha256,
                    byte_count=asset.byte_count,
                    media_type=asset.media_type,
                    routing=routing,
                ),
            )
            completed = ImageGenerationOperationEvidence(
                turn_id=turn_id,
                project_id=project_id,
                operation_id=operation_id,
                invocation_id=invocation_id,
                state="completed",
                asset_ref=asset.asset_ref,
                media_type=asset.media_type,
                dimensions=asset.dimensions,
                receipt_ref=receipt_ref,
            )
        except Exception as error:
            if isinstance(error, ToolProviderFailure):
                raise
            raise ToolProviderFailure(
                "image_generation.needs_reconcile",
                effect_certainty="unknown",
            ) from error
        if completed.state != "completed":
            raise ToolProviderFailure(
                "image_generation.needs_reconcile",
                effect_certainty="unknown",
            )
        return self._result(completed, replayed=True)

    def _result(self, evidence: ImageGenerationOperationEvidence, *, replayed: bool) -> Mapping[str, object]:
        if (
            evidence.asset_ref is None
            or evidence.media_type is None
            or evidence.dimensions is None
            or evidence.receipt_ref is None
        ):
            raise ToolProviderFailure("image_generation.needs_reconcile", effect_certainty="unknown")
        content = {
            "operation_id": evidence.operation_id,
            "invocation_id": evidence.invocation_id,
            "asset_ref": evidence.asset_ref,
            "media_type": evidence.media_type,
            "dimensions": {"width": evidence.dimensions.width, "height": evidence.dimensions.height},
            "replayed": replayed,
        }
        artifact = validate_turn_presentation_artifact({
            "schema_version": "1.0.0",
            "kind": IMAGE_GENERATION_OUTCOME,
            "content": content,
        })
        return {
            "summary": "generated image asset is available",
            "receipt_ref": evidence.receipt_ref,
            "payload_ref": None,
            "evidence_refs": [evidence.asset_ref],
            "result": artifact,
        }


class ImageGenerationTurnPlanner:
    """Map one explicit image-generation outcome to the governed Tool."""

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        completed = next(
            (event for event in reversed(events) if event.get("type") == "tool.completed"),
            None,
        )
        if completed is not None:
            data = completed.get("data")
            if not isinstance(data, Mapping):
                raise ValueError("image generation terminal event is invalid")
            return {
                "type": "complete",
                "summary": str(data.get("summary") or "generated image asset is available"),
                "payload_ref": data.get("payload_ref"),
                "evidence_refs": list(data.get("evidence_refs") or ()),
            }
        input_payload = request.get("input")
        prompt = input_payload.get("text") if isinstance(input_payload, Mapping) else None
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("image generation prompt is required")
        return {
            "type": "tool",
            "capability_id": IMAGE_GENERATION_CAPABILITY,
            "arguments": {"prompt": prompt.strip(), "parameters": {}},
        }


def _operation_object_id(turn_id: str, operation_id: str) -> str:
    identity = f"{turn_id}\x00{operation_id}".encode("utf-8")
    return f"image-generation-operation-{hashlib.sha256(identity).hexdigest()[:32]}"


def _evidence_from_payload(value: Mapping[str, object], *, revision: int) -> ImageGenerationOperationEvidence:
    if set(value) != {
        "schema_version", "kind", "state", "turn_id", "project_id", "operation_id",
        "invocation_id", "asset_ref", "media_type", "dimensions", "receipt_ref",
    } or value.get("schema_version") != _OPERATION_SCHEMA_VERSION or value.get("kind") != _OPERATION_KIND:
        raise ValueError("image generation operation evidence is invalid")
    _reject_sensitive(value)
    dimensions_value = value.get("dimensions")
    dimensions = None
    if dimensions_value is not None:
        if not isinstance(dimensions_value, Mapping) or set(dimensions_value) != {"width", "height"}:
            raise ValueError("image generation operation dimensions are invalid")
        dimensions = GeneratedAssetDimensions(
            _positive(dimensions_value.get("width"), "operation width"),
            _positive(dimensions_value.get("height"), "operation height"),
        )
    return _validate_operation_evidence(ImageGenerationOperationEvidence(
        turn_id=_identity(value.get("turn_id"), "turn id"),
        project_id=_identity(value.get("project_id"), "project id"),
        operation_id=_identity(value.get("operation_id"), "operation id"),
        invocation_id=_identity(value.get("invocation_id"), "invocation id"),
        state=value.get("state") if isinstance(value.get("state"), str) else "",
        asset_ref=value.get("asset_ref") if isinstance(value.get("asset_ref"), str) else None,
        media_type=value.get("media_type") if isinstance(value.get("media_type"), str) else None,
        dimensions=dimensions,
        receipt_ref=value.get("receipt_ref") if isinstance(value.get("receipt_ref"), str) else None,
    ), revision=revision)


def _validate_operation_evidence(
    evidence: ImageGenerationOperationEvidence,
    *,
    revision: int,
) -> ImageGenerationOperationEvidence:
    turn_id = _identity(evidence.turn_id, "turn id")
    project_id = _identity(evidence.project_id, "project id")
    operation_id = _identity(evidence.operation_id, "operation id")
    invocation_id = _identity(evidence.invocation_id, "invocation id")
    if evidence.state not in {"started", "completed", "unknown_effect"}:
        raise ValueError("image generation operation state is invalid")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("image generation operation revision is invalid")
    if evidence.state == "completed":
        if (
            not isinstance(evidence.asset_ref, str)
            or not _ASSET_REF.fullmatch(evidence.asset_ref)
            or evidence.media_type not in _MEDIA_TYPES
            or evidence.dimensions is None
            or not isinstance(evidence.receipt_ref, str)
            or not evidence.receipt_ref.startswith("crp://")
        ):
            raise ValueError("completed image generation operation is invalid")
        _positive(evidence.dimensions.width, "operation width")
        _positive(evidence.dimensions.height, "operation height")
    elif any(value is not None for value in (
        evidence.asset_ref, evidence.media_type, evidence.dimensions, evidence.receipt_ref,
    )):
        raise ValueError("incomplete image generation operation contains output")
    return ImageGenerationOperationEvidence(
        turn_id=turn_id,
        project_id=project_id,
        operation_id=operation_id,
        invocation_id=invocation_id,
        state=evidence.state,
        asset_ref=evidence.asset_ref,
        media_type=evidence.media_type,
        dimensions=evidence.dimensions,
        receipt_ref=evidence.receipt_ref,
        revision=revision,
    )


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).strip().lower().replace("-", "_") in _SENSITIVE_KEYS:
                raise ValueError("image generation operation contains sensitive data")
            _reject_sensitive(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive(nested)


def _request_identity(request: Mapping[str, object]) -> tuple[str, str, str, str, Mapping[str, object]]:
    turn_id = _identity(request.get("turn_id"), "turn id")
    operation_id = _identity(request.get("operation_id"), "operation id")
    explicit_invocation = request.get("invocation_id")
    tool_call_id = request.get("tool_call_id")
    if tool_call_id is not None and explicit_invocation is not None and explicit_invocation != tool_call_id:
        raise ToolProviderFailure(
            "image_generation.invalid_request", effect_certainty="confirmed_none",
        )
    invocation_value = tool_call_id if tool_call_id is not None else explicit_invocation
    invocation_id = _identity(invocation_value, "invocation id")
    scope = request.get("scope")
    if not isinstance(scope, Mapping):
        raise ToolProviderFailure("image_generation.invalid_scope", effect_certainty="confirmed_none")
    project_id = _identity(scope.get("project_id"), "project id")
    arguments = request.get("arguments")
    if not isinstance(arguments, Mapping):
        raise ToolProviderFailure("image_generation.invalid_request", effect_certainty="confirmed_none")
    return turn_id, project_id, operation_id, invocation_id, arguments


def _arguments(arguments: Mapping[str, object], request: Mapping[str, object]) -> tuple[str, Mapping[str, object], str, str | None]:
    if set(arguments) - {"prompt", "parameters", "input_image_b64"}:
        raise ToolProviderFailure("image_generation.invalid_request", effect_certainty="confirmed_none")
    prompt = arguments.get("prompt")
    parameters = arguments.get("parameters", {})
    privacy = request.get("privacy")
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(parameters, Mapping) or not isinstance(privacy, Mapping):
        raise ToolProviderFailure("image_generation.invalid_request", effect_certainty="confirmed_none")
    privacy_scope = privacy.get("mode")
    if privacy_scope not in {"local_only", "remote_allowed"}:
        raise ToolProviderFailure("image_generation.invalid_privacy", effect_certainty="confirmed_none")
    image_b64 = arguments.get("input_image_b64")
    if image_b64 is not None and not isinstance(image_b64, str):
        raise ToolProviderFailure("image_generation.invalid_request", effect_certainty="confirmed_none")
    return prompt.strip(), dict(parameters), privacy_scope, image_b64


def _receipt(*, turn_id: str, project_id: str, operation_id: str, invocation_id: str, asset_ref: str, asset_sha256: str, byte_count: int, media_type: str, routing: TurnModelRoutingBinding) -> dict[str, object]:
    selected = routing.snapshot.get("selected")
    if not isinstance(selected, Mapping):
        raise ValueError("image generation route selection is unavailable")
    return {
        "schema_version": "1.0.0",
        "kind": _RECEIPT_KIND,
        "turn_id": turn_id,
        "project_id": project_id,
        "operation_id": operation_id,
        "invocation_id": invocation_id,
        "status": "completed",
        "asset_ref": asset_ref,
        "asset_sha256": asset_sha256,
        "byte_count": byte_count,
        "media_type": media_type,
        "provider_id": _identity(selected.get("provider_id"), "provider id"),
        "provider_revision": _text(selected.get("provider_revision"), "provider revision"),
        "model_id": _identity(selected.get("model_name"), "model id"),
        "route_snapshot_ref": routing.snapshot_ref,
        "route_snapshot_revision": routing.snapshot_revision,
    }


def _assert_asset_matches_routing(asset, routing: TurnModelRoutingBinding) -> None:
    """Do not publish a recovered blob against a different frozen route."""

    selected = routing.snapshot.get("selected")
    if not isinstance(selected, Mapping):
        raise ValueError("image generation route selection is unavailable")
    if (
        asset.provider_id != _identity(selected.get("provider_id"), "provider id")
        or asset.provider_revision != _revision_identity(selected.get("provider_revision"), "provider revision")
        or asset.model_id != _identity(selected.get("model_name"), "model id")
        or asset.model_revision != str(_positive(selected.get("route_revision"), "model revision"))
    ):
        raise ValueError("recovered generated asset route drifted")


def _dimensions(content: bytes, media_type: str) -> GeneratedAssetDimensions:
    if media_type == "image/png" and len(content) >= 24 and content[:8] == b"\x89PNG\r\n\x1a\n" and content[12:16] == b"IHDR":
        return GeneratedAssetDimensions(int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big"))
    if media_type == "image/jpeg":
        index = 2
        while index + 9 <= len(content):
            if content[index] != 0xFF:
                index += 1
                continue
            while index < len(content) and content[index] == 0xFF:
                index += 1
            if index >= len(content):
                break
            marker, index = content[index], index + 1
            if marker in {0xD8, 0xD9} or index + 2 > len(content):
                continue
            size = int.from_bytes(content[index:index + 2], "big")
            if size < 2 or index + size > len(content):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                return GeneratedAssetDimensions(int.from_bytes(content[index + 5:index + 7], "big"), int.from_bytes(content[index + 3:index + 5], "big"))
            index += size
    if media_type == "image/webp" and len(content) >= 30 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        kind = content[12:16]
        if kind == b"VP8X":
            return GeneratedAssetDimensions(int.from_bytes(content[24:27], "little") + 1, int.from_bytes(content[27:30], "little") + 1)
        if kind == b"VP8 " and content[23:26] == b"\x9d\x01\x2a":
            return GeneratedAssetDimensions(int.from_bytes(content[26:28], "little") & 0x3FFF, int.from_bytes(content[28:30], "little") & 0x3FFF)
    raise ValueError("image generation dimensions are unavailable")


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ToolProviderFailure("image_generation.invalid_request", effect_certainty="confirmed_none")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"image generation {label} is invalid")
    return value.strip()


def _revision_identity(value: object, label: str) -> str:
    revision = _text(value, label)
    return f"revision-{hashlib.sha256(revision.encode('utf-8')).hexdigest()}"


def _positive(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"image generation {label} is invalid")
    return value
