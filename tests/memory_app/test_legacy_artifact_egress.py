"""Legacy review snapshots must survive retained-result reuse; no app bootstrap."""

from copy import deepcopy

import pytest

from backend.memory_app.packet_egress import capture_packet_egress
from backend.recognition import RecognitionConflict
from core.document_engine import DocumentDraft
from tests.memory_app.test_artifact_egress import _recognition, _snapshot
from tests.memory_app.test_legacy_document_egress import (
    _authorize,
    _change,
    legacy_document,
)


@pytest.fixture
def legacy_artifact(legacy_document):
    env = legacy_document
    _authorize(env)
    records, service, scope = env["records"], env["service"], env["scope"]
    recognition = env["recognition"]
    packet_payload = {
        "id": "legacy-packet", "kind": "context", "project_id": scope.project_id,
        "query": "Use the reviewed evidence",
        "items": [{"id": recognition.id, "revision": recognition.revision}],
    }
    packet_payload["source_egress"] = capture_packet_egress(service, scope, packet_payload)
    env["egress"].require(packet_payload["source_egress"], "generation")
    packet_payload.update(state="consumed", task_id="legacy-task")
    document = env["documents"].create(DocumentDraft(
        title="Retained result", document_type="agent-result", markdown="Generated result",
        project_id=scope.project_id,
        source_refs=({"source_id": "legacy-task", "locator": "task://legacy-task"},),
    ))
    with records.begin() as tx:
        packet = tx.put("recognition_context_packets", "legacy-packet", packet_payload,
                        expected_revision=0)
        task = tx.put("recognition_tasks", "legacy-task", {
            "id": "legacy-task", "project_id": scope.project_id, "state": "completed",
            "document_id": document["id"], "context_packet_id": packet.object_id,
        }, expected_revision=0)
        tx.commit()
    artifact = service.stage_experience(
        scope=scope, content="Generated result", provenance={
            "kind": "model_generated_artifact", "actor": "agent", "source_refs": [
                {"type": "task", "id": task.object_id, "revision": task.revision},
                {"type": "document", "id": document["id"], "revision": document["revision"]},
                {"type": "context_packet", "id": packet.object_id, "revision": packet.revision},
            ],
        },
    )
    return {**env, "artifact": artifact, "packet_id": packet.object_id}


def test_legacy_recognition_result_can_be_retained_and_consumed_again(legacy_artifact):
    env = legacy_artifact
    snapshot = _snapshot(env["egress"], env["scope"], env["artifact"])
    env["egress"].require(snapshot, "generation")
    env["egress"].validate_snapshot(env["scope"], snapshot)
    env["egress"].require(snapshot, "embedding")
    env["egress"].require(snapshot, "rerank")

    descendant = _recognition(env["service"], env["scope"], env["artifact"])
    second_packet = capture_packet_egress(env["service"], env["scope"], {
        "kind": "context", "project_id": env["scope"].project_id,
        "items": [{"id": descendant.id, "revision": descendant.revision}],
    })
    env["egress"].require(second_packet, "generation")
    env["egress"].validate_snapshot(env["scope"], second_packet)
    dependencies = next(node["dependency_revisions"] for node in second_packet["nodes"]
                        if node["id"] == env["extracted"]["experience_id"])
    assert dependencies["legacy_review_id"] == "review-source-one"
    assert dependencies["confirmed_document_revision"] == env["extracted"]["document_revision"]


@pytest.mark.parametrize("mutation", ["missing-field", "extra-field", "invalid-id", "bool-revision"])
def test_retained_legacy_packet_rejects_malformed_dependencies(legacy_artifact, mutation):
    env = legacy_artifact
    packet = env["records"].read("recognition_context_packets", env["packet_id"])
    frozen = deepcopy(packet.payload["source_egress"])
    node = next(node for node in frozen["nodes"]
                if "legacy_review_id" in node.get("dependency_revisions", {}))
    dependencies = node["dependency_revisions"]
    if mutation == "missing-field":
        dependencies.pop("confirmed_document_revision")
    elif mutation == "extra-field":
        dependencies["unexpected"] = 1
    elif mutation == "invalid-id":
        dependencies["legacy_review_id"] = 1
    else:
        dependencies["legacy_review_revision"] = True
    _change(env, "recognition_context_packets", env["packet_id"], source_egress=frozen)

    # Keep the retained packet revision exact, so rejection tests its malformed
    # dependency fields rather than merely detecting an outdated packet ref.
    updated_packet = env["records"].read("recognition_context_packets", env["packet_id"])
    artifact = env["records"].read("recognition_experiences", env["artifact"])
    provenance = deepcopy(artifact.payload["provenance"])
    for ref in provenance["source_refs"]:
        if ref["type"] == "context_packet":
            ref["revision"] = updated_packet.revision
    _change(env, "recognition_experiences", env["artifact"], provenance=provenance)
    updated_artifact = env["records"].read("recognition_experiences", env["artifact"])
    with pytest.raises(RecognitionConflict, match="model artifact context source egress is invalid"):
        env["egress"].snapshot(env["scope"], [
            {"type": "experience", "id": env["artifact"], "revision": updated_artifact.revision},
        ])
