"""Durable workbench turns orchestrating the existing intake domains."""
from core.storage_provider.connection_scope import with_connection_scope, create_scoped_task
import asyncio
import json
import logging
import re
import sqlite3
from contextlib import nullcontext
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.responses import FileResponse

from backend.recognition import WorkScope, RecognitionError, RecognitionConflict
from backend.recognition_retrieval import retrieve
from core.storage_provider.observability import current_observation, observation_scope, stage, timed_stage
from .turn_timings import turn_timing
from backend.shared.runtime_logging import bind_turn_id
from ..workspace_media_url import media_platform
from .share_capture import install_share_capture, share_url
from ..workspace_contracts import _json, _now, _project, _text
from ..model_config import ModelConfigurationError
from .auto_confirm import process_and_confirm
from .do import read_task
from .task_do import TaskDo, TASK_EXECUTIONS
from .task_drafts import TaskDrafts
from .insight_generation import generate_insights
from .insights import insight_view, resolve_insight
from .intent import parse_scope_tag, route_intent
from .policies import get, override, version
from .policies.pipelines import versions_for_turn
from .policies.types import PlaceInput
from .layers import is_verified
from .projects import DEFAULT_NAME, assign_scene, display_name, project_by_tag
from .privacy import is_private_project, egress_allowed
from .favorites import is_favorite_url, video_url, reuse_video
from ..packet_egress import validate_packet_egress
from ..workspace_contracts import _empty_ask_context
from .turn_execution import TurnExecutionService, SAFE_ERRORS
from .usage import record_answer_usage
from .followup import read_history, condense, validate_history, sum_usage
from .multi_query import expand_plan, expand_gap_plan
from .bookshelf import consult_bookshelf, relearn_cited
from .outcome_corrections import record_redo
from ..transaction_records import TransactionRecords
from core.document_engine import SQLiteDocumentRepository
from .workbench_transport import negotiate_turn_response, SSEDelivery, routed_response
from .route import RouteService
from .multipart import execute_parts, project_parts

from .workbench_receipts import (
    ITEM_STATES,
    _ASK_LAYERS,
    _ERRORS,
    _ask_receipt,
    _safe_error,
    persist_workbench_turn,
)


_LOGGER = logging.getLogger(__name__)


def install_workbench_routes(application, *, records, models, documents, service, workspace, organization=None, research_reader=None, topology_reader=None, usage_reader=None, reminders=None):
    router = APIRouter(prefix="/api/v2/workbench")
    from .placement import PlacementSuggestions
    from .overviews import ScopeOverviews
    placement = PlacementSuggestions(records, documents, service, ScopeOverviews(records, documents, models))
    instance = "workbench-instance-" + uuid4().hex
    tasks = set()
    running = set()
    item_locks = {}
    application.state.workbench_tasks = tasks
    application.state.workbench_instance = instance
    from ..research_sources import ReadControl
    from .turn_frames import TurnFrames
    application.state.product_task_read_control_type = ReadControl
    application.state.product_task_frame_factory = TurnFrames
    research = TaskDo(records, models, TaskDrafts(records, documents), organization, research_reader, topology_reader,
        query=workspace.query, service=service,
        execution_getter=lambda: (getattr(application.state, 'ai_runtime', None),
                                  getattr(application.state, 'ai_turn_runner', None)))
    def observe_task(turn):
        return research.observe(turn) if turn.payload.get('instance') != instance else turn.payload['receipt']['do']
    application.state.workbench_task_observer = observe_task
    from .external_task_do import ExternalTaskDo
    external_research = ExternalTaskDo(application, records, models, TaskDrafts(records, documents), workspace.query)
    def task_service(turn_id):
        state = records.read(TASK_EXECUTIONS, turn_id)
        return external_research if state is not None and state.payload.get('executor') else research
    research_running = set()
    favorites_retry_locks = {}
    routes = RouteService(records, models)

    async def research_process(turn_id, timing=None):
        bind_turn_id(turn_id)
        context = (observation_scope(timing) if timing is not None
                   else turn_timing(records, "task", turn_id=turn_id))
        with context:
            try:
                await research_process_measured(turn_id)
            finally:
                if timing is not None:
                    timing.finish()

    async def research_process_measured(turn_id):
        try:
            while True:
                await task_service(turn_id).advance(turn_id)
                row = records.read("v2_turns", turn_id)
                if (row is None or row.payload["receipt"]["do"].get("task_id")
                        or row.payload["receipt"]["do"].get("state") in {"done", "partial", "failed", "interrupted", "stopped"}):
                    break
                await asyncio.sleep(1)
        finally:
            research_running.discard(turn_id)

    def start_research(turn_id):
        if turn_id in research_running:
            return
        research_running.add(turn_id)
        timing = current_observation()
        if timing is not None:
            timing.defer_finish()
        task = create_scoped_task(research_process(turn_id, timing))
        tasks.add(task)
        def finished(done):
            tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                _LOGGER.warning("workbench_failed code=research_progress_failed")
        task.add_done_callback(finished)

    def scoped(collection, identity, project, *, read=None):
        row = (records.read if read is None else read)(collection, identity)
        if row is None or row.payload.get("project_id") != project:
            raise HTTPException(404, "workbench_not_found")
        return row

    def record_stop(turn_id, project, generation):
        signal = application.state.memory_signals
        prepared = signal.prepare({'project_id': project, 'kind': 'stop',
            'client_id': 'stop-' + turn_id + '-' + str(generation), 'turn_id': turn_id}, server=True)
        signal.safe_record(prepared)

    @timed_stage("persist")
    def save(turn_id, receipt, *, run_id):
        with records.begin() as tx:
            row = tx.read("v2_turns", turn_id)
            if row is None or row.payload.get("run_id") != run_id:
                return
            now = _now()
            tx.put("v2_turns", turn_id, {**row.payload, "receipt": receipt, "updated_at": now}, expected_revision=row.revision)
            thread = tx.read("v2_threads", row.payload["thread_id"])
            tx.put("v2_threads", thread.object_id, {**thread.payload, "updated_at": now}, expected_revision=thread.revision)
            remember = receipt.get("remember")
            if remember:
                current = tx.read(ITEM_STATES, turn_id)
                tx.put(ITEM_STATES, turn_id, {"project_id": row.payload["project_id"],
                    "item_id": remember["item_id"], "turn_id": turn_id, "thread_id": thread.object_id,
                    "state": remember["state"], "error": remember["error"], "updated_at": now},
                    expected_revision=current.revision if current else 0)
            tx.commit()

    def view(row, *, read_records=None):
        payload = row.payload
        project, scope = payload["project_id"], WorkScope("local-user", payload["project_id"])
        receipt = payload["receipt"]
        if payload["intent"] == "remember":
            memory = dict(receipt["remember"])
            if memory["item_id"] is None:
                return {"id": row.object_id, "thread_id": payload["thread_id"], "intent": payload["intent"], "user_text": payload["user_text"], "created_at": payload["created_at"], "receipt": receipt}
            item = workspace.items.item_for(memory["item_id"], project)
            status = item.payload.get("status")
            memory["document_id"] = item.payload.get("document_id")
            done = 4 if status == "confirmed" else 3 if item.payload.get("draft") else 2 if item.payload.get("source_text") else 1
            memory["progress"] = {"done": done, "total": 4}
            memory["title"] = payload.get("favorite_title_prefix", "") + item.payload.get("title", memory["title"])
            if status == "failed" and not (memory["state"] == "processing" and row.object_id in running):
                memory.update(state="failed", error=memory.get("error") or "processing_failed")
            elif memory["state"] == "processing" and payload.get("instance") != instance:
                memory.update(state="failed", error="interrupted")
            memory["verified"] = bool(memory["document_id"] and is_verified(records, documents, memory["document_id"]))
            from .document_filings import filing_view
            filed = filing_view(records, memory['document_id'], project) if memory['document_id'] else None
            if (filed is not None and filed['state'] == 'filed'
                    and not (memory['state'] == 'processing' and row.object_id in running)):
                auxiliary = records.read('v2_document_filing_aux', filed['filing_document_id'])
                if auxiliary is not None and auxiliary.payload.get('project_id') == filed['target_project_id']:
                    extraction_error = memory.get('error') in {
                        None, 'insight_generation_failed', 'insight_invalid_output', 'insight_private_project'}
                    # 资料库显式补提炼后，原对话直接读取同一份已完成结果。
                    if auxiliary.payload['state'] == 'done':
                        memory['insights'] = list(auxiliary.payload['insights'])
                    if (filed.get('error') and status == 'confirmed'
                            and memory['state'] in {'done', 'failed'} and extraction_error):
                        memory.update(state='failed', error=filed['error'])
                    elif (auxiliary.payload['state'] == 'done' and status == 'confirmed'
                            and memory['state'] == 'failed' and extraction_error):
                        memory.update(state='done', error=None)
            # An automatically filed item's insights were extracted in its new project.
            insight_scope = (WorkScope("local-user", filed['target_project_id'])
                if filed is not None and filed['state'] == 'filed' else scope)
            memory["insights"] = [result for old in memory["insights"]
                if (result := insight_view(records, insight_scope, old["id"], service=service)) is not None
                and not (result['kind'] == 'candidate' and result['state'] == 'forgotten')]
            if memory['document_id']:
                placement_input = records.read('v2_place_inputs', row.object_id)
                if placement_input is not None:
                    document = documents.read(memory['document_id'])
                    assignment = records.read('v2_scene_assignments_document', memory['document_id'])
                    memory.update(document_revision=document['revision'],
                        scene=assignment.payload['scene'] if assignment else None,
                        assignment_revision=assignment.revision if assignment else 0)
                hint = records.read('v2_place_hints', memory['document_id'])
                if (hint is not None and hint.payload.get('source_project_id') == project
                        and hint.payload['document_revision'] == documents.read(memory['document_id'])['revision']):
                    assignment = records.read('v2_scene_assignments_document', memory['document_id'])
                    memory['placement'] = {**hint.payload,
                        'assignment_revision': assignment.revision if assignment else 0,
                        'current_scene': assignment.payload['scene'] if assignment else None}
                if filed is not None:
                    memory['filing'] = filed
            receipt = {"remember": memory}
        elif payload["intent"] == "ask":
            from ..kernel.receipt_projection import question_receipt, frozen_answer_request
            from .turn_frames import failed_stream_event
            receipt = {"ask": question_receipt(workspace.query.answer_turns.root, row.object_id,
                                               scope.project_id, receipt["ask"],
                                               records=records if read_records is None else read_records)}
            frozen = frozen_answer_request(workspace.query.answer_turns.root, row.object_id,
                project, question=payload['user_text'])
            failure = (failed_stream_event(workspace.query.answer_turns.root, row.object_id,
                frozen, safe_errors=SAFE_ERRORS) if frozen is not None else None)
            runtime = getattr(application.state, 'ai_runtime', None)
            if failure is not None:
                receipt = {'ask': {**{key: value for key, value in receipt['ask'].items() if key != 'interruption'},
                    'answer': None, 'error': failure['code']}}
            elif (runtime is not None and receipt['ask'].get('interruption') == 'stopped'
                    and runtime.product_stops.continuing(row.object_id)):
                receipt = {'ask': {**{key: value for key, value in receipt['ask'].items() if key != 'interruption'},
                    'answer': None}}
        elif payload["intent"] == "do":
            receipt = {"do": read_task(records if read_records is None else read_records,
                scope, documents, observe_task(row), models=models, usage_reader=usage_reader)}
        elif payload["intent"] == "inspiration":
            old = receipt["inspiration"]["insight"]
            receipt = {"inspiration": {"insight": insight_view(records, scope, old["id"], service=service)}}
            hint = records.read('v2_place_hints_candidate', old['id'])
            current = records.read('recognition_candidates', old['id'])
            if (hint is not None and current is not None
                    and hint.payload['source_project_id'] == scope.project_id
                    and hint.payload['candidate_revision'] == current.revision):
                receipt['inspiration']['placement'] = dict(hint.payload)
        elif payload["intent"] == "multi":
            receipt = project_parts(records, payload, view)
        return {"id": row.object_id, "thread_id": payload["thread_id"], "intent": payload["intent"],
            "user_text": payload["user_text"], "created_at": payload["created_at"], "receipt": receipt}

    application.state.workbench_remember_observer = lambda row: view(row)['receipt']['remember']

    def related(scope, text, insights):
        own = {row.object_id for insight in insights
            if (row := resolve_insight(records, scope, insight["id"])) is not None}
        entries = []
        for entry in service.retrieval_entries(scope=scope):
            if entry["id"] in own:
                continue
            preference = records.read("recognition_recall_preferences", entry["id"])
            if (preference and preference.payload.get("project_id") == scope.project_id
                    and preference.payload.get("user_id") == scope.user_id
                    and preference.payload.get("state") == "forgotten"):
                continue
            entries.append(entry)
        result = retrieve(scope.project_id, text, entries, limit=3)
        return [{"id": match.id, "text": match.content} for match in result.hits]

    async def process_locked(turn_id, project, item_id, run_id):
        stage = "processing_failed"
        row = scoped("v2_turns", turn_id, project)
        receipt = {"remember": dict(row.payload["receipt"]["remember"])}
        memory = receipt["remember"]
        try:
            admission_errors = []
            await process_and_confirm(workspace, item_id, project, on_error=admission_errors.append)
            if admission_errors:
                stage = admission_errors[0]
                raise RuntimeError
            item = workspace.items.item_for(item_id, project)
            if item.payload["status"] != "confirmed":
                stage = "processing_failed" if item.payload["status"] == "failed" else "confirmation_failed"
                raise RuntimeError
            memory["document_id"] = item.payload["document_id"]
            memory["progress"] = {"done": 4, "total": 4}
            placement_input = records.read('v2_place_inputs', turn_id)
            automatic = (placement_input is not None and not placement_input.payload['tagged']
                and project == 'default')
            from .document_filings import filing_view
            prior_filing = filing_view(records, memory['document_id'], project)
            already_filed = prior_filing is not None and prior_filing['state'] == 'filed'
            if placement_input is not None:
                if placement_input.payload.get('project_id') != project:
                    raise RecognitionConflict('placement_scope_changed')
                # 日常里由模型自动归属，不再另给关键词建议，免得两边说法不一。
                if not automatic and not already_filed:
                    await run_in_threadpool(placement.admit, memory['document_id'], project,
                        tagged=placement_input.payload['tagged'],
                        policy_version=placement_input.payload['policy_version'])
            filed = None
            if automatic or already_filed:
                # 没写 #项目 的资料不堆在日常：由模型判断归到已有项目或新建项目（用户 2026-10-08）。
                from .auto_place import auto_file
                filed = await run_in_threadpool(auto_file, records, documents, service, models,
                    project, memory['document_id'], retry_token=run_id)
            if filed is not None:
                # 认识在目标项目里重新提炼，回执照样列出，确认时按 filing 的目标项目；移动可以撤销。
                memory["insights"], memory["related"] = list(filed.get("insights") or []), []
                memory.update(state='failed' if filed.get('error') else 'done', error=filed.get('error'))
            else:
                stage = "insight_generation_failed"
                errors = []
                memory["insights"] = await run_in_threadpool(generate_insights, models, service, documents,
                    project, memory["document_id"], on_error=errors.append, retry_token=run_id,
                    attempt=(turn_id, run_id))
                failure = next((code for code in errors if code in _ERRORS and code != "insight_private_project"), None)
                if failure:
                    stage = failure
                    raise RuntimeError
                save(turn_id, receipt, run_id=run_id)
                stage = "related_retrieval_failed"
                memory["related"] = await run_in_threadpool(related, WorkScope("local-user", project),
                    row.payload["user_text"] or item.payload.get("source_text") or item.payload["title"], memory["insights"])
                memory.update(state="done", error=None)
        except asyncio.CancelledError:
            memory.update(state="failed", error="interrupted")
        except Exception as error:
            memory.update(state="failed", error=_safe_error(error, stage))
            _LOGGER.warning("workbench_failed code=%s", memory["error"])
        finally:
            save(turn_id, receipt, run_id=run_id)
            running.discard(turn_id)
            start_index = getattr(application.state, "start_vector_index", None)
            if memory.get("state") == "done" and start_index is not None:
                # 本机向量模式下，新资料不必等下次启动才有向量（用户 2026-10-09）。
                start_index()

    @with_connection_scope
    async def process(turn_id, project, item_id, run_id, timing=None):
        bind_turn_id(turn_id)
        # Initial submissions retain the ingress observation across background
        # processing; retries receive a fresh observation under the same turn ID.
        context = (observation_scope(timing) if timing is not None
                   else turn_timing(records, "remember", turn_id=turn_id))
        with context:
            try:
                async with item_locks.setdefault((project, item_id), asyncio.Lock()):
                    await process_locked(turn_id, project, item_id, run_id)
            finally:
                if timing is not None:
                    timing.finish()

    def start(turn_id, project, item_id, run_id):
        running.add(turn_id)
        timing = current_observation()
        if timing is not None:
            timing.defer_finish()
        task = create_scoped_task(process(turn_id, project, item_id, run_id, timing))
        tasks.add(task)
        def finished(completed):
            tasks.discard(completed)
            running.discard(turn_id)
            if not completed.cancelled() and completed.exception() is not None:
                _LOGGER.warning("workbench_failed code=workbench_persistence_failed")
        task.add_done_callback(finished)

    def project_for_tag(tag, *, rows=None):
        projects = {row.object_id: display_name(row.object_id, row.payload["name"])
                    for row in (records.list("v2_projects") if rows is None else rows)}
        projects.setdefault("inbox", "收件箱")
        projects.setdefault("me", "我")
        projects.setdefault("default", DEFAULT_NAME)
        if tag in projects:
            return tag
        if (legacy := project_by_tag(projects, tag)) is not None:
            return legacy
        matches = [identity for identity, name in projects.items() if name == tag]
        if len(matches) > 1:
            raise HTTPException(409, "ambiguous_project_tag")
        if matches:
            return matches[0]
        raise HTTPException(404, "project_tag_not_found")

    @router.post("/files")
    async def upload(project_id: str = Form("default"), file: UploadFile = File(...),
                     files: list[UploadFile] | None = File(None)):
        if files:
            return await workspace.intake.add_images(project_id, [file, *files])
        return await workspace.intake.add_file(project_id, file)

    def image_attachments(item_id, project_id):
        from .image_read import uploaded_images, group_entries
        project_id = _project(project_id)
        row = workspace.items.item_for(item_id, project_id)
        if row.payload.get('input_kind') != 'image':
            raise HTTPException(404, 'original_not_found')
        try:
            images = uploaded_images(records, workspace.intake.runtime_root, project_id,
                [{'type': 'original_item', 'id': item_id, 'revision': row.revision, 'project_id': project_id}])
            names = group_entries(records, row.payload)
        except RecognitionError as exc:
            raise HTTPException(409, str(exc)) from None
        return project_id, images, names

    @router.get('/items/{item_id}/images')
    def list_images(item_id: str, project_id: str = 'default'):
        from urllib.parse import urlencode
        from .image_read import image_inference_for_document
        project_id, images, names = image_attachments(item_id, project_id)
        query = urlencode({'project_id': project_id})
        result = {'images': [{'ordinal': image['ordinal'], 'name': entry['name'],
            'url': f'/api/v2/workbench/items/{item_id}/images/{image["ordinal"]}?{query}'}
            for image, entry in zip(images, names)]}
        inference = image_inference_for_document(records, documents, workspace.items.item_for(item_id, project_id))
        if inference is not None:
            result['image_read'] = inference
        return result

    @router.get('/items/{item_id}/images/{ordinal}')
    def download_image(item_id: str, ordinal: int, project_id: str = 'default'):
        _, images, names = image_attachments(item_id, project_id)
        if not 1 <= ordinal <= len(images):
            raise HTTPException(404, 'original_not_found')
        return FileResponse(images[ordinal - 1]['identity']['path'], filename=names[ordinal - 1]['name'])

    @with_connection_scope
    async def execute_one(body, *, request_key=None, on_started=None, on_delta=None, on_frame=None, replace_turn=None, title_prefix="", division_override=None, redo_from=None, outcome_selection=None, **part_options):
        context = (nullcontext() if current_observation() is not None
                   else turn_timing(records, "workbench"))
        with override(**versions_for_turn('memory.organize')), context:
            return await execute_one_measured(body, request_key=request_key, on_started=on_started,
                on_delta=on_delta, on_frame=on_frame, replace_turn=replace_turn, title_prefix=title_prefix,
                division_override=division_override, redo_from=redo_from, outcome_selection=outcome_selection, **part_options)

    async def execute_one_measured(body, *, request_key=None, on_started=None, on_delta=None, on_frame=None, replace_turn=None, title_prefix="", division_override=None,
                                   redo_from=None, outcome_selection=None,
                                   forced_turn_id=None, parent_turn_id=None, forced_scene=None,
                                   instruction=None, situation=None, dependency_turns=(), forced_project=None, route_meta=None, prepared_reminder=None):
        if set(body).difference({"project_id", "thread_id", "text", "item_id", "intent", "continue_from", "executor"}):
            raise HTTPException(400, "invalid_turn_fields")
        text = body.get("text", "")
        if not isinstance(text, str) or len(text) > 60000:
            raise HTTPException(400, "invalid_text")
        tag, scene, cleaned = parse_scope_tag(text)
        if parent_turn_id:
            if tag is not None and (project_for_tag(tag) != body.get('project_id') or scene != forced_scene):
                raise HTTPException(400, 'invalid_part_scope')
            scene = forced_scene
        if outcome_selection is not None:
            scene = outcome_selection['scene']
        intent = body['intent'] if 'intent' in body else get('route')(route_intent, text, bool(body.get('item_id')))
        if not isinstance(intent, str):
            raise HTTPException(400, "invalid_intent")
        executor = body.get('executor', 'builtin')
        if not isinstance(executor, str) or executor not in {'builtin', 'claude-code', 'codex', 'deepseek-harness'}:
            raise HTTPException(400, 'invalid_executor')
        if executor != 'builtin' and (intent != 'do' or body.get('intent') != 'do'
                or body.get('item_id') is not None or body.get('continue_from') is not None
                or division_override is not None or parent_turn_id is not None):
            raise HTTPException(400, 'invalid_executor_request')
        if intent == "do" and executor == 'builtin':
            available = getattr(organization, 'available', None)
            if organization is None or (callable(available) and not await run_in_threadpool(available)):
                raise HTTPException(503, "task_runtime_unavailable")
        if intent not in {"remember", "inspiration", "ask", "do"}:
            raise HTTPException(400, "invalid_intent")
        if 'continue_from' in body and intent != 'do':
            raise HTTPException(400, 'invalid_continue_from')
        current_project = _project(body.get("project_id", "default"))
        thread_id = body.get("thread_id")
        if thread_id is not None:
            _project(thread_id)
        item_id = body.get('item_id') if intent == 'remember' else None
        if item_id is not None:
            _project(item_id)
        initial_rows = records.read_batch({
            'v2_projects': None if tag else (),
            'v2_threads': (thread_id,) if thread_id else (),
            'workspace_items': (item_id,) if item_id else (),
        })
        initial = {collection: {row.object_id: row for row in rows} for collection, rows in initial_rows.items()}
        def initial_read(collection, identity):
            return initial[collection].get(identity)
        project = get('place')(PlaceInput(
            project_for_tag(tag, rows=initial_rows['v2_projects']) if tag else forced_project,
            intent, current_project))
        placement_version = version('place')
        scope = WorkScope("local-user", project)
        if thread_id:
            scoped("v2_threads", thread_id, project, read=initial_read)
        else:
            thread_id = "thread-" + uuid4().hex
        turn_id = forced_turn_id or replace_turn or "turn-" + uuid4().hex
        bind_turn_id(turn_id)
        timing = current_observation()
        if timing is not None:
            timing.set_turn_id(turn_id)
            timing.operation = "task" if intent == "do" else intent
        run_id = uuid4().hex
        from .part_context import prepare_context
        part_context = prepare_context(records, workspace.query, project, parent_turn_id, dependency_turns)
        research_state = None
        candidate_hint = None
        if intent == "remember" and prepared_reminder is not None:
            if reminders is None:
                raise ValueError('reminder_service_missing')
            item_id, item = None, None
            receipt = {"remember": {"item_id": None, "title": cleaned.splitlines()[0][:80],
                "state": "done", "progress": {"done": 4, "total": 4}, "document_id": None,
                "verified": False, "insights": [], "related": [], "error": None}}
        elif intent == "remember":
            item_id = body.get("item_id")
            if item_id is not None:
                _project(item_id)
            if item_id:
                item = workspace.items.item_for(item_id, project, read=initial_read)
                item = workspace.items.public_row(item)
                url = share_url(cleaned)
                if item.get('input_kind') == 'image' and url is not None and media_platform(url) == 'xiaohongshu':
                    try:
                        item = await workspace.intake.bind_link_images(item_id, project, url,
                            expected_revision=item['revision'])
                    except RecognitionConflict:
                        raise HTTPException(409, 'item_revision_conflicted') from None
            else:
                cleaned = _text(cleaned, "text")
                url = share_url(cleaned)
                item = await (workspace.intake.add_link({"project_id": project, "url": url})
                    if url
                    else workspace.intake.add_text({"project_id": project,
                        "text": body['text'] if parent_turn_id else cleaned},
                        preserve_text=bool(parent_turn_id)))
                item_id = item["id"]
            if scene:
                assign_scene(records, "item", item_id, project, scene)
            if parent_turn_id:
                cleaned = body['text']
            receipt = {"remember": {"item_id": item_id, "title": item["title"], "state": "processing",
                "progress": {"done": 1, "total": 4}, "document_id": item.get("document_id"),
                "verified": False, "insights": [], "related": [], "error": None}}
        elif intent == 'do' and executor != 'builtin':
            cleaned = _text(cleaned, 'text')
            initial, research_state = await run_in_threadpool(external_research.initial, turn_id, project,
                instruction or cleaned, scene, executor=executor)
            receipt, item_id = {'do': initial}, None
        elif intent == "do":
            cleaned = _text(cleaned, "text")
            if not egress_allowed(records, models, project, 'generation'):
                raise HTTPException(409, 'task_remote_blocked')
            continuation_version = None
            explicit_choice = 'continue_from' in body
            try:
                continuation_version = version('continuation')
            except ValueError as error:
                if str(error) != 'unknown_policy_interface':
                    raise
                # 明确选择可使用已登记候选，省略时保持尚未激活的旧默认。
                continuation_version = '@1' if explicit_choice else None
            # 省略选择的重做保留已经冻结的目标与共同前驱，只取得本轮策略版本。
            if continuation_version is not None and (explicit_choice or outcome_selection is None):
                from .outcomes import select_outcome, candidates
                from .links import similarity
                choice = body.get('continue_from')
                if choice is not None and (not isinstance(choice, str) or not choice):
                    raise HTTPException(400, 'invalid_continue_from')
                try:
                    chosen = get('continuation', version=continuation_version)(cleaned,
                        candidates(records, project=project, document_id=choice), project_id=project, scene=scene,
                        score_text=similarity, continue_from=choice,
                        force_new=explicit_choice and choice is None)
                    outcome_selection = (select_outcome(records, project=project, scene=scene,
                        document_id=chosen['document_id']) if chosen['document_id'] is not None else None)
                except (RecognitionConflict, ValueError):
                    raise HTTPException(409, 'outcome_unavailable') from None
            try:
                initial, research_state = research.initial(turn_id, project, instruction or cleaned, scene,
                    division_override=division_override, continuation=outcome_selection,
                    continuation_version=continuation_version, situation=situation, part_context=part_context)
            except RecognitionConflict:
                if continuation_version is None:
                    raise
                raise HTTPException(409, 'source_changed_retry') from None
            receipt = {"do": initial}
            item_id = None
        elif intent == "ask":
            cleaned = _text(cleaned, "text")
            question = _text(instruction or cleaned, 'text')
            if len(question) > 1000:
                raise HTTPException(400, "question_too_long")
            from .elsewhere import selected_version
            elsewhere_version = selected_version()
            answered = await workspace.query.answer_turns.run(turn_id=turn_id, project=project, question=question,
                situation=situation, part_context=part_context,
                operation=lambda: _answer_workbench(workspace, records, project, question, scene,
                    thread_id, turn_id, on_started, on_delta, part_context=part_context,
                    parent_turn_id=parent_turn_id, on_frame=on_frame, situation=situation,
                    tagged=tag is not None or forced_project is not None, elsewhere_version=elsewhere_version))
            receipt, chosen = answered["receipt"], answered["chosen"]
            stopped_generation = (application.state.ai_runtime.product_stops.current(turn_id).payload['generation']
                if receipt['ask'].get('interruption') == 'stopped' else None)
            from ..kernel.receipt_projection import question_receipt
            receipt = {"ask": question_receipt(workspace.query.answer_turns.root, turn_id,
                                               project, receipt["ask"], records=records)}
            item_id = None
        else:
            cleaned = _text(cleaned, "text")
            experience = service.stage_experience(scope=scope, content=cleaned,
                provenance={"kind": "user_statement", "actor": "local-user"})
            candidate = service.propose(scope=scope, content=cleaned, source_experience_ids=[experience])
            if scene:
                assign_scene(records, "candidate", candidate.id, project, scene)
            receipt = {"inspiration": {"insight": insight_view(records, scope, candidate.id, service=service)}}
            if tag is None and placement_version != '@1':
                from dataclasses import asdict
                hint = placement.suggest(cleaned, '', project, policy_version=placement_version)
                if hint is not None:
                    candidate_hint = {**asdict(hint), 'source_project_id': project,
                        'candidate_revision': candidate.revision, 'policy_version': placement_version}
                    receipt['inspiration']['placement'] = candidate_hint
            item_id = None
        now = _now()
        created_at = scoped("v2_turns", replace_turn, project).payload["created_at"] if replace_turn else now
        response = {"thread_id": thread_id, "turn": {"id": turn_id, "thread_id": thread_id,
            "intent": intent, "user_text": cleaned, "created_at": created_at, "receipt": receipt}}
        with stage("persist"), records.begin() as tx:
            if outcome_selection is not None:
                from .outcomes import validate_selection
                validate_selection(tx, project=project, scene=scene, selection=outcome_selection)
            if redo_from is not None:
                previous, sample_revision = redo_from
                old_turn, sample = tx.read('v2_turns', previous), tx.read('v2_task_divisions', previous)
                if (old_turn is None or old_turn.payload.get('project_id') != project or sample is None
                        or sample.payload.get('project_id') != project or sample.payload.get('deleted')):
                    raise HTTPException(404, 'task_division_not_found')
                if sample.revision != sample_revision:
                    raise HTTPException(409, 'task_division_revision_conflicted')
            if prepared_reminder is not None:
                # 提醒、Turn 与幂等完成记录同一次提交；stage 不另开写事务。
                reminders.stage(tx, project_id=project, scene=scene, turn_id=turn_id,
                                parsed=prepared_reminder)
            saved = persist_workbench_turn(tx, turn_id=turn_id, project=project, thread_id=thread_id,
                cleaned=cleaned, now=now, created_at=created_at, intent=intent, receipt=receipt,
                item_id=item_id, item=item if intent == 'remember' else None,
                instance=instance, run_id=run_id, title_prefix=title_prefix,
                replace_turn=replace_turn, research_state=research_state,
                parent_turn_id=parent_turn_id, route_meta=route_meta,
                track_item=prepared_reminder is None)
            if redo_from is not None:
                enlisted = SQLiteDocumentRepository(TransactionRecords(tx), namespace_id=documents.namespace_id)
                record_redo(tx, enlisted, scope=scope, turn_id=redo_from[0], new_turn_id=turn_id, now=now)
            if intent == "remember" and prepared_reminder is None:
                if placement_version != '@1':
                    old_input = tx.read('v2_place_inputs', turn_id)
                    frozen_input = {'project_id': project, 'tagged': tag is not None,
                        'policy_version': placement_version}
                    if old_input is not None and dict(old_input.payload) != frozen_input:
                        raise RecognitionConflict('placement_inputs_changed')
                    if old_input is None:
                        tx.put('v2_place_inputs', turn_id, frozen_input, expected_revision=0)
            if candidate_hint is not None:
                current = tx.read('recognition_candidates', candidate.id)
                if current is None or current.revision != candidate_hint['candidate_revision']:
                    raise RecognitionConflict('placement_candidate_changed')
                tx.put('v2_place_hints_candidate', candidate.id, candidate_hint, expected_revision=0)
            executions.complete(tx, request_key, response)
            tx.commit()
        if intent == "remember" and prepared_reminder is None:
            start(turn_id, project, item_id, run_id)
        elif intent == 'inspiration':
            from .stats import record_activity
            record_activity(records, 'remember', project, candidate.id,
                            event_id='remember-' + candidate.id)
        elif intent == "ask" and not receipt["ask"]["no_match"] and 'interruption' not in receipt['ask']:
            record_answer_usage(records, chosen, receipt["ask"]["citations"], project)
            relearn_cited(records, chosen, receipt["ask"]["citations"], turn_id=turn_id if parent_turn_id else None)
        if intent == 'ask' and receipt['ask'].get('interruption') == 'stopped':
            record_stop(turn_id, project, stopped_generation)
        if research_state is not None:
            start_research(turn_id)
        if title_prefix:
            response['turn']['receipt']['remember']['title'] = title_prefix + response['turn']['receipt']['remember']['title']
        return response

    async def execute_favorites(body, *, replace_turn=None, **part_options):
        tag, scene, cleaned = parse_scope_tag(body.get('text', ''))
        project = project_for_tag(tag) if tag else _project(body.get('project_id', 'default'))
        thread_id = body.get('thread_id')
        if thread_id:
            scoped('v2_threads', _project(thread_id), project)
        discovery = getattr(application.state, 'bilibili_favorites_discovery', None)
        if not callable(discovery):
            raise HTTPException(503, 'favorites_discovery_unavailable')
        if not egress_allowed(records, models, project, 'generation'):
            raise HTTPException(409, 'private_project_remote_blocked' if is_private_project(records, project) else 'remote_disabled')
        try:
            found = await run_in_threadpool(discovery, url=cleaned.split()[0], project_id=project)
            if not isinstance(found, (list, tuple)) or not found:
                raise ValueError('favorites_empty')
            urls = list(dict.fromkeys(video_url(value) for value in found))
        except Exception:
            identity, thread_id, now = part_options.get('forced_turn_id') or replace_turn or 'turn-' + uuid4().hex, thread_id or 'thread-' + uuid4().hex, _now()
            receipt = {'remember': {'item_id': None, 'title': '收藏夹', 'state': 'failed', 'error': 'favorites_discovery_failed',
                'progress': {'done': 0, 'total': 4}, 'document_id': None, 'verified': False, 'insights': [], 'related': []}}
            with records.begin() as tx:
                if tx.read('v2_threads', thread_id) is None:
                    tx.put('v2_threads', thread_id, {'project_id': project, 'title': '收藏夹', 'created_at': now, 'updated_at': now}, expected_revision=0)
                old = tx.read('v2_turns', identity)
                tx.put('v2_turns', identity, {'project_id': project, 'thread_id': thread_id, 'intent': 'remember', 'user_text': body['text'],
                    'created_at': old.payload['created_at'] if old else now, 'updated_at': now, 'receipt': receipt, 'item_id': None,
                    'instance': instance, 'run_id': uuid4().hex,
                    **({'route':part_options['route_meta']} if part_options.get('route_meta') else {}),
                    **({'parent_turn_id':part_options['parent_turn_id']} if part_options.get('parent_turn_id') else {})}, expected_revision=old.revision if old else 0)
                tx.commit()
            return {'thread_id': thread_id, 'turn': view(scoped('v2_turns', identity, project))}
        first = None
        for index, url in enumerate(urls[:50]):
            with turn_timing(records, "remember"):
                item_id = await reuse_video(workspace, records, models, project, url)
                text = body['text'] if part_options.get('parent_turn_id') and index == 0 else body['text'].replace(cleaned.split()[0], url, 1)
                options = {**part_options}
                if index:
                    options.update(forced_turn_id=None, request_key=None)
                    options.pop('route_meta', None)
                result = await execute_one({'project_id': project, 'text': text, 'intent': 'remember', 'item_id': item_id,
                    **({'thread_id': thread_id} if thread_id else {})}, replace_turn=replace_turn if index == 0 else None,
                    title_prefix=f'（共 {len(urls)}，先取 50）' if index == 0 and len(urls) > 50 else '', **options)
            first = first or result
            thread_id = result['thread_id']
        return first

    def entry_versions():
        return {**versions_for_turn('memory.organize'), **versions_for_turn('project.answer')}

    async def execute_routed_part(body, **options):
        _, _, cleaned = parse_scope_tag(body.get('text', ''))
        if body['intent'] == 'remember' and not body.get('item_id') and cleaned and is_favorite_url(cleaned.split()[0]):
            return await execute_favorites(body, **options)
        return await execute_one(body, **options)

    async def execute_turn(body, *, request_key=None, on_started=None, on_delta=None, on_plan=None, on_frame=None):
        with override(**entry_versions()):
            return await execute_turn_prepared(body, request_key=request_key, on_started=on_started,
                on_delta=on_delta, on_plan=on_plan, on_frame=on_frame)

    async def execute_turn_prepared(body, *, request_key=None, on_started=None, on_delta=None, on_plan=None, on_frame=None):
        route_meta = None
        project = _project(body.get('project_id', 'default'))
        text = body.get('text', '')
        tag, scene, cleaned = parse_scope_tag(text)
        if tag:
            project = project_for_tag(tag)
        requested = body.get('intent')
        if requested not in {None, 'auto', 'remember', 'ask', 'do', 'inspiration'}:
            raise HTTPException(400, 'invalid_intent')
        if (requested in {None, 'auto'} and not body.get('item_id')
                and getattr(get('retrieve'), 'agentic', False)):
            from .agentic_rag import pending_clarification
            if pending_clarification(workspace.query, records, project, body.get('thread_id')) is not None:
                # 此处已在原 TurnExecutionService 的幂等认领之后，同键回放不会重新路由。
                body, requested = {**body, 'intent': 'ask'}, 'ask'
        parsed = None
        reminder_branch = False
        if (requested in {None, 'auto', 'remember'} and reminders is not None
                and body.get('item_id') is None and cleaned.startswith('提醒我')):
            try:
                get('remind')
            except ValueError as error:
                if str(error) != 'unknown_policy_interface':
                    raise
                # 候选尚未激活时沿原路径处理；非法版本等真实契约错误继续抛出。
            else:
                reminder_branch = True
                parsed = reminders.parse(text, eligible_text=cleaned)
        if reminder_branch:
            if on_plan:
                representation = await on_plan('remember')
                if representation != 'sse':
                    on_started = on_delta = on_frame = None
            # 解析不出时间时仍由原普通记住处理，不再交路由重新选择意图。
            return await execute_one({**body, 'intent': 'remember'}, request_key=request_key,
                on_started=on_started, on_delta=on_delta, on_frame=on_frame, prepared_reminder=parsed)
        if requested in {None, 'auto'}:
            plan = await run_in_threadpool(routes.route, text, project_id=project,
                request_key=request_key or 'request-' + uuid4().hex, has_files=bool(body.get('item_id')))
            if on_plan:
                representation = await on_plan('multi' if len(plan.parts) > 1 else plan.parts[0].intent)
                if representation != 'sse':
                    on_started = on_delta = on_frame = None
            if len(plan.parts) > 1:
                return await execute_parts(body, plan, records=records, instance=instance, execute=execute_routed_part,
                    project=project, scene=scene, request_key=request_key, complete=executions.complete,
                    on_started=on_started, on_delta=on_delta)
            part = plan.parts[0]
            route_meta = {key:getattr(plan, key) for key in ('mode', 'usage', 'egress_receipt_id')}
            body = {**body, 'intent':part.intent}
            if part.situation is not None:
                return await execute_one(body, request_key=request_key, on_started=on_started,
                    on_delta=on_delta, on_frame=on_frame, situation=part.situation, route_meta=route_meta)
        _, _, cleaned = parse_scope_tag(body.get('text', ''))
        intent = body['intent'] if 'intent' in body else get('route')(route_intent, body.get('text', ''), bool(body.get('item_id')))
        if intent == 'remember' and not body.get('item_id') and cleaned and is_favorite_url(cleaned.split()[0]):
            return await execute_favorites(body, **({'route_meta':route_meta} if route_meta else {}))
        return await execute_one(body, request_key=request_key, on_started=on_started, on_delta=on_delta, on_frame=on_frame, route_meta=route_meta)

    executions = TurnExecutionService(records, execute_turn, instance=instance)
    application.state.workbench_turn_execution = executions
    install_share_capture(router, records=records, workspace=workspace, executions=executions, instance=instance, project_for_tag=project_for_tag)

    @router.post("/turns")
    @with_connection_scope
    async def create(request: Request):
        body = await _json(request)
        if body.get('executor', 'builtin') != 'builtin':
            from backend.security.device_identity import server_mode
            host = getattr(application.state, 'external_execution_host', None)
            if server_mode(request) and (host is None or not host.server_available(body['executor'])):
                raise HTTPException(409, 'external_host_isolation_unavailable')
            server = request.scope.get('server')
            if (not isinstance(server, (tuple, list)) or len(server) != 2
                    or server[0] not in {'127.0.0.1', '::1'} or type(server[1]) is not int
                    or not 1 <= server[1] <= 65535):
                raise HTTPException(409, 'external_task_backend_unavailable')
            previous = getattr(application.state, 'external_mcp_backend', tuple(server))
            if previous != tuple(server):
                raise HTTPException(409, 'external_task_backend_unavailable')
            application.state.external_mcp_backend = tuple(server)
        if set(body).difference({"project_id", "thread_id", "text", "item_id", "intent", "continue_from", "executor"}):
            raise HTTPException(400, "invalid_turn_fields")
        if not isinstance(body.get("text", ""), str) or len(body.get("text", "")) > 60000:
            raise HTTPException(400, "invalid_text")
        if 'intent' in body and not isinstance(body['intent'], str):
            raise HTTPException(400, 'invalid_intent')
        if body.get('executor', 'builtin') != 'builtin' and body.get('intent') != 'do':
            raise HTTPException(400, 'invalid_executor_request')
        if body.get('intent') in {None, 'auto'}:
            return await routed_response(executions, body, request.headers.get('idempotency-key'),
                request.headers.get('accept'))
        key = request.headers.get("idempotency-key")
        intent = executions.completed_intent(body, key)
        selections = {} if intent is not None else entry_versions()
        if intent is None:
            with override(**selections):
                intent = body['intent'] if 'intent' in body else get('route')(route_intent, body.get('text', ''), bool(body.get('item_id')))
        representation = negotiate_turn_response(request.headers.get("accept"), allow_stream=intent in {"ask", "do"})
        async def execute_created(on_started=None, on_delta=None, on_frame=None):
            with override(**selections):
                if body.get('executor', 'builtin') != 'builtin':
                    from .external_server_identity import request_memory_identity
                    try:
                        with request_memory_identity(request, application.state):
                            result = await executions.run(body, key, on_started=on_started, on_delta=on_delta, on_frame=on_frame)
                    except ValueError as error:
                        if str(error) != 'external_server_identity_unavailable':
                            raise
                        raise HTTPException(409, 'external_host_identity_unavailable') from None
                else:
                    result = await executions.run(body, key, on_started=on_started, on_delta=on_delta, on_frame=on_frame)
                bind_turn_id(result['turn']['id'])
                return result
        if representation == "sse":
            if intent == 'do':
                # Initial execution accepts and starts the existing protected
                # task once. Its running receipt is not a delivery terminal.
                result = await execute_created()
                turn = result['turn']
                product = records.read('v2_turns', turn['id'])
                execution = records.read(TASK_EXECUTIONS, turn['id'])
                if product is None or execution is None:
                    raise HTTPException(404, 'workbench_not_found')
                accepted = {'turn': {key: turn[key] for key in
                    ('id', 'thread_id', 'intent', 'user_text', 'created_at')},
                    'request': execution.payload['request']}
                response = _read_turn_stream(records, workspace.query.answer_turns.root, turn['id'],
                    _project(product.payload['project_id']), 0, view=view, safe_errors=SAFE_ERRORS, accepted=accepted)
                response.headers['Vary'] = 'Accept'
                return response
            delivery = SSEDelivery(records)
            return delivery.response(lambda started, delta, frame: execute_created(started, delta, frame))
        result = await execute_created()
        return JSONResponse(result, headers={"Vary": "Accept", "Cache-Control": "no-store"})

    @router.post('/turns/{turn_id}/stop')
    async def stop_turn(turn_id: str, body: dict):
        if set(body) != {'project_id'}:
            raise HTTPException(400, 'invalid_turn_fields')
        project, identity = _project(body['project_id']), _project(turn_id)
        product = records.read('v2_turns', identity)
        if (product is not None and product.payload.get('project_id') == project
                and product.payload.get('intent') == 'ask' and view(product)['receipt']['ask'].get('error')):
            raise HTTPException(409, 'turn_finished')
        if (product is not None and product.payload.get('project_id') == project
                and product.payload.get('receipt', {}).get('do', {}).get('executor')):
            raise HTTPException(409, 'external_task_continuation_unsupported')
        runtime = await run_in_threadpool(workspace.query.answer_turns._runtime)
        try:
            state, generation = await run_in_threadpool(runtime.product_stops.request, identity, project,
                application.state.ai_turn_runner)
        except ValueError as error:
            if str(error) == 'workbench_not_found':
                raise HTTPException(404, 'workbench_not_found') from None
            if str(error) == 'turn_finished':
                raise HTTPException(409, 'turn_finished') from None
            raise
        if product is not None:
            record_stop(identity, project, generation)
        return JSONResponse({'state': state}, status_code=202)

    @router.post('/turns/{turn_id}/continue')
    async def continue_answer(turn_id: str, body: dict, request: Request):
        if set(body) != {'project_id'}:
            raise HTTPException(400, 'invalid_turn_fields')
        project = _project(body['project_id'])
        turn = scoped('v2_turns', _project(turn_id), project)
        key = request.headers.get('idempotency-key')
        from .turn_execution import _REQUEST_KEY
        if not isinstance(key, str) or not _REQUEST_KEY.fullmatch(key):
            raise HTTPException(400, 'invalid_idempotency_key')
        if turn.payload.get('intent') == 'do':
            if turn.payload['receipt']['do'].get('executor'):
                raise HTTPException(409, 'external_task_continuation_unsupported')
            try:
                await research.continue_on_request(turn_id, project, key, instance=instance)
            except ValueError:
                raise HTTPException(409, 'source_changed_retry') from None
            return view(scoped('v2_turns', turn_id, project))
        if turn.payload.get('intent') != 'ask':
            raise HTTPException(409, 'turn_interrupted')
        if view(turn)['receipt']['ask'].get('error'):
            raise HTTPException(409, 'turn_interrupted')
        service = workspace.query.answer_turns
        def validate_current(row):
            from ..kernel.answer_continuations import plan_binding
            if 'parameters' in plan_binding(workspace.query, row):
                workspace.query.validate_answer_request(workspace.query.models, service.store_request(turn_id))
            else:
                _restored_answer_plan(workspace.query, row)
        async def operation(row, invocation):
            from ..kernel.answer_continuations import plan_binding, decode
            binding = plan_binding(workspace.query, row)
            if 'parameters' in binding:
                return await _answer_workbench(workspace, records, on_started=None, on_delta=None,
                    **decode(binding['parameters']))
            return await _continue_answer_workbench(workspace.query, records, turn, row, invocation)
        try:
            result = await service.continue_on_request(identity=turn_id, project=project, key=key,
                operation=operation, validate_current=validate_current)
        except ValueError:
            raise HTTPException(409, 'source_changed_retry') from None
        from ..kernel.receipt_projection import question_receipt
        receipt = {'ask': question_receipt(service.root, turn_id, project, result['receipt']['ask'], records=records)}
        if 'interruption' not in turn.payload.get('receipt', {}).get('ask', {}):
            # Only the service's saved resume action can reach this replay.
            # Keep the original projection and never count cited use twice.
            if receipt != turn.payload.get('receipt'):
                raise HTTPException(409, 'turn_interrupted')
            return view(turn)
        with records.begin() as tx:
            current = scoped('v2_turns', turn_id, project, read=tx.read)
            if current != turn:
                raise HTTPException(409, 'turn_in_progress')
            row = tx.put('v2_turns', turn_id, {**current.payload, 'receipt': receipt, 'updated_at': _now()},
                expected_revision=current.revision)
            executions.complete(tx, None, {'thread_id': row.payload['thread_id'],
                'turn': {'id': row.object_id, **row.payload}})
            tx.commit()
        if 'interruption' not in receipt['ask']:
            record_answer_usage(records, result['chosen'], receipt['ask']['citations'], project)
            relearn_cited(records, result['chosen'], receipt['ask']['citations'])
        return view(row)

    @router.get("/threads")
    def threads(project_id: str = "default"):
        rows = records.list_matching("v2_threads", project_id=_project(project_id))
        return {"items": [{"id": row.object_id, "title": row.payload["title"], "updated_at": row.payload["updated_at"]}
            for row in sorted(rows, key=lambda row: row.payload["updated_at"], reverse=True)
            if not row.payload.get('parent_turn_id')]}

    @router.get('/turns/{turn_id}/stream')
    async def turn_stream(turn_id: str, request: Request, project_id: str = 'default', after: str | None = None):
        from .turn_frames import MAX_SEQUENCE
        project, identity = _project(project_id), _project(turn_id)
        def cursor(value):
            if not isinstance(value, str) or len(value) > 16 or not re.fullmatch(r'0|[1-9][0-9]*', value):
                raise HTTPException(400, 'invalid_stream_cursor')
            result = int(value)
            if result > MAX_SEQUENCE:
                raise HTTPException(400, 'invalid_stream_cursor')
            return result
        header = request.headers.get('last-event-id')
        start = cursor(after) if after is not None else cursor(header) if header is not None else 0
        if header is not None and cursor(header) != start:
            raise HTTPException(400, 'invalid_stream_cursor')
        return _read_turn_stream(records, workspace.query.answer_turns.root, identity, project, start,
            view=view, safe_errors=SAFE_ERRORS)

    from .workbench_stream import read_turn_stream as _read_turn_stream

    @router.get("/threads/{thread_id}")
    async def thread(thread_id: str, project_id: str = "default"):
        project = _project(project_id)
        scoped("v2_threads", _project(thread_id), project)
        rows = records.list_matching("v2_turns", project_id=project, thread_id=thread_id)
        for row in rows:
            if row.payload["intent"] == "do":
                if row.payload["receipt"]["do"].get("executor"):
                    await external_research.advance(row.object_id, observe_only=row.payload.get("instance") != instance)
                    continue
                if row.payload.get('instance') != instance:
                    # 重启后只读原任务，保留未知执行且不重新派发。
                    continue
                if (row.payload["receipt"]["do"].get("kernel_turn_id")
                        and not row.payload["receipt"]["do"].get("task_id")
                        and row.payload["receipt"]["do"].get("state") not in {"done", "partial", "failed", "interrupted"}):
                    await task_service(row.object_id).advance(row.object_id)
                    if records.read("v2_turns", row.object_id).payload["receipt"]["do"].get("state") not in {"done", "partial", "failed", "interrupted"}:
                        start_research(row.object_id)
        rows = records.list_matching("v2_turns", project_id=project, thread_id=thread_id)
        aggregated = {identity for row in rows if row.payload.get('intent') == 'multi'
                      for identity in row.payload['part_turn_ids']}
        turns = [view(row) for row in rows if row.object_id not in aggregated]
        from ..kernel.receipt_projection import frozen_answer_request
        from .turn_frames import FRAMES, frame_text
        saved = {row.object_id for row in rows}
        for frame in records.list_matching(FRAMES, project_id=project, thread_id=thread_id):
            if frame.object_id in saved:
                continue
            turn = frame.payload.get('turn', {})
            if (turn.get('id') != frame.object_id or turn.get('thread_id') != thread_id
                    or turn.get('intent') != 'ask' or not isinstance(turn.get('created_at'), str)):
                continue
            frozen = frozen_answer_request(workspace.query.answer_turns.root, frame.object_id, project,
                question=turn.get('user_text'))
            if frozen is None:
                continue
            try:
                recipe = get('retry', version=(frozen.get('policy_versions') or {}).get('retry', '@1'))
                raw = frame_text(frame.payload)
                if raw is None:
                    continue
                partial = recipe({'kind': 'partial_output', 'text': raw})
            except (ValueError, KeyError, TypeError):
                continue
            ask = {'answer': None, **partial, 'citations': [], 'no_match': False,
                'layers': dict.fromkeys((*_ASK_LAYERS.values(), 'persona'), 0),
                'trace': [], 'egress_receipt_id': None, 'model_usage': {}, 'model_cost': None,
                'excluded_sources': [], 'context': _empty_ask_context()}
            turns.append({**turn, 'receipt': {'ask': ask}})
        return {'id': thread_id, 'turns': sorted(turns, key=lambda turn: turn['created_at'])}

    @router.get('/turns/{turn_id}/division')
    async def division(turn_id: str, project_id: str = 'default'):
        project = _project(project_id)
        scoped('v2_turns', _project(turn_id), project)
        try:
            return research.divisions.read(turn_id,project)
        except ValueError:
            raise HTTPException(404,'task_division_not_found') from None

    @router.patch('/turns/{turn_id}/division')
    async def change_division(turn_id: str, request: Request):
        body = await _json(request)
        if set(body) != {'project_id','items','expected_revision'}:
            raise HTTPException(400,'invalid_task_division')
        project = _project(body['project_id'])
        scoped('v2_turns',_project(turn_id),project)
        try:
            research.divisions.adjust(turn_id,project=project,items=body['items'],expected_revision=body['expected_revision'])
            return research.divisions.read(turn_id,project)
        except RecognitionConflict:
            raise HTTPException(409,'task_division_revision_conflicted') from None
        except ValueError:
            raise HTTPException(400,'invalid_task_division') from None

    @router.delete('/turns/{turn_id}/division')
    async def delete_division(turn_id: str, request: Request):
        body = await _json(request)
        if set(body) != {'project_id','expected_revision'}:
            raise HTTPException(400,'invalid_task_division')
        project = _project(body['project_id'])
        scoped('v2_turns',_project(turn_id),project)
        try:
            research.divisions.remove(turn_id,project=project,expected_revision=body['expected_revision'])
        except RecognitionConflict:
            raise HTTPException(409,'task_division_revision_conflicted') from None
        except ValueError:
            raise HTTPException(404,'task_division_not_found') from None
        return {'deleted':True}

    @router.post('/turns/{turn_id}/redo')
    async def redo_division(turn_id: str, request: Request):
        body = await _json(request)
        if set(body).difference({'continue_from'}) != {'project_id','expected_revision'}:
            raise HTTPException(400,'invalid_task_division')
        project = _project(body['project_id'])
        old = scoped('v2_turns',_project(turn_id),project)
        if old.payload.get('receipt', {}).get('do', {}).get('executor'):
            raise HTTPException(409, 'external_task_continuation_unsupported')
        try:
            sample = research.divisions.read(turn_id,project)
        except ValueError:
            raise HTTPException(404,'task_division_not_found') from None
        if sample['revision'] != body['expected_revision']:
            raise HTTPException(409,'task_division_revision_conflicted')
        from .outcomes import COLLECTION, select_outcome
        document_id = old.payload.get('receipt', {}).get('do', {}).get('document_id')
        selected = None
        if 'continue_from' not in body and document_id and records.read(COLLECTION, document_id) is not None:
            execution = records.read(TASK_EXECUTIONS, turn_id)
            try:
                selected = select_outcome(records, project=project, scene=execution.payload['scene'],
                                          document_id=document_id, mode='redo')
            except (RecognitionConflict, AttributeError, KeyError):
                raise HTTPException(409, 'outcome_selection_changed') from None
        return await execute_one({'project_id':project,'thread_id':old.payload['thread_id'],
            'intent':'do','text':sample['task_text'],
            **({'continue_from': body['continue_from']} if 'continue_from' in body else {})},division_override=sample['items'],
            redo_from=(turn_id, sample['revision']), outcome_selection=selected)

    @router.post("/turns/{turn_id}/retry")
    async def retry(turn_id: str, request: Request):
        body = await _json(request)
        if set(body) != {"project_id"}:
            raise HTTPException(400, "invalid_retry_fields")
        project = _project(body["project_id"])
        row = scoped("v2_turns", _project(turn_id), project)
        current = view(row)
        if row.payload["intent"] != "remember" or current["receipt"]["remember"]["state"] != "failed" or turn_id in running:
            raise HTTPException(409, "turn_not_retryable")
        item_id = row.payload["item_id"]
        if item_id is None and current['receipt']['remember'].get('error') == 'favorites_discovery_failed':
            async with favorites_retry_locks.setdefault(turn_id, asyncio.Lock()):
                latest = scoped('v2_turns', turn_id, project)
                if latest.payload['receipt']['remember']['state'] != 'failed' or latest.payload['item_id'] is not None:
                    raise HTTPException(409, 'turn_not_retryable')
                result = await execute_favorites({'project_id': project, 'thread_id': latest.payload['thread_id'], 'text': latest.payload['user_text'], 'intent': 'remember'}, replace_turn=turn_id)
                return result['turn']
        workspace.items.item_for(item_id, project)
        run_id = uuid4().hex
        with records.begin() as tx:
            tx.put("v2_turns", turn_id, {**row.payload, "instance": instance, "run_id": run_id}, expected_revision=row.revision)
            tx.commit()
        current["receipt"]["remember"].update(state="processing", error=None)
        save(turn_id, current["receipt"], run_id=run_id)
        start(turn_id, project, item_id, run_id)
        return current

    application.include_router(router)


async def _answer_workbench(workspace, records, project, cleaned, scene, thread_id, turn_id, on_started, on_delta,
                            part_context=None, parent_turn_id=None, *, on_frame=None, situation=None, tagged=False, elsewhere_version=None):
    query = workspace.query
    if query.ask_target()["execution_location"] == "remote" and is_private_project(records, project):
        raise HTTPException(409, "private_project_remote_blocked")
    from ..kernel.answer_continuations import save_preparation
    from ..kernel.answer_turns import ACTIVE_ANSWER
    from .turn_frames import TurnFrames, STREAMS
    active_request, active_store, _, _ = ACTIVE_ANSWER.get()
    original_request = active_store.get_request(active_request['turn_id'])
    query.validate_answer_request(query.models, original_request)
    with records.begin() as tx:
        thread = tx.read('v2_threads', thread_id)
        if thread is not None and thread.payload.get('project_id') != project:
            raise HTTPException(404, 'workbench_not_found')
        if thread is None:
            now = _now()
            tx.put('v2_threads', thread_id, {'project_id': project, 'title': cleaned[:40],
                'created_at': now, 'updated_at': now}, expected_revision=0)
            tx.commit()
    stream = records.read(STREAMS, turn_id)
    turn = stream.payload['turn'] if stream is not None else {
        'id': turn_id, 'thread_id': thread_id, 'intent': 'ask', 'user_text': cleaned, 'created_at': _now()}
    frames = TurnFrames(records, turn_id=turn_id, project_id=project, recipe=get('retry'), turn=turn,
        request=original_request, on_frame=on_frame,
        text_prefix='' if query.answer_turns.active[turn_id].get('continuation') is not None else None)
    stream_bound = frames.start()
    save_preparation(query, turn_id, {'project': project, 'cleaned': cleaned, 'scene': scene,
        'thread_id': thread_id, 'turn_id': turn_id, 'part_context': part_context, 'parent_turn_id': parent_turn_id,
        'situation': situation, 'tagged': tagged, 'elsewhere_version': elsewhere_version},
        request=active_request, store=active_store, runtime=query.answer_turns.application.state.ai_runtime)
    if on_started and (on_frame is None or not stream_bound):
        on_started({'thread_id': thread_id, 'turn': {'id': turn_id, 'intent': 'ask', 'user_text': cleaned}})
    target, budget, overhead = query.ask_budget()
    history = read_history(records, project, thread_id, cleaned, budget=budget,
        prompt_overhead=overhead, excluded_turn=turn_id, query=query)
    rewritten = await condense(query, project, cleaned, history, turn_id=turn_id)
    retrieval_question = rewritten["question"] or cleaned
    agentic = getattr(get('retrieve'), 'agentic', False)
    collected = ({'candidates': [], 'excluded_sources': [], 'policy_versions': versions_for_turn('project.answer')}
        if agentic else query.collect_candidates(project, retrieval_question, scene=scene,
            **({'situation':situation} if situation is not None else {})))
    retrieval = get('retrieve', version=collected['policy_versions']['retrieve'])
    gap = getattr(retrieval, 'drilldown', False) is True and not agentic
    prepare = query.prepare_drilldown if gap else query.prepare_ask
    plan = prepare(project, cleaned, scene=scene, collected=collected,
        retrieval_question=retrieval_question, history=history["text"], situation=situation, part_context=part_context)
    if history["text"] or history.get('clarification'):
        try:
            validate_history(query, project, history, target)
        except RecognitionError:
            raise HTTPException(409, "source_changed_retry") from None
    if history["text"] or history.get('clarification'):
        plan["history_guard"] = lambda: validate_history(query, project, history, target)
    if agentic:
        from .agentic_rag import investigate
        try:
            plan, expanded, rewrite = await investigate(query, project, retrieval_question, plan,
                turn_id=turn_id, scene=scene, history=history)
        except RecognitionError:
            raise HTTPException(409, 'source_changed_retry') from None
    elif gap:
        try:
            plan, expanded, rewrite = await expand_gap_plan(query, project, retrieval_question, plan, collected,
                turn_id=turn_id, scene=scene)
        except RecognitionError:
            raise HTTPException(409, 'source_changed_retry') from None
        except ModelConfigurationError as failure:
            code = str(failure)
            _LOGGER.warning("ask model configuration failure: %s", code)
            if target['execution_location'] == 'remote' and code in {
                'model_configuration_changed_before_request', 'model_egress_remote_not_consented'
            }:
                if not egress_allowed(records, query.models, project, 'generation'):
                    code = 'private_project_remote_blocked' if is_private_project(records, project) else 'remote_disabled'
                elif code == 'model_configuration_changed_before_request' or query.ask_target() != target:
                    code = 'ask_model_target_changed'
            if code in {'ask_model_target_changed', 'remote_disabled', 'private_project_remote_blocked'}:
                raise HTTPException(409, code) from None
            raise HTTPException(502, 'answer_generation_failed') from None
    else:
        plan, expanded, rewrite = await expand_plan(query, project, retrieval_question, plan, collected,
            turn_id=turn_id, scene=scene)
    try:
        if not agentic:
            await run_in_threadpool(consult_bookshelf, query, project, retrieval_question, plan, scene=scene)
    except RecognitionError:
        raise HTTPException(409, "source_changed_retry") from None
    from .search import supplement
    wordings = [retrieval_question, *rewrite['queries']] if rewrite['used'] else [retrieval_question]
    try:
        if not agentic:
            await supplement(query, plan, cleaned, wordings, key=turn_id)
    except RecognitionError:
        raise HTTPException(409, 'source_changed_retry') from None
    plan["history_count"] = len(history["turns"])
    plan["trace"][0].update(rewrite=rewrite, rewrite_status=expanded["status"],
        rewrite_receipt_ids=expanded["receipt_ids"],
        condensed_question=rewritten["question"],
        condense_receipt_ids=rewritten["receipt_ids"], condense_status=rewritten["status"], condense_usage_known=bool(rewritten["usage"]),
        history_turn_ids=[{"id": turn["id"], "revision": turn["revision"]} for turn in history["turns"]])
    if agentic:
        plan['trace'][0]['agentic'] = plan['_rag_summary']
    # execute_ask releases its evidence after completion; retain only mapping metadata.
    chosen = tuple({"layer": candidate["layer"], "persona": bool(candidate.get("persona")),
        **({'inspiration': True} if candidate.get('inspiration') else {}),
        "bookshelf": bool(candidate.get("bookshelf")), "bookshelf_restore": candidate.get("bookshelf_restore"),
        "temporal": bool(candidate.get("temporal")), "validity": dict(candidate.get("validity") or {}),
        "stale": bool(candidate.get("stale")),
        "project_id": candidate["scope"].project_id,
        **({'url':candidate['href']} if candidate['kind'] == 'search' else {}),
        "entry": {key: candidate["entry"].get(key) for key in ("id", "document_id", "item_id", "source_id")}}
        for candidate in plan["chosen"])
    plan['search_materials'] = {index: dict(candidate) for index, candidate in enumerate(plan['chosen'], 1)
                                if candidate['kind'] == 'search'}
    try:
        plan["bookshelf_guard"]()
    except RecognitionError:
        raise HTTPException(409, "source_changed_retry") from None
    has_context = bool(chosen or plan.get("profile", {}).get("text") or part_context)
    clarifying = agentic and plan['_rag_summary'].get('status') == 'clarify'
    allow_elsewhere = not clarifying and (not agentic or not has_context)
    navigation_coverage = None
    if elsewhere_version is not None and allow_elsewhere:
        from core.search_and_recall.evidence_windows import query_terms
        wordings = [retrieval_question, *rewrite['queries']] if rewrite['used'] else [retrieval_question]
        navigation_coverage = get('elsewhere', version=elsewhere_version)(operation='coverage',
            coverage_questions=wordings, terms_for=query_terms,
            evidence='\n'.join(c['excerpt'] for c in plan['chosen'] if not c.get('persona')))
    redirect, routed = None, False
    if elsewhere_version is not None and allow_elsewhere and project == 'default' and not tagged and not part_context:
        has_hits = any(not c.get('persona') for c in chosen)
        if not (has_hits and (navigation_coverage or 0) >= .6):
            # 日常里没找到时先定该去哪个项目问；页面会转过去重问，不必先生成一句"没有资料"（用户 2026-10-09）。
            routed = True
            from .elsewhere import suggest_elsewhere
            redirect = suggest_elsewhere(query, project, cleaned, coverage=navigation_coverage,
                has_hits=has_hits, tagged=tagged, policy_version=elsewhere_version)
            if redirect is None:
                from .auto_place import ask_home
                redirect = await run_in_threadpool(ask_home, records, query.documents, query.models, project, cleaned, turn_id)
            if redirect is not None:
                has_context, chosen = False, ()
    if clarifying:
        has_context = False
    preview_id = query.store_ask_preview(plan) if has_context else None
    authority = None
    if parent_turn_id:
        from .part_context import freeze_answer_authority
        authority = freeze_answer_authority(plan, history, parent_turn_id, turn_id)
    if has_context:
        from ..kernel.answer_continuations import save_plan
        from ..kernel.answer_turns import ACTIVE_ANSWER
        active_request, active_store, _, _ = ACTIVE_ANSWER.get()
        save_plan(query, turn_id, plan, history, request=active_request, store=active_store,
            runtime=query.answer_turns.application.state.ai_runtime)
    def streamed(text):
        frames.delta(text)
        if on_delta is not None and (on_frame is None or not stream_bound):
            on_delta(text)
    interrupted = False
    try:
        if clarifying:
            text = '\n'.join(plan['_rag_summary']['questions'])
            streamed(text)
            result = {'answer': text, 'sources': [], 'model_used': True, 'no_match': False,
                      'model_usage': {}, 'excluded_sources': plan['excluded_sources']}
        elif has_context:
            result = await query.execute_ask(preview_id, project, cleaned, True,
                on_delta=streamed if on_delta is not None else None, on_retry=frames.retry)
        elif redirect is not None:
            # This turn only points to the project the page re-asks in.
            result = {"no_match": True, "answer": "", "sources": [], "model_used": False,
                      "excluded_sources": plan["excluded_sources"]}
        else:
            result = query.public_ask_preview('', plan)
        interrupted = 'interruption' in result
    finally:
        frames.close(completed=not interrupted)
    from .search import persist_cited
    search_originals = persist_cited(workspace.items, plan, result)
    chosen = tuple({**candidate, 'entry':{**candidate['entry'], 'item_id':search_originals[index]}}
                   if index in search_originals else candidate for index, candidate in enumerate(chosen, 1))
    for entry in (result.get('context') or {}).get('entries', []):
        if entry.get('url'):
            entry['id'] = next((identity for number, identity in search_originals.items()
                if chosen[number - 1]['url'] == entry['url']), entry['id'])
    result["model_usage"] = sum_usage(rewritten["usage"], expanded["usage"], result.get("model_usage", {}))
    if not rewritten["usage_complete"] or not expanded["usage_complete"]:
        result["model_usage"]["observed_only"] = True
    egress = records.read("workspace_ask_receipts", preview_id) if preview_id else None
    receipt = _ask_receipt(plan, chosen, result, preview_id, egress.payload if egress else None)
    if plan.get('search'):
        receipt['search'] = dict(plan['search'])
        partial = result['model_usage'].get('observed_only') is True or any(
            type(plan['search']['model_usage'].get(name)) is not int
            for name in ('input_tokens', 'output_tokens'))
        result['model_usage'] = sum_usage(result['model_usage'], plan['search']['model_usage'])
        if partial:
            result['model_usage']['observed_only'] = True
        receipt['model_usage'] = result['model_usage']
    if routed:
        if redirect is not None:
            receipt['elsewhere'] = redirect
    elif elsewhere_version is not None and allow_elsewhere:
        from .elsewhere import suggest_elsewhere
        hint = suggest_elsewhere(query, project, cleaned, coverage=navigation_coverage,
            has_hits=any(not c.get('persona') for c in chosen), tagged=tagged,
            policy_version=elsewhere_version)
        weak = not (any(not c.get('persona') for c in chosen) and (navigation_coverage or 0) >= .6)
        if hint is None and project == 'default' and not tagged and weak:
            # 资料会被自动归到别的项目，日常里找不到时指出该去哪个项目问（用户 2026-10-09）。
            from .auto_place import ask_home
            hint = await run_in_threadpool(ask_home, records, query.documents, query.models, project, cleaned, turn_id)
        if hint is not None:
            receipt['elsewhere'] = hint
    return {"receipt": {"ask": receipt}, "chosen": list(chosen),
            **({'multipart_authority':authority} if parent_turn_id else {})}


def _restored_answer_plan(query, row):
    from ..kernel.answer_continuations import restore_plan
    plan, guards, history = restore_plan(query, row)
    project = plan['project_id']
    if history.get('text'):
        plan['history_guard'] = lambda: validate_history(query, project, history, plan['target'])
    if 'overview_guard' in guards:
        from .overviews import validate_navigation
        values = guards['overview_guard']
        if set(values) != {'project', 'scene', 'overview'} or values['project'] != project:
            raise RecognitionError('invalid_answer_continuation_overview')
        plan['overview_guard'] = lambda values=values: validate_navigation(query, project, values['scene'], values['overview'])
    from .bookshelf import _validate
    values = guards['bookshelf_guard']
    if set(values) != {'spines', 'version', 'project', 'scene'} or values['project'] != project:
        raise RecognitionError('invalid_answer_continuation_bookshelf')
    plan['bookshelf_guard'] = lambda values=values: _validate(query, values['spines'], values['version'], project, values['scene'])
    if 'search_guard' in guards:
        from .search import validate_search_binding
        binding = guards['search_guard']
        plan['search_guard'] = lambda: validate_search_binding(query, binding, plan)
    query.validate_ask_plan(plan)
    return plan


async def _continue_answer_workbench(query, records, turn, row, invocation):
    from ..kernel.answer_turns import ACTIVE_ANSWER
    from .turn_frames import TurnFrames
    plan = _restored_answer_plan(query, row)
    frozen, store, _, _ = ACTIVE_ANSWER.get()
    original = store.get_immutable_payload(turn.object_id, 'answer-model-input-answer')
    if original is None:
        raise RecognitionError('original_answer_input_unavailable')
    prior = row.payload['result']
    old = prior['receipt']['ask']
    preview = query.store_ask_preview(plan)
    frames = TurnFrames(records, turn_id=turn.object_id, project_id=plan['project_id'], recipe=get('retry'),
        turn={key: turn.payload[key] for key in ('thread_id', 'intent', 'user_text', 'created_at')} | {'id': turn.object_id},
        request=store.get_request(turn.object_id), text_prefix=old['partial'])
    frame_started = False
    def streamed(text):
        nonlocal frame_started
        if text and not frame_started:
            frame_started = True
            frames.start()
        frames.delta(text)
    interrupted = False
    try:
        result = await query.execute_ask(preview, plan['project_id'], plan['question'], True, on_delta=streamed,
            on_retry=frames.retry,
            continuation={'invocation_key': invocation, 'messages': original[1]['messages'],
                'partial': old['partial'], 'usage': old['model_usage']})
        interrupted = 'interruption' in result
    finally:
        frames.close(completed=not interrupted)
    egress = records.read('workspace_ask_receipts', preview)
    return {'receipt': {'ask': _ask_receipt(plan, prior['chosen'], result, preview, egress.payload)},
            'chosen': prior['chosen']}
