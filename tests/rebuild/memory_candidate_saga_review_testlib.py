"""Shared saga-backed Memory candidate review wiring for tests.

Mirrors the production review endpoint: candidate promotion happens only
through the durable Memory publication review staging saga (SQLite staging
authority), never through a direct domain dual-write path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.memory_core.publication_trust_audit_uow import (
    SQLiteMemoryPublicationTrustAuditUnitOfWork,
)
from core.product_core import (
    MemoryCandidateReviewError,
    MemoryCandidateReviewResult,
    MemoryPublicationReviewStagingSagaService,
    MemoryPublicationReviewStagingServiceConflict,
    MemoryPublicationReviewStagingServiceError,
)
from core.storage_provider import (
    JsonObjectStore,
    SQLiteMemoryPublicationReviewStagingSagaStore,
    SQLiteStructuredRecordStore,
)


def saga_records(runtime_root: Path) -> SQLiteStructuredRecordStore:
    """Return the SQLite staging authority used by the review saga."""

    return SQLiteStructuredRecordStore(runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME)


def build_saga_service(
    object_store: JsonObjectStore,
    runtime_root: Path,
) -> MemoryPublicationReviewStagingSagaService:
    records = saga_records(runtime_root)
    return MemoryPublicationReviewStagingSagaService(
        object_store,
        records,
        SQLiteMemoryPublicationReviewStagingSagaStore(records),
    )


def review_candidate_to_staging(
    object_store: JsonObjectStore,
    runtime_root: Path,
    candidate_id: str,
    *,
    review_reason: str,
    reviewed_at: str,
    **kwargs: Any,
):
    """Promote one candidate through the durable saga; return the finalized operation."""

    return build_saga_service(object_store, runtime_root).review_to_staging(
        candidate_id,
        review_reason=review_reason,
        reviewed_at=reviewed_at,
        **kwargs,
    )


def publish_staging_user_confirmed(
    runtime_root: Path,
    *,
    layer: str,
    staged_id: str,
    published_at: str,
):
    """Publish one staged Memory through the SQLite trust-audit authority."""

    with SQLiteMemoryPublicationTrustAuditUnitOfWork(
        runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    ).begin() as transaction:
        result = transaction.publish_user_confirmed(
            layer=layer,
            staged_id=staged_id,
            published_at=published_at,
        )
        transaction.commit()
    return result


DEFAULT_REVIEWED_AT = "2026-07-12T19:05:00+08:00"


def saga_promote_closures(
    object_store: JsonObjectStore,
    runtime_root: Path,
) -> tuple[Any, Any]:
    """Return (promote_to_atom, promote_to_layer) wired like the review route."""

    service = build_saga_service(object_store, runtime_root)

    def promote(
        candidate_id: str,
        *,
        target_layer: str,
        reason: str,
        reviewed_by: str = "user",
        reviewed_at: str = DEFAULT_REVIEWED_AT,
        atom_type: str | None = None,
        tags: tuple[str, ...] = (),
        confidence: float = 0.7,
        series_id: str | None = None,
        scenario_ids: tuple[str, ...] = (),
        atom_ids: tuple[str, ...] = (),
    ) -> MemoryCandidateReviewResult:
        if reviewed_by != "user":
            raise MemoryCandidateReviewError("generic Memory review requires user")
        try:
            operation = service.review_to_staging(
                candidate_id,
                review_reason=reason,
                reviewed_at=reviewed_at,
                atom_type=atom_type,
                tags=tags,
                confidence=confidence,
                series_id=series_id,
                scenario_ids=scenario_ids,
                atom_ids=atom_ids,
            )
        except (
            MemoryPublicationReviewStagingServiceConflict,
            MemoryPublicationReviewStagingServiceError,
        ) as error:
            raise MemoryCandidateReviewError(str(error)) from error
        return MemoryCandidateReviewResult(
            candidate_id=candidate_id,
            status="promoted",
            reviewed_by="user",
            reviewed_at=str(operation.context["reviewed_at"]),
            promoted_layer=target_layer,
            promoted_object_id=operation.evidence.draft_id,
        )

    def promote_to_atom(
        candidate_id: str,
        *,
        reason: str,
        reviewed_by: str = "user",
        reviewed_at: str = DEFAULT_REVIEWED_AT,
        atom_type: str | None = None,
        tags: tuple[str, ...] = (),
        confidence: float = 0.7,
    ) -> MemoryCandidateReviewResult:
        return promote(
            candidate_id,
            target_layer="atom",
            reason=reason,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            atom_type=atom_type,
            tags=tags,
            confidence=confidence,
        )

    def promote_to_layer(
        candidate_id: str,
        *,
        target_layer: str,
        reason: str,
        reviewed_by: str = "user",
        reviewed_at: str = DEFAULT_REVIEWED_AT,
        atom_type: str | None = None,
        tags: tuple[str, ...] = (),
        confidence: float = 0.7,
        series_id: str | None = None,
        scenario_ids: tuple[str, ...] = (),
        atom_ids: tuple[str, ...] = (),
    ) -> MemoryCandidateReviewResult:
        return promote(
            candidate_id,
            target_layer=target_layer,
            reason=reason,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            atom_type=atom_type,
            tags=tags,
            confidence=confidence,
            series_id=series_id,
            scenario_ids=scenario_ids,
            atom_ids=atom_ids,
        )

    return promote_to_atom, promote_to_layer

