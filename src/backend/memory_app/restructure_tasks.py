"""Prepare governed restructuring packets without invoking a model."""

from uuid import uuid4

from backend.recognition import RecognitionConflict
from backend.recognition.restructuring import RestructureProposalService, _assert_snapshot_current, _validate_snapshot

from .constraints import ProjectConstraintService
from .context_adapter import compile_selected, _conservative_message_tokens, ContextSelectionError
from .restructure_generation import STEP_VERSION, build_messages
from .packet_egress import capture_packet_egress


def verify_restructure_snapshot(service, scope, packet):
    if packet.get("kind") != "restructure" or packet.get("step_version") != STEP_VERSION:
        raise RecognitionConflict("restructure step changed; preview again")
    snapshot = _validate_snapshot(scope, packet.get("snapshot"))
    with service.records.begin() as uow:
        _assert_snapshot_current(service, uow, scope, snapshot)
    return snapshot


def preview_restructure(service, models, *, scope, expected_revisions, operation, instruction):
    """Caller holds the application's mutation lock across capture and save."""
    snapshot = RestructureProposalService(service).capture(scope=scope,
        recognition_ids=tuple(expected_revisions), expected_revisions=expected_revisions)
    messages = build_messages(scope=scope, snapshot=snapshot, operation=operation, instruction=instruction)
    model_revision = models.public()["generation"]["revision"]
    constraints = ProjectConstraintService(service.records).active(scope)
    # Reuse the existing constraint message rules and budget policy. The
    # complete replacement messages are counted again before persistence.
    base = compile_selected(scope.project_id, [], [], instruction, model_revision, constraints=constraints)
    messages = [
        {"role": "system", "content": base["messages"][0]["content"] + "\n" + messages[0]["content"]},
        {"role": "user", "content": base["messages"][1]["content"] + "\n\n" + messages[1]["content"]},
    ]
    tokens = _conservative_message_tokens(messages)
    if tokens > base["usable_input_tokens"]:
        raise ContextSelectionError("restructure_context_exceeds_input_capacity")
    packet_id = f"restructure-{uuid4().hex}"
    payload = {"id": packet_id, "kind": "restructure", "state": "ready",
        "project_id": scope.project_id, "query": instruction, "instruction": instruction,
        "snapshot": snapshot, "operation": operation, "step_version": STEP_VERSION,
        "proposal_id": f"proposal-{uuid4().hex}", "messages": messages,
        "model_revision": model_revision, "constraints": base["constraints"],
        "items": [{"id": row["id"], "revision": row["revision"]} for row in snapshot["recognitions"]],
        "token_count": tokens, "token_estimate": base["token_estimate"],
        "max_input_tokens": base["max_input_tokens"], "usable_input_tokens": base["usable_input_tokens"],
        "structure_headroom_tokens": base["structure_headroom_tokens"]}
    payload["source_egress"] = capture_packet_egress(service, scope, payload)
    with service.records.begin() as uow:
        _assert_snapshot_current(service, uow, scope, snapshot)
        uow.put("recognition_context_packets", packet_id, payload, expected_revision=0)
        uow.commit()
    return payload
