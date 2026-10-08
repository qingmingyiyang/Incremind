from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from core.ai_kernel import (
    CapabilityDefinition,
    ContextCompaction,
    ContextEntry,
    ContextManifest,
    ContextManifestError,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
    V1TurnContextManifestResolver,
    context_manifest_from_payload,
    context_manifest_to_payload,
    validate_context_manifest_for_request,
)


ROOT = Path(__file__).resolve().parents[2]


class _CompletePlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        context_event = next(event for event in events if event["type"] == "context.resolved")
        context = payloads.get(context_event["data"]["payload_ref"])
        capability_manifest = payloads.get(context["capability_manifest_ref"])
        assert capability_manifest["capability_ids"] == ["memory.recall"]
        return {"type": "complete", "summary": "done"}


class _CrossTurnContextResolver:
    def resolve(self, request, capability_manifest_ref, capability_manifest):
        manifest = V1TurnContextManifestResolver().resolve(
            request, capability_manifest_ref, capability_manifest
        )
        return replace(
            manifest,
            capability_manifest_ref="crp://session/turn-other/capability-manifest/ref",
        )


def test_context_manifest_schema_and_roundtrip_are_strict() -> None:
    schema = json.loads(
        (ROOT / "core-contracts" / "ai" / "turn-context-manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    request = _request()
    capability_manifest = _capability_manifest(request)
    ref = f"crp://session/{request['turn_id']}/capability-manifest/ref"
    manifest = V1TurnContextManifestResolver().resolve(request, ref, capability_manifest)
    payload = context_manifest_to_payload(manifest)
    assert Draft202012Validator(schema).is_valid(payload)
    assert context_manifest_from_payload(payload) == manifest
    with pytest.raises(ContextManifestError, match="shape"):
        context_manifest_from_payload({**payload, "body": "must not be accepted"})


def test_default_manifest_records_refs_without_copying_input_body() -> None:
    request = _request()
    request["input"]["refs"] = [
        {"kind": "project_skill", "object_id": "skill-a", "uri": "crp://skills/project-a/skill-a"}
    ]
    manifest = V1TurnContextManifestResolver().resolve(
        request,
        f"crp://session/{request['turn_id']}/capability-manifest/ref",
        _capability_manifest(request),
    )
    encoded = json.dumps(context_manifest_to_payload(manifest), ensure_ascii=False)
    assert request["input"]["text"] not in encoded
    assert "crp://skills/project-a/skill-a" in encoded
    assert manifest.entries[-1].disclosure == "reference_only"
    assert manifest.entries[-1].payload_ref is None


def test_runtime_self_manifest_is_diagnostic_context_not_a_capability() -> None:
    request = _request()
    capability_manifest = _capability_manifest(request)
    baseline = V1TurnContextManifestResolver().resolve(
        request,
        f"crp://session/{request['turn_id']}/capability-manifest/ref",
        capability_manifest,
    )
    diagnostic = ContextEntry(
        entry_id="context-entry-runtime-self-manifest",
        kind="runtime_self_manifest",
        source_ref="crp://runtime/default/self-manifest/rsm-test",
        payload_ref=f"crp://session/{request['turn_id']}/runtime-self-manifest/rsm-test",
        source_project_id=None,
        revision_identity="rsm-test",
        content_fingerprint=None,
        provenance_refs=(),
        disclosure="model",
        selection_reason="host_runtime_diagnostics",
        content_bytes=0,
    )
    manifest = replace(baseline, entries=(*baseline.entries, diagnostic))

    assert validate_context_manifest_for_request(
        manifest, request, capability_manifest=capability_manifest
    ) == manifest
    assert capability_manifest.capability_ids == ("memory.recall",)
    assert context_manifest_to_payload(manifest)["entries"][-1]["kind"] == "runtime_self_manifest"


def test_manifest_enforces_utf8_budget_and_compaction_lineage() -> None:
    request = _request()
    baseline = V1TurnContextManifestResolver().resolve(
        request,
        f"crp://session/{request['turn_id']}/capability-manifest/ref",
        _capability_manifest(request),
    )
    model_entry = ContextEntry(
        entry_id="memory-r1-a",
        kind="memory_r1",
        source_ref="crp://memory/project-alpha/r1-a",
        payload_ref=f"crp://session/{request['turn_id']}/memory-r1/a",
        source_project_id="project-alpha",
        revision_identity="rev-7",
        content_fingerprint="existing-fingerprint",
        provenance_refs=("crp://sources/project-alpha/source-a",),
        disclosure="model",
        selection_reason="project_memory_recall",
        content_bytes=len("中文".encode("utf-8")),
    )
    output_entry = replace(
        model_entry,
        entry_id="memory-r1-compact",
        kind="context_summary",
        payload_ref=f"crp://session/{request['turn_id']}/memory-r1/compact",
        content_bytes=3,
    )
    compaction = ContextCompaction(
        compaction_id="compact-1",
        strategy="deterministic_excerpt",
        source_entry_ids=(model_entry.entry_id,),
        output_entry_id=output_entry.entry_id,
        input_bytes=6,
        output_bytes=3,
    )
    manifest = replace(
        baseline,
        entries=(*baseline.entries, model_entry, output_entry),
        compactions=(compaction,),
        selected_context_bytes=9,
    )
    assert validate_context_manifest_for_request(manifest, request) == manifest
    with pytest.raises(ContextManifestError, match="exceeds byte budget"):
        context_manifest_to_payload(
            replace(manifest, selected_context_bytes=manifest.max_context_bytes + 1)
        )
    with pytest.raises(ContextManifestError, match="unknown entries"):
        context_manifest_to_payload(
            replace(manifest, compactions=(replace(compaction, output_entry_id="missing"),))
        )
    with pytest.raises(ContextManifestError, match="byte accounting drifted"):
        context_manifest_to_payload(
            replace(manifest, compactions=(replace(compaction, output_bytes=2),))
        )
    with pytest.raises(ContextManifestError, match="output is invalid"):
        context_manifest_to_payload(
            replace(manifest, entries=(*baseline.entries, model_entry, replace(output_entry, kind="memory_r1")))
        )


def test_runtime_persists_context_manifest_and_scopes_planner_to_its_payload_refs() -> None:
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(
        planner=_CompletePlanner(),
        registry=_registry(),
        events=events,
        payloads=payloads,
    )
    request = _request()
    receipt = runtime.submit_turn(request)
    assert receipt.status == "completed"
    context_event = next(
        event for event in events.events_after(str(request["turn_id"]))
        if event["type"] == "context.resolved"
    )
    context = payloads.get(context_event["data"]["payload_ref"])
    assert context["turn_id"] == request["turn_id"]
    assert context["project_id"] == "project-alpha"
    assert context["project_profile_revision"] == 1


def test_cross_turn_context_ref_fails_before_planner() -> None:
    runtime = SynchronousAIRuntime(
        planner=_CompletePlanner(),
        registry=_registry(),
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
        context_manifest_resolver=_CrossTurnContextResolver(),
    )
    receipt = runtime.submit_turn(_request())
    assert receipt.status == "failed"
    assert tuple(runtime.events_after(receipt.turn_id))[-1]["type"] == "turn.failed"


def _registry() -> ScopedCapabilityRegistry:
    registry = ScopedCapabilityRegistry()
    registry.register(
        CapabilityDefinition(
            "memory.recall", 1, "read", False, "read_only",
            "crp://default/contracts/in.schema.json",
            "crp://default/contracts/out.schema.json",
        ),
        _Provider(),
    )
    return registry


class _Provider:
    def invoke(self, request):
        return {"summary": "unused"}


def _capability_manifest(request):
    from core.ai_kernel import V1TurnPolicyCapabilityManifestResolver

    return V1TurnPolicyCapabilityManifestResolver().resolve(request, _registry().list())


def _request() -> dict[str, object]:
    return json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(
            encoding="utf-8"
        )
    )
