from backend.recognition import RecognitionConflict
from core.ai_kernel.product_admission import bundle_from_payload
from ..kernel.receipt_projection import frozen_public_turn_requests
from ..kernel.route_source_snapshot import native_source_snapshot
from ..transaction_records import TransactionRecords
from .material_origin_admission import read_origin_item
from .material_origins import ORIGIN_KIND
from .shared_scopes import _prepared_task_without_native


def logical_scope(value):
    if not isinstance(value, str) or value not in {'personal', 'shared'}:
        raise ValueError('invalid_scope_kind')
    return value


class TurnLogicalScope:
    def __init__(self, application):
        state = application.state
        self.records = state.recognition_records
        self.root = state.recognition_runtime_root
        self.namespace = state.recognition_documents.namespace_id
        self.owner_user_id = getattr(state, 'server_user_id', 'local-user')

    def _unaccepted(self, row, actual):
        example = actual.read('v2_route_examples', row.payload.get('parent_turn_id') or row.object_id)
        if (example is not None and example.payload.get('origin', {}).get('kind') == 'shared'
                or actual.read('v2_task_executions', row.object_id) is not None):
            raise RecognitionConflict('turn_logical_scope_changed')
        project = row.payload['project_id']
        if project not in {'default', 'inbox', 'me'} and actual.read('v2_projects', project) is None:
            raise RecognitionConflict('turn_logical_scope_changed')
        # 旧个人正文按真实个人项目读取；此结果不证明共享来源，也不授予原生继续执行资格。
        return 'personal'

    def _origin(self, request, bundle, identity, *, shared_leases=None):
        selected = [] if bundle is None else [item for item in bundle.items if item.kind == ORIGIN_KIND]
        if not selected:
            return 'personal'
        if len(selected) != 1:
            raise RecognitionConflict('turn_logical_scope_changed')
        item = selected[0]
        personal = item.payload.get('execution')
        expected = {'owner_user_id': self.owner_user_id, 'namespace_id': self.namespace,
            'namespace': 'personal/' + self.owner_user_id + '/' + self.namespace}
        if (not isinstance(personal, dict) or any(personal.get(key) != value for key, value in expected.items())
                or identity.get('owner_user_id') != self.owner_user_id
                or identity.get('namespace_id') != self.namespace):
            raise RecognitionConflict('turn_logical_scope_changed')
        lease, auxiliary = read_origin_item(request, item, actor_user_id=item.payload.get('actor_user_id'), personal=personal)
        if auxiliary is not None or lease.project_id != request['scope']['project_id']:
            raise RecognitionConflict('turn_logical_scope_changed')
        if shared_leases is not None:
            shared_leases.append(lease)
        return 'shared'

    def request(self, request):
        snapshot = native_source_snapshot(self.records, request['turn_id'])
        if snapshot['request'] != request:
            raise RecognitionConflict('turn_logical_scope_changed')
        bundle = snapshot['admission']
        bindings = [] if bundle is None else [item.payload for item in bundle.items
            if item.kind == 'workbench-steer-binding-v1']
        return self._origin(request, bundle, bindings[0] if len(bindings) == 1 else {})

    def _incomplete_multi(self, row, actual, children, missing):
        from ..route_correction_scopes import correction_parent_id, planned_child_scope_binding

        value = row.payload
        parts = value['receipt']['parts']
        thread = actual.read('v2_threads', value['thread_id'])
        if (not isinstance(parts, list) or len(parts) != len(children) or thread is None
                or thread.payload.get('project_id') != value['project_id']
                or any(not isinstance(parts[index], dict) or parts[index].get('index') != index
                    or parts[index].get('receipt') != {}
                    or parts[index].get('state') not in {'waiting', 'running', 'failed', 'not_started'}
                    for index in missing)):
            raise RecognitionConflict('turn_logical_scope_incomplete_changed')
        if correction_parent_id(row.object_id):
            kinds = set()
            personal = f'personal/{self.owner_user_id}/{self.namespace}'
            for index in missing:
                proof = planned_child_scope_binding(actual, row.object_id, children[index])
                operation = actual.read('v2_route_correction_operations', proof['operation_id'])
                part, planned = parts[index], operation.payload['plan']['parts'][index]
                origin = operation.payload['source_origin']
                if (proof['part_index'] != index or operation.payload['status'] not in {'running', 'partial', 'unknown'}
                        or origin['project_id'] != value['project_id'] or part.get('turn_id') != children[index]
                        or part.get('project_id') != proof['target']['project_id']
                        or any(part.get(field) != planned[field] for field in ('intent', 'span', 'instruction', 'depends_on'))):
                    raise RecognitionConflict('turn_logical_scope_incomplete_changed')
                for identity in proof['selected_source_ids']:
                    member = actual.read('v2_turns', identity)
                    if member is None or member.payload['intent'] == 'multi':
                        raise RecognitionConflict('turn_logical_scope_incomplete_changed')
                    leases = []
                    kind = self.read(member, reader=actual, shared_leases=leases)
                    if (kind != origin['kind'] or (kind == 'personal' and origin['namespace'] != personal)
                            or (kind == 'shared' and (not leases or any(lease.namespace != origin['namespace'] for lease in leases)))):
                        raise RecognitionConflict('turn_logical_scope_incomplete_changed')
                    kinds.add(kind)
            if len(kinds) != 1:
                raise RecognitionConflict('turn_logical_scope_mixed')
            return kinds.pop()

        from .attachments import expand_parts
        from .route import RoutePlan
        from .turn_message_order import ACCEPTANCES

        accepted = actual.read(ACCEPTANCES, row.object_id)
        if accepted is not None and (accepted.payload['public_id'] != row.object_id
                or accepted.payload['thread_id'] != value['thread_id']
                or accepted.payload['requested_project_id'] != value['project_id']
                or accepted.payload['body'].get('scope_kind', 'personal') != 'personal'):
            raise RecognitionConflict('turn_logical_scope_incomplete_changed')
        plan = RoutePlan.model_validate(value.get('route_plan'))
        planned = [part for part, _ in expand_parts(plan.parts, value.get('attachment_item_ids', []))]
        if (len(planned) != len(parts) or value['receipt']['route'] != {
                field: getattr(plan, field) for field in ('mode', 'usage', 'egress_receipt_id')}
                or any(any(part.get(field) != getattr(expected, field)
                    for field in ('intent', 'span', 'instruction', 'depends_on')) for part, expected in zip(parts, planned))
                or any(parts[index].get('turn_id') is not None or parts[index].get('project_id') is not None for index in missing)):
            raise RecognitionConflict('turn_logical_scope_incomplete_changed')
        return self._unaccepted(row, actual)

    def read(self, row, *, reader=None, shared_leases=None):
        if reader is None:
            with self.records.begin_read() as transaction:
                current = TransactionRecords(transaction)
                observed = current.read('v2_turns', row.object_id)
                if observed != row:
                    raise RecognitionConflict('turn_logical_scope_changed')
                return self.read(observed, reader=current, shared_leases=shared_leases)
        actual = reader
        if row.payload['intent'] == 'multi':
            identities = row.payload['part_turn_ids']
            if (not isinstance(identities, list) or any(not isinstance(identity, str) or identity == row.object_id
                    for identity in identities) or len(identities) != len(set(identities))):
                raise RecognitionConflict('turn_logical_scope_changed')
            children = [actual.read('v2_turns', identity) for identity in identities]
            missing = [index for index, child in enumerate(children) if child is None]
            kinds = {self.read(child, reader=reader, shared_leases=shared_leases) for child in children if child is not None}
            if missing:
                kinds.add(self._incomplete_multi(row, actual, identities, missing))
            if len(kinds) != 1:
                raise RecognitionConflict('turn_logical_scope_mixed')
            return kinds.pop()
        if row.payload['intent'] == 'remember':
            from .shared_workbench_remember import read_shared_remember
            binding = read_shared_remember(actual, row, owner_user_id=self.owner_user_id, namespace_id=self.namespace)
            return 'shared' if binding is not None else 'personal'
        if row.payload['intent'] not in {'ask', 'do'}:
            return 'personal'
        if row.payload['intent'] == 'do' and _prepared_task_without_native(self, actual, row):
            values = actual.read('v2_task_executions', row.object_id).payload
            bundle = bundle_from_payload(values['admission_bundle'])
            identity = next(item.payload for item in bundle.items if item.kind == 'workbench-steer-binding-v1')
            return self._origin(values['request'], bundle, identity)
        native_id = (row.object_id if row.payload['intent'] == 'ask'
            else row.payload.get('receipt', {}).get('do', {}).get('kernel_turn_id'))
        if native_id is None or native_source_snapshot(self.records, native_id, reader=reader)['request'] is None:
            return self._unaccepted(row, actual)
        entries = frozen_public_turn_requests(self.records, self.root, row.object_id, reader=reader)
        kinds = set()
        for entry in entries:
            snapshot = native_source_snapshot(self.records, entry['native_request']['turn_id'], reader=reader)
            if snapshot['request'] != entry['native_request']:
                raise RecognitionConflict('turn_logical_scope_changed')
            kind = self._origin(snapshot['request'], snapshot['admission'], entry['identity'],
                shared_leases=shared_leases)
            if kind == 'shared' and not entry['qualified']:
                raise RecognitionConflict('turn_logical_scope_changed')
            kinds.add(kind)
        if not kinds:
            return 'personal'
        if len(kinds) != 1:
            raise RecognitionConflict('turn_logical_scope_mixed')
        return kinds.pop()
