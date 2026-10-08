from __future__ import annotations

import hashlib
import time
from queue import Empty, Queue
from threading import Thread
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from core.product_core.memory_projection_contract import (
    GENERATOR_POLICY_ID,
    PROJECTION_VERSION,
)
from core.product_core.memory_projection_repository import ProjectionReadResult
from core.product_core.progressive_memory_retrieval import (
    ProgressiveRetrievalPlan,
    RetrievalStagePlan,
    plan_progressive_memory_retrieval,
)
from core.product_core.progressive_recall_shadow import SeriesRouteDecision


SCHEMA_VERSION = "1.0.0"
BUNDLE_VERSION = "progressive-recall-context-v1"
TRACE_VERSION = "progressive-recall-drilldown-trace-v1"
ALLOWED_SIGNALS = frozenset(
    {
        "insufficient_evidence",
        "low_confidence",
        "conflicting_evidence",
        "explicit_detail_request",
        "source_verification_required",
    }
)
MAX_ITEM_CHARS = 2400
_FINGERPRINT_LENGTH = 64


class ProgressiveRecallDrilldownError(ValueError):
    """Raised when a progressive drilldown request violates its read contract."""


@dataclass(frozen=True, slots=True)
class EvidenceSourceRef:
    source_id: str
    locator: str
    source_content_hash: str

    def to_payload(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "locator": self.locator,
            "source_content_hash": self.source_content_hash,
        }


@dataclass(frozen=True, slots=True)
class AuthorityEvidenceCandidate:
    layer: str
    object_type: str
    object_id: str
    revision_identity: str
    content: str
    content_hash: str
    source_refs: tuple[EvidenceSourceRef, ...]
    series_id: str | None = None
    relevance_score: float = 0.0


class ProgressiveRecallAuthorityReaderPort(Protocol):
    def read_structured(
        self,
        *,
        project_id: str,
        series_ids: tuple[str, ...],
        allowed_source_refs: tuple[tuple[str, str], ...],
        query: str,
    ) -> Sequence[AuthorityEvidenceCandidate]:
        """Read current R2 candidates without changing authority."""

    def read_source_evidence(
        self,
        *,
        project_id: str,
        source_refs: tuple[EvidenceSourceRef, ...],
        allowed_source_refs: tuple[tuple[str, str], ...],
        query: str,
    ) -> Sequence[AuthorityEvidenceCandidate]:
        """Atomically authorize and read current R3 candidates."""


@dataclass(frozen=True, slots=True)
class ProgressiveRecallDrilldownResult:
    bundle: Mapping[str, object]
    trace: Mapping[str, object]


def run_progressive_recall_drilldown(
    *,
    project_id: str,
    query: str,
    projection_result: ProjectionReadResult,
    route: SeriesRouteDecision,
    authority_reader: ProgressiveRecallAuthorityReaderPort,
    escalation_signals: Sequence[str] = (),
    consumed_items: int = 0,
    consumed_chars: int = 0,
) -> ProgressiveRecallDrilldownResult:
    started = time.perf_counter()
    clean_project_id = _required_text(project_id, "project_id")
    plan = plan_progressive_memory_retrieval(query)
    signals = _signals(escalation_signals)
    _validate_consumed(consumed_items, consumed_chars, plan)
    projection, selected_series, allowed_refs = _projection_scope(
        clean_project_id,
        projection_result,
        route,
    )
    authority_fingerprint = _required_fingerprint(
        projection.get("authority_fingerprint")
    )
    remaining_items = max(0, plan.max_items - consumed_items)
    remaining_chars = max(0, plan.max_chars - consumed_chars)
    accepted: list[dict[str, object]] = []
    drop_codes: list[str] = []
    error_codes: list[str] = []
    stage_traces: list[dict[str, object]] = []

    r2_plan = _stage(plan, "r2_structured_content")
    r2_attempted, r2_reasons = _should_read(r2_plan, signals)
    r2_started = time.perf_counter()
    r2_candidates: Sequence[AuthorityEvidenceCandidate] = ()
    if r2_attempted and remaining_items and remaining_chars and allowed_refs:
        try:
            r2_candidates, timed_out = _bounded_read(
                lambda: authority_reader.read_structured(
                    project_id=clean_project_id,
                    series_ids=selected_series,
                    allowed_source_refs=allowed_refs,
                    query=query,
                ),
                timeout_ms=r2_plan.timeout_ms,
            )
            if timed_out:
                error_codes.append("r2_reader_timeout_partial")
        except Exception:
            error_codes.append("r2_reader_unavailable")
    r2_items, remaining_items, remaining_chars = _accept_candidates(
        r2_candidates,
        expected_layer="r2_structured_content",
        max_stage_items=r2_plan.max_items,
        max_stage_chars=r2_plan.max_chars,
        remaining_items=remaining_items,
        remaining_chars=remaining_chars,
        drop_codes=drop_codes,
        allowed_source_refs=allowed_refs,
    )
    accepted.extend(r2_items)
    r2_ms = _elapsed_ms(r2_started)
    stage_traces.append(
        _stage_trace(r2_plan.stage, r2_attempted, r2_items, r2_reasons)
    )

    r3_plan = _stage(plan, "r3_source_evidence")
    r3_attempted, r3_reasons = _should_read(r3_plan, signals)
    r3_started = time.perf_counter()
    r3_candidates: Sequence[AuthorityEvidenceCandidate] = ()
    r3_refs = _r3_refs(r2_items)
    if not r3_refs:
        r3_refs = _allowed_refs_with_hash(r2_candidates, allowed_refs)
    if r3_attempted and remaining_items and remaining_chars and allowed_refs:
        try:
            r3_candidates, timed_out = _bounded_read(
                lambda: authority_reader.read_source_evidence(
                    project_id=clean_project_id,
                    source_refs=r3_refs,
                    allowed_source_refs=allowed_refs,
                    query=query,
                ),
                timeout_ms=r3_plan.timeout_ms,
            )
            if timed_out:
                error_codes.append("r3_reader_timeout_partial")
        except Exception:
            error_codes.append("r3_reader_unavailable")
    r3_items, remaining_items, remaining_chars = _accept_candidates(
        r3_candidates,
        expected_layer="r3_source_evidence",
        max_stage_items=r3_plan.max_items,
        max_stage_chars=r3_plan.max_chars,
        remaining_items=remaining_items,
        remaining_chars=remaining_chars,
        drop_codes=drop_codes,
        allowed_source_refs=allowed_refs,
    )
    accepted.extend(r3_items)
    r3_ms = _elapsed_ms(r3_started)
    stage_traces.append(
        _stage_trace(r3_plan.stage, r3_attempted, r3_items, r3_reasons)
    )

    used_items = len(accepted)
    used_chars = sum(int(item["char_count"]) for item in accepted)
    identity = "\n".join(
        (
            clean_project_id,
            authority_fingerprint,
            plan.query_fingerprint,
            plan.intent,
            *(
                f"{item['layer']}:{item['object_id']}:{item['excerpt_hash']}"
                for item in accepted
            ),
        )
    )
    bundle_id = f"progressive-context-{_sha256(identity)[:40]}"
    budget = {
        "max_items": plan.max_items,
        "max_chars": plan.max_chars,
        "used_items": consumed_items + used_items,
        "used_chars": consumed_chars + used_chars,
        "remaining_items": remaining_items,
        "remaining_chars": remaining_chars,
    }
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "bundle_version": BUNDLE_VERSION,
        "bundle_id": bundle_id,
        "project_id": clean_project_id,
        "authority_fingerprint": authority_fingerprint,
        "query_fingerprint": plan.query_fingerprint,
        "intent": plan.intent,
        "stages_read": [
            stage["stage"] for stage in stage_traces if stage["attempted"]
        ],
        "items": accepted,
        "budget": budget,
        "drop_codes": sorted(set(drop_codes)),
        "safety": {
            "ephemeral": True,
            "read_only": True,
            "persistence_allowed": False,
            "logging_allowed": False,
            "provider_egress_allowed": False,
            "business_writes_allowed": False,
        },
    }
    trace_core = {
        "schema_version": SCHEMA_VERSION,
        "trace_version": TRACE_VERSION,
        "bundle_id": bundle_id,
        "project_id": clean_project_id,
        "authority_fingerprint": authority_fingerprint,
        "query_fingerprint": plan.query_fingerprint,
        "intent": plan.intent,
        "signals": list(signals),
        "stages": stage_traces,
        "budget": {
            key: budget[key]
            for key in (
                "used_items",
                "used_chars",
                "remaining_items",
                "remaining_chars",
            )
        },
        "drop_codes": sorted(set(drop_codes)),
        "error_codes": sorted(set(error_codes)),
        "performance": {
            "r2_ms": r2_ms,
            "r3_ms": r3_ms,
            "total_ms": _elapsed_ms(started),
        },
        "safety": {
            "content_included": False,
            "query_included": False,
            "locator_included": False,
            "path_included": False,
            "read_only": True,
            "production_prompt_changed": False,
        },
    }
    trace_id = f"progressive-drilldown-{_sha256(_trace_identity(trace_core))[:40]}"
    return ProgressiveRecallDrilldownResult(
        bundle=bundle,
        trace={"trace_id": trace_id, **trace_core},
    )


def _projection_scope(
    project_id: str,
    result: ProjectionReadResult,
    route: SeriesRouteDecision,
) -> tuple[Mapping[str, object], tuple[str, ...], tuple[tuple[str, str], ...]]:
    if (
        result.status != "fresh"
        or result.fallback_to_authority
        or not isinstance(result.projection, Mapping)
    ):
        raise ProgressiveRecallDrilldownError("fresh projection is required")
    projection = result.projection
    if (
        projection.get("project_id") != project_id
        or projection.get("projection_version") != PROJECTION_VERSION
        or projection.get("status") != "ready"
    ):
        raise ProgressiveRecallDrilldownError("projection identity is not current")
    if route.project_id != project_id:
        raise ProgressiveRecallDrilldownError("route project_id mismatch")
    series_ids = tuple(dict.fromkeys(item.series_id for item in route.candidates))
    if not series_ids:
        return projection, (), ()
    refs: list[tuple[str, str]] = []
    raw_r1 = projection.get("r1_items")
    if not isinstance(raw_r1, list):
        raise ProgressiveRecallDrilldownError("projection r1_items are invalid")
    for item in raw_r1:
        if not isinstance(item, Mapping) or item.get("series_id") not in series_ids:
            continue
        if (
            item.get("project_id") != project_id
            or item.get("authority_fingerprint")
            != projection.get("authority_fingerprint")
            or (
                item.get("generator_policy_id")
                or projection.get("generator_policy_id")
            )
            != GENERATOR_POLICY_ID
            or item.get("status") != "ready"
        ):
            raise ProgressiveRecallDrilldownError("selected r1 item is stale")
        for ref in _mapping_sequence(item.get("source_refs")):
            pair = (
                _required_text(ref.get("source_id"), "source_id"),
                _safe_locator(ref.get("locator")),
            )
            if pair not in refs:
                refs.append(pair)
    return projection, series_ids, tuple(refs)


def _accept_candidates(
    candidates: Sequence[AuthorityEvidenceCandidate],
    *,
    expected_layer: str,
    max_stage_items: int,
    max_stage_chars: int,
    remaining_items: int,
    remaining_chars: int,
    drop_codes: list[str],
    allowed_source_refs: tuple[tuple[str, str], ...],
) -> tuple[list[dict[str, object]], int, int]:
    accepted: list[dict[str, object]] = []
    stage_chars = 0
    seen: set[tuple[str, str]] = set()
    ordered = sorted(
        (item for item in candidates if isinstance(item, AuthorityEvidenceCandidate)),
        key=lambda item: (
            -item.relevance_score,
            item.object_type,
            item.object_id,
            item.revision_identity,
        ),
    )
    for candidate in ordered:
        if candidate.layer != expected_layer:
            drop_codes.append("candidate_layer_mismatch")
            continue
        key = (candidate.object_type, candidate.object_id)
        if key in seen:
            drop_codes.append("duplicate_candidate")
            continue
        if len(accepted) >= min(max_stage_items, remaining_items):
            drop_codes.append("stage_item_budget_exhausted")
            break
        content = candidate.content
        refs = _validated_candidate_refs(
            candidate.source_refs,
            allowed_source_refs,
        )
        if not content.strip() or not refs:
            drop_codes.append("candidate_missing_evidence")
            continue
        if _sha256(content) != _required_fingerprint(candidate.content_hash):
            drop_codes.append("candidate_content_hash_drift")
            continue
        room = min(
            MAX_ITEM_CHARS,
            max_stage_chars - stage_chars,
            remaining_chars,
        )
        if room <= 0:
            drop_codes.append("stage_char_budget_exhausted")
            break
        excerpt = content[:room]
        payload = {
            "layer": candidate.layer,
            "item_id": f"{candidate.layer}:{candidate.object_type}:{candidate.object_id}",
            "series_id": candidate.series_id,
            "object_type": candidate.object_type,
            "object_id": candidate.object_id,
            "revision_identity": candidate.revision_identity,
            "content": excerpt,
            "content_hash": _required_fingerprint(candidate.content_hash),
            "excerpt_hash": _sha256(excerpt),
            "source_refs": [ref.to_payload() for ref in refs],
            "char_count": len(excerpt),
            "truncated": len(excerpt) < len(content),
        }
        accepted.append(payload)
        seen.add(key)
        stage_chars += len(excerpt)
        remaining_chars -= len(excerpt)
        remaining_items -= 1
    return accepted, remaining_items, remaining_chars


def _bounded_read(call, *, timeout_ms: int) -> tuple[Sequence[AuthorityEvidenceCandidate], bool]:
    """Bound a read-only authority call without making timeout a Turn failure.

    A daemon thread is used because the reader contract is read-only and Python
    cannot safely interrupt arbitrary synchronous I/O.  A late result is
    discarded; it has no authority to write or publish anything.
    """
    result: Queue[tuple[str, object]] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            result.put_nowait(("ok", call()))
        except Exception as error:  # caller maps this to a safe availability code
            result.put_nowait(("error", error))

    worker = Thread(target=invoke, daemon=True, name="memory-retrieval-read")
    worker.start()
    try:
        status, value = result.get(timeout=timeout_ms / 1000)
    except Empty:
        return (), True
    if status == "error":
        raise RuntimeError("memory authority reader unavailable") from value
    if not isinstance(value, Sequence):
        raise RuntimeError("memory authority reader returned invalid result")
    return value, False


def _should_read(
    stage: RetrievalStagePlan,
    signals: tuple[str, ...],
) -> tuple[bool, tuple[str, ...]]:
    reasons = tuple(
        signal for signal in signals if signal in stage.escalate_when
    )
    if (
        "source_verification_required" in signals
        and stage.stage in {"r2_structured_content", "r3_source_evidence"}
        and "source_verification_required" not in reasons
    ):
        reasons = (*reasons, "source_verification_required")
    if stage.initial:
        return True, ("planned_initial", *reasons)
    return bool(reasons), reasons


def _stage(
    plan: ProgressiveRetrievalPlan,
    name: str,
) -> RetrievalStagePlan:
    return next(stage for stage in plan.stages if stage.stage == name)


def _stage_trace(
    stage: str,
    attempted: bool,
    items: Sequence[Mapping[str, object]],
    reasons: tuple[str, ...],
) -> dict[str, object]:
    return {
        "stage": stage,
        "attempted": attempted,
        "item_count": len(items),
        "char_count": sum(int(item["char_count"]) for item in items),
        "reason_codes": list(reasons),
    }


def _r3_refs(items: Sequence[Mapping[str, object]]) -> tuple[EvidenceSourceRef, ...]:
    refs: list[EvidenceSourceRef] = []
    for item in items:
        for payload in _mapping_sequence(item.get("source_refs")):
            ref = EvidenceSourceRef(
                source_id=_required_text(payload.get("source_id"), "source_id"),
                locator=_safe_locator(payload.get("locator")),
                source_content_hash=_required_fingerprint(
                    payload.get("source_content_hash")
                ),
            )
            if ref not in refs:
                refs.append(ref)
    return tuple(refs)


def _allowed_refs_with_hash(
    candidates: Sequence[AuthorityEvidenceCandidate],
    allowed_refs: tuple[tuple[str, str], ...],
) -> tuple[EvidenceSourceRef, ...]:
    allowed = set(allowed_refs)
    refs: list[EvidenceSourceRef] = []
    for candidate in candidates:
        for ref in candidate.source_refs:
            if (ref.source_id, ref.locator) in allowed and ref not in refs:
                refs.append(ref)
    return tuple(refs)


def _validated_candidate_refs(
    refs: Sequence[EvidenceSourceRef],
    allowed_source_refs: tuple[tuple[str, str], ...],
) -> tuple[EvidenceSourceRef, ...]:
    allowed = set(allowed_source_refs)
    result: list[EvidenceSourceRef] = []
    for ref in refs:
        if not isinstance(ref, EvidenceSourceRef):
            continue
        normalized = EvidenceSourceRef(
            source_id=_required_text(ref.source_id, "source_id"),
            locator=_safe_locator(ref.locator),
            source_content_hash=_required_fingerprint(ref.source_content_hash),
        )
        if (normalized.source_id, normalized.locator) not in allowed:
            continue
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _signals(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ProgressiveRecallDrilldownError("signals must be a sequence")
    result: list[str] = []
    for value in values:
        if value not in ALLOWED_SIGNALS:
            raise ProgressiveRecallDrilldownError("unsupported escalation signal")
        if value not in result:
            result.append(value)
    return tuple(result)


def _validate_consumed(
    items: int,
    chars: int,
    plan: ProgressiveRetrievalPlan,
) -> None:
    if (
        not isinstance(items, int)
        or isinstance(items, bool)
        or not 0 <= items <= plan.max_items
        or not isinstance(chars, int)
        or isinstance(chars, bool)
        or not 0 <= chars <= plan.max_chars
    ):
        raise ProgressiveRecallDrilldownError("consumed budget is invalid")


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _trace_identity(trace: Mapping[str, object]) -> str:
    stages = trace.get("stages")
    stage_identity = ""
    if isinstance(stages, list):
        stage_identity = "\n".join(
            f"{item.get('stage')}:{item.get('attempted')}:{item.get('item_count')}:{item.get('char_count')}"
            for item in stages
            if isinstance(item, Mapping)
        )
    return "\n".join(
        (
            str(trace.get("bundle_id")),
            str(trace.get("project_id")),
            str(trace.get("authority_fingerprint")),
            str(trace.get("query_fingerprint")),
            str(trace.get("intent")),
            ",".join(str(item) for item in trace.get("signals", ())),
            ",".join(str(item) for item in trace.get("drop_codes", ())),
            ",".join(str(item) for item in trace.get("error_codes", ())),
            stage_identity,
        )
    )


def _safe_locator(value: object) -> str:
    locator = _required_text(value, "locator")
    if (
        locator.startswith(("/", "\\"))
        or "\\" in locator
        or (len(locator) > 2 and locator[1] == ":")
    ):
        raise ProgressiveRecallDrilldownError("locator must be platform neutral")
    return locator


def _required_fingerprint(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _FINGERPRINT_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProgressiveRecallDrilldownError("SHA-256 fingerprint is required")
    return value


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProgressiveRecallDrilldownError(f"{field} is required")
    return value.strip()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _elapsed_ms(started: float) -> float:
    return round(max(0.0, (time.perf_counter() - started) * 1000), 3)
