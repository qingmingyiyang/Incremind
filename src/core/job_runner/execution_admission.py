"""Atomic v2 admission of the executable Effect behind a Job projection.

This is deliberately a narrow command-boundary composition seam.  It accepts
durable Gate evidence rather than evaluating policy, and it writes only facts
and a derived Job projection in the transaction owned by its caller.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from core.effect_log import EFFECT_V2, Effect, EffectIntent, EffectLog

from .admission import JobAdmissionAuthorization
from .job_projection import (
    JobProjectionBuilder,
    JobProjectionRecord,
    _non_authoritative_fact_payload,
)
from .legacy_history import LegacyJobHistoryProjection


@dataclass(frozen=True, slots=True)
class JobExecutionAdmission:
    """The immutable execution Effect and its derived Job query record."""

    record: JobProjectionRecord
    effect: Effect
    created: bool


class JobExecutionAdmissionAuthority:
    """Compose authorized v2 execution planning with a Job query projection.

    The caller opens, commits, or rolls back the SQLite transaction.  This
    authority never creates a Gate decision and never transitions an Effect.
    """

    def __init__(self, effect_log: EffectLog, projection_builder: JobProjectionBuilder) -> None:
        self._effects = effect_log
        self._projection = projection_builder
        self._legacy_history = LegacyJobHistoryProjection()

    def admit_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        payload: Mapping[str, object],
        authorization: JobAdmissionAuthorization,
        intent: EffectIntent,
    ) -> JobExecutionAdmission:
        """Plan one authorized execution Effect inside the caller transaction.

        The Job payload remains a non-authoritative display fact.  Execution
        state is derived from the returned v2 Effect registered as the attempt
        node, so this method cannot make Job status or lease authoritative.
        """

        if not connection.in_transaction:
            raise RuntimeError("Job execution admission requires a caller-owned transaction")
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        job_id = payload.get("id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("Job execution payload requires a non-empty id")
        attempt = payload.get("attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
            raise ValueError("Job execution payload requires a non-negative attempt")
        if intent.contract_version != EFFECT_V2:
            raise ValueError("Job execution admission requires an effect-v2 intent")
        if intent.parent_id is not None:
            raise ValueError("Job execution Effect must be the Job subtree root")
        if authorization.job_id != job_id:
            raise ValueError("Job execution payload identity drifted from authorization")
        if intent.payload.get("attempt_index") != attempt:
            raise ValueError("Job execution attempt drifted from intent")
        if intent.payload.get("job_ref") != authorization.admission_ref:
            raise ValueError("Job execution fact reference drifted from authorization")
        authorization.validate_for_intent(intent)

        try:
            self._legacy_history.read_in_connection(connection, job_id=job_id)
        except KeyError:
            pass
        else:
            raise ValueError(
                "legacy Job history cannot be admitted as executable Effect"
            )

        effect, created = self._effects.plan_v2_in_connection(
            connection,
            intent,
            gate_decision_id=authorization.gate_decision_id,
            gate_fact=authorization.gate_fact,
            now=authorization.admitted_at,
        )
        self._projection.register_node_in_connection(
            connection,
            job_id=job_id,
            node_kind="attempt",
            node_key="execution",
            attempt=attempt,
            effect_operation_id=effect.operation_id,
        )
        if created:
            recorded_at = datetime.fromtimestamp(
                authorization.admitted_at, timezone.utc,
            ).isoformat()
            self._projection.append_fact_in_connection(
                connection,
                job_id=job_id,
                effect_operation_id=effect.operation_id,
                payload=payload,
                recorded_at=recorded_at,
            )
            record = self._projection.rebuild_in_connection(
                connection,
                job_id=job_id,
                rebuilt_at=recorded_at,
            )
        else:
            # Replays are read-only: the original immutable fact and cache are
            # already the source of the returned projection.  The Job metadata
            # must also be byte-for-byte equivalent after authority fields are
            # removed; a stable Effect identity cannot hide a changed Job body.
            stored = connection.execute(
                "SELECT payload_json FROM job_effect_fact "
                "WHERE job_id=? AND effect_operation_id=? "
                "ORDER BY sequence ASC LIMIT 1",
                (job_id, effect.operation_id),
            ).fetchone()
            expected = json.dumps(
                _non_authoritative_fact_payload(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if stored is None or str(stored[0]) != expected:
                raise ValueError("Job execution replay metadata drifted")
            record = self._projection.derive_in_connection(connection, job_id=job_id)
        return JobExecutionAdmission(record=record, effect=effect, created=created)
