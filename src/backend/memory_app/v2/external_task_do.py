"""工作台选择固定插件，复用原external.task.execute冻结任务并只保存草稿。"""
import json
from uuid import uuid4

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from core.storage_provider.connection_scope import with_connection_scope

from ..workspace_contracts import _now
from .external_executor_plugins import EXECUTOR_CLIENTS, plugin_status
from .external_execution import RESULT_KIND, RECEIPT_KIND
from .external_runner import ARCHIVE
from .external_runs import ExternalRuns
from core.ai_kernel import validate_turn_request
from .task_do import TASK_EXECUTIONS
from .outcomes import record_outcome


class ExternalTaskDo:
    def __init__(self, application, records, models, drafts, query):
        self.application, self.records, self.models = application, records, models
        self.drafts, self.query = drafts, query
        self.active = set()

    @with_connection_scope
    def initial(self, turn_id, project, text, scene, *, executor):
        runtime = self.query.answer_turns._runtime()
        state = self.application.state
        status = next(row for row in plugin_status(self.records, state, self.models)['plugins'] if row['id'] == executor)
        if not status['available']:
            raise HTTPException(409, status['reason'])
        external = getattr(state, 'external_runner', None)
        if external is None:
            raise HTTPException(409, 'external_task_runtime_unavailable')
        context, runner = state.external_context, state.ai_turn_runner
        delivery_id, identity = 'turn-' + uuid4().hex, 'turn-' + uuid4().hex
        try:
            host_owner = state.external_execution_host
            host_owner.bind_server_memory(identity)
            context.prepare_recall(delivery_id, {'client': EXECUTOR_CLIENTS[executor], 'tool': 'recall',
                'query': text, 'budget': 3000, 'scope': {'user_id': context.owner_id, 'project_id': project}},
                scene=scene, session_id='session-' + turn_id, operation_id='op-' + delivery_id,
                idempotency_key=delivery_id, created_at=_now())
            handoff = context.execute(delivery_id, runtime=runtime, runner=runner)
            # 输入正文只来自用户任务及已获资格的原交付，不从本机目录读取额外内容。
            task = json.dumps({'task': text, 'context': handoff['text']}, ensure_ascii=False)
            from pathlib import Path
            import sys
            config = {'mcpServers': {'chriptmas-memory': {'command': str(Path(sys.executable).resolve()),
                'args': ['-I', str(Path(__file__).resolve().parent.parent / 'mcp.py')]}}}
            host, port = state.external_mcp_backend
            config['mcpServers']['chriptmas-memory']['env'] = {
                'CHRIPTMAS_MCP_BACKEND_URL': 'http://' + ('[::1]' if host == '::1' else host) + ':' + str(port)}
            if state.deployment.mode == 'server':
                config['mcpServers']['chriptmas-memory']['env'].update({
                    'CHRIPTMAS_DEVICE_KEY': '${CHRIPTMAS_DEVICE_KEY}',
                    'CHRIPTMAS_MCP_TARGET_USER': '${CHRIPTMAS_MCP_TARGET_USER}'})
            request = external.prepare(identity, delivery_turn_id=delivery_id, executor=executor,
                cli_version=status['runtime_version'], task=task, mcp_config=config,
                session_id='session-' + turn_id, operation_id='op-' + identity,
                idempotency_key='task-' + turn_id, created_at=_now())
        except HTTPException:
            state.external_execution_host.release_server_memory(identity)
            raise
        except Exception:
            state.external_execution_host.release_server_memory(identity)
            raise HTTPException(409, 'external_task_not_prepared') from None
        receipt = {'task_id': None, 'document_id': None, 'title': text[:80], 'state': 'running',
            'executor': executor, 'kernel_turn_id': identity, 'experts': [], 'division': [],
            'continues': None, 'changes': [], 'fallback_new': False, 'division_examples': [],
            'progress': {'done': 0, 'total': 3}, 'usage': None}
        return receipt, {'project_id': project, 'request': request, 'task_text': text, 'scene': scene,
            'executor': executor, 'started': False, 'owner': None, 'claimed_at': None, 'failures': 0}

    def _observed(self, values, turn_id):
        state = self.application.state
        owner_id = state.external_context.owner_id
        request = validate_turn_request(values['request'])
        identity = request['turn_id']
        turns = state.ai_turn_store
        original, archive = turns.get_request(identity), turns.get_immutable_payload(identity, ARCHIVE)
        if (original != request or archive is None or archive[1]['request'] != request
                or archive[1]['owner_id'] != owner_id or archive[1]['turn_id'] != identity
                or archive[1]['launch']['executor'] != values['executor']
                or request['scope']['project_id'] != values['project_id']
                or request['session_id'] != 'session-' + turn_id
                or json.loads(request['input']['text']).get('task') != values['task_text']):
            raise ValueError('external_task_binding_invalid')
        terminal = state.ai_runtime.receipt_for(identity)
        payload, proof = turns.get_immutable_payload(identity, RESULT_KIND), turns.get_immutable_payload(identity, RECEIPT_KIND)
        run = ExternalRuns(self.records).read(identity, owner_id=owner_id)
        completed = (terminal.status == 'completed' and payload is not None and proof is not None
            and run is not None and run['status'] == 'completed'
            and proof[1] == {'turn_id': identity, 'owner_id': owner_id, 'payload_ref': payload[0],
                'run_revision': run['revision'], 'status': 'completed', 'effect_certainty': 'confirmed_applied'}
            and payload[1]['turn_id'] == identity and payload[1]['owner_id'] == owner_id
            and payload[1]['status'] == 'completed' and payload[1]['exit_code'] == run['exit_code'] == 0
            and payload[1]['usage'] == run['usage']
            and isinstance(payload[1]['message'], str) and 0 < len(payload[1]['message']) <= 200000
            and bool(payload[1]['message'].strip()))
        return terminal, payload, completed

    @with_connection_scope
    async def advance(self, turn_id, *, observe_only=False):
        if turn_id in self.active:
            return
        self.query.answer_turns._runtime()
        state = self.application.state
        owner, execute = uuid4().hex, False
        with self.records.begin() as tx:
            saved, turn = tx.read(TASK_EXECUTIONS, turn_id), tx.read('v2_turns', turn_id)
            if saved is None or turn is None or turn.payload['receipt']['do']['state'] != 'running':
                return
            values = dict(saved.payload)
            # 旧owner或已开始Turn只读原事实，不按时间释放ExternalRuns槽，也不重新提交。
            execute = not observe_only and not values['started'] and values.get('owner') is None
            if execute:
                tx.put(TASK_EXECUTIONS, turn_id, {**values, 'owner': owner, 'started': True,
                    'claimed_at': _now()}, expected_revision=saved.revision)
                tx.commit()
            else:
                owner = values.get('owner')
        self.active.add(turn_id)
        try:
            if execute:
                try:
                    await run_in_threadpool(state.ai_turn_runner.accept_and_submit, values['request'])
                    await run_in_threadpool(state.ai_turn_runner.wait_for_terminal,
                        values['request']['turn_id'], timeout_seconds=1230)
                except Exception:
                    pass
            try:
                terminal, payload, completed = self._observed(values, turn_id)
                status = terminal.status
            except Exception:
                payload, completed, status = None, False, 'interrupted'
            with self.records.begin() as tx:
                saved, turn = tx.read(TASK_EXECUTIONS, turn_id), tx.read('v2_turns', turn_id)
                if saved is None or turn is None or saved.payload.get('owner') != owner:
                    return
                receipt = dict(turn.payload['receipt']['do'])
                if completed:
                    identity = values['request']['turn_id']
                    result = self.drafts.create_in_uow(tx, turn_id=identity, project=values['project_id'],
                        operation='deliver-' + identity, title=receipt['title'], markdown=payload[1]['message'])
                    receipt.update(state='done', document_id=result['document_id'], verified=False,
                        usage=payload[1]['usage'], progress={'done': 3, 'total': 3})
                else:
                    receipt.update(state='failed' if status in {'failed', 'cancelled', 'timed_out'} else 'interrupted',
                        error='external_task_execution_failed' if execute else 'external_task_interrupted',
                        effect_certainty='unknown')
                tx.put('v2_turns', turn_id, {**turn.payload, 'receipt': {'do': receipt}, 'updated_at': _now()},
                    expected_revision=turn.revision)
                tx.put(TASK_EXECUTIONS, turn_id, {**saved.payload, 'owner': None, 'claimed_at': None},
                    expected_revision=saved.revision)
                if completed:
                    record_outcome(tx, project=values['project_id'], document_id=result['document_id'],
                        turn_id=turn_id, scene=values['scene'], task_text=values['task_text'])
                from .turn_frames import complete_stream
                updated = tx.read('v2_turns', turn_id)
                complete_stream(tx, {'thread_id': updated.payload['thread_id'],
                    'turn': {'id': updated.object_id, **updated.payload}})
                tx.commit()
        finally:
            self.active.discard(turn_id)
            if execute:
                state.external_execution_host.release_server_memory(values['request']['turn_id'])
