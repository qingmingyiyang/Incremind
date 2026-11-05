import re

from fastapi import APIRouter, HTTPException, Request
from core.storage_provider.connection_scope import with_connection_scope

from ..workspace_contracts import _json, _project
from .external_context import ExternalContextError
from .external_execution import RESULT_KIND, RECEIPT_KIND
from .external_final_text import KIND as FINAL_TEXT_KIND, qualified_final_text
from .external_executor_plugins import EXECUTOR_CLIENTS
from .external_output_contract import delivery_policy_versions
from .external_runner import ARCHIVE, _archive_fields


class TaskCitationError(ExternalContextError):
    pass


def explicit_citations(text, mapping):
    if not isinstance(text, str) or len(text) > 200000 or not isinstance(mapping, dict):
        raise TaskCitationError('external_task_citations_invalid')
    return list(dict.fromkeys(identity for identity in re.findall(r'\[(M[1-9][0-9]*)\]', text)
        if identity in mapping))


class TaskCitations:
    def __init__(self, task, product_id, project_id, *, owner=None):
        self.task, self.records = task, task.records
        self.state = task.application.state
        self.context = self.state.external_context
        self.product_id, self.project_id, self.owner = product_id, project_id, owner
        row = self.records.read('v2_task_executions', product_id)
        if row is None or row.payload.get('project_id') != project_id:
            raise HTTPException(404, 'external_task_not_found')
        self.values = dict(row.payload)
        self.request = self.values['request']
        self.identity = self.request['turn_id']
        self.turns = self.state.ai_turn_store
        self.archive = self.turns.get_immutable_payload(self.identity, ARCHIVE)
        self.native = self.turns.get_immutable_payload(self.identity, RESULT_KIND)
        self.proof = self.turns.get_immutable_payload(self.identity, RECEIPT_KIND)
        self.final = self.turns.get_immutable_payload(self.identity, FINAL_TEXT_KIND)
        if (self.context.records is not self.records or self.archive is None
                or not _archive_fields(self.archive[1])
                or self.request['scope'] != {'kind': 'project', 'project_id': project_id, 'series_id': None}
                or self.request['desired_outcome'] != 'project.task'
                or self.request['capability_request']['mode'] != 'execute_exact_v1'
                or self.request['capability_request']['capability_id'] != 'external.task.execute'
                or self.request['capability_request']['arguments'] != {'binding_ref': self.archive[0]}):
            raise TaskCitationError('external_task_citations_binding_invalid')
        self.delivery = self.archive[1]['delivery']
        ref, memory, outcome = self.context._completed(self.delivery['turn_id'])
        if (self.delivery['owner_id'] != self.context.owner_id
                or self.delivery['project_id'] != project_id or self.delivery['immutable_ref'] != ref
                or self.delivery['outcome_ref'] != outcome or self.delivery['mapping'] != memory['mapping']
                or self.delivery['client'] != EXECUTOR_CLIENTS[self.values['executor']]
                or self.delivery['refs'] != self.request['input']['refs']
                or self.delivery['privacy'] != self.request['privacy']
                or self.delivery['policy_versions'] != delivery_policy_versions(self.request)):
            raise TaskCitationError('external_task_citations_binding_invalid')
        self.memory = (ref, memory, outcome)
        with self.records.begin() as tx:
            final = self._validate(tx)
        # 仅使用物理执行关闭后冻结的最终回复；缺证明时不推断引用。
        self.ids = explicit_citations(final['text'], self.delivery['mapping']) if final is not None else None

    def _validate(self, reader):
        try:
            return self._bound(reader)
        except (KeyError, TypeError, ValueError):
            raise TaskCitationError('external_task_citations_binding_invalid') from None

    def _bound(self, reader):
        execution = reader.read('v2_task_executions', self.product_id)
        product = reader.read('v2_turns', self.product_id)
        if (execution is None or product is None or not execution.payload.get('started')
                or execution.payload.get('owner') != self.owner
                or any(execution.payload.get(key) != self.values.get(key) for key in
                    ('request', 'executor', 'project_id', 'task_text', 'scene'))
                or self.request['session_id'] != 'session-' + self.product_id
                or product.payload.get('project_id') != self.project_id or product.payload.get('intent') != 'do'
                or product.payload.get('user_text') != self.values['task_text']
                or self.state.external_context is not self.context or self.state.ai_turn_store is not self.turns
                or self.turns.get_immutable_payload(self.identity, ARCHIVE) != self.archive
                or self.turns.get_immutable_payload(self.identity, RESULT_KIND) != self.native
                or self.turns.get_immutable_payload(self.identity, RECEIPT_KIND) != self.proof
                or self.turns.get_immutable_payload(self.identity, FINAL_TEXT_KIND) != self.final
                or self.context._completed(self.delivery['turn_id']) != self.memory):
            raise TaskCitationError('external_task_citations_binding_invalid')
        receipt = product.payload['receipt']['do']
        if (receipt.get('kernel_turn_id') != self.identity or receipt.get('executor') != self.values['executor']
                or receipt.get('state') != ('running' if self.owner is not None else 'done')):
            raise TaskCitationError('external_task_citations_owner_changed')
        _, _, completed = self.task._observed(self.values, self.product_id, reader=reader)
        if not completed:
            raise TaskCitationError('external_task_citations_not_completed')
        if self.owner is None:
            # 出生证明沿原任务草稿 reader；后续编辑不改变最初的原生结果。
            from backend.recognition import WorkScope
            from backend.recognition.product_draft_dependencies import product_draft_source
            bound = product_draft_source(reader, WorkScope(self.context.owner_id, self.project_id),
                receipt.get('document_id'), 1, turn_id=self.identity)
            if bound is None or bound.public_turn_id != self.product_id or bound.request != self.request:
                raise TaskCitationError('external_task_citations_binding_invalid')
        return qualified_final_text(reader, self.turns, self.request, self.product_id, self.values['executor'],
            owner_id=self.context.owner_id, product_owner=self.owner)

    def _view(self, state, registered):
        return {'product_id': self.product_id, 'project_id': self.project_id,
            'state': state, 'ids': list(self.ids) if self.ids is not None else None, 'registered_ids': registered}

    def read(self):
        try:
            value = self.context.report_use(self.delivery['turn_id'], [], client=self.delivery['client'],
                _validate_binding=self._validate, _read_only=True)
        except ExternalContextError as error:
            if str(error) != 'external_context_unavailable':
                raise
            return self._view('unknown', None)
        registered = value['ids']
        return self._view('not_qualified' if self.ids is None else
            'recorded' if all(identity in registered for identity in self.ids) else 'pending', registered)

    def report(self):
        if self.ids is None:
            return self.read()
        try:
            self.context.report_use(self.delivery['turn_id'], self.ids, client=self.delivery['client'],
                _validate_binding=self._validate)
        except ExternalContextError as error:
            if str(error) != 'external_context_unavailable':
                raise
            return self._view('unknown', None)
        return self.read()


def install_citation_routes(application, *, records, models, workspace):
    router = APIRouter(prefix='/api/v2/external-agent/tasks')

    @with_connection_scope
    def operation(request, product_id, project_id, *, write=False):
        if request.app is not application or application.state.recognition_records is not records:
            raise HTTPException(403, 'external_task_citations_scope_invalid')
        state = application.state
        if state.deployment.mode == 'server':
            from backend.security.user_context import USER_ACCESS
            access = USER_ACCESS.get()
            scope = request.scope.get('state', {})
            if (access is None or scope.get('user_access') is not access
                    or scope.get('server_dispatch_authority') is not state.server_context.auth
                    or state.server_user_id != access.target_user_id
                    or state.server_context.users.root_for(access.target_user_id) != state.recognition_runtime_root):
                raise HTTPException(403, 'external_task_citations_scope_invalid')
            state.server_context.users.authorize_claim(access.caller, access.target_user_id)
        workspace.query.answer_turns._runtime()
        from .external_task_do import ExternalTaskDo
        task = ExternalTaskDo(application, records, models, state.task_drafts, workspace.query)
        try:
            service = TaskCitations(task, _project(product_id), _project(project_id))
            return service.report() if write else service.read()
        except ExternalContextError:
            raise HTTPException(409, 'external_task_citations_unavailable') from None

    @router.get('/{product_id}/citations')
    def get_citations(product_id: str, project_id: str, request: Request):
        return operation(request, product_id, project_id)

    @router.post('/{product_id}/citations')
    async def retry_citations(product_id: str, request: Request):
        body = await _json(request)
        if set(body) != {'project_id'}:
            raise HTTPException(400, 'external_task_citations_invalid')
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(operation, request, product_id, body['project_id'], write=True)

    application.include_router(router)
