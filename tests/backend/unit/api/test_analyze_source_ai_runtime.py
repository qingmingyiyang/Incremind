from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from backend.api.analyze_source_ai_runtime import (
    AnalyzeSourceCapability,
    AnalyzeSourceReadiness,
    ArtifactSourceManifestResolver,
    PlatformArtifactSourceManifestResolver,
    ResolvedSourceManifest,
    analyze_source_capability_definition,
)
from core.ai_kernel import (
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    InMemoryTurnStateStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
    ToolExecutionBoundaryDecision,
)
from core.job_runner import SQLiteJobRecord
from core.media_hands import MediaHandsAdmission, SourcePermissionSnapshot
from core.source_processing import (
    PlatformResolver,
    SourceManifestArtifactRepository,
    SourceManifestCodec,
    SourcePermissionAuthority,
)
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[4]


class _Resolver:
    def __init__(self, fixture: str, *, permission: str | None = None) -> None:
        value = json.loads((ROOT / f"core-contracts/rebuild/source-processing/fixtures/{fixture}").read_text(encoding="utf-8"))
        if permission is not None:
            value["permission"]["decision"] = permission
        self.manifest = SourceManifestCodec.decode(value)
        self.calls = 0

    def resolve(self, arguments, scope):
        assert arguments["intent"] == "organize"
        assert scope["project_id"] == "project-1"
        self.calls += 1
        manifest_ref = f"crp://default/source-manifests/{self.manifest.source_id}-r1"
        snapshot = None
        if self.manifest.permission.decision == "granted":
            snapshot = SourcePermissionSnapshot(
                project_id="project-1",
                manifest_ref=manifest_ref,
                manifest_revision="manifest-r1",
                grant_ref="crp://default/source-permissions/projects/project-1/fixture-permission/r1",
                grant_revision="r1",
                revocation_generation=0,
            )
        return ResolvedSourceManifest(
            self.manifest,
            manifest_ref,
            "manifest-r1",
            ("crp://default/evidence/resolver-r1",),
            permission_snapshot=snapshot,
        )


class _FixturePlatformProvider:
    def __init__(self, fixture: str, platform: str | None = None) -> None:
        self.fixture = fixture
        self.platform = platform or ("xiaohongshu" if fixture.startswith("xiaohongshu") else "bilibili")

    def provide(self, text, *, project_id):
        del text
        assert project_id == "project-1"
        value = json.loads(
            (ROOT / f"core-contracts/rebuild/source-processing/fixtures/{self.fixture}").read_text(
                encoding="utf-8"
            )
        )
        value["platform"] = self.platform
        return SourceManifestCodec.decode(value)


class _Provisioner:
    def __init__(self) -> None:
        self.calls = []

    def provision(self, **kwargs):
        self.calls.append(kwargs)
        job_id = f"media_hands:{kwargs['manifest'].source_id}:analyze_source"
        return MediaHandsAdmission(SQLiteJobRecord({"id": job_id}, 7), False)


class _ToolPlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        del capabilities, payloads, execution_control
        if any(event["type"] == "tool.completed" for event in events):
            return {"type": "complete", "summary": "source intake done"}
        return {"type": "tool", "capability_id": "analyze_source", "arguments": _request()["arguments"]}


class _AskBoundary:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, request, capability, decision):
        del request, capability
        self.calls += 1
        return ToolExecutionBoundaryDecision(
            "ask", ("test_user_approval",), (), 1, True, False, dict(decision["arguments"]),
        )


def _request():
    return {
        "turn_id": "turn-1", "tool_call_id": "tool-call-1", "operation_id": "operation-1",
        "idempotency_key": "idempotency-key-0001", "scope": {"project_id": "project-1"},
        "arguments": {
            "input": {"kind": "text", "text": "fixture share text", "source_ref": None},
            "intent": "organize",
            "output_profile": {"profile_id": "default", "revision": "1"},
            "resource_budget": {"max_assets": 8, "max_bytes": 1000, "max_seconds": 20},
        },
    }


def _capability(resolver, provisioner, payloads, readiness):
    return AnalyzeSourceCapability(
        resolver=resolver,
        provisioner=provisioner,
        payloads=payloads,
        readiness=lambda: readiness,
        created_at=lambda: "2026-08-25T00:00:00Z",
        namespace_id="default",
    )


def _contract_errors(name: str, value: object):
    schema = json.loads((ROOT / f"core-contracts/ai/{name}").read_text(encoding="utf-8"))
    return list(Draft202012Validator(schema).iter_errors(value))


def test_native_tool_definition_is_local_receipted_and_unavailable_by_default() -> None:
    definition = analyze_source_capability_definition()
    tool = definition.tool_definition
    assert tool is not None
    assert definition.capability_id == tool.tool_id == "analyze_source"
    assert definition.mode == tool.effect == "write"
    assert definition.operation_semantics == tool.operation_semantics == "receipt_required"
    assert definition.input_schema_uri == tool.input_schema_uri
    assert definition.output_schema_uri == tool.output_schema_uri
    assert tool.destination == "platform" and tool.egress_class == "remote"
    assert tool.network_scope == ("platform_metadata_endpoint",)
    assert tool.data_egress_scope == ("source_url",)
    assert not tool.available
    registry = ScopedCapabilityRegistry()
    registration = registry.register(definition, object())
    assert registry.get("analyze_source") == definition
    registration.close()


def test_disabled_readiness_returns_receipt_without_resolve_or_provision() -> None:
    resolver, provisioner, payloads = _Resolver("bilibili-video.json"), _Provisioner(), InMemoryTurnPayloadStore()
    capability = _capability(resolver, provisioner, payloads, AnalyzeSourceReadiness(False, False, False))
    result = capability.invoke(_request())
    assert result["result"]["reason"] == "feature_disabled"
    assert result["receipt_ref"].startswith("crp://")
    assert resolver.calls == 0 and provisioner.calls == []


def test_invalid_nested_arguments_fail_before_readiness_or_resolver() -> None:
    resolver, provisioner, payloads = _Resolver("bilibili-video.json"), _Provisioner(), InMemoryTurnPayloadStore()
    capability = _capability(resolver, provisioner, payloads, AnalyzeSourceReadiness(True, True, True))
    request = _request()
    request["arguments"]["resource_budget"]["max_assets"] = 0
    try:
        capability.invoke(request)
    except ValueError as error:
        assert "resource budget" in str(error)
    else:
        raise AssertionError("invalid nested arguments must fail closed")
    assert resolver.calls == 0 and provisioner.calls == []
    request = _request()
    request["arguments"]["input"] = {"kind": "source_ref", "text": None, "source_ref": "crp://"}
    try:
        capability.invoke(request)
    except ValueError as error:
        assert "source reference" in str(error)
    else:
        raise AssertionError("uncontrolled CRP reference must fail closed")
    assert resolver.calls == 0 and provisioner.calls == []


def test_artifact_resolver_reads_only_the_turn_project_manifest(tmp_path) -> None:
    value = json.loads(
        (ROOT / "core-contracts/rebuild/source-processing/fixtures/bilibili-video.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = SourceManifestCodec.decode(value)
    repository = SourceManifestArtifactRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", namespace_id="default"),
        namespace_id="default",
    )
    artifact = repository.put(
        project_id="project-1",
        manifest_id="bilibili-video-r1",
        manifest=manifest,
    )
    resolver = ArtifactSourceManifestResolver(repository)
    request = _request()
    request["arguments"]["input"] = {
        "kind": "source_ref",
        "text": None,
        "source_ref": artifact.public_ref,
    }

    resolved = resolver.resolve(request["arguments"], request["scope"])

    assert resolved.manifest == manifest
    assert resolved.manifest_ref == artifact.public_ref
    assert resolved.manifest_revision == "r1"
    assert set(resolved.evidence_refs).issuperset(manifest.provenance_refs)
    with pytest.raises(ValueError):
        resolver.resolve(request["arguments"], {"project_id": "project-2"})


def test_terminal_manifest_returns_receipt_without_provision() -> None:
    resolver, provisioner, payloads = _Resolver("xiaohongshu-unknown.json"), _Provisioner(), InMemoryTurnPayloadStore()
    capability = _capability(resolver, provisioner, payloads, AnalyzeSourceReadiness(True, True, True))
    result = capability.invoke(_request())
    assert result["result"]["status"] == "terminal"
    assert result["result"]["reason"] == "unknown_content_kind"
    assert provisioner.calls == []
    receipt = payloads.get(result["receipt_ref"])
    assert receipt["outcome"] == result["result"]
    assert _contract_errors("analyze-source-result.schema.json", result["result"]) == []
    assert _contract_errors("analyze-source-receipt.schema.json", receipt) == []


def test_denied_and_unresolved_permission_never_provision() -> None:
    for decision, reason in (("denied", "source_permission_denied"), ("unknown", "source_permission_unresolved")):
        resolver = _Resolver("bilibili-video.json", permission=decision)
        provisioner, payloads = _Provisioner(), InMemoryTurnPayloadStore()
        result = _capability(
            resolver, provisioner, payloads, AnalyzeSourceReadiness(True, True, True),
        ).invoke(_request())
        assert result["result"]["reason"] == reason
        assert provisioner.calls == []


def test_admission_propagates_kernel_idempotency_and_replays_receipt() -> None:
    resolver, provisioner, payloads = _Resolver("bilibili-video.json"), _Provisioner(), InMemoryTurnPayloadStore()
    capability = _capability(resolver, provisioner, payloads, AnalyzeSourceReadiness(True, True, True))
    first = capability.invoke(_request())
    second = capability.invoke(_request())

    assert first["result"]["status"] == "admitted"
    assert first["result"]["job_revision"] == 7
    assert first["receipt_ref"] == second["receipt_ref"]
    assert resolver.calls == 1 and len(provisioner.calls) == 1
    assert provisioner.calls[0]["idempotency_key"] == _request()["idempotency_key"]
    assert provisioner.calls[0]["operation"] == "analyze_source"
    assert "crp://default/evidence/resolver-r1" in first["evidence_refs"]
    receipt = payloads.get(first["receipt_ref"])
    assert _contract_errors("analyze-source-result.schema.json", first["result"]) == []
    assert _contract_errors("analyze-source-receipt.schema.json", receipt) == []

    drifted = _request()
    drifted["operation_id"] = "operation-drifted"
    try:
        capability.invoke(drifted)
    except ValueError as error:
        assert "identity drift" in str(error)
    else:
        raise AssertionError("immutable receipt identity drift must fail closed")


def test_kernel_boundary_runs_once_and_approval_reuses_the_frozen_decision() -> None:
    resolver, provisioner, payloads = _Resolver("bilibili-video.json"), _Provisioner(), InMemoryTurnPayloadStore()
    capability = _capability(resolver, provisioner, payloads, AnalyzeSourceReadiness(False, False, False))
    registry = ScopedCapabilityRegistry()
    registry.register(analyze_source_capability_definition(available=True), capability)
    events = InMemoryTurnEventStore()
    boundary = _AskBoundary()
    runtime = SynchronousAIRuntime(
        planner=_ToolPlanner(), registry=registry, events=events, payloads=payloads,
        state=InMemoryTurnStateStore(), execution_boundary=boundary,
    )
    turn_request = json.loads(
        (ROOT / "core-contracts/ai/fixtures/turn-request/valid-project-answer.json").read_text(encoding="utf-8")
    )
    turn_request["capability_policy"] = {
        "allowed": ["analyze_source"], "denied": [], "require_approval": ["analyze_source"],
    }
    waiting = runtime.submit_turn(turn_request)
    assert waiting.status == "waiting_approval" and boundary.calls == 1
    approval_event = tuple(events.events_after(waiting.turn_id))[-1]
    completed = runtime.apply_action({
        "schema_version": "1.0.0", "action_id": "action-analyze-source-0000000000001",
        "turn_id": waiting.turn_id, "type": "approve", "target_event_id": approval_event["event_id"],
        "reason": "approved", "actor": "user", "expected_sequence": waiting.current_sequence,
        "idempotency_key": "approve-analyze-source-0001", "created_at": "2026-08-25T00:00:01Z",
    })
    assert completed.status == "completed"
    assert boundary.calls == 1
    assert resolver.calls == 0 and provisioner.calls == []
    tool_completed = next(event for event in events.events_after(waiting.turn_id) if event["type"] == "tool.completed")
    assert tool_completed["data"]["receipt_ref"].startswith("crp://")


@pytest.mark.parametrize(
    ("text", "platform", "fixture", "content_kind"),
    [
        ("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili", "bilibili-video.json", "video"),
        ("小红书 分享", "xiaohongshu", "xiaohongshu-image-set.json", "image_set"),
        ("小红书 分享", "xiaohongshu", "xiaohongshu-video.json", "video"),
        ("小红书 分享", "xiaohongshu", "xiaohongshu-mixed.json", "mixed"),
    ],
)
def test_platform_resolver_persists_one_project_scoped_manifest_artifact(
    tmp_path, text: str, platform: str, fixture: str, content_kind: str
) -> None:
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    artifacts = SourceManifestArtifactRepository(store, namespace_id="default")
    resolver = PlatformArtifactSourceManifestResolver(
        artifacts=artifacts,
        platforms=PlatformResolver({platform: _FixturePlatformProvider(fixture)}),
        permissions=SourcePermissionAuthority(store, namespace_id="default"),
    )
    request = _request()
    request["arguments"]["input"] = {"kind": "text", "text": text, "source_ref": None}
    first = resolver.resolve(request["arguments"], request["scope"])
    second = resolver.resolve(request["arguments"], request["scope"])

    assert first == second
    assert first.manifest is not None and first.manifest.content_kind == content_kind
    assert first.manifest_ref is not None and "/projects/project-1/" in first.manifest_ref
    with pytest.raises(ValueError):
        ArtifactSourceManifestResolver(artifacts).resolve(
            {"input": {"kind": "source_ref", "text": None, "source_ref": first.manifest_ref}},
            {"project_id": "project-2"},
        )


def test_controlled_credential_rotation_creates_a_new_immutable_manifest_artifact(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")

    class Controlled:
        revision = 0

        def provide_controlled(self, text, *, project_id, credential_subject_id):
            assert project_id == "project-1" and credential_subject_id == "account-a"
            self.revision += 1
            value = json.loads((
                ROOT / "core-contracts/rebuild/source-processing/fixtures/xiaohongshu-image-set.json"
            ).read_text(encoding="utf-8"))
            value["schema_version"] = "1.1.0"
            value["platform"] = "xiaohongshu"
            if "other-note" in text:
                value["source_id"] = "xhs-other-note"
            value["credential_binding"] = {
                "mode": "controlled_credential", "provider": "xiaohongshu",
                "credential_subject_id": "account-a",
                "authorization_ref": "crp://controlled-credentials/xiaohongshu/0123456789abcdef0123456789abcdef",
                "authorization_revision": self.revision,
                "secret_generation": self.revision,
                "boundary_profile_id": "boundary-a", "boundary_profile_revision": 3,
            }
            return SourceManifestCodec.decode(value)

    controlled = Controlled()
    resolver = PlatformArtifactSourceManifestResolver(
        artifacts=SourceManifestArtifactRepository(store, namespace_id="default"),
        platforms=PlatformResolver({"xiaohongshu": _FixturePlatformProvider("xiaohongshu-image-set.json")}),
        permissions=SourcePermissionAuthority(store, namespace_id="default"),
        controlled_xiaohongshu=controlled,
    )
    request = _request()
    request["arguments"]["input"] = {
        "kind": "text", "text": "https://www.xiaohongshu.com/explore/65f1234567890abc12345678",
        "source_ref": None,
    }
    request["arguments"]["access"] = {
        "mode": "controlled_credential", "credential_subject_id": "account-a",
    }
    first = resolver.resolve(request["arguments"], request["scope"])
    second = resolver.resolve(request["arguments"], request["scope"])
    assert first.manifest_ref != second.manifest_ref
    assert first.manifest.credential_binding.authorization_revision == 1
    assert second.manifest.credential_binding.authorization_revision == 2
    request["arguments"]["input"]["text"] = "https://www.xiaohongshu.com/explore/other-note"
    third = resolver.resolve(request["arguments"], request["scope"])
    assert third.manifest_ref not in {first.manifest_ref, second.manifest_ref}


@pytest.mark.parametrize(
    ("text", "providers", "reason"),
    [
        ("ordinary local note", {}, "unknown_platform"),
        (
            "https://bilibili.com/video/a https://xiaohongshu.com/explore/b",
            {"bilibili": _FixturePlatformProvider("bilibili-video.json")},
            "ambiguous_platform",
        ),
        ("https://bilibili.com/video/a", {}, "provider_unavailable"),
    ],
)
def test_platform_terminal_outcomes_create_no_job(
    tmp_path, text: str, providers: dict, reason: str
) -> None:
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    resolver = PlatformArtifactSourceManifestResolver(
        artifacts=SourceManifestArtifactRepository(store, namespace_id="default"),
        platforms=PlatformResolver(providers),
        permissions=SourcePermissionAuthority(store, namespace_id="default"),
    )
    request = _request()
    request["arguments"]["input"] = {"kind": "text", "text": text, "source_ref": None}
    provisioner = _Provisioner()
    result = _capability(
        resolver, provisioner, InMemoryTurnPayloadStore(), AnalyzeSourceReadiness(True, True, True)
    ).invoke(request)
    assert result["result"]["status"] == "terminal"
    assert result["result"]["reason"] == reason
    assert provisioner.calls == []


def test_granted_manifest_without_media_provider_is_terminal_before_job(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    artifacts = SourceManifestArtifactRepository(store, namespace_id="default")
    permissions = SourcePermissionAuthority(store, namespace_id="default")

    class UnknownXiaohongshuProvider:
        def provide(self, text, *, project_id):
            del text
            assert project_id == "project-1"
            value = json.loads(
                (ROOT / "core-contracts/rebuild/source-processing/fixtures/xiaohongshu-image-set.json").read_text(
                    encoding="utf-8"
                )
            )
            value["platform"] = "xiaohongshu"
            value["permission"] = {
                "decision": "unknown",
                "evidence_refs": [
                    "crp://default/source-resolution-evidence/projects/project-1/xhs-images-r1"
                ],
            }
            value["provenance_refs"] = value["permission"]["evidence_refs"]
            for asset in value["assets"]:
                asset["evidence_refs"] = value["permission"]["evidence_refs"]
            return SourceManifestCodec.decode(value)

    resolver = PlatformArtifactSourceManifestResolver(
        artifacts=artifacts,
        platforms=PlatformResolver({"xiaohongshu": UnknownXiaohongshuProvider()}),
        permissions=permissions,
        executable_platforms=frozenset({"bilibili"}),
    )
    request = _request()
    request["arguments"]["input"] = {
        "kind": "text",
        "text": "https://www.xiaohongshu.com/explore/65f1234567890abc12345678",
        "source_ref": None,
    }
    initial = resolver.resolve(request["arguments"], request["scope"])
    assert initial.manifest is not None and initial.manifest.permission.decision == "unknown"
    assert initial.terminal_reason is None
    metadata_ref = initial.manifest.permission.evidence_refs[0]
    permissions.grant(
        project_id="project-1",
        permission_id="xhs-images-permission",
        source_id=initial.manifest.source_id,
        platform="xiaohongshu",
        source_manifest_ref=initial.manifest_ref,
        source_manifest_revision=initial.manifest_revision,
        metadata_evidence_ref=metadata_ref,
        actor_id="test-user",
        command_id="grant-xhs-images",
        created_at="2026-08-25T00:00:00Z",
        expected_revision=0,
    )
    provisioner = _Provisioner()
    result = _capability(
        resolver,
        provisioner,
        InMemoryTurnPayloadStore(),
        AnalyzeSourceReadiness(True, True, True),
    ).invoke(request)
    assert result["result"]["status"] == "terminal"
    assert result["result"]["reason"] == "media_provider_unavailable"
    assert provisioner.calls == []
