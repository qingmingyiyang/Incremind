"""Read-only tray projection of existing intake and task domain records."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from backend.recognition import RecognitionError, WorkScope
from core.document_engine import SQLiteDocumentRepository
from ..task_status import get_task_status
from ..workspace_contracts import _project
from .do import task_receipt


_INTAKE_ERRORS = frozenset({
    "processing_failed", "source_text_empty", "source_text_too_large", "asr_unavailable",
    "audio_transcription_failed", "remote_processing_target_changed", "audio_transcription_evidence_invalid",
    "model_output_incomplete", "model_output_missing_content", "model_response_invalid",
    "model_not_configured", "model_request_failed", "unsupported_media_url", "invalid_source",
    "media_short_link_unavailable", "media_short_link_target_invalid", "media_short_link_redirect_limit",
    "media_short_link_address_blocked", "bilibili_metadata_unavailable", "bilibili_video_unavailable",
    "bilibili_asr_unavailable", "bilibili_video_transcription_failed", "xiaohongshu_page_unavailable",
    "xiaohongshu_metadata_unavailable", "xiaohongshu_video_required", "xiaohongshu_video_unavailable",
    "xiaohongshu_asr_unavailable", "xiaohongshu_video_transcription_failed", "xiaohongshu_video_speech_unavailable",
})


def _now():
    return datetime.now(timezone.utc)


def _timestamp(payload):
    return (payload.get("updated_at") or payload.get("finished_at")
            or payload.get("confirmed_at") or payload.get("created_at"))


def _recent_completion(value, now):
    if not isinstance(value, str):
        return False
    try:
        finished = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if finished.tzinfo is None:
            return False
        return timedelta(0) <= now - finished < timedelta(hours=24)
    except ValueError:
        return False


def _turn_target(records, project, turn):
    """Only expose a receipt address whose thread belongs to the same scope."""
    thread_id = turn.payload.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        return None
    thread = records.read("v2_threads", thread_id)
    if thread is None or thread.payload.get("project_id") != project:
        return None
    return {"type": "turn", "id": turn.object_id, "thread_id": thread_id,
            "turn_id": turn.object_id}


def _document_target(records, project, identity, namespace):
    if not isinstance(identity, str) or not identity:
        return None
    document = SQLiteDocumentRepository(records, namespace_id=namespace).read(identity)
    if document is None or document.get("project_id") != project:
        return None
    return {"type": "document", "id": identity}


def _do_view(records, scope, turn, now, namespace):
    target = _turn_target(records, scope.project_id, turn)
    receipt = turn.payload.get("receipt", {}).get("do")
    if target is None or not isinstance(receipt, dict):
        return None
    task_id = receipt.get("task_id")
    finished_at = _timestamp(turn.payload)
    if task_id is not None:
        if not isinstance(task_id, str) or not task_id:
            return None
        try:
            status = get_task_status(records, scope, task_id, document_namespace=namespace)
        except RecognitionError:
            return None
        # Thread GET uses the same domain status. Project it without running
        # research, polling the workflow, or writing a refreshed receipt.
        receipt = task_receipt(status, receipt)
        if status.get("status") == "completed":
            finished_at = status.get("finished_at")
    internal = receipt.get("state")
    state = {"researching": "processing", "preparing": "processing", "running": "processing",
             "waiting_approval": "pending", "failed": "failed"}.get(internal)
    if internal == "done" and _recent_completion(finished_at, now):
        state = "done"
    if state is None:
        return None
    progress = receipt.get("progress")
    if not (isinstance(progress, dict) and set(progress) == {"done", "total"}
            and type(progress["done"]) is int and type(progress["total"]) is int
            and 0 <= progress["done"] <= progress["total"]):
        progress = None
    return {"id": turn.object_id, "kind": "task", "title": receipt.get("title", ""),
            "state": state, "progress": progress, "pending_count": int(state == "pending"),
            "error": "task_failed" if state == "failed" else None,
            "target": target, "updated_at": _timestamp(turn.payload)}


def _pending_index(service, scope):
    """Use domain-scoped candidates and their explicit document provenance."""
    experiences = {item.id: item for item in service.list_experiences(scope=scope)}
    by_document = {}
    candidates = [candidate for candidate in service.list_candidates(scope=scope)
                  if service.records.read('v2_candidate_fade', candidate.id) is None]
    for candidate in candidates:
        for identity in candidate.source_experience_ids:
            experience = experiences.get(identity)
            if experience is None:
                continue
            for source in experience.provenance.source_refs:
                if source.type == "document":
                    by_document.setdefault(source.id, set()).add(candidate.id)
    return by_document, {candidate.id for candidate in candidates}


def _intake_view(row, by_document, pending_ids, *, local_fallback=False):
    payload = row.payload
    status = payload.get("status")
    pending = set(by_document.get(payload.get("document_id"), ()))
    if payload.get("candidate_id") in pending_ids:
        pending.add(payload["candidate_id"])
    state = {"staged": "processing", "processing": "processing", "confirming": "processing", "ready": "failed",
             "failed": "failed"}.get(status)
    if status == "confirmed" and pending:
        state = "pending"
    elif status == "confirmed" and local_fallback and _recent_completion(_timestamp(payload), _now()):
        state = "done"
    if state is None:
        return None
    # Each level requires a persisted checkpoint, never elapsed-time estimates.
    done = 1
    if payload.get("source_text"):
        done = 2
    if payload.get("draft"):
        done = 3
    if status == "confirmed":
        done = 4
    error = payload.get("error") if state == "failed" else None
    if error is not None and (not isinstance(error, str) or error not in _INTAKE_ERRORS):
        error = "processing_failed"
    return {"id": row.object_id, "kind": "intake", "title": payload.get("title", row.object_id),
            "state": state, "progress": {"done": done, "total": 4},
            "pending_count": len(pending) if state == "pending" else 0,
            "error": error,
            "target": {"type": "item", "id": row.object_id}, "updated_at": _timestamp(payload)}


def _task_view(records, scope, row, now, document_namespace="recognition"):
    if (row.payload.get("state") == "completed"
            and not _recent_completion(row.payload.get("finished_at"), now)):
        return None
    try:
        status = get_task_status(records, scope, row.object_id, document_namespace=document_namespace)
    except RecognitionError:
        # Preserve the domain's cross-project and invalid-result visibility guard.
        return None
    internal = status["status"]
    state = {"queued": "processing", "running": "processing", "waiting_approval": "pending",
             "failed": "failed", "interrupted": "failed"}.get(internal)
    if internal == "completed" and _recent_completion(status.get("finished_at"), now):
        state = "done"
    if state is None:
        return None
    return {"id": row.object_id, "kind": "task", "title": status["title"], "state": state,
            "progress": None, "pending_count": 1 if state == "pending" else 0,
            "error": "task_" + internal if state == "failed" else None,
            "target": (_document_target(records, scope.project_id, status.get("document_id"), document_namespace)
                       or {"type": "task", "id": row.object_id}), "updated_at": _timestamp(row.payload)}


def install_job_routes(application, *, records, service, document_namespace="recognition"):
    router = APIRouter(prefix="/api/v2/jobs")

    @router.get("")
    def list_jobs(project_id: str = "default"):
        project_id = _project(project_id)
        scope = WorkScope("local-user", project_id)
        by_document, pending_ids = _pending_index(service, scope)
        items = []
        backup = getattr(application.state, 'memory_backup', None)
        if backup is not None:
            for identity, failure in backup.failures().items():
                items.append({'id': identity, 'kind': 'task', 'title': '备份失败', 'state': 'failed',
                    'progress': None, 'pending_count': 0, 'error': 'backup_failed',
                    'target': {'type': 'backup', 'id': identity}, 'updated_at': failure.get('updated_at')})
        turns = records.list_matching("v2_turns", project_id=project_id)
        for row in records.list_matching("workspace_items", project_id=project_id):
            from .image_read import effective_image_read
            image = effective_image_read(records, row) if row.payload.get('input_kind') == 'image' else None
            local_fallback = image is not None and image.get('local_fallback') is True
            view = _intake_view(row, by_document, pending_ids, local_fallback=local_fallback)
            targets = [turn for turn in turns if turn.payload.get("intent") == "remember"
                       and turn.payload.get("item_id") == row.object_id
                       and turn.payload.get("receipt", {}).get("remember", {}).get("item_id") == row.object_id
                       and _turn_target(records, project_id, turn) is not None]
            target = (_turn_target(records, project_id, max(targets, key=lambda turn: (
                turn.payload.get("created_at", ""), turn.object_id))) if targets else
                _document_target(records, project_id, row.payload.get("document_id"), document_namespace))
            states = []
            for state_row in records.list_matching("v2_workbench_item_states", project_id=project_id, item_id=row.object_id):
                state = state_row.payload
                turn = next((turn for turn in targets if turn.object_id == state.get("turn_id")), None)
                if (turn is not None and turn.object_id == state_row.object_id
                        and turn.payload.get("project_id") == project_id
                        and turn.payload.get("item_id") == row.object_id
                        and turn.payload.get("thread_id") == state.get("thread_id")):
                    states.append({**state, "created_at": turn.payload.get("created_at", "")})
            if states:
                state = max(states, key=lambda payload: (payload.get("created_at", ""), payload.get("updated_at", "")))
                turn = records.read("v2_turns", state["turn_id"])
                instance = getattr(application.state, "workbench_instance", None)
                if (instance is not None and turn.payload.get("instance") != instance
                        and turn.payload.get("receipt", {}).get("remember", {}).get("state") == "processing"):
                    state = {**state, "state": "failed", "error": "interrupted"}
                if state.get("state") in {"processing", "failed"}:
                    view = view or {"id": row.object_id, "kind": "intake", "title": row.payload.get("title", row.object_id),
                        "progress": {"done": 4 if row.payload.get("status") == "confirmed" else 1, "total": 4},
                        "pending_count": len(by_document.get(row.payload.get("document_id"), ())),
                        "updated_at": state["updated_at"]}
                    code = state.get("error")
                    view.update(state=state["state"], error=None if state["state"] == "processing" else code if code in {
                        "processing_failed", "insight_generation_failed", "insight_invalid_output", "insight_private_project",
                        "confirmation_failed", "related_retrieval_failed", "interrupted", "remote_disabled",
                        "private_project_remote_blocked", "scene_assignment_failed"} else "processing_failed",
                        target=_turn_target(records, project_id, turn))
            if view is not None:
                if local_fallback:
                    view['image_read'] = {'local_fallback': True}
                if target is not None:
                    view["target"] = target
                items.append(view)
        now = _now()
        for row in records.list_matching('v2_consolidation_jobs', project_id=project_id):
            status = row.payload.get('status')
            state = 'processing' if status == 'running' else 'failed' if status == 'failed' else 'done'
            if state == 'done' and not _recent_completion(row.payload.get('updated_at'), now):
                continue
            items.append({'id': row.object_id, 'kind': 'task', 'title': '现在整理', 'state': state,
                'progress': None, 'pending_count': 0, 'error': 'task_failed' if state == 'failed' else None,
                'target': {'type': 'task', 'id': row.object_id}, 'updated_at': row.payload.get('updated_at')})
        represented_tasks = set()
        task_targets = {}
        for turn in sorted(turns, key=lambda row: (row.payload.get("created_at", ""), row.object_id)):
            if turn.payload.get("intent") != "do":
                continue
            task_id = turn.payload.get("receipt", {}).get("do", {}).get("task_id")
            target = _turn_target(records, project_id, turn)
            if isinstance(task_id, str) and task_id and target is not None:
                task_targets[task_id] = target
            view = _do_view(records, scope, turn, now, document_namespace)
            if view is not None:
                items.append(view)
                task_id = turn.payload["receipt"]["do"].get("task_id")
                if task_id:
                    represented_tasks.add(task_id)
        for row in records.list_matching("recognition_tasks", project_id=project_id):
            if row.object_id in represented_tasks:
                continue
            view = _task_view(records, scope, row, now, document_namespace)
            if view is not None:
                if row.object_id in task_targets:
                    view["target"] = task_targets[row.object_id]
                items.append(view)
        items.sort(key=lambda item: (item["updated_at"] or "", item["id"]), reverse=True)
        return {"items": items, "counts": {
            state: sum(item["state"] == state for item in items)
            for state in ("processing", "pending", "failed")}}

    @router.post('/{job_id}/retry')
    def retry_backup(job_id: str):
        backup = getattr(application.state, 'memory_backup', None)
        result = backup.retry(job_id) if backup is not None else None
        if result is None:
            raise HTTPException(404, 'backup_job_not_found')
        if result.get('status') == 'failed':
            raise HTTPException(400, 'backup_failed')
        return {'id': job_id, 'status': result['status']}

    application.include_router(router)
