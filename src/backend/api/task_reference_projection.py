"""Read-only TaskReference projections over existing durable owners.

This adapter deliberately owns no task state. It exposes only owners whose
identity, project scope and public projection are already durable. The first
supported owner is a Bilibili favorite batch; other task families must opt in
with the same fixed-identity and read-back requirements.
"""
from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import quote

from backend.api.bilibili_favorite_batch import BilibiliFavoriteBatchRepository
from core.task_reference_contract import task_updated_utc_key, workbench_transform_task_reference


class TaskReferenceError(ValueError):
    pass


_VERSION = "task-ref.v1"
_FAVORITE_BATCH_KIND = "bilibili_favorite_batch"
_MEDIA_PROCESSING_JOB_KIND = "media_processing_job"
_WORLD_ACTION_KIND = "world_action"
_WORKBENCH_CONTENT_TRANSFORM_KIND = "workbench_content_transform"
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


def task_ref_for_favorite_batch(*, project_id: str, batch_id: str) -> str:
    """Create a deterministic opaque reference for one durable batch owner."""
    return _encode({"v": _VERSION, "k": _FAVORITE_BATCH_KIND, "p": project_id, "i": batch_id})


def task_ref_for_media_processing_job(*, project_id: str, job_id: str) -> str:
    """Create a deterministic opaque reference for one Source-anchored media job."""
    return _encode({"v": _VERSION, "k": _MEDIA_PROCESSING_JOB_KIND, "p": project_id, "i": job_id})


def task_ref_for_world_action(*, project_id: str, action_id: str) -> str:
    """Create a deterministic opaque reference for one planned World action."""
    return _encode({"v": _VERSION, "k": _WORLD_ACTION_KIND, "p": project_id, "i": action_id})


def task_ref_for_workbench_content_transform(*, project_id: str, job_id: str) -> str:
    """Create a stable reference for one Source-scoped content transform."""
    return workbench_transform_task_reference(project_id=project_id, job_id=job_id)


def workbench_transform_task_ref_for_document(
    *,
    document: Mapping[str, object],
    object_store: TaskReferenceObjectStore,
    jobs: Sequence[Mapping[str, object]],
    receipt_reader: Callable[[str], Mapping[str, object] | None],
    document_revision_reader: Callable[[str, int], Mapping[str, object] | None],
    document_markdown_reader: Callable[[str, int], str | None],
) -> str | None:
    """Return a task reference only for one fully verified transform owner.

    This derived lookup scans the already bounded transform Job family, never
    Documents or generic Jobs.  Ambiguous ownership fails closed instead of
    choosing a source task for a document that could expose another project.
    """
    document_id, project_id = _text(document.get("id")), _text(document.get("project_id"))
    if not document_id or not project_id:
        return None
    matches: list[str] = []
    for job in jobs:
        owner = _workbench_transform_owner(object_store, job, project_id=project_id)
        if owner is None:
            continue
        outputs = _workbench_transform_outputs(
            owner, receipt_reader, lambda candidate_id: document if candidate_id == document_id else None,
            document_revision_reader, document_markdown_reader,
        )
        if any(_text(output.get("artifact_id")) == document_id for output in outputs):
            job_id = _text(job.get("id"))
            if job_id:
                matches.append(task_ref_for_workbench_content_transform(project_id=project_id, job_id=job_id))
    return matches[0] if len(matches) == 1 else None


class TaskReferenceObjectStore(Protocol):
    """The read-only object-store surface needed by media task projections."""

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None: ...

    def list(self, collection: str) -> Sequence[Mapping[str, object]]: ...


def list_task_references(
    *,
    repository: BilibiliFavoriteBatchRepository,
    project_id: str | None,
    project_batch_projection: Callable[[Mapping[str, object]], Mapping[str, object]],
    object_store: TaskReferenceObjectStore | None = None,
    world_action_overview: Callable[[str], Mapping[str, object]] | None = None,
    world_action_status: Callable[[str, str], Mapping[str, object]] | None = None,
    workbench_transform_jobs: Callable[[], Sequence[Mapping[str, object]]] | None = None,
    workbench_transform_job_page: Callable[[tuple[str, str] | None, int], Sequence[Mapping[str, object]]] | None = None,
    workbench_transform_job_reader: Callable[[str], Mapping[str, object] | None] | None = None,
    workbench_transform_receipt: Callable[[str], Mapping[str, object] | None] | None = None,
    document_reader: Callable[[str], Mapping[str, object] | None] | None = None,
    document_revision_reader: Callable[[str, int], Mapping[str, object] | None] | None = None,
    document_markdown_reader: Callable[[str, int], str | None] | None = None,
    limit: int = 50,
    cursor: str | None = None,
    filter: str | None = None,
) -> dict[str, object]:
    """List the durable owners currently supported by the task adapter.

    A cursor is the last opaque reference from the same time-and-reference
    sorted owner list.  The filter is applied before cursor lookup, so a
    cursor can never advance through an owner that is outside the visible
    server-side scope.
    It never accepts or returns a storage path, internal collection name or
    client-selected owner identifier.
    """
    if limit < 1 or limit > 100:
        raise TaskReferenceError("task page limit is invalid")
    _project(project_id)
    records = repository.list(project_id=project_id)
    items = [_favorite_batch_summary(record.payload, project_batch_projection(record.payload)) for record in records]
    if object_store is not None:
        items.extend(_media_processing_job_summaries(object_store, project_id=project_id))
    if world_action_overview is not None and world_action_status is not None:
        items.extend(_world_action_summaries(
            project_id=project_id,
            overview=world_action_overview(project_id=project_id),
            action_status=world_action_status,
        ))
    if (
        object_store is not None
        and (workbench_transform_jobs is not None or workbench_transform_job_page is not None)
        and workbench_transform_receipt is not None
        and document_reader is not None
        and document_revision_reader is not None
        and document_markdown_reader is not None
    ):
        if workbench_transform_job_page is None:
            items.extend(_workbench_transform_candidates(workbench_transform_jobs(), project_id=project_id))
    items.sort(key=_task_reference_sort_key, reverse=True)
    if workbench_transform_job_page is not None:
        return _page_task_candidates_with_transform_batches(
            items, project_id=project_id, cursor=cursor, filter=filter, limit=limit,
            job_page=workbench_transform_job_page,
            job_reader=workbench_transform_job_reader,
            materialize_transform=lambda candidate: _materialize_workbench_transform_candidate(
                candidate, object_store=object_store, receipt_reader=workbench_transform_receipt,
                document_reader=document_reader, revision_reader=document_revision_reader,
                markdown_reader=document_markdown_reader,
            ),
        )
    page, has_more = _page_task_candidates(
        items,
        project_id=project_id,
        cursor=cursor,
        filter=filter,
        limit=limit,
        materialize_transform=lambda candidate: _materialize_workbench_transform_candidate(
            candidate, object_store=object_store, receipt_reader=workbench_transform_receipt,
            document_reader=document_reader, revision_reader=document_revision_reader,
            markdown_reader=document_markdown_reader,
        ),
    )
    return {
        "items": page,
        "next_cursor": page[-1]["task_ref"] if page and has_more else None,
    }


def _task_reference_sort_key(item: Mapping[str, object]) -> tuple[datetime, str]:
    """Keep mixed durable owners in one deterministic newest-first order.

    Owners that do not expose a usable public update time remain discoverable,
    but sort after timestamped records using their opaque reference as a
    stable tie-breaker.  The reference has no storage identity or path.
    """
    return (_timestamp_sort_key(_text(item.get("updated_at"))), _text(item.get("task_ref")))


def _timestamp_sort_key(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _task_timestamp_cursor(value: str) -> str:
    return task_updated_utc_key(value)


def _page_task_candidates(
    candidates: Sequence[dict[str, object]], *, project_id: str, cursor: str | None,
    filter: str | None, limit: int,
    materialize_transform: Callable[[Mapping[str, object]], dict[str, object] | None],
) -> tuple[list[dict[str, object]], bool]:
    """Materialize Source/receipt-backed owners only as a page needs them.

    The candidate reference is derived only from the requested project and a
    durable Job identity.  It is never returned until Source scope and, for a
    completed transform, every published artifact has been read back.
    This retains the global sort before filtering without an arbitrary
    pre-filter cutoff.  A one-item lookahead makes ``next_cursor`` exact.
    """
    if filter not in {None, "attention", "active", "delivered"}:
        raise TaskReferenceError("task filter is invalid")
    start = 0
    if cursor is not None:
        reference = _decode(cursor)
        if reference["p"] != project_id:
            raise TaskReferenceError("task cursor project is invalid")
        index = next((
            index for index, candidate in enumerate(candidates)
            if candidate.get("task_ref") == cursor
        ), None)
        if index is None:
            raise TaskReferenceError("task cursor is unavailable")
        item = _materialize_task_candidate(candidates[index], materialize_transform)
        if item is None or not _task_reference_matches_filter(item, filter):
            raise TaskReferenceError("task cursor is unavailable")
        start = index + 1

    page: list[dict[str, object]] = []
    for candidate in candidates[start:]:
        if _candidate_cannot_match_filter(candidate, filter):
            continue
        item = _materialize_task_candidate(candidate, materialize_transform)
        if item is None or not _task_reference_matches_filter(item, filter):
            continue
        if len(page) == limit:
            return page, True
        page.append(item)
    return page, False


def _page_task_candidates_with_transform_batches(
    items: Sequence[dict[str, object]], *, project_id: str, cursor: str | None,
    filter: str | None, limit: int,
    job_page: Callable[[tuple[str, str] | None, int], Sequence[Mapping[str, object]]],
    job_reader: Callable[[str], Mapping[str, object] | None] | None,
    materialize_transform: Callable[[Mapping[str, object]], dict[str, object] | None],
) -> dict[str, object]:
    """Merge bounded SQL transform pages with the existing task families."""
    if filter not in {None, "attention", "active", "delivered"}:
        raise TaskReferenceError("task filter is invalid")
    threshold: tuple[str, str] | None = None
    if cursor is not None:
        reference = _decode(cursor)
        if reference["p"] != project_id:
            raise TaskReferenceError("task cursor project is invalid")
        base = next((item for item in items if item["task_ref"] == cursor), None)
        if base is not None:
            if not _task_reference_matches_filter(base, filter):
                raise TaskReferenceError("task cursor is unavailable")
            threshold = (_task_timestamp_cursor(_text(base.get("updated_at"))), cursor)
        elif reference["k"] == _WORKBENCH_CONTENT_TRANSFORM_KIND:
            job = job_reader(reference["i"]) if job_reader is not None else None
            candidates = _workbench_transform_candidates((job,) if isinstance(job, Mapping) else (), project_id=project_id)
            if not candidates:
                raise TaskReferenceError("task cursor is unavailable")
            item = materialize_transform(candidates[0])
            if item is None or not _task_reference_matches_filter(item, filter):
                raise TaskReferenceError("task cursor is unavailable")
            threshold = (_task_timestamp_cursor(_text(job.get("updated_at"))), cursor)
        else:
            raise TaskReferenceError("task cursor is unavailable")

    base_index = 0
    if threshold is not None:
        while base_index < len(items) and _task_reference_sort_key(items[base_index]) >= (
            _timestamp_sort_key(threshold[0]), threshold[1],
        ):
            base_index += 1

    batch_after = threshold
    batch: list[dict[str, object]] = []
    batch_index = 0

    def next_transform() -> dict[str, object] | None:
        nonlocal batch_after, batch, batch_index
        while True:
            if batch_index >= len(batch):
                jobs = job_page(batch_after, 32)
                if not jobs:
                    return None
                batch = _workbench_transform_candidates(jobs, project_id=project_id)
                batch_index = 0
                last = jobs[-1]
                last_id = _text(last.get("id"))
                batch_after = (_task_timestamp_cursor(_text(last.get("updated_at"))), task_ref_for_workbench_content_transform(project_id=project_id, job_id=last_id))
                if not batch:
                    continue
            candidate = batch[batch_index]
            batch_index += 1
            return candidate

    transform = next_transform()
    page: list[dict[str, object]] = []
    while True:
        base = items[base_index] if base_index < len(items) else None
        if base is None and transform is None:
            return {"items": page, "next_cursor": None}
        if base is None or (transform is not None and _task_reference_sort_key(transform) > _task_reference_sort_key(base)):
            candidate, is_transform = transform, True
            transform = next_transform()
        else:
            candidate, is_transform = base, False
            base_index += 1
        if candidate is None or _candidate_cannot_match_filter(candidate, filter):
            continue
        item = materialize_transform(candidate) if is_transform else dict(candidate)
        if item is None or not _task_reference_matches_filter(item, filter):
            continue
        if len(page) == limit:
            return {"items": page, "next_cursor": page[-1]["task_ref"]}
        page.append(item)


def _materialize_task_candidate(
    candidate: Mapping[str, object], materialize_transform: Callable[[Mapping[str, object]], dict[str, object] | None],
) -> dict[str, object] | None:
    if candidate.get("_task_candidate_kind") == _WORKBENCH_CONTENT_TRANSFORM_KIND:
        return materialize_transform(candidate)
    return dict(candidate)


def _candidate_cannot_match_filter(candidate: Mapping[str, object], filter: str | None) -> bool:
    """Skip only states whose mismatch is known without owner read-back."""
    if candidate.get("_task_candidate_kind") != _WORKBENCH_CONTENT_TRANSFORM_KIND:
        return False
    job = candidate.get("_task_candidate_job")
    if not isinstance(job, Mapping):
        return False
    # Source and artifact read-back cannot promote a Job into an incompatible
    # lifecycle state. Completed jobs still require full delivery verification.
    status = _text(job.get("status"))
    if filter == "active":
        return status not in {"pending", "running"}
    if filter == "delivered":
        return status != "completed"
    return False


def _task_reference_matches_filter(item: Mapping[str, object], filter: str | None) -> bool:
    if filter is None:
        return True
    if filter == "attention":
        return item.get("status") != "empty" and item.get("attention") is not None
    if filter == "active":
        return item.get("status") in {"accepted", "active"}
    return item.get("status") == "delivered"


def task_reference_detail(
    *,
    repository: BilibiliFavoriteBatchRepository,
    task_ref: str,
    project_id: str,
    project_batch_projection: Callable[[Mapping[str, object]], Mapping[str, object]],
    object_store: TaskReferenceObjectStore | None = None,
    world_action_overview: Callable[[str], Mapping[str, object]] | None = None,
    world_action_status: Callable[[str, str], Mapping[str, object]] | None = None,
    workbench_transform_jobs: Callable[[], Sequence[Mapping[str, object]]] | None = None,
    workbench_transform_job_reader: Callable[[str], Mapping[str, object] | None] | None = None,
    workbench_transform_receipt: Callable[[str], Mapping[str, object] | None] | None = None,
    document_reader: Callable[[str], Mapping[str, object] | None] | None = None,
    document_revision_reader: Callable[[str, int], Mapping[str, object] | None] | None = None,
    document_markdown_reader: Callable[[str, int], str | None] | None = None,
) -> dict[str, object] | None:
    _project(project_id)
    reference = _decode(task_ref)
    if reference["p"] != project_id:
        raise TaskReferenceError("task reference project is invalid")
    if reference["k"] == _WORLD_ACTION_KIND:
        return _world_action_detail(
            reference=reference,
            project_id=project_id,
            overview=world_action_overview,
            action_status=world_action_status,
        )
    if reference["k"] == _MEDIA_PROCESSING_JOB_KIND:
        return _media_processing_job_detail(object_store, reference=reference, project_id=project_id)
    if reference["k"] == _WORKBENCH_CONTENT_TRANSFORM_KIND:
        return _workbench_transform_detail(
            reference=reference, project_id=project_id, object_store=object_store,
            jobs=workbench_transform_jobs, job_reader=workbench_transform_job_reader, receipt_reader=workbench_transform_receipt,
            document_reader=document_reader, revision_reader=document_revision_reader,
            markdown_reader=document_markdown_reader,
        )
    if reference["k"] != _FAVORITE_BATCH_KIND:
        raise TaskReferenceError("task reference is invalid")
    batch = repository.get(reference["i"])
    if batch is None:
        return None
    if batch.payload.get("project_id") != project_id:
        # A stale or forged reference must not disclose whether another
        # project owns this durable identifier.
        raise TaskReferenceError("task reference project is invalid")
    projection = project_batch_projection(batch.payload)
    summary = _favorite_batch_summary(batch.payload, projection)
    return {
        **summary,
        "detail": {
            "owner_kind": _FAVORITE_BATCH_KIND,
            "owner_revision": projection.get("revision"),
            "admission_status": projection.get("admission_status"),
            "processing_status": projection.get("processing_status"),
            "counts": projection.get("counts"),
            # These are existing owner projections. No URL is fabricated for
            # an object that cannot be opened through a published reader.
            "outputs": _outputs(projection),
            "children": _children(projection),
            "allowed_actions": summary["allowed_actions"],
        },
    }


def _favorite_batch_summary(owner: Mapping[str, object], projection: Mapping[str, object]) -> dict[str, object]:
    project_id, batch_id = _text(owner.get("project_id")), _text(owner.get("batch_id"))
    if not project_id or not batch_id:
        raise TaskReferenceError("task owner is invalid")
    status, attention = _status(projection)
    return {
        "task_ref": task_ref_for_favorite_batch(project_id=project_id, batch_id=batch_id),
        "project_id": project_id,
        "title": "Bilibili 收藏夹导入",
        "status": status,
        "attention": attention,
        "next_action": _next_action(projection),
        "updated_at": _text(projection.get("updated_at")) or _text(owner.get("updated_at")),
        "delivered_at": _text(projection.get("updated_at")) if status == "delivered" else None,
        "allowed_actions": _allowed_actions(projection),
    }


def _media_processing_job_summaries(
    object_store: TaskReferenceObjectStore,
    *,
    project_id: str,
) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for job in object_store.list("media_processing_jobs"):
        owner = _media_processing_job_owner(object_store, job, project_id=project_id)
        if owner is not None:
            summaries.append(_media_processing_job_summary(owner, object_store))
    return sorted(summaries, key=lambda item: str(item["task_ref"]))


def _world_action_summaries(
    *,
    project_id: str,
    overview: Mapping[str, object],
    action_status: Callable[[str, str], Mapping[str, object]],
) -> list[dict[str, object]]:
    actions = _world_action_owners(project_id=project_id, overview=overview)
    summaries: list[dict[str, object]] = []
    for owner in actions:
        status = _verified_world_action_status(
            action_status, project_id=project_id, action_id=_text(owner.get("action_id")),
        )
        if status is not None:
            summaries.append(_world_action_summary(owner, status, project_id=project_id))
    return sorted(summaries, key=lambda item: str(item["task_ref"]))


def _world_action_detail(
    *,
    reference: Mapping[str, str],
    project_id: str,
    overview: Callable[[str], Mapping[str, object]] | None,
    action_status: Callable[[str, str], Mapping[str, object]] | None,
) -> dict[str, object] | None:
    if overview is None or action_status is None:
        return None
    owners = _world_action_owners(project_id=project_id, overview=overview(project_id=project_id))
    owner = next((item for item in owners if _text(item.get("action_id")) == reference["i"]), None)
    if owner is None:
        return None
    status = _verified_world_action_status(action_status, project_id=project_id, action_id=reference["i"])
    if status is None:
        return None
    summary = _world_action_summary(owner, status, project_id=project_id)
    return {
        **summary,
        "detail": {
            "owner_kind": _WORLD_ACTION_KIND,
            "processing_status": _text(status.get("status")) or "unknown",
            # A World Turn's answer, context and raw outcome refs are private.
            # No published artifact reader exists for this owner yet.
            "outputs": [],
            "children": [],
            "allowed_actions": [],
        },
    }


def _world_action_owners(*, project_id: str, overview: Mapping[str, object]) -> list[Mapping[str, object]]:
    state = overview.get("state")
    if not isinstance(state, Mapping) or _text(state.get("project_id")) != project_id:
        raise TaskReferenceError("world action project is invalid")
    values = state.get("planned_actions")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise TaskReferenceError("world action owner is invalid")
    owners: list[Mapping[str, object]] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        action_id = _text(item.get("action_id"))
        if _turn_id_for_world_action(action_id) is not None:
            owners.append(item)
    return owners


def _verified_world_action_status(
    action_status: Callable[[str, str], Mapping[str, object]],
    *,
    project_id: str,
    action_id: str,
) -> Mapping[str, object] | None:
    turn_id = _turn_id_for_world_action(action_id)
    if turn_id is None:
        raise TaskReferenceError("world action owner is invalid")
    status = action_status(project_id=project_id, turn_id=turn_id)
    if (
        not isinstance(status, Mapping)
        or _text(status.get("action_id")) != action_id
        or _text(status.get("turn_id")) != turn_id
    ):
        raise TaskReferenceError("world action verification is invalid")
    # Without an admitted Turn there is no session or receipt to read back.
    # Do not turn a planned-but-unbound action into a task owner.
    if _text(status.get("status")) == "not_admitted":
        return None
    return status


def _world_action_summary(
    owner: Mapping[str, object],
    status: Mapping[str, object],
    *,
    project_id: str,
) -> dict[str, object]:
    action_id = _text(owner.get("action_id"))
    if not action_id:
        raise TaskReferenceError("world action owner is invalid")
    # action_status verifies project/session/receipt/plan, but deliberately
    # does not disclose project_id. The caller-scoped World overview owns it.
    return _world_action_summary_for_project(owner, status, project_id)


def _world_action_summary_for_project(
    owner: Mapping[str, object],
    status: Mapping[str, object],
    project_id: str,
) -> dict[str, object]:
    action_id = _text(owner.get("action_id"))
    state, attention = _world_action_status(status)
    return {
        "task_ref": task_ref_for_world_action(project_id=project_id, action_id=action_id),
        "project_id": project_id,
        "title": _text(owner.get("title")) or "项目行动",
        "status": state,
        "attention": attention,
        "next_action": None,
        "updated_at": None,
        "delivered_at": None,
        "allowed_actions": [],
    }


def _world_action_status(status: Mapping[str, object]) -> tuple[str, dict[str, str] | None]:
    value = _text(status.get("status"))
    if value in {"accepted", "queued", "running"}:
        return "active", None
    if value == "waiting_approval":
        return "attention", {"kind": "waiting_approval", "label": "等待你的审批"}
    if value == "ready_for_feedback" or status.get("ready_for_feedback") is True:
        return "attention", {"kind": "ready_for_feedback", "label": "等待你的反馈"}
    if value in {"failed", "cancelled"}:
        return "attention", {"kind": f"world_action_{value}", "label": "项目行动需要处理"}
    if value == "completed":
        return "completed", {"kind": "outputs_unavailable", "label": "处理已结束，结果仍需核验"}
    return "unknown", {"kind": "verification_required", "label": "结果需要核验"}


def _turn_id_for_world_action(action_id: str) -> str | None:
    prefix = "world-action-"
    suffix = action_id.removeprefix(prefix)
    if not suffix or suffix == action_id:
        return None
    return f"world-turn-{suffix}"


def _workbench_transform_candidates(
    jobs: Sequence[Mapping[str, object]], *, project_id: str,
) -> list[dict[str, object]]:
    """Return sortable transform handles without reading Sources or outputs."""
    candidates: list[dict[str, object]] = []
    for job in jobs:
        job_id = _text(job.get("id"))
        raw_items = job.get("transform_items")
        if (
            not job_id
            or _text(job.get("job_type")) != _WORKBENCH_CONTENT_TRANSFORM_KIND
            or _text(job.get("execution_version")) != "effect-v2"
            or not isinstance(raw_items, Sequence)
            or isinstance(raw_items, (str, bytes))
            or not raw_items
        ):
            continue
        # This internal value is sort/cursor scaffolding only.  The Source
        # remains the project authority and must validate before exposure.
        candidates.append({
            "_task_candidate_kind": _WORKBENCH_CONTENT_TRANSFORM_KIND,
            "_task_candidate_job": job,
            "task_ref": task_ref_for_workbench_content_transform(project_id=project_id, job_id=job_id),
            "updated_at": _text(job.get("updated_at")),
        })
    return candidates


def _materialize_workbench_transform_candidate(
    candidate: Mapping[str, object], *, object_store: TaskReferenceObjectStore | None,
    receipt_reader: Callable[[str], Mapping[str, object] | None] | None,
    document_reader: Callable[[str], Mapping[str, object] | None] | None,
    revision_reader: Callable[[str, int], Mapping[str, object] | None] | None,
    markdown_reader: Callable[[str, int], str | None] | None,
) -> dict[str, object] | None:
    if (
        object_store is None or receipt_reader is None or document_reader is None
        or revision_reader is None or markdown_reader is None
    ):
        return None
    job = candidate.get("_task_candidate_job")
    project_id = _decode(_text(candidate.get("task_ref"))).get("p")
    if not isinstance(job, Mapping) or not project_id:
        return None
    owner = _workbench_transform_owner(object_store, job, project_id=project_id)
    if owner is None:
        return None
    return _workbench_transform_summary(
        owner, receipt_reader, document_reader, revision_reader, markdown_reader,
    )


def _workbench_transform_detail(
    *, reference: Mapping[str, str], project_id: str, object_store: TaskReferenceObjectStore | None,
    jobs: Callable[[], Sequence[Mapping[str, object]]] | None,
    job_reader: Callable[[str], Mapping[str, object] | None] | None,
    receipt_reader: Callable[[str], Mapping[str, object] | None] | None,
    document_reader: Callable[[str], Mapping[str, object] | None] | None,
    revision_reader: Callable[[str, int], Mapping[str, object] | None] | None,
    markdown_reader: Callable[[str, int], str | None] | None,
) -> dict[str, object] | None:
    if object_store is None or (jobs is None and job_reader is None) or receipt_reader is None or document_reader is None or revision_reader is None or markdown_reader is None:
        return None
    job = job_reader(reference["i"]) if job_reader is not None else next((item for item in jobs() if _text(item.get("id")) == reference["i"]), None)
    if job is None:
        return None
    owner = _workbench_transform_owner(object_store, job, project_id=project_id)
    if owner is None:
        raise TaskReferenceError("task reference project is invalid")
    summary = _workbench_transform_summary(owner, receipt_reader, document_reader, revision_reader, markdown_reader)
    return {
        **summary,
        "detail": {
            "owner_kind": _WORKBENCH_CONTENT_TRANSFORM_KIND,
            "owner_revision": owner["job"].get("revision"),
            "processing_status": _text(owner["job"].get("status")) or "unknown",
            "source_ids": [_text(item.get("source_id")) for item in owner["items"]],
            "outputs": _workbench_transform_outputs(
                owner, receipt_reader, document_reader, revision_reader, markdown_reader,
            ),
            "children": [], "allowed_actions": [],
        },
    }


def _workbench_transform_owner(
    object_store: TaskReferenceObjectStore, job: Mapping[str, object], *, project_id: str,
) -> dict[str, object] | None:
    job_id = _text(job.get("id"))
    if (
        not job_id or _text(job.get("job_type")) != _WORKBENCH_CONTENT_TRANSFORM_KIND
        or _text(job.get("execution_version")) != "effect-v2"
    ):
        return None
    raw_items = job.get("transform_items")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)) or not raw_items:
        return None
    items: list[Mapping[str, object]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            return None
        source_id = _text(item.get("source_id"))
        source = object_store.read("sources", source_id) if source_id else None
        # Never trust the Job-level project hint: Source owns the user scope.
        if (
            not isinstance(source, Mapping) or _text(source.get("id")) != source_id
            or _text(source.get("project_id")) != project_id
        ):
            return None
        items.append(item)
    return {"job": job, "items": tuple(items), "project_id": project_id}


def _workbench_transform_summary(
    owner: Mapping[str, object], receipt_reader: Callable[[str], Mapping[str, object] | None],
    document_reader: Callable[[str], Mapping[str, object] | None],
    revision_reader: Callable[[str, int], Mapping[str, object] | None],
    markdown_reader: Callable[[str, int], str | None],
) -> dict[str, object]:
    job = owner["job"]
    if not isinstance(job, Mapping):
        raise TaskReferenceError("workbench transform owner is invalid")
    job_id, project_id = _text(job.get("id")), _text(owner.get("project_id"))
    # Active and terminal-failure Jobs have no result that could become a
    # delivered artifact.  Avoid opening the receipt/document chain merely to
    # render their task row; completed Jobs remain strict read-back verified.
    outputs = (
        _workbench_transform_outputs(owner, receipt_reader, document_reader, revision_reader, markdown_reader)
        if _text(job.get("status")) == "completed" else []
    )
    status, attention = _workbench_transform_status(job, outputs, owner["items"])
    return {
        "task_ref": task_ref_for_workbench_content_transform(project_id=project_id, job_id=job_id),
        "project_id": project_id, "kind": _WORKBENCH_CONTENT_TRANSFORM_KIND,
        "title": "资料内容整理", "status": status, "attention": attention,
        "next_action": None, "updated_at": _text(job.get("updated_at")),
        "delivered_at": _text(job.get("updated_at")) if status == "delivered" else None,
        "allowed_actions": [],
    }


def _workbench_transform_status(
    job: Mapping[str, object], outputs: Sequence[Mapping[str, object]], items: object,
) -> tuple[str, dict[str, str] | None]:
    status = _text(job.get("status"))
    item_count = len(items) if isinstance(items, Sequence) else 0
    if status in {"pending", "running"}:
        return "active", None
    if status == "completed":
        if item_count and len(outputs) == item_count:
            return "delivered", None
        return "completed", {"kind": "outputs_unavailable", "label": "整理已结束，结果仍需核验"}
    if status in {"failed", "cancelled", "waiting_user"}:
        return "attention", {"kind": f"workbench_transform_{status}", "label": "资料整理需要处理"}
    return "unknown", {"kind": "verification_required", "label": "结果需要核验"}


def _workbench_transform_outputs(
    owner: Mapping[str, object], receipt_reader: Callable[[str], Mapping[str, object] | None],
    document_reader: Callable[[str], Mapping[str, object] | None],
    revision_reader: Callable[[str, int], Mapping[str, object] | None],
    markdown_reader: Callable[[str, int], str | None],
) -> list[dict[str, object]]:
    job, project_id, items = owner.get("job"), _text(owner.get("project_id")), owner.get("items")
    if not isinstance(job, Mapping) or not isinstance(items, Sequence):
        return []
    receipt = receipt_reader(_text(job.get("id")))
    raw_outputs = receipt.get("outputs") if isinstance(receipt, Mapping) else None
    if not isinstance(raw_outputs, Sequence) or isinstance(raw_outputs, (str, bytes)) or len(raw_outputs) != len(items):
        return []
    published = {
        _text(item.get("object_id")) for item in job.get("published_outputs", ())
        if isinstance(item, Mapping) and _text(item.get("kind")) == "document"
        and _text(item.get("status")) == "published"
    }
    outputs: list[dict[str, object]] = []
    for source_item, output in zip(items, raw_outputs):
        if not isinstance(source_item, Mapping) or not isinstance(output, Mapping):
            return []
        source_id, document_id = _text(source_item.get("source_id")), _text(output.get("document_id"))
        revision = output.get("document_revision")
        current = document_reader(document_id) if document_id else None
        published_document = revision_reader(document_id, revision) if document_id and isinstance(revision, int) else None
        current_revision = current.get("revision") if isinstance(current, Mapping) else None
        current_refs = current.get("source_refs") if isinstance(current, Mapping) else None
        published_snapshot = published_document.get("source_snapshot") if isinstance(published_document, Mapping) else None
        published_refs = published_snapshot.get("source_refs") if isinstance(published_snapshot, Mapping) else None
        if (
            not source_id or _text(output.get("source_id")) != source_id or document_id not in published
            or not isinstance(current, Mapping) or _text(current.get("id")) != document_id
            or _text(current.get("project_id")) != project_id or not isinstance(current_refs, Sequence)
            or not any(isinstance(ref, Mapping) and _text(ref.get("source_id")) == source_id for ref in current_refs)
            or not isinstance(published_document, Mapping) or _text(published_document.get("document_id")) != document_id
            or published_document.get("revision") != revision
            or not isinstance(published_refs, Sequence)
            or not any(isinstance(ref, Mapping) and _text(ref.get("source_id")) == source_id for ref in published_refs)
            or not isinstance(current_revision, int) or not _readable_document(markdown_reader, document_id, current_revision)
            or not isinstance(revision, int) or not _readable_document(markdown_reader, document_id, revision)
        ):
            return []
        outputs.append({"artifact_id": document_id, "kind": "document", "title": "已整理文档",
                        "status": "completed", "href": _library_document_href(document_id)})
    return outputs


def _readable_document(markdown_reader: Callable[[str, int], str | None], document_id: str, revision: int) -> bool:
    try:
        value = markdown_reader(document_id, revision)
    except (OSError, ValueError):
        return False
    return isinstance(value, str) and bool(value.strip())


def _media_processing_job_detail(
    object_store: TaskReferenceObjectStore | None,
    *,
    reference: Mapping[str, str],
    project_id: str,
) -> dict[str, object] | None:
    if object_store is None:
        return None
    job = object_store.read("media_processing_jobs", reference["i"])
    if job is None:
        return None
    owner = _media_processing_job_owner(object_store, job, project_id=project_id)
    if owner is None:
        # Do not disclose whether a stale task reference belongs to a Source in
        # another project, lacks a Source, or is malformed in storage.
        raise TaskReferenceError("task reference project is invalid")
    summary = _media_processing_job_summary(owner, object_store)
    return {
        **summary,
        "detail": {
            "owner_kind": _MEDIA_PROCESSING_JOB_KIND,
            "owner_revision": owner["job"].get("revision"),
            "processing_status": _text(owner["job"].get("status")) or "unknown",
            "outputs": _media_processing_outputs(object_store, owner),
            "children": [],
            "allowed_actions": [],
        },
    }


def _media_processing_job_owner(
    object_store: TaskReferenceObjectStore,
    job: Mapping[str, object],
    *,
    project_id: str,
) -> dict[str, Mapping[str, object]] | None:
    job_id = _text(job.get("id"))
    source_id = _text(job.get("source_id"))
    if not job_id or not source_id:
        return None
    source = object_store.read("sources", source_id)
    if not isinstance(source, Mapping):
        return None
    # Project membership is deliberately Source-owned. Do not accept a media
    # job's project hint, Source metadata, or an implicit default project.
    if _text(source.get("id")) != source_id or _text(source.get("project_id")) != project_id:
        return None
    return {"job": job, "source": source}


def _media_processing_job_summary(
    owner: Mapping[str, Mapping[str, object]],
    object_store: TaskReferenceObjectStore,
) -> dict[str, object]:
    job, source = owner["job"], owner["source"]
    job_id = _text(job.get("id"))
    source_id = _text(source.get("id"))
    project_id = _text(source.get("project_id"))
    status, attention = _media_processing_status(job, _media_processing_outputs(object_store, owner))
    source_type = _text(job.get("source_type")) or _text(source.get("type"))
    return {
        "task_ref": task_ref_for_media_processing_job(project_id=project_id, job_id=job_id),
        "project_id": project_id,
        "title": _media_processing_title(source_type),
        "status": status,
        "attention": attention,
        "next_action": None,
        "updated_at": _text(job.get("updated_at")) or _text(source.get("updated_at")),
        "delivered_at": _text(job.get("updated_at")) if status == "delivered" else None,
        "allowed_actions": [],
        # This is an existing renderer deep link to the Source. It does not
        # manufacture an output-specific reader that the library does not own.
        "library_href": _library_source_href(source_id),
    }


def _media_processing_status(
    job: Mapping[str, object],
    outputs: Sequence[Mapping[str, object]],
) -> tuple[str, dict[str, str] | None]:
    status = _text(job.get("status"))
    if status in {"queued", "running"}:
        return "active", None
    if status == "completed":
        if outputs:
            return "delivered", None
        return "completed", {"kind": "outputs_unavailable", "label": "处理已结束，结果仍需核验"}
    if status == "failed":
        return "attention", {"kind": "media_processing_failed", "label": "媒体处理失败，需要处理"}
    if status == "skipped":
        return "attention", {"kind": "media_processing_skipped", "label": "媒体处理已跳过，需要处理"}
    return "unknown", {"kind": "verification_required", "label": "结果需要核验"}


def _media_processing_outputs(
    object_store: TaskReferenceObjectStore,
    owner: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    job, source = owner["job"], owner["source"]
    job_id = _text(job.get("id"))
    source_id = _text(source.get("id"))
    outputs: list[dict[str, object]] = []
    for output in object_store.list("media_processing_outputs"):
        output_id, output_kind = _text(output.get("id")), _text(output.get("output_kind"))
        if (
            not output_id
            or not output_kind
            or _text(output.get("job_id")) != job_id
            or _text(output.get("source_id")) != source_id
            or _text(output.get("status")) != "completed"
        ):
            continue
        outputs.append({
            "artifact_id": output_id,
            "kind": output_kind,
            "title": _media_output_title(output_kind),
            "status": "completed",
            "href": _library_source_href(source_id),
        })
    return sorted(outputs, key=lambda item: str(item["artifact_id"]))


def _media_processing_title(source_type: str) -> str:
    return {
        "image": "图片文字提取",
        "audio": "音频转写",
        "video": "视频处理",
    }.get(source_type, "媒体处理")


def _media_output_title(output_kind: str) -> str:
    return {
        "ocr_text": "识别文本",
        "transcript": "转写文本",
        "frame_index": "关键帧索引",
    }.get(output_kind, "媒体处理结果")


def _library_source_href(source_id: str) -> str:
    return f"#view=rebuild-library-overview&source_id={quote(source_id, safe='')}"


def _library_document_href(document_id: str) -> str:
    return f"#view=rebuild-library-overview&document_id={quote(document_id, safe='')}"


def _status(projection: Mapping[str, object]) -> tuple[str, dict[str, str] | None]:
    processing = _text(projection.get("processing_status"))
    admission = _text(projection.get("admission_status"))
    if processing == "empty" or projection.get("total") == 0:
        return "empty", {"kind": "empty_batch", "label": "收藏夹为空"}
    if processing == "waiting_user" or any(
        item["status"] == "waiting_user" for item in _children(projection)
    ):
        return "attention", {"kind": "waiting_user", "label": "等待你的处理"}
    if processing == "unknown" or admission == "unknown":
        return "unknown", {"kind": "verification_required", "label": "结果需要核验"}
    if processing == "partial_failure" or admission == "partial_failure":
        return "attention", {"kind": "failed_items", "label": "有失败项需要处理"}
    if admission == "awaiting_continue":
        return "attention", {"kind": "continue_admission", "label": "等待继续登记"}
    if processing in {"running", "queued"} or admission in {"queued", "running"}:
        return "active", None
    if processing == "complete":
        children = _children(projection)
        if children and all(item["openable"] for item in children):
            return "delivered", None
        return "completed", {"kind": "outputs_unavailable", "label": "处理已结束，结果仍需核验"}
    return "accepted", None


def _allowed_actions(projection: Mapping[str, object]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if _text(projection.get("admission_status")) == "awaiting_continue":
        result.append({"kind": "continue", "label": "继续登记"})
    if projection.get("has_failed") is True or _text(projection.get("processing_status")) == "partial_failure":
        result.append({"kind": "retry_failed", "label": "重试失败项"})
    return result


def _next_action(projection: Mapping[str, object]) -> dict[str, str] | None:
    actions = _allowed_actions(projection)
    return None if not actions else {"label": actions[0]["label"]}


def _children(projection: Mapping[str, object]) -> list[dict[str, object]]:
    values = projection.get("child_outputs")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    result = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        job_id = _text(item.get("job_id"))
        if not job_id:
            continue
        result.append({
            "job_id": job_id,
            "status": _text(item.get("status")) or "unknown",
            "openable": item.get("openable") is True,
        })
    return result


def _outputs(projection: Mapping[str, object]) -> list[dict[str, object]]:
    result = []
    values = projection.get("child_outputs")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return result
    for child in values:
        if not isinstance(child, Mapping):
            continue
        outputs = child.get("outputs")
        if not isinstance(outputs, Sequence) or isinstance(outputs, (str, bytes)):
            continue
        for output in outputs:
            if not isinstance(output, Mapping) or output.get("read_back") is not True:
                continue
            object_id, kind = _text(output.get("object_id")), _text(output.get("kind"))
            if object_id and kind:
                result.append({"artifact_id": object_id, "kind": kind, "status": _text(output.get("status")) or "unknown"})
    return result


def _encode(value: Mapping[str, str]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "tr1_" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> dict[str, str]:
    if not isinstance(value, str) or not value.startswith("tr1_") or len(value) > 512:
        raise TaskReferenceError("task reference is invalid")
    try:
        encoded = value[4:] + "=" * (-len(value[4:]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise TaskReferenceError("task reference is invalid") from error
    if not isinstance(payload, Mapping) or set(payload) != {"v", "k", "p", "i"}:
        raise TaskReferenceError("task reference is invalid")
    if payload.get("v") != _VERSION or payload.get("k") not in {
        _FAVORITE_BATCH_KIND,
        _MEDIA_PROCESSING_JOB_KIND,
        _WORLD_ACTION_KIND,
        _WORKBENCH_CONTENT_TRANSFORM_KIND,
    }:
        raise TaskReferenceError("task reference is invalid")
    result = {key: _text(payload.get(key)) for key in ("v", "k", "p", "i")}
    if not result["p"] or not result["i"]:
        raise TaskReferenceError("task reference is invalid")
    return result


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _project(value: object) -> str:
    result = _text(value)
    if _PROJECT_ID.fullmatch(result) is None:
        raise TaskReferenceError("task project is invalid")
    return result
