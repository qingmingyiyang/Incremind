from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
import backend.api.routes.series as series_routes
from backend.api import ai_runtime
from backend.api.ai_runtime import build_ai_runtime
from backend.api.series_intake_ai_runtime import (
    SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY,
    SERIES_INTAKE_ORGANIZE_OUTCOME,
    SERIES_INTAKE_ORGANIZE_SNAPSHOT_KIND,
    SeriesIntakeOrganizeCommitCapability,
    SeriesIntakeOrganizeTurnPlanner,
)
from backend.replay.contracts import CreateIntakeRequest, UpdateIntakeRequest
from backend.replay.prompts import REPLAY_INTAKE_ORGANIZER_PROMPT_VERSION
from backend.replay.series_workspace import SeriesWorkspace
from core.ai_kernel import (
    CapabilityDefinition,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    InMemoryTurnStateStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
)
from core.model_gateway import ModelResult
from tests.backend.integration.api.turn_model_routing_fixture import RoutingSnapshotFixture


class _Gateway:
    def __init__(self, *, before_result=None) -> None:
        self.calls = 0
        self.before_result = before_result

    def invoke(self, request):
        self.calls += 1
        snapshot = request.parameters["_model_routing_snapshot"]
        sink = request.metadata_sink
        assert sink is not None
        sink.model_call_routed(
            snapshot_ref=request.parameters["_model_routing_snapshot_ref"],
            snapshot_revision=request.parameters["_model_routing_snapshot_revision"],
            prompt_cache_scope_identity=snapshot["prompt_cache_scope"]["identity"],
            provider="fixture", model="json-1", execution_location="remote",
        )
        sink.model_call_started(provider="fixture", model="json-1")
        sink.model_call_completed(usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
        if self.before_result is not None:
            self.before_result()
        return ModelResult(
            output={
                "title": "整理后的标题",
                "structured_text": "## 完成事项\n\n- 已完成\n\n## 问题记录\n\n无\n\n## 后续计划\n\n- 人工复核",
                "summary": "整理后的摘要",
                "tags": ["集成测试"],
                "suggested_actions": ["人工复核"],
                "suggested_report_type": "daily",
            },
            provider="fixture", model="json-1", usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        )


def test_series_intake_turn_waits_approval_then_completes_with_replay_and_nested_evidence(tmp_path: Path) -> None:
    workspace = SeriesWorkspace(tmp_path)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="待整理的原始内容"))
    gateway = _Gateway()
    runtime = _runtime(tmp_path, workspace, gateway)
    request = _request(item)

    waiting = runtime.submit_turn(request)

    assert waiting.status == "waiting_approval", tuple(runtime.events_after(waiting.turn_id))[-1]["data"]["error_code"]
    assert gateway.calls == 0
    assert workspace.get_intake("default", item.intake_id).status == "pending"
    assert not tuple(workspace.series_path("default").glob("intake/operations/*.json"))

    completed = _approve(runtime, waiting)
    replay = runtime.submit_turn(request)

    assert completed.status == "completed"
    assert replay.status == "completed" and replay.replayed is True
    assert gateway.calls == 1
    saved = workspace.get_intake("default", item.intake_id)
    assert saved.status == "reviewing" and saved.title == "整理后的标题"
    events = tuple(runtime.events_after(completed.turn_id))
    assert any(event["type"] == "tool.started" and event["data"]["capability_id"] == SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY for event in events)
    nested_requests = [
        event for event in events
        if event["type"] == "model.requested"
        and event.get("correlation", {}).get("tool_call_id") is not None
    ]
    assert len(nested_requests) == 1
    assert nested_requests[0]["data"]["model_call_purpose"] == "primary"
    tool = next(event for event in events if event["type"] == "tool.completed" and event["data"]["capability_id"] == SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY)
    assert tool["data"]["receipt_ref"]
    assert any("model" in str(event.get("type")) and "completed" in str(event.get("type")) for event in events)
    assert any("model" in str(ref) for ref in tool["data"]["evidence_refs"])


def test_series_intake_cas_conflict_is_confirmed_none_not_unknown_effect(tmp_path: Path) -> None:
    workspace = SeriesWorkspace(tmp_path)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="模型读取的旧内容"))
    gateway = _Gateway(before_result=lambda: workspace.update_intake(
        "default", item.intake_id, UpdateIntakeRequest(raw_text="用户在审批后编辑的新内容"),
    ))
    runtime = _runtime(tmp_path, workspace, gateway)

    failed = _approve(runtime, runtime.submit_turn(_request(item, suffix="conflict")))

    assert failed.status == "failed" and gateway.calls == 1
    assert workspace.get_intake("default", item.intake_id).raw_text == "用户在审批后编辑的新内容"
    events = tuple(runtime.events_after(failed.turn_id))
    failure = next(event for event in reversed(events) if event["type"] == "tool.failed")
    assert failure["data"]["error_code"] == "series_intake.stale_revision"
    outcome_event = next(event for event in reversed(events) if event["type"] == "tool.outcome.recorded")
    outcome = runtime._series_intake_test_payloads.get(outcome_event["data"]["payload_ref"])
    assert outcome["effect_certainty"] == "confirmed_none"
    assert outcome["status"] == "failed"


def test_legacy_organize_route_submits_canonical_turn_auto_approves_and_returns_intake(monkeypatch, tmp_path: Path) -> None:
    workspace = SeriesWorkspace(tmp_path)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="遗留同步接口的原始内容"))
    captured: list[dict[str, object]] = []
    approvals: list[dict[str, object]] = []

    class _Runtime:
        composition_metadata = {"series_intake_organize_remote_usable": True}

        def submit_turn(self, turn):
            captured.append(dict(turn))
            return SimpleNamespace(turn_id=turn["turn_id"], status="waiting_approval", current_sequence=4)

        def events_after(self, _turn_id):
            return ({
                "type": "approval.required",
                "event_id": "event-0123456789abcdef0123456789abcdef",
                "sequence": 4,
            },)

        def apply_action(self, action):
            approvals.append(dict(action))
            current = workspace.get_intake("default", item.intake_id)
            workspace._save_intake(  # noqa: SLF001 -- fake canonical Turn materialization.
                current.model_copy(update={"title": "Canonical 整理结果", "status": "reviewing"}),
                previous_status=current.status, expected_revision=current.revision,
            )
            return SimpleNamespace(turn_id=action["turn_id"], status="completed", current_sequence=8)

        def presentation_for(self, _turn_id):
            return {"status": "reviewing", "series_id": "default", "intake_id": item.intake_id}

    authority = SimpleNamespace(
        project_id="project-a", series_id="default", object_id="series-authority-a",
        payload_revision=3, storage_revision=2, authority_identity="sqlite:structured-records-v1",
        authority_ref="crp://default/memory/series/series-authority-a",
    )
    monkeypatch.setattr(series_routes, "get_or_build_ai_runtime", lambda *_args: _Runtime())
    monkeypatch.setattr(
        series_routes, "build_series_turn_scope_authority",
        lambda _root: SimpleNamespace(freeze_agent_series_scope=lambda **_kwargs: authority),
    )
    assert not hasattr(SeriesWorkspace, "organize_intake")
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.state.series_workspace = workspace
    app.include_router(series_routes.router)

    response = TestClient(app).post(f"/api/series/default/intake/{item.intake_id}/organize")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "reviewing"
    assert response.json()["title"] == "Canonical 整理结果"
    assert len(captured) == len(approvals) == 1
    contract_root = Path(__file__).resolve().parents[4] / "core-contracts" / "ai"
    Draft202012Validator(
        json.loads((contract_root / "turn-request.schema.json").read_text(encoding="utf-8")),
        format_checker=FormatChecker(),
    ).validate(captured[0])
    Draft202012Validator(
        json.loads((contract_root / "turn-action.schema.json").read_text(encoding="utf-8")),
        format_checker=FormatChecker(),
    ).validate(approvals[0])
    turn = captured[0]
    assert turn["desired_outcome"] == SERIES_INTAKE_ORGANIZE_OUTCOME
    assert turn["capability_policy"] == {
        "allowed": [SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY], "denied": [],
        "require_approval": [SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY],
    }
    assert turn["scope"]["project_id"] == "project-a"
    assert turn["scope"]["authority"]["authority_ref"] == authority.authority_ref
    snapshot = json.loads(turn["input"]["text"])
    assert snapshot["authority"] == turn["scope"]["authority"]
    assert snapshot["expected_revision"] == item.revision
    assert snapshot["intake"]["intake_id"] == item.intake_id
    assert approvals[0]["type"] == "approve" and approvals[0]["turn_id"] == turn["turn_id"]


def test_shared_runtime_registers_series_intake_as_approved_receipt_write(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        ai_runtime,
        "resolve_model_gateway_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(
            gateway=None, egress_consented=False, adapter_kind="unavailable",
        ),
    )

    runtime = build_ai_runtime(SimpleNamespace(root_dir=tmp_path))
    definition = next(
        item for item in runtime.capability_registry_snapshot().definitions
        if item.capability_id == SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY
    )

    assert definition.mode == "write"
    assert definition.requires_approval is True
    assert definition.operation_semantics == "receipt_required"


def test_legacy_route_confirmed_none_failure_advances_revision_and_allows_explicit_retry(monkeypatch, tmp_path: Path) -> None:
    workspace = SeriesWorkspace(tmp_path)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="允许用户显式重试"))
    turns: list[dict[str, object]] = []

    class _Runtime:
        composition_metadata = {"series_intake_organize_remote_usable": True}

        def submit_turn(self, turn):
            turns.append(dict(turn))
            status = "failed" if len(turns) == 1 else "waiting_approval"
            return SimpleNamespace(turn_id=turn["turn_id"], status=status, current_sequence=4)

        def execution_projection_for(self, _turn_id, _view):
            return {"tool_steps": [{
                "capability_id": SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY,
                "attempts": [{"effect_certainty": "confirmed_none"}],
            }]}

        def events_after(self, _turn_id):
            return ({
                "type": "approval.required",
                "event_id": "event-fedcba9876543210fedcba9876543210",
                "sequence": 4,
            },)

        def apply_action(self, action):
            current = workspace.get_intake("default", item.intake_id)
            workspace._save_intake(  # noqa: SLF001 -- fake canonical materialization.
                current.model_copy(update={"title": "重试成功", "status": "reviewing"}),
                previous_status=current.status, expected_revision=current.revision,
            )
            return SimpleNamespace(turn_id=action["turn_id"], status="completed", current_sequence=8)

        def presentation_for(self, _turn_id):
            return {"status": "reviewing", "series_id": "default", "intake_id": item.intake_id}

    authority = SimpleNamespace(
        project_id="project-a", series_id="default", object_id="series-authority-a",
        payload_revision=3, storage_revision=2, authority_identity="sqlite:structured-records-v1",
        authority_ref="crp://default/memory/series/series-authority-a",
    )
    runtime = _Runtime()
    monkeypatch.setattr(series_routes, "get_or_build_ai_runtime", lambda *_args: runtime)
    monkeypatch.setattr(
        series_routes, "build_series_turn_scope_authority",
        lambda _root: SimpleNamespace(freeze_agent_series_scope=lambda **_kwargs: authority),
    )
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.state.series_workspace = workspace
    app.include_router(series_routes.router)
    client = TestClient(app)

    failed = client.post(f"/api/series/default/intake/{item.intake_id}/organize")
    failed_item = workspace.get_intake("default", item.intake_id)
    completed = client.post(f"/api/series/default/intake/{item.intake_id}/organize")

    assert failed.status_code == 409
    assert failed_item.status == "failed" and failed_item.revision != item.revision
    assert completed.status_code == 200 and completed.json()["title"] == "重试成功"
    assert len(turns) == 2 and turns[0]["turn_id"] != turns[1]["turn_id"]


def _runtime(tmp_path: Path, workspace: SeriesWorkspace, gateway: _Gateway) -> SynchronousAIRuntime:
    registry = ScopedCapabilityRegistry()
    payloads = InMemoryTurnPayloadStore()
    registry.register(
        CapabilityDefinition(
            SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY, 1, "write", True, "receipt_required",
            "crp://default/contracts/series-intake-organize-request.schema.json",
            "crp://default/contracts/series-intake-organize-result.schema.json",
        ),
        SeriesIntakeOrganizeCommitCapability(
            workspace=workspace, gateway=gateway, payloads=payloads, namespace_id="default",
        ),
    )
    routing = RoutingSnapshotFixture(
        payloads, required_capability="structured", egress_purpose="series_intake_organize",
    )
    routing.context_resolver = _SeriesContextResolver(routing)
    runtime = SynchronousAIRuntime(
        planner=SeriesIntakeOrganizeTurnPlanner(), registry=registry,
        events=InMemoryTurnEventStore(), payloads=payloads, state=InMemoryTurnStateStore(),
        manifest_resolver=routing, context_manifest_resolver=routing.context_resolver,
    )
    runtime._series_intake_test_payloads = payloads  # type: ignore[attr-defined]
    return runtime


class _SeriesContextResolver:
    """The shared routing fixture is project-oriented; production Turn scope is Series."""

    def __init__(self, routing: RoutingSnapshotFixture) -> None:
        self._routing = routing

    def resolve(self, request, capability_manifest_ref, capability_manifest):
        manifest = self._routing.resolve_context(request, capability_manifest_ref, capability_manifest)
        return replace(manifest, series_id=request["scope"]["series_id"])


def _request(item, *, suffix: str = "complete") -> dict[str, object]:
    authority = {
        "kind": "project_series_scope_v1", "object_id": "series-authority-a",
        "payload_revision": 1, "storage_revision": 1,
        "authority_identity": "sqlite:structured-records-v1",
        "authority_ref": "crp://default/memory/series/series-authority-a",
    }
    snapshot = {
        "kind": SERIES_INTAKE_ORGANIZE_SNAPSHOT_KIND,
        "series_id": item.series_id, "project_id": "project-a", "authority": authority,
        "intake_id": item.intake_id, "intake": item.model_dump(mode="json"),
        "expected_revision": item.revision,
        "prompt_version": REPLAY_INTAKE_ORGANIZER_PROMPT_VERSION,
    }
    identity = "0123456789abcdef0123456789abcdef" if suffix == "complete" else "fedcba9876543210fedcba9876543210"
    return {
        "schema_version": "1.0.0", "turn_id": f"turn-{identity}",
        "session_id": f"session-{suffix}", "operation_id": f"op-series-intake-{suffix}",
        "idempotency_key": f"series-intake-{suffix}",
        "scope": {"kind": "series", "project_id": "project-a", "series_id": item.series_id, "authority": authority},
        "input": {
            "kind": "text", "text": json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            "refs": [{"kind": "atom", "object_id": item.intake_id, "uri": f"crp://default/series/{item.series_id}/intake/{item.intake_id}"}],
        },
        "desired_outcome": SERIES_INTAKE_ORGANIZE_OUTCOME,
        "privacy": {"mode": "remote_allowed", "allow_remote": True, "pii": "possible", "consent_refs": ["crp://default/consent/provider-egress-policy"], "retention": "local_durable"},
        "capability_policy": {"allowed": [SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY], "denied": [], "require_approval": [SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY]},
        "context_policy": {"include_project_skill": False, "include_memory": False, "include_session_history": False, "max_context_bytes": 4096},
        "approval_policy": {"mode": "always", "auto_approve_read_only": True},
        "created_at": "2026-08-25T00:00:00+00:00",
    }


def _approve(runtime: SynchronousAIRuntime, waiting):
    approval = next(event for event in reversed(tuple(runtime.events_after(waiting.turn_id))) if event["type"] == "approval.required")
    return runtime.apply_action({
        "schema_version": "1.0.0", "action_id": f"action-{waiting.turn_id[5:]}",
        "turn_id": waiting.turn_id, "type": "approve", "target_event_id": approval["event_id"],
        "reason": "Series Intake organize approved", "actor": "user",
        "expected_sequence": waiting.current_sequence, "idempotency_key": f"approve-{waiting.turn_id}",
        "created_at": "2026-08-25T00:00:01+00:00",
    })
