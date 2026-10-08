from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path

from backend.api.source_document_ai_runtime import (
    DOCUMENT_DRAFT_PROPOSE_CAPABILITY,
    SOURCE_DOCUMENT_DRAFT_OUTCOME,
    SOURCE_EVIDENCE_CAPABILITY,
    SourceDocumentDraftPlanner,
    SourceDocumentDraftProposalCapability,
    SourceDocumentEvidenceCapability,
)
from core.ai_kernel import (
    CapabilityDefinition,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    InMemoryTurnStateStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
)
from core.model_gateway import ModelResult
from core.storage_provider import JsonObjectStore
from tests.backend.integration.api.turn_model_routing_fixture import RoutingSnapshotFixture


ROOT = Path(__file__).resolve().parents[4]


class _Gateway:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        assert request.privacy_scope == "remote_allowed"
        assert request.parameters["messages"][0]["role"] == "system"
        assert request.parameters["messages"][1]["role"] == "user"
        assert request.parameters["messages"][0]["content"] != request.parameters["messages"][1]["content"]
        user_payload = json.loads(request.parameters["messages"][1]["content"])
        assert user_payload["source_id"] == "source-1"
        assert user_payload["structured_body"] == "完整结构化正文"
        assert "provider" not in user_payload and "api_key" not in user_payload
        return ModelResult(
            {
                "title": "AI 资料手册",
                "markdown": "## 结论\n\n结论来自已完成的 Source 结构。\n\n## 待确认\n\n请人工核对。",
            },
            "provider-test",
            "model-test",
            {},
        )


def test_source_document_turn_requires_approval_then_writes_one_document_and_stable_receipt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store)
    gateway = _Gateway()
    runtime = _runtime(tmp_path, store, gateway)
    request = _request()

    waiting = runtime.submit_turn(request)

    assert waiting.status == "waiting_approval"
    assert gateway.calls == 1
    assert store.revision("sources", "source-1") == 1
    assert store.list("documents") == ()
    assert store.list("source_template_outputs") == ()
    assert store.list("memory_candidates") == ()
    approval = tuple(runtime.events_after(waiting.turn_id))[-1]
    completed = runtime.apply_action(_approval(waiting, approval))

    assert completed.status == "completed"
    presentation = runtime.presentation_for(completed.turn_id)
    assert presentation is not None
    assert presentation["provider_enhanced"] is True
    assert presentation["memory_publication_state"] == "not_published"
    assert presentation["request_payload_persisted"] is False
    assert len(store.list("documents")) == 1
    assert len(store.list("source_template_outputs")) == 1
    assert store.list("memory_candidates") == ()
    tool_event = next(
        event
        for event in runtime.events_after(completed.turn_id)
        if event["type"] == "tool.completed" and event["data"]["capability_id"] == DOCUMENT_DRAFT_PROPOSE_CAPABILITY
    )
    assert tool_event["data"]["receipt_ref"] == presentation["output_ref"]
    assert tool_event["data"]["receipt_ref"].startswith("crp://default/source-template-outputs/")


def test_source_document_turn_fails_closed_when_source_drifts_while_waiting_for_approval(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _write_source(store)
    runtime = _runtime(tmp_path, store, _Gateway())
    waiting = runtime.submit_turn(_request(turn_id="turn-ffffffffffffffffffffffffffffffff"))
    approval = tuple(runtime.events_after(waiting.turn_id))[-1]
    changed = dict(source)
    changed["title"] = "审批前发生漂移"
    store.write("sources", "source-1", changed, expected_revision=1)

    failed = runtime.apply_action(_approval(waiting, approval, suffix="stale"))

    assert failed.status == "failed"
    failure = tuple(runtime.events_after(failed.turn_id))[-1]
    assert failure["data"]["error_code"] == "ai.stale_baseline"
    assert store.list("documents") == ()
    assert store.list("source_template_outputs") == ()
    assert store.list("memory_candidates") == ()


def test_source_document_capability_contract_is_approval_gated_and_receipted() -> None:
    evidence = CapabilityDefinition(
        SOURCE_EVIDENCE_CAPABILITY,
        1,
        "read",
        False,
        "read_only",
        "crp://default/contracts/in.schema.json",
        "crp://default/contracts/out.schema.json",
    )
    proposal = CapabilityDefinition(
        DOCUMENT_DRAFT_PROPOSE_CAPABILITY,
        1,
        "write",
        True,
        "receipt_required",
        "crp://default/contracts/in.schema.json",
        "crp://default/contracts/out.schema.json",
    )

    assert SOURCE_DOCUMENT_DRAFT_OUTCOME == "source.document.draft"
    assert evidence.mode == "read" and evidence.requires_approval is False
    assert proposal.mode == "write" and proposal.requires_approval is True
    assert proposal.operation_semantics == "receipt_required"


def _runtime(tmp_path: Path, store: JsonObjectStore, gateway: _Gateway) -> SynchronousAIRuntime:
    registry = ScopedCapabilityRegistry()
    registry.register(
        CapabilityDefinition(
            SOURCE_EVIDENCE_CAPABILITY,
            1,
            "read",
            False,
            "read_only",
            "crp://default/contracts/in.schema.json",
            "crp://default/contracts/out.schema.json",
        ),
        SourceDocumentEvidenceCapability(runtime_root=tmp_path, store=store, namespace_id="default"),
    )
    registry.register(
        CapabilityDefinition(
            DOCUMENT_DRAFT_PROPOSE_CAPABILITY,
            1,
            "write",
            True,
            "receipt_required",
            "crp://default/contracts/in.schema.json",
            "crp://default/contracts/out.schema.json",
        ),
        SourceDocumentDraftProposalCapability(runtime_root=tmp_path, store=store, namespace_id="default"),
    )
    payloads = InMemoryTurnPayloadStore()
    routing = RoutingSnapshotFixture(payloads, required_capability="structured", egress_purpose="document_draft")
    return SynchronousAIRuntime(
        planner=SourceDocumentDraftPlanner(gateway),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=payloads,
        state=InMemoryTurnStateStore(),
        manifest_resolver=routing,
        context_manifest_resolver=routing.context_resolver,
    )


def _request(*, turn_id: str = "turn-0123456789abcdef0123456789abcdef") -> dict[str, object]:
    request = json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8")
    )
    request["turn_id"] = turn_id
    request["operation_id"] = "op-source-document-0001" if turn_id.startswith("turn-0") else "op-source-document-stale"
    request["idempotency_key"] = "source-document-turn-0001" if turn_id.startswith("turn-0") else "source-document-turn-stale"
    request["desired_outcome"] = SOURCE_DOCUMENT_DRAFT_OUTCOME
    request["input"] = {
        "kind": "references",
        "text": "answer_manual",
        "refs": [{"kind": "source", "object_id": "source-1", "uri": "crp://default/sources/source-1"}],
    }
    request["capability_policy"] = {
        "allowed": [SOURCE_EVIDENCE_CAPABILITY, DOCUMENT_DRAFT_PROPOSE_CAPABILITY],
        "denied": [],
        "require_approval": [DOCUMENT_DRAFT_PROPOSE_CAPABILITY],
    }
    request["privacy"] = {
        "mode": "remote_allowed",
        "allow_remote": True,
        "pii": "possible",
        "consent_refs": ["crp://default/consent/provider-egress-policy"],
        "retention": "local_durable",
    }
    return request


def _approval(waiting, approval: Mapping[str, object], *, suffix: str = "happy") -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "action_id": "action-0123456789abcdef0123456789abcdef" if suffix == "happy" else "action-ffffffffffffffffffffffffffffffff",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval["event_id"],
        "reason": "Source document draft approved",
        "actor": "user",
        "expected_sequence": waiting.current_sequence,
        "idempotency_key": f"approve-source-document-{suffix}",
        "created_at": "2026-08-23T08:00:00Z",
    }


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _write_source(store: JsonObjectStore) -> dict[str, object]:
    source = {
        "schema_version": "1.0.0",
        "id": "source-1",
        "project_id": "project-alpha",
        "title": "Source 标题",
        "metadata": {
            "content_structure": {
                "status": "completed",
                "summary": "结构摘要",
                "key_points": ["关键点"],
                "structured_body": "完整结构化正文",
                "structure_ref": "crp://default/source-content/source-1.json",
                "series_candidate": "项目资料",
            }
        },
    }
    store.write("sources", "source-1", source, expected_revision=0)
    return source
