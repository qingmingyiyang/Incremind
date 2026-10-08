from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.memory_publication_effect_runtime import INTENTS, RECEIPTS
from core.capability_packages.thought_graph_context import (
    DocumentDraftAdapter,
    MemoryProposalAdapter,
    PlatformProposalHandoffAdapter,
    ProjectSkillProposalAdapter,
    review_proposal,
)
from core.context_graph import (
    ContextBinding,
    ContextCompiler,
    ContextGraphEdge,
    ContextGraphNode,
    ContextGraphSnapshot,
    ContextPermissionGrant,
    ContextProvenance,
    FrozenContextRevisions,
    StalenessEvaluationInput,
)
from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository
from core.effect_log import Effect, EffectReaper, EffectRunner, EffectState
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate
from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    STRUCTURED_DATABASE_NAME,
    TARGET_IDENTITY,
)
from core.memory_core import (
    shared_trust_audit_activation_id,
    shared_trust_audit_activation_payload,
)
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
)


ROOT = Path(__file__).resolve().parents[3]
SKILL_FIXTURE = ROOT / "core-contracts/rebuild/fixtures/project_skill/valid-active-skill.json"


class _SimulatedClientDisconnect(OSError):
    pass


def _store(root: Path) -> JsonObjectStore:
    return JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")


def _binding(graph_id: str) -> ContextBinding:
    nodes = (
        ContextGraphNode(
            node_id="requirement", node_type="question", title="Requirement",
            content_ref=f"content:{graph_id}:requirement", content_revision="r1",
            source_refs=("source:requirement",), trust="user_authored",
            created_at="2026-08-30T00:00:00Z", updated_at="2026-08-30T00:00:00Z",
            metadata={"project_id": "project-alpha", "content": "Create a reviewable LineMap proposal."},
        ),
        ContextGraphNode(
            node_id="evidence", node_type="evidence", title="Evidence",
            content_ref=f"content:{graph_id}:evidence", content_revision="r1",
            source_refs=("source:evidence",), trust="verified",
            created_at="2026-08-30T00:00:00Z", updated_at="2026-08-30T00:00:00Z",
            metadata={"project_id": "project-alpha", "content": "The proposal remains draft-only until review."},
        ),
        ContextGraphNode(
            node_id="decision", node_type="decision", title="Decision",
            content_ref=f"content:{graph_id}:decision", content_revision="r1",
            source_refs=("source:decision",), trust="user_authored",
            created_at="2026-08-30T00:00:00Z", updated_at="2026-08-30T00:00:00Z",
            metadata={"project_id": "project-alpha", "content": "Use the governed platform Effect path after confirmation."},
        ),
    )
    snapshot = ContextGraphSnapshot(
        schema_version="1.0.0", graph_id=graph_id, graph_revision="g4",
        project_id="project-alpha", source_type="fixture", source_revision="source-r1",
        created_at="2026-08-30T00:00:00Z", nodes=nodes,
        edges=(
            ContextGraphEdge("edge-1", "requirement", "evidence", "full_chain", 1, 0),
            ContextGraphEdge("edge-2", "evidence", "decision", "full_chain", 1, 1),
        ),
        selected_outputs=("decision",), token_estimate=40,
        provenance=ContextProvenance(
            source_type="fixture", source_revision="source-r1",
            imported_at="2026-08-30T00:00:00Z", importer_id="fixture",
            importer_revision="1", source_ref=f"fixture://{graph_id}",
        ),
    )
    revisions = FrozenContextRevisions("4.0.0", "b4", "p4", "m4", "2.0.0")
    return ContextCompiler().compile(
        snapshot, revisions=revisions, expected_revisions=revisions,
        permission_grant=ContextPermissionGrant(
            "project-alpha", "grant-r1",
            frozenset(node.content_ref for node in nodes),
        ),
        token_budget=700,
        staleness_input=StalenessEvaluationInput.baseline(snapshot, revisions),
    )


def _import(client: TestClient, payload: dict[str, object]) -> str:
    response = client.post(
        "/api/rebuild/external-agent/proposals",
        json={"project_id": "project-alpha", "proposal": payload},
    )
    assert response.status_code == 200, response.text
    draft_ids = response.json()["draft_ids"]
    assert len(draft_ids) == 1
    return str(draft_ids[0])


def _assert_v2_gate_intent_and_receipt(root: Path, effect: Effect) -> None:
    assert effect.contract_version == "effect-v2"
    database = root / ".rebuild-data" / "jobs.sqlite3"
    with sqlite3.connect(database) as connection:
        gate = connection.execute(
            "SELECT decision, policy_revision FROM effect_gate_fact WHERE decision_id = ?",
            (effect.gate_decision_id,),
        ).fetchone()
        intent = connection.execute(
            "SELECT intent_ref, intent_digest, schema_version "
            "FROM effect_intent_fact WHERE operation_id = ?",
            (effect.operation_id,),
        ).fetchone()
        receipt = connection.execute(
            "SELECT receipt_ref, receipt_kind, receipt_schema_version "
            "FROM effect_receipt WHERE operation_id = ?",
            (effect.operation_id,),
        ).fetchone()
    assert gate == ("allow", str(effect.rev_set["policy"]))
    assert intent == (
        effect.intent_ref,
        effect.intent_digest,
        effect.intent_schema_version,
    )
    assert receipt == (
        effect.result_ref,
        effect.expected_receipt_kind,
        effect.expected_receipt_schema_version,
    )


async def _post_and_disconnect_after_response_start(
    app, path: str, payload: dict[str, object],
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    delivered = False

    async def receive() -> dict[str, object]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message.get("type") == "http.response.start":
            raise _SimulatedClientDisconnect(
                "simulated UI disconnect after formal Effect settled",
            )

    await app({
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }, receive, send)


def _contains_simulated_disconnect(error: BaseException) -> bool:
    if isinstance(error, _SimulatedClientDisconnect):
        return True
    return any(
        _contains_simulated_disconnect(nested)
        for nested in getattr(error, "exceptions", ())
        if isinstance(nested, BaseException)
    )


def _document_effect_evidence_counts(
    root: Path, draft_id: str,
) -> tuple[str, int, int]:
    intent_ref = f"crp://default/external-document-apply-intents/{draft_id}"
    with sqlite3.connect(root / ".rebuild-data" / "jobs.sqlite3") as connection:
        rows = connection.execute(
            "SELECT operation_id FROM effect WHERE kind = ? AND intent_ref = ?",
            ("external_document_apply", intent_ref),
        ).fetchall()
        if len(rows) != 1:
            raise AssertionError("expected exactly one formal Document Effect")
        effect_id = str(rows[0][0])
        intent_count = int(connection.execute(
            "SELECT COUNT(*) FROM effect_intent_fact WHERE operation_id = ?",
            (effect_id,),
        ).fetchone()[0])
        receipt_count = int(connection.execute(
            "SELECT COUNT(*) FROM effect_receipt WHERE operation_id = ?",
            (effect_id,),
        ).fetchone()[0])
    return effect_id, intent_count, receipt_count


def _activate_memory_authority(root: Path) -> None:
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    authority = SQLiteAggregateAuthorityStore(root / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    members = (
        "memory_atoms", "memory_publications", "memory_scenarios",
        "memory_series_memory", "memory_transitions", "project_skills",
    )
    evidence = AggregateAuthorityEvidence(
        "linemap-memory-v1", "a" * 64, "b" * 64, TARGET_IDENTITY,
    )
    with records.begin() as transaction:
        for member in members:
            transaction.put("aggregate_authority_targets", f"default~{member}", {
                "namespace_id": "default", "aggregate": member,
                "migration_id": evidence.migration_id,
                "source_fingerprint": evidence.source_fingerprint,
                "target_fingerprint": evidence.target_fingerprint,
                "target_identity": evidence.target_identity,
            }, expected_revision=0)
        transaction.put(
            "aggregate_authority_compound_activations",
            shared_trust_audit_activation_id("default"),
            shared_trust_audit_activation_payload(
                namespace_id="default", target_identity=TARGET_IDENTITY,
                activation_id="linemap-memory-v1",
                member_migrations={member: evidence.migration_id for member in members},
                source_fingerprint=evidence.source_fingerprint,
                target_fingerprint=evidence.target_fingerprint,
                activated_at="2026-08-29T00:00:00+00:00",
            ),
            expected_revision=0,
        )
        transaction.commit()
    for member in members:
        initial = authority.create_json_active(
            namespace_id="default", aggregate=member, reason="LineMap fixture",
        )
        staged = authority.transition(
            namespace_id="default", aggregate=member,
            expected_revision=initial.revision, to_state="sqlite_staged",
            evidence=evidence, reason="LineMap fixture",
        )
        authority.transition(
            namespace_id="default", aggregate=member,
            expected_revision=staged.revision, to_state="sqlite_active",
            evidence=evidence, reason="LineMap fixture",
        )


def test_linemap_document_confirmation_runs_effect_handler_and_binds_receipt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(store)
    document = documents.create(DocumentDraft(
        title="LineMap formal target", document_type="notes", markdown="Before.",
        project_id="project-alpha",
        source_refs=({"source_id": "linemap-source", "locator": "char:0-7"},),
    ))
    proposal = DocumentDraftAdapter().create(
        _binding("document-effect"), project_id="project-alpha",
        title="LineMap document revision", content="After LineMap review.",
        paragraphs=({"source_node_ids": ("evidence",), "text": "After LineMap review."},),
    )
    payload = dict(PlatformProposalHandoffAdapter().create_payload(
        review_proposal(proposal, decision="accept"), target_id=str(document["id"]),
    ))

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        draft_id = _import(client, payload)
        with pytest.raises(KeyError):
            client.app.state.effect_runtime.log.get(draft_id)

        denied = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": False, "expected_revision": 1},
        )
        assert denied.status_code == 400
        with pytest.raises(KeyError):
            client.app.state.effect_runtime.log.get(draft_id)

        applied = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )
        assert applied.status_code == 200, applied.text
        effect_id = str(applied.json()["effect_id"])
        assert effect_id.startswith("eff2_")
        effect = client.app.state.effect_runtime.log.get(effect_id)
        assert effect.kind == "external_document_apply"
        assert effect.state is EffectState.SETTLED_OK
        assert effect.result_ref and "external-document-apply-receipts" in effect.result_ref
        _assert_v2_gate_intent_and_receipt(tmp_path, effect)

    assert documents.markdown(str(document["id"])) == "After LineMap review."
    assert len(documents.revisions(str(document["id"]))) == 2

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as restarted:
        replay = restarted.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )
        assert replay.status_code == 409
        assert restarted.app.state.effect_runtime.log.get(effect_id).state is EffectState.SETTLED_OK
    assert len(documents.revisions(str(document["id"]))) == 2


def test_linemap_project_skill_confirmation_runs_effect_handler_and_binds_receipt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    skills = ObjectStoreProjectSkillRepository(store)
    structured = json.loads(SKILL_FIXTURE.read_text(encoding="utf-8"))
    for key in (
        "revision", "markdown_revision", "json_revision", "markdown_uri", "json_uri",
        "created_at", "updated_at",
    ):
        structured.pop(key, None)
    structured.update({"id": "skill-project-alpha", "project_id": "project-alpha"})
    skills.save(ProjectSkillUpdate(
        "project-alpha", "# Before", structured, 0, "initial",
    ))
    proposed = {**structured, "style_preferences": {
        "voice": "LineMap verified", "format_defaults": ["Markdown"],
    }}
    proposal = ProjectSkillProposalAdapter().create(
        _binding("skill-effect"), project_id="project-alpha",
        title="LineMap Project Skill update", content="# LineMap reviewed skill",
        sections=proposed,
    )
    payload = dict(PlatformProposalHandoffAdapter().create_payload(
        review_proposal(proposal, decision="accept"), target_id="skill-project-alpha",
    ))

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        draft_id = _import(client, payload)
        applied = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )
        assert applied.status_code == 200, applied.text
        effect_id = str(applied.json()["effect_id"])
        assert effect_id.startswith("eff2_")
        effect = client.app.state.effect_runtime.log.get(effect_id)
        assert effect.kind == "external_project_skill_apply"
        assert effect.state is EffectState.SETTLED_OK
        assert effect.result_ref and "external-project-skill-apply-receipts" in effect.result_ref
        _assert_v2_gate_intent_and_receipt(tmp_path, effect)

    updated = skills.load("project-alpha")
    assert updated is not None and updated["revision"] == 2
    assert updated["style_preferences"]["voice"] == "LineMap verified"
    assert len(skills.revisions("project-alpha")) == 2


def test_linemap_document_crash_after_handler_is_recovered_once_by_core_reaper(
    tmp_path: Path, monkeypatch,
) -> None:
    store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(store)
    document = documents.create(DocumentDraft(
        title="LineMap recovery target", document_type="notes", markdown="Before crash.",
        project_id="project-alpha",
        source_refs=({"source_id": "linemap-source", "locator": "char:0-13"},),
    ))
    proposal = DocumentDraftAdapter().create(
        _binding("document-recovery"), project_id="project-alpha",
        title="Recover LineMap document", content="Committed before simulated process loss.",
    )
    payload = dict(PlatformProposalHandoffAdapter().create_payload(
        review_proposal(proposal, decision="accept"), target_id=str(document["id"]),
    ))
    original_execute = EffectRunner.execute_planned

    def interrupt_after_handler(self, operation_id, handler, *, now, receipt_kind=None):
        def interrupted(effect):
            handler(effect)
            raise OSError("simulated sidecar loss after handler")

        return original_execute(
            self, operation_id, interrupted, now=now, receipt_kind=receipt_kind,
        )

    with monkeypatch.context() as patch:
        patch.setattr(EffectRunner, "execute_planned", interrupt_after_handler)
        with TestClient(
            create_app(SimpleNamespace(root_dir=tmp_path)),
            raise_server_exceptions=False,
        ) as client:
            draft_id = _import(client, payload)
            failed = client.post(
                f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
                json={"confirm": True, "expected_revision": 1},
            )
            assert failed.status_code == 500
            inflight = [
                candidate
                for candidate in client.app.state.effect_runtime.log.expired_inflight(now=2**31)
                if candidate.kind == "external_document_apply"
            ]
            assert len(inflight) == 1
            effect = inflight[0]
            effect_id = effect.operation_id
            assert effect_id.startswith("eff2_")
            assert effect.state is EffectState.INFLIGHT and effect.result_ref is None

    assert documents.markdown(str(document["id"])) == "Committed before simulated process loss."
    assert len(documents.revisions(str(document["id"]))) == 2

    from core.effect_log import EffectLog

    log = EffectLog(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    recovered = EffectReaper(log).recover_expired(now=2**31)
    assert recovered and recovered[0].state is EffectState.PLANNED

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as restarted:
        effect = restarted.app.state.effect_runtime.log.get(effect_id)
        assert effect.state is EffectState.SETTLED_OK
        assert effect.result_ref and "external-document-apply-receipts" in effect.result_ref
        _assert_v2_gate_intent_and_receipt(tmp_path, effect)

    assert len(documents.revisions(str(document["id"]))) == 2


def test_linemap_document_response_disconnect_does_not_replay_effect(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(store)
    document = documents.create(DocumentDraft(
        title="LineMap disconnect target", document_type="notes",
        markdown="Before disconnect.", project_id="project-alpha",
        source_refs=({"source_id": "linemap-source", "locator": "char:0-18"},),
    ))
    proposal = DocumentDraftAdapter().create(
        _binding("document-disconnect"), project_id="project-alpha",
        title="LineMap disconnected response",
        content="Committed before the UI disconnected.",
    )
    payload = dict(PlatformProposalHandoffAdapter().create_payload(
        review_proposal(proposal, decision="accept"), target_id=str(document["id"]),
    ))

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        draft_id = _import(client, payload)
        path = f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply"
        with pytest.raises(BaseException) as caught:
            asyncio.run(_post_and_disconnect_after_response_start(
                client.app,
                path,
                {"confirm": True, "expected_revision": 1},
            ))
        assert _contains_simulated_disconnect(caught.value)
        effect_id, intent_count, receipt_count = _document_effect_evidence_counts(
            tmp_path, draft_id,
        )
        assert effect_id.startswith("eff2_")
        effect = client.app.state.effect_runtime.log.get(effect_id)
        assert effect.state is EffectState.SETTLED_OK
        assert (intent_count, receipt_count) == (1, 1)
        _assert_v2_gate_intent_and_receipt(tmp_path, effect)

    assert documents.markdown(str(document["id"])) == "Committed before the UI disconnected."
    assert len(documents.revisions(str(document["id"]))) == 2

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as restarted:
        replay = restarted.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )
        assert replay.status_code == 409
        assert restarted.app.state.effect_runtime.log.get(effect_id).state is EffectState.SETTLED_OK
    assert _document_effect_evidence_counts(tmp_path, draft_id) == (effect_id, 1, 1)
    assert len(documents.revisions(str(document["id"]))) == 2


def test_linemap_memory_remains_proposal_until_second_confirmation_then_uses_effect(
    tmp_path: Path,
) -> None:
    _activate_memory_authority(tmp_path)
    proposal = MemoryProposalAdapter().create(
        _binding("memory-effect"), project_id="project-alpha",
        title="LineMap Memory Proposal", content="User-reviewed LineMap memory.",
    )
    payload = dict(PlatformProposalHandoffAdapter().create_payload(
        review_proposal(proposal, decision="accept"),
    ))
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME,
    )

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        imported = client.post(
            "/api/rebuild/external-agent/proposals",
            json={"project_id": "project-alpha", "proposal": payload},
        )
        assert imported.status_code == 200, imported.text
        candidate_id = str(imported.json()["memory_candidate_id"])
        assert records.list("memory_atoms") == ()

        reviewed = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={"action": "promote_to_atom", "reason": "LineMap first review."},
        )
        assert reviewed.status_code == 200, reviewed.text
        staged_id = str(reviewed.json()["promoted_object_id"])
        assert records.list("memory_atoms") == ()
        assert records.list(INTENTS) == ()

        published = client.post(
            f"/api/rebuild/staging-atoms/{staged_id}/publication",
            json={"confirm": True, "reason": "LineMap second confirmation."},
        )
        assert published.status_code == 200, published.text
        intents = records.list(INTENTS)
        assert len(intents) == 1
        effect_id = str(published.json()["operation_id"])
        assert effect_id.startswith("eff2_")
        effect = client.app.state.effect_runtime.log.get(effect_id)
        assert effect.kind == "formal_memory_publication"
        assert effect.state is EffectState.SETTLED_OK
        assert effect.result_ref and effect.result_ref.startswith(
            "receipt:formal-memory-publication/eff2_",
        )
        receipt = records.read(RECEIPTS, effect_id)
        assert receipt is not None and receipt.payload["status"] == "published"
        _assert_v2_gate_intent_and_receipt(tmp_path, effect)

    assert len(records.list("memory_atoms")) == 1
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as restarted:
        replay = restarted.post(
            f"/api/rebuild/staging-atoms/{staged_id}/publication",
            json={"confirm": True, "reason": "LineMap second confirmation."},
        )
        assert replay.status_code == 200
        assert replay.json()["operation_id"] == effect_id
        assert restarted.app.state.effect_runtime.log.get(effect_id).state is EffectState.SETTLED_OK
    assert len(records.list("memory_atoms")) == 1
