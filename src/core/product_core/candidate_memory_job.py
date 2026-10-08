from __future__ import annotations

import hashlib
from dataclasses import dataclass

from core.effect_log import EFFECT_V2


class CandidateMemoryJobContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CandidateMemoryJobInput:
    parent_job_id: str
    source_id: str
    project_id: str
    evidence_kind: str
    evidence_id: str


def build_candidate_memory_job(value: CandidateMemoryJobInput, *, now: str) -> dict[str, object]:
    if not isinstance(value, CandidateMemoryJobInput):
        raise CandidateMemoryJobContractError("candidate Job input is invalid")
    parent = _required(value.parent_job_id, "parent_job_id")
    source = _required(value.source_id, "source_id")
    project = _required(value.project_id, "project_id")
    evidence = _required(value.evidence_id, "evidence_id")
    if value.evidence_kind != "source_content_read":
        raise CandidateMemoryJobContractError("candidate Job evidence_kind is unsupported")
    timestamp = _required(now, "now")
    digest = hashlib.sha256("\x1f".join((parent, source, project, value.evidence_kind, evidence)).encode()).hexdigest()[:24]
    job_id = f"job-extract-memory-candidate-{digest}"
    return {
        "schema_version": "1.0.0",
        "id": job_id,
        "job_type": "extract_memory_candidate",
        "execution_version": EFFECT_V2,
        "parent_job_id": parent,
        "source_id": source,
        "project_id": project,
        "evidence_kind": value.evidence_kind,
        "evidence_id": evidence,
        "idempotency_key": job_id,
        "status": "pending",
        "attempt": 0,
        "max_attempts": 3,
        "lease": None,
        "progress": {"current": 0, "total": 1, "percent": 0, "message": "awaiting candidate creation"},
        "steps": [{"name": "create_candidate", "status": "pending", "error": None}],
        "error": None,
        "checkpoint": None,
        "staged_outputs": [],
        "published_outputs": [],
        "events": [],
        "input_refs": [
            {"kind": "source", "object_id": source, "uri": f"crp://default/sources/{source}"},
            {"kind": value.evidence_kind, "object_id": evidence, "uri": f"crp://default/source-content-reads/{evidence}.json"},
        ],
        "candidate_policy": {"requires_user_confirmation": True, "auto_promote_allowed": False},
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _required(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateMemoryJobContractError(f"{label} is required")
    return value.strip()
