from __future__ import annotations

import copy
import json
from pathlib import Path
from threading import Event, Thread

from backend.security.ai_tool_execution_boundary import (
    AIToolExecutionBoundary,
    TurnCapabilityBindingGuard,
)
from backend.api.ai_profile_resolvers import (
    ProjectAwareCapabilityManifestResolver,
    ProjectAwareContextManifestResolver,
    TurnProjectProfileSnapshotAuthority,
)
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_boundary import BoundaryGrant, EphemeralTokenVault
from core.ai_kernel import (
    CapabilityDefinition,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    InMemoryTurnStateStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
)
from core.ai_tooling import ToolConnectionIdentity, ToolDefinition, ToolRetryPolicy


ROOT = Path(__file__).resolve().parents[4]


def test_remote_soft_pii_is_tokenized_before_boundary_returns_arguments(tmp_path: Path) -> None:
    profiles = ProjectBoundaryProfileStore(tmp_path)
    profiles.update("project-alpha", mode="open", remote_default="allow", expected_revision=0)
    boundary = AIToolExecutionBoundary(profiles)
    result = boundary.evaluate(
        _remote_turn("answer.remote"),
        _capability("answer.remote", "external", approval=False),
        {"type": "tool", "capability_id": "answer.remote", "arguments": {"prompt": "联系 alice@example.com"}},
    )
    assert result.outcome == "allow_redacted"
    assert result.redaction_required is True
    assert "alice@example.com" not in repr(result.arguments)
    assert "[[CRP:EMAIL:" in str(result.arguments["prompt"])


def test_remote_hard_secret_is_denied_and_not_returned(tmp_path: Path) -> None:
    boundary = AIToolExecutionBoundary(ProjectBoundaryProfileStore(tmp_path))
    secret = "sk-abcdefghijklmnopqrstuvwx"
    result = boundary.evaluate(
        _remote_turn("answer.remote"),
        _capability("answer.remote", "external", approval=False),
        {"type": "tool", "capability_id": "answer.remote", "arguments": {"prompt": secret}},
    )
    assert result.outcome == "deny"
    assert result.arguments == {}
    assert secret not in repr(result)


def test_frozen_hot_path_sanitizer_blocks_secret_without_boundary_evaluation(tmp_path: Path) -> None:
    boundary = AIToolExecutionBoundary(ProjectBoundaryProfileStore(tmp_path))
    capability = _capability("answer.remote", "external", approval=False)

    assert boundary.sanitize_candidate_arguments(
        capability,
        {"prompt": "sk-abcdefghijklmnopqrstuvwx"},
        turn_id="turn-0123456789abcdef0123456789abcdef",
    ) is None
    sanitized = boundary.sanitize_candidate_arguments(
        capability,
        {"prompt": "联系 alice@example.com"},
        turn_id="turn-0123456789abcdef0123456789abcdef",
    )
    assert sanitized is not None
    assert "alice@example.com" not in repr(sanitized)
    assert "[[CRP:EMAIL:" in str(sanitized["prompt"])


def test_local_read_is_allowed_without_argument_transformation(tmp_path: Path) -> None:
    result = AIToolExecutionBoundary(ProjectBoundaryProfileStore(tmp_path)).evaluate(
        _turn(),
        _capability("memory.recall", "read", approval=False, semantics="read_only"),
        {"type": "tool", "capability_id": "memory.recall", "arguments": {"query": "项目状态"}},
    )
    assert result.outcome == "allow"
    assert result.arguments == {"query": "项目状态"}


def test_native_mcp_tool_binds_redaction_token_to_server_identity(tmp_path: Path) -> None:
    profiles = ProjectBoundaryProfileStore(tmp_path)
    profiles.update("project-alpha", mode="open", remote_default="allow", expected_revision=0)
    vault = EphemeralTokenVault()
    boundary = AIToolExecutionBoundary(profiles, vault=vault)

    result = boundary.evaluate(
        _remote_turn("calendar.read"),
        _mcp_capability(),
        {"type": "tool", "capability_id": "calendar.read", "arguments": {"query": "联系 alice@example.com"}},
    )

    assert result.outcome == "allow_redacted"
    tokenized = str(result.arguments["query"])
    assert vault.rehydrate_exact(
        tokenized,
        turn_id=str(_turn()["turn_id"]),
            destination_id=(
                "mcp:calendar-server:p2025-11-25:m1:ecalendar-local:"
                "cpersonal-calendar:g1:r1:s1"
            ),
        trusted_projection=True,
    ) == "联系 alice@example.com"


def test_native_mcp_grant_does_not_survive_connection_identity_change(tmp_path: Path) -> None:
    profiles = ProjectBoundaryProfileStore(tmp_path)
    profiles.update(
        "project-alpha",
        mode="guarded",
        remote_default="review",
        persistent_grants=(BoundaryGrant(
            grant_id="grant-calendar-old",
            subject_id="ai-kernel",
            project_id="project-alpha",
            target_id=(
                "calendar.read@mcp:calendar-server:p2025-11-25:m1:eold-endpoint:"
                "cpersonal-calendar:g1:r1:s1"
            ),
            actions=("read",),
            data_classes=("calendar_event",),
            destinations=("mcp",),
            expires_at=None,
            revision=1,
        ),),
        expected_revision=0,
    )

    result = AIToolExecutionBoundary(profiles).evaluate(
        _remote_turn("calendar.read"),
        _mcp_capability(),
        {"type": "tool", "capability_id": "calendar.read", "arguments": {}},
    )

    assert result.outcome == "allow"
    assert result.matched_grant_ids == ()
    assert "persistent_grant_matched" not in result.reason_codes


def test_persisted_turn_binding_blocks_boundary_widening_before_provider_invoke(tmp_path: Path) -> None:
    capability_profiles = ProjectCapabilityProfileStore(tmp_path)
    boundary_profiles = ProjectBoundaryProfileStore(tmp_path)
    boundary_profiles.update(
        "project-alpha", mode="guarded", remote_default="review", expected_revision=0
    )
    capability_profiles.update(
        "project-alpha",
        expected_revision=0,
        boundary_profile_id="project-boundary-project-alpha",
        boundary_profile_revision=1,
    )
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    provider = _CountingProvider()
    registry = ScopedCapabilityRegistry()
    capability = _capability("answer.remote", "external", approval=False)
    registry.register(capability, provider)
    snapshots = TurnProjectProfileSnapshotAuthority(capability_profiles, boundary_profiles)
    runtime = SynchronousAIRuntime(
        planner=_BoundaryWideningPlanner(boundary_profiles),
        registry=registry,
        events=events,
        payloads=payloads,
        state=InMemoryTurnStateStore(),
        manifest_resolver=ProjectAwareCapabilityManifestResolver(snapshots),
        context_manifest_resolver=ProjectAwareContextManifestResolver(snapshots),
        execution_boundary=AIToolExecutionBoundary(
            boundary_profiles,
            binding_guard=TurnCapabilityBindingGuard(
                capability_profiles, boundary_profiles, events=events, payloads=payloads,
            ),
        ),
    )
    request = _remote_turn("answer.remote")

    receipt = runtime.submit_turn(request)

    assert receipt.status == "failed"
    assert provider.calls == 0
    boundary_event = next(event for event in events.events_after(receipt.turn_id) if event["type"] == "tool.requested")
    decision = payloads.get(boundary_event["data"]["payload_ref"])
    assert decision["outcome"] == "deny"
    assert decision["reason_codes"] == ["ai.boundary_binding_drift"]


def test_binding_guard_leaves_global_v1_fallback_unaffected(tmp_path: Path) -> None:
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    profiles = ProjectBoundaryProfileStore(tmp_path)
    boundary = AIToolExecutionBoundary(
        profiles,
        binding_guard=TurnCapabilityBindingGuard(
            ProjectCapabilityProfileStore(tmp_path), profiles, events=events, payloads=payloads,
        ),
    )
    request = _turn()
    request["scope"] = {"kind": "global", "project_id": None, "series_id": None}

    result = boundary.evaluate(
        request,
        _capability("memory.recall", "read", approval=False, semantics="read_only"),
        {"type": "tool", "capability_id": "memory.recall", "arguments": {"query": "status"}},
    )

    assert result.outcome == "allow"


def test_final_dispatch_fence_blocks_mutation_after_initial_boundary_decision(tmp_path: Path) -> None:
    capability_profiles = ProjectCapabilityProfileStore(tmp_path)
    boundary_profiles = ProjectBoundaryProfileStore(tmp_path)
    boundary_profiles.update("project-alpha", mode="guarded", remote_default="review", expected_revision=0)
    capability_profiles.update(
        "project-alpha", expected_revision=0,
        boundary_profile_id="project-boundary-project-alpha", boundary_profile_revision=1,
    )
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    provider = _CountingProvider()
    registry = ScopedCapabilityRegistry()
    registry.register(_capability("memory.recall", "read", approval=False, semantics="read_only"), provider)
    snapshots = TurnProjectProfileSnapshotAuthority(capability_profiles, boundary_profiles)
    guard = TurnCapabilityBindingGuard(
        capability_profiles, boundary_profiles, events=events, payloads=payloads,
    )
    runtime = SynchronousAIRuntime(
        planner=_OneToolPlanner("memory.recall"), registry=registry, events=events,
        payloads=payloads, state=InMemoryTurnStateStore(),
        manifest_resolver=ProjectAwareCapabilityManifestResolver(snapshots),
        context_manifest_resolver=ProjectAwareContextManifestResolver(snapshots),
        execution_boundary=_MutateAfterDecisionBoundary(boundary_profiles, binding_guard=guard),
    )

    receipt = runtime.submit_turn(_turn())

    assert receipt.status == "failed"
    assert provider.calls == 0


def test_boundary_mutation_cannot_complete_while_provider_is_inside_dispatch_fence(tmp_path: Path) -> None:
    capability_profiles = ProjectCapabilityProfileStore(tmp_path)
    boundary_profiles = ProjectBoundaryProfileStore(tmp_path)
    boundary_profiles.update("project-alpha", mode="guarded", remote_default="review", expected_revision=0)
    capability_profiles.update(
        "project-alpha", expected_revision=0,
        boundary_profile_id="project-boundary-project-alpha", boundary_profile_revision=1,
    )
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    provider = _BlockingProvider()
    registry = ScopedCapabilityRegistry()
    registry.register(_capability("memory.recall", "read", approval=False, semantics="read_only"), provider)
    snapshots = TurnProjectProfileSnapshotAuthority(capability_profiles, boundary_profiles)
    runtime = SynchronousAIRuntime(
        planner=_OneToolPlanner("memory.recall"), registry=registry, events=events,
        payloads=payloads, state=InMemoryTurnStateStore(),
        manifest_resolver=ProjectAwareCapabilityManifestResolver(snapshots),
        context_manifest_resolver=ProjectAwareContextManifestResolver(snapshots),
        execution_boundary=AIToolExecutionBoundary(
            boundary_profiles,
            binding_guard=TurnCapabilityBindingGuard(
                capability_profiles, boundary_profiles, events=events, payloads=payloads,
            ),
        ),
    )
    runtime_done = Event()
    mutation_started = Event()
    mutation_done = Event()

    def run_turn() -> None:
        runtime.submit_turn(_turn())
        runtime_done.set()

    def mutate_boundary() -> None:
        mutation_started.set()
        boundary_profiles.set_mode(
            "project-alpha", mode="sealed", remote_default="deny", expected_revision=1,
        )
        mutation_done.set()

    runtime_thread = Thread(target=run_turn)
    runtime_thread.start()
    assert provider.started.wait(timeout=5)
    mutation_thread = Thread(target=mutate_boundary)
    mutation_thread.start()
    assert mutation_started.wait(timeout=5)
    assert not mutation_done.wait(timeout=0.1)
    provider.release.set()
    runtime_thread.join(timeout=5)
    mutation_thread.join(timeout=5)

    assert runtime_done.is_set()
    assert mutation_done.is_set()
    assert provider.calls == 1
    assert boundary_profiles.get("project-alpha").profile.revision == 2


class _BoundaryWideningPlanner:
    def __init__(self, profiles: ProjectBoundaryProfileStore) -> None:
        self._profiles = profiles
        self._changed = False

    def plan(self, _request, _events, _capabilities, _payloads, execution_control=None):
        if not self._changed:
            self._profiles.update(
                "project-alpha", mode="open", remote_default="allow", expected_revision=1
            )
            self._changed = True
            return {"type": "tool", "capability_id": "answer.remote", "arguments": {"query": "status"}}
        return {"type": "complete", "summary": "done"}


class _CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, _request):
        self.calls += 1
        return {"summary": "unexpected", "result": {"ok": True}}


class _BlockingProvider(_CountingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def invoke(self, _request):
        self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=5)
        return {"summary": "completed", "result": {"ok": True}}


class _OneToolPlanner:
    def __init__(self, capability_id: str) -> None:
        self._capability_id = capability_id
        self._planned = False

    def plan(self, _request, _events, _capabilities, _payloads, execution_control=None):
        if not self._planned:
            self._planned = True
            return {"type": "tool", "capability_id": self._capability_id, "arguments": {}}
        return {"type": "complete", "summary": "done"}


class _MutateAfterDecisionBoundary(AIToolExecutionBoundary):
    def __init__(self, profiles: ProjectBoundaryProfileStore, *, binding_guard: TurnCapabilityBindingGuard) -> None:
        super().__init__(profiles, binding_guard=binding_guard)
        self._profiles = profiles

    def evaluate(self, request, capability, decision):
        result = super().evaluate(request, capability, decision)
        self._profiles.set_mode(
            "project-alpha", mode="open", remote_default="allow", expected_revision=1,
        )
        return result


def _turn() -> dict[str, object]:
    return json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(
            encoding="utf-8"
        )
    )


def _remote_turn(capability_id: str) -> dict[str, object]:
    request = copy.deepcopy(_turn())
    request["privacy"] = {
        "mode": "remote_allowed", "allow_remote": True, "pii": "none",
        "consent_refs": [], "retention": "local_durable",
    }
    request["capability_policy"]["allowed"].append(capability_id)
    return request


def _capability(
    capability_id: str,
    mode: str,
    *,
    approval: bool,
    semantics: str = "receipt_required",
) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id, 1, mode, approval, semantics,
        "crp://default/contracts/in.schema.json",
        "crp://default/contracts/out.schema.json",
    )


def _mcp_capability() -> CapabilityDefinition:
    tool = ToolDefinition(
        "calendar.read", 1, "Read calendar", "Read events from MCP",
        "mcp", "calendar-server", "read", ("calendar_event",), "mcp",
        "crp://default/contracts/in.schema.json",
        "crp://default/contracts/out.schema.json",
        None, "read_only", "parallel", (), "never_retry",
        ToolRetryPolicy(1, 0, ()), None, None, "read_only", "remote",
        ("calendar-server",), ("calendar_event",), 10_000,
        ("calendar.read",), ("mcp_server_enabled",),
        connection_identity=ToolConnectionIdentity(
            "mcp", "calendar-server", "2025-11-25", 1,
            "calendar-local", "personal-calendar", 1, 1, 1,
        ),
    )
    return CapabilityDefinition(
        "calendar.read", 1, "read", False, "read_only",
        tool.input_schema_uri, tool.output_schema_uri, tool,
    )
