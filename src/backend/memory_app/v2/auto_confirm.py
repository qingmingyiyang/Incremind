"""Admit completed drafts through the existing frozen confirmation workflow."""
from __future__ import annotations

import logging

from fastapi import HTTPException

from ..workspace_contracts import _project
from .projects import assign_scene, scene_of


_LOGGER = logging.getLogger(__name__)


def _failure(stage, exc):
    # Provider errors can contain source text or credentials. Never log messages.
    _LOGGER.warning("v2_auto_confirm_failed stage=%s exception_type=%s",
                    stage, type(exc).__name__)


def _inherit_scene(domains, row, project_id):
    document_id = row.payload.get("document_id")
    if not document_id:
        return
    records = domains.items.records
    scene = scene_of(records, "item", row.object_id)
    if scene is None or scene.get("project_id") != project_id:
        return
    document = domains.review.documents.read(document_id)
    if document is None or document.get("project_id") != project_id:
        return
    # Any existing assignment may be a later user choice. Reentry only repairs
    # an absent assignment, avoiding both overrides and extra sidecar revisions.
    assign_scene(records, "document", document_id, project_id, scene["scene"], if_absent=True)


async def process_and_confirm(domains, item_id, project_id, *, on_error=None) -> dict:
    """Process once, confirm once, and recover interrupted admission on reentry."""
    project_id = _project(project_id)
    row = domains.items.item_for(item_id, project_id)
    newly_admitted = row.payload.get('status') != 'confirmed'
    if row.payload.get("status") in {"staged", "failed"}:
        try:
            await domains.intake.process(item_id, {"project_id": project_id})
        except HTTPException as exc:
            # A concurrent processor may have claimed the item after our read.
            # Authorization and other processing errors retain domain semantics.
            if exc.status_code != 409 or exc.detail not in (
                "invalid_item_state", "record_revision_conflict", "processing_run_changed",
            ):
                raise
        row = domains.items.item_for(item_id, project_id)
    if row.payload.get("status") in {"ready", "confirming"}:
        revision = (row.payload.get("reviewed_revision")
                    if row.payload["status"] == "confirming" else row.revision)
        try:
            await domains.review.confirm(item_id, {
                "project_id": project_id, "expected_revision": revision,
            })
        except Exception as exc:
            _failure("confirm", exc)
            if on_error is not None:
                on_error("confirmation_failed")
        row = domains.items.item_for(item_id, project_id)
    if row.payload.get("status") == "confirmed":
        from .stats import record_activity
        if newly_admitted:
            record_activity(domains.items.records, 'remember', project_id, row.object_id,
                            event_id='remember-' + row.object_id)
        try:
            _inherit_scene(domains, row, project_id)
        except Exception as exc:
            _failure("scene", exc)
            if on_error is not None:
                on_error("scene_assignment_failed")
    return domains.items.public_row(row)
