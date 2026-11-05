from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from backend.recognition import RecognitionConflict, WorkScope
from backend.security.audited_records import AuditedRecordStore
from core.ai_kernel.product_admission import ProductAdmissionItem, bundle_from_payload, validate_bundle
from ..constraints import ProjectConstraintService
from ..transaction_records import TransactionRecords
from ..workbench_identity_binding import execution_owner
from .policies import get


KIND = 'project-constraints-v1'
ACTIVATIONS = 'v2_project_constraint_admission'


def constraint_markers(request):
    return [ref for ref in request['input']['refs'] if ref.get('kind') == 'session_event'
            and urlsplit(ref.get('uri', '')).path.startswith('/project-constraints/')]


class ProjectConstraintInput:
    def __init__(self, application, records, documents, runtime_root):
        self.application, self.records, self.documents = application, records, documents
        self.root = Path(runtime_root).resolve(strict=True)
        self.physical = self._physical()
        self.installed_at = self._activation_boundary()

    def _physical(self):
        path = Path(self.records.database_path).resolve(strict=True)
        file, directory = path.stat(), path.parent.stat()
        return {'path': str(path), 'file': [file.st_dev, file.st_ino],
                'directory': [directory.st_dev, directory.st_ino]}

    def _assert_resources(self):
        state = self.application.state
        if (state.recognition_records is not self.records or state.recognition_documents is not self.documents
                or Path(state.recognition_runtime_root).resolve(strict=True) != self.root
                or state.recognition_models.records is not self.records
                or Path(state.recognition_models.root).resolve(strict=True) != self.root
                or self._physical() != self.physical):
            raise RecognitionConflict('project constraints owner changed')

    def _activation_boundary(self):
        self._assert_resources()
        state = self.application.state
        if isinstance(self.records, AuditedRecordStore):
            if (getattr(state, 'server_user_id', None) != self.records.user_id
                    or self.records.namespace != self.documents.namespace_id):
                raise RecognitionConflict('project constraints owner changed')
            owner = {'owner_user_id': self.records.user_id, 'namespace_id': self.documents.namespace_id,
                     'origin_kind': 'personal'}
        else:
            owner = execution_owner(self.application, self.records, self.documents)
        # 生效边界属于逻辑空间，随整库备份保留；实例重建与文件替换不授予旧轮资格。
        with self.records.begin() as tx:
            row = tx.read(ACTIVATIONS, KIND)
            if row is None:
                activated_at = datetime.now(timezone.utc).isoformat()
                row = tx.put(ACTIVATIONS, KIND, {'version': KIND, 'owner': owner,
                    'activated_at': activated_at}, expected_revision=0)
                view = TransactionRecords(tx)
                for execution in tx.list('v2_task_executions'):
                    value = execution.payload
                    public = tx.read('v2_turns', execution.object_id)
                    request = value.get('request')
                    if (value.get('started') is not False or value.get('executor')
                            or public is None or public.payload.get('intent') != 'do'
                            or public.payload.get('receipt', {}).get('do', {}).get('state')
                            not in {'running', 'preparing', 'researching'}
                            or not isinstance(request, dict) or not self.eligible(request)
                            or request['desired_outcome'] != 'project.task' or constraint_markers(request)):
                        continue
                    if self._prepared(request, view).object_id != execution.object_id:
                        raise RecognitionConflict('project constraints prepared request changed')
                    tx.put(ACTIVATIONS, self._legacy_key(request),
                        self._legacy_binding(request, public, owner), expected_revision=0)
            if (row.revision != 1 or set(row.payload) != {'version', 'owner', 'activated_at'}
                    or row.payload['version'] != KIND or row.payload['owner'] != owner):
                raise RecognitionConflict('project constraints activation changed')
            activated_at = datetime.fromisoformat(row.payload['activated_at'])
            if activated_at.tzinfo is None or activated_at.utcoffset().total_seconds() != 0:
                raise RecognitionConflict('project constraints activation time unavailable')
            tx.commit()
        return activated_at

    @staticmethod
    def _legacy_key(request):
        from ..turn_routing import _revision
        return 'prepared-' + _revision({'turn_id': request['turn_id']})

    @staticmethod
    def _legacy_binding(request, public, owner):
        from ..turn_routing import _revision
        return {'version': KIND, 'owner': owner, 'public_turn_id': public.object_id,
                'native_turn_id': request['turn_id'], 'request_revision': _revision(request),
                'created_at': public.payload.get('created_at')}

    def _legacy_prepared(self, request, view, owner):
        row = view.read(ACTIVATIONS, self._legacy_key(request))
        if row is None:
            return False
        self._prepared(request, view)
        public = view.read('v2_turns', request['session_id'].removeprefix('session-'))
        return row.revision == 1 and row.payload == self._legacy_binding(request, public, owner)

    def _owner(self):
        self._assert_resources()
        return execution_owner(self.application, self.records, self.documents)

    @staticmethod
    def eligible(request):
        kind, version = request['desired_outcome'], request['execution_policy']['template_version']
        return kind == 'project.answer' and version == 2 or kind == 'project.task' and version == 3

    def snapshot(self, project, *, reader=None):
        owner = self._owner()
        if reader is self.records:
            reader = None
        if reader is not None and Path(reader.database_path).resolve(strict=True) != Path(self.records.database_path).resolve(strict=True):
            raise RecognitionConflict('project constraints reader changed')
        context = self.records.begin_read() if reader is None else nullcontext(reader)
        with context as transaction:
            view = transaction if isinstance(transaction, TransactionRecords) else TransactionRecords(transaction)
            rows = ProjectConstraintService(view).active(WorkScope('local-user', project))
            items = [{key: deepcopy(row[key]) for key in
                      ('id', 'revision', 'content', 'valid_from', 'valid_until')} for row in rows]
            if self._owner() != owner:
                raise RecognitionConflict('project constraints owner changed')
        return {'version': 'project-constraints@1', 'owner': owner, 'physical': deepcopy(self.physical),
                'project_id': project, 'items': items, 'prompt': get('constraint_scope', version='@1')().prompt(items)}

    def augment(self, request, store, items):
        if not self.eligible(request) or any(item.kind == 'material-origin-v1' for item in items):
            return tuple(items)
        if any(item.kind == KIND for item in items):
            raise RecognitionConflict('project constraints already prepared')
        if getattr(self.application.state, 'ai_turn_store', None) is not store:
            raise RecognitionConflict('project constraints native store changed')
        owner = self._owner()
        project, identity = request['scope']['project_id'], request['turn_id']
        scope = {**owner, 'turn_id': identity, 'project_id': project}
        reference = {'kind': 'session_event', 'object_id': identity,
                     'uri': f"crp://{owner['namespace_id']}/project-constraints/{identity}"}
        return (*items, ProductAdmissionItem(KIND, reference, scope,
            {'native_turn_id': identity, 'bundle': self.snapshot(project)}))

    def _prepared(self, request, reader):
        public_id = request['session_id'].removeprefix('session-')
        execution = reader.read('v2_task_executions', public_id)
        public = reader.read('v2_turns', public_id)
        if (request['desired_outcome'] != 'project.task' or execution is None or public is None
                or execution.payload.get('request') != request or public.payload.get('intent') != 'do'
                or execution.payload.get('project_id') != request['scope']['project_id']
                or public.payload.get('project_id') != request['scope']['project_id']
                or public.payload.get('receipt', {}).get('do', {}).get('kernel_turn_id') != request['turn_id']):
            raise RecognitionConflict('project constraints prepared request unavailable')
        return execution

    def validate(self, request, *, reader=None):
        if not self.eligible(request):
            return None
        owner = self._owner()
        if reader is self.records:
            reader = None
        store = getattr(self.application.state, 'ai_turn_store', None)
        native = store.get_request(request['turn_id']) if store is not None else None
        markers = constraint_markers(request)
        shared = any(isinstance(ref.get('uri'), str) and ref['uri'].startswith('crp://material-origin/')
            for ref in request['input']['refs'])
        context = self.records.begin_read() if reader is None else nullcontext(reader)
        with context as transaction:
            view = transaction if isinstance(transaction, TransactionRecords) else TransactionRecords(transaction)
            if Path(view.database_path).resolve(strict=True) != Path(self.records.database_path).resolve(strict=True):
                raise RecognitionConflict('project constraints reader changed')
            if native is not None:
                if native != request:
                    # 原生接纳可补充绑定；仅原公共任务保存的准备请求能够沿用该资格。
                    added_bindings = {'agent_binding', 'agent_policy_binding'} - request.keys()
                    if {key: value for key, value in native.items() if key not in added_bindings} != request:
                        raise RecognitionConflict('project constraints native request changed')
                    self._prepared(request, view)
                admitted = store.restore_admission(native) if markers or shared else None
                accepted = [event for event in store.events_after(request['turn_id']) if event['type'] == 'turn.accepted']
                legacy_at = accepted[0]['occurred_at'] if len(accepted) == 1 else None
            else:
                prepared = self._prepared(request, view)
                legacy_at = None
                admitted = bundle_from_payload(prepared.payload['admission_bundle']) if markers or shared else None
                if admitted is not None:
                    validate_bundle(request, admitted)
            has_origin = shared or admitted is not None and any(item.kind == 'material-origin-v1' for item in admitted.items)
            source_admission = getattr(self, '_shared_origin', None)
            if has_origin and source_admission is None:
                resolver = getattr(self, 'shared_origin_resolver', None)
                if (not callable(resolver) or getattr(resolver, '__self__', None)
                        is not getattr(self.application.state, 'shared_query_factory', None)):
                    raise RecognitionConflict('project constraints shared factory unavailable')
                return resolver(self.application, native or request, admitted, reader=view)
            if source_admission is not None:
                if admitted is None:
                    raise RecognitionConflict('material_origin_binding_missing')
                source_admission._bind_bundle(native or request, admitted)
            if not markers:
                # 无标记只保留实际旧轮；新请求须经统一接纳准备，不能降为旧路径。
                accepted_before_activation = legacy_at is not None and datetime.fromisoformat(legacy_at) < self.installed_at
                if not accepted_before_activation and not self._legacy_prepared(request, view, owner):
                    raise RecognitionConflict('project constraints admission unavailable')
                return None
            if admitted is None or len(markers) != 1:
                raise RecognitionConflict('project constraints admission unavailable')
            selected = [item for item in admitted.items if item.kind == KIND]
            scope = {**owner, 'turn_id': request['turn_id'], 'project_id': request['scope']['project_id']}
            reference = {'kind': 'session_event', 'object_id': request['turn_id'],
                         'uri': f"crp://{owner['namespace_id']}/project-constraints/{request['turn_id']}"}
            if (len(selected) != 1 or selected[0].scope != scope or selected[0].ref != reference
                    or markers != [reference] or set(selected[0].payload) != {'native_turn_id', 'bundle'}
                    or selected[0].payload['native_turn_id'] != request['turn_id']):
                raise RecognitionConflict('project constraints admission changed')
            snapshot = selected[0].payload['bundle']
            if snapshot != self.snapshot(request['scope']['project_id'], reader=view):
                raise RecognitionConflict('project constraints changed; preview again')
            return deepcopy(snapshot)

    def messages(self, request, messages, *, reader=None):
        snapshot = self.validate(request, reader=reader)
        result = deepcopy(messages)
        if snapshot is None or not snapshot['prompt']:
            return result
        block = {'role': 'system', 'content': snapshot['prompt']}
        prefix = get('constraint_scope', version='@1')().instruction_prefix
        existing = [row for row in result if row.get('role') == 'system'
                    and isinstance(row.get('content'), str) and row['content'].startswith(prefix)]
        if existing:
            if existing != [block]:
                raise RecognitionConflict('project constraints input changed')
            return result
        return [block, *result]

    def validate_plan(self, plan):
        snapshot = plan.get('_project_constraints')
        if snapshot is not None and snapshot != self.snapshot(plan['project_id']):
            raise RecognitionConflict('project constraints changed; preview again')

    def record_input(self, root_request, request, messages, store, *, logical_input):
        self.validate(root_request)
        if not constraint_markers(root_request):
            return None
        if (store is not getattr(self.application.state, 'ai_turn_store', None)
                or request['scope']['project_id'] != root_request['scope']['project_id']
                or store.get_request(request['turn_id']) != request):
            raise RecognitionConflict('project constraints model request unavailable')
        from ..turn_routing import _revision
        kind = 'project-constraints-model-input-' + _revision(logical_input)
        return store.get_or_create_immutable_payload(request['turn_id'], kind,
            {'turn_id': request['turn_id'], 'root_turn_id': root_request['turn_id'],
             'messages': deepcopy(messages), 'constraints_ref': constraint_markers(root_request)[0]})

    def validate_public_answer(self, turn_id, *, reader):
        store = getattr(self.application.state, 'ai_turn_store', None)
        request = store.get_request(turn_id) if store is not None else None
        if request is None:
            raise RecognitionConflict('project constraints native request unavailable')
        return self.validate(request, reader=reader)

    def input_budget(self, models, messages, *, revision, max_tokens, response_model=None, limit=None):
        from .task_attachment_input import input_tokens, _budget
        limits = models.generation_budget_limits(expected_revision=revision, max_tokens=max_tokens)
        budget = _budget(limits)
        if limit is not None:
            if type(limit) is not int or limit < 0:
                raise RecognitionConflict('project constraints input budget unavailable')
            budget = min(budget, limit)
        if input_tokens(messages, response_model) > budget:
            raise RecognitionConflict('project constraints exceed input budget')
