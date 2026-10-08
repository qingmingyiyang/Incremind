from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from backend.api import image_generation_ai_runtime as runtime
from core.ai_kernel import InMemoryTurnPayloadStore
from core.ai_kernel.dispatcher import ToolProviderFailure
from core.model_gateway import ImageGenerationResult
from core.storage_provider import GeneratedAssetAuthority, JsonObjectStore


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL6qQAAAABJRU5ErkJggg=="
)


class _Gateway:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error
        self.requests = []

    def generate(self, request):
        self.calls += 1
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ImageGenerationResult(
            capability="image_generation",
            image_bytes=_PNG_1X1,
            media_type="image/png",
            provider="provider-visible-name",
            model="provider-visible-model",
        )


class _Resolver:
    def __init__(self, gateway, *, error: Exception | None = None) -> None:
        self.gateway = gateway
        self.error = error
        self.calls: list[dict[str, object]] = []

    def resolve(self, *, project_id, routing_snapshot, privacy_scope):
        self.calls.append({
            "project_id": project_id,
            "routing_snapshot": routing_snapshot,
            "privacy_scope": privacy_scope,
        })
        if self.error is not None:
            raise self.error
        return self.gateway


def _request(**overrides) -> dict[str, object]:
    values: dict[str, object] = {
        "turn_id": "turn-image-a",
        "operation_id": "operation-image-a",
        "invocation_id": "invocation-image-a",
        "scope": {"kind": "project", "project_id": "project-a"},
        "privacy": {"mode": "remote_allowed", "allow_remote": True},
        "arguments": {"prompt": "private prompt text", "parameters": {"size": "1024x1024"}},
    }
    values.update(overrides)
    return values


def _capability(monkeypatch, tmp_path, gateway, *, assets=None, resolver=None):
    payloads = InMemoryTurnPayloadStore()
    monkeypatch.setattr(
        runtime,
        "load_turn_model_routing_binding",
        lambda *_args, **_kwargs: SimpleNamespace(
            snapshot_ref="crp://default/model-routing/snapshot-a",
            snapshot_revision="a" * 64,
            snapshot={"selected": {
                "provider_id": "provider-a", "provider_revision": "provider-revision-a",
                "model_name": "model-image-a", "route_revision": 3,
            }},
        ),
    )
    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "legacy")
    authority = assets or GeneratedAssetAuthority(
        object_store=objects,
        vault_root=tmp_path / "vault",
    )
    resolver = resolver or _Resolver(gateway)
    return payloads, runtime.ImageGenerationCapability(
        gateway_resolver=resolver,
        assets=authority,
        payloads=payloads,
        operations=runtime.ObjectStoreImageGenerationOperationEvidence(object_store=objects),
    ), resolver


def test_image_generation_ingests_asset_before_safe_receipt(monkeypatch, tmp_path) -> None:
    gateway = _Gateway()
    payloads, capability, resolver = _capability(monkeypatch, tmp_path, gateway)
    request = _request()

    first = capability.invoke(request)
    assert gateway.calls == 1
    assert first["result"]["content"]["asset_ref"].startswith("crp-ref-default-assets-generated-")
    assert first["result"]["content"]["replayed"] is False
    receipt = payloads.get(first["receipt_ref"])
    serialized = json.dumps(receipt, ensure_ascii=False)
    assert "private prompt text" not in serialized
    assert all(term not in serialized.casefold() for term in ("prompt", "url", "secret", "path", "bytes"))
    assert receipt["asset_ref"] == first["result"]["content"]["asset_ref"]
    assert resolver.calls[0] == {
        "project_id": "project-a",
        "routing_snapshot": {
            "selected": {
                "provider_id": "provider-a", "provider_revision": "provider-revision-a",
                "model_name": "model-image-a", "route_revision": 3,
            },
        },
        "privacy_scope": "remote_allowed",
    }


def test_new_invocation_does_not_write_operation_lifecycle(monkeypatch, tmp_path) -> None:
    gateway = _Gateway()
    _payloads, capability, _resolver = _capability(monkeypatch, tmp_path, gateway)

    capability.invoke(_request())

    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "legacy")
    records = objects.list("image_generation_operations")
    assert records == ()


def test_provider_failure_is_unknown_and_asset_probe_does_not_recall_provider(monkeypatch, tmp_path) -> None:
    gateway = _Gateway(error=ConnectionError("provider connection lost"))
    _payloads, capability, _resolver = _capability(monkeypatch, tmp_path, gateway)
    request = _request()

    with pytest.raises(ToolProviderFailure) as first:
        capability.invoke(request)
    assert first.value.effect_certainty == "unknown"
    assert capability.recover_completed_invocation(request) is None
    assert gateway.calls == 1


def test_invalid_request_fails_before_provider_with_confirmed_none(monkeypatch, tmp_path) -> None:
    gateway = _Gateway()
    _payloads, capability, _resolver = _capability(monkeypatch, tmp_path, gateway)
    request = _request(arguments={"parameters": {}})

    with pytest.raises(ToolProviderFailure) as failure:
        capability.invoke(request)

    assert failure.value.effect_certainty == "confirmed_none"
    assert gateway.calls == 0


def test_gateway_resolution_failure_is_confirmed_none_before_provider(monkeypatch, tmp_path) -> None:
    gateway = _Gateway()
    resolver = _Resolver(gateway, error=RuntimeError("route is unavailable"))
    _payloads, capability, observed_resolver = _capability(
        monkeypatch, tmp_path, gateway, resolver=resolver,
    )

    with pytest.raises(ToolProviderFailure) as failure:
        capability.invoke(_request())

    assert failure.value.error_code == "image_generation.gateway_unavailable"
    assert failure.value.effect_certainty == "confirmed_none"
    assert gateway.calls == 0
    assert len(observed_resolver.calls) == 1


def test_asset_persistence_failure_after_provider_is_unknown(monkeypatch, tmp_path) -> None:
    class _BrokenAssets:
        def store(self, **_kwargs):
            raise OSError("vault unavailable")

    gateway = _Gateway()
    _payloads, capability, _resolver = _capability(monkeypatch, tmp_path, gateway, assets=_BrokenAssets())

    with pytest.raises(ToolProviderFailure) as failure:
        capability.invoke(_request())

    assert failure.value.error_code == "image_generation.effect_unknown"
    assert failure.value.effect_certainty == "unknown"
    assert gateway.calls == 1


def test_completed_asset_recovers_after_runtime_restart_without_second_provider_call(monkeypatch, tmp_path) -> None:
    gateway = _Gateway()
    payloads, first_runtime, resolver = _capability(monkeypatch, tmp_path, gateway)
    first = first_runtime.invoke(_request())
    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "legacy")
    assets = GeneratedAssetAuthority(object_store=objects, vault_root=tmp_path / "vault")
    restarted = runtime.ImageGenerationCapability(
        gateway_resolver=resolver,
        assets=assets,
        payloads=payloads,
        operations=runtime.ObjectStoreImageGenerationOperationEvidence(object_store=objects),
    )

    replay = restarted.recover_completed_invocation(_request())

    assert gateway.calls == 1
    assert replay is not None
    assert replay["result"]["content"]["asset_ref"] == first["result"]["content"]["asset_ref"]
    assert replay["result"]["content"]["replayed"] is True


def test_completed_operation_provider_recovery_uses_durable_receipt_without_gateway_call(monkeypatch, tmp_path) -> None:
    gateway = _Gateway()
    payloads, first_runtime, resolver = _capability(monkeypatch, tmp_path, gateway)
    first = first_runtime.invoke(_request())
    resolver_calls = len(resolver.calls)
    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "legacy")
    restarted = runtime.ImageGenerationCapability(
        gateway_resolver=resolver,
        assets=GeneratedAssetAuthority(object_store=objects, vault_root=tmp_path / "vault"),
        payloads=payloads,
        operations=runtime.ObjectStoreImageGenerationOperationEvidence(object_store=objects),
    )

    recovered = restarted.recover_completed_invocation(_request())

    assert recovered is not None
    assert recovered["result"]["content"]["asset_ref"] == first["result"]["content"]["asset_ref"]
    assert recovered["result"]["content"]["replayed"] is True
    assert gateway.calls == 1
    assert len(resolver.calls) == resolver_calls


def test_unknown_effect_without_asset_stays_quarantined_after_restart(monkeypatch, tmp_path) -> None:
    gateway = _Gateway(error=ConnectionError("provider connection lost"))
    _payloads, first_runtime, resolver = _capability(monkeypatch, tmp_path, gateway)
    with pytest.raises(ToolProviderFailure):
        first_runtime.invoke(_request())
    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "legacy")
    restarted = runtime.ImageGenerationCapability(
        gateway_resolver=resolver,
        assets=GeneratedAssetAuthority(object_store=objects, vault_root=tmp_path / "vault"),
        payloads=InMemoryTurnPayloadStore(),
        operations=runtime.ObjectStoreImageGenerationOperationEvidence(object_store=objects),
    )

    assert restarted.recover_completed_invocation(_request()) is None
    assert gateway.calls == 1


def test_unknown_operation_provider_recovery_without_asset_stays_quarantined(monkeypatch, tmp_path) -> None:
    gateway = _Gateway(error=ConnectionError("provider connection lost"))
    _payloads, capability, resolver = _capability(monkeypatch, tmp_path, gateway)
    with pytest.raises(ToolProviderFailure):
        capability.invoke(_request())
    resolver_calls = len(resolver.calls)

    recovered = capability.recover_completed_invocation(_request())

    assert recovered is None
    assert gateway.calls == 1
    assert len(resolver.calls) == resolver_calls


def test_crash_after_blob_before_metadata_commit_reconciles_across_instances_without_provider_retry(monkeypatch, tmp_path) -> None:
    gateway = _Gateway()
    payloads, first_runtime, resolver = _capability(monkeypatch, tmp_path, gateway)
    assets = first_runtime._assets  # noqa: SLF001 -- fault injection at the authority seam.
    original_mark_stored = assets._mark_stored  # noqa: SLF001

    def crash_after_blob(_prepared):
        raise OSError("simulated crash after blob write")

    monkeypatch.setattr(assets, "_mark_stored", crash_after_blob)
    with pytest.raises(ToolProviderFailure) as initial:
        first_runtime.invoke(_request())
    assert initial.value.effect_certainty == "unknown"
    prepared = assets.find_by_operation(turn_id="turn-image-a", operation_id="operation-image-a")
    assert len(prepared) == 1 and prepared[0].status == "preparing"
    monkeypatch.setattr(assets, "_mark_stored", original_mark_stored)

    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "legacy")
    restarted = runtime.ImageGenerationCapability(
        gateway_resolver=resolver,
        assets=GeneratedAssetAuthority(object_store=objects, vault_root=tmp_path / "vault"),
        payloads=payloads,
        operations=runtime.ObjectStoreImageGenerationOperationEvidence(object_store=objects),
    )

    resolver_calls = len(resolver.calls)
    recovered = restarted.recover_completed_invocation(_request())

    assert recovered is not None
    assert gateway.calls == 1
    assert recovered["result"]["content"]["replayed"] is True
    assert len(resolver.calls) == resolver_calls
    reconciled = assets.find_by_operation(turn_id="turn-image-a", operation_id="operation-image-a")
    assert reconciled[0].status == "stored"
