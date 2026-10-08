from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TurnModelObservation:
    """Auditable model outcome captured from the platform Turn evidence chain."""

    suite_run_id: str
    replicate_index: int
    operation_id: str
    case_id: str
    variant: str
    turn_id: str
    turn_terminal_event_id: str
    model_receipt_ref: str
    routing_snapshot_ref: str
    model_request_id: str
    model_attempt_id: str
    routing_snapshot_revision: str
    status: str
    output_text: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    route_key: str
    route_revision: str
    provider_id: str
    provider_revision: str
    model_name: str
    execution_location: str
    capability_revision: str
    compiler_revision: str
    boundary_revision: str
    decoding_revision: str
