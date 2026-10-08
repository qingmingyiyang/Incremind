"""Immutable manual-review facts paired with a staging Memory draft."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


STAGING_PUBLICATION_CONTEXT_COLLECTION = "staging_memory_publication_contexts"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


class ManualPublicationContractError(ValueError):
    pass


def context_id(layer: str, draft_id: str) -> str:
    _require_layer(layer)
    _require_safe("draft_id", draft_id)
    return f"{layer}~{draft_id}"


def build_context(
    *,
    namespace_id: str,
    layer: str,
    draft_id: str,
    candidate_id: str,
    reviewed_at: str,
    review_reason: str,
    source_refs: Sequence[Mapping[str, object]],
    evidence_refs: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    _require_safe("namespace_id", namespace_id)
    _require_layer(layer)
    _require_safe("draft_id", draft_id)
    _require_safe("candidate_id", candidate_id)
    if (
        not isinstance(reviewed_at, str)
        or not reviewed_at
        or not isinstance(review_reason, str)
        or not review_reason.strip()
    ):
        raise ManualPublicationContractError("manual review facts are invalid")
    return {
        "schema_version": "1.0.0",
        "id": context_id(layer, draft_id),
        "layer": layer,
        "draft_id": draft_id,
        "source_candidate_id": candidate_id,
        "review_ref": f"crp://{namespace_id}/memory-candidates/{candidate_id}.json",
        "reviewer": "user",
        "reviewed_at": reviewed_at,
        "review_reason": review_reason.strip(),
        "policy_id": "local-manual-v1",
        "source_refs": _refs(source_refs, "source_refs"),
        "evidence_refs": _refs(evidence_refs, "evidence_refs"),
    }


def validate_context(value: Mapping[str, object], *, namespace_id: str, layer: str, draft_id: str) -> dict[str, object]:
    required = {
        "schema_version",
        "id",
        "layer",
        "draft_id",
        "source_candidate_id",
        "review_ref",
        "reviewer",
        "reviewed_at",
        "review_reason",
        "policy_id",
        "source_refs",
        "evidence_refs",
    }
    if (
        set(value) != required
        or value.get("schema_version") != "1.0.0"
        or value.get("id") != context_id(layer, draft_id)
        or value.get("layer") != layer
        or value.get("draft_id") != draft_id
    ):
        raise ManualPublicationContractError("manual publication context is invalid")
    candidate = value.get("source_candidate_id")
    if not isinstance(candidate, str):
        raise ManualPublicationContractError("manual publication context is invalid")
    _require_safe("candidate_id", candidate)
    if (
        value.get("review_ref") != f"crp://{namespace_id}/memory-candidates/{candidate}.json"
        or value.get("reviewer") != "user"
        or value.get("policy_id") != "local-manual-v1"
    ):
        raise ManualPublicationContractError("manual publication context is invalid")
    if (
        not isinstance(value.get("reviewed_at"), str)
        or not value["reviewed_at"]
        or not isinstance(value.get("review_reason"), str)
        or not value["review_reason"].strip()
    ):
        raise ManualPublicationContractError("manual publication context is invalid")
    return {
        **dict(value),
        "source_refs": _refs(value.get("source_refs"), "source_refs"),
        "evidence_refs": _refs(value.get("evidence_refs"), "evidence_refs"),
    }


def build_record(
    *, context: Mapping[str, object], namespace_id: str, layer: str, draft_id: str, revision: int, published_at: str
) -> dict[str, object]:
    context = validate_context(context, namespace_id=namespace_id, layer=layer, draft_id=draft_id)
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(published_at, str)
        or not published_at
    ):
        raise ManualPublicationContractError("manual publication facts are invalid")
    slug = layer.replace("_", "-")
    publication_id = f"memory-publication-{slug}-{draft_id}"
    transition_id = f"transition-memory-publication-{slug}-{draft_id}"
    ref_layer = {"atom": "atom", "scenario": "scenario", "series_memory": "series", "project_skill": "project-skill"}[
        layer
    ]
    return {
        "schema_version": "1.0.0",
        "id": publication_id,
        "publication_id": publication_id,
        "layer": layer,
        "object_type": layer,
        "object_id": draft_id,
        "published_object_id": draft_id,
        "published_revision": revision,
        "status": "published",
        "source_candidate_id": context["source_candidate_id"],
        "review_ref": context["review_ref"],
        "reviewer": "user",
        "reviewed_at": context["reviewed_at"],
        "policy_id": "local-manual-v1",
        "published_by": "user",
        "published_at": published_at,
        "reason": context["review_reason"],
        "published_ref": f"crp://{namespace_id}/memory/{ref_layer}/{draft_id}.json",
        "transition_ref": f"crp://{namespace_id}/memory-transitions/{transition_id}.json",
        "rollback_ref": f"crp://{namespace_id}/memory-publications/{publication_id}/rollback",
        "source_refs": context["source_refs"],
        "evidence_refs": context["evidence_refs"],
        "created_at": published_at,
    }


def build_replacement_record(
    *,
    context: Mapping[str, object],
    namespace_id: str,
    layer: str,
    draft_id: str,
    revision: int,
    supersedes_publication: Mapping[str, object],
    published_at: str,
) -> dict[str, object]:
    """Build one append-only publication event for a confirmed replacement.

    A replacement must not reuse the original publication id: that would
    overwrite the audit event that established the previous current revision.
    """

    if revision < 2:
        raise ManualPublicationContractError("replacement revision must advance an existing publication")
    previous_id = supersedes_publication.get("publication_id")
    if (
        supersedes_publication.get("id") != previous_id
        or supersedes_publication.get("layer") != layer
        or supersedes_publication.get("published_object_id") != draft_id
        or supersedes_publication.get("published_revision") != revision - 1
        or supersedes_publication.get("status") != "published"
    ):
        raise ManualPublicationContractError("replacement publication baseline is invalid")
    if not isinstance(previous_id, str) or not previous_id:
        raise ManualPublicationContractError("replacement publication baseline is invalid")
    record = build_record(
        context=context,
        namespace_id=namespace_id,
        layer=layer,
        draft_id=draft_id,
        revision=revision,
        published_at=published_at,
    )
    slug = layer.replace("_", "-")
    publication_id = f"memory-publication-{slug}-{draft_id}-r{revision}"
    transition_id = f"transition-memory-publication-{slug}-{draft_id}-r{revision}"
    return {
        **record,
        "id": publication_id,
        "publication_id": publication_id,
        "transition_ref": f"crp://{namespace_id}/memory-transitions/{transition_id}.json",
        "rollback_ref": f"crp://{namespace_id}/memory-publications/{publication_id}/rollback",
        "supersedes_publication_id": previous_id,
        "supersedes_revision": revision - 1,
    }


def _refs(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ManualPublicationContractError(f"{label} is required")
    result = []
    for item in value:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("source_id"), str)
            or not item["source_id"]
            or not isinstance(item.get("locator"), str)
            or not item["locator"]
        ):
            raise ManualPublicationContractError(f"{label} is invalid")
        result.append(dict(item))
    if not result:
        raise ManualPublicationContractError(f"{label} is required")
    return result


def _require_layer(value: str) -> None:
    if value not in {"atom", "scenario", "series_memory", "project_skill"}:
        raise ManualPublicationContractError("manual publication layer is invalid")


def _require_safe(label: str, value: str) -> None:
    if not isinstance(value, str) or not _SAFE.fullmatch(value):
        raise ManualPublicationContractError(f"{label} is invalid")
