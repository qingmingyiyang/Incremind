import sqlite3

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from backend.security.user_context import USER_ACCESS, UserError, authorize_user
from core.ai_kernel.product_admission import bundle_from_payload, validate_bundle
from ..kernel.route_source_snapshot import native_source_connection, native_source_snapshot
from ..transaction_records import TransactionRecords
from ..workspace_contracts import _project
from .shared_task_redo import KIND as REDO_KIND
from .task_do import TASK_EXECUTIONS
from .turn_execution import REQUESTS, SAFE_ERRORS, _REQUEST_KEY
from .turn_frames import FRAMES, STREAMS
from .turn_logical_scope import TurnLogicalScope
from .turn_message_order import ACCEPTANCES


_FIELDS = {'project_id', 'scope_kind', 'intent', 'text'}
_OPTIONAL = {'executor', 'thread_id'}


def _unverifiable():
    return HTTPException(409, 'shared_task_command_unverifiable')


def _acceptance_ids(reader, key):
    # 在原读取事务中按请求编号选择记录，不加载其它对话正文。
    return [row[0] for row in reader.connection.execute(
        "SELECT object_id FROM crp_structured_records WHERE collection = ? "
        "AND json_extract(payload_json, '$.request_key') = ? LIMIT 2", (ACCEPTANCES, key))]


def _native_task_exists(records, reader, public_id):
    # 原生任务编号随机，受理轮次固定绑定任务幂等键和会话。
    with native_source_connection(records, reader=reader) as connection:
        return connection.execute(
            "SELECT 1 FROM ai_turns WHERE idempotency_key = ? OR "
            "(session_id = ? AND json_extract(request_json, '$.desired_outcome') = 'project.task') LIMIT 1",
            ('task-' + public_id, 'session-' + public_id)).fetchone() is not None


def _accepted(scope, reader, request, acceptance, turn, execution, stream, frames, *, redo_body=None, redo_kernel_id=None):
    project, public_id, thread_id = (acceptance[field] for field in ('requested_project_id', 'public_id', 'thread_id'))
    thread = reader.read('v2_threads', thread_id)
    if (thread is None or thread.payload.get('project_id') != project
            or turn.payload.get('project_id') != project or turn.payload.get('thread_id') != thread_id
            or turn.payload.get('intent') != 'do' or turn.payload.get('parent_turn_id') is not None
            or execution.payload.get('project_id') != project
            or execution.payload.get('task_text') != turn.payload.get('user_text')):
        return None
    native = execution.payload['request']
    kernel_id = turn.payload['receipt']['do']['kernel_turn_id']
    if (native['turn_id'] != kernel_id or native['session_id'] != 'session-' + public_id
            or native['idempotency_key'] != 'task-' + public_id or native['desired_outcome'] != 'project.task'
            or native['scope']['project_id'] != project
            or scope.read(turn, reader=reader) != 'shared'):
        return None
    if request['state'] == 'completed':
        saved = request['result']['turn']
        if (saved.get('id') != public_id or saved.get('scope_kind') != 'shared' or saved.get('intent') != 'do'
                or saved.get('thread_id') != thread_id or saved.get('user_text') != turn.payload.get('user_text')):
            return None
    elif 'result' in request:
        return None
    delivery = {'id': public_id, **{field: turn.payload[field]
        for field in ('thread_id', 'intent', 'user_text', 'created_at')}}
    for row in (stream, frames):
        if row is not None and (row.payload.get('project_id') != project
                or row.payload.get('thread_id') != thread_id or row.payload.get('turn') != delivery):
            return None
    snapshot = None
    if stream is not None or redo_body is not None:
        snapshot = native_source_snapshot(scope.records, kernel_id, reader=reader)
    if stream is not None and (snapshot['request'] is None
            or stream.payload.get('binding') != {'kernel_turn_id': kernel_id, 'request': snapshot['request']}):
        return None
    if frames is not None and frames.payload.get('projection') != {'kernel_turn_id': kernel_id, 'request': native}:
        return None
    if redo_body is not None:
        bundle = snapshot['admission']
        if snapshot['request'] is None:
            bundle = bundle_from_payload(execution.payload['admission_bundle'])
            validate_bundle(native, bundle)
        items = [] if bundle is None else [item for item in bundle.items if item.kind == REDO_KIND]
        if len(items) != 1:
            return None
        original = items[0].payload['source']
        if (original['public_id'] != redo_body['_redo'] or original['revision'] != redo_body['expected_revision']
                or original['source_kind'] != redo_body['source_kind']
                or original['guard']['execution_revision'] != original['revision']
                or original['native_request']['turn_id'] != redo_kernel_id
                or original['native_request']['session_id'] != 'session-' + redo_body['_redo']):
            return None
    return {'state': 'accepted', 'thread_id': thread_id, 'turn_id': public_id}


def read_shared_task_command(application, executions, key, project_id, scope_kind, *, redo_from=None, expected_revision=None):
    if scope_kind != 'shared':
        raise HTTPException(400, 'invalid_scope_kind')
    if not isinstance(key, str) or not _REQUEST_KEY.fullmatch(key):
        raise HTTPException(400, 'invalid_idempotency_key')
    project = _project(project_id)
    redo_body = None
    if redo_from is not None or expected_revision is not None:
        if redo_from is None or type(expected_revision) is not int or expected_revision < 1:
            raise HTTPException(400, 'invalid_task_division')
        redo_from = _project(redo_from)
        redo_body = {'project_id': project, '_redo': redo_from,
            'source_kind': 'execution', 'expected_revision': expected_revision}
    state, access = application.state, USER_ACCESS.get()
    context = getattr(state, 'server_context', None)
    if (context is None or access is None or access.target_user_id != getattr(state, 'server_user_id', None)
            or getattr(state, 'shared_query_factory', None) is None):
        raise HTTPException(403, 'shared_task_owner_changed')
    try:
        current = authorize_user(context.users, access.caller, access.target_user_id)
    except UserError:
        raise HTTPException(403, 'shared_task_owner_changed') from None
    if current != access:
        raise HTTPException(403, 'shared_task_owner_changed')
    records = state.recognition_records
    learning = getattr(state, 'product_steer_learning', None)
    if (learning is None or learning.application is not application or learning.records is not records
            or executions is not getattr(state, 'workbench_turn_execution', None) or executions.records is not records):
        raise _unverifiable()
    order = learning.order
    result = {'command_id': key, 'project_id': project, 'scope_kind': 'shared', 'state': 'unknown'}
    if redo_body is not None:
        result.update(redo_from=redo_from, expected_revision=expected_revision)
    scope = TurnLogicalScope(application)
    redo_kernel_id = None
    with records.begin_read() as tx:
        try:
            order.freshness._validate()
            reader = order.freshness._reader(TransactionRecords(tx))
            request = reader.read(REQUESTS, key)
            identities = _acceptance_ids(reader, key)
        except (ValueError, OSError, sqlite3.Error):
            raise _unverifiable() from None
        if redo_body is not None:
            original = reader.read('v2_turns', redo_from)
            if (original is None or original.payload.get('project_id') != project
                    or original.payload.get('intent') != 'do'):
                raise HTTPException(404, 'shared_task_command_unavailable')
            try:
                original_task = original.payload['receipt']['do']
                original_shared = not original_task.get('executor') and scope.read(original, reader=reader) == 'shared'
                if original_shared:
                    original_thread = _project(original.payload['thread_id'])
                    redo_kernel_id = _project(original_task['kernel_turn_id'])
            except (HTTPException, ValueError, KeyError, TypeError, AttributeError, OSError, sqlite3.Error):
                raise _unverifiable() from None
            if not original_shared:
                raise HTTPException(404, 'shared_task_command_unavailable')
        if request is None:
            if identities:
                raise _unverifiable()
            raise HTTPException(404, 'shared_task_not_received')
        body = request.payload.get('body')
        if not isinstance(body, dict):
            raise HTTPException(404, 'shared_task_command_unavailable')
        if redo_body is not None:
            valid_body = body == redo_body and type(body.get('expected_revision')) is int
            thread_id, origin = original_thread, 'redo'
        else:
            valid_body = (_FIELDS <= body.keys() <= _FIELDS | _OPTIONAL
                and body.get('project_id') == project and body.get('scope_kind') == 'shared'
                and body.get('intent') == 'do' and body.get('executor', 'builtin') == 'builtin')
            thread_id, origin = body.get('thread_id'), 'user'
        if not valid_body:
            raise HTTPException(404, 'shared_task_command_unavailable')
        if len(identities) != 1:
            raise _unverifiable()
        try:
            accepted = order._acceptance(reader, identities[0]).payload
        except (ValueError, AttributeError, KeyError, TypeError):
            raise _unverifiable() from None
        if (accepted['requested_project_id'] != project or accepted['origin'] != origin
                or accepted['new_thread'] != (thread_id is None)
                or thread_id is not None and accepted['thread_id'] != thread_id):
            return result
        public_id = accepted['public_id']
        turn = reader.read('v2_turns', public_id)
        execution = reader.read(TASK_EXECUTIONS, public_id)
        stream, frames = reader.read(STREAMS, public_id), reader.read(FRAMES, public_id)
        phase = request.payload.get('state')
        error = request.payload.get('error')
        if (redo_body is not None and phase == 'interrupted' and error == 'turn_interrupted'
                and request.payload.get('status_code') == 409 and 'result' not in request.payload):
            # 旧实例仍可能提交，已记录的中断只解释待核对状态。
            return {**result, 'error': error}
        if any(row is not None for row in (turn, execution, stream, frames)):
            if phase in {'running', 'completed'} and turn is not None and execution is not None:
                try:
                    return {**result, **(_accepted(scope, reader, request.payload,
                        accepted, turn, execution, stream, frames, redo_body=redo_body,
                        redo_kernel_id=redo_kernel_id) or {})}
                except (ValueError, KeyError, TypeError, AttributeError, OSError, sqlite3.Error):
                    return result
            return result
        if phase == 'running' and request.payload.get('instance') == executions.instance:
            return {**result, 'state': 'preparing'}
        if phase == 'failed' and isinstance(error, str) and error in SAFE_ERRORS and 'result' not in request.payload:
            try:
                if not _native_task_exists(records, reader, public_id):
                    return {**result, 'state': 'failed', 'error': error}
            except (ValueError, OSError, sqlite3.Error):
                return result
    return result


def install_shared_task_command_route(router, application, executions):
    @router.get('/task-commands/{key}')
    def task_command(key: str, project_id: str, scope_kind: str, redo_from: str | None = None,
                     expected_revision: int | None = None):
        headers = {'Cache-Control': 'no-store'}
        try:
            result = read_shared_task_command(application, executions, key, project_id, scope_kind,
                redo_from=redo_from, expected_revision=expected_revision)
        except HTTPException as error:
            return JSONResponse({'detail': error.detail}, status_code=error.status_code, headers=headers)
        return JSONResponse(result, headers=headers)
